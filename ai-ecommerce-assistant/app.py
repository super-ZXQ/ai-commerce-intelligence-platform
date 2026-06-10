import os
import re
import sys
import json
import ast
import time
import datetime
import hashlib
import decimal
import logging
from pathlib import Path
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_openai import ChatOpenAI
from langchain_community.agent_toolkits import create_sql_agent, SQLDatabaseToolkit
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# 复用 backend 的共享工具（clean_sql），保证两处实现一致
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from backend.utils.text_cleaner import clean_sql  # noqa: E402

# 业务知识来源抽取（无 streamlit 依赖，便于单元测试）
from rag import extract_rag_sources  # noqa: E402
from rag import metrics as rag_metrics  # noqa: E402

load_dotenv()

logger = logging.getLogger(__name__)

# 用户反馈落盘路径（jsonl，append-only）
FEEDBACK_LOG_PATH = os.environ.get(
    "RAG_FEEDBACK_PATH",
    str(Path(__file__).resolve().parent / "eval" / "feedback.jsonl"),
)

# 启用 RAG 事件 JSONL 落盘（默认关闭磁盘 IO，避免拖慢；可由环境变量开启）
if os.environ.get("RAG_EVENTS_LOG", "0") == "1":
    rag_metrics.enable_event_file_logging()


def record_feedback(question: str, answer: str, rating: str,
                    rag_sources: list[dict] | None = None) -> bool:
    """把用户 👍/👎 反馈追加到 jsonl 文件，供离线分析检索质量。

    Args:
        question: 用户问题。
        answer: AI 回答（前 500 字符，避免文件膨胀）。
        rating: "up" 或 "down"。
        rag_sources: 本次回答引用的业务知识来源（用于线下分析"误检索"）。

    Returns:
        是否落盘成功。
    """
    payload = {
        "ts": int(time.time() * 1000),
        "question": question[:300],
        "answer_preview": answer[:500],
        "rating": rating,
        "rag_sources_count": len(rag_sources or []),
        "rag_filenames": [s.get("filename", "") for s in (rag_sources or [])],
    }
    try:
        p = Path(FEEDBACK_LOG_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        logger.warning("反馈落盘失败: %s", e)
        return False

API_KEY = os.getenv("LLM_API_KEY")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
MODEL_NAME = os.getenv("LLM_MODEL", "deepseek-v4-flash")

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "ai_commerce_intelligence_platform")

USE_MYSQL = bool(DB_USER and DB_PASSWORD)

BUSINESS_CONTEXT = """## 数据时间范围
- 数据时间范围：2025-01-01 至 2025-12-31（共1年数据）
- 当前日期：2026年（数据截止到2025年12月31日）
- 【重要】当用户提到"最近N天"时，指的是数据中的最近N天，而非当前日期

## 业务指标定义
- 付款金额 = 实际销售额（非订单金额）
- 平台类型枚举值：APP、微信公众号、Web网站、其他
- 是否退款：是=已退款，否=未退款
- 退款率 = 退款订单数 / 总订单数
- 复购率 = 消费2次及以上的用户数 / 总用户数
- 客单价 = 总付款金额 / 总订单数
- RFM 分层阈值：Recency≤30天为活跃，Frequency≥2次为高频，Monetary≥1500元为高价值
- 下单小时：0-23，高峰时段为 11-13 时和 19-21 时
- 星期几：周一至周日

## 回答规则
1. 始终先查看表结构确认列名，再编写 SQL
2. 日期筛选使用 order_date 列，格式 'YYYY-MM-DD'
3. 【重要】当用户提到"最近N天"时，自动替换为数据中的最近N天。例如：
   - "最近7天" → 数据中最近7天：2025-12-25 至 2025-12-31
   - "最近30天" → 数据中最近30天
4. 金额查询使用 payment_amount（实际付款金额）
5. 退款相关使用 is_refunded = '是' 表示已退款
6. SQL 结果较大时使用 LIMIT 限制
7. 先给出数据结论，再附上 SQL 语句
8. 用中文回答
9. 仅回答电商数据相关问题
10. 如果发现某指标明显异常（如某渠道退款率远超平均值），请在回答末尾添加【⚠️ 异常预警】段落，给出业务建议"""

SENSITIVE_PATTERNS = [
    r"密码", r"手机号", r"身份证", r"地址.*具体", r"订单明细.*用户名",
    r"个人.*信息", r"隐私", r"password", r"phone.*number",
]

SENSITIVE_RESPONSE = "⚠️ 该数据已脱敏，仅支持聚合查询，无法提供用户个人隐私数据。"


def is_sensitive_query(query: str) -> bool:
    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, query, re.IGNORECASE):
            return True
    return False


def get_cache_key(question: str) -> str:
    return hashlib.md5(question.encode()).hexdigest()


st.set_page_config(page_title="AI Commerce Intelligence Platform", page_icon="🤖", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown("""
<style>
    [data-testid="stSidebar"] { min-width: 300px; max-width: 360px; }
    .step-indicator {
        display: flex; gap: 8px; align-items: center;
        padding: 12px 16px; border-radius: 8px;
        background: rgba(79,70,229,0.1); margin-bottom: 12px;
    }
    .step-item { font-size: 14px; }
    .step-done { color: #22C55E; }
    .step-active { color: #FACC15; animation: pulse 1s infinite; }
    .step-pending { color: #6B7280; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
    .highlight-num { color: #818CF8; font-weight: 700; font-size: 1.1em; }
    .warning-box {
        background: rgba(234,179,8,0.1); border-left: 4px solid #EAB308;
        padding: 10px 14px; border-radius: 6px; margin: 8px 0;
    }
    .sql-block {
        position: relative; background: #1a1a2e; border-radius: 8px;
        padding: 12px; margin: 8px 0; font-family: monospace;
        font-size: 13px; overflow-x: auto;
    }
    .sql-copy-btn {
        position: absolute; top: 6px; right: 6px;
        background: rgba(79,70,229,0.3); border: 1px solid #4F46E5;
        color: #E4E4E7; padding: 2px 10px; border-radius: 4px;
        cursor: pointer; font-size: 11px;
    }
    .sql-copy-btn:hover { background: rgba(79,70,229,0.6); }
    .query-time { color: #9CA3AF; font-size: 12px; margin-top: 4px; }
    .history-item {
        padding: 6px 10px; border-radius: 6px; margin-bottom: 4px;
        background: rgba(79,70,229,0.08); cursor: pointer;
        transition: background 0.2s; font-size: 13px;
    }
    .history-item:hover { background: rgba(79,70,229,0.2); }
</style>
""", unsafe_allow_html=True)

st.title("🤖 AI 智能商业分析平台")
st.caption("基于 LangChain + DeepSeek V4 Flash 的 Text-to-SQL 智能查询 | 自然语言提问 → 自动生成 SQL → 数据可视化")

if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": "你好！我是你的数据分析助手。你可以问我：\n"
                       "- 销售额最高的 3 个商品编号和金额\n"
                       "- APP 和公众号哪个销售额高\n"
                       "- 用户复购率是多少",
            "chart_data": None, "chart_title": "", "csv_data": None,
            "sql": None, "query_time": None,
        }
    ]

