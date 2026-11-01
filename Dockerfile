FROM python:3.12-slim

# 不生成 .pyc、不缓冲日志；固定 HOME 供以非 root 身份运行时使用。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app

WORKDIR /app

COPY backend/requirements.txt ./backend/
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.1.2" "setuptools>=83.0.0" \
    && pip install --no-cache-dir -r ./backend/requirements.txt

COPY backend/ ./backend/
COPY agent_core/ ./agent_core/
COPY ai-ecommerce-assistant/knowledge_base/ ./ai-ecommerce-assistant/knowledge_base/
COPY data/ /app/data/
COPY sql/ /app/sql/

RUN python -c "import agent_core" \
    && mkdir -p /home/app \
    && chown 10001:10001 /home/app

# 非 root 运行：应用只读代码与数据卷，不需要任何特权。
USER 10001:10001

EXPOSE 8000

# 容器健康 = 可接流量：/readyz 校验业务库、事件库、认证库三个连接池。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/readyz'); r.raise_for_status()" || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
