"""冻结数据集的真实路由/RAG/MySQL评测；缺凭据或实验失败显式记录，不伪造。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import secrets
import sys
from importlib.metadata import PackageNotFoundError, version
import tempfile
import time
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from agent_core.business_data import BUSINESS_CONTEXT, describe_data_range
from agent_core.db_schema import describe_table
from agent_core.eval.ab_routing_eval import LABELS, prf
from agent_core.model_adapter import OpenAIModelAdapter
from agent_core.rag import MarkdownKnowledgeRetriever
from agent_core.routing import classify_intent
from agent_core.sql_safety import SQLValidationError, apply_execution_guard, validate_and_limit_sql

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
KB = ROOT / "ai-ecommerce-assistant" / "knowledge_base"


def cases(name):
    return [json.loads(line) for line in (HERE / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def route_summary(details):
    gold, pred = [d["gold"] for d in details], [d["pred"] for d in details]
    summary = {
        "n": len(details), "accuracy": sum(g == p for g, p in zip(gold, pred)) / len(details),
        "macro_f1": sum(prf(gold, pred, label)[2] for label in LABELS) / len(LABELS),
        "mean_latency_ms": sum(d["latency_ms"] for d in details) / len(details),
        "input_tokens": sum(d.get("input_tokens") or 0 for d in details),
        "output_tokens": sum(d.get("output_tokens") or 0 for d in details),
        "usage_missing": sum(d.get("model_called", False) and d.get("input_tokens") is None for d in details),
        "errors": dict(Counter(d["error"] for d in details if d.get("error"))), "details": details,
    }
    called = [d for d in details if d.get("model_called")]
    if any(d.get("error") for d in details) and not any(not d.get("error") for d in called):
        summary.update(status="PROVIDER_FAILED", accuracy=None, macro_f1=None,
                       input_tokens=None, output_tokens=None, cost_cny=None)
    else:
        summary["status"] = "RUN"
        if not called:
            summary["cost_cny"] = 0
        elif os.getenv("LLM_PRICE_IN_PER_1K") and os.getenv("LLM_PRICE_OUT_PER_1K") and not summary["usage_missing"]:
            summary["cost_cny_estimate"] = (summary["input_tokens"] * float(os.environ["LLM_PRICE_IN_PER_1K"])
                                           + summary["output_tokens"] * float(os.environ["LLM_PRICE_OUT_PER_1K"])) / 1000
        else:
            summary["cost_cny"] = None
    return summary


async def routing_experiment(settings, live):
    from agent_core.llm_router import classify_intent_llm_detailed

    sets = {
        "curated": cases("agent_cases.jsonl"), "robustness_dev": cases("routing_robustness.jsonl"),
        "legacy_heldout": cases("routing_robustness_heldout.jsonl"), "independent_audit": cases("audit_routing.jsonl"),
    }
    output = {}
    provider_failed = False
    for name, dataset in sets.items():
        variants = {"A": [], "B": [], "C": []}
        async def one(item):
            nonlocal provider_failed
            started = time.perf_counter()
            rule = classify_intent(item["query"])
            base = {"id": item.get("id"), "gold": item["expected_intent"], "pred": rule,
                    "latency_ms": (time.perf_counter() - started) * 1000, "model_called": False}
            variants["A"].append(base)
            if not live:
                return
            # 高风险永远由确定性策略先拦截；这不是LLM自身安全能力的测量。
            if rule == "blocked":
                variants["B"].append(dict(base))
                variants["C"].append(dict(base))
                return
            started = time.perf_counter()
            if provider_failed:
                b = {**base, "pred": "not_run", "error": "provider_auth_failed"}
            else:
                try:
                    b = await classify_intent_llm_detailed(item["query"])
                    b = {**base, **b, "pred": b["intent"], "model_called": True}
                except Exception as exc:
                    if getattr(exc, "status_code", None) == 401:
                        provider_failed = True
                    b = {**base, "pred": "error", "error": type(exc).__name__, "model_called": True,
                         "latency_ms": (time.perf_counter() - started) * 1000}
            variants["B"].append(b)
            # C保留明确规则结果；缺少明确数据标记的兜底data才调用LLM。
            fallback = rule == "data" and not any(
                word in item["query"].lower() for word in ("多少", "最多", "最高", "最低", "排名", "趋势", "最近", "同比", "环比", "top", "各平台", "当前")
            )
            # 使用相同单次LLM响应作为配对实验，C成本只计其实际需要调用的样本。
            variants["C"].append(dict(b if fallback else base))
        semaphore = asyncio.Semaphore(4)

        async def limited(item):
            async with semaphore:
                await one(item)

        await asyncio.gather(*(limited(item) for item in dataset))
        output[name] = {key: route_summary(value) if value else {"status": "NOT_RUN"} for key, value in variants.items()}
    return output


def result_equal(actual, expected):
    """保留列身份与Top-K顺序；数值只允许小数舍入误差，不逐行排序列。"""
    if len(actual) != len(expected):
        return False
    for left, right in zip(actual, expected):
        if len(left) != len(right):
            return False
        for a, b in zip(left, right):
            if a is None or b is None:
                if a is not b:
                    return False
            elif isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
                if not math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=.005):
                    return False
            elif str(a) != str(b):
                return False
    return True


async def sql_experiment(settings, live):
    if not live:
        return {"status": "NOT_RUN", "reason": "未启用真实模型或缺少凭据"}
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    admin = create_engine(settings.database_url, hide_parameters=True)
    schema, user, password = "eval_" + uuid4().hex, "eval_" + uuid4().hex[:20], secrets.token_urlsafe(24)
    engine = reader = None
    details = []
    try:
        with admin.begin() as connection:
            connection.execute(text(f"CREATE DATABASE `{schema}`"))
        cfg = Config(str(ROOT / "backend" / "alembic.ini"))
        cfg.attributes["migration_url"] = admin.url.set(database=schema).render_as_string(hide_password=False)
        command.upgrade(cfg, "head")
        engine = create_engine(admin.url.set(database=schema), hide_parameters=True)
        fixture = []
        for index in range(36):
            fixture.append({
                "order_id": f"fixture-{index}", "user_name": f"user-{index % 8}",
                "product_id": f"P{index % 5}", "payment_amount": (index + 1) * 11.25,
                "platform_type": ["APP", "微信公众号", "Web网站"][index % 3],
                "channel_id": f"C{index % 4}", "order_date": [date(2025, 1, 10), date(2025, 2, 15), date(2026, 1, 20)][index % 3],
                "is_refund": "是" if index % 7 == 0 else "否",
                "order_status": "REFUNDED" if index % 7 == 0 else ("CANCELLED" if index % 11 == 0 else "ACTIVE"),
            })
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO orders(order_id,user_name,product_id,payment_amount,platform_type,channel_id,order_date,is_refund,order_status) VALUES(:order_id,:user_name,:product_id,:payment_amount,:platform_type,:channel_id,:order_date,:is_refund,:order_status)"), fixture)
        with admin.begin() as connection:
            connection.execute(text(f"CREATE USER '{user}'@'%' IDENTIFIED BY :password"), {"password": password})
            from agent_core.sql_safety import ORDER_COLUMNS
            fields = ",".join(sorted(ORDER_COLUMNS))
            connection.execute(text(f"GRANT SELECT ({fields}) ON `{schema}`.orders TO '{user}'@'%'"))
        reader = create_engine(admin.url.set(database=schema, username=user, password=password), hide_parameters=True,
                               connect_args={"connect_timeout": 5, "read_timeout": 15})
        from agent_core.db_permissions import verify_ai_permissions
        verify_ai_permissions(reader)
        schema_text = describe_table(reader, "orders") + "\n" + describe_data_range(reader)
        adapter = OpenAIModelAdapter(api_key=settings.llm_api_key, base_url=settings.llm_base_url,
                                     model=settings.llm_model, business_context=BUSINESS_CONTEXT)
        provider_failed = False
        for item in cases("audit_sql.jsonl"):
            started = time.perf_counter()
            detail = {"id": item["id"], "category": item["category"], "attempts": 0, "correct": False,
                      "execution_success": False, "input_tokens": 0, "output_tokens": 0, "usage_missing": False}
            if item.get("expected_intent"):
                detail.update(correct=classify_intent(item["query"]) == item["expected_intent"], error=None)
            elif provider_failed:
                detail["error"] = "provider_auth_failed"
            else:
                # 同一只读事务快照内执行oracle与所有候选，禁止写入与并发数据漂移。
                with reader.connect() as connection:
                    expected = connection.execute(text(item["reference_sql"])).fetchall() if item.get("reference_sql") else []
                    previous_error = None
                    for attempt in range(2):
                        detail["attempts"] += 1
                        try:
                            response = await adapter.generate_sql(item["query"], schema_text, [], [], previous_error)
                            detail["input_tokens"] += response.input_tokens or 0
                            detail["output_tokens"] += response.output_tokens or 0
                            detail["usage_missing"] |= response.input_tokens is None
                            sql = apply_execution_guard(validate_and_limit_sql(response.content), 10000)
                            actual = connection.execute(text(sql)).fetchall()
                            detail.update(sql=sql, execution_success=True, correct=not item.get("expected_reject") and result_equal(actual, expected),
                                          error="result_mismatch" if item.get("expected_reject") or not result_equal(actual, expected) else None)
                            break
                        except Exception as exc:
                            detail["error"] = "sql_policy" if isinstance(exc, SQLValidationError) else type(exc).__name__
                            previous_error = detail["error"]
                            if getattr(exc, "status_code", None) == 401:
                                provider_failed = True
                                break
                            if item.get("expected_reject") and isinstance(exc, SQLValidationError):
                                detail["correct"] = True
                                break
            detail["latency_ms"] = (time.perf_counter() - started) * 1000
            details.append(detail)
        execution = [d for d in details if d["category"] not in {"invalid", "dangerous"}]
        retries = [d for d in execution if d["attempts"] > 1]
        summary = {
            "status": "RUN", "fixture_rows": len(fixture), "fixture_sha256": hashlib.sha256(json.dumps(fixture, default=str, sort_keys=True).encode()).hexdigest(),
            "n": len(details), "execution_cases": len(execution),
            "execution_success_rate": sum(d["execution_success"] for d in execution) / len(execution),
            "result_accuracy": sum(d["correct"] for d in execution) / len(execution),
            "all_case_accuracy": sum(d["correct"] for d in details) / len(details),
            "retried_cases": len(retries), "retry_success_rate": sum(d["correct"] for d in retries) / len(retries) if retries else None,
            "mean_tokens": sum(d["input_tokens"] + d["output_tokens"] for d in details) / sum(d["attempts"] > 0 for d in details),
            "p50_ms": percentile([d["latency_ms"] for d in details], .5), "p95_ms": percentile([d["latency_ms"] for d in details], .95),
            "error_categories": dict(Counter(d["error"] for d in details if d.get("error"))), "details": details,
        }
        return sql_summary_provenance(summary)
    finally:
        if reader is not None:
            reader.dispose()
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f"DROP USER IF EXISTS '{user}'@'%'").execution_options(autocommit=True))
            connection.execute(text(f"DROP DATABASE IF EXISTS `{schema}`"))
        admin.dispose()


def sql_summary_provenance(summary):
    """提供方失败不伪装成模型能力0分，也不把未返回的token计为零成本。"""
    attempted = [d for d in summary["details"] if d["attempts"]]
    failed = attempted and all(not d["execution_success"] and d.get("error") not in {"sql_policy", "result_mismatch"} for d in attempted)
    if failed:
        summary.update(status="PROVIDER_FAILED", execution_success_rate=None, result_accuracy=None,
                       all_case_accuracy=None, retry_success_rate=None, mean_tokens=None,
                       reason="真实提供方未返回可执行SQL，不能得出模型能力指标")
        for detail in attempted:
            detail["usage_missing"] = True
            detail["input_tokens"] = detail["output_tokens"] = None
    return summary


async def rag_experiment(real):
    dataset = cases("audit_rag.jsonl")
    lexical = MarkdownKnowledgeRetriever(KB)
    stores = {"lexical": lexical}
    if real:
        sys.path.insert(0, str(ROOT / "ai-ecommerce-assistant"))
        from build_knowledge_base import load_kb_files
        from rag import VectorStore, get_embeddings

        # 临时目录隔离生产向量库；句向量为真实BGE，不使用FakeEmbedding。
        persist = tempfile.mkdtemp(prefix="audit-bge-")
        store = VectorStore(persist_dir=persist, embedding=get_embeddings())
        docs = load_kb_files(str(KB))
        store.add_documents(docs)
        stores["bge_chroma"] = store
    output = {}
    raw = {}
    for name, retriever in stores.items():
        details = []
        for item in dataset:
            started = time.perf_counter()
            if name == "lexical":
                found = await retriever.retrieve(item["query"], top_k=3)
                ranked = [(s.filename, s.section) for s in found]
            else:
                found = retriever.search(item["query"], k=3, score_threshold=.4)
                ranked = [(s["metadata"]["filename"], s["metadata"]["section"]) for s in found]
            raw[name, item["id"]] = ranked
            details.append(rag_detail(item, ranked, (time.perf_counter() - started) * 1000))
        output[name] = rag_summary(details)
    if real:
        details = []
        for item in dataset:
            # RRF只融合已检索的Top3，不调阈值；延迟为配对两路的检索耗时之和。
            scores = {}
            for name in stores:
                seen = set()
                for rank, source in enumerate(raw[name, item["id"]], 1):
                    if source in seen:
                        continue
                    seen.add(source)
                    scores[source] = scores.get(source, 0) + 1 / (60 + rank)
            ranked = sorted(scores, key=lambda source: (-scores[source], source))[:3]
            latency = sum(next(d["latency_ms"] for d in output[name]["details"] if d["id"] == item["id"]) for name in stores)
            details.append(rag_detail(item, ranked, latency))
        output["hybrid_rrf"] = rag_summary(details)
        output["bge_chroma"]["temporary_vector_dir"] = persist
    else:
        output["bge_chroma"] = output["hybrid_rrf"] = {"status": "NOT_RUN"}
    output["reranker"] = {"status": "NOT_RUN", "reason": "可选组件；无收益证据，未引入"}
    return output


def rag_detail(item, ranked, ms):
    positions = [i for i, (filename, section) in enumerate(ranked, 1)
                 if filename == item.get("filename") and section == item.get("section")]
    return {"id": item["id"], "no_answer": item.get("no_answer", False), "hit": bool(positions),
            "rr": 1 / positions[0] if positions else 0, "refused": not ranked, "latency_ms": ms, "sources": ranked}


def rag_summary(details):
    positive = [d for d in details if not d["no_answer"]]
    negative = [d for d in details if d["no_answer"]]
    return {"n": len(details), "answerable": len(positive), "unanswerable": len(negative),
            "recall_at_3": sum(d["hit"] for d in positive) / len(positive),
            "mrr": sum(d["rr"] for d in positive) / len(positive),
            "no_answer_accuracy": sum(d["refused"] for d in negative) / len(negative),
            "mean_latency_ms": sum(d["latency_ms"] for d in details) / len(details), "details": details}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--real-bge", action="store_true")
    parser.add_argument("--section", choices=["routing", "rag", "sql", "all"], default="all")
    parser.add_argument("--output", default="docs/evaluation/audit_experiments.json")
    args = parser.parse_args()
    from backend.config import get_settings

    settings = get_settings()
    live = args.live_model and bool(settings.llm_api_key)
    if live:
        os.environ["LLM_API_KEY"] = settings.llm_api_key
        os.environ["LLM_BASE_URL"] = settings.llm_base_url
        os.environ["LLM_MODEL"] = settings.llm_model
    report = {"date": "2026-10-13", "python": platform.python_version(), "platform": platform.platform(),
              "model": settings.llm_model if live else None, "model_enabled": live,
              "datasets": {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in ("audit_routing.jsonl", "audit_rag.jsonl", "audit_sql.jsonl")}}
    report["packages"] = {}
    for package in ("langgraph", "langchain-openai", "sqlglot", "chromadb", "sentence-transformers", "PyJWT"):
        try:
            report["packages"][package] = version(package)
        except PackageNotFoundError:
            report["packages"][package] = None
    report["source_sha256"] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("agent_core/routing.py", "agent_core/rag/retriever.py", "agent_core/sql_safety.py",
                     "agent_core/model_adapter.py", "agent_core/eval/audit_experiments.py",
                     "ai-ecommerce-assistant/rag/vector_store.py", "ai-ecommerce-assistant/build_knowledge_base.py")
    }
    experiments = {"routing": lambda: routing_experiment(settings, live), "rag": lambda: rag_experiment(args.real_bge),
                   "sql": lambda: sql_experiment(settings, live)}
    for name, experiment in experiments.items():
        if args.section not in ("all", name):
            continue
        print(f"开始 {name}", flush=True)
        try:
            report[name] = await experiment()
        except Exception as exc:
            report[name] = {"status": "FAILED", "error_type": type(exc).__name__}
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已记录 {name}", flush=True)
    return 1 if experiment_failed(report) else 0


def experiment_failed(value):
    if isinstance(value, dict):
        return value.get("status") in {"FAILED", "PROVIDER_FAILED"} or any(experiment_failed(v) for v in value.values())
    if isinstance(value, list):
        return any(experiment_failed(v) for v in value)
    return False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
