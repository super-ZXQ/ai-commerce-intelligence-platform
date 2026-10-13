"""仅在新开发集测量规则与真实BGE阈值，不据冻结审计题调整生产策略。"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import unicodedata
from collections import Counter

from agent_core.eval.audit_experiments import HERE, KB, ROOT, cases, rag_detail, rag_summary, route_summary
from agent_core.routing import classify_intent


def normalize(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def main():
    routing = cases("premerge_routing_dev.jsonl")
    calibration = cases("premerge_rag_calibration.jsonl")
    old_queries = set()
    for path in HERE.glob("*.jsonl"):
        if path.name.startswith("premerge_"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if item.get("query"):
                    old_queries.add(normalize(item["query"]))
    assert not any(normalize(item["query"]) in old_queries for item in routing + calibration)
    report = {
        "date": "2026-10-14", "scope": "development_only_no_production_changes",
        "normalized_exact_overlap_with_old_sets": 0,
        "dataset_sha256": {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
                           for name in ("premerge_routing_dev.jsonl", "premerge_rag_calibration.jsonl")},
    }
    details = [{"id": item["id"], "gold": item["expected_intent"], "pred": classify_intent(item["query"]),
                "latency_ms": 0, "model_called": False} for item in routing]
    # 本实验只分析分类，不用伪造的零延迟报告性能指标。
    report["routing_dev"] = route_summary(details)
    report["routing_dev"].pop("mean_latency_ms")
    report["routing_dev"]["confusion"] = dict(Counter(f"{d['gold']} -> {d['pred']}" for d in details))
    audit = json.loads((ROOT / "docs/evaluation/audit_routing_run.json").read_text(encoding="utf-8"))
    source = audit.get("routing", audit)["independent_audit"]["A"]
    report["previous_audit_failures"] = [d for d in source["details"] if d["gold"] != d["pred"]]
    sys.path.insert(0, str(ROOT / "ai-ecommerce-assistant"))
    from build_knowledge_base import load_kb_files
    from rag import VectorStore, get_embeddings

    persist = tempfile.mkdtemp(prefix="premerge-calibration-")
    store = VectorStore(persist_dir=persist, embedding=get_embeddings())
    store.add_documents(load_kb_files(str(KB)))
    scored = []
    for item in calibration:
        found = store.search(item["query"], k=3, score_threshold=0)
        scored.append({"id": item["id"], "no_answer": item.get("no_answer", False), "results": [
            {"filename": d["metadata"]["filename"], "section": d["metadata"]["section"], "score": d["score"]}
            for d in found]})
    report["bge_raw_scores"] = scored
    report["thresholds"] = {}
    for threshold in (.40, .45, .50, .55, .60, .65, .70, .75, .80):
        details = []
        for item, row in zip(calibration, scored):
            ranked = [(d["filename"], d["section"]) for d in row["results"] if d["score"] >= threshold]
            details.append(rag_detail(item, ranked, 0))
        summary = rag_summary(details)
        summary.pop("mean_latency_ms")
        report["thresholds"][str(threshold)] = summary
    target = ROOT / "docs/evaluation/premerge_calibration.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("开发分类准确率", report["routing_dev"]["accuracy"])
    print("开发检索阈值对照", {k: {m: v[m] for m in ("recall_at_3", "no_answer_accuracy")}
                            for k, v in report["thresholds"].items()})


if __name__ == "__main__":
    main()
