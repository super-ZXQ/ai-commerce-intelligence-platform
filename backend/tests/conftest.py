"""pytest 共享 fixture。"""
from __future__ import annotations

import asyncio
import sys

import pytest


def pytest_configure(config):
    """P6.10 修复：Windows 默认 ProactorEventLoop 上 aiomysql + pytest-asyncio
    多 test 跑会触发 AttributeError: 'NoneType' object has no attribute 'send'。
    必须在 pytest 启动前改 loop policy，否则 pytest-asyncio 已建好 Proactor loop。"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """每个测试前后清空限流计数。

    P6.10 修复：/api/auth/login 默认 5 次/分钟，pytest 跑多个 test
    会触发 429 导致 authed_client fixture 拿不到 token。
    """
    from backend.utils import rate_limiter
    rate_limiter._rate_store.clear()
    yield
    rate_limiter._rate_store.clear()
