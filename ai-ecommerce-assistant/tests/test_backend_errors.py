"""后端错误映射：401/403/5xx 必须区分展示，不能把权限不足说成服务不可用。"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app  # noqa: E402


def _response(status: int, detail: object) -> httpx.Response:
    return httpx.Response(
        status,
        json={"detail": detail},
        request=httpx.Request("GET", "http://backend/api/ai/config"),
    )


def test_401_maps_to_auth_error():
    error = app._error_from_response(_response(401, "会话已失效"))
    assert isinstance(error, app.BackendAuthError)
    assert error.message == "会话已失效"


def test_403_maps_to_permission_error():
    error = app._error_from_response(
        _response(403, {"message": "缺少 ai:query 权限", "error_code": "FORBIDDEN"})
    )
    assert isinstance(error, app.BackendPermissionError)
    assert not isinstance(error, app.BackendAuthError)
    assert error.code == "FORBIDDEN"


def test_500_maps_to_generic_backend_error():
    error = app._error_from_response(_response(500, {"message": "内部错误"}))
    assert type(error) is app.BackendError
    assert error.status_code == 500