if "query_cache" not in st.session_state:
    st.session_state.query_cache = {}

if "query_history" not in st.session_state:
    st.session_state.query_history = []


def get_db_uri():
    if USE_MYSQL:
        return f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    db_path = os.path.join(os.path.dirname(__file__), "ecommerce.db")
    return f"sqlite:///{db_path}"


@st.cache_resource
def init_db():
    try:
        return SQLDatabase.from_uri(get_db_uri())
    except Exception as e:
        st.error(f"❌ 数据库连接失败：{e}")
        return None


@st.cache_resource
def init_engine() -> Engine | None:
    """独立于 langchain 的 SQLAlchemy Engine，直接执行 SQL。
    避免 db.run(sql, fetch="cursor") 在部分方言（如 KingbaseES）上抛
    NotImplementedError 后 fallback 到 ast.literal_eval 的 'malformed node or string' 错误。"""
    try:
        return create_engine(get_db_uri(), pool_pre_ping=True)
    except Exception as e:
        st.warning(f"⚠️ SQLAlchemy engine 初始化失败：{e}")
        return None


@st.cache_resource
def init_retriever():
    """初始化 RAG 检索器（懒加载，第一次调用时下载 BGE 模型 ~93MB）。

    Returns:
        (retriever, status_dict) 元组。status_dict 包含 ok / error / count。
    """
    try:
        from rag import VectorStore, Retriever, get_embeddings
        embed = get_embeddings()
        store = VectorStore(embedding=embed)
        retriever = Retriever(store, k=3, score_threshold=0.4)
        return retriever, {
            "ok": True,
            "error": None,
            "count": store.count(),
        }
    except Exception as e:
        logger.error("RAG 初始化失败: %s", e)
        return None, {
            "ok": False,
            "error": str(e),
            "count": 0,
        }


@st.cache_resource
def init_agent(_db, _retriever):
    llm = ChatOpenAI(
        api_key=API_KEY,
        base_url=BASE_URL,
        model=MODEL_NAME,
        temperature=0.1,
    )
    toolkit = SQLDatabaseToolkit(db=_db, llm=llm)

    # 默认 prefix（向后兼容，无 RAG 时使用）
    default_prefix = f"""你是一个专业的 AI 智能商业分析助手。你可以访问一个名为 `orders` 的电商订单数据库表。

{BUSINESS_CONTEXT}

当用户提问时，你需要：
1. 理解用户意图
2. 生成正确的 SQL 查询
3. 执行查询获取数据
4. 用中文总结结论
5. 如果发现异常指标，给出业务建议

请始终用中文回答。"""

    # 尝试用 RAG 增强版 prefix + 业务知识工具
    try:
        from rag import build_augmented_prefix
        from rag.tools import build_knowledge_tool

        extra_tools = []
        if _retriever is not None:
            extra_tools.append(build_knowledge_tool(_retriever))
            prefix = build_augmented_prefix(BUSINESS_CONTEXT)
        else:
            prefix = default_prefix
    except Exception as e:
        logger.error("RAG 增强初始化失败，使用默认 prefix: %s", e)
        prefix = default_prefix
        extra_tools = []

    return create_sql_agent(
        llm=llm,
        toolkit=toolkit,
        prefix=prefix,
        extra_tools=extra_tools,
        verbose=True,
        agent_type="zero-shot-react-description",
    )


def detect_chart_type(df: pd.DataFrame, question: str = "") -> str:
    if df is None or len(df) == 0 or len(df.columns) < 2:
        return "none"
    
    col1, col2 = df.columns[0], df.columns[1]
    
    try:
        pd.to_numeric(df[col2], errors='raise')
    except (ValueError, TypeError):
        return "none"
    
    question_lower = question.lower()
    col1_lower = col1.lower()
    col2_lower = col2.lower() if col2 else ""
    
    rank_keywords = ['top', '最高', '排名', '排行', r'前\d+', '销量最高', '销售额最高']
    if any(re.search(k, question_lower) for k in rank_keywords):
        return "bar"
    
    compare_keywords = ['哪个', '谁', '比较', '对比', 'vs', '更多', '更高', '更低', '差异']
    if any(k in question_lower for k in compare_keywords):
        return "bar"
    
    rate_keywords = ['退款率', '转化率', '点击率', '复购率', 'rate', 'ratio', '比例对比', '各.*率']
    if any(k in question_lower for k in rate_keywords) or \
       (any(k in col2_lower for k in ['rate', 'ratio', '率']) and len(df) > 2):
        return "bar_h"
    
    first_col_str = df[col1].astype(str)
    
    is_date_format = first_col_str.str.match(r'^\d{4}-\d{2}-\d{2}').any() or \
                     first_col_str.str.match(r'^\d{2}/\d{2}').any() or \
                     (first_col_str.str.contains(r'^(0?[1-9]|1[0-9]|2[0-3])$', regex=True).any() and len(df) >= 12)
    
    time_col_names = ['date', '时间', 'time', 'hour', 'weekday', '星期', 'month', '月', 'order_date', 'order_hour']
    
    time_keywords = ['每天', '每日', '趋势', '变化', '时间段', '24小时', '最近', '周', '月份']
    if is_date_format or \
       (any(k in col1_lower for k in time_col_names)) or \
       (any(k in question_lower for k in time_keywords) and not any(k in question_lower for k in ['top', '最高'])):
        return "line"
    
    share_keywords = ['占比', '份额', '构成', '组成', '分布情况', 'percent of total']
    if any(k in question_lower for k in share_keywords) and len(df) <= 8:
        return "pie"
    
    if len(df) <= 4:
        return "pie"
    
    return "bar"


