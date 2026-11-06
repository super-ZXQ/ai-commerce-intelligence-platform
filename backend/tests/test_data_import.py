"""业务数据导入：列映射、行校验、上传/预览接口与权限边界。"""
from __future__ import annotations

import asyncio
import io
import uuid
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook
from sqlalchemy import delete

from backend.database import auth_session_factory, import_session_factory
from backend.main import app
from backend.models.database_models import AuthUser, DataImportRow, DataImportRun
from backend.utils.auth import hash_password


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest_asyncio.fixture
async def importer_user():
    username = "importer_" + uuid.uuid4().hex[:8]
    password = "importer-pass-123"
    async with auth_session_factory() as session:
        session.add(
            AuthUser(
                username=username,
                password_hash=hash_password(password),
                role="importer",
                scopes="",
                is_active=True,
                token_version=1,
            )
        )
        await session.commit()
    try:
        yield {"username": username, "password": password}
    finally:
        async with auth_session_factory() as session:
            await session.execute(delete(AuthUser).where(AuthUser.username == username))
            await session.commit()


@pytest_asyncio.fixture
async def importer_client(client: AsyncClient, importer_user: dict):
    async with client:
        response = await client.post(
            "/api/auth/login",
            json={"username": importer_user["username"], "password": importer_user["password"]},
        )
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        yield client


@pytest_asyncio.fixture
async def cleanup_runs():
    run_ids: list[int] = []
    yield run_ids
    if not run_ids:
        return
    async with import_session_factory() as session:
        await session.execute(delete(DataImportRow).where(DataImportRow.run_id.in_(run_ids)))
        await session.execute(delete(DataImportRun).where(DataImportRun.id.in_(run_ids)))
        await session.commit()


def _csv_bytes(rows: list[str]) -> bytes:
    header = (
        "订单号,用户名,商品编号,订单金额,付款金额,渠道编号,平台类型,"
        "下单时间,付款时间,是否退款,优惠金额,支付耗时_秒\n"
    )
    return (header + "\n".join(rows) + "\n").encode("utf-8")


def _unique_order_id() -> str:
    return "it-" + uuid.uuid4().hex[:12]


async def _upload(client: AsyncClient, content: bytes, filename: str = "orders.csv"):
    return await client.post(
        "/api/data-import/upload",
        files={"file": (filename, content, "application/octet-stream")},
        data={"conflict_policy": "reject"},
    )


