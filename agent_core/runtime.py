"""可注入、可测试的分节点 Agent Runtime。"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from agent_core.models import AgentIntent, AgentResult, AgentSource, AgentStep, AgentUsage, ModelResponse
from agent_core.operational_metrics import record_llm_failure, record_rag_failure
from agent_core.session import ConversationStore, MemoryConversationStore, Message
from agent_core.sql_safety import (
    SQLValidationError,
    apply_execution_guard,
    validate_and_limit_sql,
)
from agent_core.routing import classify_intent
from agent_core.streaming import reset_token_hook, set_token_hook

Retriever = Callable[[str], Awaitable[list[AgentSource]]]
SchemaLoader = Callable[[], Awaitable[str]]
SQLGenerator = Callable[[str, str, list[Message], list[AgentSource], str | None], Awaitable[ModelResponse]]
SQLExecutor = Callable[[str], Awaitable[list[dict[str, Any]]]]
AnswerGenerator = Callable[
    [str, AgentIntent, list[Message], list[AgentSource], str | None, list[dict[str, Any]]],
    Awaitable[ModelResponse],
]

_INJECTION_PATTERNS = (
    r"忽略(?:以上|之前|系统).*(?:指令|提示词)",
    r"(?:system prompt|developer message|ignore previous instructions)",
    r"输出.*(?:api.?key|密钥|环境变量)",
)

# 数据库侧时限生效后留给 wait_for 的宽限期倍数：数据库负责中断，wait_for 只做兜底。
_TIMEOUT_GRACE_FACTOR = 1.5


class RuntimeState(TypedDict, total=False):
    query: str
    owner: str
    request_id: str
    thread_id: str
    intent: AgentIntent
    history: list[Message]
    sources: list[AgentSource]
    schema: str
    generated_sql: str
    sql: str
    rows: list[dict[str, Any]]
    sql_error: str
    retry_count: int
    answer: str
    steps: list[AgentStep]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    has_usage: bool
    result: AgentResult
    started_at: float


def _public_error(exc: Exception) -> str:
    """仅保留错误类别，避免数据库结构、凭证或请求内容进入轨迹。"""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "查询执行超时"
    if isinstance(exc, SQLValidationError):
        return str(exc)
    return f"{type(exc).__name__}：执行失败"


class AgentRuntime:
    """共享状态图；模型、检索器和数据库均由调用端注入。"""

    def __init__(
        self,
        *,
        retriever: Retriever,
        schema_loader: SchemaLoader,
        sql_generator: SQLGenerator,
        sql_executor: SQLExecutor,
        answer_generator: AnswerGenerator,
        conversations: ConversationStore | None = None,
        sql_timeout_seconds: float = 10,
        sql_dialect: str = "mysql",
    ):
        self._retriever = retriever
        self._schema_loader = schema_loader
        self._sql_generator = sql_generator
        self._sql_executor = sql_executor
        self._answer_generator = answer_generator
        self._conversations = conversations or MemoryConversationStore()
        self._sql_timeout_seconds = sql_timeout_seconds
        self._sql_dialect = sql_dialect

        graph = StateGraph(RuntimeState)
        graph.add_node("input_safety", self._input_safety)
        graph.add_node("load_history", self._load_history)
        graph.add_node("route", self._route)
        graph.add_node("safe_response", self._safe_response)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("load_schema", self._load_schema)
        graph.add_node("generate_sql", self._generate_sql)
        graph.add_node("validate_sql", self._validate_sql)
        graph.add_node("execute_sql", self._execute_sql)
        graph.add_node("synthesize", self._synthesize)
        graph.add_node("save_session", self._save_session)
        graph.add_node("finalize", self._finalize)

        graph.add_edge(START, "input_safety")
        graph.add_edge("input_safety", "load_history")
        graph.add_edge("load_history", "route")
        graph.add_conditional_edges(
            "route",
            lambda state: state["intent"],
            {
                "blocked": "safe_response",
                "clarification": "safe_response",
                "knowledge": "retrieve",
                "data": "load_schema",
                "hybrid": "retrieve",
            },
        )
        graph.add_conditional_edges(
            "retrieve",
            lambda state: "schema" if state["intent"] == "hybrid" else "answer",
            {"schema": "load_schema", "answer": "synthesize"},
        )
        graph.add_conditional_edges(
            "load_schema",
            lambda state: "answer" if state.get("sql_error") else "generate",
            {"answer": "synthesize", "generate": "generate_sql"},
        )
        graph.add_conditional_edges(
            "generate_sql",
            self._after_generation,
            {"validate": "validate_sql", "retry": "generate_sql", "answer": "synthesize"},
        )
        graph.add_conditional_edges(
            "validate_sql",
            self._after_validation,
            {"execute": "execute_sql", "retry": "generate_sql", "answer": "synthesize"},
        )
        graph.add_conditional_edges(
            "execute_sql",
            self._after_execution,
            {"retry": "generate_sql", "answer": "synthesize"},
        )
        graph.add_edge("safe_response", "save_session")
        graph.add_edge("synthesize", "save_session")
        graph.add_edge("save_session", "finalize")
        graph.add_edge("finalize", END)
        self._graph = graph.compile()

    @property
    def conversations(self) -> ConversationStore:
        """暴露会话存储，供调用方实现"重新生成"等需要删除轮次的操作。"""
        return self._conversations

    @staticmethod
    def _event(name: str, started: float, summary: str, status: str = "success") -> AgentStep:
        return AgentStep(
            name=name,
            status="error" if status == "error" else "success",
            duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
            summary=summary,
        )

    @staticmethod
    def _append(state: RuntimeState, event: AgentStep) -> list[AgentStep]:
        return [*state.get("steps", []), event]

    @staticmethod
    def _usage_update(response: ModelResponse, state: RuntimeState) -> dict[str, Any]:
        values = (response.input_tokens, response.output_tokens, response.total_tokens)
        if all(value is None for value in values):
            return {}
        return {
            "input_tokens": state.get("input_tokens", 0) + (response.input_tokens or 0),
            "output_tokens": state.get("output_tokens", 0) + (response.output_tokens or 0),
            "total_tokens": state.get("total_tokens", 0) + (response.total_tokens or 0),
            "has_usage": True,
        }

    async def _input_safety(self, state: RuntimeState) -> dict[str, Any]:
        started = time.perf_counter()
        injected = any(re.search(pattern, state["query"], re.IGNORECASE) for pattern in _INJECTION_PATTERNS)
        update: dict[str, Any] = {}
        if injected:
            update["intent"] = "blocked"
        update["steps"] = self._append(state, self._event("input_safety", started, "输入安全检查完成"))
        return update

    async def _load_history(self, state: RuntimeState) -> dict[str, Any]:
        started = time.perf_counter()
        history = await self._conversations.get_history(state["owner"], state["thread_id"])
        return {
            "history": history,
            "steps": self._append(state, self._event("load_history", started, f"加载最近 {len(history) // 2} 轮会话")),
        }

    async def _route(self, state: RuntimeState) -> dict[str, Any]:
        started = time.perf_counter()
        intent = state.get("intent") or classify_intent(state["query"])
        return {
            "intent": intent,
            "steps": self._append(state, self._event("route", started, f"识别为 {intent} 类型问题")),
        }

    async def _safe_response(self, state: RuntimeState) -> dict[str, Any]:
        started = time.perf_counter()
        answer = (
            "⚠️ 仅支持聚合分析和只读查询，不能提供隐私数据、泄露配置或修改数据库。"
            if state["intent"] == "blocked"
            else "请补充要分析的指标、时间范围或维度，例如“最近30天各平台销售额趋势”。"
        )
        return {
            "answer": answer,
            "steps": self._append(state, self._event("safe_response", started, "未调用模型和数据库")),
        }

    async def _retrieve(self, state: RuntimeState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            sources = (await self._retriever(state["query"]))[:3]
            event = self._event("retrieve", started, f"检索到 {len(sources)} 条来源")
        except Exception as exc:  # noqa: BLE001 - RAG 无法使用时允许无引用降级
            record_rag_failure(type(exc).__name__)
            sources = []
            event = self._event("retrieve", started, "知识检索不可用，已降级", "error")
        return {
            "sources": sources,
            "steps": self._append(state, event),
        }

    async def _load_schema(self, state: RuntimeState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            schema = await self._schema_loader()
            event = self._event("load_schema", started, "读取只读业务表结构")
            error = ""
        except Exception as exc:  # noqa: BLE001 - 数据库边界需可控降级
            schema = ""
            error = _public_error(exc)
            event = self._event("load_schema", started, "业务表结构读取失败", "error")
        return {
            "schema": schema,
            "sql_error": error,
            "steps": self._append(state, event),
        }

    async def _generate_sql(self, state: RuntimeState) -> dict[str, Any]:
        started = time.perf_counter()
        previous_error = state.get("sql_error")
        try:
            response = await self._sql_generator(
                state["query"], state["schema"], state.get("history", []), state.get("sources", []), previous_error
            )
        except Exception as exc:  # noqa: BLE001 - 模型边界需可控降级
            record_llm_failure(type(exc).__name__)
            retry_count = state.get("retry_count", 0) + 1
            return {
                "generated_sql": "",
                "sql_error": _public_error(exc),
                "retry_count": retry_count,
                "steps": self._append(state, self._event("generate_sql", started, "SQL 生成失败", "error")),
            }
        update = self._usage_update(response, state)
        update.update(
            {
                "generated_sql": response.content.strip(),
                "sql_error": "",
                "steps": self._append(state, self._event("generate_sql", started, "生成结构化 SQL 候选")),
            }
        )
        return update

    @staticmethod
    def _after_generation(state: RuntimeState) -> str:
        if not state.get("sql_error"):
            return "validate"
        return "retry" if state.get("retry_count", 0) <= 1 else "answer"

    async def _validate_sql(self, state: RuntimeState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            sql = validate_and_limit_sql(state["generated_sql"])
            # 把执行时限一并下推到数据库；state["sql"] 即真正执行的语句，便于审计。
            sql = apply_execution_guard(sql, int(self._sql_timeout_seconds * 1000), self._sql_dialect)
            return {
                "sql": sql,
                "steps": self._append(state, self._event("validate_sql", started, "SQL AST 只读校验通过")),
            }
        except SQLValidationError as exc:
            retry_count = state.get("retry_count", 0) + 1
            return {
                "sql_error": _public_error(exc),
                "retry_count": retry_count,
                "steps": self._append(state, self._event("validate_sql", started, "SQL 校验失败", "error")),
            }

    @staticmethod
    def _after_validation(state: RuntimeState) -> str:
        if not state.get("sql_error"):
            return "execute"
        return "retry" if state.get("retry_count", 0) <= 1 else "answer"

    async def _execute_sql(self, state: RuntimeState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            # 执行上限已经写进 SQL（数据库自己中断）；这里只是兜底：wait_for 取消不了
            # 已在库内运行的查询，所以留出宽限期，避免误报"查询执行超时"。
            rows = await asyncio.wait_for(
                self._sql_executor(state["sql"]),
                timeout=self._sql_timeout_seconds * _TIMEOUT_GRACE_FACTOR,
            )
            return {
                "rows": rows,
                "sql_error": "",
                "steps": self._append(state, self._event("execute_sql", started, f"只读查询返回 {len(rows)} 行")),
            }
        except Exception as exc:  # noqa: BLE001 - 工具边界需统一降级
            retry_count = state.get("retry_count", 0) + 1
            return {
                "sql_error": _public_error(exc),
                "retry_count": retry_count,
                "steps": self._append(state, self._event("execute_sql", started, "只读查询失败", "error")),
            }

    @staticmethod
    def _after_execution(state: RuntimeState) -> str:
        if state.get("sql_error") and state.get("retry_count", 0) <= 1:
            return "retry"
        return "answer"

    async def _synthesize(self, state: RuntimeState) -> dict[str, Any]:
        started = time.perf_counter()
        if state.get("sql_error"):
            return {
                "answer": f"⚠️ 查询执行失败：{state['sql_error']}。已停止自动重试，请调整问题后再试。",
                "steps": self._append(state, self._event("synthesize", started, "返回可控失败说明", "error")),
            }
        try:
            response = await self._answer_generator(
                state["query"], state["intent"], state.get("history", []), state.get("sources", []),
                state.get("sql"), state.get("rows", []),
            )
        except Exception as exc:  # noqa: BLE001 - 模型边界需可控降级
            record_llm_failure(type(exc).__name__)
            return {
                "answer": f"⚠️ 回答生成失败：{_public_error(exc)}。",
                "steps": self._append(state, self._event("synthesize", started, "回答生成失败", "error")),
            }
        update = self._usage_update(response, state)
        update.update(
            {
                "answer": response.content,
                "steps": self._append(state, self._event("synthesize", started, "根据工具结果合成回答")),
            }
        )
        return update

    async def _save_session(self, state: RuntimeState) -> dict[str, Any]:
        started = time.perf_counter()
        await self._conversations.save_turn(state["owner"], state["thread_id"], state["query"], state["answer"])
        return {"steps": self._append(state, self._event("save_session", started, "保存本轮会话"))}

    async def _finalize(self, state: RuntimeState) -> dict[str, Any]:
        latency_ms = max(0, round((time.perf_counter() - state["started_at"]) * 1000))
        usage = AgentUsage(
            latency_ms=latency_ms,
            input_tokens=state.get("input_tokens") if state.get("has_usage") else None,
            output_tokens=state.get("output_tokens") if state.get("has_usage") else None,
            total_tokens=state.get("total_tokens") if state.get("has_usage") else None,
        )
        return {
            "result": AgentResult(
                answer=state["answer"], sql=state.get("sql"), rows=state.get("rows", []),
                intent=state["intent"], sources=state.get("sources", []), steps=state.get("steps", []),
                usage=usage, sql_error=state.get("sql_error") or None,
            )
        }

    async def invoke(self, query: str, *, owner: str = "anonymous", thread_id: str | None = None) -> RuntimeState:
        return await self._graph.ainvoke(
            {
                "query": query.strip(), "owner": owner, "request_id": str(uuid4()),
                "thread_id": thread_id or str(uuid4()), "steps": [], "retry_count": 0,
                "sources": [], "rows": [], "started_at": time.perf_counter(),
            }
        )

    async def astream(
        self,
        query: str,
        *,
        owner: str = "anonymous",
        thread_id: str | None = None,
        on_step: Callable[[dict[str, Any]], None] | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> RuntimeState:
        """流式执行：每个节点完成即回调 on_step(步骤 dict)。

        on_token 经 ContextVar 传递给 answer 生成方（见 agent_core.streaming），
        调用端注入的回调签名保持不变。返回值与 invoke() 完全一致，
        调用方的后处理逻辑无需区分流式 / 非流式。
        """
        hook_token = set_token_hook(on_token) if on_token is not None else None
        try:
            state: RuntimeState = {
                "query": query.strip(), "owner": owner, "request_id": str(uuid4()),
                "thread_id": thread_id or str(uuid4()), "steps": [], "retry_count": 0,
                "sources": [], "rows": [], "started_at": time.perf_counter(),
            }
            emitted = 0
            # stream_mode="updates" 逐节点产出增量；RuntimeState 无 reducer，
            # 每个 update 的 "steps" 都是全量列表，用 emitted 游标只下发新步骤。
            async for update in self._graph.astream(state, stream_mode="updates"):
                for node_update in update.values():
                    if not isinstance(node_update, dict):
                        continue
                    state.update(node_update)
                    if on_step is not None:
                        steps = node_update.get("steps") or []
                        for step in steps[emitted:]:
                            on_step(asdict(step))
                        emitted = max(emitted, len(steps))
            return state
        finally:
            if hook_token is not None:
                reset_token_hook(hook_token)