def create_chart(df: pd.DataFrame, title: str = "", question: str = "") -> go.Figure | None:
    chart_type = detect_chart_type(df, question)
    
    if chart_type == "none":
        return None

    cols = df.columns.tolist()
    x_col, y_col = cols[0], cols[1]

    if not title or title == "undefined" or not title.strip():
        title = f"{y_col} 分析"

    if chart_type == "line":
        fig = px.line(df, x=x_col, y=y_col, template="plotly_dark", title=title,
                      markers=True, color_discrete_sequence=["#818CF8"])
        fig.update_traces(line_width=3, marker_size=10,
                          fill='tozeroy', fillcolor='rgba(129,140,248,0.1)')
        fig.add_scatter(x=df[x_col], y=df[y_col], mode='markers',
                        marker=dict(size=12, color="#FACC15", line=dict(width=2, color='#fff')), showlegend=False)

    elif chart_type == "pie":
        fig = px.pie(df, names=x_col, values=y_col, template="plotly_dark", title=title,
                     hole=0.4, color_discrete_sequence=px.colors.qualitative.Set2)
        fig.update_traces(textposition='inside', textinfo='percent+label',
                          textfont=dict(size=13, color='white'),
                          hovertemplate='<b>%{label}</b><br>数值: %{value:,.2f}<br>占比: %{percent}<extra></extra>',
                          pull=[0.02] * len(df))

    elif chart_type in ["bar_h", "bar"]:
        df_plot = df.copy()

        try:
            df_plot[y_col] = pd.to_numeric(df_plot[y_col], errors='coerce')
        except Exception:
            pass

        if len(df_plot) > 8:
            df_plot = df_plot.nlargest(8, columns=y_col).reset_index(drop=True)

        n_items = len(df_plot)
        # 企业级配色：渐变红色系（高→低）
        bar_colors = ['#EF4444', '#F87171', '#FCA5A5', '#FECACA', '#FEF2F2',
                      '#FEE2E2', '#FEF5F7', '#FFF1F2']

        def format_y_label(val):
            val_str = str(val).strip()
            if val_str.isdigit():
                hour_val = int(val_str)
                if 0 <= hour_val <= 23:
                    return f"{hour_val}时"
                elif hour_val >= 1 and hour_val <= 31:
                    return f"{hour_val}日"
            return val_str

        # 计算合适的图表高度：每项至少 85px + 标题区 + 边距，让条形更显著
        dynamic_height = max(550, 140 + n_items * 85)

        if chart_type == "bar_h":
            fig = go.Figure()
            for i in range(n_items):
                y_val = df_plot[y_col].iloc[i]
                x_raw = df_plot[x_col].iloc[i]
                x_label = format_y_label(x_raw)
                try:
                    y_num = float(y_val)
                    y_text = f'{y_num:,.0f}'
                except (ValueError, TypeError):
                    y_num = 0
                    y_text = str(y_val)

                fig.add_trace(go.Bar(
                    x=[y_num],
                    y=[x_label],
                    orientation='h',
                    name=x_label,
                    marker=dict(
                        color=bar_colors[i % len(bar_colors)],
                        line=dict(width=0, color='rgba(255,255,255,0)'),
                        cornerradius=4,
                    ),
                    text=y_text,
                    textposition='outside',
                    textfont=dict(size=14, color='#E4E4E7', family='monospace'),
                    hovertemplate=f'<b>{x_label}</b><br>%{{x:,.0f}}<extra></extra>',
                ))

            fig.update_layout(height=dynamic_height, barmode='group')

        else:
            df_sorted = df_plot.sort_values(by=y_col, ascending=True).reset_index(drop=True)
            fig = go.Figure()
            for i in range(n_items):
                y_val = df_sorted[y_col].iloc[i]
                x_raw = df_sorted[x_col].iloc[i]
                x_label = format_y_label(x_raw)
                try:
                    y_num = float(y_val)
                    y_text = f'{y_num:,.0f}'
                except (ValueError, TypeError):
                    y_num = 0
                    y_text = str(y_val)

                fig.add_trace(go.Bar(
                    x=[y_num],
                    y=[x_label],
                    orientation='h',
                    name=x_label,
                    marker=dict(
                        color=bar_colors[(n_items - 1 - i) % len(bar_colors)],
                        line=dict(width=0, color='rgba(255,255,255,0)'),
                        cornerradius=4,
                    ),
                    text=y_text,
                    textposition='outside',
                    textfont=dict(size=14, color='#E4E4E7', family='monospace'),
                    hovertemplate=f'<b>{x_label}</b><br>%{{x:,.0f}}<extra></extra>',
                ))

            fig.update_layout(height=dynamic_height, barmode='group')

    else:
        fig = go.Figure()

    fig.update_layout(
        title=dict(text=title, x=0.02, xanchor='left', y=0.97, yanchor='top',
                   font=dict(size=18, color='#818CF8')),
        margin=dict(l=80 if chart_type in ["bar_h", "bar"] else 30,
                    r=100 if chart_type in ["bar_h", "bar"] else 30,
                    t=70, b=50),
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(15,17,23,0.8)',
        font=dict(color='#E4E4E7', size=13),
        title_font=dict(size=16, color='#818CF8'),
        showlegend=False,
        xaxis=dict(
            showgrid=False if chart_type in ["bar_h", "bar"] else True,
            gridcolor='rgba(255,255,255,0.05)',
            showticklabels=False if chart_type in ["bar_h", "bar"] else True,
            tickangle=-20 if chart_type == "line" else 0,
            tickfont=dict(size=11),
            zeroline=False,
            range=[0, None] if chart_type in ["bar_h", "bar"] else None,
        ),
        yaxis=dict(
            showgrid=True if chart_type in ["bar_h", "bar"] else True,
            gridcolor='rgba(255,255,255,0.05)',
            tickfont=dict(size=11),
        ),
        hovermode='closest',
        bargap=0.5,
    )

    return fig


def extract_sql_from_intermediate(response: dict) -> str | None:
    steps = response.get("intermediate_steps", [])
    
    for step in steps:
        if isinstance(step, tuple) and len(step) >= 2:
            action, observation = step
            
            if hasattr(action, "tool_input"):
                tool_input = action.tool_input
                if isinstance(tool_input, dict):
                    sql = tool_input.get("sql") or tool_input.get("query")
                    if sql and isinstance(sql, str) and "SELECT" in sql.upper():
                        return clean_sql_local(sql)
                elif isinstance(tool_input, str) and "SELECT" in tool_input.upper():
                    return clean_sql_local(tool_input)

            if isinstance(observation, str):
                # 观测文本可能含 HTML 高亮标签，先剥再抽
                clean_obs = re.sub(r'<[^>]+>', '', observation)
                sql_match = re.search(r'SELECT\s+[\s\S]+?(?:;|$)', clean_obs, re.IGNORECASE)
                if sql_match:
                    return clean_sql_local(sql_match.group(0))

    output = response.get("output", "")
    if isinstance(output, str):
        patterns = [
            r'```sql\s*(.*?)```',
            r'```(SELECT[\s\S]*?)```',
            r'SELECT\s+[\s\S]+?FROM\s+[\s\S]+?(?:;|```|$)',
        ]
        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE | re.DOTALL)
            if match:
                return clean_sql_local(match.group(1))

    return None


# 业务知识来源抽取：从 RAG 工具的 sentinel observation 还原元数据列表。
# 函数实现见 rag.extractor，单独成模块以便无 streamlit 依赖的单元测试。
def extract_sql_from_answer(answer: str) -> str | None:
    if not answer or not isinstance(answer, str):
        return None

    # 保护 markdown 代码块：避免后续 HTML 标签清理破坏 ``` 标记
    codeblocks: dict[str, str] = {}
    def _stash(m):
        key = f"\x00SQLCB{len(codeblocks)}ENDSQLCB\x00"
        codeblocks[key] = m.group(0)
        return key
    stripped = re.sub(r'```[\s\S]*?```', _stash, answer)
    # 剥离非代码块中的 HTML 高亮标签（LLM 可能把上一轮带高亮的 SQL 又回显了）
    stripped = re.sub(r'<[^>]+>', '', stripped)

    # 还原代码块
    for k, v in codeblocks.items():
        stripped = stripped.replace(k, v)

    patterns = [
        r'```sql\s*(.*?)```',
        r'```(SELECT[\s\S]*?)```',
        r'(SELECT\s+[\s\S]*?;)',
        r'SELECT\s+[\w\s,\(\)\*]+\s+FROM\s+\w+[\s\S]*?(?:;|```|$)',
    ]
    for pattern in patterns:
        match = re.search(pattern, stripped, re.IGNORECASE | re.DOTALL)
        if match:
            return clean_sql_local(match.group(1))
    return None


