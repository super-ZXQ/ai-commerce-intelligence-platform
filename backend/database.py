import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.config import get_settings
from backend.utils.text_cleaner import sanitize_error

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_async_engine(
    settings.async_database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle,
    # 连接取出前主动探活，避免复用 MySQL 已关闭的空闲连接。
    pool_pre_ping=True,
    echo=settings.debug,
)

# 事件写路径不能复用 API/Agent 的读连接池；部署时它会使用独立最小权限账号。
event_engine = create_async_engine(
    settings.event_async_database_url,
    pool_size=max(1, min(settings.db_pool_size, 5)),
    max_overflow=max(1, min(settings.db_max_overflow, 5)),
    pool_recycle=settings.db_pool_recycle,
    pool_pre_ping=True,
    echo=settings.debug,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
event_session_factory = async_sessionmaker(
    event_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_event_db() -> AsyncSession:
    """订单事件写入专用会话；路由层只在事件摄入接口注入它。"""
    async with event_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_db_connection() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("数据库连接检查失败: %s", sanitize_error(exc))
        return False


async def check_event_db_connection() -> bool:
    try:
        async with event_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("事件数据库连接检查失败: %s", sanitize_error(exc))
        return False
