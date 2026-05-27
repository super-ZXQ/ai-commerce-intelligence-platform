import os
import re
import json
import ast
import time
import hashlib
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_openai import ChatOpenAI
from langchain_community.agent_toolkits import create_sql_agent, SQLDatabaseToolkit

load_dotenv()

API_KEY = os.getenv("LLM_API_KEY")
BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
MODEL_NAME = os.getenv("LLM_MODEL", "qwen-plus")

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "ecommerce_analysis")

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


st.set_page_config(page_title="AI 电商分析助手", page_icon="🤖", layout="wide",
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

st.title("🤖 AI 电商数据分析助手")
st.caption("基于 LangChain + Qwen 的 Text-to-SQL 智能查询 | 自然语言提问 → 自动生成 SQL → 数据可视化")

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
def init_agent(_db):
    llm = ChatOpenAI(
        api_key=API_KEY,
        base_url=BASE_URL,
        model=MODEL_NAME,
        temperature=0.1,
    )
    toolkit = SQLDatabaseToolkit(db=_db, llm=llm)

    prefix = f"""你是一个专业的电商数据分析助手。你可以访问一个名为 `orders` 的电商订单数据库表。

{BUSINESS_CONTEXT}

当用户提问时，你需要：
1. 理解用户意图
2. 生成正确的 SQL 查询
3. 执行查询获取数据
4. 用中文总结结论
5. 如果发现异常指标，给出业务建议

请始终用中文回答。"""

    return create_sql_agent(
        llm=llm,
        toolkit=toolkit,
        prefix=prefix,
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
        
        if len(df_plot) > 5:
            df_plot = df_plot.nlargest(5, columns=y_col).reset_index(drop=True)
        
        n_items = len(df_plot)
        bar_colors = ['#EF4444', '#F87171', '#FCA5A5', '#FECACA', '#FEF2F2']
        
        def format_y_label(val):
            val_str = str(val).strip()
            if val_str.isdigit():
                hour_val = int(val_str)
                if 0 <= hour_val <= 23:
                    return f"{hour_val}时"
                elif hour_val >= 1 and hour_val <= 31:
                    return f"{hour_val}日"
            return val_str
        
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
                        line=dict(width=0.5, color='rgba(255,255,255,0.3)'),
                    ),
                    text=y_text,
                    textposition='outside',
                    textfont=dict(size=12, color='#E4E4E7', family='monospace'),
                    hovertemplate=f'<b>{x_label}</b><br>%{{x:,.0f}}<extra></extra>',
                ))
            
            dynamic_height = 300 + max(n_items * 35, 100)
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
                        line=dict(width=0.5, color='rgba(255,255,255,0.3)'),
                    ),
                    text=y_text,
                    textposition='outside',
                    textfont=dict(size=12, color='#E4E4E7', family='monospace'),
                    hovertemplate=f'<b>{x_label}</b><br>%{{x:,.0f}}<extra></extra>',
                ))
            
            dynamic_height = 300 + max(n_items * 35, 100)
            fig.update_layout(height=dynamic_height, barmode='group')

    else:
        fig = go.Figure()

    fig.update_layout(
        margin=dict(l=80 if chart_type in ["bar_h", "bar"] else 30, 
                    r=100 if chart_type in ["bar_h", "bar"] else 30,
                    t=50, b=50),
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
                        return clean_sql(sql)
                elif isinstance(tool_input, str) and "SELECT" in tool_input.upper():
                    return clean_sql(tool_input)
            
            if isinstance(observation, str):
                sql_match = re.search(r'SELECT\s+[\s\S]+?(?:;|$)', observation, re.IGNORECASE)
                if sql_match:
                    return clean_sql(sql_match.group(0))
    
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
                return clean_sql(match.group(1))
    
    return None


def extract_sql_from_answer(answer: str) -> str | None:
    if not answer or not isinstance(answer, str):
        return None
    
    patterns = [
        r'```sql\s*(.*?)```',
        r'```(SELECT[\s\S]*?)```',
        r'(SELECT\s+[\s\S]*?;)',
        r'SELECT\s+[\w\s,\(\)\*]+\s+FROM\s+\w+[\s\S]*?(?:;|```|$)',
    ]
    for pattern in patterns:
        match = re.search(pattern, answer, re.IGNORECASE | re.DOTALL)
        if match:
            return clean_sql(match.group(1))
    return None


def parse_data_from_answer(answer: str) -> pd.DataFrame | None:
    if not answer or not isinstance(answer, str):
        return None
    
    rows = []
    
    patterns = [
        r'(?:商品编号|PR\d+|platform_type|APP|微信公众号|order_date|order_hour)[^\d]*([\w]+)\s*[：:]\s*([￥¥$]?\s*[\d,]+\.?\d*)\s*(?:元|单|%|)?',
        r'[-•]\s*(.+?)[：:]\s*([￥¥$]?\s*[\d,]+\.?\d*)',
        r'(\w+)\s*[：:]\s*([￥¥$]?\s*[\d,]+\.?\d*)\s*(?:元|单|%|)?',
        r'(APP|微信公众号)[^0-9]*(\d[\d,]*)\s*(?:单)?',
        r'(PR\d{6})[^\d]*(\d[\d,]*\.?\d*)',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, answer, re.IGNORECASE)
        if matches and len(matches) >= 2:
            for match in matches:
                key = match[0].strip()
                value_str = re.sub(r'[^\d.]', '', match[1])
                try:
                    value = float(value_str) if value_str else None
                    if value is not None and value > 0:
                        rows.append({'name': key, 'value': value})
                except (ValueError, TypeError):
                    continue
            if len(rows) >= 2:
                return pd.DataFrame(rows)
    
    if len(rows) == 1:
        return None
    
    number_pattern = r'([\d,]+\.?\d*)\s*(?:元|单|%|)'
    all_numbers = re.findall(number_pattern, answer)
    
    if len(all_numbers) >= 3:
        keywords = ['PR', 'APP', '微信', '商品', '平台']
        for kw in keywords:
            if kw in answer:
                context_matches = re.findall(rf'({kw}[^0-9\n]*?)[:：]?\s*({number_pattern})', answer, re.IGNORECASE)
                if context_matches and len(context_matches) >= 2:
                    for cm in context_matches:
                        try:
                            val = float(re.sub(r'[^\d.]', '', cm[1]))
                            rows.append({'name': cm[0].strip()[:30], 'value': val})
                        except (ValueError, TypeError):
                            continue
                    if len(rows) >= 2:
                        return pd.DataFrame(rows)
    
    return None if not rows else pd.DataFrame(rows)


def clean_sql(sql: str) -> str:
    if not sql:
        return ""
    sql = re.sub(r'```(?:sql)?\s*', '', sql)
    sql = re.sub(r'```\s*$', '', sql)
    return sql.strip()


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


def run_sql_query(sql: str) -> pd.DataFrame | None:
    try:
        db = init_db()
        if db is None:
            return None
        sql = clean_sql(sql)
        
        result = db.run(sql)
        
        if isinstance(result, str):
            try:
                rows = json.loads(result)
                df = pd.DataFrame(rows)
                return df
            except json.JSONDecodeError:
                try:
                    result_cleaned = re.sub(r"Decimal\(([^)]+)\)", r"\1", result)
                    data = ast.literal_eval(result_cleaned)
                    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], tuple):
                        cols = extract_column_names(sql)
                        if cols and len(cols) == len(data[0]):
                            df = pd.DataFrame(data, columns=cols)
                        else:
                            df = pd.DataFrame(data)
                        return df
                    elif isinstance(data, list):
                        return pd.DataFrame(data)
                    return None
                except Exception as e:
                    st.warning(f"解析数据库结果失败：{e}")
                    return None
        
        if isinstance(result, list):
            if len(result) == 0:
                return pd.DataFrame()
            if isinstance(result[0], dict):
                return pd.DataFrame(result)
            elif isinstance(result[0], tuple):
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
    answer = re.sub(r'(\d+\.?\d*%?)', r'<span class="highlight-num">\1</span>', answer)
    answer = re.sub(r'【⚠️ 异常预警】(.*?)(?=\n|$)', r'<div class="warning-box">⚠️ 异常预警\1</div>', answer)
    st.markdown(answer, unsafe_allow_html=True)


