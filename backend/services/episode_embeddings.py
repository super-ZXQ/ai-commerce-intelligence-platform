"""本地向量嵌入：FastEmbed + BGE，零外部服务，惰性加载。"""

from __future__ import annotations

import logging
import threading

from backend.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
_model = None
_model_lock = threading.Lock()


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """返回归一化向量；依赖缺失、模型不可用或下载失败时返回 None。

    调用方必须容忍 None 并降级到关键词召回，不能让向量故障中断问答。
    """
    global _model
    try:
        # 延迟导入：未安装 fastembed 的本地/测试环境仍可启动。
        from fastembed import TextEmbedding
    except ImportError:
        logger.warning("fastembed 未安装，情节召回使用关键词降级")
        return None

    try:
        with _model_lock:
            if _model is None:
                _model = TextEmbedding(model_name=settings.agent_episode_embedding_model)
            vectors = list(_model.embed(texts))
        return [[float(value) for value in vector] for vector in vectors]
    except Exception as exc:  # noqa: BLE001 - 模型下载/推理失败都降级
        logger.warning("向量模型不可用: %s", type(exc).__name__)
        return None