def parse_data_from_answer(answer: str) -> pd.DataFrame | None:
    if not answer or not isinstance(answer, str):
        return None

    rows = []

    # 模式1：商品编号/平台 + 数值（最精确，优先匹配）
    # 匹配 "商品编号: PR000385 销售额: 481182" 或 "APP: 201单" 等
    patterns = [
        # 商品编号 PRxxxxx + 金额
        r'(?:^|\n|[-•|])\s*(PR\d{4,})[^\d]*(\d[\d,]*\.?\d*)',
        # 商品编号: xxx 格式
        r'(?:商品编号|产品编号|product_id)[^\w]*(PR\d+)[^\d]*(\d[\d,]*\.?\d*)',
        # 平台类型 + 数值
        r'(APP|微信公众号|网站|web|小程序|公众号)[^\d]{0,10}(\d[\d,]*\.?\d*)',
        # 中文键名: 值
        r'[-•|]\s*([^\d:：\n]{2,20}?)[：:]\s*([￥¥$]?\s*[\d,]+\.?\d*)',
        # 纯 key: value
        r'(\w+)\s*[：:]\s*([￥¥$]?\s*[\d,]+\.?\d*)\s*(?:元|单|%|)?',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, answer, re.IGNORECASE | re.MULTILINE)
        if matches and len(matches) >= 2:
            for match in matches:
                key = match[0].strip()
                value_str = re.sub(r'[^\d.]', '', match[1])
                try:
                    value = float(value_str) if value_str else None
                    if value is not None and value > 0 and not _is_garbled_key(key):
                        rows.append({'name': key, 'value': value})
                except (ValueError, TypeError):
                    continue
            if len(rows) >= 2:
                rows = _filter_outlier_rows(rows)
                if len(rows) >= 2:
                    return pd.DataFrame(rows)

    # 模式2：从上下文中提取（带关键词的数值）
    number_pattern = r'([\d,]+\.?\d*)\s*(?:元|单|%|)'
    all_numbers = re.findall(number_pattern, answer)

    if len(all_numbers) >= 3:
        keywords = ['PR', 'APP', '微信', '商品', '平台']
        for kw in keywords:
            if kw in answer:
                context_matches = re.findall(rf'({kw}[^0-9\n]*?)(?:[:：]?\s*)?({number_pattern})', answer, re.IGNORECASE)
                if context_matches and len(context_matches) >= 2:
                    for cm in context_matches:
                        try:
                            val = float(re.sub(r'[^\d.]', '', cm[1]))
                            label = cm[0].strip()
                            # 清理标签：去掉无意义的修饰词
                            label = re.sub(r'^(及|对应|的|为|是|有|共)\s*', '', label)
                            label = label[:30] if len(label) > 30 else label
                            if label and val > 0:
                                rows.append({'name': label, 'value': val})
                        except (ValueError, TypeError):
                            continue
                    if len(rows) >= 2:
                        rows = _filter_outlier_rows(rows)
                        if len(rows) >= 2:
                            return pd.DataFrame(rows)

    return None if not rows else pd.DataFrame(rows)


def _filter_outlier_rows(rows: list[dict]) -> list[dict]:
    """过滤远小于其他数值的离群小值（例如从"3个"中误抓到的"1"），避免
    图表上出现毫无意义的单像素柱和误导性的"1"标签。"""
    if len(rows) < 2:
        return rows
    values = [r['value'] for r in rows]
    max_val = max(values)
    if max_val <= 0:
        return rows
    threshold = max_val * 0.05
    return [r for r in rows if r['value'] >= threshold]


def _is_garbled_key(key: str) -> bool:
    """判断解析到的 key 是否是乱码（Java 对象引用、Python repr、driver 内部类型等），
    避免这些内容被当成图表标签或表格列名展示给用户。"""
    if not key:
        return True
    if re.search(r'[a-zA-Z_][\w.]*@[0-9a-f]{6,8}', key):
        return True
    if re.search(r'\b(?:com|org|net|io|java)\.[a-zA-Z][\w.]*', key):
        return True
    if re.search(r"<class\s+['\"]", key):
        return True
    if re.search(r"^[\[\{]|[\]\}]$", key):
        return True
    # 纯 driver 内部类型名/对象引用（区分于合法列名 orders/users/sales）
    if re.search(r'\b(?:KBObjectField|JDBC|ResultSet)\b', key):
        return True
    if re.search(r'^\d+\s*rows?(?:\s|$)', key, re.IGNORECASE):
        return True
    return False


def clean_sql_local(sql: str) -> str:
    """兼容旧调用：自动剥除 HTML 标签。"""
    return clean_sql(sql, strip_html=True)


def strip_markdown_tables(text: str) -> str:
    """剥离 markdown 表格（含表头分隔线 |---|），避免 LLM 生成的预填充表格
    （如带"待查询结果填充"占位符的表格）与系统真实数据表格重复显示。"""
    if not text:
        return text
    # 匹配以 | 开头的连续多行表格（含对齐分隔行 |---|、|:---:| 等）
    text = re.sub(
        r'(?:^|\n)[ \t]*\|[^\n]*\|(?:[ \t]*\n[ \t]*\|[-:\s|]+\|)?'
        r'(?:[ \t]*\n[ \t]*\|[^\n]*\|)*',
        '\n',
        text,
    )
    # 清理可能残留的多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# 不可读的"乱码"内容特征：Java 对象引用、Python repr 调试输出、driver 内部类型名
GARBLED_PATTERNS = [
    # Java toString 输出：com.kingbase8.util.KBObjectField@7c0a2f2b
    r'[a-zA-Z_][\w.]*@[0-9a-f]{6,8}',
    # Java/Kotlin 类的全限定名（含包路径）
    r'(?:^|\s)(?:com|org|net|io|java)\.[a-zA-Z][\w.]*(?:\s|$|[,;:|])',
    # Python repr：<class 'list'>、<sqlalchemy...>
    r"<class\s+['\"][\w.]+['\"]>",
    r"<sqlalchemy\.[\w.]+(\.[\w]+)?\s+object",
    # Python 列表/字典 repr：['orders']、{'key': 'value'}
    r"\[\s*'[^']*'\s*\]",
    r"\{\s*'[^']*'\s*:\s*'[^']*'\s*\}",
    # 内部 driver 错误/类型提示
    r"\d+\s*rows?\s*(?:affected|returned)?",
    r"KBObjectField|JDBC|ResultSet",
    # 行内"占位符"型描述
    r"待查询结果填充|查询结果填充|待补充|待填充",
]


