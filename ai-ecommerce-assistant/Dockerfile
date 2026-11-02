FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app

WORKDIR /app/ai-ecommerce-assistant

COPY ai-ecommerce-assistant/requirements.txt .
# BuildKit 在现代 Docker Desktop 中禁止构建期 host 网络；默认 bridge 网络即可访问 PyPI。
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.1.2" "setuptools>=83.0.0" \
    && pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==2.13.0+cpu" \
    && pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/backend/
COPY agent_core/ /app/agent_core/
COPY ai-ecommerce-assistant/ ./

RUN PYTHONPATH=/app python -c "import agent_core" \
    && mkdir -p /home/app \
    && chown 10001:10001 /home/app

USER 10001:10001

EXPOSE 8502

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8502/ai/_stcore/health'); r.raise_for_status()" || exit 1

CMD ["streamlit", "run", "app.py", "--server.port=8502", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false", "--server.fileWatcherType=none"]
