"""按权限对返回给客户端的用户名做字段级脱敏。

用户名属于客户 PII：viewer 只需要看板聚合与订单明细，不需要完整姓名。
拥有 data:pii 的 analyst/admin 仍返回原文；导出接口本身要求 data:export，
不在此处重复处理。
"""

from __future__ import annotations

from collections.abc import Iterable

from backend.utils.auth import SCOPE_DATA_PII


def has_pii_access(scopes: Iterable[str]) -> bool:
    """调用方是否可见完整用户名。"""
    return SCOPE_DATA_PII in scopes


def mask_user_name(name: str | None) -> str | None:
    """保留可辨识的最短前缀，其余替换为 *。

    中文姓名保留姓（张*），英文/数字保留前两位（Al***，短名退化为 A*）。
    空值与空白原样返回，避免把"无用户名"显示成脱敏符号。
    """
    if not name or not name.strip():
        return name
    value = name.strip()
    if len(value) == 1:
        # 单字名保留任何一个字符都等于泄露全名。
        return "*"
    if all(ord(ch) < 128 for ch in value):
        keep = 1 if len(value) == 2 else 2
        return value[:keep] + "*" * (len(value) - keep)
    return value[0] + "*" * (len(value) - 1)