db = init_db()
agent = init_agent(db) if db else None

with st.sidebar:
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
             "chart_data": None, "chart_title": "", "csv_data": None, "sql": None, "query_time": None}
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
    db_label = "MySQL (ecommerce_analysis)" if USE_MYSQL else "SQLite (本地)"
    st.caption(f"🗄️ 数据库：{db_label}")
    st.caption(f"🧠 模型：{MODEL_NAME}")

for msg_idx, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant" and msg.get("question"):
            col1, col2, col3 = st.columns([1, 1, 3])
            with col1:
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
                        st.plotly_chart(fig, use_container_width=True, key=f"msg_chart_{msg_idx}", config={
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
                            st.plotly_chart(fig, use_container_width=True, key=f"cache_chart_{cache_key}", config={
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
                })
                st.stop()

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
                    st.plotly_chart(fig, use_container_width=True, key=f"live_chart_{cache_key}", config={
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
        }
        st.session_state.messages.append(msg_data)

        st.session_state.query_cache[cache_key] = {
            "answer": answer,
            "chart_data": chart_data,
            "chart_title": chart_title,
            "csv_data": csv_data,
            "sql": extracted_sql,
            "query_time": query_time,
        }

        st.session_state.query_history.append({
            "question": prompt,
            "answer": answer[:100],
        })
