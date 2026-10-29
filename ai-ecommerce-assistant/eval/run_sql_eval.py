"""Text-to-SQL 离线评估：消费 gold_qa.jsonl 的 data 类用例（d01-d10）。

RAG 评估（run_eval.py）只覆盖知识检索；本脚本补上 Text-to-SQL 这一核心能力的
评估闭环，避免"检索有指标、生成靠感觉"：

  1. 用与生产一致的 OpenAIModelAdapter.generate_sql 生成 SQL（需 LLM_API_KEY）
  2. sqlglot AST 只读校验（validate_and_limit_sql，与生产同一道闸门）
  3. gold 关键词命中检查（check_sql_keywords，归一化大小写/空白/反引号后匹配）
  4. 可选 --execute 落库执行，记录行数与执行错误（默认关闭，无数据库也能评估）

用法：
  python eval/run_sql_eval.py                       # 生成 + 校验 + 关键词
  python eval/run_sql_eval.py --execute             # 额外落库执行
  python eval/run_sql_eval.py --output report.json  # 同时落盘 JSON 报告

无 LLM_API_KEY 时无法运行生成侧评估；但 check_sql_keywords /
validate_and_limit_sql 是纯函数，由 tests/test_sql_eval.py 在 CI 覆盖。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_core.model_adapter import OpenAIModelAdapter  # noqa: E402
from agent_core.sql_safety import SQLValidationError, validate_and_limit_sql  # noqa: E402

DEFAULT_DATASET = Path(__file__).with_name("gold_qa.jsonl")
BUSINESS_CONTEXT = "## 表结构\n详见下方；只生成针对 orders 表的只读查询。"


def normalize_sql(sql: str) -> str:
    """关键词匹配前的归一化：大写、折叠空白、去反引号与语句末分号。

    括号旁的空格在 SQL 里不影响语义（"SUM( x )" ≡ "SUM(x)"），一并压掉，
    否则 gold 关键词 "SUM(payment_amount)" 会因模型多打一个空格而误报未命中。
    """
    normalized = sql.replace("`", "").upper()
    normalized = re.sub(r"\s+", " ", normalized).strip().rstrip(";")
    return re.sub(r"\s*([()])\s*", r"\1", normalized)


def check_sql_keywords(sql: str, keywords: list[str]) -> list[str]:
    """返回 SQL 未命中的 gold 关键词列表（空列表 = 全部命中）。

    关键词同样做归一化，因此 "SUM(payment_amount)" 能匹配
    "select sum( payment_amount )" 这类写法差异。
    """
    normalized_sql = normalize_sql(sql)
    return [kw for kw in keywords if normalize_sql(kw) not in normalized_sql]


def load_data_cases(path: Path) -> list[dict]:
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("category") != "data":
            continue  # 知识类用例归 run_eval.py 管
        if not item.get("expected_sql_keywords"):
            raise ValueError(f"评测集第 {line_number} 行 data 用例缺少 expected_sql_keywords: {item.get('id')}")
        cases.append(item)
    if not cases:
        raise ValueError("评测集中没有带 expected_sql_keywords 的 data 类用例")
    return cases


def load_schema() -> str:
    """用与 app.py 相同的连接逻辑反射 orders 表结构（SQLite 回落本地演示库）。"""
    from urllib.parse import quote_plus

    from dotenv import load_dotenv
    from sqlalchemy import create_engine

    from agent_core.db_schema import describe_table

    load_dotenv(str(PROJECT_ROOT / ".env"))
    user, password = os.getenv("DB_USER"), os.getenv("DB_PASSWORD")
    if user and password:
        uri = (
            f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
            f"@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '3306')}"
            f"/{os.getenv('DB_NAME', 'ai_commerce_intelligence_platform')}?charset=utf8mb4"
        )
    else:
        uri = f"sqlite:///{Path(__file__).resolve().parents[1] / 'ecommerce.db'}"
    engine = create_engine(uri)
    try:
        return describe_table(engine, "orders")
    finally:
        engine.dispose()


def execute_sql(sql: str) -> dict:
    """落库执行（只读校验通过后调用），返回行数或错误摘要。"""
    from sqlalchemy import create_engine, text

    from backend.utils.sql_guard import guard_read_only_engine

    user, password = os.getenv("DB_USER"), os.getenv("DB_PASSWORD")
    if user and password:
        from urllib.parse import quote_plus

        uri = (
            f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
            f"@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '3306')}"
            f"/{os.getenv('DB_NAME', 'ai_commerce_intelligence_platform')}?charset=utf8mb4"
        )
    else:
        uri = f"sqlite:///{Path(__file__).resolve().parents[1] / 'ecommerce.db'}"
    engine = create_engine(uri)
    guard_read_only_engine(engine)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(sql)).fetchall()
        return {"executed": True, "rows": len(rows)}
    except Exception as exc:
        return {"executed": False, "exec_error": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        engine.dispose()


async def evaluate_case(adapter: OpenAIModelAdapter, schema: str, case: dict,
                        do_execute: bool) -> dict:
    """评估单条 data 用例：生成 → AST 校验 → 关键词命中 →（可选）落库执行。"""
    t0 = time.time()
    result: dict = {
        "id": case["id"],
        "question": case["question"],
        "expected_sql_keywords": case["expected_sql_keywords"],
    }
    try:
        response = await adapter.generate_sql(
            case["question"], schema, history=[], sources=[], previous_error=None,
        )
        sql = response.content.strip()
    except Exception as exc:
        result.update({
            "generated_sql": None,
            "parse_ok": False,
            "parse_error": f"{type(exc).__name__}: {exc}"[:200],
            "missing_keywords": list(case["expected_sql_keywords"]),
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        })
        return result

    result["generated_sql"] = sql
    try:
        validated = validate_and_limit_sql(sql)
        result["parse_ok"] = True
    except SQLValidationError as exc:
        result.update({"parse_ok": False, "parse_error": str(exc)})
        validated = None

    if validated is not None and do_execute:
        result.update(execute_sql(validated))

    missing = check_sql_keywords(sql, case["expected_sql_keywords"])
    result["missing_keywords"] = missing
    result["keyword_ok"] = not missing
    # 开启落库检查时，执行失败不能被关键词命中掩盖。
    result["passed"] = bool(result.get("parse_ok")) and not missing and (
        not do_execute or result.get("executed") is True
    )
    result["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    return result


def render_report(report: dict) -> str:
    lines = [
        "# Text-to-SQL 评估报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 用例数：{report['total']}",
        f"- AST 只读校验通过率：{report['sql_parse_rate_pct']}%",
        f"- gold 关键词全命中率：{report['keyword_pass_rate_pct']}%",
        f"- 综合通过率（校验 ∧ 关键词）：{report['overall_pass_rate_pct']}%",
        "",
        "| 用例 | 问题 | AST | 关键词 | 未命中关键词 |",
        "|---|---|---|---|---|",
    ]
    for case in report["cases"]:
        lines.append(
            f"| {case['id']} | {case['question'][:24]} "
            f"| {'✅' if case.get('parse_ok') else '❌'} "
            f"| {'✅' if case.get('keyword_ok') else '❌'} "
            f"| {', '.join(case.get('missing_keywords', [])) or '-'} |"
        )
    failures = [c for c in report["cases"] if not c.get("passed")]
    if failures:
        lines += ["", "## 失败明细", ""]
        for case in failures:
            lines.append(f"### {case['id']} {case['question']}")
            lines.append(f"- 生成的 SQL：`{case.get('generated_sql') or '(生成失败)'}`")
            if case.get("parse_error"):
                lines.append(f"- 校验错误：{case['parse_error']}")
            if case.get("missing_keywords"):
                lines.append(f"- 未命中关键词：{case['missing_keywords']}")
            lines.append("")
    return "\n".join(lines) + "\n"


async def run(dataset: Path, do_execute: bool) -> dict:
    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        raise SystemExit("❌ LLM_API_KEY 未配置：SQL 评估需要调用模型生成 SQL")
    adapter = OpenAIModelAdapter(
        api_key=api_key,
        base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
        model=os.environ.get("LLM_MODEL", "deepseek-chat"),
        business_context=BUSINESS_CONTEXT,
    )
    schema = load_schema()
    cases = load_data_cases(dataset)
    results = []
    for case in cases:
        print(f"评估 {case['id']}: {case['question']}")
        results.append(await evaluate_case(adapter, schema, case, do_execute))

    total = len(results)
    parse_ok = sum(1 for r in results if r.get("parse_ok"))
    keyword_ok = sum(1 for r in results if r.get("keyword_ok"))
    passed = sum(1 for r in results if r.get("passed"))
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "total": total,
        "sql_parse_rate_pct": round(parse_ok / total * 100, 2),
        "keyword_pass_rate_pct": round(keyword_ok / total * 100, 2),
        "overall_pass_rate_pct": round(passed / total * 100, 2),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Text-to-SQL 离线评估")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, help="JSON 报告输出路径（同时生成同名 .md）")
    parser.add_argument("--execute", action="store_true",
                        help="AST 校验通过后落库执行，记录行数/执行错误")
    args = parser.parse_args()

    report = asyncio.run(run(args.dataset, args.execute))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        args.output.with_suffix(".md").write_text(render_report(report), encoding="utf-8")

    # 阈值与 run_eval.py 的 RAG 门槛对齐思路：校验与关键词双 80%
    ok = report["sql_parse_rate_pct"] >= 80 and report["keyword_pass_rate_pct"] >= 80
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
