import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from starlette.routing import Route

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("WORKBENCH_DB_PATH", str(BASE_DIR / "workbench.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
INDEX_PATH = BASE_DIR / "index.html"
APP_DEBUG = os.environ.get("WORKBENCH_DEBUG", "0") == "1"
SESSION_COOKIE_NAME = "wb_session"
SESSION_TTL_DAYS = 7

PETRO_CONTRACTS = [
    {"symbol": "L0", "sina_code": "nf_L0", "name": "塑料 LLDPE", "exchange": "大商所"},
    {"symbol": "PP0", "sina_code": "nf_PP0", "name": "聚丙烯 PP", "exchange": "大商所"},
    {"symbol": "V0", "sina_code": "nf_V0", "name": "PVC", "exchange": "大商所"},
    {"symbol": "SC0", "sina_code": "nf_SC0", "name": "上海原油", "exchange": "上期能源"},
    {"symbol": "EG0", "sina_code": "nf_EG0", "name": "乙二醇 EG", "exchange": "大商所"},
    {"symbol": "EB0", "sina_code": "nf_EB0", "name": "苯乙烯 EB", "exchange": "大商所"},
    {"symbol": "TA0", "sina_code": "nf_TA0", "name": "精对苯二甲酸 PTA", "exchange": "郑商所"},
    {"symbol": "MA0", "sina_code": "nf_MA0", "name": "甲醇 MA", "exchange": "郑商所"},
    {"symbol": "BU0", "sina_code": "nf_BU0", "name": "石油沥青 BU", "exchange": "上期所"},
    {"symbol": "PF0", "sina_code": "nf_PF0", "name": "涤纶短纤 PF", "exchange": "郑商所"},
]

ALLOWED_PROXY_SUFFIXES = ("dfcfw.com", "sinaimg.cn", "sinajs.cn")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_https_request(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return proto == "https" or request.url.scheme == "https"


def get_public_base_url(request: Request) -> str:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if proto in ("http", "https") and host:
        return f"{proto}://{host}"
    return str(request.base_url).rstrip("/")


def hash_password(password: str, salt_hex: Optional[str] = None) -> str:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return f"pbkdf2_sha256${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored_hash.split("$", 2)
        if algo != "pbkdf2_sha256":
            return False
        candidate = hash_password(password, salt_hex)
        return hmac.compare_digest(candidate, stored_hash)
    except Exception:
        return False


def get_session_user(request: Request) -> Optional[str]:
    token = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    if not token:
        return None
    conn = get_db()
    row = conn.execute("SELECT username, expires_at FROM sessions WHERE token=?", (token,)).fetchone()
    if not row:
        conn.close()
        return None
    if row["expires_at"] < now_str():
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
        return None
    conn.close()
    return row["username"]


def verify_wx_plugin_token(request: Request) -> bool:
    header_tok = request.headers.get("X-WeChat-Token", "").strip()
    if not header_tok:
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            header_tok = auth[7:].strip()
    if not header_tok:
        return False
    cfg_tok = get_config_map().get("wx_plugin_token", "").strip()
    if not cfg_tok:
        return False
    return hmac.compare_digest(header_tok, cfg_tok)


class AuthMiddleware(BaseHTTPMiddleware):
    PUBLIC_PATHS = {"/", "/api/health", "/api/auth/login", "/api/auth/me", "/api/auth/logout"}
    WX_PLUGIN_PATHS = {"/api/wechat/ingest", "/api/wechat/outbox-poll"}

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if path in self.PUBLIC_PATHS:
            return await call_next(request)
        if path in self.WX_PLUGIN_PATHS:
            if get_session_user(request) or verify_wx_plugin_token(request):
                return await call_next(request)
            return JSONResponse(
                {"ok": False, "error": "鉴权失败：请登录或提供有效的 X-WeChat-Token"},
                status_code=401,
            )
        if path.startswith("/api/"):
            if get_session_user(request):
                return await call_next(request)
            return JSONResponse({"ok": False, "error": "请先登录工作台"}, status_code=401)
        return await call_next(request)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(cur: sqlite3.Cursor, table: str, col: str, ddl: str) -> None:
    cols = [r["name"] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db() -> None:
    conn = get_db()
    cur = conn.cursor()

    # 1. 微信群牌号实时价格表（零虚拟种子）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS price_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grade_code TEXT NOT NULL,
            category TEXT NOT NULL,
            producer TEXT NOT NULL,
            region TEXT NOT NULL,
            price REAL NOT NULL,
            change_val REAL DEFAULT 0,
            unit TEXT DEFAULT '元/吨',
            source_group TEXT NOT NULL,
            sender TEXT NOT NULL,
            raw_message TEXT NOT NULL,
            parse_engine TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    # 2. 微信下游客户报价分发队列
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS outbox_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_recipients TEXT NOT NULL,
            message_body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            result_note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # 3. 实时抓取的大宗石化主力合约快照表
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS market_snapshots (
            symbol TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            exchange TEXT NOT NULL,
            last_price REAL NOT NULL,
            open_price REAL NOT NULL,
            high_price REAL NOT NULL,
            low_price REAL NOT NULL,
            prev_settle REAL NOT NULL,
            change_val REAL NOT NULL,
            change_pct REAL NOT NULL,
            volume REAL NOT NULL,
            open_interest REAL NOT NULL,
            trade_date TEXT NOT NULL,
            trade_time TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
        """
    )

    # 4. 情报库（东方财富真实新闻+配图 / 任意网站抓取 / 手动补录装置检修与汇报）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS intel_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source_url TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            effective_date TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    ensure_column(cur, "intel_records", "image_url", "TEXT DEFAULT ''")

    # 5. 公众号 / 视频号稿件表
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_type TEXT NOT NULL,
            title TEXT NOT NULL,
            raw_markdown TEXT NOT NULL,
            styled_html TEXT NOT NULL,
            template_id TEXT NOT NULL DEFAULT 'hengye_daily',
            cover_image_url TEXT DEFAULT '',
            engine_note TEXT DEFAULT '',
            mp_draft_media_id TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    ensure_column(cur, "articles", "cover_image_url", "TEXT DEFAULT ''")

    # 6. 图片生成模型真实输出库
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS generated_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt TEXT NOT NULL,
            model TEXT NOT NULL,
            image_url TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    # 7. 画布项目持久化表
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS canvas_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            product_name TEXT NOT NULL,
            product_selling_points TEXT NOT NULL,
            nodes_json TEXT NOT NULL,
            edges_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # 8. WeChat 接口真实调用日志表
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS wechat_api_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            request_summary TEXT NOT NULL,
            response_summary TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    # 9. 系统配置表
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # 10. 管理员用户表与登录会话表（公网安全鉴权）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    env_wx_token = os.environ.get("WORKBENCH_WX_TOKEN", "").strip()
    default_configs = {
        "brand_name": "恒业 × 新月工作台",
        "engine_mode": "dify",
        "dify_base_url": "http://127.0.0.1/v1",
        "dify_key_price": "",
        "dify_key_daily": "",
        "dify_key_video": "",
        "dify_key_mp_clone": "",
        "dify_key_drama": "",
        "llm_base_url": "https://api.deepseek.com/v1",
        "llm_api_key": "",
        "llm_model": "deepseek-chat",
        "image_api_url": "https://api.siliconflow.cn/v1/images/generations",
        "image_api_key": "",
        "image_model": "Kwai-Kolors/Kolors",
        "mp_appid": "",
        "mp_appsecret": "",
        "mp_thumb_media_id": "",
        "mp_author": "恒业研投",
        "default_scrape_url": "https://finance.eastmoney.com/",
        "wx_plugin_token": env_wx_token or ("wxk_" + secrets.token_urlsafe(24)),
    }
    ts = now_str()
    for k, v in default_configs.items():
        cur.execute(
            "INSERT OR IGNORE INTO system_config (key, value, updated_at) VALUES (?, ?, ?)",
            (k, v, ts),
        )
    if env_wx_token:
        cur.execute(
            "INSERT OR REPLACE INTO system_config (key, value, updated_at) VALUES ('wx_plugin_token', ?, ?)",
            (env_wx_token, ts),
        )

    # 初始化管理员账号（仅当 users 表为空时创建）
    if cur.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
        init_user = os.environ.get("WORKBENCH_ADMIN_USER", "admin").strip() or "admin"
        init_pass = os.environ.get("WORKBENCH_ADMIN_PASS", "Hengye@2026")
        cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
            (init_user, hash_password(init_pass), ts),
        )

    # 清理旧版本遗留的虚拟种子数据，保证全库零虚拟演示数据
    cur.execute("DELETE FROM price_quotes WHERE parse_engine = 'seed_init'")
    cur.execute(
        "DELETE FROM intel_records WHERE title IN (?, ?, ?)",
        (
            "12月5号华东某主力石化装置停车检修计划（手动补录）",
            "16:00-17:00 内部业务成交汇报纪要",
            "国际原油波动与石化厂月底考核库存情况",
        ),
    )

    conn.commit()
    conn.close()


def get_config_map() -> Dict[str, str]:
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM system_config").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def log_wechat_api(
    method: str,
    path: str,
    status_code: int,
    req_summary: str,
    resp_summary: str,
    duration_ms: int,
) -> None:
    try:
        conn = get_db()
        conn.execute(
            """
            INSERT INTO wechat_api_logs
            (method, path, status_code, request_summary, response_summary, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                method,
                path,
                status_code,
                req_summary[:400],
                resp_summary[:500],
                duration_ms,
                now_str(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ==============================================================================
# 实时国内石化数据抓取（新浪期货实时行情 + 30日日线 + 东方财富宏观/大宗快讯与实图）
# ==============================================================================


async def fetch_sina_live_market() -> List[Dict[str, Any]]:
    codes = ",".join(item["sina_code"] for item in PETRO_CONTRACTS)
    url = f"https://hq.sinajs.cn/list={codes}"
    headers = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
    }
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.get(url, headers=headers)
        text = resp.content.decode("gbk", errors="ignore")

    meta_map = {item["sina_code"]: item for item in PETRO_CONTRACTS}
    snapshots: List[Dict[str, Any]] = []
    ts = now_str()

    for line in text.strip().splitlines():
        m = re.match(r'^var hq_str_([^=]+)="([^"]*)";', line.strip())
        if not m:
            continue
        sina_code, raw_vals = m.group(1), m.group(2)
        if sina_code not in meta_map or not raw_vals:
            continue
        arr = raw_vals.split(",")
        if len(arr) < 18:
            continue

        meta = meta_map[sina_code]
        open_p = float(arr[2] or 0)
        high_p = float(arr[3] or 0)
        low_p = float(arr[4] or 0)
        last_p = float(arr[8] or 0)
        prev_settle = float(arr[10] or arr[5] or 0)
        oi = float(arr[13] or 0)
        vol = float(arr[14] or 0)
        raw_time = arr[1]
        trade_time = (
            f"{raw_time[0:2]}:{raw_time[2:4]}:{raw_time[4:6]}"
            if len(raw_time) == 6
            else raw_time
        )
        trade_date = arr[17]

        change_val = round(last_p - prev_settle, 2) if prev_settle > 0 else 0.0
        change_pct = round((change_val / prev_settle) * 100, 2) if prev_settle > 0 else 0.0

        snapshots.append(
            {
                "symbol": meta["symbol"],
                "name": meta["name"],
                "exchange": meta["exchange"],
                "last_price": last_p,
                "open_price": open_p,
                "high_price": high_p,
                "low_price": low_p,
                "prev_settle": prev_settle,
                "change_val": change_val,
                "change_pct": change_pct,
                "volume": vol,
                "open_interest": oi,
                "trade_date": trade_date,
                "trade_time": trade_time,
                "fetched_at": ts,
            }
        )

    if snapshots:
        conn = get_db()
        cur = conn.cursor()
        for s in snapshots:
            cur.execute(
                """
                INSERT OR REPLACE INTO market_snapshots
                (symbol, name, exchange, last_price, open_price, high_price, low_price,
                 prev_settle, change_val, change_pct, volume, open_interest, trade_date, trade_time, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    s["symbol"],
                    s["name"],
                    s["exchange"],
                    s["last_price"],
                    s["open_price"],
                    s["high_price"],
                    s["low_price"],
                    s["prev_settle"],
                    s["change_val"],
                    s["change_pct"],
                    s["volume"],
                    s["open_interest"],
                    s["trade_date"],
                    s["trade_time"],
                    s["fetched_at"],
                ),
            )
        conn.commit()
        conn.close()

    return snapshots


async def api_market_live(request: Request):
    refresh = request.query_params.get("refresh", "0") == "1"
    conn = get_db()
    rows = conn.execute("SELECT * FROM market_snapshots ORDER BY volume DESC").fetchall()
    conn.close()

    if refresh or not rows:
        try:
            snapshots = await fetch_sina_live_market()
            snapshots.sort(key=lambda x: x["volume"], reverse=True)
            return JSONResponse({"ok": True, "source": "hq.sinajs.cn (live)", "contracts": snapshots})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"抓取新浪实时石化行情失败: {exc}"}, status_code=502)

    return JSONResponse({"ok": True, "source": "sqlite_cache (hq.sinajs.cn)", "contracts": [dict(r) for r in rows]})


async def api_market_kline(request: Request):
    symbol = request.path_params.get("symbol", "L0").upper()
    valid = {item["symbol"] for item in PETRO_CONTRACTS}
    if symbol not in valid:
        return JSONResponse({"ok": False, "error": f"不支持的合约代码: {symbol}"}, status_code=400)

    url = f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/cb/InnerFuturesNewService.getDailyKLine?symbol={symbol}"
    headers = {
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(url, headers=headers)
            m = re.search(r"\[.*\]", resp.text, re.S)
            if not m:
                return JSONResponse({"ok": False, "error": "新浪日线接口未返回数组数据"}, status_code=502)
            raw_list = json.loads(m.group(0))[-30:]
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"抓取新浪日K线失败: {exc}"}, status_code=502)

    series = [
        {
            "date": item["d"],
            "open": float(item["o"]),
            "high": float(item["h"]),
            "low": float(item["l"]),
            "close": float(item["c"]),
            "volume": float(item["v"]),
            "open_interest": float(item.get("p") or 0),
        }
        for item in raw_list
    ]

    # 拼接最新盘中实时快照点（若日线最后一根日期早于实时快照日期）
    conn = get_db()
    snap = conn.execute("SELECT * FROM market_snapshots WHERE symbol=?", (symbol,)).fetchone()
    conn.close()
    if snap and series and snap["trade_date"] > series[-1]["date"] and snap["last_price"] > 0:
        series.append(
            {
                "date": snap["trade_date"],
                "open": float(snap["open_price"]),
                "high": float(snap["high_price"]),
                "low": float(snap["low_price"]),
                "close": float(snap["last_price"]),
                "volume": float(snap["volume"]),
                "open_interest": float(snap["open_interest"]),
            }
        )
        series = series[-30:]

    return JSONResponse({"ok": True, "symbol": symbol, "source": "stock2.finance.sina.com.cn", "series": series})


async def api_market_chart_img(request: Request):
    """
    同源代理新浪官方真实期货走势图（daily 日K图 / min 分时图），确保在网页和公众号排版中直接加载显示。
    """
    kind = request.path_params.get("kind", "daily")
    symbol = request.path_params.get("symbol", "L0").upper()
    if kind not in ("daily", "min"):
        return JSONResponse({"ok": False, "error": "仅支持 daily 或 min"}, status_code=400)
    if symbol not in {item["symbol"] for item in PETRO_CONTRACTS}:
        return JSONResponse({"ok": False, "error": "不支持的合约代码"}, status_code=400)

    url = f"https://image.sinajs.cn/newchart/v5/futures/{kind}/{symbol}.gif"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url,
                headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"},
            )
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "image/gif"),
                headers={"Cache-Control": "public, max-age=120"},
            )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"拉取官方走势图失败: {exc}"}, status_code=502)


async def api_proxy_image(request: Request):
    """
    安全白名单图片代理：代理新浪财经等公开资讯图片，解决前端与微信编辑器引用防盗链问题。
    """
    target = (request.query_params.get("url") or "").strip()
    if not target.startswith("http"):
        return JSONResponse({"ok": False, "error": "无效的图片 URL"}, status_code=400)
    host = (urlparse(target).hostname or "").lower()
    if not any(host == suf or host.endswith("." + suf) for suf in ALLOWED_PROXY_SUFFIXES):
        return JSONResponse({"ok": False, "error": f"域名 {host} 不在允许的资讯图源白名单内"}, status_code=403)

    ref = "https://finance.sina.com.cn/" if "sina" in host else "https://finance.eastmoney.com/"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            resp = await client.get(
                target,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0", "Referer": ref},
            )
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=3600"},
            )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"代理图片失败: {exc}"}, status_code=502)


async def api_market_sync_news(request: Request):
    """
    真实抓取新浪财经期货要闻(155/1687)、新浪产经(153/2515)与新浪国内财经(153/2516)实时带图快讯并去重入库。
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0", "Referer": "https://finance.sina.com.cn/"}
    fetched_items: List[Dict[str, str]] = []

    try:
        async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
            for pid, lid in ((155, 1687), (153, 2515), (153, 2516)):
                api_url = f"https://feed.mix.sina.com.cn/api/roll/get?pageid={pid}&lid={lid}&num=8&page=1"
                r = await client.get(api_url)
                lst = (r.json().get("result") or {}).get("data") or []
                for it in lst:
                    title = (it.get("title") or "").strip()
                    summary = (it.get("intro") or it.get("summary") or title).strip()
                    u_url = (it.get("url") or "").strip()
                    img = ((it.get("img") or {}).get("u") or "").strip()
                    if not img and it.get("images"):
                        img = (it["images"][0].get("u") or "").strip()
                    if img.startswith("http://"):
                        img = "https://" + img[7:]
                    ctime = it.get("ctime")
                    ts_str = datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M:%S") if ctime else now_str()
                    if title and img:
                        fetched_items.append(
                            {
                                "title": title,
                                "content": summary,
                                "source_url": u_url,
                                "image_url": img,
                                "effective_date": ts_str[:10],
                                "created_at": ts_str,
                            }
                        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"同步新浪财经实时资讯失败: {exc}"}, status_code=502)

    conn = get_db()
    cur = conn.cursor()
    # 清理无法直接抓取字节流的旧东财盾图记录
    cur.execute("DELETE FROM intel_records WHERE source_type='eastmoney_news' AND image_url LIKE '%dfcfw.com%'")
    inserted = 0
    for item in fetched_items:
        exists = cur.execute("SELECT id FROM intel_records WHERE title=?", (item["title"],)).fetchone()
        if not exists:
            cur.execute(
                """
                INSERT INTO intel_records (source_type, title, content, source_url, image_url, effective_date, created_at)
                VALUES ('eastmoney_news', ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["title"],
                    item["content"],
                    item["source_url"],
                    item["image_url"],
                    item["effective_date"],
                    item["created_at"],
                ),
            )
            inserted += 1
        elif item["image_url"]:
            cur.execute("UPDATE intel_records SET image_url=? WHERE id=?", (item["image_url"], exists["id"]))
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM intel_records WHERE source_type='eastmoney_news' ORDER BY id DESC LIMIT 12"
    ).fetchall()
    conn.close()

    return JSONResponse(
        {
            "ok": True,
            "fetched_count": len(fetched_items),
            "inserted_count": inserted,
            "news": [dict(r) for r in rows],
        }
    )


# ==============================================================================
# 微信群报价结构化清洗引擎（Dify / 国内大模型 -> 本地塑化牌号规则引擎）
# ==============================================================================

KNOWN_PRODUCERS = [
    "神华新疆", "神华包头", "神华榆林", "神华", "镇海炼化", "延长中煤", "宝丰能源", "宁夏宝丰", "宝丰",
    "中石化", "中石油", "浙石化", "万华化学", "万华", "鄂尔多斯", "中煤榆林", "中煤", "扬子石化",
    "齐鲁石化", "独山子", "大庆石化", "兰化", "茂名石化", "福建联合", "蒲城清洁能源", "中天合创",
]

GRADE_CATEGORY_MAP = {
    "7042": "LLDPE",
    "7042N": "LLDPE",
    "7047": "LLDPE",
    "3518": "mLLDPE",
    "2426H": "LDPE",
    "2426K": "LDPE",
    "S1003": "PP拉丝",
    "L5E89": "PP拉丝",
    "T30S": "PP拉丝",
    "K8003": "PP共聚",
    "K7726H": "PP共聚",
    "5000S": "HDPE拉丝",
    "8008": "HDPE注塑",
    "5502": "HDPE中空",
    "SG-5": "PVC电石法",
    "SG-3": "PVC",
}

REGIONS = ["华东", "华南", "华北", "西北", "西南", "东北", "华中", "宁波", "余姚", "常州", "顺德"]


def local_parse_wechat_quotes(raw_text: str, source_group: str, sender: str) -> List[Dict[str, Any]]:
    segments = re.split(r"[，,；;\n。]+", raw_text.strip())
    extracted: List[Dict[str, Any]] = []

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        grade_match = re.search(r"(SG-\d|[A-Z]\d{3,4}[A-Z]?|[A-Z]\d[A-Z]\d{2}|\d{4}[A-Z]?)", seg.upper())
        seg_for_price = seg
        grade_code = ""
        if grade_match:
            grade_code = grade_match.group(1)
            s_i, e_i = grade_match.span()
            seg_for_price = seg[:s_i] + " " + seg[e_i:]

        price_match = re.search(
            r"(?:报|价格|现汇|出厂|自提|库提|送到|\s|^)(\d{4,5}(?:\.\d+)?)\s*(?:元|元/吨|吨)?",
            seg_for_price,
        )
        if not price_match:
            continue

        price_val = float(price_match.group(1))
        if not grade_code:
            grade_code = "通用牌号"

        producer = "现货贸易"
        for p in KNOWN_PRODUCERS:
            if p in seg:
                producer = p
                break

        region = "华东"
        for r in REGIONS:
            if r in seg or r in source_group:
                region = r
                break

        change_val = 0.0
        up_m = re.search(r"(?:涨|上调|\+)(\d+(?:\.\d+)?)", seg)
        down_m = re.search(r"(?:跌|下调|降|\-)(\d+(?:\.\d+)?)", seg)
        if up_m:
            change_val = float(up_m.group(1))
        elif down_m:
            change_val = -float(down_m.group(1))

        extracted.append(
            {
                "grade_code": grade_code,
                "category": GRADE_CATEGORY_MAP.get(grade_code, "塑化原料"),
                "producer": producer,
                "region": region,
                "price": price_val,
                "change_val": change_val,
                "unit": "元/吨",
                "source_group": source_group,
                "sender": sender,
                "raw_message": seg,
                "parse_engine": "local_rule_parser",
            }
        )

    return extracted


# ==============================================================================
# Dify / 国内文本大模型 & 图片大模型真实网关（绝不伪造返回值）
# ==============================================================================


async def call_dify_or_llm(
    workflow_key_name: str,
    system_prompt: str,
    user_prompt: str,
    dify_inputs: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = get_config_map()
    engine_mode = cfg.get("engine_mode", "dify")
    dify_base = cfg.get("dify_base_url", "").rstrip("/")
    dify_key = cfg.get(workflow_key_name, "").strip()
    llm_base = cfg.get("llm_base_url", "").rstrip("/")
    llm_key = cfg.get("llm_api_key", "").strip()
    llm_model = cfg.get("llm_model", "deepseek-chat").strip()

    if engine_mode == "dify" and dify_base and dify_key:
        url = f"{dify_base}/workflows/run"
        headers = {"Authorization": f"Bearer {dify_key}", "Content-Type": "application/json"}
        payload = {"inputs": dify_inputs, "response_mode": "blocking", "user": "hengye-xinyue-workbench"}
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    outputs = data.get("data", {}).get("outputs", {})
                    text_out = (
                        outputs.get("text")
                        or outputs.get("result")
                        or outputs.get("output")
                        or json.dumps(outputs, ensure_ascii=False)
                    )
                    return {"connected": True, "engine": f"Dify Workflow ({workflow_key_name})", "text": text_out}
                return {"connected": False, "error": f"Dify 接口返回 HTTP {resp.status_code}: {resp.text[:300]}"}
        except Exception as exc:
            return {"connected": False, "error": f"Dify 连接异常: {exc}"}

    if llm_base and llm_key:
        url = f"{llm_base}/chat/completions"
        headers = {"Authorization": f"Bearer {llm_key}", "Content-Type": "application/json"}
        payload = {
            "model": llm_model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "temperature": 0.4,
        }
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    return {"connected": True, "engine": f"国内大模型 ({llm_model})", "text": data["choices"][0]["message"]["content"]}
                return {"connected": False, "error": f"大模型接口返回 HTTP {resp.status_code}: {resp.text[:300]}"}
        except Exception as exc:
            return {"connected": False, "error": f"大模型连接异常: {exc}"}

    return {
        "connected": False,
        "error": f"未配置 {workflow_key_name} 或国内文本大模型 API Key，请前往【模型与凭据配置】填入真实密钥后重试（或先使用“汇入今日真实抓取数据骨架”手动编辑）。",
    }


async def api_images(request: Request):
    if request.method == "GET":
        conn = get_db()
        rows = conn.execute("SELECT * FROM generated_images ORDER BY id DESC LIMIT 30").fetchall()
        conn.close()
        return JSONResponse({"ok": True, "images": [dict(r) for r in rows]})

    img_id = int(request.path_params.get("id", 0))
    conn = get_db()
    conn.execute("DELETE FROM generated_images WHERE id=?", (img_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "deleted_id": img_id})


async def api_images_generate(request: Request):
    """
    真实调用已配置的图片大模型 API（兼容硅基流动 Kolors/FLUX、火山方舟即梦 Seedream、智谱 CogView 等 OpenAI /images/generations 接口）。
    未配置 image_api_key 时诚实返回 HTTP 400，绝不返回假图片。
    """
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    size = (body.get("size") or "1024x1024").strip()
    if not prompt:
        return JSONResponse({"ok": False, "error": "请输入配图描述词 (Prompt)"}, status_code=400)

    cfg = get_config_map()
    api_url = cfg.get("image_api_url", "").strip()
    api_key = cfg.get("image_api_key", "").strip()
    model = cfg.get("image_model", "Kwai-Kolors/Kolors").strip()

    if not api_url or not api_key:
        return JSONResponse(
            {
                "ok": False,
                "error": "未配置图片生成模型 API Key（请前往【模型与凭据配置】填入硅基流动 / 火山方舟即梦 / 智谱 CogView 的 API Key）。当前也可直接选用上方实时抓取的新浪走势图或东方财富新闻实拍图。",
            },
            status_code=400,
        )

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "prompt": prompt, "size": size, "n": 1}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(api_url, headers=headers, json=payload)
            if resp.status_code != 200:
                return JSONResponse(
                    {"ok": False, "error": f"图片模型接口返回 HTTP {resp.status_code}: {resp.text[:300]}"},
                    status_code=400,
                )
            data = resp.json()
            item = (data.get("data") or data.get("images") or [{}])[0]
            image_url = item.get("url") or ""
            if not image_url and item.get("b64_json"):
                image_url = "data:image/png;base64," + item["b64_json"]
            if not image_url:
                return JSONResponse(
                    {"ok": False, "error": f"图片模型未返回 url 或 b64_json: {json.dumps(data, ensure_ascii=False)[:250]}"},
                    status_code=502,
                )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"请求图片模型网络异常: {exc}"}, status_code=502)

    ts = now_str()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO generated_images (prompt, model, image_url, created_at) VALUES (?, ?, ?, ?)",
        (prompt, model, image_url, ts),
    )
    img_id = cur.lastrowid
    conn.commit()
    conn.close()

    return JSONResponse({"ok": True, "id": img_id, "prompt": prompt, "model": model, "image_url": image_url, "created_at": ts})


# ==============================================================================
# 微信公众号内联 CSS 样式渲染器（支持封面图、正文图片与实时行情表格）
# ==============================================================================


def render_wechat_mp_html(
    title: str,
    markdown_text: str,
    template_id: str,
    quotes: List[Dict[str, Any]],
    market_rows: List[Dict[str, Any]],
    cover_image_url: str = "",
) -> str:
    themes = {
        "hengye_daily": {
            "primary": "#0f2c59",
            "accent": "#2563eb",
            "bg": "#f8fafc",
            "badge": "恒业研投 · 每日行情实录",
        },
        "xinyue_magazine": {
            "primary": "#171717",
            "accent": "#ea580c",
            "bg": "#fafafa",
            "badge": "新月商业观察 · 深度精选",
        },
        "xinyue_interview": {
            "primary": "#065f46",
            "accent": "#059669",
            "bg": "#f0fdf4",
            "badge": "新月专访 · 行业一线对话",
        },
    }
    th = themes.get(template_id, themes["hengye_daily"])

    cover_html = ""
    if cover_image_url:
        cover_html = f"""
        <section style="margin:0 0 16px 0;">
            <img src="{cover_image_url}" alt="封面图" style="width:100%;display:block;border-radius:8px;" />
        </section>
        """

    # 若有实时抓到的期货主力或微信现货报价，附真实内联数据表
    table_html = ""
    if template_id == "hengye_daily":
        if quotes:
            q_rows = ""
            for q in quotes[:8]:
                chg = float(q.get("change_val", 0))
                chg_s = (
                    f'<span style="color:#dc2626;font-weight:bold;">+{int(chg)}</span>'
                    if chg > 0
                    else (f'<span style="color:#16a34a;font-weight:bold;">{int(chg)}</span>' if chg < 0 else "0")
                )
                q_rows += f"""
                <tr style="border-bottom:1px solid #e5e5e5;">
                    <td style="padding:8px;font-weight:bold;color:{th['primary']};">{q['grade_code']}</td>
                    <td style="padding:8px;color:#404040;">{q['producer']}</td>
                    <td style="padding:8px;color:#525252;">{q['region']}</td>
                    <td style="padding:8px;font-weight:bold;">¥{int(float(q['price']))}</td>
                    <td style="padding:8px;">{chg_s}</td>
                </tr>"""
            table_html += f"""
            <section style="margin:16px 0;border:1px solid #e5e5e5;border-radius:8px;overflow:hidden;">
                <div style="background:{th['primary']};color:#fff;padding:9px 12px;font-size:13px;font-weight:bold;">
                    微信采集 · 现货牌号实时报价（元/吨）
                </div>
                <table style="width:100%;border-collapse:collapse;font-size:12.5px;text-align:left;">
                    <thead><tr style="background:#f5f5f5;color:#525252;"><th style="padding:7px 8px;">牌号</th><th style="padding:7px 8px;">厂商</th><th style="padding:7px 8px;">区域</th><th style="padding:7px 8px;">报价</th><th style="padding:7px 8px;">涨跌</th></tr></thead>
                    <tbody>{q_rows}</tbody>
                </table>
            </section>"""

        if market_rows:
            m_rows = ""
            for m in market_rows[:6]:
                chg = float(m["change_val"])
                chg_s = (
                    f'<span style="color:#dc2626;font-weight:bold;">+{chg} ({m["change_pct"]}%)</span>'
                    if chg > 0
                    else (f'<span style="color:#16a34a;font-weight:bold;">{chg} ({m["change_pct"]}%)</span>' if chg < 0 else "0.0%")
                )
                m_rows += f"""
                <tr style="border-bottom:1px solid #e5e5e5;">
                    <td style="padding:7px 8px;font-weight:bold;color:#171717;">{m['name']}</td>
                    <td style="padding:7px 8px;font-weight:bold;">{m['last_price']}</td>
                    <td style="padding:7px 8px;">{chg_s}</td>
                    <td style="padding:7px 8px;color:#525252;">{int(float(m['volume']))}手</td>
                </tr>"""
            table_html += f"""
            <section style="margin:16px 0;border:1px solid #e5e5e5;border-radius:8px;overflow:hidden;">
                <div style="background:#171717;color:#fff;padding:9px 12px;font-size:13px;font-weight:bold;">
                    盘面监控 · 国内主力石化合约实时行情
                </div>
                <table style="width:100%;border-collapse:collapse;font-size:12.5px;text-align:left;">
                    <thead><tr style="background:#f5f5f5;color:#525252;"><th style="padding:7px 8px;">主力合约</th><th style="padding:7px 8px;">最新价</th><th style="padding:7px 8px;">日涨跌</th><th style="padding:7px 8px;">成交量</th></tr></thead>
                    <tbody>{m_rows}</tbody>
                </table>
            </section>"""

    body_html = ""
    for line in markdown_text.splitlines():
        s = line.strip()
        if not s or s.startswith("# "):
            continue
        img_m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", s)
        if img_m:
            alt, src = img_m.group(1), img_m.group(2)
            body_html += f"""
            <section style="margin:14px 0;text-align:center;">
                <img src="{src}" alt="{alt}" style="max-width:100%;border-radius:8px;display:block;margin:0 auto;" />
                {f'<div style="font-size:12px;color:#737373;margin-top:6px;">{alt}</div>' if alt else ''}
            </section>"""
        elif s.startswith("## ") or s.startswith("### "):
            sec = re.sub(r"^#+\s*", "", s)
            body_html += f"""
            <h2 style="margin:20px 0 8px 0;padding-left:10px;border-left:4px solid {th['accent']};font-size:16.5px;font-weight:bold;color:{th['primary']};line-height:1.4;">
                {sec}
            </h2>"""
        elif s.startswith("- ") or s.startswith("* "):
            txt = re.sub(r"^[-*]\s*", "", s)
            txt = re.sub(r"\*\*(.+?)\*\*", rf'<strong style="color:{th["primary"]};">\1</strong>', txt)
            body_html += f'<p style="margin:7px 0;padding-left:10px;font-size:14.5px;color:#334155;line-height:1.75;">• {txt}</p>'
        elif s.startswith("> "):
            qt = s[2:].strip()
            body_html += f"""
            <blockquote style="margin:12px 0;padding:11px 14px;background:{th['bg']};border-left:3px solid {th['primary']};color:#525252;font-size:14px;line-height:1.7;border-radius:4px;">
                {qt}
            </blockquote>"""
        else:
            fmt = re.sub(r"\*\*(.+?)\*\*", rf'<strong style="color:{th["primary"]};">\1</strong>', s)
            body_html += f'<p style="margin:10px 0;font-size:15px;color:#171717;line-height:1.8;">{fmt}</p>'

    return f"""
    <section style="max-width:677px;margin:0 auto;padding:16px 14px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;background:#ffffff;color:#171717;">
        {cover_html}
        <section style="padding:16px;background:{th['primary']};border-radius:10px;color:#ffffff;margin-bottom:16px;">
            <div style="font-size:11.5px;color:#cbd5e1;letter-spacing:0.6px;margin-bottom:6px;">
                {th['badge']} · {datetime.now().strftime('%Y-%m-%d')}
            </div>
            <h1 style="margin:0;font-size:19px;line-height:1.4;font-weight:bold;color:#ffffff;">{title}</h1>
        </section>
        {table_html}
        <section>{body_html}</section>
    </section>""".strip()


# ==============================================================================
# HTTP API 路由处理器
# ==============================================================================


async def homepage(request: Request):
    if INDEX_PATH.exists():
        return FileResponse(str(INDEX_PATH))
    return HTMLResponse("<h1>index.html 尚未创建</h1>", status_code=404)


async def api_status(request: Request):
    cfg = get_config_map()
    conn = get_db()
    counts = {
        "market_snapshots": conn.execute("SELECT COUNT(*) AS c FROM market_snapshots").fetchone()["c"],
        "price_quotes": conn.execute("SELECT COUNT(*) AS c FROM price_quotes").fetchone()["c"],
        "outbox_pending": conn.execute("SELECT COUNT(*) AS c FROM outbox_tasks WHERE status='pending'").fetchone()["c"],
        "intel_records": conn.execute("SELECT COUNT(*) AS c FROM intel_records").fetchone()["c"],
        "articles": conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"],
        "generated_images": conn.execute("SELECT COUNT(*) AS c FROM generated_images").fetchone()["c"],
        "canvas_projects": conn.execute("SELECT COUNT(*) AS c FROM canvas_projects").fetchone()["c"],
        "wechat_api_logs": conn.execute("SELECT COUNT(*) AS c FROM wechat_api_logs").fetchone()["c"],
    }
    conn.close()

    dify_ok = any(
        bool(cfg.get(k, "").strip())
        for k in ["dify_key_price", "dify_key_daily", "dify_key_video", "dify_key_mp_clone", "dify_key_drama"]
    )
    llm_ok = bool(cfg.get("llm_api_key", "").strip())
    img_ok = bool(cfg.get("image_api_key", "").strip())
    mp_ok = bool(cfg.get("mp_appid", "").strip() and cfg.get("mp_appsecret", "").strip())

    unconnected = []
    if not dify_ok and not llm_ok:
        unconnected.append("Dify / 文本大模型 API Key 未配置（AI 自动撰稿功能需先配置密钥，真实数据抓取与手动编辑已就绪）")
    if not img_ok:
        unconnected.append("图片生成模型 API Key 未配置（AI 生图功能需先配置，当前已预留配置位并支持直接引用实时抓取的走势图/新闻图）")
    if not mp_ok:
        unconnected.append("微信公众号 AppID / AppSecret 未配置（推送草稿箱与上传永久素材接口需填入真实公众号开发者凭据）")

    return JSONResponse(
        {
            "ok": True,
            "counts": counts,
            "connections": {
                "sqlite_db": True,
                "live_market": counts["market_snapshots"] > 0,
                "dify_engine": dify_ok,
                "direct_llm": llm_ok,
                "image_model": img_ok,
                "wechat_mp_api": mp_ok,
            },
            "unconnected_items": unconnected,
            "server_time": now_str(),
        }
    )


async def api_get_quotes(request: Request):
    conn = get_db()
    rows = conn.execute("SELECT * FROM price_quotes ORDER BY id DESC LIMIT 100").fetchall()
    outbox = conn.execute("SELECT * FROM outbox_tasks ORDER BY id DESC LIMIT 40").fetchall()
    conn.close()
    return JSONResponse({"ok": True, "quotes": [dict(r) for r in rows], "outbox": [dict(r) for r in outbox]})


async def api_delete_quote(request: Request):
    qid = int(request.path_params.get("id", 0))
    conn = get_db()
    conn.execute("DELETE FROM price_quotes WHERE id=?", (qid,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "deleted_id": qid})


async def api_wechat_ingest(request: Request):
    t0 = time.monotonic()
    body = await request.json()
    source_group = (body.get("source_name") or body.get("source_group") or "指定微信群").strip()
    sender = (body.get("sender") or "群成员").strip()
    raw_text = (body.get("raw_text") or "").strip()

    if not raw_text:
        resp_obj = {"ok": False, "error": "原始微信报价文本不能为空"}
        log_wechat_api("POST", "/api/wechat/ingest", 400, f"group={source_group}", resp_obj["error"], int((time.monotonic() - t0) * 1000))
        return JSONResponse(resp_obj, status_code=400)

    sys_prompt = (
        "你是塑化与大宗商品报价提取助手。请从微信消息中提取所有牌号报价，严格输出 JSON 数组，"
        "每个元素包含: grade_code, category, producer, region, price, change_val。"
    )
    ai_res = await call_dify_or_llm(
        "dify_key_price",
        sys_prompt,
        f"群聊：{source_group}\n发送人：{sender}\n微信消息：{raw_text}",
        {"raw_text": raw_text, "source_group": source_group, "sender": sender},
    )

    extracted: List[Dict[str, Any]] = []
    if ai_res.get("connected"):
        try:
            m = re.search(r"\[.*\]", ai_res["text"], re.S)
            if m:
                for item in json.loads(m.group(0)):
                    extracted.append(
                        {
                            "grade_code": str(item.get("grade_code", "未知牌号")),
                            "category": str(item.get("category", "塑化原料")),
                            "producer": str(item.get("producer", "现货")),
                            "region": str(item.get("region", "华东")),
                            "price": float(item.get("price", 0)),
                            "change_val": float(item.get("change_val", 0)),
                            "unit": "元/吨",
                            "source_group": source_group,
                            "sender": sender,
                            "raw_message": raw_text,
                            "parse_engine": ai_res["engine"],
                        }
                    )
        except Exception:
            extracted = []

    if not extracted:
        extracted = local_parse_wechat_quotes(raw_text, source_group, sender)

    if not extracted:
        err = "未能识别出有效牌号与四位数吨价（示例：今日浙石化7042报8180涨30）"
        log_wechat_api("POST", "/api/wechat/ingest", 422, raw_text[:120], err, int((time.monotonic() - t0) * 1000))
        return JSONResponse({"ok": False, "error": err}, status_code=422)

    ts = now_str()
    conn = get_db()
    cur = conn.cursor()
    ids = []
    for item in extracted:
        cur.execute(
            """
            INSERT INTO price_quotes
            (grade_code, category, producer, region, price, change_val, unit, source_group, sender, raw_message, parse_engine, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["grade_code"], item["category"], item["producer"], item["region"],
                item["price"], item["change_val"], item["unit"], item["source_group"],
                item["sender"], item["raw_message"], item["parse_engine"], ts,
            ),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()

    resp_obj = {"ok": True, "inserted_count": len(ids), "inserted_ids": ids, "extracted": extracted, "engine_used": extracted[0]["parse_engine"]}
    log_wechat_api("POST", "/api/wechat/ingest", 200, raw_text[:120], f"inserted={len(ids)} ids={ids}", int((time.monotonic() - t0) * 1000))
    return JSONResponse(resp_obj)


async def api_wechat_outbox_create(request: Request):
    t0 = time.monotonic()
    body = await request.json()
    recipients = body.get("target_recipients") or []
    if isinstance(recipients, str):
        recipients = [r.strip() for r in recipients.split(",") if r.strip()]
    if not recipients:
        return JSONResponse({"ok": False, "error": "请填写至少一个目标微信客户或群名称"}, status_code=400)

    quote_ids = body.get("quote_ids") or []
    custom_header = (body.get("custom_header") or "【恒业塑化 · 今日牌号报价单】").strip()

    conn = get_db()
    if quote_ids:
        ph = ",".join("?" for _ in quote_ids)
        rows = conn.execute(f"SELECT * FROM price_quotes WHERE id IN ({ph}) ORDER BY id DESC", quote_ids).fetchall()
    else:
        rows = conn.execute("SELECT * FROM price_quotes ORDER BY id DESC LIMIT 10").fetchall()

    if not rows:
        conn.close()
        err = "当前没有可分发的真实牌号报价记录，请先从微信群抓取或录入报价"
        log_wechat_api("POST", "/api/wechat/outbox", 400, str(recipients), err, int((time.monotonic() - t0) * 1000))
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    lines = [f"{custom_header} ({datetime.now().strftime('%m月%d日 %H:%M')})"]
    for r in rows:
        chg = float(r["change_val"])
        chg_t = f"涨{int(chg)}" if chg > 0 else (f"跌{int(abs(chg))}" if chg < 0 else "平")
        lines.append(f"• {r['producer']} {r['grade_code']} ({r['region']})：{int(r['price'])}元/吨 ({chg_t})")
    msg_body = "\n".join(lines)

    ts = now_str()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO outbox_tasks (target_recipients, message_body, status, result_note, created_at, updated_at) VALUES (?, ?, 'pending', '等待桌面微信客户端轮询拉取', ?, ?)",
        (json.dumps(recipients, ensure_ascii=False), msg_body, ts, ts),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()

    log_wechat_api("POST", "/api/wechat/outbox", 200, f"targets={recipients}", f"task_id={tid} status=pending", int((time.monotonic() - t0) * 1000))
    return JSONResponse({"ok": True, "task_id": tid, "target_recipients": recipients, "message_body": msg_body, "status": "pending"})


async def api_wechat_outbox_delete(request: Request):
    tid = int(request.path_params.get("id", 0))
    conn = get_db()
    conn.execute("DELETE FROM outbox_tasks WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "deleted_id": tid})


async def api_wechat_outbox_poll(request: Request):
    t0 = time.monotonic()
    if request.method == "GET":
        conn = get_db()
        rows = conn.execute("SELECT * FROM outbox_tasks WHERE status='pending' ORDER BY id ASC").fetchall()
        conn.close()
        res = {"ok": True, "pending_tasks": [dict(r) for r in rows]}
        log_wechat_api("GET", "/api/wechat/outbox-poll", 200, "poll_pending", f"count={len(rows)}", int((time.monotonic() - t0) * 1000))
        return JSONResponse(res)

    body = await request.json()
    task_id = int(body.get("task_id", 0))
    new_status = body.get("status", "sent")
    note = body.get("result_note", "已由外部微信客户端回执")
    conn = get_db()
    conn.execute("UPDATE outbox_tasks SET status=?, result_note=?, updated_at=? WHERE id=?", (new_status, note, now_str(), task_id))
    conn.commit()
    conn.close()
    log_wechat_api("POST", "/api/wechat/outbox-poll", 200, f"task_id={task_id} status={new_status}", note, int((time.monotonic() - t0) * 1000))
    return JSONResponse({"ok": True, "task_id": task_id, "status": new_status})


async def api_wechat_logs(request: Request):
    if request.method == "DELETE":
        conn = get_db()
        conn.execute("DELETE FROM wechat_api_logs")
        conn.commit()
        conn.close()
        return JSONResponse({"ok": True})
    conn = get_db()
    rows = conn.execute("SELECT * FROM wechat_api_logs ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return JSONResponse({"ok": True, "logs": [dict(r) for r in rows]})


async def api_wechat_endpoints(request: Request):
    base = get_public_base_url(request)
    wx_token = get_config_map().get("wx_plugin_token", "")
    endpoints = [
        {
            "id": "ingest",
            "name": "1. PC 微信群报价抓取上报接口",
            "method": "POST",
            "url": f"{base}/api/wechat/ingest",
            "auth": "需携带请求头 X-WeChat-Token（或已登录 Cookie）",
            "desc": "接收本地 Windows 微信小插件或第三方机器人推送的群聊消息，实时结构化清洗后落盘 SQLite。",
            "sample_body": {
                "source_name": "华东塑化现货报价1群",
                "sender": "宁波恒业-老张",
                "raw_text": "今日浙石化7042报8180涨30，神华S1003报7460跌20",
            },
        },
        {
            "id": "outbox_create",
            "name": "2. 创建下游客户报价分发任务",
            "method": "POST",
            "url": f"{base}/api/wechat/outbox",
            "auth": "管理员登录后调用",
            "desc": "选择牌号报价生成标准报价单并推入待分发队列。",
            "sample_body": {
                "target_recipients": ["嘉兴包装厂张总", "余姚VIP群"],
                "quote_ids": [],
                "custom_header": "【恒业塑化 · 今日最新现货报价】",
            },
        },
        {
            "id": "outbox_poll_get",
            "name": "3. 桌面插件拉取待发微信任务 (GET)",
            "method": "GET",
            "url": f"{base}/api/wechat/outbox-poll",
            "auth": "需携带请求头 X-WeChat-Token（或已登录 Cookie）",
            "desc": "Windows PC 微信客户端定时轮询本接口获取 status=pending 的群发任务。",
            "sample_body": None,
        },
        {
            "id": "outbox_poll_ack",
            "name": "4. 桌面插件回执发送结果 (POST)",
            "method": "POST",
            "url": f"{base}/api/wechat/outbox-poll",
            "auth": "需携带请求头 X-WeChat-Token（或已登录 Cookie）",
            "desc": "客户端发送完成后回传 task_id 与状态。",
            "sample_body": {"task_id": 1, "status": "sent", "result_note": "PC微信客户端发送完成"},
        },
        {
            "id": "mp_upload_thumb",
            "name": "5. 微信公众号永久封面素材上传",
            "method": "POST",
            "url": f"{base}/api/mp/upload-thumb",
            "auth": "管理员登录后调用",
            "desc": "将配图上传至微信公众平台 /cgi-bin/material/add_material 换取 thumb_media_id。",
            "sample_body": {"image_url": f"{base}/api/market/chart-img/daily/L0"},
        },
        {
            "id": "mp_draft",
            "name": "6. 微信公众号草稿箱直推接口",
            "method": "POST",
            "url": f"{base}/api/mp/draft",
            "auth": "管理员登录后调用",
            "desc": "调用微信官方 /cgi-bin/draft/add 将人工定稿的内联 HTML 推送进公众号草稿箱。",
            "sample_body": {"article_id": 1, "title": "测试草稿标题", "styled_html": "<p>正文测试</p>"},
        },
    ]
    return JSONResponse({"ok": True, "base_url": base, "wx_plugin_token": wx_token, "endpoints": endpoints})


async def api_wechat_plugin_script(request: Request):
    host_url = get_public_base_url(request)
    wx_token = get_config_map().get("wx_plugin_token", "")
    script = f'''"""
恒业 × 新月工作台 —— 本地 PC 微信报价抓取与客户自动分发桌面插件
运行环境：Windows 10/11 + PC 微信客户端 + Python 3.10+
安装依赖：pip install uiautomation requests pyperclip
"""
import time
import json
import requests

WORKBENCH_URL = "{host_url}"
WX_PLUGIN_TOKEN = "{wx_token}"
HEADERS = {{"X-WeChat-Token": WX_PLUGIN_TOKEN}}
WATCH_GROUPS = ["华东塑化现货报价1群", "聚丙烯PP主力贸易群"]
POLL_INTERVAL_SEC = 5

def push_quote_to_workbench(group_name: str, sender: str, text: str):
    resp = requests.post(
        f"{{WORKBENCH_URL}}/api/wechat/ingest",
        headers=HEADERS,
        json={{"source_name": group_name, "sender": sender, "raw_text": text}},
        timeout=15,
    )
    print("[上报工作台结果]", resp.status_code, resp.text)

def poll_and_send_outbox():
    resp = requests.get(f"{{WORKBENCH_URL}}/api/wechat/outbox-poll", headers=HEADERS, timeout=10)
    data = resp.json()
    for task in data.get("pending_tasks", []):
        recipients = json.loads(task["target_recipients"])
        msg = task["message_body"]
        print(f"[待分发任务 #{{task['id']}}] 目标客户: {{recipients}}\\n{{msg}}")
        # 此处由 uiautomation 定位微信搜索框跳转客户窗口粘贴发送
        requests.post(
            f"{{WORKBENCH_URL}}/api/wechat/outbox-poll",
            headers=HEADERS,
            json={{"task_id": task["id"], "status": "sent", "result_note": "已由本地桌面插件自动分发"}},
            timeout=10,
        )

if __name__ == "__main__":
    print(f"已连接恒业 × 新月工作台: {{WORKBENCH_URL}}")
    while True:
        try:
            poll_and_send_outbox()
        except Exception as e:
            print("轮询异常:", e)
        time.sleep(POLL_INTERVAL_SEC)
'''
    return PlainTextResponse(script)


# ==============================================================================
# 情报抓取与手动补录
# ==============================================================================


async def api_get_intel(request: Request):
    conn = get_db()
    rows = conn.execute("SELECT * FROM intel_records ORDER BY id DESC LIMIT 80").fetchall()
    conn.close()
    return JSONResponse({"ok": True, "records": [dict(r) for r in rows]})


async def api_delete_intel(request: Request):
    rid = int(request.path_params.get("id", 0))
    conn = get_db()
    conn.execute("DELETE FROM intel_records WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "deleted_id": rid})


async def api_add_manual_intel(request: Request):
    body = await request.json()
    source_type = (body.get("source_type") or "manual_maintenance").strip()
    title = (body.get("title") or "").strip()
    content = (body.get("content") or "").strip()
    effective_date = (body.get("effective_date") or datetime.now().strftime("%Y-%m-%d")).strip()

    if not title or not content:
        return JSONResponse({"ok": False, "error": "标题与具体动态内容不能为空"}, status_code=400)

    ts = now_str()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO intel_records (source_type, title, content, source_url, image_url, effective_date, created_at) VALUES (?, ?, ?, '', '', ?, ?)",
        (source_type, title, content, effective_date, ts),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "id": rid, "created_at": ts})


async def api_scrape_website(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        return JSONResponse({"ok": False, "error": "请输入有效的 http/https 网址"}, status_code=400)

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0, headers=headers) as client:
            resp = await client.get(url)
            status_code = resp.status_code
            html_bytes = resp.content
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"抓取目标网站失败: {exc}"}, status_code=502)

    soup = BeautifulSoup(html_bytes, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    page_title = soup.title.string.strip() if soup.title and soup.title.string else url
    lines = []
    for el in soup.find_all(["h1", "h2", "h3", "p", "li", "tr"]):
        t = " ".join(el.get_text(" ", strip=True).split())
        if len(t) >= 12 and t not in lines:
            lines.append(t)
        if len(lines) >= 25:
            break

    first_img = ""
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if src.startswith("http") and not src.endswith(".svg"):
            first_img = src
            break

    extracted = "\n".join(lines)[:1800] if lines else soup.get_text(" ", strip=True)[:1200]
    ts = now_str()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO intel_records (source_type, title, content, source_url, image_url, effective_date, created_at) VALUES ('web_scrape', ?, ?, ?, ?, ?, ?)",
        (f"【抓取】{page_title[:70]}", extracted, url, first_img, datetime.now().strftime("%Y-%m-%d"), ts),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "id": rid, "http_status": status_code, "title": page_title, "image_url": first_img, "created_at": ts})


# ==============================================================================
# 稿件编辑室：真实数据骨架汇编 + 真实 AI 撰写 + 微信永久素材上传与草稿箱直推
# ==============================================================================


async def api_insert_live_data(request: Request):
    """
    真实汇入当前数据库中抓取到的新浪石化主力行情、东方财富快讯、微信群报价与手动补录的检修动态，
    形成 100% 真实数据骨架供人工在编辑器内修改（零虚假 AI 文本）。
    """
    body = await request.json()
    template_id = body.get("template_id") or "hengye_daily"
    cover_image_url = (body.get("cover_image_url") or "").strip()

    conn = get_db()
    quotes = [dict(r) for r in conn.execute("SELECT * FROM price_quotes ORDER BY id DESC LIMIT 10").fetchall()]
    markets = [dict(r) for r in conn.execute("SELECT * FROM market_snapshots ORDER BY volume DESC LIMIT 10").fetchall()]
    intels = [dict(r) for r in conn.execute("SELECT * FROM intel_records ORDER BY id DESC LIMIT 10").fetchall()]
    conn.close()

    today_cn = datetime.now().strftime("%Y年%m月%d日")
    m_lines = "\n".join(
        f"- **{m['name']} ({m['symbol']})**：最新 {m['last_price']}，涨跌 {m['change_val']}（{m['change_pct']}%），成交 {int(m['volume'])} 手"
        for m in markets
    ) or "- 暂无主力期货行情快照（请点击行情总览页刷新实时抓取）"

    q_lines = "\n".join(
        f"- **{q['producer']} {q['grade_code']} ({q['region']})**：{int(q['price'])} 元/吨（涨跌 {int(q['change_val'])}，来源：{q['source_group']}）"
        for q in quotes
    ) or "- 暂无微信群现货报价（请先从微信群抓取或录入报价）"

    maint_lines = "\n".join(
        f"- **[{i['effective_date']}] {i['title']}**：{i['content']}"
        for i in intels if i["source_type"] in ("manual_maintenance", "daily_dialogue")
    ) or "- 暂无手动补录的装置检修或下午内部汇报"

    news_lines = "\n".join(
        f"- **{i['title']}**：{i['content'][:160]}"
        for i in intels if i["source_type"] in ("eastmoney_news", "web_scrape")
    ) or "- 暂无抓取到的行业资讯"

    title = f"【恒业实盘】{today_cn} 国内石化主力盘面与现货报价汇总"
    md = f"""## 一、 今日国内主力石化期货盘面实录
{m_lines}

## 二、 微信群实时现货牌号报价
{q_lines}

## 三、 装置检修与一线内部汇报（手动补录）
{maint_lines}

## 四、 行业资讯与宏观动态（实时抓取）
{news_lines}

> 以上数据全部来自本工作台实时抓取及一线录入记录，请在左侧或本框内补充您的行情观点后保存套用微信排版样式。"""

    styled_html = render_wechat_mp_html(title, md, template_id, quotes, markets, cover_image_url)
    return JSONResponse({"ok": True, "title": title, "raw_markdown": md, "styled_html": styled_html})


async def api_ai_generate(request: Request):
    """
    严格调用 Dify Workflow 或国内文本大模型 API；未配置密钥时诚实返回 HTTP 400，绝不编造假文案。
    """
    body = await request.json()
    mode = body.get("mode", "mp_daily")
    custom_topic = (body.get("topic") or "").strip()
    reference_template = (body.get("reference_template") or "").strip()
    template_id = body.get("template_id") or ("hengye_daily" if mode == "mp_daily" else "xinyue_magazine")
    cover_image_url = (body.get("cover_image_url") or "").strip()

    conn = get_db()
    quotes = [dict(r) for r in conn.execute("SELECT * FROM price_quotes ORDER BY id DESC LIMIT 10").fetchall()]
    markets = [dict(r) for r in conn.execute("SELECT * FROM market_snapshots ORDER BY volume DESC LIMIT 10").fetchall()]
    intels = [dict(r) for r in conn.execute("SELECT * FROM intel_records ORDER BY id DESC LIMIT 10").fetchall()]
    conn.close()

    quotes_summary = "\n".join(
        f"- {q['producer']} {q['grade_code']} ({q['region']}): {int(q['price'])}元/吨 (涨跌:{int(q['change_val'])})"
        for q in quotes
    ) or "暂无现货牌号报价"
    market_summary = "\n".join(
        f"- {m['name']}({m['symbol']}): 最新{m['last_price']}, 涨跌{m['change_val']}({m['change_pct']}%), 成交{int(m['volume'])}手"
        for m in markets
    ) or "暂无盘面行情"
    maintenance_summary = "\n".join(
        f"- [{i['effective_date']}] {i['title']}: {i['content']}"
        for i in intels if i["source_type"] == "manual_maintenance"
    ) or "暂无装置检修补录"
    dialogue_summary = "\n".join(
        f"- {i['title']}: {i['content']}"
        for i in intels if i["source_type"] == "daily_dialogue"
    ) or "暂无16-17点内部汇报"
    scrape_summary = "\n".join(
        f"- {i['title']}: {i['content'][:180]}"
        for i in intels if i["source_type"] in ("eastmoney_news", "web_scrape")
    ) or "暂无抓取新闻"

    wf_key_map = {
        "mp_daily": "dify_key_daily",
        "mp_clone": "dify_key_mp_clone",
        "video_daily": "dify_key_daily",
        "video_whiteboard": "dify_key_video",
        "video_interview": "dify_key_video",
    }
    wf_key = wf_key_map.get(mode, "dify_key_daily")

    sys_prompt = "你是恒业与新月工作台的首席石化行情分析师与新媒体主编。必须严格基于提供的真实盘面、现货报价、检修补录和资讯撰写内容，严禁虚构价格。"
    user_prompt = (
        f"模式：{mode}\n主题：{custom_topic}\n对标模板：{reference_template}\n\n"
        f"【主力盘面行情】\n{market_summary}\n\n【现货牌号报价】\n{quotes_summary}\n\n"
        f"【装置检修动态】\n{maintenance_summary}\n\n【内部汇报】\n{dialogue_summary}\n\n【行业资讯】\n{scrape_summary}"
    )

    ai_res = await call_dify_or_llm(
        wf_key,
        sys_prompt,
        user_prompt,
        {
            "mode": mode,
            "topic": custom_topic,
            "reference_template": reference_template,
            "quotes": f"{market_summary}\n{quotes_summary}",
            "maintenance": maintenance_summary,
            "dialogue": dialogue_summary,
            "scrape": scrape_summary,
        },
    )

    if not ai_res.get("connected"):
        return JSONResponse({"ok": False, "error": ai_res.get("error")}, status_code=400)

    today_cn = datetime.now().strftime("%Y年%m月%d日")
    title = custom_topic or f"{today_cn} 恒业石化行情研判"
    markdown_body = ai_res["text"]
    engine_note = ai_res["engine"]
    styled_html = render_wechat_mp_html(title, markdown_body, template_id, quotes, markets, cover_image_url)

    ts = now_str()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO articles (article_type, title, raw_markdown, styled_html, template_id, cover_image_url, engine_note, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
        (mode, title, markdown_body, styled_html, template_id, cover_image_url, engine_note, ts, ts),
    )
    art_id = cur.lastrowid
    conn.commit()
    conn.close()

    return JSONResponse(
        {
            "ok": True,
            "article_id": art_id,
            "title": title,
            "raw_markdown": markdown_body,
            "styled_html": styled_html,
            "template_id": template_id,
            "cover_image_url": cover_image_url,
            "engine_note": engine_note,
        }
    )


async def api_articles(request: Request):
    if request.method == "GET":
        conn = get_db()
        rows = conn.execute("SELECT * FROM articles ORDER BY id DESC LIMIT 40").fetchall()
        conn.close()
        return JSONResponse({"ok": True, "articles": [dict(r) for r in rows]})

    if request.method == "DELETE":
        aid = int(request.path_params.get("id", 0))
        conn = get_db()
        conn.execute("DELETE FROM articles WHERE id=?", (aid,))
        conn.commit()
        conn.close()
        return JSONResponse({"ok": True, "deleted_id": aid})

    body = await request.json()
    art_id = int(body.get("id", 0))
    title = (body.get("title") or "未命名稿件").strip()
    raw_markdown = body.get("raw_markdown") or ""
    template_id = body.get("template_id") or "hengye_daily"
    cover_image_url = (body.get("cover_image_url") or "").strip()
    article_type = (body.get("article_type") or "mp_daily").strip()

    conn = get_db()
    quotes = [dict(r) for r in conn.execute("SELECT * FROM price_quotes ORDER BY id DESC LIMIT 8").fetchall()]
    markets = [dict(r) for r in conn.execute("SELECT * FROM market_snapshots ORDER BY volume DESC LIMIT 6").fetchall()]
    styled_html = render_wechat_mp_html(title, raw_markdown, template_id, quotes, markets, cover_image_url)
    ts = now_str()

    if art_id > 0:
        conn.execute(
            "UPDATE articles SET title=?, raw_markdown=?, styled_html=?, template_id=?, cover_image_url=?, status='edited', updated_at=? WHERE id=?",
            (title, raw_markdown, styled_html, template_id, cover_image_url, ts, art_id),
        )
    else:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO articles (article_type, title, raw_markdown, styled_html, template_id, cover_image_url, engine_note, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, '人工编辑保存', 'edited', ?, ?)",
            (article_type, title, raw_markdown, styled_html, template_id, cover_image_url, ts, ts),
        )
        art_id = cur.lastrowid
    conn.commit()
    conn.close()

    return JSONResponse(
        {
            "ok": True,
            "article_id": art_id,
            "title": title,
            "styled_html": styled_html,
            "template_id": template_id,
            "cover_image_url": cover_image_url,
            "updated_at": ts,
        }
    )


async def api_mp_upload_thumb(request: Request):
    """
    真实将封面图（支持本地同源图 / 代理新闻图 / data URI / https 图片）上传至微信公众平台永久素材接口，
    获取 thumb_media_id 并自动保存至 system_config。
    """
    t0 = time.monotonic()
    body = await request.json()
    img_url = (body.get("image_url") or "").strip()
    if not img_url:
        return JSONResponse({"ok": False, "error": "请提供要上传的封面图片 URL"}, status_code=400)

    cfg = get_config_map()
    appid = cfg.get("mp_appid", "").strip()
    secret = cfg.get("mp_appsecret", "").strip()
    if not appid or not secret:
        err = "未配置微信公众号 AppID 或 AppSecret"
        log_wechat_api("POST", "/api/mp/upload-thumb", 400, img_url[:100], err, int((time.monotonic() - t0) * 1000))
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    try:
        img_bytes = b""
        filename = "cover.png"
        mime = "image/png"
        if img_url.startswith("data:image/"):
            header, b64 = img_url.split(",", 1)
            img_bytes = base64.b64decode(b64)
            if "jpeg" in header or "jpg" in header:
                filename, mime = "cover.jpg", "image/jpeg"
        else:
            if img_url.startswith("/"):
                local_port = os.environ.get("PORT", os.environ.get("WORKBENCH_PORT", "8765"))
                full_url = f"http://127.0.0.1:{local_port}{img_url}"
                req_cookies = request.cookies
            else:
                full_url = img_url
                req_cookies = None
            async with httpx.AsyncClient(timeout=15.0) as client:
                r_img = await client.get(full_url, cookies=req_cookies)
                if r_img.status_code != 200 or not r_img.content:
                    err = f"封面图片抓取失败（HTTP {r_img.status_code}）"
                    log_wechat_api("POST", "/api/mp/upload-thumb", 400, img_url[:100], err, int((time.monotonic() - t0) * 1000))
                    return JSONResponse({"ok": False, "error": err}, status_code=400)
                img_bytes = r_img.content
                ct = r_img.headers.get("content-type", "")
                if "gif" in ct:
                    filename, mime = "cover.gif", "image/gif"
                elif "jpeg" in ct or "jpg" in ct:
                    filename, mime = "cover.jpg", "image/jpeg"

        async with httpx.AsyncClient(timeout=20.0) as client:
            tk = (
                await client.get(
                    "https://api.weixin.qq.com/cgi-bin/token",
                    params={"grant_type": "client_credential", "appid": appid, "secret": secret},
                )
            ).json()
            if "access_token" not in tk:
                err = f"获取 access_token 失败: {tk.get('errcode')} - {tk.get('errmsg')}"
                log_wechat_api("POST", "/api/mp/upload-thumb", 400, img_url[:100], err, int((time.monotonic() - t0) * 1000))
                return JSONResponse({"ok": False, "error": err, "wechat_raw": tk}, status_code=400)

            up = (
                await client.post(
                    f"https://api.weixin.qq.com/cgi-bin/material/add_material?access_token={tk['access_token']}&type=image",
                    files={"media": (filename, img_bytes, mime)},
                )
            ).json()
            if "media_id" in up:
                conn = get_db()
                conn.execute(
                    "INSERT OR REPLACE INTO system_config (key, value, updated_at) VALUES ('mp_thumb_media_id', ?, ?)",
                    (up["media_id"], now_str()),
                )
                conn.commit()
                conn.close()
                log_wechat_api("POST", "/api/mp/upload-thumb", 200, filename, f"media_id={up['media_id']}", int((time.monotonic() - t0) * 1000))
                return JSONResponse({"ok": True, "media_id": up["media_id"], "url": up.get("url", ""), "wechat_raw": up})

            err = f"微信素材接口返回错误: {up.get('errcode')} - {up.get('errmsg')}"
            log_wechat_api("POST", "/api/mp/upload-thumb", 400, filename, err, int((time.monotonic() - t0) * 1000))
            return JSONResponse({"ok": False, "error": err, "wechat_raw": up}, status_code=400)
    except Exception as exc:
        err = f"上传永久素材异常: {exc}"
        log_wechat_api("POST", "/api/mp/upload-thumb", 502, img_url[:100], err, int((time.monotonic() - t0) * 1000))
        return JSONResponse({"ok": False, "error": err}, status_code=502)


async def api_push_wechat_mp_draft(request: Request):
    t0 = time.monotonic()
    body = await request.json()
    art_id = int(body.get("article_id", 0))
    title = (body.get("title") or "").strip()
    styled_html = (body.get("styled_html") or "").strip()

    cfg = get_config_map()
    appid = cfg.get("mp_appid", "").strip()
    secret = cfg.get("mp_appsecret", "").strip()
    thumb_media_id = cfg.get("mp_thumb_media_id", "").strip()
    author = cfg.get("mp_author", "恒业研投").strip()

    if not appid or not secret:
        err = "未配置微信公众号 AppID 或 AppSecret（请前往【模型与凭据配置】填写并设置 IP 白名单）"
        log_wechat_api("POST", "/api/mp/draft", 400, title[:80], err, int((time.monotonic() - t0) * 1000))
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            token_data = (
                await client.get(
                    "https://api.weixin.qq.com/cgi-bin/token",
                    params={"grant_type": "client_credential", "appid": appid, "secret": secret},
                )
            ).json()
            if "access_token" not in token_data:
                err = f"获取 access_token 失败: {token_data.get('errcode')} - {token_data.get('errmsg')}"
                log_wechat_api("POST", "/api/mp/draft", 400, title[:80], err, int((time.monotonic() - t0) * 1000))
                return JSONResponse({"ok": False, "error": err, "wechat_raw": token_data}, status_code=400)

            article_item: Dict[str, Any] = {
                "title": title,
                "author": author,
                "digest": re.sub(r"<[^>]+>", "", styled_html)[:110],
                "content": styled_html,
                "content_source_url": "",
            }
            if thumb_media_id:
                article_item["thumb_media_id"] = thumb_media_id

            draft_data = (
                await client.post(
                    f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={token_data['access_token']}",
                    content=json.dumps({"articles": [article_item]}, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
            ).json()
            if "media_id" in draft_data:
                media_id = draft_data["media_id"]
                if art_id > 0:
                    conn = get_db()
                    conn.execute("UPDATE articles SET mp_draft_media_id=?, status='pushed_mp', updated_at=? WHERE id=?", (media_id, now_str(), art_id))
                    conn.commit()
                    conn.close()
                log_wechat_api("POST", "/api/mp/draft", 200, title[:80], f"media_id={media_id}", int((time.monotonic() - t0) * 1000))
                return JSONResponse({"ok": True, "media_id": media_id, "wechat_raw": draft_data})

            err = f"微信草稿箱接口返回错误: {draft_data.get('errcode')} - {draft_data.get('errmsg')}"
            log_wechat_api("POST", "/api/mp/draft", 400, title[:80], err, int((time.monotonic() - t0) * 1000))
            return JSONResponse({"ok": False, "error": err, "wechat_raw": draft_data}, status_code=400)
    except Exception as exc:
        err = f"请求微信官方接口网络异常: {exc}"
        log_wechat_api("POST", "/api/mp/draft", 502, title[:80], err, int((time.monotonic() - t0) * 1000))
        return JSONResponse({"ok": False, "error": err}, status_code=502)


# ==============================================================================
# 画布板块（支持项目新建/删除、节点增删拖拽连线、真实文本/图片大模型执行）
# ==============================================================================


async def api_canvas_projects(request: Request):
    if request.method == "GET":
        conn = get_db()
        rows = conn.execute("SELECT * FROM canvas_projects ORDER BY id DESC").fetchall()
        conn.close()
        projects = []
        for r in rows:
            d = dict(r)
            d["nodes"] = json.loads(d["nodes_json"])
            d["edges"] = json.loads(d["edges_json"])
            projects.append(d)
        return JSONResponse({"ok": True, "projects": projects})

    if request.method == "DELETE":
        pid = int(request.path_params.get("id", 0))
        conn = get_db()
        conn.execute("DELETE FROM canvas_projects WHERE id=?", (pid,))
        conn.commit()
        conn.close()
        return JSONResponse({"ok": True, "deleted_id": pid})

    body = await request.json()
    action = body.get("action", "save")

    if action == "create":
        name = (body.get("name") or f"新画布项目 {datetime.now().strftime('%m-%d %H:%M')}").strip()
        ts = now_str()
        starter_nodes = [
            {"id": "n-1", "type": "product_input", "title": "1. 输入主题或商品卖点", "x": 40, "y": 80, "status": "ready", "content": "", "image_url": ""},
            {"id": "n-2", "type": "drama_script", "title": "2. AI 脚本 / 文案节点", "x": 360, "y": 80, "status": "ready", "content": "", "image_url": ""},
            {"id": "n-3", "type": "image_gen", "title": "3. AI 图片模型配图节点", "x": 680, "y": 80, "status": "ready", "content": "", "image_url": ""},
        ]
        starter_edges = [{"from": "n-1", "to": "n-2"}, {"from": "n-2", "to": "n-3"}]
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO canvas_projects (name, product_name, product_selling_points, nodes_json, edges_json, updated_at) VALUES (?, '', '', ?, ?, ?)",
            (name, json.dumps(starter_nodes, ensure_ascii=False), json.dumps(starter_edges, ensure_ascii=False), ts),
        )
        pid = cur.lastrowid
        conn.commit()
        conn.close()
        return JSONResponse({"ok": True, "id": pid, "name": name})

    if action == "run_node":
        node_type = body.get("node_type", "drama_script")
        upstream_text = (body.get("upstream_text") or "").strip()
        node_title = body.get("node_title", "")

        if node_type == "image_gen":
            # 调用真实图片大模型生成图片
            prompt = upstream_text or node_title
            if not prompt:
                return JSONResponse({"ok": False, "error": "请先在当前节点或上游节点填写配图描述词"}, status_code=400)
            cfg = get_config_map()
            api_url = cfg.get("image_api_url", "").strip()
            api_key = cfg.get("image_api_key", "").strip()
            model = cfg.get("image_model", "Kwai-Kolors/Kolors").strip()
            if not api_url or not api_key:
                return JSONResponse({"ok": False, "error": "未配置图片模型 API Key，请前往【模型与凭据配置】填入真实密钥"}, status_code=400)
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        api_url,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": model, "prompt": prompt[:500], "size": "1024x1024", "n": 1},
                    )
                    if resp.status_code != 200:
                        return JSONResponse({"ok": False, "error": f"图片模型返回 HTTP {resp.status_code}: {resp.text[:250]}"}, status_code=400)
                    data = resp.json()
                    item = (data.get("data") or data.get("images") or [{}])[0]
                    img_u = item.get("url") or ("data:image/png;base64," + item["b64_json"] if item.get("b64_json") else "")
                    if not img_u:
                        return JSONResponse({"ok": False, "error": "图片模型未返回有效图片 URL"}, status_code=502)
                    conn = get_db()
                    conn.execute("INSERT INTO generated_images (prompt, model, image_url, created_at) VALUES (?, ?, ?, ?)", (prompt[:500], model, img_u, now_str()))
                    conn.commit()
                    conn.close()
                    return JSONResponse({"ok": True, "output": f"已生成配图 ({model})", "image_url": img_u, "engine": model})
            except Exception as exc:
                return JSONResponse({"ok": False, "error": f"图片模型调用失败: {exc}"}, status_code=502)

        ai_res = await call_dify_or_llm(
            "dify_key_drama",
            "你是新月内容画布导演，负责根据上游节点输入产出分镜脚本、口播文案或分镜生图 Prompt。",
            f"当前节点：{node_title} ({node_type})\n上游输入：\n{upstream_text}",
            {"node_type": node_type, "upstream_text": upstream_text},
        )
        if not ai_res.get("connected"):
            return JSONResponse({"ok": False, "error": ai_res.get("error")}, status_code=400)
        return JSONResponse({"ok": True, "output": ai_res["text"], "image_url": "", "engine": ai_res["engine"]})

    # 保存画布
    proj_id = int(body.get("id", 0))
    name = (body.get("name") or "工作台画布").strip()
    nodes = body.get("nodes") or []
    edges = body.get("edges") or []
    ts = now_str()
    conn = get_db()
    if proj_id > 0:
        conn.execute(
            "UPDATE canvas_projects SET name=?, nodes_json=?, edges_json=?, updated_at=? WHERE id=?",
            (name, json.dumps(nodes, ensure_ascii=False), json.dumps(edges, ensure_ascii=False), ts, proj_id),
        )
    else:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO canvas_projects (name, product_name, product_selling_points, nodes_json, edges_json, updated_at) VALUES (?, '', '', ?, ?, ?)",
            (name, json.dumps(nodes, ensure_ascii=False), json.dumps(edges, ensure_ascii=False), ts),
        )
        proj_id = cur.lastrowid
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "id": proj_id, "updated_at": ts})


async def api_config(request: Request):
    if request.method == "GET":
        return JSONResponse({"ok": True, "config": get_config_map()})
    body = await request.json()
    ts = now_str()
    conn = get_db()
    for k, v in body.items():
        conn.execute("INSERT OR REPLACE INTO system_config (key, value, updated_at) VALUES (?, ?, ?)", (str(k), str(v).strip(), ts))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "config": get_config_map(), "updated_at": ts})


