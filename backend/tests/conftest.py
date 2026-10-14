"""pytest 共享 fixture。"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest


def pytest_configure(config):
    """Windows 使用 Selector loop，避免 aiomysql 测试连接跨事件循环失效。"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # 认证测试必须与开发机/CI 的生产凭据完全解耦；数据库连接变量仍由调用方提供。
    os.environ["DEBUG"] = "true"
    os.environ["JWT_SECRET"] = "pytest-only-secret-never-use-in-production"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "admin123"
    os.environ["ANALYST_USERNAME"] = "analyst"
    os.environ["ANALYST_PASSWORD"] = "analyst123"
    # 仅测试环境显式允许CI主账号；生产配置仍默认拒绝回退。
    os.environ.setdefault("AI_DB_ALLOW_MAIN_FALLBACK", "true")


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """每个测试前后清空限流计数。

    登录接口默认有限流；测试间共享计数会导致后续 fixture 意外收到 429。
    """
    from backend.utils import rate_limiter
    rate_limiter._rate_store.clear()
    yield
    rate_limiter._rate_store.clear()