def strip_garbled_content(text: str) -> str:
    """剥离 LLM 回答中误带的数据库对象引用、Python repr 调试输出等不可读内容。
    这些通常是 LLM 直接复制 SQL 工具 observation（如 KingbaseES JDBC 返回的
    Java 对象 toString、SQLAlchemy 内部 repr）导致的，需要在渲染前清除。"""
    if not text:
        return text

    # 1. 按行扫描：包含乱码特征词的整行直接删除
    cleaned_lines = []
    for line in text.splitlines():
        if any(re.search(pat, line) for pat in GARBLED_PATTERNS):
            continue
        cleaned_lines.append(line)
    text = '\n'.join(cleaned_lines)

    # 2. 清理行内的乱码片段（保留行，去掉乱码）
    for pat in GARBLED_PATTERNS:
        text = re.sub(pat, '', text)

    # 3. 清理可能残留的多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def clean_sqlalchemy_result(result: str) -> str:
    """将 SQLAlchemy 返回的字符串结果中的特殊类型（Decimal、datetime、UUID 等）
    转换为 ast.literal_eval 可解析的字面量。

    SQLAlchemy 通过 str()/repr() 序列化结果时，会保留类型名：
        Decimal('123.45')          -> '123.45'
        datetime.date(2025,12,31)  -> '2025-12-31'
        datetime.datetime(...)     -> '2025-12-31 10:30:00'
        datetime.time(...)         -> '10:30:00'
        UUID('...')                -> '...'
    """
    if not result:
        return result

    # Decimal('123.45') 或 Decimal("123.45") -> 字符串字面量
    result = re.sub(r"Decimal\(\s*'(.*?)'\s*\)", r"'\1'", result)
    result = re.sub(r'Decimal\(\s*"(.*?)"\s*\)', r'"\1"', result)
    # Decimal(123.45) 纯数字 -> 数字字面量
    result = re.sub(r"Decimal\(\s*([+-]?[\d.eE]+)\s*\)", r"\1", result)

    # datetime.date(YYYY, M, D) -> 'YYYY-MM-DD'
    def _fmt_date(m):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"'{y:04d}-{mo:02d}-{d:02d}'"

    result = re.sub(
        r"datetime\.date\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
        _fmt_date, result
    )

    # datetime.datetime(YYYY, M, D, h, m, s) -> 'YYYY-MM-DD HH:MM:SS'
    def _fmt_datetime(m):
        nums = [int(g) if g else 0 for g in m.groups()]
        while len(nums) < 6:
            nums.append(0)
        return f"'{nums[0]:04d}-{nums[1]:02d}-{nums[2]:02d} {nums[3]:02d}:{nums[4]:02d}:{nums[5]:02d}'"

    result = re.sub(
        r"datetime\.datetime\(\s*(\d+)?\s*(?:,\s*(\d+))?\s*(?:,\s*(\d+))?"
        r"(?:\s*,\s*(\d+))?(?:\s*,\s*(\d+))?(?:\s*,\s*(\d+))?\s*\)",
        _fmt_datetime, result
    )

    # datetime.time(h, m, s) -> 'HH:MM:SS'
    def _fmt_time(m):
        nums = [int(g) if g else 0 for g in m.groups() if g is not None]
        while len(nums) < 3:
            nums.append(0)
        return f"'{nums[0]:02d}:{nums[1]:02d}:{nums[2]:02d}'"

    result = re.sub(
        r"datetime\.time\(\s*(\d+)?\s*(?:,\s*(\d+))?\s*(?:,\s*(\d+))?\s*\)",
        _fmt_time, result
    )

    # UUID('...') / UUID("...") -> '...'
    result = re.sub(r"UUID\(\s*'([^']*)'\s*\)", r"'\1'", result)
    result = re.sub(r'UUID\(\s*"([^"]*)"\s*\)', r'"\1"', result)

    return result


def extract_column_names(sql: str) -> list[str]:
    sql_upper = sql.upper().strip()
    select_match = re.search(r'SELECT\s+(.*?)\s+FROM', sql_upper, re.DOTALL)
    if not select_match:
        return []
    
    select_clause = select_match.group(1)
    columns = []
    
    parts = re.split(r',\s*(?![^(]*\))', select_clause)
    for part in parts:
        part = part.strip()
        as_match = re.search(r'\bAS\s+(\w+)', part, re.IGNORECASE)
        if as_match:
            columns.append(as_match.group(1))
        else:
            col_match = re.search(r'(\w+)\s*$', part)
            if col_match:
                columns.append(col_match.group(1))
    
    return columns if columns else None


def _convert_value(val):
    """将 SQLAlchemy 返回的原始值转换为 DataFrame 友好的 Python 原生类型。
    处理 datetime/Decimal/bytes/UUID 等特殊类型，避免后续处理时类型不一致。"""
    if val is None:
        return None
    if isinstance(val, datetime.datetime):
        return val.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(val, datetime.date):
        return val.strftime('%Y-%m-%d')
    if isinstance(val, datetime.time):
        return val.strftime('%H:%M:%S')
    if isinstance(val, datetime.timedelta):
        return val.total_seconds()
    if isinstance(val, decimal.Decimal):
        return float(val)
    if isinstance(val, bytes):
        try:
            return val.decode('utf-8')
        except UnicodeDecodeError:
            return val.hex()
    if isinstance(val, (set, frozenset)):
        return list(val)
    return val


def run_sql_query(sql: str) -> pd.DataFrame | None:
    try:
        sql = clean_sql_local(sql)

        # 路径 0（首选）：直接用 SQLAlchemy Engine 执行，绕开 langchain wrapper。
        # 不依赖 db.run(fetch="cursor") 的方言实现，对 KingbaseES/MySQL/SQLite 都能
        # 拿到原生 SQLAlchemy Result 对象，类型转换更稳定。
        engine = init_engine()
        if engine is not None:
            try:
                with engine.connect() as conn:
                    raw_result = conn.execute(text(sql))
                    cols = list(raw_result.keys())
                    raw_rows = raw_result.fetchall()
                if not raw_rows:
                    return pd.DataFrame(columns=cols)
                converted = [
                    tuple(_convert_value(v) for v in row) for row in raw_rows
                ]
                return pd.DataFrame(converted, columns=cols)
            except Exception as e:
                st.warning(f"⚠️ 引擎直连执行失败，回退到 langchain：{e}")

        # 路径 1：langchain SQLDatabase.run(fetch="cursor")
        db = init_db()
        if db is None:
            return None

        result = None
        try:
            result = db.run(sql, fetch="cursor")
        except (TypeError, ValueError, NotImplementedError):
            result = db.run(sql)

        if hasattr(result, "keys") and hasattr(result, "fetchall"):
            try:
                cols = list(result.keys())
                raw_rows = result.fetchall()
            except Exception as e:
                st.warning(f"读取数据库结果失败：{e}")
                return None

            if not raw_rows:
                return pd.DataFrame(columns=cols)

            converted = [
                tuple(_convert_value(v) for v in row) for row in raw_rows
            ]
            return pd.DataFrame(converted, columns=cols)

        # 路径 2：字符串结果（fallback，旧版 langchain）
        if isinstance(result, str):
            try:
                rows = json.loads(result)
                return pd.DataFrame(rows)
            except json.JSONDecodeError:
                try:
                    result_cleaned = clean_sqlalchemy_result(result)
                    data = ast.literal_eval(result_cleaned)
                    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], tuple):
                        cols = extract_column_names(sql)
                        if cols and len(cols) == len(data[0]):
                            return pd.DataFrame(data, columns=cols)
                        return pd.DataFrame(data)
                    if isinstance(data, list):
                        return pd.DataFrame(data)
                    return None
                except Exception as e:
                    st.warning(f"解析数据库结果失败：{e}")
                    return None

        # 路径 3：已经是 list[dict] / list[tuple] / (cols, data) / 标量
        if isinstance(result, list):
            if len(result) == 0:
                return pd.DataFrame()
            if isinstance(result[0], dict):
                return pd.DataFrame(result)
            if isinstance(result[0], tuple):
                cols = extract_column_names(sql)
                if cols and len(cols) == len(result[0]):
                    return pd.DataFrame(result, columns=cols)
                return pd.DataFrame(result)
            return pd.DataFrame(result)

        if isinstance(result, tuple) and len(result) == 2:
            cols, data = result
            return pd.DataFrame(data, columns=cols)

        if isinstance(result, (int, float, str)):
            cols = extract_column_names(sql)
            if not cols:
                cols = ['value']
            return pd.DataFrame([{cols[0]: result}])

        return None

    except Exception as e:
        return None


