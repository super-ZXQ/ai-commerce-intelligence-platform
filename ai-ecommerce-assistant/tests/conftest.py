"""pytest 共享 fixture。

设计原则：
- 不依赖真实 Embedding 模型（避免 93MB 下载）
- 临时目录隔离 Chroma 持久化
- 确定性 fake embedder：相同文本→相同向量
"""
from __future__ import annotations

import hashlib
import math
import shutil
import sys
from pathlib import Path

import pytest

# 让 ai-ecommerce-assistant 可作为包导入
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class FakeEmbeddings:
    """确定性 fake embedder，绕过 HuggingFace 真实模型。

    特点：
    - 相同文本 → 完全相同向量
    - 不同文本 → 几乎正交（hash 决定）
    - 384 维 L2 归一化（兼容 Chroma 余弦距离）
    """

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        raw: list[float] = []
        seed = text or " "
        counter = 0
        while len(raw) < self.dim:
            digest = hashlib.sha256(f"{seed}::{counter}".encode()).digest()
            for byte in digest:
                if len(raw) >= self.dim:
                    break
                # [-1, 1] 范围
                raw.append(byte / 127.5 - 1.0)
            counter += 1
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


@pytest.fixture
def fake_embeddings() -> FakeEmbeddings:
    """默认 64 维，测试用足够。"""
    return FakeEmbeddings(dim=64)


@pytest.fixture
def tmp_chroma_dir(tmp_path: Path) -> str:
    """每个测试用独立临时目录，测试结束自动清理。

    Windows 上 Chroma 的 sqlite 文件可能被句柄持有，
    fixture 退出时强制 best-effort 清理。
    """
    d = tmp_path / "chroma"
    d.mkdir()
    yield str(d)
    # 强制释放 Windows 上的文件句柄
    import gc
    gc.collect()
    shutil.rmtree(d, ignore_errors=True)
