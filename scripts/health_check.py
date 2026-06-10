#!/usr/bin/env python
"""全栈健康检查脚本。

巡检项：
- backend: /health, /health/detailed, /api/monitor/services-status
- RAG stats: /api/monitor/rag-stats（JSON + Prometheus 格式）
- BI 看板:  http://localhost:8501/_stcore/health
- AI 助手: http://localhost:8502/_stcore/health
- MySQL:   pymysql 连通性 + SELECT 1
- Redis:   redis-py 连通性 + PING

用法：
  # 默认（localhost，本地 docker-compose 端口）
  python scripts/health_check.py

  # 自定义目标 + 输出
  python scripts/health_check.py --backend http://api.example.com \\
      --bi https://bi.example.com --ai https://ai.example.com \\
      --output report.json --fail-on-error

  # CI 用：仅检查后端 + DB
  python scripts/health_check.py --checks backend,db --fail-on-error

  # 显式指定 DB/Redis（不传则跳过）
  python scripts/health_check.py --db "mysql+pymysql://root:123456@localhost/ai_commerce" \\
      --redis redis://localhost:6379/0

退出码：
  0 - 全部健康
  1 - 有 check 失败（--fail-on-error 开启时）
  2 - 脚本/参数错误
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

# 强制 UTF-8 stdout（Windows PowerShell 默认 GBK，无法打印 • 等符号）
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass

# 允许直接 python scripts/health_check.py
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    import httpx
except ImportError:
    print("❌ 缺少 httpx，请先 `pip install httpx`", file=sys.stderr)
    sys.exit(2)


# ─────────────────── 数据结构 ───────────────────


@dataclass
class CheckResult:
    """单次 check 的结果。"""
    name: str
    target: str
    status: str  # "ok" | "warn" | "error" | "skipped"
    latency_ms: float
    detail: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    timestamp: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ─────────────────── 检查函数 ───────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_http(name: str, url: str, timeout: float = 5.0,
                expect_status: int = 200,
                expect_key: Optional[str] = None) -> CheckResult:
    """HTTP 健康检查通用函数。"""
    started = time.time()
    res = CheckResult(name=name, target=url, status="ok",
                      latency_ms=0.0, timestamp=_now())
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(url)
        elapsed = (time.time() - started) * 1000
        res.latency_ms = round(elapsed, 1)
        if r.status_code != expect_status:
            res.status = "error"
            res.error = f"HTTP {r.status_code} (期望 {expect_status})"
            res.detail["body_preview"] = r.text[:200]
            return res
        if expect_key is not None:
            try:
                body = r.json()
            except Exception:
                res.status = "warn"
                res.error = "响应不是 JSON"
                res.detail["body_preview"] = r.text[:200]
                return res
            if expect_key not in body:
                res.status = "error"
                res.error = f"响应缺少 key: {expect_key}"
                res.detail["keys"] = list(body.keys())[:10]
                return res
            res.detail["response"] = body
        return res
    except Exception as e:
        elapsed = (time.time() - started) * 1000
        res.latency_ms = round(elapsed, 1)
        res.status = "error"
        res.error = f"{type(e).__name__}: {e}"
        return res


def check_backend(backend: str) -> list[CheckResult]:
    """后端一组端点检查。"""
    return [
        _check_http("backend:/health", f"{backend}/health", expect_key="status"),
        _check_http("backend:/health/detailed", f"{backend}/health/detailed"),
        _check_http("backend:/api/monitor/services-status",
                    f"{backend}/api/monitor/services-status"),
        _check_http("backend:/api/monitor/rag-stats",
                    f"{backend}/api/monitor/rag-stats"),
    ]


def check_bi(bi: str) -> list[CheckResult]:
    return [_check_http("bi:streamlit", f"{bi}/_stcore/health")]


def check_ai(ai: str) -> list[CheckResult]:
    return [_check_http("ai:streamlit", f"{ai}/_stcore/health")]


def check_mysql(db_url: str) -> list[CheckResult]:
    """MySQL 连通性 + SELECT 1。"""
    name = "mysql:select_1"
    res = CheckResult(name=name, target=_redact_url(db_url),
                      status="ok", latency_ms=0.0, timestamp=_now())
    started = time.time()
    try:
        import pymysql
    except ImportError:
        res.status = "skipped"
        res.error = "pymysql 未安装"
        return [res]
    try:
        parsed = urlparse(db_url.replace("mysql+pymysql://", "mysql://"))
        conn = pymysql.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=parsed.username or "root",
            password=parsed.password or "",
            database=(parsed.path or "/").lstrip("/") or None,
            connect_timeout=5,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.close()
        res.latency_ms = round((time.time() - started) * 1000, 1)
        return [res]
    except Exception as e:
        res.latency_ms = round((time.time() - started) * 1000, 1)
        res.status = "error"
        res.error = f"{type(e).__name__}: {e}"
        return [res]


def check_redis(redis_url: str) -> list[CheckResult]:
    name = "redis:ping"
    res = CheckResult(name=name, target=_redact_url(redis_url),
                      status="ok", latency_ms=0.0, timestamp=_now())
    started = time.time()
    try:
        import redis
    except ImportError:
        res.status = "skipped"
        res.error = "redis 未安装"
        return [res]
    try:
        r = redis.Redis.from_url(redis_url, socket_connect_timeout=3)
        r.ping()
        res.latency_ms = round((time.time() - started) * 1000, 1)
        return [res]
    except Exception as e:
        res.latency_ms = round((time.time() - started) * 1000, 1)
        res.status = "error"
        res.error = f"{type(e).__name__}: {e}"
        return [res]


def _redact_url(url: str) -> str:
    """URL 脱敏，避免密码泄露到日志。"""
    try:
        p = urlparse(url)
        if p.password:
            netloc = p.netloc.replace(f":{p.password}@", ":***@")
            return f"{p.scheme}://{netloc}{p.path}"
    except Exception:
        pass
    return url


# ─────────────────── 入口 ───────────────────


def main() -> int:
    p = argparse.ArgumentParser(
        description="全栈健康检查（backend / BI / AI / DB / Redis）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--backend", default="http://localhost:8000",
                   help="后端 base URL（默认 http://localhost:8000）")
    p.add_argument("--bi", default="http://localhost:8501",
                   help="Streamlit BI base URL（默认 http://localhost:8501）")
    p.add_argument("--ai", default="http://localhost:8502",
                   help="Streamlit AI 助手 base URL（默认 http://localhost:8502）")
    p.add_argument("--db", default=os.environ.get("DATABASE_URL"),
                   help="MySQL URL（默认读环境变量 DATABASE_URL）")
    p.add_argument("--redis", dest="redis_url", default=os.environ.get("REDIS_URL"),
                   help="Redis URL（默认读环境变量 REDIS_URL）")
    p.add_argument("--checks", default="backend,bi,ai,db,redis",
                   help="要跑的检查（逗号分隔：backend,bi,ai,db,redis）")
    p.add_argument("--output", help="把 JSON 报告写到指定文件")
    p.add_argument("--fail-on-error", action="store_true",
                   help="有 check 失败时返回非零退出码（CI 用）")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="HTTP 请求超时秒数（默认 5）")
    args = p.parse_args()

    checks = {c.strip() for c in args.checks.split(",") if c.strip()}
    results: list[CheckResult] = []

    if "backend" in checks:
        results.extend(check_backend(args.backend.rstrip("/")))
    if "bi" in checks:
        results.extend(check_bi(args.bi.rstrip("/")))
    if "ai" in checks:
        results.extend(check_ai(args.ai.rstrip("/")))
    if "db" in checks:
        if args.db:
            results.extend(check_mysql(args.db))
        else:
            results.append(CheckResult(
                name="mysql:select_1", target="(unset)", status="skipped",
                latency_ms=0.0, error="未提供 --db / DATABASE_URL",
                timestamp=_now()))
    if "redis" in checks:
        if args.redis_url:
            results.extend(check_redis(args.redis_url))
        else:
            results.append(CheckResult(
                name="redis:ping", target="(unset)", status="skipped",
                latency_ms=0.0, error="未提供 --redis / REDIS_URL",
                timestamp=_now()))

    # ─────────── 汇总 ───────────
    total = len(results)
    err = sum(1 for r in results if r.status == "error")
    warn = sum(1 for r in results if r.status == "warn")
    ok = sum(1 for r in results if r.status == "ok")
    skipped = sum(1 for r in results if r.status == "skipped")
    summary_status = "ok" if err == 0 else "error"
    summary = {
        "status": summary_status,
        "checked_at": _now(),
        "total": total,
        "ok": ok,
        "warn": warn,
        "error": err,
        "skipped": skipped,
        "results": [r.to_dict() for r in results],
    }

    # ─────────── 控制台输出（人读） ───────────
    icon = {"ok": "✅", "warn": "⚠️ ", "error": "❌", "skipped": "⏭️ "}
    print(f"\n{'='*60}")
    print(f"  全栈健康检查  •  {summary_status.upper()}  •  "
          f"{ok}/{total} OK  •  {err} ERROR  •  {warn} WARN")
    print(f"{'='*60}\n")
    for r in results:
        i = icon.get(r.status, "?")
        line = f"  {i}  [{r.latency_ms:>6.1f}ms]  {r.name:<40s}  {r.target}"
        if r.error:
            line += f"\n        └─ {r.error}"
        print(line)
    print()

    # ─────────── 报告输出 ───────────
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"📄 报告已写入: {args.output}")

    # ─────────── 退出码 ───────────
    if args.fail_on_error and err > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
