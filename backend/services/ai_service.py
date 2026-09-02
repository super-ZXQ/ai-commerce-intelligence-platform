import asyncio
import hashlib
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache

from langchain_community.agent_toolkits import SQLDatabaseToolkit, create_sql_agent
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_openai import ChatOpenAI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from backend.config import get_settings
from backend.models.schemas import AIQueryResponse
from backend.utils.sql_guard import guard_read_only_engine, is_read_only_sql
from backend.utils.text_cleaner import clean_sql as _clean_sql

logger = logging.getLogger(__name__)

settings = get_settings()

SENSITIVE_PATTERNS = [
    r"密码", r"手机号", r"身份证", r"地址.*具体",
    r"个人.*信息", r"隐私", r"password", r"phone.*number",
]

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


def _is_sensitive(query: str) -> bool:
    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, query, re.IGNORECASE):
            return True
    return False


def _extract_sql_from_intermediate(response: dict) -> str | None:
    steps = response.get("intermediate_steps", [])
    for step in steps:
        if isinstance(step, tuple) and len(step) >= 2:
            action, observation = step
            if hasattr(action, "tool_input"):
                tool_input = action.tool_input
                if isinstance(tool_input, dict):
                    sql = tool_input.get("sql") or tool_input.get("query")
                    if sql and isinstance(sql, str) and "SELECT" in sql.upper():
                        return _clean_sql(sql, strip_html=True)
                elif isinstance(tool_input, str) and "SELECT" in tool_input.upper():
                    return _clean_sql(tool_input, strip_html=True)
            if isinstance(observation, str):
                clean_obs = re.sub(r'<[^>]+>', '', observation)
                sql_match = re.search(r'SELECT\s+[\s\S]+?(?:;|$)', clean_obs, re.IGNORECASE)
                if sql_match:
                    return _clean_sql(sql_match.group(0), strip_html=True)
    output = response.get("output", "")
    if isinstance(output, str):
        match = re.search(r'```sql\s*(.*?)```', output, re.IGNORECASE | re.DOTALL)
        if match:
            return _clean_sql(match.group(1), strip_html=True)
    return None


def _extract_sql_from_answer(answer: str) -> str | None:
    if not answer or not isinstance(answer, str):
        return None
    # 保护代码块后再剥离 HTML 高亮标签（LLM 可能回显带 span 的 SQL）
    codeblocks: dict[str, str] = {}
    def _stash(m):
        key = f"\x00SQLCB{len(codeblocks)}ENDSQLCB\x00"
        codeblocks[key] = m.group(0)
        return key
    stripped = re.sub(r'```[\s\S]*?```', _stash, answer)
    stripped = re.sub(r'<[^>]+>', '', stripped)
    for k, v in codeblocks.items():
        stripped = stripped.replace(k, v)
    patterns = [
        r'```sql\s*(.*?)```',
        r'```(SELECT[\s\S]*?)```',
        r'(SELECT\s+[\s\S]*?;)',
    ]
    for pattern in patterns:
        match = re.search(pattern, stripped, re.IGNORECASE | re.DOTALL)
        if match:
            return _clean_sql(match.group(1), strip_html=True)
    return None


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

    db = _get_sync_db()
    with db._engine.connect() as connection:
        rows = connection.execute(text(sql)).mappings().all()
    return [
        {str(key): _json_safe_value(value) for key, value in row.items()}
        for row in rows
    ]


@lru_cache(maxsize=1)
def _get_sync_db() -> SQLDatabase:
    # 优先使用只读账户（部署环境为 ea_ai）：即使只读黑名单被绕过也无法写库。
    # 连接串组件统一走 quote_plus，密码含 @:/ 等字符时不会被污染。
    db = SQLDatabase.from_uri(
        settings.ai_database_url,
        engine_args={
            "pool_pre_ping": True,
            "pool_size": 3,
            "max_overflow": 2,
            "pool_recycle": 3600,
        },
    )
    guard_read_only_engine(db._engine)
    return db


_agent_cache: dict = {"agent": None, "settings_hash": None}
# agent 构建涉及网络 I/O（数据库引擎、LLM 客户端），并发首问需加锁防止重复构建
_agent_build_lock = asyncio.Lock()


def _get_settings_hash() -> str:
    return hashlib.md5(
        f"{settings.llm_api_key}:{settings.llm_base_url}:{settings.llm_model}".encode()
    ).hexdigest()


def _build_agent():
    db = _get_sync_db()
    llm = ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=0.1,
        timeout=120,
        max_retries=2,
    )
    toolkit = SQLDatabaseToolkit(db=db, llm=llm)

    prefix = f"""你是一个专业的 AI 智能商业分析助手。你可以访问一个名为 `orders` 的电商订单数据库表。

{BUSINESS_CONTEXT}

重要：你必须严格按照以下 ReAct 格式回答，每一步都要以 "Thought:" 开头：

Thought: 我需要查看表结构
Action: sql_db_schema
Action Input: orders

Thought: 根据表结构，我需要查询...
Action: sql_db_query
Action Input: SELECT ... FROM orders WHERE ...

Thought: 查询完成，总结结果
Final Answer: 根据查询结果...

注意：
- Action Input 必须是纯文本，不是 JSON
- sql_db_query 的 Action Input 必须是完整的 SELECT 语句
- 不要创建表，直接查询 orders 表
- 使用 payment_amount 表示付款金额（实际销售额）"""

    agent = create_sql_agent(
        llm=llm,
        toolkit=toolkit,
        prefix=prefix,
        verbose=False,
        agent_type="zero-shot-react-description",
        handle_parsing_errors=True,
        agent_executor_kwargs={"return_intermediate_steps": True},
    )
    return agent