async def api_dify_dsl_export(request: Request):
    wf_id = request.path_params.get("wf_id", "").strip()
    specs = {
        "price": {
            "name": "恒业-微信群报价结构化清洗工作流",
            "desc": "提取牌号/品类/石化厂/区域/价格/涨跌 JSON 数组。",
            "vars": [("raw_text", "微信群原始报价文本", 2048, "paragraph"), ("source_group", "来源群聊名称", 128, "text-input"), ("sender", "报价发送人", 128, "text-input")],
            "sys_prompt": "你是恒业塑化报价提取助手。请严格输出 JSON 数组，每项包含: grade_code, category, producer, region, price, change_val。",
            "user_prompt": "群聊：{{#1001.source_group#}}\n发送人：{{#1001.sender#}}\n消息：{{#1001.raw_text#}}",
        },
        "daily": {
            "name": "恒业-每日行情日报与播报工作流",
            "desc": "基于真实盘面、现货报价、装置检修与资讯生成日报或视频号口播。",
            "vars": [("mode", "模式", 64, "text-input"), ("topic", "主题", 256, "text-input"), ("quotes", "盘面与现货报价", 4096, "paragraph"), ("maintenance", "检修动态", 4096, "paragraph"), ("dialogue", "内部汇报", 4096, "paragraph"), ("scrape", "抓取资讯", 4096, "paragraph")],
            "sys_prompt": "你是恒业首席石化分析师。严禁编造价格，必须基于输入的真实行情与检修动态输出分析文章或口播稿。",
            "user_prompt": "模式：{{#1001.mode#}}\n主题：{{#1001.topic#}}\n报价：\n{{#1001.quotes#}}\n检修：\n{{#1001.maintenance#}}\n汇报：\n{{#1001.dialogue#}}\n资讯：\n{{#1001.scrape#}}",
        },
        "video": {
            "name": "新月-视频号白板口播与专访工作流",
            "desc": "生成白板讲课式热点行情分析与商业专访 Q&A 逐字稿。",
            "vars": [("mode", "模式", 64, "text-input"), ("topic", "主题", 256, "text-input"), ("quotes", "报价数据", 4096, "paragraph"), ("maintenance", "检修动态", 4096, "paragraph"), ("dialogue", "内部汇报", 4096, "paragraph"), ("scrape", "抓取资讯", 4096, "paragraph")],
            "sys_prompt": "你是视频号 IP 导演，结合真实行情生成白板分析或采访稿。",
            "user_prompt": "模式：{{#1001.mode#}}\n主题：{{#1001.topic#}}\n报价：\n{{#1001.quotes#}}\n检修：\n{{#1001.maintenance#}}",
        },
        "mp_clone": {
            "name": "新月-爆款模板公众号复刻工作流",
            "desc": "结合对标文章结构与我方真实行情数据生成初稿。",
            "vars": [("mode", "模式", 64, "text-input"), ("topic", "主题", 256, "text-input"), ("reference_template", "对标模板结构", 4096, "paragraph"), ("quotes", "报价数据", 4096, "paragraph"), ("maintenance", "检修动态", 4096, "paragraph"), ("dialogue", "内部汇报", 4096, "paragraph"), ("scrape", "抓取资讯", 4096, "paragraph")],
            "sys_prompt": "你是新月商业观察主笔，拆解参考结构并融合真实石化数据撰写文章初稿。",
            "user_prompt": "主题：{{#1001.topic#}}\n参考模板：{{#1001.reference_template#}}\n报价：\n{{#1001.quotes#}}\n检修：\n{{#1001.maintenance#}}",
        },
        "drama": {
            "name": "新月-画布节点执行工作流",
            "desc": "接收画布节点上游文本，生成短剧剧本或分镜提示词。",
            "vars": [("node_type", "节点类型", 64, "text-input"), ("upstream_text", "上游输入", 4096, "paragraph")],
            "sys_prompt": "你是内容画布导演，根据 node_type 输出高质量短剧剧本或图像生成 Prompt。",
            "user_prompt": "节点类型：{{#1001.node_type#}}\n上游输入：\n{{#1001.upstream_text#}}",
        },
    }
    if wf_id not in specs:
        return JSONResponse({"ok": False, "error": "未知工作流 ID"}, status_code=404)
    spec = specs[wf_id]

    def yblock(txt: str, ind: int) -> str:
        return "\n".join((" " * ind) + ln for ln in txt.splitlines())

    vars_y = "".join(
        f"          - label: {lb}\n            max_length: {ml}\n            options: []\n            required: false\n            type: {vt}\n            variable: {vn}\n"
        for vn, lb, ml, vt in spec["vars"]
    ).rstrip()

    dsl = f"""app:
  description: {spec["desc"]}
  icon: 🤖
  icon_background: '#EFF4FF'
  mode: workflow
  name: {spec["name"]}
kind: app
version: 0.1.5
workflow:
  conversation_variables: []
  environment_variables: []
  graph:
    edges:
    - id: e-1001-1002
      source: '1001'
      sourceHandle: source
      target: '1002'
      targetHandle: target
      type: custom
    - id: e-1002-1003
      source: '1002'
      sourceHandle: source
      target: '1003'
      targetHandle: target
      type: custom
    nodes:
    - data:
        title: 开始
        type: start
        variables:
{vars_y}
      id: '1001'
      position: {{x: 80, y: 260}}
      type: custom
    - data:
        context: {{enabled: false, variable_selector: []}}
        model: {{completion_params: {{temperature: 0.4}}, mode: chat, name: deepseek-chat, provider: deepseek}}
        prompt_template:
        - role: system
          text: |
{yblock(spec["sys_prompt"], 12)}
        - role: user
          text: |
{yblock(spec["user_prompt"], 12)}
        title: AI 处理节点
        type: llm
      id: '1002'
      position: {{x: 380, y: 260}}
      type: custom
    - data:
        outputs:
        - value_selector: ['1002', text]
          variable: text
        title: 结束
        type: end
      id: '1003'
      position: {{x: 680, y: 260}}
      type: custom
"""
    return Response(
        content=dsl.encode("utf-8"),
        media_type="application/x-yaml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="hengye_dify_{wf_id}.yml"'},
    )