def show_step_progress(steps_done: int):
    steps = [
        ("🔍 解析意图", 1),
        ("📊 生成 SQL", 2),
        ("✅ 查库完成", 3),
        ("🤖 总结结论", 4),
    ]
    html_parts = ['<div class="step-indicator">']
    for label, step_num in steps:
        if step_num < steps_done:
            css_class = "step-done"
        elif step_num == steps_done:
            css_class = "step-active"
        else:
            css_class = "step-pending"
        html_parts.append(f'<span class="step-item {css_class}">{label}</span>')
        if step_num < 4:
            html_parts.append('<span class="step-pending">→</span>')
    html_parts.append('</div>')
    return ''.join(html_parts)


def render_sql_block(sql: str, query_time: float | None = None):
    escaped_sql = sql.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    copy_id = f"sql_{hashlib.md5(sql.encode()).hexdigest()[:8]}"
    time_str = f'<div class="query-time">⏱️ 查询耗时：{query_time:.2f} 秒</div>' if query_time else ''

    st.markdown(f"""
    <div class="sql-block">
        <button class="sql-copy-btn" onclick="
            navigator.clipboard.writeText(document.getElementById('{copy_id}').textContent);
            this.textContent='✅ 已复制'; setTimeout(()=>this.textContent='📋 复制 SQL', 1500);
        ">📋 复制 SQL</button>
        <pre id="{copy_id}" style="margin:0;white-space:pre-wrap;word-break:break-all;color:#A5B4FC;">{escaped_sql}</pre>
    </div>
    {time_str}
    """, unsafe_allow_html=True)


def render_answer_with_highlights(answer: str):
    # 1. 剥离 LLM 回答中的乱码内容（Java 对象引用、Python repr 调试输出等）
    answer = strip_garbled_content(answer)

    # 2. 剥离 LLM 生成的预填充 markdown 表格（避免与系统真实数据表格重复显示）
    answer = strip_markdown_tables(answer)

    # 3. 保护 markdown 代码块（```...```），避免数字高亮污染 SQL/代码
    # 注意：占位符不能包含数字或百分号，否则会被下面的高亮正则误匹配
    placeholders: dict[str, str] = {}
    _idx = 0

    def _protect(match):
        nonlocal _idx
        # 26 进制字母编码 (A, B, ..., Z, AA, AB, ...) 避免数字
        n, chars = _idx, []
        _idx += 1
        while True:
            chars.append(chr(ord('A') + n % 26))
            n = n // 26 - 1
            if n < 0:
                break
        idx_str = ''.join(reversed(chars))
        key = f"\x00CODEBLOCKSLOT{idx_str}ENDSLOT\x00"
        placeholders[key] = match.group(0)
        return key

    answer = re.sub(r'```[\s\S]*?```', _protect, answer)

    # 4. 对非代码部分应用高亮
    answer = re.sub(r'(\d+\.?\d*%?)', r'<span class="highlight-num">\1</span>', answer)
    answer = re.sub(r'【⚠️ 异常预警】(.*?)(?=\n|$)', r'<div class="warning-box">⚠️ 异常预警\1</div>', answer)

    # 5. 还原代码块
    for key, original in placeholders.items():
        answer = answer.replace(key, original)

    st.markdown(answer, unsafe_allow_html=True)


db = init_db()
retriever, rag_status = init_retriever()
agent = init_agent(db, retriever) if db else None

with st.sidebar:
    st.divider()
    with st.expander("📚 RAG 知识库", expanded=False):
        if rag_status.get("ok"):
            st.success("✅ RAG 已启用")
            st.metric("向量库", f"{rag_status['count']} chunks", label_visibility="visible")
            if retriever:
                stats = retriever.get_stats()
                k1, k2 = st.columns(2)
                k1.metric("命中率", f"{stats['hit_rate_pct']:.0f}%")
                k2.metric("平均延迟", f"{stats['avg_latency_ms']:.0f}ms")
                st.caption(f"缓存: {stats['cache_size']} 条")
        else:
            st.error(f"❌ RAG 未启用: {rag_status.get('error', '未知错误')[:80]}")
            st.caption("将降级为纯 SQL 查询模式")

    st.header("📜 查询历史")
    if st.session_state.query_history:
        for i, item in enumerate(reversed(st.session_state.query_history[-5:])):
            display_q = item["question"][:25] + "..." if len(item["question"]) > 25 else item["question"]
            if st.button(f"🔄 {display_q}", key=f"hist_{i}", use_container_width=True):
                st.session_state.pending_question = item["question"]
    else:
        st.caption("暂无查询历史")

    st.divider()
    st.header("💡 示例问题")

    st.markdown("**📊 销售分析**")
    sales_examples = [
        "销售额最高的 3 个商品编号和金额",
        "APP 和微信公众号，谁的订单量更多？",
    ]
    for ex in sales_examples:
        if st.button(ex, key=ex, use_container_width=True):
            st.session_state.pending_question = ex

    st.markdown("**⏰ 时间分析**")
    time_examples = [
        "最近 7 天每天的销售额是多少？",
        "哪个时间段的订单量最多？",
    ]
    for ex in time_examples:
        if st.button(ex, key=ex, use_container_width=True):
            st.session_state.pending_question = ex

    st.markdown("**👥 用户分析**")
    user_examples = [
        "用户平均消费频次是多少？",
        "各平台的退款率分别是多少？",
    ]
    for ex in user_examples:
        if st.button(ex, key=ex, use_container_width=True):
            st.session_state.pending_question = ex

    st.divider()
    if st.button("🗑️ 清空对话", use_container_width=True):
        st.session_state.messages = [
            {"role": "assistant", "content": "对话已清空，继续提问吧！",
             "chart_data": None, "chart_title": "", "csv_data": None, "sql": None, "query_time": None,
             "rag_sources": []}
        ]
        st.session_state.query_cache = {}
        st.rerun()

    st.divider()
    st.header("🎯 智能推荐")
    
    RECOMMENDATIONS = {
        '退款': ['哪个平台退款金额最多？', '退款率最高的时间段是？', '哪些商品退款最多？'],
        '销售': ['各平台销售额占比', '最近7天销售额趋势', 'TOP10热销商品'],
        '订单': ['哪个时间段订单量最多？', 'APP和微信订单量对比', '日均订单量是多少'],
        '用户': ['用户复购率是多少？', '客单价分布情况', '高价值用户特征'],
        '平台': ['各平台的转化率对比', '哪个渠道用户增长最快？', '平台留存率分析'],
        '商品': ['销量最高的TOP5商品', '库存周转快的商品', '新品表现如何'],
        '时间': ['周末和工作日订单对比', '节假日销售高峰', '月度销售趋势'],
    }
    
    last_questions = [h.get("question", "") for h in st.session_state.query_history[-3:]]
    recommended = set()
    
    for q in last_questions:
        for keyword, recs in RECOMMENDATIONS.items():
            if keyword in q:
                for rec in recs[:2]:
                    if rec not in [h.get("question", "") for h in st.session_state.query_history]:
                        recommended.add(rec)
    
    if recommended:
        for rec in list(recommended)[:3]:
            if st.button(f"💡 {rec}", key=f"rec_{rec[:20]}", use_container_width=True):
                st.session_state.pending_question = rec
    else:
        st.caption("查询后显示相关推荐")

    st.divider()
    db_type = "MySQL" if USE_MYSQL else "SQLite"
    db_label = "MySQL (ai_commerce_intelligence_platform)" if USE_MYSQL else "SQLite (本地)"
    st.caption(f"🗄️ 数据库：{db_label}")
    st.caption(f"🧠 模型：{MODEL_NAME}")

