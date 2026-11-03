import asyncio
import hashlib
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from agent_core import AgentRuntime
from agent_core.db_schema import describe_table
from agent_core.prompts import BUSINESS_CONTEXT
from agent_core.memory import EpisodeSummary, EpisodeTurn, MemoryFact, MemoryTurn
from agent_core.model_adapter import OpenAIModelAdapter
from agent_core.prompt_privacy import PromptMasker, derive_masking_key, masking_scope
from agent_core.provider_policy import (
    ModelConfig,
    ProviderPolicyError,
    build_byok_config,
    build_managed_config,
)
from agent_core.rag import MarkdownKnowledgeRetriever
from agent_core.session import FallbackConversationStore, MemoryConversationStore, RedisConversationStore
from agent_core.sql_safety import DEFAULT_MAX_ROWS, dialect_from_url
from backend.config import get_settings
from backend.models.schemas import AIQueryResponse, AgentUsage
from backend.services import episode_memory_service, memory_service
from backend.utils.sql_guard import guard_read_only_engine, is_read_only_sql
from backend.utils.text_cleaner import sanitize_error

logger = logging.getLogger(__name__)

settings = get_settings()

def _detect_chart_type(columns: list[str], rows: list) -> str | None:
    if not rows or len(columns) < 2:
        return None
    col1 = columns[0].lower()
    time_keywords = ['date', '时间', 'time', 'hour', '日期', '月']
    if any(k in col1 for k in time_keywords):
        return "line"
    if len(rows) <= 6:
        return "pie"
    return "bar"


def _build_visualization(columns: list[str], rows: list) -> dict | None:
    chart_type = _detect_chart_type(columns, rows)
    if not chart_type:
        return None
    return {
        "chart_type": chart_type,
        "x_field": columns[0],
        "y_field": columns[1] if len(columns) > 1 else None,
        "data": [dict(zip(columns, row, strict=True)) for row in rows] if rows else [],
    }


