"""生产AI连接必须只有orders业务列SELECT，不能依赖账号命名判断权限。"""
import re

from sqlalchemy import text

from agent_core.sql_safety import ORDER_COLUMNS


def verify_ai_permissions(engine) -> None:
    target = f"`{engine.url.database}`.`orders`".lower()
    with engine.connect() as connection:
        grants = [row[0] for row in connection.execute(text("SHOW GRANTS FOR CURRENT_USER"))]
    has_select = False
    for grant in grants:
        if "WITH GRANT OPTION" in grant.upper():
            raise ValueError("AI账号不能有授权权限")
        if grant.upper().startswith("GRANT USAGE ON *.* TO "):
            continue
        match = re.match(r"GRANT SELECT(?: \(([^)]+)\))? ON (.+?) TO ", grant, re.IGNORECASE)
        if not match or match[2].lower() != target:
            raise ValueError("AI账号权限超出orders业务表只读范围（含角色授权）")
        if match[1] and {name.strip().strip("`").lower() for name in match[1].split(",")} - ORDER_COLUMNS:
            raise ValueError("AI账号包含未许可字段的读取权限")
        has_select = True
    if not has_select:
        raise ValueError("AI账号缺少必要的SELECT权限")
