"""共享 SQLAlchemy 表结构描述，供 API 与 Streamlit 注入 Agent。"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from agent_core.sql_safety import ORDER_COLUMNS


def describe_table(engine: Engine, table_name: str) -> str:
    """返回适合 Text-to-SQL 提示词的紧凑表结构。"""
    if engine.dialect.name == "mysql":
        # 列级SELECT账号无SHOW CREATE TABLE权限，元数据查询只返回其可见字段。
        with engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table_name ORDER BY ORDINAL_POSITION"
            ), {"table_name": table_name}).all()
        columns = [{"name": name, "type": kind, "nullable": nullable == "YES"}
                   for name, kind, nullable in rows]
    else:
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
