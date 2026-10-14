FROM python:3.12-slim

WORKDIR /app/ai-ecommerce-assistant

# 注意：此处不用 ai-ecommerce-assistant/requirements.lock。该锁按 PyPI 解析，
# 含 sys_platform == 'linux' 激活的 nvidia-*/cuda-* 条目，与下方 CPU 专属源
# 安装的 torch==2.13.0+cpu 互斥（会把 CUDA 全家桶拖进镜像）。
# requirements.txt 的直接依赖本身已全部 == 钉死，CPU 镜像构建可复现。
COPY ai-ecommerce-assistant/requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.1.2" "setuptools>=83.0.0" \
    && pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==2.13.0+cpu" \
    && pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/backend/
COPY agent_core/ /app/agent_core/
COPY ai-ecommerce-assistant/ ./

RUN PYTHONPATH=/app python -c "import agent_core"

EXPOSE 8502

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8502/ai/_stcore/health'); r.raise_for_status()" || exit 1

CMD ["streamlit", "run", "app.py", "--server.port=8502", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false", "--server.fileWatcherType=none"]
