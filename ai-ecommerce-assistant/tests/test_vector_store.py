"""VectorStore 集成测试：使用 fake embedder + 临时目录，不依赖真实模型。"""
from __future__ import annotations

import pytest
from langchain_core.documents import Document

from rag.vector_store import VectorStore


@pytest.fixture
def store(fake_embeddings, tmp_chroma_dir):
    return VectorStore(
        persist_dir=tmp_chroma_dir,
        collection_name="test_kb",
        embedding=fake_embeddings,
    )


def test_empty_store_returns_zero_count(store):
    assert store.count() == 0


def test_search_on_empty_store_returns_empty(fake_embeddings, tmp_chroma_dir):
    s = VectorStore(
        persist_dir=tmp_chroma_dir, collection_name="empty", embedding=fake_embeddings,
    )
    assert s.search("anything") == []


def test_add_documents_increments_count(store):
    docs = [Document(page_content="hello", metadata={"doc_id": "d1"})]
    store.add_documents(docs, ids=["d1"])
    assert store.count() == 1


def test_add_documents_returns_ids(store):
    docs = [
        Document(page_content="a", metadata={"doc_id": "d1"}),
        Document(page_content="b", metadata={"doc_id": "d2"}),
    ]
    ids = store.add_documents(docs, ids=["d1", "d2"])
    assert ids == ["d1", "d2"]


def test_add_documents_id_auto_from_metadata(fake_embeddings, tmp_chroma_dir):
    """未传 ids 时，从 metadata.doc_id 兜底。"""
    s = VectorStore(persist_dir=tmp_chroma_dir, collection_name="auto", embedding=fake_embeddings)
    docs = [Document(page_content="x", metadata={"doc_id": "auto-1"})]
    s.add_documents(docs)
    assert s.count() == 1
    assert "auto-1" in s.get_all_ids()


def test_add_documents_id_collision_overwrites(store):
    """重复 ID 应覆盖，不抛异常。"""
    docs1 = [Document(page_content="v1", metadata={"doc_id": "d1"})]
    docs2 = [Document(page_content="v2", metadata={"doc_id": "d1"})]
    store.add_documents(docs1, ids=["d1"])
    store.add_documents(docs2, ids=["d1"])
    assert store.count() == 1
    results = store.search("v2", k=1)
    # 应该能找到 v2 而不是 v1
    assert any("v2" in r["content"] for r in results)


def test_search_finds_added_doc(store):
    doc = Document(
        page_content="复购率定义",
        metadata={"doc_id": "d1", "source": "biz.md", "section": "复购率"},
    )
    store.add_documents([doc], ids=["d1"])
    # 相同 query 应命中（fake embedder 完全一致）
    results = store.search("复购率定义", k=3)
    assert len(results) >= 1
    assert any("复购率定义" in r["content"] for r in results)


def test_search_returns_score_and_metadata(store):
    doc = Document(
        page_content="alpha",
        metadata={"doc_id": "d1", "source": "a.md", "section": "S"},
    )
    store.add_documents([doc], ids=["d1"])
    results = store.search("alpha", k=1)
    assert len(results) == 1
    r = results[0]
    assert "score" in r
    assert "metadata" in r
    assert r["metadata"]["source"] == "a.md"


def test_search_score_threshold_filter(fake_embeddings, tmp_chroma_dir):
    """阈值过滤：score < 阈值的结果被过滤。

    归一化方案：search() 用 Chroma similarity_search_with_relevance_scores，
    score 范围 0-1（1=完全相同）。fake embedder 命中时 score≈1.0。
    """
    s = VectorStore(persist_dir=tmp_chroma_dir, collection_name="threshold_test", embedding=fake_embeddings)
    s.add_documents(
        [Document(page_content="only doc", metadata={"doc_id": "d1"})],
        ids=["d1"],
    )
    # 阈值设到 1.5（>1.0）：fake embedder 命中时 score≈1.0 < 1.5 → 过滤
    results = s.search("only doc", k=3, score_threshold=1.5)
    assert results == []
    # 阈值设到 0.5：score≈1.0 >= 0.5 → 保留
    results = s.search("only doc", k=3, score_threshold=0.5)
    assert len(results) >= 1


def test_search_respects_k(store):
    """k 参数应限制返回数量。"""
    docs = [
        Document(page_content=f"doc {i}", metadata={"doc_id": f"d{i}"})
        for i in range(5)
    ]
    store.add_documents(docs, ids=[f"d{i}" for i in range(5)])
    results = store.search("doc", k=2)
    assert len(results) <= 2


def test_delete_all_clears_count(store):
    store.add_documents(
        [Document(page_content="x", metadata={"doc_id": "d1"})],
        ids=["d1"],
    )
    assert store.count() == 1
    store.delete_all()
    assert store.count() == 0


def test_reset_clears_and_keeps_usable(store):
    store.add_documents(
        [Document(page_content="x", metadata={"doc_id": "d1"})],
        ids=["d1"],
    )
    store.reset(force_physical=False)
    assert store.count() == 0
    # reset 后仍可继续使用
    store.add_documents(
        [Document(page_content="y", metadata={"doc_id": "d2"})],
        ids=["d2"],
    )
    assert store.count() == 1
