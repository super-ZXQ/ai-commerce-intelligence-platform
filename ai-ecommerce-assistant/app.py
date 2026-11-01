import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# 复用 backend 的共享工具（clean_sql），保证两处实现一致
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
# 业务知识来源抽取（无 streamlit 依赖，便于单元测试）
# 注意：rag.* 子模块的顶层 import 会立刻拉起 BGE + Chroma（embedding + 持久化客户端），
# 严重拖慢 Streamlit 冷启动。RAG 仅在知识类问题处理路径才需要，
# 因此把 from rag import metrics 挪到使用处（条件块内）——首次启用事件落盘时才付代价。

from assistant_charts import create_chart
from assistant_text_utils import (
    _convert_value,
    parse_data_from_answer,
    strip_garbled_content,
    strip_markdown_tables,
)

from agent_core import AgentRuntime
from agent_core.db_schema import describe_table
from agent_core.model_adapter import OpenAIModelAdapter
from agent_core.models import AgentSource
from agent_core.session import MemoryConversationStore
from agent_core.sql_safety import dialect_from_url
from backend.utils.sql_guard import (
    ensure_read_only_sql,
    guard_read_only_engine,
)
from backend.utils.text_cleaner import clean_sql, sanitize_error

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

logger = logging.getLogger(__name__)

# 用户反馈落盘路径（jsonl，append-only）
FEEDBACK_LOG_PATH = os.environ.get(
    "RAG_FEEDBACK_PATH",
    str(Path(__file__).resolve().parent / "eval" / "feedback.jsonl"),
)

# 启用 RAG 事件 JSONL 落盘（默认关闭磁盘 IO，避免拖慢；可由环境变量开启）
if os.environ.get("RAG_EVENTS_LOG", "0") == "1":
    from rag import metrics as rag_metrics  # 仅在真正启用时付一次导入代价
    rag_metrics.enable_event_file_logging()


def record_feedback(question: str, answer: str, rating: str,
                    rag_sources: list[dict] | None = None) -> bool:
    """把用户赞成/反对反馈追加到 jsonl 文件，供离线分析检索质量。

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

LLM_MODE = os.getenv("LLM_MODE", "managed").strip().lower()
if LLM_MODE not in {"managed", "byok"}:
    logger.warning("未知 LLM_MODE=%s，回落到 managed", LLM_MODE)
    LLM_MODE = "managed"
ALLOW_BYOK = os.getenv("ALLOW_BYOK", "false").strip().lower() == "true"
SERVER_API_KEY = os.getenv("LLM_API_KEY", "").strip()
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
MODEL_NAME = os.getenv("LLM_MODEL", "deepseek-chat")
PROVIDER_ALLOWLIST = tuple(
    item.strip().lower().rstrip(".")
    for item in os.getenv("LLM_PROVIDER_ALLOWLIST", "api.deepseek.com").split(",")
    if item.strip()
)

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
- 数据起止日期仅用于解释“最近”问题；用户未指定时间时，禁止添加 order_time/order_date 条件或全年过滤
- 订单量必须使用 COUNT(DISTINCT order_id)，禁止使用 COUNT(*)
- 除非用户明确要求排除退款，否则销售额、订单量、用户数等指标包含退款订单
- 用户明确指定月份或日期时必须严格使用该范围，不得扩大为全年
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
5. 退款相关使用 is_refund = '是' 表示已退款
6. SQL 结果较大时使用 LIMIT 限制
7. 先给出数据结论，再附上 SQL 语句
8. 用中文回答
9. 仅回答电商数据相关问题
10. 如果发现某指标明显异常（如某渠道退款率远超平均值），请在回答末尾添加【异常预警】段落，给出业务建议"""

SENSITIVE_PATTERNS = [
    r"密码", r"手机号", r"身份证", r"地址.*具体", r"订单明细.*用户名",
    r"个人.*信息", r"隐私", r"password", r"phone.*number",
]

SENSITIVE_RESPONSE = "该数据已脱敏，仅支持聚合查询，无法提供用户个人隐私数据。"


def is_sensitive_query(query: str) -> bool:
    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, query, re.IGNORECASE):
            return True
    return False


# 会话内查询缓存：TTL 防止长会话后数据/配置已变仍命中旧答案；
# 容量上限防止消息携带全量 records 导致 session 无界膨胀。
QUERY_CACHE_TTL_SECONDS = 1800
QUERY_CACHE_MAX_ENTRIES = 50


def get_cache_key(question: str, fingerprint: str = "") -> str:
    """缓存 key 必须绑定模型指纹（key:base_url:model 的哈希），
    否则换模型/API Key 后同一问题会直接命中旧模型的缓存答案。"""
    return hashlib.md5(f"{fingerprint}:{question}".encode()).hexdigest()


def _cache_lookup(cache: dict, key: str) -> dict | None:
    """带 TTL 的缓存读取；过期条目即时清除。"""
    entry = cache.get(key)
    if entry is None:
        return None
    if time.time() - entry["ts"] > QUERY_CACHE_TTL_SECONDS:
        cache.pop(key, None)
        return None
    return entry["data"]


def _cache_store(cache: dict, key: str, data: dict) -> None:
    """写入缓存；超出上限时按写入顺序淘汰最旧条目。"""
    while len(cache) >= QUERY_CACHE_MAX_ENTRIES:
        cache.pop(next(iter(cache)))
    cache[key] = {"ts": time.time(), "data": data}