def _json_safe_value(value):
    """把数据库类型转换为 API 可稳定序列化的基础类型。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _execute_query_with_columns(sql: str) -> list[dict]:
    """执行只读 SQL，并直接使用数据库返回的列名映射结果。

    不再从 SELECT 文本中按逗号猜测列名；CASE、ROUND 等复杂表达式内部
    也包含逗号，字符串切分会导致列名与结果错位。
    """
    if not is_read_only_sql(sql):
        raise ValueError("仅允许执行只读 SQL")

    with _get_sync_engine().connect() as connection:
        # yield_per 会启用服务端游标，避免 LIMIT 失效时把全表拉进内存；
        # 同时仍设置硬上限，AST 改写与执行层形成双保险。
        result = connection.execution_options(
            yield_per=DEFAULT_MAX_ROWS + 1
        ).execute(text(sql))
        rows = []
        for row in result.mappings():
            if len(rows) >= DEFAULT_MAX_ROWS:
                break
            rows.append({str(key): _json_safe_value(value) for key, value in row.items()})
    return rows


# SQL 执行时限与 Runtime 共用同一常量；驱动层 read_timeout 必须略大于它，
# 否则 asyncio.wait_for 取消协程后，pymysql 仍会占着连接与线程。
_SQL_TIMEOUT_SECONDS = 10


@lru_cache(maxsize=1)
def _get_sync_engine() -> Engine:
    # 优先使用只读账户（部署环境为 ea_ai）：即使只读黑名单被绕过也无法写库。
    # 连接串组件统一走 quote_plus，密码含 @:/ 等字符时不会被污染。
    engine = create_engine(
        settings.ai_database_url,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=2,
        pool_recycle=3600,
        # 驱动级超时是最后一道兜底：数据库/网络卡死时主动断开 socket，
        # 避免慢查询永久占用默认线程池与连接池。
        connect_args={
            "connect_timeout": 5,
            "read_timeout": _SQL_TIMEOUT_SECONDS + 2,
            "write_timeout": _SQL_TIMEOUT_SECONDS + 2,
        },
    )
    guard_read_only_engine(engine)
    return engine


_knowledge_retriever = MarkdownKnowledgeRetriever(
    Path(__file__).resolve().parents[2] / "ai-ecommerce-assistant" / "knowledge_base"
)


async def _runtime_retrieve(query: str):
    return await _knowledge_retriever.retrieve(query, top_k=3)


def knowledge_status() -> dict:
    """后端知识库状态；Streamlit 不再各自初始化本地检索器。"""
    return _knowledge_retriever.status()


def _probe_database() -> dict:
    dialect = dialect_from_url(settings.ai_database_url)
    try:
        with _get_sync_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"ok": True, "dialect": dialect}
    except Exception as exc:  # noqa: BLE001 - 只暴露异常类型，不泄露连接串
        logger.warning("AI 数据库探活失败: %s", type(exc).__name__)
        return {"ok": False, "dialect": dialect, "error": type(exc).__name__}


async def database_status() -> dict:
    """AI 查询所用只读连接的可用性，供前端状态条展示。"""
    return await asyncio.to_thread(_probe_database)


async def pop_last_conversation_turn(
    owner: str,
    thread_id: str,
    question: str | None = None,
) -> bool:
    """撤销末轮问答；question 用于确认待删轮次，避免误删其他上下文。"""
    return await _conversations.pop_last_turn(owner, thread_id, question)


async def _runtime_schema() -> str:
    return await asyncio.to_thread(describe_table, _get_sync_engine(), "orders")


async def _runtime_recall_memories(owner: str) -> list[MemoryFact]:
    return await memory_service.recall_memories(owner)


async def _runtime_write_memory(turn: MemoryTurn) -> int:
    return await memory_service.remember_turn(turn)


async def _runtime_recall_episodes(owner: str, query: str) -> list[EpisodeSummary]:
    return await episode_memory_service.recall_episodes(owner, query)


async def _runtime_write_episode(turn: EpisodeTurn) -> bool:
    return await episode_memory_service.save_episode(turn)


async def _runtime_execute(sql: str) -> list[dict]:
    # AST 层之后仍保留执行器黑名单与数据库只读账户两层防护。
    return await asyncio.to_thread(_execute_query_with_columns, sql)


def _prompt_masking_key() -> bytes:
    """提示词假名密钥；配置里没有可用机密时退回进程内随机密钥。"""
    return derive_masking_key(settings.prompt_masking_secret)


@lru_cache(maxsize=1)
def _get_default_adapter() -> OpenAIModelAdapter:
    """延迟构造托管模型适配器。

    在模块导入期构造会在 LLM_BASE_URL 配置错误时直接杀死整个 FastAPI
    进程（即使当前使用 BYOK 或根本不调用 AI）。改为首次使用时构造，
    配置错误只在 AI 请求内以可控错误返回。
    """
    return OpenAIModelAdapter(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        business_context=BUSINESS_CONTEXT,
        allowed_hosts=settings.llm_allowed_hosts,
        history_max_chars=settings.agent_history_max_chars,
        masking_key=_prompt_masking_key(),
        max_output_tokens=settings.agent_llm_max_output_tokens,
        prompt_rows_max_chars=settings.agent_prompt_rows_max_chars,
    )


def _conversation_store():
    memory = MemoryConversationStore(
        ttl_seconds=settings.agent_session_ttl_seconds,
        max_sessions=settings.agent_session_max_sessions,
        max_turns=settings.agent_session_max_turns,
    )
    # 会话使用独立逻辑 DB 仅作命名隔离；同一实例的 allkeys-lru 仍可能淘汰会话。
    primary = (
        RedisConversationStore(
            settings.redis_session_url,
            ttl_seconds=settings.agent_session_ttl_seconds,
            max_turns=settings.agent_session_max_turns,
        )
        if settings.redis_enabled
        else None
    )
    return FallbackConversationStore(primary, memory)


_conversations = _conversation_store()


def _build_runtime(adapter: OpenAIModelAdapter) -> AgentRuntime:
    return AgentRuntime(
        retriever=_runtime_retrieve,
        schema_loader=_runtime_schema,
        sql_generator=adapter.generate_sql,
        sql_executor=_runtime_execute,
        answer_generator=adapter.astream_answer,
        analysis_planner=adapter.choose_refund_analysis,
        conversations=_conversations,
        memory_recall=_runtime_recall_memories if settings.agent_memory_enabled else None,
        memory_writer=_runtime_write_memory if settings.agent_memory_enabled else None,
        episode_recall=_runtime_recall_episodes if settings.agent_episode_enabled else None,
        episode_writer=_runtime_write_episode if settings.agent_episode_enabled else None,
        memory_max_chars=settings.agent_memory_max_chars,
        sql_timeout_seconds=_SQL_TIMEOUT_SECONDS,
        # 方言决定能否把执行时限下推到数据库（MySQL 用 MAX_EXECUTION_TIME）。
        sql_dialect=dialect_from_url(settings.ai_database_url),
    )


@lru_cache(maxsize=1)
def _get_default_runtime() -> AgentRuntime:
    return _build_runtime(_get_default_adapter())


# BYOK 运行时按“模式+密钥哈希+地址+模型”隔离；只缓存少量指纹，避免密钥常驻内存。
_MAX_CACHED_RUNTIMES = 8
_runtime_cache: OrderedDict[str, tuple[AgentRuntime, OpenAIModelAdapter]] = OrderedDict()
# 事件循环可能只保留弱引用；显式持有后台关闭任务，避免 aclose 被提前回收。
_adapter_close_tasks: set[asyncio.Task] = set()


class AIModelConfigError(ValueError):
    """模型模式或 BYOK 请求头不符合部署策略。"""


def resolve_model_config(
    *,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
) -> ModelConfig | None:
    """按部署模式解析本次请求的模型配置。

    - managed：完全使用服务端配置，出现任何 BYOK 请求头都拒绝，避免静默改变计费主体；
    - byok：必须携带密钥、地址和模型，地址仍受统一 allowlist 与 SSRF 校验；
    - managed 尚未配置密钥时返回 None，由 Runtime 在真正调用模型时给出可控失败。
    """
    has_byok_headers = any(value and value.strip() for value in (api_key, base_url, model))
    if settings.llm_mode == "managed":
        if has_byok_headers:
            raise AIModelConfigError("托管模式不接受 BYOK 请求头")
        if not settings.llm_api_key:
            return None
        try:
            return build_managed_config(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                allowed_hosts=settings.llm_allowed_hosts,
                resolve_dns=False,
            )
        except ProviderPolicyError as exc:
            raise AIModelConfigError(str(exc)) from exc

    if not settings.allow_byok:
        raise AIModelConfigError("BYOK 未启用")
    if not (api_key and base_url and model):
        raise AIModelConfigError("BYOK 模式需要 X-LLM-Api-Key、X-LLM-Base-Url 和 X-LLM-Model")
    try:
        return build_byok_config(
            api_key=api_key.strip(),
            base_url=base_url.strip(),
            model=model.strip(),
            allowed_hosts=settings.llm_allowed_hosts,
            # 用户可控地址必须解析 DNS 并拒绝内网/链路本地结果；
            # 适配器每次建连前还会再校验一次，降低 DNS 重绑定风险。
            resolve_dns=True,
        )
    except ProviderPolicyError as exc:
        raise AIModelConfigError(str(exc)) from exc


def _runtime_for(config: ModelConfig | None) -> AgentRuntime:
    if config is None:
        return _get_default_runtime()
    fingerprint = hashlib.sha256(
        f"{config.mode}|{hashlib.sha256(config.api_key.encode()).hexdigest()}|"
        f"{config.base_url}|{config.model}".encode()
    ).hexdigest()
    cached = _runtime_cache.get(fingerprint)
    if cached is not None:
        _runtime_cache.move_to_end(fingerprint)
        return cached[0]

    adapter = OpenAIModelAdapter(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        business_context=BUSINESS_CONTEXT,
        allowed_hosts=settings.llm_allowed_hosts,
        history_max_chars=settings.agent_history_max_chars,
        masking_key=_prompt_masking_key(),
        max_output_tokens=settings.agent_llm_max_output_tokens,
        prompt_rows_max_chars=settings.agent_prompt_rows_max_chars,
    )
    runtime = _build_runtime(adapter)
    _runtime_cache[fingerprint] = (runtime, adapter)
    while len(_runtime_cache) > _MAX_CACHED_RUNTIMES:
        _, (_, evicted_adapter) = _runtime_cache.popitem(last=False)
        try:
            task = asyncio.get_running_loop().create_task(evicted_adapter.aclose())
        except RuntimeError:
            pass
        else:
            _adapter_close_tasks.add(task)
            task.add_done_callback(_adapter_close_tasks.discard)
    return runtime


def _state_to_response(state: dict[str, Any], masker: PromptMasker | None = None) -> AIQueryResponse:
    result = state["result"]
    visualization = None
    if result.rows:
        columns = list(result.rows[0])
        visualization = _build_visualization(columns, [list(item.values()) for item in result.rows])
    answer = result.answer
    if masker is not None:
        # 模型只看到假名；返回给用户的文本按本轮映射换回原值。
        answer = masker.restore(answer)
    return AIQueryResponse(
        sql=result.sql,
        result=result.rows,
        answer=answer,
        visualization=visualization,
        sql_error=result.sql_error,
        request_id=state["request_id"],
        thread_id=state["thread_id"],
        intent=result.intent,
        analysis_evidence=result.analysis_evidence,
        sources=[
            {
                "filename": item.filename,
                "section": item.section,
                "doc_type": item.doc_type,
                "score": item.score,
                "snippet": item.snippet,
            }
            for item in result.sources
        ],
        steps=[
            {
                "name": item.name,
                "status": item.status,
                "duration_ms": item.duration_ms,
                "summary": item.summary,
            }
            for item in result.steps
        ],
        usage=AgentUsage(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            total_tokens=result.usage.total_tokens,
            latency_ms=result.usage.latency_ms,
        ) if result.usage else None,
    )


async def process_natural_language_query(
    query: str,
    thread_id: str | None = None,
    *,
    owner: str = "anonymous",
    config: ModelConfig | None = None,
) -> AIQueryResponse:
    """非流式入口；配额、并发与审计由路由层统一执行。"""
    runtime = _runtime_for(config)
    with masking_scope(_prompt_masking_key()) as masker:
        state = await runtime.invoke(query, owner=owner, thread_id=thread_id)
        return _state_to_response(state, masker)


async def stream_natural_language_query(
    query: str,
    thread_id: str | None = None,
    *,
    owner: str = "anonymous",
    config: ModelConfig | None = None,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    on_token: Callable[[str], None] | None = None,
) -> AIQueryResponse:
    """流式入口：步骤与 token 经回调下发，返回值与非流式完全一致。"""
    runtime = _runtime_for(config)
    with masking_scope(_prompt_masking_key()) as masker:
        state = await runtime.astream(
            query,
            owner=owner,
            thread_id=thread_id,
            on_step=on_step,
            on_token=on_token,
        )
        return _state_to_response(state, masker)


async def test_model_connection(config: ModelConfig) -> bool:
    """探活只使用本次配置，不复用其他主体的模型客户端。"""
    adapter = OpenAIModelAdapter(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        business_context=BUSINESS_CONTEXT,
        allowed_hosts=settings.llm_allowed_hosts,
    )
    try:
        await asyncio.to_thread(adapter.test_connection)
        return True
    except Exception as exc:  # noqa: BLE001 - 只返回布尔状态，避免密钥/地址进入响应
        logger.warning("模型连接测试失败: %s", sanitize_error(exc))
        return False
    finally:
        await adapter.aclose()