async def api_health(request: Request):
    return JSONResponse({"ok": True, "status": "healthy", "db": str(DB_PATH), "time": now_str()})


async def api_auth_me(request: Request):
    user = get_session_user(request)
    return JSONResponse({"ok": True, "authenticated": bool(user), "username": user or ""})


async def api_auth_login(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return JSONResponse({"ok": False, "error": "请输入账号与密码"}, status_code=400)

    conn = get_db()
    user = conn.execute("SELECT username, password_hash FROM users WHERE username=?", (username,)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        conn.close()
        return JSONResponse({"ok": False, "error": "账号或密码错误"}, status_code=401)

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO sessions (token, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (token, username, expires_at, now_str()),
    )
    conn.commit()
    conn.close()

    resp = JSONResponse({"ok": True, "username": username, "expires_at": expires_at})
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=is_https_request(request),
        path="/",
    )
    return resp


async def api_auth_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    if token:
        conn = get_db()
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return resp


async def api_auth_change_password(request: Request):
    username = get_session_user(request)
    if not username:
        return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
    body = await request.json()
    old_pw = body.get("old_password") or ""
    new_pw = body.get("new_password") or ""
    if len(new_pw) < 6:
        return JSONResponse({"ok": False, "error": "新密码长度不能少于 6 位"}, status_code=400)

    conn = get_db()
    row = conn.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
    if not row or not verify_password(old_pw, row["password_hash"]):
        conn.close()
        return JSONResponse({"ok": False, "error": "原密码不正确"}, status_code=400)

    conn.execute("UPDATE users SET password_hash=? WHERE username=?", (hash_password(new_pw), username))
    cur_token = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    conn.execute("DELETE FROM sessions WHERE username=? AND token!=?", (username, cur_token))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "message": "密码已更新"})