st.set_page_config(page_title="AI Commerce Intelligence Platform", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Noto+Sans+SC:wght@400;500;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

    :root {
        --brand: #1565C0;
        --teal: #14B8A6;
        --purple: #8B5CF6;
        --orange: #F97316;
        --green: #22C55E;
        --bg: #F6F8FC;
        --surface: #FFFFFF;
        --surface-2: #F8FAFC;
        --sidebar: #EEF2F7;
        --line: #E2E8F0;
        --line-strong: #CBD5E1;
        --ink: #0F172A;
        --muted: #64748B;
        --sans: 'Manrope', 'Noto Sans SC', -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
        --mono: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace;
    }

    html, body, [class*="css"] { font-family: var(--sans); }
    .stApp { background: var(--bg); color: var(--ink); }
    .block-container { max-width: 1380px; padding-top: 2rem; padding-bottom: 7rem; }
    [data-testid="stHeader"] { background: var(--bg); border-bottom: 1px solid rgba(226,232,240,.7); }
    [data-testid="stSidebar"] {
        min-width: 300px; max-width: 340px; background: var(--sidebar); color: var(--ink);
        border-right: 1px solid var(--line);
    }
    [data-testid="stSidebar"] .stMarkdown,
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] label { color: var(--ink); }
    [data-testid="stChatMessage"] {
        background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
        padding: .8rem 1rem; box-shadow: none;
    }
    [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] { line-height: 1.68; }
    [data-testid="stChatInput"] {
        border: 1px solid var(--line-strong); border-radius: 14px; background: var(--surface);
        box-shadow: none;
    }
    [data-testid="stChatInput"]:focus-within {
        border-color: var(--brand); box-shadow: 0 0 0 2px rgba(21,101,192,.14);
    }
    h1, h2, h3 { color: var(--ink); letter-spacing: -.02em; font-family: var(--sans); }
    h1 { font-size: clamp(2rem, 3vw, 2.6rem); font-weight: 800; }
    h2, h3 { font-weight: 750; }
    .stCaption, [data-testid="stCaptionContainer"] { color: var(--muted); }
    .stButton > button, [data-testid="stFormSubmitButton"] > button, [data-testid="stDownloadButton"] > button {
        border-radius: 10px; border: 1px solid var(--line-strong); background: var(--surface);
        color: var(--ink); font-weight: 650; box-shadow: none; transition: background .15s, border-color .15s;
    }
    .stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover,
    [data-testid="stDownloadButton"] > button:hover { background: var(--surface-2); border-color: #94A3B8; }
    .stButton > button:focus-visible, [data-testid="stFormSubmitButton"] > button:focus-visible,
    [data-testid="stDownloadButton"] > button:focus-visible, [data-testid="stChatInput"] textarea:focus-visible {
        outline: 2px solid rgba(21,101,192,.34); outline-offset: 2px;
    }
    [data-testid="stExpander"] {
        background: var(--surface); border: 1px solid var(--line); border-radius: 10px; box-shadow: none;
    }
    [data-testid="stAlert"] { border-radius: 10px; box-shadow: none; }
    [data-testid="stMetric"] {
        background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
        padding: 12px 14px; box-shadow: none;
    }
    [data-testid="stMetricValue"] { font-family: var(--mono); font-variant-numeric: tabular-nums; }
    [data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }

    .status-strip {
        display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin: 14px 0 18px;
    }
    .status-card {
        min-width: 0; background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
        padding: 12px 14px;
    }
    .status-label {
        display: flex; align-items: center; gap: 7px; color: var(--muted); font-family: var(--mono);
        font-size: 10.5px; letter-spacing: 1px; text-transform: uppercase;
    }
    .status-dot { width: 7px; height: 7px; border-radius: 2px; flex: none; }
    .status-ok .status-dot { background: var(--green); }
    .status-warn .status-dot { background: var(--orange); }
    .status-error .status-dot { background: #DC2626; }
    .status-value {
        margin-top: 6px; color: var(--ink); font-weight: 750; font-size: 14px;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .status-note { margin-top: 3px; color: var(--muted); font-size: 12px; line-height: 1.45; }

    .setup-panel {
        background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
        padding: 18px 20px; margin: 12px 0 18px;
    }
    .setup-title { color: var(--ink); font-size: 18px; font-weight: 800; letter-spacing: -.02em; }
    .setup-body { color: var(--muted); font-size: 14px; line-height: 1.7; margin-top: 7px; }
    .setup-actions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 14px; }
    .setup-action {
        min-width: 0; background: var(--surface-2); border: 1px solid var(--line); border-radius: 10px;
        padding: 11px 12px;
    }
    .setup-step { color: var(--brand); font-family: var(--mono); font-size: 11px; font-weight: 700; }
    .setup-action strong { display: block; color: var(--ink); font-size: 13px; margin-top: 4px; }
    .setup-action span:last-child { display: block; color: var(--muted); font-size: 12px; line-height: 1.45; margin-top: 3px; }

    .step-indicator {
        display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
        padding: 11px 13px; border-radius: 10px;
        background: var(--surface-2); border: 1px solid var(--line); margin-bottom: 12px;
    }
    .step-item { font-size: 13px; font-family: var(--mono); font-variant-numeric: tabular-nums; }
    .step-done { color: #15803D; }
    .step-active { color: var(--brand); font-weight: 700; }
    .step-error { color: #DC2626; font-weight: 700; }
    .step-pending { color: #94A3B8; }
    .highlight-num { color: var(--brand); font-weight: 750; font-variant-numeric: tabular-nums; }
    .warning-box {
        background: rgba(249,115,22,0.08); border: 1px solid rgba(249,115,22,0.22);
        color: #9A3412; padding: 10px 13px; border-radius: 8px; margin: 8px 0;
    }
    .sql-block {
        position: relative; background: #0F172A; border: 1px solid #1E293B; border-radius: 10px;
        padding: 14px; margin: 10px 0 6px; font-family: var(--mono);
        font-size: 13px; overflow-x: auto;
    }
    .sql-copy-btn {
        position: absolute; top: 8px; right: 8px;
        background: rgba(20,184,166,0.14); border: 1px solid rgba(20,184,166,0.38);
        color: #CCFBF1; padding: 3px 9px; border-radius: 6px;
        cursor: pointer; font-size: 11px; font-family: var(--mono);
    }
    .sql-copy-btn:hover { background: rgba(20,184,166,0.24); }
    .query-time {
        color: var(--muted); font-size: 12px; margin-top: 4px;
        font-family: var(--mono); font-variant-numeric: tabular-nums;
    }
    .history-item {
        padding: 6px 10px; border-radius: 6px; margin-bottom: 4px;
        background: rgba(21,101,192,0.07); cursor: pointer;
        transition: background 0.2s; font-size: 13px;
    }
    .history-item:hover { background: rgba(21,101,192,0.13); }
    ::selection { background: rgba(21,101,192,.16); }
    * { scrollbar-width: thin; scrollbar-color: #CBD5E1 transparent; }
    *::-webkit-scrollbar { width: 10px; height: 10px; }
    *::-webkit-scrollbar-thumb { background: #CBD5E1; border-radius: 999px; border: 3px solid transparent; background-clip: content-box; }
    *::-webkit-scrollbar-track { background: transparent; }

    @media (max-width: 760px) {
        .block-container { padding-left: 1rem; padding-right: 1rem; padding-top: 1.2rem; }
        .status-strip, .setup-actions { grid-template-columns: 1fr; }
        [data-testid="stMarkdownContainer"] h1 { font-size: 1.65rem !important; }
        [data-testid="stChatMessage"] { border-radius: 12px; }
    }
</style>
""", unsafe_allow_html=True)

st.title("AI 智能商业分析平台")
st.caption("基于 LangGraph + RAG 的结构化 Text-to-SQL | 安全路由 → 只读查询 → 来源与轨迹可见")

if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": "你好！我是你的数据分析助手。你可以问我：\n"
                       "- 销售额最高的 3 个商品编号和金额\n"
                       "- APP 和公众号哪个销售额高\n"
                       "- 用户复购率是多少",
            "chart_title": "", "result_data": None,
            "sql": None, "query_time": None,
        }
    ]

if "query_cache" not in st.session_state:
    st.session_state.query_cache = {}

if "query_history" not in st.session_state:
    st.session_state.query_history = []

if "model_api_key" in st.session_state:
    # 旧会话可能残留平台密钥；启动时立即清除，避免继续出现在组件默认值中。
    st.session_state.pop("model_api_key", None)
if "byok_model" not in st.session_state:
    st.session_state.byok_model = MODEL_NAME
if "byok_base_url" not in st.session_state:
    st.session_state.byok_base_url = (
        f"https://{PROVIDER_ALLOWLIST[0]}" if PROVIDER_ALLOWLIST else ""
    )
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid4())


def get_db_uri():
    if USE_MYSQL:
        return (
            f"mysql+pymysql://{quote_plus(DB_USER)}:{quote_plus(DB_PASSWORD)}"
            f"@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
        )
    db_path = os.path.join(os.path.dirname(__file__), "ecommerce.db")
    return f"sqlite:///{db_path}"


@st.cache_resource
def init_engine() -> Engine | None:
    """初始化受只读拦截器保护的 SQLAlchemy Engine。"""
    try:
        engine = create_engine(get_db_uri(), pool_pre_ping=True)
        guard_read_only_engine(engine)
        return engine
    except Exception as e:
        logger.warning("SQLAlchemy engine 初始化失败: %s", e, exc_info=True)
        st.warning(f"SQLAlchemy engine 初始化失败：{sanitize_error(e)}")
        return None


@st.cache_resource
def init_retriever():
    """初始化 RAG 检索器（懒加载，第一次调用时下载 BGE 模型 ~93MB）。

    Returns:
        (retriever, status_dict) 元组。status_dict 包含 ok / error / count。
    """
    try:
        from rag import Retriever, VectorStore, get_embeddings
        embed = get_embeddings()
        store = VectorStore(embedding=embed)
        retriever = Retriever(store, k=3, score_threshold=0.4)
        retriever.dump_stats()
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


class _LazyRetriever:
    """RAG 检索器懒加载代理。

    关键设计：模块顶层 import app.py 时不会拉起 BGE/Chromadb。
    真正的初始化只发生在首次 knowledge 类问题触发 retrieve() 调用时。
    - status：与 init_retriever 的 status_dict 同形，但 ok=False 时 count=0
    - get_stats()：未初始化时返回空 dict（避免 NoneType 报错）
    - dump_stats()：未初始化时 no-op
    - retrieve()：未初始化时返回空列表（与旧逻辑 "if _retriever is None: return []" 对齐）
    """
    def __init__(self):
        self._inner = None
        self.status = {"ok": False, "error": "not initialized yet", "count": 0}
        self._initialized = False

    def _ensure(self):
        if self._initialized:
            return
        self._initialized = True
        try:
            inner, status = init_retriever()
            self._inner = inner
        except Exception as e:
            logger.error("RAG 懒加载失败: %s", e)
            status = {"ok": False, "error": str(e), "count": 0}
            self._inner = None
        # 必须原地更新而不是重新赋值：模块层 rag_status = retriever.status 捕获的是
        # 同一 dict 引用，重新赋值后旧引用会永远停留在"未初始化"状态，
        # 导致 RAG 面板即使初始化成功也一直显示"未启用"。
        self.status.clear()
        self.status.update(status)

    def retrieve(self, query: str, k: int = 3):
        self._ensure()
        if self._inner is None:
            return []
        return self._inner.retrieve(query, k=k)

    def get_stats(self) -> dict:
        self._ensure()
        if self._inner is None:
            return {}
        return self._inner.get_stats()

    def dump_stats(self) -> None:
        self._ensure()
        if self._inner is not None:
            self._inner.dump_stats()


def _active_model_config() -> tuple[str, str, str, str] | None:
    """返回 (mode, api_key, base_url, model)；平台密钥只存在于服务端配置。"""
    if LLM_MODE == "managed":
        if not SERVER_API_KEY:
            return None
        return "managed", SERVER_API_KEY, BASE_URL, MODEL_NAME
    if not ALLOW_BYOK or not PROVIDER_ALLOWLIST:
        return None
    api_key = st.session_state.get("byok_api_key", "").strip()
    if not api_key:
        return None
    base_url = st.session_state.get("byok_base_url", "").strip()
    model_name = st.session_state.get("byok_model", MODEL_NAME).strip() or MODEL_NAME
    return "byok", api_key, base_url, model_name


def has_model_credentials() -> bool:
    return _active_model_config() is not None


def current_model_name() -> str:
    config = _active_model_config()
    return config[3] if config else "待配置"


def get_session_runtime(_engine, _retriever):
    """按当前 Streamlit 会话创建共享 Runtime；平台密钥不进入全局缓存。"""
    config = _active_model_config()
    if _engine is None or config is None:
        return None

    mode, api_key, base_url, model_name = config
    # 指纹只保留密钥哈希，session_state 中不出现平台密钥原文。
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    fingerprint = hashlib.sha256(
        f"{mode}:{key_hash}:{base_url}:{model_name}".encode()
    ).hexdigest()
    if st.session_state.get("runtime_fingerprint") == fingerprint:
        return st.session_state.get("runtime_instance")

    model_adapter = OpenAIModelAdapter(
        api_key=api_key,
        base_url=base_url,
        model=model_name,
        business_context=BUSINESS_CONTEXT,
        allowed_hosts=PROVIDER_ALLOWLIST,
    )

    async def retrieve_knowledge(query: str) -> list[AgentSource]:
        if _retriever is None:
            return []
        docs = await asyncio.to_thread(_retriever.retrieve, query, k=3)
        return [
            AgentSource(
                filename=Path(item.get("metadata", {}).get("source", "unknown")).name,
                section=item.get("metadata", {}).get("section", ""),
                doc_type=item.get("metadata", {}).get("doc_type", "markdown"),
                score=float(item.get("score", 0)),
                snippet=re.sub(r"\s+", " ", item.get("content", ""))[:240],
            )
            for item in docs
        ]

    async def load_schema() -> str:
        return await asyncio.to_thread(describe_table, _engine, "orders")

    async def execute_sql(sql: str) -> list[dict]:
        try:
            frame = await asyncio.to_thread(run_sql_query, sql)
        except SQLExecutionError as exc:
            # to_thread 的 worker 线程里 st.* 无效，脱敏原因必须在这里（脚本线程）展示；
            # 重试仍失败时会看到两条提示，属预期。重新抛出让 Runtime 走 sql_error
            # 路径（自动纠错重试一次后向用户报告失败），而不是把异常吞成空结果。
            st.warning(f"查询执行失败：{exc}")
            raise
        return frame.to_dict(orient="records")

    runtime = AgentRuntime(
        retriever=retrieve_knowledge,
        schema_loader=load_schema,
        sql_generator=model_adapter.generate_sql,
        sql_executor=execute_sql,
        answer_generator=model_adapter.astream_answer,
        analysis_planner=model_adapter.choose_refund_analysis,
        conversations=MemoryConversationStore(ttl_seconds=1800, max_sessions=1, max_turns=6),
        sql_timeout_seconds=10,
        # get_db_uri() 在无 MySQL 环境会回落到 SQLite，方言必须按实际连接串判断。
        sql_dialect=dialect_from_url(get_db_uri()),
    )
    st.session_state.runtime_instance = runtime
    st.session_state.runtime_fingerprint = fingerprint
    return runtime


def render_runtime_status(db_engine, rag_status: dict) -> None:
    """在主区域展示模型、数据库、RAG 三类可观察状态，便于无 Key/降级时快速定位。"""
    has_model = has_model_credentials()
    db_ok = db_engine is not None
    rag_count = int(rag_status.get("count") or 0)
    rag_ready = bool(rag_status.get("ok")) and rag_count > 0
    model_note = (
        "平台托管，密钥不下发浏览器"
        if LLM_MODE == "managed"
        else "BYOK 仅保存在当前会话"
    )
    items = [
        {
            "label": "MODEL",
            "state": "ok" if has_model else "warn",
            "value": current_model_name() if has_model else "待配置",
            "note": model_note if has_model else (
                "联系部署方配置平台模型" if LLM_MODE == "managed" else "在侧栏填写 API Key"
            ),
        },
        {
            "label": "DATABASE",
            "state": "ok" if db_ok else "error",
            "value": ("MySQL" if USE_MYSQL else "SQLite") if db_ok else "不可用",
            "note": "只读连接已就绪" if db_ok else "检查数据库服务与环境变量",
        },
        {
            "label": "RAG",
            "state": "ok" if rag_ready else "warn",
            "value": f"{rag_count} chunks" if rag_ready else ("知识库为空" if rag_status.get("ok") else "已降级"),
            "note": "业务知识检索可用" if rag_ready else "纯 SQL 查询模式",
        },
    ]
    cards = []
    for item in items:
        cards.append(
            f'<div class="status-card status-{item["state"]}">'
            f'<div class="status-label"><span class="status-dot"></span>{item["label"]}</div>'
            f'<div class="status-value">{html.escape(str(item["value"]))}</div>'
            f'<div class="status-note">{html.escape(item["note"])}</div>'
            '</div>'
        )
    st.markdown(f'<div class="status-strip">{"".join(cards)}</div>', unsafe_allow_html=True)


def render_setup_guide(db_engine) -> None:
    """无模型凭据或数据库不可用时，在主区域给出可恢复的下一步。"""
    has_model = has_model_credentials()
    if has_model and db_engine is not None:
        return
    if db_engine is None:
        title = "数据库连接不可用"
        body = "页面仍可查看配置与历史状态；恢复数据库连接后即可继续发起只读查询。"
    elif LLM_MODE == "managed":
        title = "平台模型尚未配置"
        body = "当前为服务端托管模式，请联系部署方配置 LLM_API_KEY；用户无需也不能在页面填写平台密钥。"
    else:
        title = "完成模型配置后开始查询"
        body = "当前为 BYOK 模式。填写部署方允许的提供方与自己的 API Key 后，助手会展示路由、检索、SQL 与结果证据。"
    st.markdown(f"""
    <div class="setup-panel">
        <div class="setup-title">{title}</div>
        <div class="setup-body">{body}</div>
        <div class="setup-actions">
            <div class="setup-action"><span class="setup-step">01</span><strong>配置模型</strong><span>{"由部署方配置服务端密钥。" if LLM_MODE == "managed" else "在侧栏选择允许的提供方并填写 API Key。"}</span></div>
            <div class="setup-action"><span class="setup-step">02</span><strong>测试连接</strong><span>先验证模型可用性，再开始查询。</span></div>
            <div class="setup-action"><span class="setup-step">03</span><strong>检查状态</strong><span>确认数据库为只读可用，RAG 失败时自动降级。</span></div>
        </div>
    </div>
    """, unsafe_allow_html=True)






class SQLExecutionError(RuntimeError):
    """只读 SQL 执行失败，携带已脱敏的原因文本。"""


def run_sql_query(sql: str) -> pd.DataFrame:
    """执行只读 SQL 并返回 DataFrame；失败时抛 SQLExecutionError（携带脱敏原因）。

    注意：本函数运行在 asyncio.to_thread 的 worker 线程里，绝不能调用 st.*——
    Streamlit 会因缺少 ScriptRunContext 静默丢弃该 UI 调用，失败原因永远到不了
    用户眼前。脱敏原因的展示由调用方 execute_sql 协程（脚本线程）负责。
    """
    try:
        # strip_html=True：模型或用户回显的 SQL 可能带着高亮 span 标签，必须剥除
        sql = clean_sql(sql, strip_html=True)
        ensure_read_only_sql(sql)
        engine = init_engine()
        if engine is None:
            raise SQLExecutionError("数据库连接不可用")
        with engine.connect() as conn:
            raw_result = conn.execute(text(sql))
            cols = list(raw_result.keys())
            raw_rows = raw_result.fetchall()
        converted = [tuple(_convert_value(value) for value in row) for row in raw_rows]
        return pd.DataFrame(converted, columns=cols)
    except Exception as exc:
        logger.warning("只读 SQL 执行失败: %s", exc, exc_info=True)
        raise SQLExecutionError(sanitize_error(exc)) from exc


# 节点名 → 进度条文案；执行轨迹面板仍显示英文节点名与耗时明细。
STEP_LABELS = {
    "input_safety": "🔒 输入安全检查",
    "load_history": "📜 加载会话",
    "route": "🔍 解析意图",
    "retrieve": "📚 检索知识",
    "load_schema": "🗂️ 读取表结构",
    "generate_sql": "📝 生成 SQL",
    "validate_sql": "🛡️ 校验 SQL",
    "execute_sql": "🗄️ 执行查询",
    "synthesize": "🤖 合成回答",
    "save_session": "💾 保存会话",
}


def render_live_steps(steps: list[dict]) -> str:
    """把 runtime.astream 实时产出的节点步骤渲染为进度条（复用 step-indicator 样式）。"""
    parts = ['<div class="step-indicator">']
    for index, step in enumerate(steps):
        label = STEP_LABELS.get(step.get("name", ""), step.get("name", "步骤"))
        if step.get("status") == "error":
            # 错误步骤红色高亮并加 ⚠️ 前缀
            label = f"⚠️ {label}"
            css_class = "step-error"
        else:
            # 最后一项视为"当前步骤"高亮，其余标记已完成。
            css_class = "step-active" if index == len(steps) - 1 else "step-done"
        parts.append(f'<span class="step-item {css_class}">{label}</span>')
    parts.append('</div>')
    return ''.join(parts)


def render_sql_block(sql: str, query_time: float | None = None):
    escaped_sql = sql.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    copy_id = f"sql_{hashlib.md5(sql.encode()).hexdigest()[:8]}"
    time_str = f'<div class="query-time">查询耗时：{query_time:.2f} 秒</div>' if query_time else ''

    st.markdown(f"""
    <div class="sql-block">
        <button class="sql-copy-btn" onclick="
            navigator.clipboard.writeText(document.getElementById('{copy_id}').textContent);
            this.textContent='已复制'; setTimeout(()=>this.textContent='复制 SQL', 1500);
        ">复制 SQL</button>
        <pre id="{copy_id}" style="margin:0;white-space:pre-wrap;word-break:break-all;color:#A5B4FC;">{escaped_sql}</pre>
    </div>
    {time_str}
    """, unsafe_allow_html=True)


def render_result_block(
    result_df: pd.DataFrame | None,
    *,
    sql: str | None,
    query_time: float | None,
    question: str,
    chart_title: str,
    key_prefix: str,
    show_metric: bool = False,
    warn_empty: bool = False,
) -> list[dict] | None:
    """渲染"数据表 + 图表/指标卡 + 下载 + SQL 块"结果区。

    历史回放、缓存命中、实时查询三条路径共用，避免同一渲染逻辑维护三份。
    show_metric：实时路径对"单值/均值类"结果展示指标卡而非图表；回放路径
    传 False 即可（存储时此类结果已置为空数据）。
    warn_empty：是否对"SQL 执行无结果"给出警示（仅实时路径）。

    Returns:
        供消息/缓存存储的 records；单值结果或无数据时为 None。
    """
    if result_df is None or len(result_df) == 0:
        if warn_empty and sql:
            st.warning("SQL 执行无结果")
        if sql:
            render_sql_block(sql, query_time)
        return None

    st.dataframe(result_df, use_container_width=True, hide_index=True)

    stored_records = result_df.to_dict(orient="records")
    is_single_value = len(result_df) == 1 and len(result_df.columns) == 1
    is_avg_question = any(k in question.lower() for k in ["平均", "avg", "mean", "per capita"])
    if show_metric and (is_single_value or (is_avg_question and len(result_df) == 1)):
        if is_avg_question and len(result_df.columns) > 1:
            avg_cols = [c for c in result_df.columns
                        if "avg" in c.lower() or "freq" in c.lower() or "per" in c.lower()]
            target_col = avg_cols[0] if avg_cols else result_df.columns[-1]
        else:
            target_col = result_df.columns[0]
        single_val = result_df.iloc[0][target_col]
        st.metric(
            label="查询结果",
            value=f"{single_val:.2f}" if isinstance(single_val, (int, float)) else str(single_val),
        )
        stored_records = None
    elif len(result_df.columns) >= 2:
        fig = create_chart(result_df, chart_title, question)
        if fig:
            st.plotly_chart(fig, width="stretch", key=f"{key_prefix}_chart", config={
                "displaylogo": False,
                "modeBarButtonsToAdd": ["downloadPNG", "zoomIn", "zoomOut", "fullscreen"],
            })

    st.download_button(
        "导出 CSV",
        result_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="query_result.csv",
        mime="text/csv",
        key=f"{key_prefix}_dl",
    )
    if sql:
        render_sql_block(sql, query_time)
    return stored_records


def _highlight_answer(answer: str) -> str:
    """把 LLM 回答转成带数字高亮的安全 HTML（纯函数，不依赖 streamlit，便于单测）。"""
    # 1. 剥离 LLM 回答中的乱码内容（Java 对象引用、Python repr 调试输出等）
    answer = strip_garbled_content(answer)

    # 2. 剥离 LLM 生成的预填充 markdown 表格（避免与系统真实数据表格重复显示）
    answer = strip_markdown_tables(answer)
    answer = html.escape(answer)

    # 3. 占位符保护：代码块（```...```）、行内代码（`...`）与 HTML 实体。
    #    实体（如撇号 &#x27;）里的数字被高亮正则包裹后会破坏实体、渲染成乱码；
    #    行内代码里的数字也不应被染色。占位符不能包含数字或百分号，
    #    否则会被下面的高亮正则误匹配。
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

    answer = re.sub(r'```[\s\S]*?```|`[^`\n]*`|&#x?[0-9a-fA-F]+;', _protect, answer)

    # 4. 对非保护部分应用高亮
    answer = re.sub(r'(\d+\.?\d*%?)', r'<span class="highlight-num">\1</span>', answer)
    answer = re.sub(r'【[^】]{0,4}异常预警】(.*?)(?=\n|$)', r'<div class="warning-box">异常预警\1</div>', answer)

    # 5. 还原占位内容
    for key, original in placeholders.items():
        answer = answer.replace(key, original)
    return answer


def render_answer_with_highlights(answer: str):
    st.markdown(_highlight_answer(answer), unsafe_allow_html=True)


def render_agent_steps(steps: list[dict]) -> None:
    if not steps:
        return
    with st.expander(f"执行轨迹 ({len(steps)} 步)", expanded=False):
        for step in steps:
            status = "DONE" if step.get("status") == "success" else "WARN"
            st.caption(
                f"{status} · {step.get('name', 'step')} · {step.get('duration_ms', 0)}ms — "
                f"{step.get('summary', '')}"
            )


db_engine = init_engine()
# RAG 检索器用懒加载代理替代 eager 调用：模块顶层 import 时不拉起
# BGE(93MB) 与 Chromadb，只有当用户真的问出 knowledge/hybrid 类问题时
# 才会触发首次初始化。首页/纯 data 问题路径零 RAG 代价。
retriever = _LazyRetriever()
# rag_status 与 retriever.status 是同一个 dict 引用；_ensure() 原地更新它，
# 所以首次 RAG 调用之后的 rerun 能读到最新状态（读取本身不触发懒加载）。
rag_status = retriever.status
runtime = get_session_runtime(db_engine, retriever)
render_runtime_status(db_engine, rag_status)
render_setup_guide(db_engine)

with st.sidebar:
    with st.expander("模型连接", expanded=not has_model_credentials()):
        if LLM_MODE == "managed":
            st.caption(
                f"模式：服务端托管\n\n"
                f"提供方：`{BASE_URL}`\n\n"
                f"模型：`{MODEL_NAME}`"
            )
            st.caption("平台密钥只保存在服务端，不会显示或写入浏览器会话。")
            if st.button("测试托管模型连接", use_container_width=True):
                if not SERVER_API_KEY:
                    st.warning("服务端未配置 LLM_API_KEY，请联系部署方")
                else:
                    try:
                        probe = OpenAIModelAdapter(
                            api_key=SERVER_API_KEY,
                            base_url=BASE_URL,
                            model=MODEL_NAME,
                            business_context=BUSINESS_CONTEXT,
                            allowed_hosts=PROVIDER_ALLOWLIST,
                        )
                        probe.test_connection()
                        st.success("连接成功，模型已响应")
                    except Exception as exc:
                        st.error(f"连接失败：{sanitize_error(exc)}")
        elif not ALLOW_BYOK:
            st.info("BYOK 未启用；请联系部署方配置平台模型。")
        else:
            with st.form("byok_form"):
                provider = st.selectbox(
                    "模型提供方",
                    PROVIDER_ALLOWLIST,
                    index=0,
                    help="只能选择部署方允许的提供方主机。",
                )
                model_name = st.text_input(
                    "模型名称",
                    value=st.session_state.get("byok_model", MODEL_NAME),
                    placeholder="deepseek-chat",
                )
                model_api_key = st.text_input(
                    "API Key",
                    value="",
                    type="password",
                    help="只保存在当前浏览器会话，不写入文件或日志。",
                )
                apply_config = st.form_submit_button("应用配置", use_container_width=True)
                test_connection = st.form_submit_button("测试连接", use_container_width=True)
                if apply_config or test_connection:
                    if not model_api_key.strip() and not st.session_state.get("byok_api_key"):
                        st.warning("请填写 API Key")
                    else:
                        if model_api_key.strip():
                            st.session_state.byok_api_key = model_api_key.strip()
                        st.session_state.byok_model = model_name.strip() or MODEL_NAME
                        st.session_state.byok_base_url = f"https://{provider}"
                        st.session_state.pop("runtime_instance", None)
                        st.session_state.pop("runtime_fingerprint", None)
                        if test_connection:
                            try:
                                probe = OpenAIModelAdapter(
                                    api_key=st.session_state.byok_api_key,
                                    base_url=st.session_state.byok_base_url,
                                    model=st.session_state.byok_model,
                                    business_context=BUSINESS_CONTEXT,
                                    allowed_hosts=PROVIDER_ALLOWLIST,
                                )
                                probe.test_connection()
                                st.success("连接成功，模型已响应")
                            except Exception as exc:
                                st.error(f"连接失败：{sanitize_error(exc)}")
                        if apply_config:
                            st.rerun()

            if st.session_state.get("byok_api_key"):
                st.success("BYOK 密钥仅保存在当前会话")
                if st.button("清除 BYOK 密钥", use_container_width=True):
                    st.session_state.pop("byok_api_key", None)
                    st.session_state.pop("runtime_instance", None)
                    st.session_state.pop("runtime_fingerprint", None)
                    st.rerun()
            else:
                st.info("选择允许的提供方并填写 API Key 后即可开始查询")

    st.divider()
    with st.expander("RAG 知识库", expanded=False):
        rag_count = int(rag_status.get("count") or 0)
        if rag_status.get("ok") and rag_count > 0:
            st.success("RAG 已启用")
            st.metric("向量库", f"{rag_count} chunks", label_visibility="visible")
            # ok=True 说明懒加载已完成，此时 get_stats() 不会再次触发重初始化
            stats = retriever.get_stats()
            k1, k2 = st.columns(2)
            k1.metric("命中率", f"{stats['hit_rate_pct']:.0f}%")
            k2.metric("平均延迟", f"{stats['avg_latency_ms']:.0f}ms")
            st.caption(f"缓存: {stats['cache_size']} 条")
        elif rag_status.get("ok"):
            st.warning("RAG 知识库为空")
            st.caption("将降级为纯 SQL 查询模式；请先构建或检查向量库。")
        else:
            st.error(f"RAG 未启用：{rag_status.get('error', '未知错误')[:80]}")
            st.caption("将降级为纯 SQL 查询模式")

    st.header("查询历史")
    if st.session_state.query_history:
        for i, item in enumerate(reversed(st.session_state.query_history[-5:])):
            display_q = item["question"][:25] + "..." if len(item["question"]) > 25 else item["question"]
            if st.button(display_q, key=f"hist_{i}", use_container_width=True):
                st.session_state.pending_question = item["question"]
    else:
        st.caption("暂无查询历史")

    st.divider()
    st.header("示例问题")

    st.markdown("**销售分析**")
    sales_examples = [
        "销售额最高的 3 个商品编号和金额",
        "APP 和微信公众号，谁的订单量更多？",
    ]
    for ex in sales_examples:
        if st.button(ex, key=ex, use_container_width=True):
            st.session_state.pending_question = ex

    st.markdown("**时间分析**")
    time_examples = [
        "最近 7 天每天的销售额是多少？",
        "哪个时间段的订单量最多？",
    ]
    for ex in time_examples:
        if st.button(ex, key=ex, use_container_width=True):
            st.session_state.pending_question = ex

    st.markdown("**用户分析**")
    user_examples = [
        "用户平均消费频次是多少？",
        "各平台的退款率分别是多少？",
    ]
    for ex in user_examples:
        if st.button(ex, key=ex, use_container_width=True):
            st.session_state.pending_question = ex

    st.divider()
    if st.button("清空对话", use_container_width=True):
        st.session_state.messages = [
            {"role": "assistant", "content": "对话已清空，继续提问吧！",
             "chart_title": "", "result_data": None, "sql": None, "query_time": None,
             "rag_sources": []}
        ]
        st.session_state.query_cache = {}
        # 轮换 thread_id：Agent 会话存储按 owner+thread_id 记忆上下文，
        # 只清 UI 消息会导致"清空"后下一问仍注入最多 6 轮旧对话。
        st.session_state.thread_id = str(uuid4())
        st.rerun()

    st.divider()
    st.header("智能推荐")
    
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
            if st.button(rec, key=f"rec_{rec[:20]}", use_container_width=True):
                st.session_state.pending_question = rec
    else:
        st.caption("查询后显示相关推荐")

    st.divider()
    db_type = "MySQL" if USE_MYSQL else "SQLite"
    db_label = "MySQL (ai_commerce_intelligence_platform)" if USE_MYSQL else "SQLite (本地)"
    st.caption(f"数据库：{db_label}")
    st.caption(f"模型：{current_model_name()}")

for msg_idx, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant" and msg.get("question"):
            col1, col2, col3 = st.columns([1, 1, 3])
            with col1:
                thumbs_up = st.button("有帮助", key=f"up_{msg_idx}")
            with col2:
                thumbs_down = st.button("需改进", key=f"down_{msg_idx}")
            # 反馈状态按问答内容哈希记录，而不是消息索引：重新生成会删除消息对，
            # 索引前移后索引 key 会把反馈错位到别的消息行上。
            feedback_key = "fb_" + hashlib.md5(
                (msg.get("question", "") + "\x00" + msg.get("content", "")).encode()
            ).hexdigest()[:12]
            if "feedbacks" not in st.session_state:
                st.session_state.feedbacks = {}
            # 落盘：仅在本次按钮被点击时记录（避免重复触发）
            if thumbs_up:
                st.session_state.feedbacks[feedback_key] = "有帮助"
                record_feedback(
                    question=msg["question"],
                    answer=msg.get("content", ""),
                    rating="up",
                    rag_sources=msg.get("rag_sources", []),
                )
                st.success("感谢反馈！")
            elif thumbs_down:
                st.session_state.feedbacks[feedback_key] = "需改进"
                record_feedback(
                    question=msg["question"],
                    answer=msg.get("content", ""),
                    rating="down",
                    rag_sources=msg.get("rag_sources", []),
                )
                st.info("我们会持续优化，谢谢反馈！")

            # 按钮点击会触发整页重跑；根据已保存的反馈状态持续显示重新生成入口。
            if st.session_state.feedbacks.get(feedback_key) == "需改进":
                if st.button("重新生成", key=f"regen_{msg_idx}", use_container_width=True):
                    st.session_state.pending_question = msg["question"]
                    # UI 删除问答对的同时，把会话存储里对应的末轮也弹掉，
                    # 否则重新生成的回答会以被删掉的旧答案为上下文（自我复读）。
                    # question 参数保证只在末轮确实是这一轮时才删（防误删其他轮次）。
                    if runtime is not None:
                        try:
                            asyncio.run(runtime.conversations.pop_last_turn(
                                "streamlit-session", st.session_state.thread_id,
                                question=msg["question"],
                            ))
                        except Exception as exc:
                            logger.warning("弹出会话末轮失败: %s", exc)
                    # 替换这一轮问答，避免重新生成后在对话记录中出现重复内容。
                    if msg_idx > 0 and st.session_state.messages[msg_idx - 1].get("role") == "user":
                        del st.session_state.messages[msg_idx - 1:msg_idx + 1]
                    else:
                        st.session_state.messages.pop(msg_idx)
                    st.rerun()

        
        render_answer_with_highlights(msg["content"])

        # 显示本次回答引用的业务知识（参考来源）
        if msg.get("rag_sources"):
            with st.expander(f"参考知识 ({len(msg['rag_sources'])} 条)", expanded=False):
                for src in msg["rag_sources"]:
                    st.markdown(
                        f"**[{src['rank']}] {src['filename']}** > {src['section']} "
                        f"`相关度 {src['score']:.2f}`"
                    )
                    st.caption(src["preview"])
        render_agent_steps(msg.get("agent_steps") or [])
        if msg.get("analysis_evidence"):
            with st.expander("多步分析数据依据", expanded=False):
                for item in msg["analysis_evidence"]:
                    st.markdown(f"**{item['label']}**")
                    st.code(item["sql"], language="sql")
                    st.dataframe(pd.DataFrame(item["rows"]), use_container_width=True)

        try:
            # result_data 为空（单值结果/无行）也走共用函数：SQL 块仍需独立展示
            result_df = pd.DataFrame(msg["result_data"]) if msg.get("result_data") else None
            render_result_block(
                result_df,
                sql=msg.get("sql"),
                query_time=msg.get("query_time"),
                question=msg.get("question", ""),
                chart_title=msg.get("chart_title", ""),
                key_prefix=f"msg_{msg_idx}",
            )
        except Exception as e:
            st.caption(f"历史结果加载失败：{str(e)[:50]}")

prompt = st.chat_input("输入你的业务问题...")

if hasattr(st.session_state, "pending_question"):
    prompt = st.session_state.pending_question
    del st.session_state.pending_question

if prompt:
    if not runtime:
        recovery = (
            "数据库连接不可用。请先恢复数据库服务或环境变量，再重新提交问题。"
            if db_engine is None
            else "模型 API Key 尚未配置。请在侧栏完成模型连接并测试通过后，再重新提交问题。"
        )
        st.session_state.messages.append({"role": "user", "content": prompt,
                                          "chart_title": "", "result_data": None,
                                          "sql": None, "query_time": None})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            st.warning(recovery)
            st.caption("恢复路径：检查 Base URL、模型名称、API Key 与数据库只读连接；RAG 失败时会自动降级为纯 SQL 查询模式。")
        st.session_state.messages.append({"role": "assistant", "content": recovery,
                                          "chart_title": "", "result_data": None,
                                          "sql": None, "query_time": None, "rag_sources": []})
        st.stop()

    if is_sensitive_query(prompt):
        st.session_state.messages.append({"role": "user", "content": prompt,
                                          "chart_title": "", "result_data": None,
                                          "sql": None, "query_time": None})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            st.warning(SENSITIVE_RESPONSE)
        st.session_state.messages.append({"role": "assistant", "content": SENSITIVE_RESPONSE,
                                          "chart_title": "", "result_data": None,
                                          "sql": None, "query_time": None})
        st.stop()

    cache_key = get_cache_key(prompt, st.session_state.get("runtime_fingerprint", ""))
    cached = _cache_lookup(st.session_state.query_cache, cache_key)
    if cached is not None:
        st.session_state.messages.append({"role": "user", "content": prompt,
                                          "chart_title": "", "result_data": None,
                                          "sql": None, "query_time": None})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            st.info("从缓存读取")
            render_answer_with_highlights(cached["answer"])
            try:
                # result_data 为空也走共用函数：SQL 块仍需展示（与历史回放一致）
                result_df = pd.DataFrame(cached["result_data"]) if cached.get("result_data") else None
                render_result_block(
                    result_df,
                    sql=cached.get("sql"),
                    query_time=cached.get("query_time"),
                    question=prompt,
                    chart_title=cached.get("chart_title", ""),
                    key_prefix=f"cache_{cache_key}",
                )
            except Exception as e:
                st.caption(f"缓存图表加载失败：{str(e)[:50]}")
            render_agent_steps(cached.get("agent_steps") or [])
            if cached.get("analysis_evidence"):
                with st.expander("多步分析数据依据", expanded=False):
                    for item in cached["analysis_evidence"]:
                        st.markdown(f"**{item['label']}**")
                        st.code(item["sql"], language="sql")
                        st.dataframe(pd.DataFrame(item["rows"]), use_container_width=True)
        st.session_state.messages.append({
            "role": "assistant",
            "content": cached["answer"],
            "chart_title": cached.get("chart_title"),
            "result_data": cached.get("result_data"),
            "sql": cached.get("sql"),
            "query_time": cached.get("query_time"),
            "question": prompt,
            "rag_sources": cached.get("rag_sources") or [],
            "agent_steps": cached.get("agent_steps") or [],
            "analysis_evidence": cached.get("analysis_evidence") or [],
        })
        st.stop()

    st.session_state.messages.append({"role": "user", "content": prompt,
                                      "chart_title": "", "result_data": None,
                                      "sql": None, "query_time": None})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        step_placeholder = st.empty()
        answer_placeholder = st.empty()
        live_steps: list[dict] = []
        answer_text: list[str] = []

        def _on_step(step: dict) -> None:
            # 节点完成即刷新实时进度；回调与 Streamlit 脚本同线程，可直接更新 UI。
            live_steps.append(step)
            step_placeholder.markdown(render_live_steps(live_steps), unsafe_allow_html=True)

        def _on_token(delta: str) -> None:
            # 答案逐 token 打字机渲染
            answer_text.append(delta)
            answer_placeholder.markdown("".join(answer_text) + "▌")

        try:
            state = asyncio.run(
                runtime.astream(
                    prompt,
                    owner="streamlit-session",
                    thread_id=st.session_state.thread_id,
                    on_step=_on_step,
                    on_token=_on_token,
                )
            )
            agent_result = state["result"]
            answer = agent_result.answer
            query_time = agent_result.usage.latency_ms / 1000 if agent_result.usage else None
            rag_sources = [
                {
                    "rank": index,
                    "filename": source.filename,
                    "section": source.section,
                    "doc_type": source.doc_type,
                    "score": source.score,
                    "preview": source.snippet,
                }
                for index, source in enumerate(agent_result.sources, 1)
            ]
            agent_steps = [
                {
                    "name": step.name,
                    "status": step.status,
                    "duration_ms": step.duration_ms,
                    "summary": step.summary,
                }
                for step in agent_result.steps
            ]
        except Exception as exc:
            step_placeholder.empty()
            answer_placeholder.empty()
            st.error(f"执行出错：{sanitize_error(exc)}")
            st.session_state.messages.append({
                "role": "assistant",
                "content": "执行失败。请检查模型 Base URL、API Key、模型名称与数据库只读连接后重试；你的问题已保留在对话中。",
                "chart_title": "", "result_data": None,
                "sql": None, "query_time": None, "rag_sources": [],
            })
            st.stop()

        # 流式渲染结束：清空占位（含打字机光标），改用高亮版最终答案。
        step_placeholder.empty()
        answer_placeholder.empty()

        chart_title = prompt[:30]
        extracted_sql = agent_result.sql

        render_answer_with_highlights(answer)
        render_agent_steps(agent_steps)
        if agent_result.analysis_evidence:
            with st.expander("多步分析数据依据", expanded=False):
                for item in agent_result.analysis_evidence:
                    st.markdown(f"**{item['label']}**")
                    st.code(item["sql"], language="sql")
                    st.dataframe(pd.DataFrame(item["rows"]), use_container_width=True)
        result_df = pd.DataFrame(agent_result.rows)

        # SQL 无结果时，不直接断言"没有数据"——从回答文本兜底解析表格数据
        if len(result_df) == 0:
            fallback_df = parse_data_from_answer(answer)
            if fallback_df is not None and len(fallback_df) > 0:
                st.info(f"从回答文本解析数据成功，行数：{len(fallback_df)}")
                result_df = fallback_df

        result_data = render_result_block(
            result_df,
            sql=extracted_sql,
            query_time=query_time,
            question=prompt,
            chart_title=chart_title,
            key_prefix=f"live_{cache_key}",
            show_metric=True,
            warn_empty=True,
        )

        msg_data = {
            "role": "assistant",
            "content": answer,
            "chart_title": chart_title,
            "result_data": result_data,
            "sql": extracted_sql,
            "query_time": query_time,
            "question": prompt,
            "rag_sources": rag_sources,
            "agent_steps": agent_steps,
            "analysis_evidence": agent_result.analysis_evidence,
        }
        st.session_state.messages.append(msg_data)

        _cache_store(st.session_state.query_cache, cache_key, {
            "answer": answer,
            "chart_title": chart_title,
            "result_data": result_data,
            "sql": extracted_sql,
            "query_time": query_time,
            "rag_sources": rag_sources,
            "agent_steps": agent_steps,
            "analysis_evidence": agent_result.analysis_evidence,
        })

        st.session_state.query_history.append({
            "question": prompt,
            "answer": answer[:100],
        })

