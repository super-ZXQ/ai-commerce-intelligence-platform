"""Lazy RAG 加载代理测试。

设计目标：
  1. 验证模块顶层 import app.py 不拉起 BGE/Chroma（用户首屏不付 RAG 代价）
  2. 验证 _LazyRetriever 接口对齐真实 Retriever（retrieve/get_stats/dump_stats）
  3. 验证 knowledge 类问题路径触发懒加载，data 类问题路径不触发
  4. 验证 init 失败时 retrieve() 优雅降级为空列表
  5. 验证多次调用复用同一实例（不会重复触发 init_retriever）
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch


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
    import subprocess
    from pathlib import Path

    # 新增Chroma集成测试会先加载Rust扩展；真正冷启动必须用独立解释器验证。
    code = "import sys; import app; assert not any(k == 'rag' or k.startswith(('rag.', 'chromadb', 'sentence_transformers')) for k in sys.modules)"
    subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True)


def test_lazy_retriever_initial_state():
    """新建的 _LazyRetriever 必须显示未初始化、未触发任何 RAG 加载。"""
    _purge_app()
    import app
    r = app._LazyRetriever()
    assert r.status == {"ok": False, "error": "not initialized yet", "count": 0}
    assert r._inner is None
    assert r._initialized is False


def test_lazy_retriever_retrieve_uninitialized_returns_empty():
    """未初始化时 retrieve() 必须返回空列表（旧行为 if _retriever is None: return []）。"""
    _purge_app()
    import app
    r = app._LazyRetriever()
    # 内部 init 应被触发一次
    with patch("app.init_retriever", return_value=(None, {"ok": False, "error": "no KB", "count": 0})):
        out = r.retrieve("什么是复购率？", k=3)
    assert out == []
    assert r._initialized is True
    assert r.status["ok"] is False


def test_lazy_retriever_init_failure_is_graceful():
    """init_retriever 抛异常时 _ensure 必须捕获，status.ok=False，retrieve() 仍返回空。"""
    _purge_app()
    import app
    r = app._LazyRetriever()
    with patch("app.init_retriever", side_effect=RuntimeError("Chroma 不可用")):
        out = r.retrieve("anything", k=3)
    assert out == []
    assert r.status["ok"] is False
    assert "Chroma 不可用" in r.status["error"]


def test_lazy_retriever_reuses_instance_across_calls():
    """多次 retrieve/get_stats 调用只触发一次 init_retriever（_ensure 幂等）。"""
    _purge_app()
    import app
    inner = MagicMock()
    inner.retrieve.return_value = [{"content": "x", "metadata": {}, "score": 0.9}]
    inner.get_stats.return_value = {"calls": 1}
    status = {"ok": True, "error": None, "count": 5}

    with patch("app.init_retriever", return_value=(inner, status)) as mock_init:
        r = app._LazyRetriever()
        r.retrieve("q1", k=3)
        r.retrieve("q2", k=3)
        r.get_stats()
        r.dump_stats()
        # init 只应被调用一次
        assert mock_init.call_count == 1, f"init should be called once, got {mock_init.call_count}"
        # 但 inner 接收了所有调用
        assert inner.retrieve.call_count == 2
        assert inner.get_stats.call_count == 1
        assert inner.dump_stats.call_count == 1


def test_lazy_retriever_status_reference_stays_fresh():
    """回归：模块层捕获的 status 引用（app.py 的 rag_status = retriever.status）
    必须在懒加载完成后读到新值。

    _ensure 曾用整体赋值替换 status dict，外部捕获的旧引用永远停留在
    "未初始化"，导致 RAG 面板即使初始化成功也一直显示"未启用"。
    """
    _purge_app()
    import app
    r = app._LazyRetriever()
    captured = r.status  # 与 app.py 的 rag_status 同一引用

    inner = MagicMock()
    with patch("app.init_retriever", return_value=(inner, {"ok": True, "error": None, "count": 5})):
        r.retrieve("q")

    assert captured is r.status, "status 必须原地更新，不能替换 dict 对象"
    assert captured["ok"] is True
    assert captured["count"] == 5
