"""Langfuse trace 级观测（可选，默认关闭）。

与 Prometheus 的分工见 docs/observability.md：Prometheus 记录聚合指标
（QPS、失败率、token 总量），Langfuse 记录单次查询的执行轨迹——
每个 LangGraph 节点一个 span、每次 LLM 调用一个 generation，
含 token / 成本 / 延迟，可在 UI 里回放完整链路。

未配置环境变量时本模块全部静默降级（与 A/B 评测同风格的开关设计），
任何 Langfuse 侧异常都不会影响主流程。

环境变量：
  LANGFUSE_ENABLED                "true" 时启用，默认关闭
  LANGFUSE_PUBLIC_KEY             自托管实例的 public key
  LANGFUSE_SECRET_KEY             自托管实例的 secret key
  LANGFUSE_HOST                   自托管实例地址，如 http://localhost:3000
  LANGFUSE_PRICE_INPUT_PER_MTOK   输入单价（美元/百万 token），可选；
                                  未配置时代码侧不估算成本（可在 Langfuse
                                  UI 的 model price map 中配置自动计算）
  LANGFUSE_PRICE_OUTPUT_PER_MTOK  输出单价（美元/百万 token），可选
  LANGFUSE_FLUSH_EACH_QUERY       "true" 时每次查询结束等待上报完成，
                                  默认 false（后台批量上报，进程退出时兜底 flush）
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 %s 不是合法数字，已忽略：%s", name, raw)
        return None


def trace_id_for(request_id: str) -> str:
    """把 RuntimeState.request_id（uuid4 字符串）映射为 Langfuse trace_id。

    同一请求的 trace_id 由 request_id 确定派生，面试排查时可凭
    request_id 反查 trace。request_id 非 UUID 格式时用 uuid5 保证
    结果仍是合法 32 位 hex。
    """
    try:
        return uuid.UUID(request_id).hex
    except (ValueError, AttributeError, TypeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, f"agent-query:{request_id}").hex


class _LangfuseGateway:
    """Langfuse SDK 的惰性门面：未启用 / SDK 缺失时所有操作为 no-op。"""

    def __init__(self) -> None:
        self._client = None
        self._handler_cls = None
        self._resolved = False

    def _resolve(self) -> bool:
        if self._resolved:
            return self._client is not None
        self._resolved = True
        if not _env_flag("LANGFUSE_ENABLED"):
            return False
        try:
            from langfuse import Langfuse
            from langfuse.langchain import CallbackHandler
        except ImportError:
            logger.warning("LANGFUSE_ENABLED=true 但 langfuse 包未安装，trace 观测静默跳过")
            return False
        if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
            logger.warning("LANGFUSE_ENABLED=true 但未配置 LANGFUSE_PUBLIC_KEY/SECRET_KEY，trace 观测静默跳过")
            return False
        try:
            self._client = Langfuse()
            self._handler_cls = CallbackHandler
        except Exception as exc:  # noqa: BLE001 - 观测初始化失败不得影响主流程
            logger.warning("Langfuse 客户端初始化失败，trace 观测静默跳过：%s", type(exc).__name__)
            self._client = None
            return False
        return True

    def build_query_config(
        self,
        *,
        request_id: str,
        owner: str,
        query: str,
        thread_id: str,
    ) -> dict[str, Any] | None:
        """构造 LangGraph 的 config：挂 Langfuse callback，并把请求标识
        写进 trace metadata。未启用时返回 None。"""
        if not self._resolve():
            return None
        trace_id = trace_id_for(request_id)
        try:
            handler = self._handler_cls(
                public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
                trace_context={"trace_id": trace_id},
                update_trace=True,
            )
        except Exception as exc:  # noqa: BLE001 - 同上，观测失败不影响查询
            logger.warning("Langfuse handler 创建失败，本次查询不上报 trace：%s", type(exc).__name__)
            return None
        return {
            "callbacks": [handler],
            "metadata": {
                "langfuse_trace_id": trace_id,
                "owner": owner,
                "thread_id": thread_id,
                "query_length": len(query),
            },
            "tags": ["agent-query"],
        }

    def record_query_summary(
        self,
        request_id: str,
        *,
        intent: str | None,
        usage: Any,
        latency_ms: int,
        step_names: list[str],
        sql_error: str | None,
    ) -> None:
        """查询结束后向同一 trace 追加汇总事件（token/延迟/意图/步骤链）。"""
        if not self._resolve():
            return
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        metadata: dict[str, Any] = {
            "intent": intent,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "latency_ms": latency_ms,
            "steps": step_names,
            "sql_error": sql_error,
        }
        cost = estimate_cost_usd(input_tokens, output_tokens)
        if cost is not None:
            metadata["estimated_cost_usd"] = cost
        try:
            self._client.create_event(
                trace_context={"trace_id": trace_id_for(request_id)},
                name="agent-finalize",
                output={"sql_error": sql_error} if sql_error else None,
                metadata=metadata,
            )
            if _env_flag("LANGFUSE_FLUSH_EACH_QUERY"):
                self._client.flush()
        except Exception as exc:  # noqa: BLE001 - 观测失败不影响查询
            logger.warning("Langfuse 汇总事件上报失败（trace 可能不完整）：%s", type(exc).__name__)


_gateway = _LangfuseGateway()


def build_query_config(
    *,
    request_id: str,
    owner: str,
    query: str,
    thread_id: str,
) -> dict[str, Any] | None:
    """模块级入口：构造 LangGraph config；未启用 Langfuse 时返回 None。"""
    return _gateway.build_query_config(
        request_id=request_id, owner=owner, query=query, thread_id=thread_id
    )


def record_query_summary(
    request_id: str,
    *,
    intent: str | None,
    usage: Any,
    latency_ms: int,
    step_names: list[str],
    sql_error: str | None,
) -> None:
    """模块级入口：查询结束汇总；未启用时静默返回。"""
    _gateway.record_query_summary(
        request_id,
        intent=intent,
        usage=usage,
        latency_ms=latency_ms,
        step_names=step_names,
        sql_error=sql_error,
    )


def estimate_cost_usd(
    input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """按配置单价估算成本（美元）。任一侧未配置单价时返回 None——
    不编造数字（延续仓库诚实传统）。"""
    price_in = _env_float("LANGFUSE_PRICE_INPUT_PER_MTOK")
    price_out = _env_float("LANGFUSE_PRICE_OUTPUT_PER_MTOK")
    if price_in is None and price_out is None:
        return None
    cost = 0.0
    if price_in is not None and input_tokens:
        cost += input_tokens / 1_000_000 * price_in
    if price_out is not None and output_tokens:
        cost += output_tokens / 1_000_000 * price_out
    return round(cost, 6)
