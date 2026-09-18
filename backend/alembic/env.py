"""Alembic 环境：使用 migration 专用连接串，不加载 Agent/AI 只读账号。"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 保证可 import backend.*（repo root 或 backend 父目录）
_BACKEND = Path(__file__).resolve().parents[1]
_ROOT = _BACKEND.parent
for path in (str(_ROOT), str(_BACKEND)):
    if path not in sys.path:
        sys.path.insert(0, path)

from backend.config import get_settings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 同步 DDL 连接串：migration 账号 / 本地管理账号；禁止 ea_ai
settings = get_settings()
# config.attributes 供 env 之外的测试注入 URL
db_url = config.attributes.get("migration_url", None) or os.environ.get(
    "MIGRATION_DATABASE_URL", settings.migration_database_url
)
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = None  # baseline 为手写 SQL DDL，不用 ORM autogenerate


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
