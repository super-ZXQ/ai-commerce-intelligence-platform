"""真实Chroma+FakeEmbedding集成；不声称为真实BGE质量评测。"""
import pytest

import build_knowledge_base as builder
from rag import Retriever, VectorStore


def test_tail_update_deletes_old_vector_and_refreshes_existing_retriever(tmp_path, fake_embeddings, monkeypatch):
    kb = tmp_path / "kb"
    kb.mkdir()
    source = kb / "a.md"
    source.write_text("## 指标\n" + "前缀" * 150 + "旧内容", encoding="utf-8")
    monkeypatch.setattr(builder, "get_embeddings", lambda: fake_embeddings)
    persist = str(tmp_path / "vectors")
    first = builder.build(str(kb), persist)
    store = VectorStore(persist_dir=persist, embedding=fake_embeddings)
    old_ids = store.get_all_ids()
    retriever = Retriever(store, score_threshold=0, stats_path=str(tmp_path / "stats.json"))
    assert "旧内容" in retriever.retrieve("指标")[0]["content"]
    version = store.knowledge_version
    source.write_text("## 指标\n" + "前缀" * 150 + "新内容", encoding="utf-8")
    changed = builder.build(str(kb), persist)
    assert first["vector_count"] == changed["vector_count"] == 1
    assert changed["chunks_new"] == changed["chunks_removed"] == 1
    assert set(store.get_all_ids()).isdisjoint(old_ids)
    assert store.knowledge_version != version
    assert "新内容" in retriever.retrieve("指标")[0]["content"]
    repeated = builder.build(str(kb), persist)
    assert repeated["chunks_new"] == repeated["chunks_removed"] == 0
    assert repeated["vector_count"] == 1


def test_repeated_sections_do_not_duplicate_ids(tmp_path, fake_embeddings, monkeypatch):
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "a.md").write_text("## A\n相同内容\n## A\n相同内容", encoding="utf-8")
    monkeypatch.setattr(builder, "get_embeddings", lambda: fake_embeddings)
    assert builder.build(str(kb), str(tmp_path / "vectors"))["vector_count"] == 1


def test_delete_error_does_not_report_success(tmp_path, fake_embeddings, monkeypatch):
    kb = tmp_path / "kb"
    kb.mkdir()
    source = kb / "a.md"
    source.write_text("## A\nold", encoding="utf-8")
    monkeypatch.setattr(builder, "get_embeddings", lambda: fake_embeddings)
    persist = str(tmp_path / "vectors")
    builder.build(str(kb), persist)
    source.write_text("## A\nnew", encoding="utf-8")

    def broken(*a, **k):
        raise RuntimeError("delete unavailable")

    monkeypatch.setattr(VectorStore, "delete_documents", broken)
    with pytest.raises(RuntimeError, match="delete unavailable"):
        builder.build(str(kb), persist)


def test_removing_last_source_clears_vectors(tmp_path, fake_embeddings, monkeypatch):
    kb = tmp_path / "kb"
    kb.mkdir()
    source = kb / "a.md"
    source.write_text("## A\nold", encoding="utf-8")
    monkeypatch.setattr(builder, "get_embeddings", lambda: fake_embeddings)
    persist = str(tmp_path / "vectors")
    builder.build(str(kb), persist)
    source.unlink()
    report = builder.build(str(kb), persist)
    assert report["chunks_removed"] == 1
    assert report["vector_count"] == 0
