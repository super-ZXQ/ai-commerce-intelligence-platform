FROM python:3.12-slim

# 不生成 .pyc、不缓冲日志；固定 HOME 供以非 root 身份运行时使用。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app

WORKDIR /app

COPY backend/requirements.txt ./backend/
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.1.2" "setuptools>=83.0.0" \
    && pip install --no-cache-dir -r ./backend/requirements.txt

# 情节向量模型：构建时通过镜像源预热；失败不阻塞构建，运行时再懒加载。
ENV HF_ENDPOINT=https://hf-mirror.com \
    HF_HOME=/home/app/.cache/huggingface
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-zh-v1.5')" || true
RUN chown -R 10001:10001 /tmp/fastembed_cache 2>/dev/null || true

COPY backend/ ./backend/
COPY agent_core/ ./agent_core/
COPY ai-ecommerce-assistant/knowledge_base/ ./ai-ecommerce-assistant/knowledge_base/
# 数据文件不烘进镜像：data-import 服务通过 ./data:/app/data:ro 挂载。
# 顶层 sql/ 仅作参考 DDL，运行时由 Alembic 迁移，镜像不需要。

RUN python -c "import agent_core" \
    && mkdir -p /home/app \
    && chown -R 10001:10001 /home/app

# 非 root 运行：应用只读代码与数据卷，不需要任何特权。
USER 10001:10001

EXPOSE 8000

# 容器健康 = 可接流量：/readyz 校验业务库、事件库、认证库三个连接池。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/readyz'); r.raise_for_status()" || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
