import asyncio
import logging
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from agent_core import AgentRuntime
from agent_core.db_schema import describe_table
from agent_core.db_permissions import verify_ai_permissions
from agent_core.business_data import BUSINESS_CONTEXT, describe_data_range
from agent_core.model_adapter import OpenAIModelAdapter
from agent_core.rag import MarkdownKnowledgeRetriever
from agent_core.session import FallbackConversationStore, MemoryConversationStore, RedisConversationStore
from agent_core.sql_safety import dialect_from_url
from backend.config import get_settings
from backend.models.schemas import AIQueryResponse, AgentUsage
from backend.utils.sql_guard import guard_read_only_engine, is_read_only_sql

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
    if not settings.debug:
        verify_ai_permissions(engine)
    guard_read_only_engine(engine)
    return engine


_knowledge_retriever = MarkdownKnowledgeRetriever(
    Path(__file__).resolve().parents[2] / "ai-ecommerce-assistant" / "knowledge_base"
)


async def _runtime_retrieve(query: str):
    return await _knowledge_retriever.retrieve(query, top_k=3)


async def _runtime_schema() -> str:
    def load():
        engine = _get_sync_engine()
        return describe_table(engine, "orders") + "\n" + describe_data_range(engine)
    return await asyncio.to_thread(load)


async def _runtime_execute(sql: str) -> list[dict]:
    # AST 层之后仍保留执行器黑名单与数据库只读账户两层防护。
    return await asyncio.to_thread(_execute_query_with_columns, sql)


_model_adapter = OpenAIModelAdapter(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    model=settings.llm_model,
    business_context=BUSINESS_CONTEXT,
)


def _conversation_store():
    memory = MemoryConversationStore(ttl_seconds=1800, max_sessions=1000, max_turns=6)
    primary = RedisConversationStore(settings.redis_url) if settings.redis_enabled else None
    return FallbackConversationStore(primary, memory)


_runtime = AgentRuntime(
    retriever=_runtime_retrieve,
    schema_loader=_runtime_schema,
    sql_generator=_model_adapter.generate_sql,
    sql_executor=_runtime_execute,
    answer_generator=_model_adapter.answer,
    conversations=_conversation_store(),
    sql_timeout_seconds=10,
    # 方言决定能否把执行时限下推到数据库（MySQL 用 MAX_EXECUTION_TIME）。
    sql_dialect=dialect_from_url(settings.ai_database_url),
)


async def process_natural_language_query(
    query: str,
    thread_id: str | None = None,
    *,
    owner: str = "anonymous",
) -> AIQueryResponse:
    """使用共享分节点 Runtime，并转换为向后兼容的 API 响应。"""
    state = await _runtime.invoke(query, owner=owner, thread_id=thread_id)
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
