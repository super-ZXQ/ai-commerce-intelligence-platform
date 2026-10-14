"""服务端Key不能进入输入框或跟随浏览器自定义URL。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock


def test_server_key_uses_only_operator_endpoint(monkeypatch):
    import app

    captured = {}

    def adapter(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(generate_sql=AsyncMock(), astream_answer=AsyncMock())

    monkeypatch.setattr(app, "API_KEY", "test-server-only-key")
    monkeypatch.setattr(app, "BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(app, "OpenAIModelAdapter", adapter)
    monkeypatch.setattr(app, "get_db_uri", lambda: "sqlite:///:memory:")
    app.st.session_state.model_api_key = ""
    app.st.session_state.model_base_url = "https://attacker.example"
    app.st.session_state.pop("runtime_fingerprint", None)
    app.get_session_runtime(object(), None)
    assert captured["api_key"] == "test-server-only-key"
    assert captured["base_url"] == "https://api.deepseek.com"
    assert app.st.session_state.model_api_key == ""
    app.st.session_state.pop("runtime_fingerprint", None)
    app.st.session_state.pop("runtime_instance", None)


def test_direct_streamlit_execution_stops_before_sensitive_work():
    from streamlit.testing.v1 import AppTest

    code = "import streamlit as st\nfrom agent_core.page_auth import require_page_user\nrequire_page_user()\nst.text('sensitive-content')"
    page = AppTest.from_string(code).run(timeout=15)
    assert not page.exception
    assert len(page.error) == 1
    assert len(page.text) == 0
