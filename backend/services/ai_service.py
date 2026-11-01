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
from agent_core.model_adapter import OpenAIModelAdapter
from agent_core.provider_policy import (
    ModelConfig,
    ProviderPolicyError,
    build_byok_config,
    build_managed_config,
)
from agent_core.rag import MarkdownKnowledgeRetriever
from agent_core.session import FallbackConversationStore, MemoryConversationStore, RedisConversationStore
from agent_core.sql_safety import dialect_from_url
from backend.config import get_settings
from backend.models.schemas import AIQueryResponse, AgentUsage
from backend.utils.sql_guard import guard_read_only_engine, is_read_only_sql
from backend.utils.text_cleaner import sanitize_error

logger = logging.getLogger(__name__)

settings = get_settings()

BUSINESS_CONTEXT = """## 数据时间范围
- 数据时间范围：2025-01-01 至 2025-12-31（共1年数据）
- 当用户提到"最近N天"时，指的是数据中的最近N天

## 业务指标定义
- 付款金额 = 实际销售额（非订单金额）
- 平台类型枚举值：APP、微信公众号、Web网站、其他
- 是否退款：是=已退款，否=未退款
- 退款率 = 退款订单数 / 总订单数
- 复购率 = 消费2次及以上的用户数 / 总用户数
- 客单价 = 总付款金额 / 总订单数
- 数据起止日期仅用于解释“最近”问题；用户未指定时间时，禁止添加 order_time/order_date 条件或全年过滤
- 订单量必须使用 COUNT(DISTINCT order_id)，禁止使用 COUNT(*)
- 除非用户明确要求排除退款，否则销售额、订单量、用户数等指标包含退款订单
- 用户明确指定月份或日期时必须严格使用该范围，不得扩大为全年

## 回答规则
1. 始终先查看表结构确认列名，再编写 SQL
2. 日期筛选使用 order_date 列，格式 'YYYY-MM-DD'
3. 金额查询使用 payment_amount
4. 退款相关使用 is_refund = '是'（注意：is_refund 是物理列名，
   ORM 属性名是 is_refunded，本提示词面向原生 SQL，必须用物理列名 is_refund）
5. SQL 结果较大时使用 LIMIT 限制
6. 先给出数据结论，再附上 SQL 语句
7. 用中文回答
8. 仅回答电商数据相关问题"""


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
        "data": [dict(zip(columns, row)) for row in rows] if rows else [],
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
        rows = connection.execute(text(sql)).mappings().all()
    return [
        {str(key): _json_safe_value(value) for key, value in row.items()}
        for row in rows
    ]


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


async def _runtime_execute(sql: str) -> list[dict]:
    # AST 层之后仍保留执行器黑名单与数据库只读账户两层防护。
    return await asyncio.to_thread(_execute_query_with_columns, sql)


_model_adapter = OpenAIModelAdapter(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    model=settings.llm_model,
    business_context=BUSINESS_CONTEXT,
    allowed_hosts=settings.llm_allowed_hosts,
)


def _conversation_store():
    memory = MemoryConversationStore(ttl_seconds=1800, max_sessions=1000, max_turns=6)
    # 会话使用独立逻辑 DB 仅作命名隔离；同一实例的 allkeys-lru 仍可能淘汰会话。
    primary = (
        RedisConversationStore(settings.redis_session_url)
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
        sql_timeout_seconds=10,
        # 方言决定能否把执行时限下推到数据库（MySQL 用 MAX_EXECUTION_TIME）。
        sql_dialect=dialect_from_url(settings.ai_database_url),
    )


_runtime = _build_runtime(_model_adapter)

# BYOK 运行时按“模式+密钥哈希+地址+模型”隔离；只缓存少量指纹，避免密钥常驻内存。
_MAX_CACHED_RUNTIMES = 8
_runtime_cache: OrderedDict[str, tuple[AgentRuntime, OpenAIModelAdapter]] = OrderedDict()


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
            resolve_dns=False,
        )
    except ProviderPolicyError as exc:
        raise AIModelConfigError(str(exc)) from exc


def _runtime_for(config: ModelConfig | None) -> AgentRuntime:
    if config is None:
        return _runtime
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
    )
    runtime = _build_runtime(adapter)
    _runtime_cache[fingerprint] = (runtime, adapter)
    while len(_runtime_cache) > _MAX_CACHED_RUNTIMES:
        _, (_, evicted_adapter) = _runtime_cache.popitem(last=False)
        try:
            asyncio.get_running_loop().create_task(evicted_adapter.aclose())
        except RuntimeError:
            pass
    return runtime


def _state_to_response(state: dict[str, Any]) -> AIQueryResponse:
    result = state["result"]
    visualization = None
    if result.rows:
        columns = list(result.rows[0])
        visualization = _build_visualization(columns, [list(item.values()) for item in result.rows])
    return AIQueryResponse(
        sql=result.sql,
        result=result.rows,
        answer=result.answer,
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
    state = await runtime.invoke(query, owner=owner, thread_id=thread_id)
    return _state_to_response(state)


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
    state = await runtime.astream(
        query,
        owner=owner,
        thread_id=thread_id,
        on_step=on_step,
        on_token=on_token,
    )
    return _state_to_response(state)


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
