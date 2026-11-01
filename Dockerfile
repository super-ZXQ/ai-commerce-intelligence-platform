FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt ./backend/
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.1.2" "setuptools>=83.0.0" \
    && pip install --no-cache-dir -r ./backend/requirements.txt

COPY backend/ ./backend/
COPY agent_core/ ./agent_core/
COPY ai-ecommerce-assistant/knowledge_base/ ./ai-ecommerce-assistant/knowledge_base/
COPY data/ /app/data/
COPY sql/ /app/sql/

RUN python -c "import agent_core"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/health'); r.raise_for_status()" || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
