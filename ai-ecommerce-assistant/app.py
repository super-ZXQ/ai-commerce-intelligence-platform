import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# 复用与后端一致的纯文本处理工具（不包含数据库/模型客户端）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from assistant_charts import create_chart
from assistant_text_utils import (
    parse_data_from_answer,
    strip_garbled_content,
    strip_markdown_tables,
)

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

logger = logging.getLogger(__name__)

# 用户反馈落盘路径（jsonl，append-only）
FEEDBACK_LOG_PATH = os.environ.get(
    "RAG_FEEDBACK_PATH",
    str(Path(__file__).resolve().parent / "eval" / "feedback.jsonl"),
)

# 后端是唯一执行入口：模型、SQL、RAG、额度、并发与审计都不在本进程执行。
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "http://localhost:8000").rstrip("/")
LOGIN_URL = os.environ.get("LOGIN_URL", "http://localhost:8000/login")
LOGOUT_URL = os.environ.get("LOGOUT_URL", "http://localhost:8000/logout")
AUTH_COOKIE_NAME = os.environ.get("AUTH_COOKIE_NAME", "ea_session")
AI_READ_TIMEOUT = float(os.environ.get("AI_READ_TIMEOUT_SECONDS", "180"))


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

if "query_history" not in st.session_state:
    st.session_state.query_history = []

for legacy_key in ("model_api_key", "runtime_instance", "runtime_fingerprint"):
    # 旧版本把平台密钥/本地 Runtime 放在会话里；升级后立即清除残留。
    st.session_state.pop(legacy_key, None)
if "byok_model" not in st.session_state:
    st.session_state.byok_model = ""
if "byok_base_url" not in st.session_state:
    st.session_state.byok_base_url = ""
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid4())


class BackendError(RuntimeError):
    """后端返回的可控错误；message 已由后端脱敏。"""

    def __init__(self, status_code: int, message: str, code: str = "BACKEND_ERROR"):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


class BackendAuthError(BackendError):
    pass


class BackendPermissionError(BackendError):
    """已登录但缺少 ai:query 等 scope；与后端不可用区分展示。"""


def _session_token() -> str:
    """从浏览器请求读取 HttpOnly 会话 Cookie；本进程不签发、不刷新身份。"""
    try:
        return (st.context.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    except Exception:
        return ""


def _auth_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = _session_token()
    if token:
        headers["Cookie"] = f"{AUTH_COOKIE_NAME}={token}"
    return headers


def _byok_headers(config: dict | None) -> dict[str, str]:
    """只有后端声明 byok 模式时才发送用户密钥；managed 模式发送会被后端拒绝。"""
    if not config or config.get("mode") != "byok":
        return {}
    api_key = st.session_state.get("byok_api_key", "").strip()
    if not api_key:
        return {}
    return {
        "X-LLM-Api-Key": api_key,
        "X-LLM-Base-Url": st.session_state.get("byok_base_url", "").strip(),
        "X-LLM-Model": st.session_state.get("byok_model", "").strip(),
    }


def _api_headers(config: dict | None = None) -> dict[str, str]:
    return {**_auth_headers(), **_byok_headers(config)}


def _error_from_response(response: httpx.Response) -> BackendError:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        message = str(detail.get("message") or "请求失败")
        code = str(detail.get("error_code") or "BACKEND_ERROR")
    elif isinstance(detail, str):
        message, code = detail, "BACKEND_ERROR"
    else:
        message, code = f"后端返回 {response.status_code}", "BACKEND_ERROR"
    if response.status_code == 401:
        return BackendAuthError(401, message, code)
    if response.status_code == 403:
        return BackendPermissionError(403, message, code)
    return BackendError(response.status_code, message, code)


def _request_json(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    config: dict | None = None,
    timeout: float = 20.0,
):
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                method,
                f"{BACKEND_API_URL}{path}",
                headers=_api_headers(config),
                json=body,
            )
    except httpx.HTTPError as exc:
        logger.warning("后端请求失败: %s", type(exc).__name__)
        raise BackendError(0, "无法连接后端服务，请检查部署状态") from exc
    if response.status_code >= 400:
        raise _error_from_response(response)
    return response.json()