async def _get_agent():
    if not settings.llm_api_key:
        return None

    current_hash = _get_settings_hash()
    if _agent_cache["agent"] is not None and _agent_cache["settings_hash"] == current_hash:
        return _agent_cache["agent"]

    async with _agent_build_lock:
        # double-check：等锁期间可能已被其他并发请求构建完成
        if _agent_cache["agent"] is not None and _agent_cache["settings_hash"] == current_hash:
            return _agent_cache["agent"]
        agent = await asyncio.to_thread(_build_agent)
        _agent_cache["agent"] = agent
        _agent_cache["settings_hash"] = current_hash
        return agent


async def process_natural_language_query(query: str) -> AIQueryResponse:
    """处理自然语言查询，返回SQL执行结果"""
    if _is_sensitive(query):
        return AIQueryResponse(
            sql=None,
            result=[],
            answer="⚠️ 该数据已脱敏，仅支持聚合查询，无法提供用户个人隐私数据。",
            visualization=None,
        )

    if not settings.llm_api_key:
        return AIQueryResponse(
            sql=None,
            result=[],
            answer="⚠️ AI功能未配置，请在 .env 中设置 LLM_API_KEY。",
            visualization=None,
        )

    try:
        agent = await _get_agent()
        if agent is None:
            return AIQueryResponse(
                sql=None,
                result=[],
                answer="⚠️ AI功能未配置，请在 .env 中设置 LLM_API_KEY。",
                visualization=None,
            )

        try:
            response = await asyncio.to_thread(agent.invoke, {"input": query})
        except Exception as invoke_err:
            err_msg = str(invoke_err)
            if "output parsing error" in err_msg.lower() or "Could not parse" in err_msg:
                match = re.search(r'Could not parse LLM output:\s*`([^`]*)', err_msg, re.DOTALL)
                raw_output = match.group(1).strip() if match else err_msg
                return AIQueryResponse(
                    sql=_extract_sql_from_answer(raw_output),
                    result=[],
                    answer=raw_output,
                    visualization=None,
                )
            raise

        answer = response.get("output", "抱歉，暂时无法回答这个问题。")

        if "output parsing error" in str(answer).lower() or "Could not parse" in str(answer):
            extracted_sql = _extract_sql_from_intermediate(response)
            if not extracted_sql:
                extracted_sql = _extract_sql_from_answer(answer)
            match = re.search(r'Could not parse LLM output:\s*`([^`]*)', answer, re.DOTALL)
            clean_answer = match.group(1).strip() if match else answer.split("Could not parse")[0].strip()

            result_data = []
            visualization = None
            sql_error = None
            if extracted_sql:
                try:
                    if is_read_only_sql(extracted_sql):
                        result_data = await asyncio.to_thread(
                            _execute_query_with_columns,
                            extracted_sql,
                        )

                        if result_data and isinstance(result_data[0], dict):
                            columns = list(result_data[0].keys())
                            rows = [list(item.values()) for item in result_data]
                            visualization = _build_visualization(columns, rows)
                except SQLAlchemyError as e:
                    # 记录并上抛执行错误，避免"SQL 挂了"被误判为"无数据"
                    logger.warning(f"解析错误后SQL执行失败: {e}")
                    sql_error = f"SQL 执行失败: {e}"

            if not result_data:
                steps = response.get("intermediate_steps", [])
                for step in steps:
                    if isinstance(step, tuple) and len(step) >= 2:
                        _, observation = step
                        if isinstance(observation, str):
                            obs_match = re.search(r'\[?\{[^}]+\}?\]', observation)
                            if obs_match:
                                try:
                                    parsed = json.loads(obs_match.group(0))
                                    if isinstance(parsed, list):
                                        result_data = parsed
                                    elif isinstance(parsed, dict):
                                        result_data = [parsed]
                                    if result_data and isinstance(result_data[0], dict):
                                        columns = list(result_data[0].keys())
                                        rows = [list(item.values()) for item in result_data]
                                        visualization = _build_visualization(columns, rows)
                                    break
                                except json.JSONDecodeError:
                                    pass

            return AIQueryResponse(
                sql=extracted_sql,
                result=result_data,
                answer=clean_answer,
                visualization=visualization,
                sql_error=sql_error,
            )

        extracted_sql = _extract_sql_from_intermediate(response)

        result_data = []
        visualization = None
        sql_error = None

        if extracted_sql:
            try:
                if not is_read_only_sql(extracted_sql):
                    logger.warning(f"拦截危险SQL: {extracted_sql[:100]}")
                    return AIQueryResponse(
                        sql=None,
                        result=[],
                        answer="⚠️ 仅支持数据查询操作，不允许修改数据库",
                        visualization=None,
                    )
                result_data = await asyncio.to_thread(
                    _execute_query_with_columns,
                    extracted_sql,
                )

                if result_data and isinstance(result_data[0], dict):
                    columns = list(result_data[0].keys())
                    rows = [list(item.values()) for item in result_data]
                    visualization = _build_visualization(columns, rows)
            except SQLAlchemyError as e:
                # 记录并上抛执行错误，避免"SQL 挂了"被误判为"无数据"
                logger.warning(f"SQL执行失败: {e}")
                sql_error = f"SQL 执行失败: {e}"

        return AIQueryResponse(
            sql=extracted_sql,
            result=result_data,
            answer=answer,
            visualization=visualization,
            sql_error=sql_error,
        )

    except Exception:  # noqa: BLE001 - 顶层兜底，AI 调用链可能抛任意异常
        logger.exception("AI查询处理失败")
        return AIQueryResponse(
            sql=None,
            result=[],
            answer="⚠️ 查询处理失败，请稍后重试",
            visualization=None,
        )
