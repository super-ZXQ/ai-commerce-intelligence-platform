"""安全加固 + Bug 修复回归测试。

覆盖范围（不依赖 MySQL）：
- CSV 公式注入防护
- XFF 取值方向（最右侧）
- 缓存 _RefLock 引用计数 + 清理竞态安全
- RFM 数据不可用时以异常抛出（不被缓存）
- RFM distinct 计数口径
- RFM 路由参数补齐
- 错误信息脱敏
- AI SQL 只读账户连接串
- 明文密码生产环境拒绝
- sql_guard 只读拦截不变
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.config import Settings
from backend.routes.export import _sanitize_cell
from backend.services.rfm_service import (
    RfmDataUnavailableError,
    _build_rfm_snapshot,
    _fetch_rfm_raw,
    clear_rfm_snapshot_cache,
)
from backend.utils import cache
from backend.utils.rate_limiter import _get_client_id
from backend.utils.sql_guard import is_read_only_sql

# ─── 安全#5：CSV 公式注入防护 ───────────────────────────


class TestCsvFormulaInjection:
    def test_leading_equals_gets_prefixed(self):
        assert _sanitize_cell("=SUM(A1:A10)") == "'=SUM(A1:A10)"

    def test_leading_plus_gets_prefixed(self):
        assert _sanitize_cell("+cmd|/C calc") == "'+cmd|/C calc"

    def test_leading_minus_gets_prefixed(self):
        assert _sanitize_cell("-1+2") == "'-1+2"

    def test_leading_at_gets_prefixed(self):
        assert _sanitize_cell("@SUM(...)") == "'@SUM(...)"

    def test_normal_value_unchanged(self):
        assert _sanitize_cell("普通文本") == "普通文本"
        assert _sanitize_cell(123) == 123

    def test_empty_string_unchanged(self):
        assert _sanitize_cell("") == ""

    def test_tab_prefix(self):
        assert _sanitize_cell("\tcmd") == "'\tcmd"

    def test_cr_prefix(self):
        assert _sanitize_cell("\r\nCMD") == "'\r\nCMD"


# ─── 安全#4：XFF 取最右侧 IP ───────────────────────────


class TestXffLastHop:
    def test_client_forged_header_returns_rightmost(self):
        """客户端伪造的第一个 IP 必须被忽略，取最右一跳。"""
        request = SimpleNamespace(
            headers={"x-forwarded-for": "10.10.10.10, 172.16.0.1, 192.168.1.100"},
            client=SimpleNamespace(host="127.0.0.1"),
        )
        assert _get_client_id(request, trust_proxy_headers=True) == "192.168.1.100"

    def test_single_ip(self):
        request = SimpleNamespace(
            headers={"x-forwarded-for": "1.2.3.4"},
            client=SimpleNamespace(host="127.0.0.1"),
        )
        assert _get_client_id(request, trust_proxy_headers=True) == "1.2.3.4"

    def test_no_proxy_headers_uses_client(self):
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.1"))
        assert _get_client_id(request, trust_proxy_headers=True) == "10.0.0.1"


# ─── Bug#6：缓存 _RefLock 引用计数 + 清理安全 ────────────


class TestRefLockRefCount:
    @pytest.mark.asyncio
    async def test_cleanup_does_not_reclaim_lock_while_coroutine_waiting(self, monkeypatch):
        """并发请求持有锁引用时，cleanup 不回收锁对象。"""
        monkeypatch.setattr(cache, "_redis_client", None)
        monkeypatch.setattr(cache, "_redis_available", False)
        cache._memory_cache.clear()
        cache._cache_locks.clear()

        entered = asyncio.Event()
        block = asyncio.Event()
        calls = 0

        @cache.cached(ttl=300)
        async def slow_func():
            nonlocal calls
            calls += 1
            entered.set()
            await block.wait()

        # 启动两个并发请求
        t1 = asyncio.create_task(slow_func())
        await entered.wait()

        # t1 正在执行中；启动 t2（会在锁上排队）
        t2 = asyncio.create_task(slow_func())

        # 让 t2 进入等待队列
        await asyncio.sleep(0.05)

        # 此时锁 refs == 2，清理不应回收
        cache.cleanup_memory_cache()
        assert len(cache._cache_locks) == 1, "清理不应回收正在使用的锁"

        # 释放 t1
        block.set()
        await asyncio.gather(t1, t2)
        assert calls == 1, "只应执行一次"

        # 执行完成后缓存已写入；此时 cleanup 可以回收
        cache.cleanup_memory_cache()
        assert len(cache._cache_locks) == 1, "缓存条目未过期，锁应保留"

        # 手动过期后清理应回收
        for v in cache._memory_cache.values():
            v["expires_at"] = 0
        cache.cleanup_memory_cache()
        assert len(cache._cache_locks) == 0, "过期后锁应被回收"


# ─── Bug#7：RFM 数据不可用 → 异常而非缓存 ────────────────


class TestRfmDataUnavailable:
    @pytest.mark.asyncio
    async def test_no_orders_raises_error_not_dict(self, monkeypatch):
        """数据库无订单时应抛 RfmDataUnavailableError，不返回 error 字典。"""
        def fake_max_date(_stmt):
            result = SimpleNamespace(scalar=lambda: None)
            return result

        async def fake_execute(_stmt):
            return fake_max_date(_stmt)

        fake_db = SimpleNamespace(execute=fake_execute)
        with pytest.raises(RfmDataUnavailableError, match="无订单数据"):
            await _build_rfm_snapshot(fake_db)

    @pytest.mark.asyncio
    async def test_error_not_cached_by_decorator(self, monkeypatch):
        """RfmDataUnavailableError 必须穿透 @cached，不会被错误结果填充缓存。"""
        monkeypatch.setattr(cache, "_redis_client", None)
        monkeypatch.setattr(cache, "_redis_available", False)
        clear_rfm_snapshot_cache()
        cache.clear()

        call_count = 0

        async def fake_build(db, **kw):
            nonlocal call_count
            call_count += 1
            raise RfmDataUnavailableError("测试：无数据")

        monkeypatch.setattr(
            "backend.services.rfm_service._get_rfm_snapshot",
            fake_build,
        )

        # 装饰器 @cached(ttl=600) 在 compute_rfm 上（无 lru_cache 层）
        from backend.services.rfm_service import compute_rfm

        for _ in range(3):
            with pytest.raises(RfmDataUnavailableError):
                await compute_rfm(object())

        assert call_count == 3, "异常不应被缓存，每次都应重新执行"


# ─── Bug#9：RFM distinct 计数口径 ─────────────────────────


class TestRfmDistinctCount:
    @pytest.mark.asyncio
    async def test_sql_uses_distinct_order_no(self, monkeypatch):
        """确认 _fetch_rfm_raw 的 SQL 使用 count(DISTINCT order_no)。"""
        captured_stmt = None

        async def fake_execute(stmt):
            nonlocal captured_stmt
            captured_stmt = stmt
            return SimpleNamespace(all=list)

        fake_db = SimpleNamespace(execute=fake_execute)
        import datetime
        await _fetch_rfm_raw(fake_db, datetime.date(2025, 6, 1))

        sql_str = str(captured_stmt).lower()
        assert "count" in sql_str, "应包含 count 聚合"
        assert "distinct" in sql_str, "应使用 distinct 去重"


# ─── 错误信息脱敏 ───────────────────────────────────────


class TestSanitizeError:
    def test_dsn_password_is_masked(self):
        from backend.utils.text_cleaner import sanitize_error
        exc = Exception(
            "Can't connect to MySQL: mysql+pymysql://root:MySecret123@db:3306/shop"
        )
        sanitized = sanitize_error(exc)
        assert "MySecret123" not in sanitized
        assert "***" in sanitized

    def test_no_dsn_unchanged(self):
        from backend.utils.text_cleaner import sanitize_error
        exc = Exception("timeout after 30s")
        assert sanitize_error(exc) == "timeout after 30s"


# ─── AI SQL 只读账户连接串 ───────────────────────────────


class TestAiDatabaseUrl:
    def test_ai_url_uses_dedicated_account(self, monkeypatch):
        s = Settings(
            _env_file=None,
            db_user="root",
            db_password="rootpw",
            ai_db_user="ea_ai",
            ai_db_password="ai_pw_only_select",
        )
        url = s.ai_database_url
        assert "ea_ai" in url
        assert "ai_pw_only_select" in url
        assert "root" not in url

    def test_ai_url_fallback_to_main_account(self):
        s = Settings(
            _env_file=None,
            db_user="root",
            db_password="rootpw",
            ai_db_user="",
            ai_db_password="",
        )
        url = s.ai_database_url
        assert "root" in url
        assert "rootpw" in url


# ─── 明文密码生产环境拒绝 ───────────────────────────────


class TestPlaintextPasswordProduction:
    def test_production_rejects_plaintext(self):
        """生产环境必须使用 bcrypt 哈希密码，明文直接拒绝。"""
        with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
            Settings(
                _env_file=None,
                debug=False,
                jwt_secret="a" * 48,
                admin_password="plain_text_password",
            )


# ─── sql_guard 只读拦截不变（回归） ─────────────────────


class TestSqlGuardRegression:
    @pytest.mark.parametrize("sql", [
        "SELECT 1",
        "WITH totals AS (SELECT 1 AS n) SELECT n FROM totals",
    ])
    def test_safe_queries_still_pass(self, sql):
        assert is_read_only_sql(sql) is True

    @pytest.mark.parametrize("sql", [
        "DELETE FROM orders",
        "SELECT 1; DROP TABLE orders",
        "SELECT SLEEP(10)",
    ])
    def test_unsafe_queries_still_blocked(self, sql):
        assert is_read_only_sql(sql) is False
