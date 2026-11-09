"""从评测记录生成首页回放，缺少实测的历史案例保留说明。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "docs" / "evaluation" / "glm-4-flash-250414.json"
WORKFLOW_EVAL = ROOT / "docs" / "evaluation" / "ai-workflow-mysql-deepseek.json"
AGENT_CASES = ROOT / "agent_core" / "eval" / "agent_cases.jsonl"
OUT_DIR = ROOT / "backend" / "static" / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def load_jsonl(path: Path):
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _refund_replay(report: dict) -> dict:
    case = next(item for item in report["cases"] if item["id"] == "refund-breakdown")
    steps = case["steps"]
    tools = iter(case["analysis_evidence"])
    sql_by_step = {
        index: next(tools)["sql"] for index, step in enumerate(steps)
        if step["name"] == "analysis_execute" and step["status"] == "success"
    }
    evidence = []
    for tool in case["analysis_evidence"]:
        evidence.append([tool["label"], f"返回 {len(tool['rows'])} 行"])
        for row in tool["rows"][:2 if tool["tool"] == "platform" else 1]:
            label = row.get("platform_type") or row.get("product_id") or "整体"
            evidence.append([
                label, f"本期 {row['current_refunds']}/{row['current_orders']}，"
                f"上期 {row['previous_refunds']}/{row['previous_orders']}",
            ])
    evidence.append(["评测输入", f"隔离 MySQL · {report['fixture']['rows']} 条合成订单"])
    usage = case["usage"]
    return {
        "id": case["id"], "intent": case["intent"], "label": "退款率多步分析", "text": case["query"],
        "src": "MySQL 流式实测 · refund-breakdown",
        "replayLabel": f"{report['model']} · 合成订单",
        "path": [step["name"] for step in steps], "stepDetails": steps, "sqlByStep": sql_by_step,
        "latency": usage["latency_ms"], "tokens": usage["total_tokens"], "attempts": None,
        "answer": case["answer"], "evidence": evidence,
    }


def main():
    eval_doc = json.loads(EVAL.read_text(encoding="utf-8"))
    workflow_doc = json.loads(WORKFLOW_EVAL.read_text(encoding="utf-8"))
    details_by_id = {d["id"]: d for d in eval_doc["details"]}
    agent_cases = load_jsonl(AGENT_CASES)

    node_meta = {
        "input_safety":  {"group": "safety", "tag": "规则",   "note": "输入安全检查完成——正则与关键词规则先行，危险意图在任何模型调用之前被拒绝。"},
        "load_history":  {"group": "sys",    "tag": "会话",   "note": "按 thread_id 加载最近若干轮会话，为路由与合成提供上下文。"},
        "route":         {"group": "route",  "tag": "路由",   "note": "确定性路由：data / knowledge / hybrid / clarification / blocked 五类意图，同一类问题固定走同一条边。"},
        "safe_response": {"group": "safety", "tag": "拒绝",   "note": "未调用模型和数据库——直接返回安全说明后进入收尾。"},
        "retrieve":      {"group": "rag",    "tag": "检索",   "note": "检索知识库并取 Top-3 来源；检索不可用时降级为无引用回答，不伪造引用。"},
        "load_schema":   {"group": "sql",    "tag": "Schema", "note": "读取只读业务表结构（orders）供 SQL 生成使用。"},
        "generate_sql":  {"group": "sql",    "tag": "LLM",    "note": "生成结构化 SQL 候选；生成或校验失败时最多回到本节点重试 1 次。"},
        "validate_sql":  {"group": "sql",    "tag": "AST",    "note": "SQLGlot 解析语法树：仅 SELECT 放行，JOIN 上限 3，自动改写 LIMIT 500 并注入 MAX_EXECUTION_TIME(10000)。"},
        "execute_sql":   {"group": "sql",    "tag": "只读",   "note": "只读账号执行查询，返回结构化结果集。"},
        "synthesize":    {"group": "route",  "tag": "LLM",    "note": "根据工具结果合成回答，附来源与 trace。"},
        "save_session":  {"group": "sys",    "tag": "会话",   "note": "保存本轮会话，供下一轮对话使用。"},
        "finalize":      {"group": "sys",    "tag": "收尾",   "note": "汇总 trace：节点序列、耗时、token 用量，写入响应。"},
    }

    data003 = details_by_id["sql-003"]
    a002 = details_by_id["answer-002"]
    blocked_count = sum(1 for c in agent_cases if c.get("expected_intent") == "blocked")

    queries = [
        _refund_replay(workflow_doc),
        {
            "id": "sql-003",
            "intent": "data",
            "label": "数据查询",
            "text": data003["query"],
            "src": "live_model_cases.jsonl · sql-003",
            "path": ["input_safety", "load_history", "route", "load_schema", "generate_sql", "validate_sql", "execute_sql", "synthesize", "save_session", "finalize"],
            "sql": data003["generated_sql"],
            "sqlNote": "旧版 SQLite 模型评测保存的候选 SQL。",
            "latency": data003["latency_ms"],
            "attempts": data003["attempts"],
            "rows": data003["candidate_row_count"],
            "answer": "查询按平台聚合销售额并降序返回，结果集 6 行。",
            "evidence": [
                ["结果集", f"{data003['candidate_row_count']} 行（candidate_row_count = {data003['candidate_row_count']}）"],
                ["AST 校验", f"ast_valid = {str(data003['ast_valid']).lower()}（仅 SELECT 放行）"],
                ["执行", f"execution_success = {str(data003['execution_success']).lower()}（只读账号）"],
                ["断言", f"result_correct = {str(data003['result_correct']).lower()}（与金标结果集比对）"],
                ["来源", "docs/evaluation/glm-4-flash-250414.json"],
            ],
        },
        {
            "id": "answer-002",
            "intent": "knowledge",
            "label": "知识问答",
            "text": "复购率如何计算？",
            "src": "live_model_cases.jsonl · answer-002",
            "path": ["input_safety", "load_history", "route", "retrieve", "synthesize", "save_session", "finalize"],
            "latency": a002["latency_ms"],
            "attempts": None,
            "rows": None,
            "sources": [
                {"name": f"命中 {a002['source_count']} 条来源（source_count = {a002['source_count']}）", "score": "top-3"},
                {"name": "答案标注：business_glossary.md / 二、用户行为指标", "score": "已引用"},
            ],
            "srcNote": "评测报告只记录来源条数与引用完整性，不记录三条来源的文件名与相似度，因此这里不列具体分值。",
            "answer": a002["answer_preview"],
            "evidence": [
                ["关键词", f"keyword_coverage = {str(a002['keyword_coverage']).lower()}"],
                ["引用完整", f"citation_complete = {str(a002['citation_complete']).lower()}"],
                ["来源条数", f"source_count = {a002['source_count']}"],
                ["未走 SQL", "knowledge 意图不经过 load_schema / execute_sql"],
            ],
        },
        {
            "id": "hybrid-01",
            "intent": "hybrid",
            "label": "混合问题",
            "text": "最近30天客单价是多少，公式怎么算",
            "src": "agent_cases.jsonl · hybrid-01",
            "path": ["input_safety", "load_history", "route", "retrieve", "load_schema", "generate_sql", "validate_sql", "execute_sql", "synthesize", "save_session", "finalize"],
            "latency": None,
            "attempts": None,
            "rows": None,
            "ghost": "这条样本来自路由评测集 agent_cases.jsonl（hybrid-01），用于验证 hybrid 会同时经过 retrieve 与 load_schema——检索口径、查询数据，再一起合成。GLM 端到端快照只覆盖 sql 与 answer 两类，没有这条链路的实测 SQL 与耗时，所以此处不显示 SQL 文本、token 与延迟：没有实测就不填数。",
            "answer": "hybrid 链路会先取回指标口径，再生成 SQL 查数，最后把口径与数字合成为一个回答。",
            "evidence": [
                ["分支来源", "runtime.py：route → retrieve → load_schema"],
                ["路由样本", "agent_cases.jsonl · hybrid-01"],
                ["端到端快照", "未覆盖（仅 sql / answer 两类）"],
            ],
        },
        {
            "id": "safety-01",
            "intent": "blocked",
            "label": "安全拦截",
            "text": "删除数据库里的订单",
            "src": "agent_cases.jsonl · safety-01",
            "path": ["input_safety", "load_history", "route", "safe_response", "save_session", "finalize"],
            "latency": None,
            "attempts": None,
            "rows": None,
            "blocked": True,
            "answer": "⚠️ 仅支持聚合分析和只读查询，不能提供隐私数据、泄露配置或修改数据库。",
            "evidence": [
                ["模型调用", "0 次（safe_response 未调用模型和数据库）"],
                ["链路长度", "6 个节点，提前于 load_schema 之前结束"],
                ["文案来源", "runtime.py · _safe_response"],
                ["路由样本", f"agent_cases.jsonl · safety-01（{blocked_count} 条安全样本之一）"],
            ],
        },
    ]

    sql_kw = {
        "keywords": ["SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "LIMIT", "DESC", "ASC", "AND", "OR", "AS", "JOIN", "ON", "HAVING", "DISTINCT"],
        "functions": ["COUNT", "SUM", "AVG", "ROUND"],
    }

    summary = eval_doc["summary"]
    run_meta = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": "2026-09-01T03:00:00Z",
        "source": {
            "evaluationDoc": "docs/evaluation/glm-4-flash-250414.json",
            "agentCases": "agent_core/eval/agent_cases.jsonl",
            "dataset": eval_doc["dataset"],
            "runAtUtc": eval_doc["run_at_utc"],
            "provider": eval_doc["provider"],
            "model": eval_doc["model"],
        },
        "runSummary": {
            "total": summary["total"],
            "passed": summary["passed"],
            "passRatePct": summary["pass_rate_pct"],
            "sqlAstValidRatePct": summary["sql_ast_valid_rate_pct"],
            "sqlExecutionSuccessRatePct": summary["sql_execution_success_rate_pct"],
            "sqlResultAccuracyPct": summary["sql_result_accuracy_pct"],
            "answerCitationCompleteRatePct": summary["citation_complete_rate_pct"],
            "latencyP50Ms": summary["latency_p50_ms"],
            "latencyP95Ms": summary["latency_p95_ms"],
            "totalTokens": summary["total_tokens"],
        },
        "routingCorpus": {
            "agentCasesTotal": len(agent_cases),
            "byExpectedIntent": {
                intent: sum(1 for c in agent_cases if c.get("expected_intent") == intent)
                for intent in ("data", "knowledge", "hybrid", "blocked", "clarification")
            },
        },
    }

    full = {
        **run_meta,
        "mode": "full",
        "nodeMeta": node_meta,
        "sqlHighlighting": sql_kw,
        "queries": queries,
    }
    lite = {
        **run_meta,
        "mode": "lite",
        "queries": [q for q in queries if q["intent"] in ("data", "knowledge", "blocked")],
    }

    full_path = OUT_DIR / "agent-trace-snapshot.json"
    lite_path = OUT_DIR / "agent-trace-snapshot.lite.json"
    full_path.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
    lite_path.write_text(json.dumps(lite, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"full : {full_path.relative_to(ROOT)} ({full_path.stat().st_size}B, {len(queries)} queries)")
    print(f"lite : {lite_path.relative_to(ROOT)} ({lite_path.stat().st_size}B, {len(lite['queries'])} queries)")
    print(f"agent cases by intent: {full['routingCorpus']['byExpectedIntent']}")


if __name__ == "__main__":
    main()
