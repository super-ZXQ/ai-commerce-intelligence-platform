"""知识库构建脚本。

职责：
1. 扫描 knowledge_base/ 目录下的所有 .md 文件
2. 按章节（## 标题）切分 chunk
3. 为每个 chunk 生成稳定 doc_id（基于内容哈希）
4. 与已有向量库对比，仅增量新增 / 更新 / 删除
5. 输出构建统计报告

使用：
    # 全量重建
    python build_knowledge_base.py --rebuild

    # 增量构建（默认）
    python build_knowledge_base.py

    # 指定目录
    python build_knowledge_base.py --kb-dir ./knowledge_base
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterator

# 让脚本可独立运行
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from langchain_core.documents import Document
from rag import VectorStore, get_embeddings, DEFAULT_PERSIST_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("build_kb")

# ── 切分配置 ──
MAX_CHUNK_CHARS = 1500  # 单 chunk 字符上限（粗略，约 500 tokens）
CHUNK_OVERLAP = 200     # 相邻 chunk 重叠字符
H2_PATTERN = re.compile(r"^##\s+(.+)$", re.MULTILINE)
H3_PATTERN = re.compile(r"^###\s+(.+)$", re.MULTILINE)
CODE_FENCE = re.compile(r"```[\s\S]*?```", re.MULTILINE)


def _doc_type(filename: str) -> str:
    """从文件名推断文档类型（用于元数据过滤）。"""
    name = filename.lower()
    if "glossary" in name:
        return "glossary"
    if "dictionary" in name:
        return "data_dict"
    if "kpi" in name or "formula" in name:
        return "kpi"
    if "gold" in name or "query" in name:
        return "gold_query"
    if "rule" in name or "benchmark" in name:
        return "business_rule"
    if "api" in name or "doc" in name:
        return "api_doc"
    return "other"


def _make_doc_id(source: str, section: str, content: str) -> str:
    raw = f"{source}::{section}::{content[:200]}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _split_by_sections(text: str) -> Iterator[tuple[str, str]]:
    """按 H2 标题切分文档。

    Yields: (section_title, section_content)
    """
    matches = list(H2_PATTERN.finditer(text))
    if not matches:
        yield ("", text.strip())
        return
    # 文档开头（在第一个 H2 之前）作为"前言"
    preamble = text[: matches[0].start()].strip()
    if preamble:
        yield ("前言/概述", preamble)
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            yield (title, content)


def _split_long_section(content: str, max_chars: int, overlap: int) -> list[str]:
    """对超长 section 按字符数再切（保留 H3 边界优先）。"""
    if len(content) <= max_chars:
        return [content]
    chunks: list[str] = []
    # 优先按 H3 切
    h3_matches = list(H3_PATTERN.finditer(content))
    if h3_matches:
        # H3 之前的前言部分
        head = content[: h3_matches[0].start()].strip()
        if head:
            chunks.extend(_sliding_window(head, max_chars, overlap))
        for i, m in enumerate(h3_matches):
            sub_start = m.start()
            sub_end = h3_matches[i + 1].start() if i + 1 < len(h3_matches) else len(content)
            sub = content[sub_start:sub_end].strip()
            if sub:
                chunks.extend(_sliding_window(sub, max_chars, overlap))
    else:
        chunks.extend(_sliding_window(content, max_chars, overlap))
    return chunks


def _sliding_window(text: str, max_chars: int, overlap: int) -> list[str]:
    """滑动窗口切分（带重叠）。"""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunk = text[start:end]
        # 尝试在自然断点切（句号/换行）
        if end < len(text):
            for sep in ["\n\n", "\n", "。", ". "]:
                last = chunk.rfind(sep)
                if last > max_chars * 0.6:
                    chunk = chunk[: last + len(sep)]
                    end = start + len(chunk)
                    break
        chunks.append(chunk.strip())
        if end >= len(text):
            break
        start = end - overlap
    return [c for c in chunks if c]


def load_kb_files(kb_dir: str) -> list[Document]:
    """加载并切分知识库目录下所有 .md 文件。"""
    kb_path = Path(kb_dir)
    if not kb_path.exists():
        raise FileNotFoundError(f"知识库目录不存在: {kb_dir}")

    md_files = sorted(kb_path.glob("*.md"))
    if not md_files:
        raise ValueError(f"知识库目录无 .md 文件: {kb_dir}")

    documents: list[Document] = []
    for fp in md_files:
        text = fp.read_text(encoding="utf-8")
        file_meta = {
            "source": str(fp),
            "filename": fp.name,
            "doc_type": _doc_type(fp.name),
        }
        for section_title, section_content in _split_by_sections(text):
            for chunk in _split_long_section(section_content, MAX_CHUNK_CHARS, CHUNK_OVERLAP):
                doc_id = _make_doc_id(fp.name, section_title, chunk)
                # 移除代码块（节省 token，SQL 示例放进 gold_queries 即可）
                chunk_clean = CODE_FENCE.sub("[代码示例已省略，详见源文档]", chunk)
                documents.append(Document(
                    page_content=chunk_clean,
                    metadata={
                        **file_meta,
                        "section": section_title or "前言/概述",
                        "doc_id": doc_id,
                    },
                ))
    logger.info("📄 加载 %d 个文件 → %d 个 chunk", len(md_files), len(documents))
    return documents


def build(kb_dir: str, persist_dir: str, rebuild: bool = False) -> dict:
    """构建（或增量更新）向量库。

    Returns:
        构建报告 dict。
    """
    t0 = time.time()
    store = VectorStore(persist_dir=persist_dir, embedding=get_embeddings())

    if rebuild:
        logger.warning("🗑️  重建模式：先清空向量库")
        store.reset()

    existing_count = store.count()
    logger.info("当前向量库文档数: %d", existing_count)

    documents = load_kb_files(kb_dir)
    if not documents:
        return {"status": "empty", "chunks": 0}

    # 增量：仅写入 / 更新新增或变更的 doc
    existing_ids = set(store.get_all_ids())
    logger.info("现有 doc_id 数量: %d", len(existing_ids))

    to_write: list[Document] = []
    to_write_ids: list[str] = []
    new_ids: set[str] = set()
    for doc in documents:
        doc_id = doc.metadata["doc_id"]
        new_ids.add(doc_id)
        if doc_id not in existing_ids:
            to_write.append(doc)
            to_write_ids.append(doc_id)

    # 找出已删除的 doc（源文档已无此 chunk）
    deleted_ids = existing_ids - new_ids
    if deleted_ids:
        logger.info("🗑️  清理 %d 个废弃 doc", len(deleted_ids))
        try:
            store._store.delete(ids=list(deleted_ids))
        except Exception as e:
            logger.warning("废弃 doc 清理失败（可忽略）: %s", e)

    if to_write:
        logger.info("✏️  写入 %d 个新 chunk...", len(to_write))
        store.add_documents(to_write, ids=to_write_ids)
    else:
        logger.info("✅ 无变更，跳过写入")

    elapsed = time.time() - t0
    final_count = store.count()
    report = {
        "status": "ok",
        "kb_dir": kb_dir,
        "persist_dir": persist_dir,
        "files": len(list(Path(kb_dir).glob("*.md"))),
        "chunks_total": len(documents),
        "chunks_new": len(to_write),
        "chunks_removed": len(deleted_ids),
        "vector_count": final_count,
        "elapsed_s": round(elapsed, 2),
    }
    return report


def main():
    parser = argparse.ArgumentParser(description="构建 AI 助手业务知识向量库")
    parser.add_argument("--kb-dir", default=str(_PROJECT_ROOT / "knowledge_base"),
                        help="知识库 Markdown 目录")
    parser.add_argument("--persist-dir", default=DEFAULT_PERSIST_DIR,
                        help="向量库持久化目录")
    parser.add_argument("--rebuild", action="store_true",
                        help="全量重建（先清空向量库）")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("🚀 AI 助手业务知识库构建")
    logger.info("=" * 60)
    logger.info("知识库目录: %s", args.kb_dir)
    logger.info("持久化目录: %s", args.persist_dir)
    logger.info("模式: %s", "全量重建" if args.rebuild else "增量更新")
    logger.info("")

    report = build(args.kb_dir, args.persist_dir, args.rebuild)
    logger.info("")
    logger.info("=" * 60)
    logger.info("📊 构建报告")
    logger.info("=" * 60)
    logger.info(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