for msg_idx, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant" and msg.get("question"):
            col1, col2, col3 = st.columns([1, 1, 3])
            with col1:
                thumbs_up = st.button("👍", key=f"up_{msg_idx}")
            with col2:
                thumbs_down = st.button("👎", key=f"down_{msg_idx}")
            feedback_key = f"fb_{msg_idx}"
            if "feedbacks" not in st.session_state:
                st.session_state.feedbacks = {}
            # 落盘：仅在本次按钮被点击时记录（避免重复触发）
            if thumbs_up:
                st.session_state.feedbacks[feedback_key] = "👍 有帮助"
                record_feedback(
                    question=msg["question"],
                    answer=msg.get("content", ""),
                    rating="up",
                    rag_sources=msg.get("rag_sources", []),
                )
                st.success("感谢反馈！")
            elif thumbs_down:
                st.session_state.feedbacks[feedback_key] = "👎 需改进"
                record_feedback(
                    question=msg["question"],
                    answer=msg.get("content", ""),
                    rating="down",
                    rag_sources=msg.get("rag_sources", []),
                )
                st.info("我们会持续优化，谢谢反馈！")
                if st.button("🔄 重新生成", key=f"regen_{msg_idx}", use_container_width=True):
                    st.session_state.pending_question = msg["question"]
                    if f"msg_{msg_idx}" in st.session_state.messages:
                        st.session_state.messages.pop(msg_idx)
                    st.rerun()
            with col2:
                feedback_key = f"feedback_{msg_idx}"
                if "feedbacks" not in st.session_state:
                    st.session_state.feedbacks = {}
                thumbs_up = st.button("👍", key=f"up_{msg_idx}")
                thumbs_down = st.button("👎", key=f"down_{msg_idx}")
                if thumbs_up:
                    st.session_state.feedbacks[feedback_key] = "👍 有帮助"
                    st.success("感谢反馈！")
                elif thumbs_down:
                    st.session_state.feedbacks[feedback_key] = "👎 需改进"
                    st.info("我们会持续优化，谢谢反馈！")
        
        render_answer_with_highlights(msg["content"])

        # 显示本次回答引用的业务知识（参考来源）
        if msg.get("rag_sources"):
            with st.expander(f"📚 参考知识 ({len(msg['rag_sources'])} 条)", expanded=False):
                for src in msg["rag_sources"]:
                    st.markdown(
                        f"**[{src['rank']}] {src['filename']}** > {src['section']} "
                        f"`相关度 {src['score']:.2f}`"
                    )
                    st.caption(src["preview"])

        if msg.get("csv_data"):
            try:
                csv_df = pd.DataFrame(msg["csv_data"])
                st.dataframe(csv_df, use_container_width=True, hide_index=True)
            except Exception:
                pass
        if msg.get("chart_data"):
            try:
                chart_df = pd.DataFrame(msg["chart_data"])
                if len(chart_df) > 0 and len(chart_df.columns) >= 2:
                    fig = create_chart(chart_df, msg.get("chart_title", ""), msg.get("question", ""))
                    if fig:
                        st.plotly_chart(fig, width='stretch', key=f"msg_chart_{msg_idx}", config={
                            'displaylogo': False,
                            'modeBarButtonsToAdd': ['downloadPNG', 'zoomIn', 'zoomOut', 'fullscreen'],
                        })
            except Exception as e:
                st.caption(f"⚠️ 图表加载失败: {str(e)[:50]}")
        if msg.get("csv_data"):
            try:
                csv_df = pd.DataFrame(msg["csv_data"])
                st.download_button(
                    "📥 导出 CSV",
                    csv_df.to_csv(index=False).encode("utf-8-sig"),
                    file_name="query_result.csv",
                    mime="text/csv",
                    key=f"dl_{hashlib.md5(str(msg['content'][:80]).encode()).hexdigest()}",
                )
            except Exception:
                pass
        if msg.get("sql"):
            render_sql_block(msg["sql"], msg.get("query_time"))

prompt = st.chat_input("输入你的业务问题...")

if hasattr(st.session_state, "pending_question"):
    prompt = st.session_state.pending_question
    del st.session_state.pending_question

