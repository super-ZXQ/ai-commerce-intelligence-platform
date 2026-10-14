FROM python:3.12-slim

WORKDIR /app

# 使用 requirements.lock 安装：传递依赖版本完全钉死，镜像构建可复现
COPY backend/requirements.lock ./backend/
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.1.2" "setuptools>=83.0.0" \
    && pip install --no-cache-dir -r ./backend/requirements.lock

COPY backend/ ./backend/
COPY agent_core/ ./agent_core/
COPY ai-ecommerce-assistant/knowledge_base/ ./ai-ecommerce-assistant/knowledge_base/
COPY data/ /app/data/
COPY sql/ /app/sql/

RUN python -c "import agent_core"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/health'); r.raise_for_status()" || exit 1

CMD ["sh", "-c", "python backend/scripts/sync_orders.py && exec uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
