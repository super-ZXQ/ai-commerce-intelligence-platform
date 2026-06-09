"""Embedding 模型工厂。

默认使用 BGE-small-zh-v1.5（本地、免费、中文 SOTA 级别），
通过 HuggingFaceEmbeddings 加载。首次运行会下载 ~93MB 模型。
"""
from __future__ import annotations

import os
import logging
import threading
from typing import Optional

from langchain_huggingface import HuggingFaceEmbeddings

logger = logging.getLogger(__name__)

# 锁确保多线程下只实例化一次
_instance_lock = threading.Lock()
_cached: dict[str, HuggingFaceEmbeddings] = {}

DEFAULT_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")


def _device() -> str:
    """检测可用设备：cuda / mps / cpu。"""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


def get_embeddings(model_name: str = DEFAULT_MODEL,
                   cache_folder: Optional[str] = None) -> HuggingFaceEmbeddings:
    """获取（或惰性创建）Embedding 单例。

    Args:
        model_name: HuggingFace 模型 ID，默认 BGE-small-zh-v1.5。
        cache_folder: 模型下载目录，默认 ~/.cache/huggingface。

    Returns:
        HuggingFaceEmbeddings 实例。
    """
    cache_key = f"{model_name}:{cache_folder}"
    if cache_key in _cached:
        return _cached[cache_key]
    with _instance_lock:
        if cache_key in _cached:
            return _cached[cache_key]
        device = _device()
        logger.info("加载 Embedding 模型: %s (device=%s)", model_name, device)
        kwargs = {
            "model_name": model_name,
            "model_kwargs": {"device": device},
            "encode_kwargs": {"normalize_embeddings": True, "batch_size": 32},
        }
        if cache_folder:
            kwargs["cache_folder"] = cache_folder
        embed = HuggingFaceEmbeddings(**kwargs)
        _cached[cache_key] = embed
        logger.info("✅ Embedding 加载完成")
        return embed


def warm_up():
    """预热：在 Streamlit 启动时同步调用，避免首次查询时下载卡顿。"""
    embed = get_embeddings()
    # 触发模型加载
    embed.embed_query("warm up")
    return embed
