FROM python:3.12-slim

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 安装运行依赖（本地/国内默认清华源，GitHub Actions 构建可传入官方 PyPI 源）
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} \
    starlette uvicorn httpx beautifulsoup4 python-multipart

COPY server.py index.html /app/

RUN mkdir -p /data
VOLUME ["/data"]

ENV WORKBENCH_DB_PATH=/data/workbench.db \
    WORKBENCH_HOST=0.0.0.0 \
    PORT=8765 \
    WORKBENCH_DEBUG=0 \
    PYTHONUNBUFFERED=1

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT','8765') + '/api/health')" || exit 1

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8765} --proxy-headers --forwarded-allow-ips='*'"]