if prompt:
    if not agent:
        st.error("请先检查 .env 中的 API Key 和数据库密码是否正确。")
        st.stop()

    if is_sensitive_query(prompt):
        st.session_state.messages.append({"role": "user", "content": prompt,
                                          "chart_data": None, "chart_title": "", "csv_data": None,
                                          "sql": None, "query_time": None})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            st.warning(SENSITIVE_RESPONSE)
        st.session_state.messages.append({"role": "assistant", "content": SENSITIVE_RESPONSE,
                                          "chart_data": None, "chart_title": "", "csv_data": None,
                                          "sql": None, "query_time": None})
        st.stop()

    cache_key = get_cache_key(prompt)
    if cache_key in st.session_state.query_cache:
        cached = st.session_state.query_cache[cache_key]
        st.session_state.messages.append({"role": "user", "content": prompt,
                                          "chart_data": None, "chart_title": "", "csv_data": None,
                                          "sql": None, "query_time": None})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            st.info("⚡ 从缓存读取")
            render_answer_with_highlights(cached["answer"])
            if cached.get("csv_data"):
                try:
                    csv_df = pd.DataFrame(cached["csv_data"])
                    st.dataframe(csv_df, use_container_width=True, hide_index=True)
                except Exception:
                    pass
            if cached.get("chart_data"):
                try:
                    chart_df = pd.DataFrame(cached["chart_data"])
                    if len(chart_df) > 0 and len(chart_df.columns) >= 2:
                        fig = create_chart(chart_df, cached.get("chart_title", ""), prompt)
                        if fig:
                            st.plotly_chart(fig, width='stretch', key=f"cache_chart_{cache_key}", config={
                                'displaylogo': False,
                                'modeBarButtonsToAdd': ['downloadPNG', 'zoomIn', 'zoomOut', 'fullscreen'],
                            })
                except Exception as e:
                    st.caption(f"⚠️ 缓存图表加载失败: {str(e)[:50]}")
            if cached.get("csv_data"):
                try:
                    csv_df = pd.DataFrame(cached["csv_data"])
                    st.download_button(
                        "📥 导出 CSV",
                        csv_df.to_csv(index=False).encode("utf-8-sig"),
                        file_name="query_result.csv",
                        mime="text/csv",
                        key=f"cached_dl_{cache_key}",
                    )
                except Exception:
                    pass
            if cached.get("sql"):
                render_sql_block(cached["sql"], cached.get("query_time"))
        st.session_state.messages.append({
            "role": "assistant",
            "content": cached["answer"],
            "chart_data": cached.get("chart_data"),
            "chart_title": cached.get("chart_title"),
            "csv_data": cached.get("csv_data"),
            "sql": cached.get("sql"),
            "query_time": cached.get("query_time"),
            "question": prompt,
            "rag_sources": cached.get("rag_sources") or [],
        })
        st.stop()

    st.session_state.messages.append({"role": "user", "content": prompt,
                                      "chart_data": None, "chart_title": "", "csv_data": None,
                                      "sql": None, "query_time": None})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        step_placeholder = st.empty()
        step_placeholder.markdown(show_step_progress(1), unsafe_allow_html=True)

        time.sleep(0.3)
        step_placeholder.markdown(show_step_progress(2), unsafe_allow_html=True)

        try:
            start_time = time.time()
            response = agent.invoke({"input": prompt})
            query_time = time.time() - start_time
            answer = response.get("output", "抱歉，我暂时无法回答这个问题。")
        except Exception as e:
            error_str = str(e)
            if "Could not parse LLM output:" in error_str or "output parsing error" in error_str.lower():
                match = re.search(r'Could not parse LLM output:\s*`(.*)`', error_str, re.DOTALL)
                if match:
                    answer = match.group(1)
                else:
                    answer = re.sub(r'.*This is the error:.*?`', '', error_str, flags=re.DOTALL).strip()
                query_time = time.time() - start_time
                response = {"output": answer, "intermediate_steps": []}
            else:
                step_placeholder.empty()
                st.error(f"⚠️ 执行出错：{error_str}")
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": "执行失败，请检查数据表结构或重试。",
                    "chart_data": None, "chart_title": "", "csv_data": None,
                    "sql": None, "query_time": None,
                    "rag_sources": [],
                })
                st.stop()

        # 抽取本次回答引用的业务知识来源，供"📚 参考知识"面板展示
        rag_sources = extract_rag_sources(response)

        step_placeholder.markdown(show_step_progress(3), unsafe_allow_html=True)
        time.sleep(0.2)
        step_placeholder.markdown(show_step_progress(4), unsafe_allow_html=True)
        time.sleep(0.2)
        step_placeholder.empty()

        chart_data = None
        chart_title = prompt[:30]
        csv_data = None
        extracted_sql = None

        extracted_sql = extract_sql_from_intermediate(response)
        if not extracted_sql:
            extracted_sql = extract_sql_from_answer(answer)

        render_answer_with_highlights(answer)

        result_df = None
        full_data_query = False
        
        if extracted_sql:
            result_df = run_sql_query(extracted_sql)
            
            time_keywords = ['时间段', '小时', 'hour', '天', '日期', '趋势', '分布', '变化', '每天', '每日', '24']
            is_time_question = any(k in prompt.lower() or k in prompt for k in time_keywords)
            has_limit = re.search(r'\bLIMIT\s+\d+', extracted_sql, re.IGNORECASE)
            too_few_rows = result_df is not None and len(result_df) <= 2
            
            if is_time_question and too_few_rows and has_limit:
                cols = result_df.columns.tolist()
                time_col = cols[0] if cols else 'order_hour'
                
                base_sql = re.sub(r'\bLIMIT\s+\d+', '', extracted_sql, flags=re.IGNORECASE)
                base_sql = re.sub(r'\bORDER\s+BY\s+[\w.,`\(\)\*\s]+DESC\b', '', base_sql, flags=re.IGNORECASE)
                
                if 'GROUP BY' in base_sql.upper():
                    full_sql = f"{base_sql.strip().rstrip(';')} ORDER BY {time_col} ASC"
                else:
                    full_sql = extracted_sql
                
                if full_sql != extracted_sql:
                    full_data_query = True
                    full_df = run_sql_query(full_sql)
                    if full_df is not None and len(full_df) > len(result_df):
                        result_df = full_df
                        extracted_sql = full_sql
        
        if result_df is None or (isinstance(result_df, pd.DataFrame) and len(result_df) == 0):
            result_df = parse_data_from_answer(answer)
            if result_df is not None and len(result_df) > 0:
                st.info(f"📊 从回答文本解析数据成功，行数: {len(result_df)}")
        
        if result_df is not None and len(result_df) > 0:
            st.dataframe(result_df, use_container_width=True, hide_index=True)
            chart_data = result_df.to_dict(orient="records")
            csv_data = result_df.to_dict(orient="records")

            is_single_value = len(result_df) == 1 and len(result_df.columns) == 1
            is_avg_question = any(k in prompt.lower() or k in prompt for k in ['平均', 'avg', 'mean', 'per capita'])
            
            if is_single_value or (is_avg_question and len(result_df) == 1):
                if is_avg_question and len(result_df.columns) > 1:
                    avg_cols = [c for c in result_df.columns if 'avg' in c.lower() or 'freq' in c.lower() or 'per' in c.lower()]
                    if avg_cols:
                        target_col = avg_cols[0]
                    else:
                        target_col = result_df.columns[-1]
                else:
                    target_col = result_df.columns[0]
                
                single_val = result_df.iloc[0][target_col]
                st.metric(label="查询结果", value=f"{single_val:.2f}" if isinstance(single_val, (int, float)) else str(single_val))
                chart_data = None
            else:
                chart_type = detect_chart_type(result_df, prompt)
                
                fig = create_chart(result_df, chart_title, prompt)
                if fig:
                    st.plotly_chart(fig, width='stretch', key=f"live_chart_{cache_key}", config={
                        'displaylogo': False,
                        'modeBarButtonsToAdd': ['downloadPNG', 'zoomIn', 'zoomOut', 'fullscreen'],
                    })
            
            st.download_button(
                "📥 导出 CSV",
                result_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="query_result.csv",
                mime="text/csv",
                key=f"dl_{cache_key}",
            )
        
        elif extracted_sql:
            st.warning("⚠️ SQL 执行无结果")

        if extracted_sql:
            render_sql_block(extracted_sql, query_time)

        msg_data = {
            "role": "assistant",
            "content": answer,
            "chart_data": chart_data,
            "chart_title": chart_title,
            "csv_data": csv_data,
            "sql": extracted_sql,
            "query_time": query_time,
            "question": prompt,
            "rag_sources": rag_sources,
        }
        st.session_state.messages.append(msg_data)

        st.session_state.query_cache[cache_key] = {
            "answer": answer,
            "chart_data": chart_data,
            "chart_title": chart_title,
            "csv_data": csv_data,
            "sql": extracted_sql,
            "query_time": query_time,
            "rag_sources": rag_sources,
        }

        st.session_state.query_history.append({
            "question": prompt,
            "answer": answer[:100],
        })
