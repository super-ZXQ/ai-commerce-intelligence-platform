"""RAG 评估脚本：对 gold_qa.jsonl 中的问题跑检索，输出命中率报告。

设计：
- 知识类（category=knowledge）：检索应命中含 expected_keywords 的文档
- 数据类（category=data）：默认跳过评估（评估的是 SQL 能力，不在 RAG 范围）
- 默认使用 fake embedder 跑快速冒烟；--real 切换到真实 BGE 模型

用法：
    python eval/run_eval.py                    # fake embedder 冒烟
    python eval/run_eval.py --real             # 真实 embedding（首次会下载 93MB）
    python eval/run_eval.py --report eval/report.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# 让 ai-ecommerce-assistant 可作为包导入
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag.retriever import Retriever  # noqa: E402


def load_gold(path: Path) -> list[dict]:
    items = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def fake_retriever() -> Retriever:
    """用 fake embedder + 内置知识子集跑冒烟评估。"""
    from unittest.mock import MagicMock
    from tests.conftest import FakeEmbeddings

    fake = FakeEmbeddings(dim=32)
    store = MagicMock()
    store.count.return_value = 6

    # 模拟 6 个业务知识 chunk
    KB = [
        {"content": "复购率 = 消费2次及以上用户数 / 总用户数", "metadata": {"source": "biz_glossary.md", "doc_type": "glossary", "section": "复购率"}, "score": 0.95},
        {"content": "客单价 = 总付款金额 / 总订单数", "metadata": {"source": "kpi_formulas.md", "doc_type": "kpi", "section": "客单价"}, "score": 0.92},
        {"content": "退款率行业基准约 2%-8%", "metadata": {"source": "business_rules.md", "doc_type": "business_rule", "section": "退款率基准"}, "score": 0.88},
        {"content": "payment_amount: 实际付款金额（单位元）", "metadata": {"source": "data_dictionary.md", "doc_type": "data_dict", "section": "payment_amount"}, "score": 0.85},
        {"content": "is_refunded: 是/否", "metadata": {"source": "data_dictionary.md", "doc_type": "data_dict", "section": "is_refunded"}, "score": 0.83},
        {"content": "Recency≤30 天为活跃用户", "metadata": {"source": "biz_glossary.md", "doc_type": "glossary", "section": "RFM"}, "score": 0.80},
    ]

    def _search(query, k=3, score_threshold=None, filter=None):
        # 假检索：找到包含 query 子串的 doc
        matched = []
        for doc in KB:
            if any(tok in doc["content"] or tok in doc["metadata"]["section"]
                   for tok in query.split()):
                matched.append(doc)
        # 补足 k
        for doc in KB:
            if len(matched) >= k:
                break
            if doc not in matched:
                matched.append(doc)
        if filter:
            matched = [d for d in matched if d["metadata"].get("doc_type") in (
                filter.get("doc_type"), )]
        return matched[:k]

    store.search.side_effect = _search
    return Retriever(store, k=3, score_threshold=0.0, cache_max=64, cache_ttl=60)


def real_retriever(persist_dir: str) -> Retriever:
    """用真实 Chroma + BGE embedding 跑评估。"""
    from rag import VectorStore, get_embeddings

    embed = get_embeddings()
    store = VectorStore(persist_dir=persist_dir, embedding=embed)
    if store.count() == 0:
        raise SystemExit(
            f"❌ 向量库为空: {persist_dir}\n"
            "请先运行: python build_knowledge_base.py"
        )
    return Retriever(store, k=3, score_threshold=0.5, cache_max=128, cache_ttl=60)


def evaluate_item(retriever: Retriever, item: dict) -> dict:
    """评估单条 gold。"""
    t0 = time.time()
    if item["category"] == "knowledge":
        docs = retriever.retrieve(item["question"], k=3)
        elapsed = (time.time() - t0) * 1000

        # 评估：检索结果里是否至少有一条含 expected_keywords
        expected = item.get("expected_keywords", [])
        expected_doc = item.get("expected_doc")
        hit = False
        hit_doc_type = None
        for d in docs:
            content = d.get("content", "")
            if all(kw in content for kw in expected):
                hit = True
                hit_doc_type = d.get("metadata", {}).get("doc_type")
                break
        # 若 expected_doc 指定，验证 doc_type 一致
        doc_type_ok = True
        if expected_doc and hit_doc_type:
            doc_type_ok = (hit_doc_type == expected_doc)

        return {
            "id": item["id"],
            "category": "knowledge",
            "question": item["question"],
            "hit": hit,
            "doc_type_ok": doc_type_ok,
            "expected_doc": expected_doc,
            "hit_doc_type": hit_doc_type,
            "n_results": len(docs),
            "top_score": docs[0]["score"] if docs else None,
            "elapsed_ms": round(elapsed, 1),
        }
    # data 类问题：不在 RAG 评估范围
    return {
        "id": item["id"],
        "category": "data",
        "question": item["question"],
        "skipped": True,
    }


def render_report(results: list[dict], out_path: Path) -> None:
    """生成 Markdown 报告。"""
    knowledge = [r for r in results if r["category"] == "knowledge"]
    hits = sum(1 for r in knowledge if r.get("hit"))
    total = len(knowledge)
    hit_rate = (hits / total * 100) if total else 0.0

    lines = [
        "# RAG 评估报告",
        "",
        f"- 评估时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 知识类问题: {total} 条",
        f"- 命中数: {hits}",
        f"- 命中率: {hit_rate:.1f}%",
        "",
        "## 知识类问题明细",
        "",
        "| ID | 命中 | 期望 doc | 实际 doc | Top1 score | 耗时(ms) |",
        "|----|------|----------|----------|------------|----------|",
    ]
    for r in knowledge:
        lines.append(
            f"| {r['id']} | {'✅' if r.get('hit') else '❌'} | "
            f"{r.get('expected_doc', '')} | {r.get('hit_doc_type', '')} | "
            f"{r.get('top_score', 'N/A')} | {r.get('elapsed_ms', '')} |"
        )
    lines.append("")

    skipped = [r for r in results if r.get("skipped")]
    if skipped:
        lines.append(f"## 数据类问题（已跳过）: {len(skipped)} 条")
        lines.append("")
        lines.append("数据类问题的评估需调用真实 LLM + SQL 工具，不在 RAG 评估范围内。")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"📄 报告已写入: {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="使用真实 Embedding 模型")
    parser.add_argument("--persist-dir", default=str(ROOT / "data" / "chroma"))
    parser.add_argument("--gold", default=str(HERE / "gold_qa.jsonl"))
    parser.add_argument("--report", default=str(HERE / "report.md"))
    parser.add_argument("--json", default=str(HERE / "report.json"))
    args = parser.parse_args()

    if args.real:
        print("🔍 使用真实 Embedding 模型...")
        retriever = real_retriever(args.persist_dir)
    else:
        print("🧪 使用 Fake embedder 冒烟评估（不依赖真实模型）")
        retriever = fake_retriever()

    gold = load_gold(Path(args.gold))
    print(f"📋 加载 {len(gold)} 条评估集")

    results = []
    for item in gold:
        r = evaluate_item(retriever, item)
        results.append(r)
        if r["category"] == "knowledge":
            mark = "✅" if r.get("hit") else "❌"
            print(f"  {mark} {r['id']}: {r['question'][:40]}...")

    # 汇总
    knowledge = [r for r in results if r["category"] == "knowledge"]
    hits = sum(1 for r in knowledge if r.get("hit"))
    total = len(knowledge)
    hit_rate = (hits / total * 100) if total else 0.0
    print(f"\n📊 命中率: {hits}/{total} = {hit_rate:.1f}%")

    # 输出
    Path(args.json).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"💾 JSON 报告: {args.json}")
    render_report(results, Path(args.report))


if __name__ == "__main__":
    main()
