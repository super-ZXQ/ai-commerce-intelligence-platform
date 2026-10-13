"""共享 SQLAlchemy 表结构描述，供 API 与 Streamlit 注入 Agent。"""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from agent_core.sql_safety import ORDER_COLUMNS


def describe_table(engine: Engine, table_name: str) -> str:
    """返回适合 Text-to-SQL 提示词的紧凑表结构。"""
    columns = inspect(engine).get_columns(table_name)
    if not columns:
        raise ValueError(f"数据表不存在或没有字段：{table_name}")
    definitions = []
    for column in columns:
        if column["name"] not in ORDER_COLUMNS:
            continue
        nullable = "" if column.get("nullable", True) else " NOT NULL"
        definitions.append(f"  {column['name']} {column['type']}{nullable}")
    return f"CREATE TABLE {table_name} (\n" + ",\n".join(definitions) + "\n)"
