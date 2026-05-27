import logging
import re
import json
import asyncio
from typing import Optional
from functools import lru_cache

from langchain_community.utilities.sql_database import SQLDatabase
from langchain_openai import ChatOpenAI
from langchain_community.agent_toolkits import create_sql_agent, SQLDatabaseToolkit

from backend.config import get_settings
from backend.models.schemas import AIQueryResponse

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
2. 日期筛选使用 order_time 列，格式 'YYYY-MM-DD'
3. 金额查询使用 payment_amount
4. 退款相关使用 is_refunded = '是'
5. SQL 结果较大时使用 LIMIT 限制
6. 先给出数据结论，再附上 SQL 语句
7. 用中文回答
8. 仅回答电商数据相关问题"""


def _is_sensitive(query: str) -> bool:
    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, query, re.IGNORECASE):
            return True
    return False


def _clean_sql(sql: str) -> str:
    if not sql:
        return ""
    sql = re.sub(r'```(?:sql)?\s*', '', sql)
    sql = re.sub(r'```\s*$', '', sql)
    return sql.strip()


def _extract_sql_from_intermediate(response: dict) -> Optional[str]:
    steps = response.get("intermediate_steps", [])
    for step in steps:
        if isinstance(step, tuple) and len(step) >= 2:
            action, observation = step
            if hasattr(action, "tool_input"):
                tool_input = action.tool_input
                if isinstance(tool_input, dict):
                    sql = tool_input.get("sql") or tool_input.get("query")
                    if sql and isinstance(sql, str) and "SELECT" in sql.upper():
                        return _clean_sql(sql)
                elif isinstance(tool_input, str) and "SELECT" in tool_input.upper():
                    return _clean_sql(tool_input)
    output = response.get("output", "")
    if isinstance(output, str):
        match = re.search(r'```sql\s*(.*?)```', output, re.IGNORECASE | re.DOTALL)
        if match:
            return _clean_sql(match.group(1))
    return None


def _extract_sql_from_answer(answer: str) -> Optional[str]:
    if not answer or not isinstance(answer, str):
        return None
    patterns = [
        r'```sql\s*(.*?)```',
        r'```(SELECT[\s\S]*?)```',
        r'(SELECT\s+[\s\S]*?;)',
    ]
    for pattern in patterns:
        match = re.search(pattern, answer, re.IGNORECASE | re.DOTALL)
        if match:
            return _clean_sql(match.group(1))
    return None


def _detect_chart_type(columns: list[str], rows: list) -> Optional[str]:
    if not rows or len(columns) < 2:
        return None
    col1 = columns[0].lower()
    time_keywords = ['date', '时间', 'time', 'hour', '日期', '月']
    if any(k in col1 for k in time_keywords):
        return "line"
    if len(rows) <= 6:
        return "pie"
    return "bar"


def _build_visualization(columns: list[str], rows: list) -> Optional[dict]:
    chart_type = _detect_chart_type(columns, rows)
    if not chart_type:
        return None
    return {
        "chart_type": chart_type,
        "x_field": columns[0],
        "y_field": columns[1] if len(columns) > 1 else None,
        "data": [dict(zip(columns, row)) for row in rows] if rows else [],
    }


@lru_cache(maxsize=1)
def _get_sync_db() -> SQLDatabase:
    db_url = (
        f"mysql+pymysql://{settings.db_user}:{settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/{settings.db_name}?charset=utf8mb4"
    )
    return SQLDatabase.from_uri(db_url)


@lru_cache(maxsize=1)
def _get_agent():
    if not settings.llm_api_key:
        return None
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

    prefix = f"""你是一个专业的电商数据分析助手。你可以访问一个名为 `orders` 的电商订单数据库表。

{BUSINESS_CONTEXT}

当用户提问时，你需要：
1. 理解用户意图
2. 生成正确的 SQL 查询
3. 执行查询获取数据
4. 用中文总结结论

请始终用中文回答。"""

    return create_sql_agent(
        llm=llm,
        toolkit=toolkit,
        prefix=prefix,
        verbose=False,
        agent_type="zero-shot-react-description",
        handle_parsing_errors=True,
    )


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
        agent = _get_agent()
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
            return AIQueryResponse(
                sql=extracted_sql,
                result=[],
                answer=clean_answer,
                visualization=None,
            )

        extracted_sql = _extract_sql_from_intermediate(response)

        result_data = []
        visualization = None

        if extracted_sql:
            try:
                db = _get_sync_db()
                raw_result = await asyncio.to_thread(db.run, extracted_sql)
                if isinstance(raw_result, str):
                    try:
                        parsed = json.loads(raw_result)
                        if isinstance(parsed, list):
                            result_data = parsed
                            if parsed and isinstance(parsed[0], dict):
                                columns = list(parsed[0].keys())
                                rows = [list(item.values()) for item in parsed]
                                visualization = _build_visualization(columns, rows)
                    except json.JSONDecodeError:
                        pass
            except Exception as e:
                logger.warning(f"SQL执行失败: {e}")

        return AIQueryResponse(
            sql=extracted_sql,
            result=result_data,
            answer=answer,
            visualization=visualization,
        )

    except Exception as e:
        logger.error(f"AI查询处理失败: {e}")
        return AIQueryResponse(
            sql=None,
            result=[],
            answer=f"⚠️ 查询处理失败: {str(e)}",
            visualization=None,
        )