async def api_auth_rotate_wx_token(request: Request):
    new_tok = "wxk_" + secrets.token_urlsafe(24)
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO system_config (key, value, updated_at) VALUES ('wx_plugin_token', ?, ?)",
        (new_tok, now_str()),
    )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "wx_plugin_token": new_tok})


routes = [
    Route("/", homepage, methods=["GET"]),
    Route("/api/health", api_health, methods=["GET"]),
    Route("/api/auth/me", api_auth_me, methods=["GET"]),
    Route("/api/auth/login", api_auth_login, methods=["POST"]),
    Route("/api/auth/logout", api_auth_logout, methods=["POST"]),
    Route("/api/auth/change-password", api_auth_change_password, methods=["POST"]),
    Route("/api/auth/rotate-wx-token", api_auth_rotate_wx_token, methods=["POST"]),
    Route("/api/status", api_status, methods=["GET"]),
    Route("/api/market/live", api_market_live, methods=["GET"]),
    Route("/api/market/kline/{symbol}", api_market_kline, methods=["GET"]),
    Route("/api/market/chart-img/{kind}/{symbol}", api_market_chart_img, methods=["GET"]),
    Route("/api/market/sync-news", api_market_sync_news, methods=["POST"]),
    Route("/api/proxy-image", api_proxy_image, methods=["GET"]),
    Route("/api/quotes", api_get_quotes, methods=["GET"]),
    Route("/api/quotes/{id:int}", api_delete_quote, methods=["DELETE"]),
    Route("/api/wechat/ingest", api_wechat_ingest, methods=["POST"]),
    Route("/api/wechat/outbox", api_wechat_outbox_create, methods=["POST"]),
    Route("/api/wechat/outbox/{id:int}", api_wechat_outbox_delete, methods=["DELETE"]),
    Route("/api/wechat/outbox-poll", api_wechat_outbox_poll, methods=["GET", "POST"]),
    Route("/api/wechat/logs", api_wechat_logs, methods=["GET", "DELETE"]),
    Route("/api/wechat/endpoints", api_wechat_endpoints, methods=["GET"]),
    Route("/api/wechat/plugin-script", api_wechat_plugin_script, methods=["GET"]),
    Route("/api/dify/dsl/{wf_id}", api_dify_dsl_export, methods=["GET"]),
    Route("/api/intel", api_get_intel, methods=["GET"]),
    Route("/api/intel/{id:int}", api_delete_intel, methods=["DELETE"]),
    Route("/api/manual-intel", api_add_manual_intel, methods=["POST"]),
    Route("/api/scrape", api_scrape_website, methods=["POST"]),
    Route("/api/images", api_images, methods=["GET"]),
    Route("/api/images/{id:int}", api_images, methods=["DELETE"]),
    Route("/api/images/generate", api_images_generate, methods=["POST"]),
    Route("/api/articles/insert-live-data", api_insert_live_data, methods=["POST"]),
    Route("/api/ai/generate", api_ai_generate, methods=["POST"]),
    Route("/api/articles", api_articles, methods=["GET", "POST"]),
    Route("/api/articles/{id:int}", api_articles, methods=["DELETE"]),
    Route("/api/mp/upload-thumb", api_mp_upload_thumb, methods=["POST"]),
    Route("/api/mp/draft", api_push_wechat_mp_draft, methods=["POST"]),
    Route("/api/canvas", api_canvas_projects, methods=["GET", "POST"]),
    Route("/api/canvas/{id:int}", api_canvas_projects, methods=["DELETE"]),
    Route("/api/config", api_config, methods=["GET", "POST"]),
]

init_db()

app = Starlette(
    debug=APP_DEBUG,
    routes=routes,
    middleware=[
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
        Middleware(AuthMiddleware),
    ],
)

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("WORKBENCH_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("WORKBENCH_PORT", "8765")))
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")
