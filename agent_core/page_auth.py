"""Streamlit 服务端门禁：不信任用户名头或浏览器角色。"""

import os

import httpx


def verify_page_session(token: str) -> dict:
    """向认证服务验证签名和有效期；认证服务不可用时拒绝访问。"""
    if not token:
        raise ValueError("未登录")
    base = os.environ.get("AUTH_BACKEND_URL", "http://localhost:8000").rstrip("/")
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
        response = client.get(
            base + "/api/auth/page-session", headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json()


def require_page_user() -> dict | None:
    """每次脚本执行都重新认证，避免已过期会话继续使用服务器模型凭据。"""
    import streamlit as st
    from streamlit.runtime.scriptrunner import get_script_run_ctx

    # 纯函数测试以普通 Python 导入入口时没有浏览器会话，不执行页面代码。
    if get_script_run_ctx(suppress_warning=True) is None:
        return None
    try:
        return verify_page_session(st.context.cookies.get("ea_session", ""))
    except (ValueError, httpx.HTTPError):
        st.error("请先在平台演示页登录，再访问此页面。")
        st.link_button("前往登录", os.environ.get("PUBLIC_LOGIN_URL", "http://localhost:8000/demo"))
        st.stop()