def fetch_ai_config(*, force: bool = False) -> dict:
    cached = st.session_state.get("ai_config_cache")
    if cached and not force and time.time() - cached["ts"] < 30:
        return cached["data"]
    data = _request_json("GET", "/api/ai/config")
    st.session_state.ai_config_cache = {"ts": time.time(), "data": data}
    return data


def has_model_credentials(config: dict | None) -> bool:
    if not config:
        return False
    if config.get("mode") == "managed":
        return bool(config.get("configured"))
    return bool(st.session_state.get("byok_api_key"))


def current_model_name(config: dict | None) -> str:
    if not config:
        return "待连接"
    if config.get("mode") == "managed":
        return str(config.get("model") or "待配置")
    return st.session_state.get("byok_model") or "BYOK 待配置"


def verify_session() -> dict | None:
    """用浏览器 Cookie 向后端确认会话；无 Cookie 或已失效时返回 None。"""
    if not _session_token():
        return None
    try:
        return _request_json("GET", "/api/auth/verify", timeout=10.0)
    except BackendAuthError:
        return None


def render_login_gate() -> None:
    st.markdown("""
    <div class="setup-panel">
        <div class="setup-title">需要登录</div>
        <div class="setup-body">AI 助手与 BI 看板共用平台登录会话。登录成功后回到本页刷新即可继续。</div>
        <div class="setup-actions">
            <div class="setup-action"><span class="setup-step">01</span><strong>统一登录</strong><span>账号状态、角色与权限均由后端判定。</span></div>
            <div class="setup-action"><span class="setup-step">02</span><strong>HttpOnly 会话</strong><span>浏览器不保存令牌，页面不显示密钥。</span></div>
            <div class="setup-action"><span class="setup-step">03</span><strong>返回助手</strong><span>登录后刷新本页发起查询。</span></div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.link_button("前往登录", LOGIN_URL)


def render_backend_unavailable(exc: BackendError) -> None:
    st.error(f"后端服务不可用：{exc.message}")
    st.caption("请确认 FastAPI 服务与数据库已启动；本页面不再自行直连数据库或模型。")


def render_permission_denied(exc: BackendError) -> None:
    st.error(f"当前账号无权使用 AI 助手：{exc.message}")
    st.caption("请联系管理员为账号授予 ai:query scope，或改用有权限的账号登录。")


def render_runtime_status(config: dict) -> None:
    """主区域展示模型、数据库、RAG 状态，全部取自后端 /api/ai/config。"""
    has_model = has_model_credentials(config)
    database = config.get("database") or {}
    db_ok = bool(database.get("ok"))
    dialect = str(database.get("dialect") or "").lower()
    db_label = {"mysql": "MySQL", "sqlite": "SQLite"}.get(dialect, dialect.upper() or "后端只读")
    rag = config.get("rag") or {}
    rag_count = int(rag.get("count") or 0)
    rag_ready = bool(rag.get("ok")) and rag_count > 0
    managed = config.get("mode") == "managed"
    items = [
        {
            "label": "MODEL",
            "state": "ok" if has_model else "warn",
            "value": current_model_name(config) if has_model else "待配置",
            "note": ("平台托管，密钥不下发浏览器" if managed else "BYOK 仅保存在当前浏览器会话") if has_model else (
                "联系部署方配置平台模型" if managed else "在侧栏填写 API Key"
            ),
        },
        {
            "label": "DATABASE",
            "state": "ok" if db_ok else "error",
            "value": db_label if db_ok else "不可用",
            "note": "后端只读连接已就绪" if db_ok else "检查后端数据库服务与只读账号",
        },
        {
            "label": "RAG",
            "state": "ok" if rag_ready else "warn",
            "value": f"{rag_count} chunks" if rag_ready else ("知识库为空" if rag.get("ok") else "已降级"),
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


def render_setup_guide(config: dict) -> None:
    """无模型凭据或后端数据库不可用时，给出可恢复的下一步。"""
    database = config.get("database") or {}
    managed = config.get("mode") == "managed"
    if not database.get("ok"):
        title = "后端数据库不可用"
        body = "AI 查询暂时不可用；恢复后端只读数据库连接后即可继续查询。"
        configure_hint = "由部署方检查 AI 只读账号与数据库服务。"
    elif has_model_credentials(config):
        return
    elif managed:
        title = "平台模型尚未配置"
        body = "当前为服务端托管模式，请联系部署方配置模型密钥；用户无需也不能在页面填写平台密钥。"
        configure_hint = "由部署方配置服务端密钥。"
    else:
        title = "完成模型配置后开始查询"
        body = "当前为 BYOK 模式。选择部署方允许的提供方并填写自己的 API Key 后，即可由后端执行查询。"
        configure_hint = "在侧栏选择允许的提供方并填写 API Key。"
    st.markdown(f"""
    <div class="setup-panel">
        <div class="setup-title">{html.escape(title)}</div>
        <div class="setup-body">{html.escape(body)}</div>
        <div class="setup-actions">
            <div class="setup-action"><span class="setup-step">01</span><strong>配置模型</strong><span>{html.escape(configure_hint)}</span></div>
            <div class="setup-action"><span class="setup-step">02</span><strong>测试连接</strong><span>先验证模型可用性，再开始查询。</span></div>
            <div class="setup-action"><span class="setup-step">03</span><strong>检查状态</strong><span>确认数据库为只读可用，RAG 失败时自动降级。</span></div>
        </div>
    </div>
    """, unsafe_allow_html=True)






def stream_backend_query(
    query: str,
    thread_id: str,
    config: dict,
    *,
    on_step,
    on_token,
) -> dict:
    """调用后端 NDJSON 流式接口，实时转发执行步骤与 token，返回最终响应。

    Runtime、RAG、只读 SQL、额度、并发与审计都在后端执行；本进程只渲染事件。
    """
    headers = {**_api_headers(config), "Content-Type": "application/json"}
    final: dict | None = None
    stream_error: BackendError | None = None
    timeout = httpx.Timeout(AI_READ_TIMEOUT, connect=10.0)
    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST",
                f"{BACKEND_API_URL}/api/ai/stream",
                headers=headers,
                json={"query": query, "thread_id": thread_id},
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise _error_from_response(response)
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("忽略无法解析的流事件")
                        continue
                    event_type = event.get("type")
                    if event_type == "step":
                        on_step(event.get("step") or {})
                    elif event_type == "token":
                        on_token(str(event.get("delta") or ""))
                    elif event_type == "final":
                        final = event.get("response") or {}
                    elif event_type == "error":
                        stream_error = BackendError(
                            502,
                            str(event.get("message") or "AI 查询失败"),
                            str(event.get("error_code") or "AI_STREAM_FAILED"),
                        )
    except httpx.HTTPError as exc:
        logger.warning("AI 流式请求中断: %s", type(exc).__name__)
        raise BackendError(0, "AI 流式连接中断，请重试") from exc

    if final is None:
        raise stream_error or BackendError(502, "后端未返回最终结果", "AI_STREAM_INCOMPLETE")
    return final


def pop_backend_last_turn(thread_id: str, question: str) -> bool:
    """重新生成前撤销后端会话末轮，避免旧回答作为上下文自我复读。"""
    result = _request_json(
        "POST",
        "/api/ai/pop-last-turn",
        body={"thread_id": thread_id, "question": question},
    )
    return bool(result.get("removed"))


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




def main() -> None:
    try:
        session_info = verify_session()
    except BackendError as exc:
        render_backend_unavailable(exc)
        st.stop()

    if session_info is None:
        render_login_gate()
        st.stop()

    try:
        ai_config = fetch_ai_config()
    except BackendAuthError:
        render_login_gate()
        st.stop()
    except BackendPermissionError as exc:
        render_permission_denied(exc)
        st.stop()
    except BackendError as exc:
        render_backend_unavailable(exc)
        st.stop()

    render_runtime_status(ai_config)
    render_setup_guide(ai_config)

    with st.sidebar:
        st.caption(
            f"登录账号：{session_info.get('username')}（{session_info.get('role') or session_info.get('principal_type')}）"
        )
        st.link_button("退出登录", LOGOUT_URL, use_container_width=True)
        st.divider()

        with st.expander("模型连接", expanded=not has_model_credentials(ai_config)):
            limits = ai_config.get("limits") or {}
            st.caption(
                f"每日额度 {limits.get('daily_request_limit', '-')} 次 · "
                f"单用户并发 {limits.get('max_inflight_per_user', '-')} · "
                f"超时 {limits.get('query_timeout_seconds', '-')} 秒"
            )
            if ai_config.get("mode") == "managed":
                st.caption(
                    f"模式：服务端托管\n\n"
                    f"提供方：`{ai_config.get('base_url')}`\n\n"
                    f"模型：`{ai_config.get('model')}`"
                )
                st.caption("平台密钥只保存在后端，不会显示或写入浏览器会话。")
                if st.button("测试托管模型连接", use_container_width=True):
                    try:
                        result = _request_json("POST", "/api/ai/test", config=ai_config, timeout=30.0)
                        st.success(f"连接成功，模型已响应：{result.get('model')}")
                    except BackendAuthError:
                        st.warning("会话已失效，请重新登录")
                    except BackendError as exc:
                        st.error(f"连接失败：{exc.message}")
            elif not ai_config.get("allow_byok"):
                st.info("BYOK 未启用；请联系部署方配置平台模型。")
            else:
                providers = ai_config.get("providers") or []
                with st.form("byok_form"):
                    provider = st.selectbox(
                        "模型提供方",
                        providers,
                        index=0,
                        help="只能选择部署方允许的提供方主机。",
                        disabled=not providers,
                    )
                    model_name = st.text_input(
                        "模型名称",
                        value=st.session_state.get("byok_model", ""),
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
                        if model_api_key.strip():
                            st.session_state.byok_api_key = model_api_key.strip()
                        if not st.session_state.get("byok_api_key"):
                            st.warning("请填写 API Key")
                        elif not model_name.strip():
                            st.warning("请填写模型名称")
                        elif not provider:
                            st.warning("部署方未配置允许的提供方")
                        else:
                            st.session_state.byok_model = model_name.strip()
                            st.session_state.byok_base_url = f"https://{provider}"
                            st.session_state.ai_config_cache = None
                            if test_connection:
                                try:
                                    result = _request_json(
                                        "POST", "/api/ai/test", config=ai_config, timeout=30.0
                                    )
                                    st.success(f"连接成功，模型已响应：{result.get('model')}")
                                except BackendAuthError:
                                    st.warning("会话已失效，请重新登录")
                                except BackendError as exc:
                                    st.error(f"连接失败：{exc.message}")
                            if apply_config:
                                st.rerun()

                if st.session_state.get("byok_api_key"):
                    st.success("BYOK 密钥仅保存在当前浏览器会话")
                    if st.button("清除 BYOK 密钥", use_container_width=True):
                        st.session_state.pop("byok_api_key", None)
                        st.rerun()
                else:
                    st.info("选择允许的提供方并填写 API Key 后即可开始查询")

        st.divider()
        with st.expander("RAG 知识库", expanded=False):
            rag = ai_config.get("rag") or {}
            rag_count = int(rag.get("count") or 0)
            if rag.get("ok") and rag_count > 0:
                st.success("RAG 已启用")
                st.metric("向量库", f"{rag_count} chunks", label_visibility="visible")
            elif rag.get("ok"):
                st.warning("RAG 知识库为空")
                st.caption("将降级为纯 SQL 查询模式；请先构建或检查向量库。")
            else:
                st.error(f"RAG 未启用：{str(rag.get('error') or '未知错误')[:80]}")
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
        st.caption("所有查询由后端执行，本页不直连数据库或模型。")
        st.caption(f"模型：{current_model_name(ai_config)}")

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
                        # 后端会话存储同样撤销末轮，否则重新生成会以被删掉的旧答案为上下文。
                        # question 参数保证只在末轮确实是这一轮时才删（防误删其他轮次）。
                        try:
                            pop_backend_last_turn(st.session_state.thread_id, msg["question"])
                        except BackendAuthError:
                            st.warning("会话已失效，请重新登录后再试")
                        except BackendError as exc:
                            logger.warning("撤销会话末轮失败: %s", exc.message)
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
        database = ai_config.get("database") or {}
        if not database.get("ok") or not has_model_credentials(ai_config):
            if not database.get("ok"):
                recovery = "后端数据库不可用。请先恢复只读数据库连接，再重新提交问题。"
            elif ai_config.get("mode") == "managed":
                recovery = "平台模型尚未配置。请联系部署方配置后端模型密钥后重试。"
            else:
                recovery = "模型 API Key 尚未配置。请在侧栏完成模型连接并测试通过后，再重新提交问题。"
            st.session_state.messages.append({"role": "user", "content": prompt,
                                              "chart_title": "", "result_data": None,
                                              "sql": None, "query_time": None})
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                st.warning(recovery)
                st.caption("恢复路径：检查后端模型配置与只读数据库连接；RAG 失败时会自动降级为纯 SQL 查询模式。")
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
                payload = stream_backend_query(
                    prompt,
                    st.session_state.thread_id,
                    ai_config,
                    on_step=_on_step,
                    on_token=_on_token,
                )
                answer = str(payload.get("answer") or "")
                usage = payload.get("usage") or {}
                query_time = (usage.get("latency_ms") or 0) / 1000 if usage else None
                rag_sources = [
                    {
                        "rank": index,
                        "filename": source.get("filename", ""),
                        "section": source.get("section", ""),
                        "doc_type": source.get("doc_type", ""),
                        "score": float(source.get("score") or 0),
                        "preview": source.get("snippet", ""),
                    }
                    for index, source in enumerate(payload.get("sources") or [], 1)
                ]
                agent_steps = payload.get("steps") or []
                analysis_evidence = payload.get("analysis_evidence") or []
                extracted_sql = payload.get("sql")
                sql_error = payload.get("sql_error")
                result_df = pd.DataFrame(payload.get("result") or [])
            except BackendAuthError:
                step_placeholder.empty()
                answer_placeholder.empty()
                st.warning("会话已失效，请重新登录后重试；本次问题未写入会话记录。")
                st.stop()
            except BackendError as exc:
                step_placeholder.empty()
                answer_placeholder.empty()
                st.error(f"执行出错：{exc.message}")
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": f"执行失败：{exc.message}。你的问题已保留在对话中，可稍后重试。",
                    "chart_title": "", "result_data": None,
                    "sql": None, "query_time": None, "rag_sources": [],
                })
                st.stop()

            # 流式渲染结束：清空占位（含打字机光标），改用高亮版最终答案。
            step_placeholder.empty()
            answer_placeholder.empty()

            chart_title = prompt[:30]
            if sql_error:
                st.warning(f"SQL 执行失败：{sql_error}")

            render_answer_with_highlights(answer)
            render_agent_steps(agent_steps)
            if analysis_evidence:
                with st.expander("多步分析数据依据", expanded=False):
                    for item in analysis_evidence:
                        st.markdown(f"**{item['label']}**")
                        st.code(item["sql"], language="sql")
                        st.dataframe(pd.DataFrame(item["rows"]), use_container_width=True)

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
                key_prefix=f"live_{len(st.session_state.messages)}",
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
                "analysis_evidence": analysis_evidence,
            }
            st.session_state.messages.append(msg_data)

            st.session_state.query_history.append({
                "question": prompt,
                "answer": answer[:100],
            })



if st.runtime.exists():
    main()