async def _wait_until(client: AsyncClient, run_id: int, expected: str, timeout: float = 10.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/api/data-import/runs/{run_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] == expected:
            return payload
        await asyncio.sleep(0.1)
    raise AssertionError(f"run {run_id} 未在 {timeout}s 内达到 {expected}")


def test_column_mapping_accepts_chinese_headers_and_reports_unknown():
    from backend.services.data_import_service import build_column_mapping

    mapping = build_column_mapping(
        ["订单号", "用户名", "订单金额", "付款金额", "平台类型", "下单时间", "备注"]
    )
    assert mapping["missing_required"] == []
    assert mapping["unknown_columns"] == ["备注"]
    canonical = {item["canonical"]: item["source"] for item in mapping["columns"]}
    assert canonical["order_id"] == "订单号"
    assert canonical["payment_time"] is None


def test_column_mapping_rejects_missing_required_columns():
    from backend.services.data_import_service import ImportFileError, build_column_mapping

    with pytest.raises(ImportFileError) as exc_info:
        build_column_mapping(["订单号", "用户名"])
    assert exc_info.value.code == "IMPORT_MISSING_COLUMNS"
    assert "order_amount" in exc_info.value.message


def test_normalize_row_derives_fields_and_flags_payment_time_order():
    from backend.services.data_import_service import normalize_row

    sources = {
        "order_id": "订单号",
        "order_amount": "订单金额",
        "payment_amount": "付款金额",
        "platform_type": "平台类型",
        "order_time": "下单时间",
        "payment_time": "付款时间",
    }
    normalized, errors = normalize_row(
        {
            "订单号": "A-1",
            "订单金额": "100.005",
            "付款金额": "100",
            "平台类型": "APP",
            "下单时间": "2025-02-01 10:00:00",
            "付款时间": "2025-02-01 09:59:00",
        },
        sources,
    )
    assert errors and errors[0][0] == "PAYMENT_BEFORE_ORDER"
    assert normalized["order_amount"] == "100.01"
    assert normalized["order_date"] == "2025-02-01"
    assert normalized["weekday"] == "Saturday"

    normalized, errors = normalize_row(
        {
            "订单号": "A-2",
            "订单金额": "100",
            "付款金额": "100",
            "平台类型": "APP",
            "下单时间": "2025-02-01 10:00:00",
            "付款时间": "2025-02-01 10:00:30",
        },
        sources,
    )
    assert errors == []
    assert normalized["payment_duration_sec"] == 30


@pytest.mark.asyncio
async def test_upload_validates_conflict_and_invalid_rows(importer_client: AsyncClient, cleanup_runs):
    duplicate = _unique_order_id()
    rows = [
        f"{duplicate},user-a,P1,100.00,100.00,c1,APP,2025-02-01 10:00:00,2025-02-01 10:00:05,否,0,5",
        f"{duplicate},user-b,P2,200.00,200.00,c1,APP,2025-02-01 11:00:00,2025-02-01 11:00:05,否,0,5",
        f"{_unique_order_id()},user-c,P3,300.00,300.00,c1,APP,not-a-date,2025-02-01 12:00:00,否,0,5",
    ]
    response = await _upload(importer_client, _csv_bytes(rows))
    assert response.status_code == 202, response.text
    payload = response.json()
    run_id = payload["run_id"]
    cleanup_runs.append(run_id)

    status_payload = await _wait_until(importer_client, run_id, "validated")
    counts = status_payload["counts"]
    assert counts["rows_read"] == 3
    assert counts["rows_valid"] == 1
    assert counts["rows_conflict"] == 1
    assert counts["rows_invalid"] == 1
    assert status_payload["can_confirm"] is True

    preview = (await importer_client.get(f"/api/data-import/runs/{run_id}/preview")).json()
    assert preview["columns"][0]["canonical"] == "order_seq_id"
    assert len(preview["valid"]) == 1
    assert len(preview["conflict"]) == 1
    assert len(preview["invalid"]) == 1
    # importer 默认没有 data:pii，预览里的用户名必须脱敏。
    assert preview["valid"][0]["fields"]["user_name"] != "user-a"

    page = (await importer_client.get(
        f"/api/data-import/runs/{run_id}/rows",
        params={"status": "invalid", "page": 1, "page_size": 100},
    )).json()
    assert page["total"] == 1
    assert page["items"][0]["error_code"] == "INVALID_DATETIME"


@pytest.mark.asyncio
async def test_upload_xlsx_streaming_path(importer_client: AsyncClient, cleanup_runs):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        ["订单号", "用户名", "订单金额", "付款金额", "平台类型", "下单时间", "是否退款"]
    )
    sheet.append(
        [_unique_order_id(), "xlsx-user", 88.5, 88.5, "APP", datetime(2025, 2, 2, 9, 0, 0), "否"]
    )
    buffer = io.BytesIO()
    workbook.save(buffer)
    content = buffer.getvalue()

    response = await _upload(importer_client, content, filename="orders.xlsx")
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    cleanup_runs.append(run_id)

    status_payload = await _wait_until(importer_client, run_id, "validated")
    assert status_payload["source_type"] == "xlsx"
    assert status_payload["counts"]["rows_valid"] == 1


@pytest.mark.asyncio
async def test_upload_rejects_disguised_binary_file(importer_client: AsyncClient):
    response = await _upload(importer_client, b"PK\x03\x04not-a-real-xlsx", filename="fake.csv")
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "IMPORT_FILE_TYPE_MISMATCH"


@pytest.mark.asyncio
async def test_upload_requires_data_import_scope(client: AsyncClient):
    # 未登录访问也必须被服务端拒绝，而不是依赖前端隐藏入口。
    response = await client.post(
        "/api/data-import/upload",
        files={"file": ("orders.csv", _csv_bytes(["x"]), "application/octet-stream")},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_viewer_cannot_upload(client: AsyncClient):
    username = "viewer_" + uuid.uuid4().hex[:8]
    password = "viewer-pass-123"
    async with auth_session_factory() as session:
        session.add(
            AuthUser(
                username=username,
                password_hash=hash_password(password),
                role="viewer",
                scopes="",
                is_active=True,
                token_version=1,
            )
        )
        await session.commit()
    try:
        async with client:
            login = await client.post(
                "/api/auth/login", json={"username": username, "password": password}
            )
            assert login.status_code == 200
            client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
            response = await _upload(client, _csv_bytes(["x"]))
            assert response.status_code == 403
    finally:
        async with auth_session_factory() as session:
            await session.execute(delete(AuthUser).where(AuthUser.username == username))
            await session.commit()


