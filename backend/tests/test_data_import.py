"""业务数据导入：列映射、行校验、上传/预览接口与权限边界。"""
from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook
from sqlalchemy import delete, select, update

from backend.database import async_session_factory, auth_session_factory, import_session_factory
from backend.main import app
from backend.models.database_models import (
    AuthUser,
    DataImportRow,
    DataImportRun,
    Order,
    OrderEvent,
)
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


@pytest_asyncio.fixture
async def cleanup_orders():
    """执行类测试会真的写入 orders/order_events；结束后按唯一订单号清理。"""
    order_ids: list[str] = []
    yield order_ids
    if not order_ids:
        return
    async with async_session_factory() as session:
        await session.execute(delete(OrderEvent).where(OrderEvent.order_no.in_(order_ids)))
        await session.execute(delete(Order).where(Order.order_no.in_(order_ids)))
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
        ["订单号", "用户名", "商品编号", "订单金额", "付款金额", "平台类型", "下单时间", "备注"]
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
        "user_name": "用户名",
        "product_id": "商品编号",
        "order_amount": "订单金额",
        "payment_amount": "付款金额",
        "platform_type": "平台类型",
        "order_time": "下单时间",
        "payment_time": "付款时间",
    }
    normalized, errors = normalize_row(
        {
            "订单号": "A-1",
            "用户名": "user-a",
            "商品编号": "P1",
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
            "用户名": "user-a",
            "商品编号": "P1",
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
        ["订单号", "用户名", "商品编号", "订单金额", "付款金额", "平台类型", "下单时间", "是否退款"]
    )
    sheet.append(
        [
            _unique_order_id(),
            "xlsx-user",
            "P1",
            88.5,
            88.5,
            "APP",
            datetime(2025, 2, 2, 9, 0, 0),
            "否",
        ]
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
async def test_insert_orders_safely_empty_batch_is_noop():
    """空候选批次不能触发「仅含默认值」的 INSERT：orders 会因缺少 order_id 以 1364 中断。"""
    from backend.services.data_import_service import _insert_orders_safely

    async with async_session_factory() as session:
        inserted, failed = await _insert_orders_safely(session, [])
        assert inserted == []
        assert failed == []


@pytest.mark.asyncio
async def test_execute_preserves_seq_id_and_refund_flag(
    importer_client: AsyncClient, cleanup_runs, cleanup_orders
):
    """Core insert 必须使用列键：order_seq_id 不能退化为自增，is_refund 不能写成 NULL。"""
    order_id = _unique_order_id()
    seq_id = 200000 + uuid.uuid4().int % 100000
    cleanup_orders.append(order_id)
    header = (
        "订单顺序编号,订单号,用户名,商品编号,订单金额,付款金额,渠道编号,平台类型,"
        "下单时间,付款时间,是否退款,优惠金额,支付耗时_秒\n"
    )
    content = (
        header
        + f"{seq_id},{order_id},user-refund,P1,150.00,150.00,c1,APP,"
        "2025-03-02 10:00:00,2025-03-02 10:00:05,是,0,5\n"
    ).encode("utf-8")

    response = await _upload(importer_client, content)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    cleanup_runs.append(run_id)

    status_payload = await _wait_until(importer_client, run_id, "validated")
    confirmed = await importer_client.post(
        f"/api/data-import/runs/{run_id}/confirm",
        json={"expected_sha256": status_payload["sha256"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    executed = await importer_client.post(
        f"/api/data-import/runs/{run_id}/execute",
        json={"confirm_token": confirmed.json()["confirm_token"]},
    )
    assert executed.status_code == 202, executed.text

    final = await _wait_until(importer_client, run_id, "completed", timeout=20.0)
    # 一致性校验必须真正生效：退款行的状态/事件版本要能对上账本。
    assert final["counts"]["rows_inserted"] == 1
    assert final["counts"]["rows_refunded"] == 1

    async with async_session_factory() as session:
        order = (
            await session.execute(select(Order).where(Order.order_no == order_id))
        ).scalar_one()
        assert order.id == seq_id
        assert order.is_refunded == "是"
        assert order.order_status == "REFUNDED"
        assert order.event_version == 2


@pytest.mark.asyncio
async def test_confirm_and_execute_import(
    importer_client: AsyncClient, cleanup_runs, cleanup_orders
):
    """完整链路：上传 → 校验 → 二次确认 → 执行 → 结果与账本写入。"""
    order_id = _unique_order_id()
    cleanup_orders.append(order_id)
    content = _csv_bytes(
        [f"{order_id},user-exec,P1,120.00,120.00,c1,APP,2025-03-01 10:00:00,2025-03-01 10:00:05,否,0,5"]
    )

    response = await _upload(importer_client, content)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    cleanup_runs.append(run_id)

    status_payload = await _wait_until(importer_client, run_id, "validated")
    sha256 = status_payload["sha256"]
    assert sha256 == hashlib.sha256(content).hexdigest()

    # 摘要不一致必须拒绝，防止确认的不是预览里的同一份文件。
    mismatch = await importer_client.post(
        f"/api/data-import/runs/{run_id}/confirm",
        json={"expected_sha256": "0" * 64},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["error_code"] == "IMPORT_SOURCE_MISMATCH"

    confirmed = await importer_client.post(
        f"/api/data-import/runs/{run_id}/confirm",
        json={"expected_sha256": sha256},
    )
    assert confirmed.status_code == 200, confirmed.text
    token = confirmed.json()["confirm_token"]
    assert token

    invalid_token = await importer_client.post(
        f"/api/data-import/runs/{run_id}/execute",
        json={"confirm_token": "x" * 32},
    )
    assert invalid_token.status_code == 403
    assert invalid_token.json()["detail"]["error_code"] == "IMPORT_CONFIRM_TOKEN_INVALID"

    executed = await importer_client.post(
        f"/api/data-import/runs/{run_id}/execute",
        json={"confirm_token": token},
    )
    assert executed.status_code == 202, executed.text

    final = await _wait_until(importer_client, run_id, "completed", timeout=20.0)
    assert final["counts"]["rows_inserted"] == 1
    assert final["counts"]["rows_rejected"] == 0
    assert final["progress_percent"] == 100.0

    result = (await importer_client.get(f"/api/data-import/runs/{run_id}/result")).json()
    assert result["run"]["status"] == "completed"
    assert result["failed"] == []

    async with async_session_factory() as session:
        order = (
            await session.execute(select(Order).where(Order.order_no == order_id))
        ).scalar_one()
        assert order.order_status == "ACTIVE"
        assert order.event_version == 1
        events = (
            await session.execute(
                select(OrderEvent)
                .where(OrderEvent.order_no == order_id)
                .order_by(OrderEvent.sequence)
            )
        ).scalars().all()
        assert [event.event_type for event in events] == ["ORDER_CREATED"]
        assert events[0].source == "data_import"


@pytest.mark.asyncio
async def test_refund_row_imports_v2_event_and_failure_csv(importer_client: AsyncClient, cleanup_runs, cleanup_orders):
    """退款行补记 v2；失败导出带 BOM 且对公式前缀转义。"""
    refund_order = _unique_order_id()
    invalid_order = "=cmd|' /C calc'!A0"
    cleanup_orders.append(refund_order)
    content = _csv_bytes(
        [
            f"{refund_order},user-refund,P1,50.00,50.00,c1,APP,2025-03-02 09:00:00,2025-03-02 09:00:03,是,0,3",
            f"\"{invalid_order}\",user-bad,P2,10.00,10.00,c1,APP,not-a-date,2025-03-02 09:00:03,否,0,3",
        ]
    )
    response = await _upload(importer_client, content)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    cleanup_runs.append(run_id)

    status_payload = await _wait_until(importer_client, run_id, "validated")
    confirmed = await importer_client.post(
        f"/api/data-import/runs/{run_id}/confirm",
        json={"expected_sha256": status_payload["sha256"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    executed = await importer_client.post(
        f"/api/data-import/runs/{run_id}/execute",
        json={"confirm_token": confirmed.json()["confirm_token"]},
    )
    assert executed.status_code == 202, executed.text

    final = await _wait_until(importer_client, run_id, "completed", timeout=20.0)
    assert final["counts"]["rows_inserted"] == 1
    assert final["counts"]["rows_refunded"] == 1

    async with async_session_factory() as session:
        order = (
            await session.execute(select(Order).where(Order.order_no == refund_order))
        ).scalar_one()
        assert order.order_status == "REFUNDED"
        assert order.event_version == 2
        refunded = (
            await session.execute(
                select(OrderEvent).where(
                    OrderEvent.order_no == refund_order,
                    OrderEvent.event_type == "ORDER_REFUNDED",
                )
            )
        ).scalar_one()
        assert refunded.sequence == 2

    export = await importer_client.get(f"/api/data-import/runs/{run_id}/failures.csv")
    assert export.status_code == 200
    body = export.content.decode("utf-8")
    assert body.startswith("\ufeff")
    # 以 = + - @ 开头的值必须加前缀，避免 Excel 当公式执行。
    assert "'=cmd" in body


@pytest.mark.asyncio
async def test_import_history_lists_own_runs(importer_client: AsyncClient, cleanup_runs):
    order_id = _unique_order_id()
    response = await _upload(
        importer_client,
        _csv_bytes(
            [
                f"{order_id},user-a,P1,100.00,100.00,c1,APP,2025-03-01 10:00:00,2025-03-01 10:00:05,否,0,5"
            ]
        ),
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    cleanup_runs.append(run_id)
    await _wait_until(importer_client, run_id, "validated")

    listed = await importer_client.get(
        "/api/data-import/runs", params={"page": 1, "page_size": 20}
    )
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["total"] >= 1
    assert any(item["run_id"] == run_id for item in payload["items"])
    # 普通导入员只能看到自己的任务。
    operators = {item["operator"] for item in payload["items"]}
    assert len(operators) == 1


@pytest.mark.asyncio
async def test_cancel_validated_run_marks_cancelled(importer_client: AsyncClient, cleanup_runs):
    response = await _upload(
        importer_client,
        _csv_bytes(
            [
                f"{_unique_order_id()},user-a,P1,100.00,100.00,c1,APP,2025-03-01 10:00:00,2025-03-01 10:00:05,否,0,5"
            ]
        ),
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    cleanup_runs.append(run_id)
    await _wait_until(importer_client, run_id, "validated")

    cancelled = await importer_client.post(f"/api/data-import/runs/{run_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    payload = cancelled.json()
    assert payload["status"] == "cancelled"
    assert payload["cancel_requested"] is True
    assert payload["can_confirm"] is False


@pytest.mark.asyncio
async def test_upload_prunes_expired_runs_but_keeps_active(importer_client: AsyncClient):
    """过期清理只删除终态历史，校验/执行中的任务必须保留。"""
    old = datetime.now() - timedelta(days=30)
    async with import_session_factory() as session:
        stale = DataImportRun(
            source="stale.csv",
            source_sha256="a" * 64,
            mode="upload",
            status="validated",
            operator="cleanup-user",
            started_at=old,
            finished_at=old,
        )
        active = DataImportRun(
            source="active.csv",
            source_sha256="b" * 64,
            mode="upload",
            status="running",
            operator="cleanup-user",
            started_at=old,
            heartbeat_at=old,
        )
        session.add_all([stale, active])
        await session.commit()
        stale_id, active_id = stale.id, active.id

    response = await _upload(
        importer_client,
        _csv_bytes(
            [
                f"{_unique_order_id()},user-a,P1,100.00,100.00,c1,APP,2025-03-01 10:00:00,2025-03-01 10:00:05,否,0,5"
            ]
        ),
    )
    assert response.status_code == 202, response.text
    async with import_session_factory() as session:
        assert await session.get(DataImportRun, stale_id) is None
        assert await session.get(DataImportRun, active_id) is not None
        await session.execute(delete(DataImportRun).where(DataImportRun.id == active_id))
        await session.execute(
            delete(DataImportRow).where(DataImportRow.run_id == response.json()["run_id"])
        )
        await session.execute(
            delete(DataImportRun).where(DataImportRun.id == response.json()["run_id"])
        )
        await session.commit()


@pytest.mark.asyncio
async def test_import_page_anonymous_redirects_to_login(client: AsyncClient):
    response = await client.get("/workspace/import")
    assert response.status_code in (302, 303, 307)
    assert response.headers["location"].endswith("/login?next=/workspace/import")


@pytest.mark.asyncio
async def test_import_page_serves_assets_for_importer(importer_client: AsyncClient):
    page = await importer_client.get("/workspace/import")
    assert page.status_code == 200, page.text
    assert "数据导入" in page.text
    assert "/static/css/import.css" in page.text
    assert "/static/js/import.js" in page.text
    assert page.headers["cache-control"] == "no-store"
    for asset in ("/static/css/import.css", "/static/js/import.js"):
        assert (await importer_client.get(asset)).status_code == 200


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


@pytest.mark.asyncio
async def test_consistency_flags_null_refund_flag():
    """退款订单的 is_refund 为 NULL 时必须判为不一致。

    旧写法 `is_refund <> '是'` 对 NULL 恒为 NULL，退款事件存在也会漏判；
    这里用「已退款但退款标记缺失」这一真实坏数据形态做回归。
    """
    from backend.services.data_import_service import _consistency_mismatch_count

    order_id = _unique_order_id()
    seq_id = 300000 + uuid.uuid4().int % 100000
    run_id = 800000 + uuid.uuid4().int % 100000
    occurred = datetime(2025, 4, 1, 12, 0, 0)

    try:
        # 已退款账本 + 退款标记为 NULL 的坏数据。
        async with async_session_factory() as session:
            session.add(
                Order(
                    id=seq_id,
                    order_no=order_id,
                    order_status="REFUNDED",
                    event_version=2,
                    is_refunded=None,
                )
            )
            session.add_all(
                [
                    OrderEvent(
                        event_id=uuid.uuid4().hex,
                        order_no=order_id,
                        event_type="ORDER_CREATED",
                        occurred_at=occurred,
                        sequence=1,
                        source="loadtest",
                        payload={"order_id": order_id},
                        processing_status="processed",
                    ),
                    OrderEvent(
                        event_id=uuid.uuid4().hex,
                        order_no=order_id,
                        event_type="ORDER_REFUNDED",
                        occurred_at=occurred + timedelta(seconds=5),
                        sequence=2,
                        source="loadtest",
                        payload={"order_id": order_id},
                        processing_status="processed",
                    ),
                ]
            )
            await session.commit()

        async with import_session_factory() as session:
            session.add(
                DataImportRow(
                    run_id=run_id,
                    row_no=1,
                    order_id=order_id,
                    status="imported",
                )
            )
            await session.commit()

        async with async_session_factory() as session:
            assert await _consistency_mismatch_count(session, run_id) == 1
            await session.execute(
                update(Order).where(Order.order_no == order_id).values(is_refunded="是")
            )
            await session.commit()

        # 补齐退款标记后，正常退款数据的判定结果不变。
        async with async_session_factory() as session:
            assert await _consistency_mismatch_count(session, run_id) == 0
    finally:
        async with import_session_factory() as session:
            await session.execute(delete(DataImportRow).where(DataImportRow.run_id == run_id))
            await session.commit()
        async with async_session_factory() as session:
            await session.execute(delete(OrderEvent).where(OrderEvent.order_no == order_id))
            await session.execute(delete(Order).where(Order.order_no == order_id))
            await session.commit()


