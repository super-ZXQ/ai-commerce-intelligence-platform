"""冷导入测试：助手进程只做 UI，RAG 由后端持有，导入时不得拉起重依赖。"""
from __future__ import annotations

import sys


def _purge_app():
    """从 sys.modules 清掉 app 及其依赖，以及任何已加载的 RAG 子模块。

    关键：必须连 rag.* / chromadb.* / sentence_transformers.* 一起清，
    否则上一个测试套件如果触发过 RAG import，这个 cold-import 断言就形同虚设。
    """
    purge_prefixes = (
        "app", "rag", "chromadb", "sentence_transformers", "huggingface",
    )
    for k in list(sys.modules):
        if any(k == p or k.startswith(p + ".") for p in purge_prefixes):
            del sys.modules[k]


def test_app_cold_import_does_not_load_rag():
    """模块冷启动：import app 后 rag/chromadb/sentence_transformers 不应在 sys.modules。"""
    _purge_app()
    import app  # noqa: F401
    heavy = [
        k for k in sys.modules
        if k == "rag" or k.startswith("rag.")
        or k.startswith("chromadb") or k.startswith("sentence_transformers")
    ]
    assert heavy == [], f"heavy RAG deps should not be loaded at import time, got: {heavy[:5]}"
