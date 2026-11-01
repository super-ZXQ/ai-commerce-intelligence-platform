"""BI 后端客户端：Cookie 透传、分页完整性与权限错误映射。"""
from __future__ import annotations

import httpx
import pytest

from bi_api import BiApiClient, BiApiError, BiAuthError, BiPermissionError


def _json_response(payload: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def test_fetch_orders_paginates_and_forwards_session_cookie():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["cookie"] == "ea_session=test-token"
        page = int(request.url.params["page"])
        assert request.url.params["page_size"] == "2"
        if page == 1:
            assert "data_version" not in request.url.params
            return _json_response(
                {"total": 3, "page": 1, "page_size": 2, "total_pages": 2,
                 "data_version": 7,
                 "items": [{"order_no": "A"}, {"order_no": "B"}]}
            )
        # 后续页必须携带首屏版本，才能与第一页处于同一快照。
        assert request.url.params["data_version"] == "7"
        return _json_response(
            {"total": 3, "page": 2, "page_size": 2, "total_pages": 2,
             "data_version": 7,
             "items": [{"order_no": "C"}]}
        )

    client = BiApiClient(
        "http://backend",
        "ea_session",
        "test-token",
        transport=httpx.MockTransport(handler),
    )
    rows = client.fetch_orders(page_size=2)

    assert [row["order_no"] for row in rows] == ["A", "B", "C"]
    assert len(requests) == 2


def test_fetch_orders_rejects_incomplete_pagination():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 1:
            return _json_response(
                {"total": 2, "data_version": 1, "items": [{"order_no": "A"}]}
            )
        return _json_response(
            {"total": 2, "data_version": 1, "items": []}
        )

    client = BiApiClient(
        "http://backend", "ea_session", "token", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(BiApiError, match="分页不完整"):
        client.fetch_orders(page_size=1)


def test_fetch_orders_retries_whole_snapshot_after_version_conflict():
    """第二页遇到 409 时必须丢弃已拼接结果，从第一页整次重读。"""
    calls: list[tuple[int, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        version_param = request.url.params.get("data_version")
        calls.append((page, version_param))
        if page == 1 and len([c for c in calls if c[0] == 1]) == 1:
            return _json_response(
                {"total": 3, "data_version": 7,
                 "items": [{"order_no": "A"}, {"order_no": "B"}]}
            )
        if page == 2 and version_param == "7":
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "error_code": "BI_DATA_VERSION_CHANGED",
                        "message": "BI 数据在分页读取期间发生变化，请重新加载",
                        "data_version": 8,
                    }
                },
            )
        if page == 1:
            return _json_response(
                {"total": 3, "data_version": 8,
                 "items": [{"order_no": "A"}, {"order_no": "B"}]}
            )
        return _json_response(
            {"total": 3, "data_version": 8, "items": [{"order_no": "C"}]}
        )

    client = BiApiClient(
        "http://backend", "ea_session", "token", transport=httpx.MockTransport(handler)
    )
    rows = client.fetch_orders(page_size=2)

    assert [row["order_no"] for row in rows] == ["A", "B", "C"]
    assert calls == [(1, None), (2, "7"), (1, None), (2, "8")]


def test_fetch_orders_rejects_mixed_versions_across_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        if int(request.url.params["page"]) == 1:
            return _json_response(
                {"total": 3, "data_version": 7,
                 "items": [{"order_no": "A"}, {"order_no": "B"}]}
            )
        return _json_response(
            {"total": 3, "data_version": 8, "items": [{"order_no": "C"}]}
        )

    client = BiApiClient(
        "http://backend", "ea_session", "token", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(BiApiError, match="持续变化"):
        client.fetch_orders(page_size=2, max_retries=2)


def test_verify_session_returns_none_on_expired_cookie():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "登录已失效"})

    client = BiApiClient(
        "http://backend", "ea_session", "expired", transport=httpx.MockTransport(handler)
    )
    assert client.verify_session() is None


def test_rfm_export_maps_permission_error_and_passes_thresholds():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(
            403,
            json={"detail": {"error_code": "SCOPE_REQUIRED", "message": "缺少权限：data:export"}},
        )

    client = BiApiClient(
        "http://backend", "ea_session", "token", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(BiPermissionError) as exc_info:
        client.fetch_rfm_export(
            segment="重要价值客户",
            reference_date="2026-01-31",
            n_bins=5,
            r_threshold=3,
            f_threshold=4,
            m_threshold=5,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == "SCOPE_REQUIRED"
    assert seen["segment"] == "重要价值客户"
    assert seen["r_threshold"] == "3"
    assert seen["f_threshold"] == "4"
    assert seen["m_threshold"] == "5"
    assert seen["limit"] == "200"


def test_rfm_export_returns_backend_bytes():
    content = "\ufeff客户ID,R评分\nuser-1,5\n".encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/export/rfm-segment"
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": "text/csv; charset=utf-8"},
        )

    client = BiApiClient(
        "http://backend", "ea_session", "token", transport=httpx.MockTransport(handler)
    )
    data = client.fetch_rfm_export(
        segment="重要价值客户",
        reference_date="2026-01-31",
        n_bins=5,
        r_threshold=3,
        f_threshold=3,
        m_threshold=3,
    )
    assert data == content


def test_backend_connection_failure_is_not_treated_as_expired_session():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("backend down", request=request)

    client = BiApiClient(
        "http://backend", "ea_session", "token", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(BiApiError, match="无法连接后端"):
        client.verify_session()


def test_missing_cookie_does_not_call_backend():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("无会话时不得请求后端")

    client = BiApiClient(
        "http://backend", "ea_session", "", transport=httpx.MockTransport(handler)
    )
    assert client.verify_session() is None
    with pytest.raises(BiAuthError):
        client.fetch_orders()
