"""结果集出域前按 SQL 投影来源假名化。

SQL 已经执行完、行数据既要返回前端、又要拼进发给外部模型提供商的提示词。
本模块只服务后者：返回一份被假名化的副本，原始行不受影响，前端仍看到真实值。
假名按服务端密钥派生，同一主体在不同请求里保持稳定，模型才能做分组与去重推理。

假名只在出域方向生效，返回用户前用 PromptMasker.restore 换回原值；会话历史里
保存的仍是假名，避免下一轮把上一轮答案中的客户名再发一次给模型提供商。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.lineage import Node, lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope

# (列名匹配, 假名前缀)。列名可能是 SQL 里的中文别名，所以按关键词匹配而非白名单枚举。
# 前缀是类别而不是列名：「客户」「买家」「user_name」都归一成「用户#…」，
# 这样同一主体在不同列名下也能被模型识别为同一个体。
_PII_FIELDS = (
    (re.compile(r"user_?name|customer|buyer|用户名|客户|买家|顾客", re.IGNORECASE), "用户"),
    (re.compile(r"order_?(?:id|no)|订单号|订单编号", re.IGNORECASE), "订单"),
    (re.compile(r"phone|mobile|tel|email|address|手机|电话|邮箱|地址", re.IGNORECASE), "联系"),
)
# 只有这些物理字段可作为普通业务数值或维度外发。新增字段默认假名化。
_PUBLIC_ORDER_COLUMNS = frozenset({
    "product_id", "order_amount", "payment_amount", "channel_id", "platform_type",
    "order_time", "payment_time", "is_refund", "discount_amount", "payment_duration_sec",
    "order_date", "order_hour", "weekday", "order_status", "event_version", "updated_at",
})
_ORDER_IDENTIFIERS = {"user_name": "用户", "order_id": "订单", "order_seq_id": "订单"}
_ORDER_SCHEMA = {"orders": {name: "UNKNOWN" for name in sorted(_PUBLIC_ORDER_COLUMNS | _ORDER_IDENTIFIERS.keys())}}
_DIGEST_PREFIX = 8
_KEY_INFO = b"agent-core:prompt-pii-mask:v1"

# 调用方未提供密钥时的进程内兜底：保证任何部署都不会退化成可离线还原的无盐摘要。
_EPHEMERAL_KEY = secrets.token_bytes(32)


def derive_masking_key(secret: str) -> bytes:
    """从服务端密钥派生本用途专用密钥，避免直接复用认证密钥。"""
    if not secret:
        return _EPHEMERAL_KEY
    return hmac.new(secret.encode("utf-8"), _KEY_INFO, hashlib.sha256).digest()


def pseudonym(label: str, value: str, key: bytes) -> str:
    digest = hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{label}#{digest[:_DIGEST_PREFIX]}"


def _label_for(name: str) -> str | None:
    return next((label for pattern, label in _PII_FIELDS if pattern.search(name)), None)


def _mask_value(label: str, value: str, key: bytes, sink: dict[str, str] | None) -> str:
    token = pseudonym(label, value, key)
    if sink is not None:
        sink[token] = value
    return token


def _mask_node(name: str, value: Any, key: bytes, sink: dict[str, str] | None) -> Any:
    """sink 收集「假名 -> 原值」；为 None 时只脱敏、不记录（无法还原的调用方）。"""
    if isinstance(value, Mapping):
        return {child: _mask_node(str(child), item, key, sink) for child, item in value.items()}
    if isinstance(value, list):
        return [_mask_node(name, item, key, sink) for item in value]
    if isinstance(value, str) and value:
        label = _label_for(name)
        if label is None:
            # 非敏感列的字符串（平台名、日期等）保持原样，否则答案无法阅读。
            return value
        return _mask_value(label, value, key, sink)
    # 数字、日期等非字符串一律原样保留：它们承载统计语义，且几乎不可能反查身份。
    return value


def _projection_label(node: Node) -> str | None:
    if isinstance(node.expression, exp.Table):
        column = node.name.rsplit(".", 1)[-1].strip('`"').lower()
        return None if column in _PUBLIC_ORDER_COLUMNS else _ORDER_IDENTIFIERS.get(column, "字段")
    if isinstance(node.expression, exp.Placeholder) or (
        not node.downstream and (node.expression.is_star or node.expression.find(exp.Column) is not None)
    ):
        return "字段"
    labels = {label for child in node.downstream if (label := _projection_label(child)) is not None}
    return next(iter(labels)) if len(labels) == 1 else ("字段" if labels else None)


def _projection_labels(sql: str) -> tuple[dict[str, str | None], bool] | None:
    """返回输出列的来源标签，以及能否按位置匹配数据库生成的表达式列名。"""
    tree = parse_one(sql, read="mysql")
    if not isinstance(tree, exp.Query):
        return {}, False
    if tree.find(exp.Table) is None:
        return None
    explicit_projections = not any(select.is_star for select in tree.selects)
    qualified = qualify(tree, dialect="mysql", schema=_ORDER_SCHEMA, identify=False)
    # COUNT 只产生数量。仅在追溯用的副本上切断该依赖，客单价等派生指标仍保留数值。
    for count in list(qualified.find_all(exp.Count)):
        count.replace(exp.Literal.number(0))
    scope = build_scope(qualified)
    if scope is None:
        return {}, False
    projections = lineage(None, qualified, dialect="mysql", schema=_ORDER_SCHEMA, scope=scope)
    labels = {name.casefold(): _projection_label(node) for name, node in projections.items()}
    if len(labels) != len(scope.expression.selects):
        return {}, False
    return labels, explicit_projections


class PromptMasker:
    """一次请求内的假名映射：出域用假名，回给用户前按映射还原。"""

    def __init__(self, masking_key: bytes | None = None):
        self._key = masking_key or _EPHEMERAL_KEY
        self._originals: dict[str, str] = {}

    def mask(self, payload: Any) -> Any:
        return _mask_node("", payload, self._key, self._originals)

    def mask_rows(self, rows: list[dict], sql: str | None) -> list[dict]:
        """对业务 SQL 追溯别名、CTE 和表达式，无法确认来源的值按敏感处理。"""
        if not sql or not rows:
            return self.mask(rows)
        try:
            projections = _projection_labels(sql)
        except (SqlglotError, RecursionError):
            projections = {}, False
        if projections is None:
            return self.mask(rows)

        labels, explicit = projections
        ordered_labels = tuple(labels.values())
        masked = []
        for row in rows:
            positional = ordered_labels if explicit and len(row) == len(labels) else ()
            safe_row = {}
            for index, (name, value) in enumerate(row.items()):
                label = labels.get(name.casefold(), positional[index] if positional else "字段")
                if label and value is not None:
                    safe_row[name] = _mask_value(label, str(value), self._key, self._originals)
                else:
                    safe_row[name] = _mask_node(name, value, self._key, self._originals)
            masked.append(safe_row)
        return masked

    def restore(self, text: str) -> str:
        """把文本里的假名换回原值。

        只替换本轮出域时记录过的假名；模型若改写了假名（漏字符、加空格）就保留
        原样——宁可答案里出现一串不可读的假名，也不能猜错客户名。
        """
        if not text or not self._originals:
            return text
        for token, original in self._originals.items():
            if token in text:
                text = text.replace(token, original)
        return text


_ACTIVE: ContextVar[PromptMasker | None] = ContextVar("agent_prompt_masker", default=None)


@contextmanager
def masking_scope(masking_key: bytes | None = None) -> Iterator[PromptMasker]:
    """打开本轮请求的假名域；域内所有出域提示词共用一份映射。"""
    masker = PromptMasker(masking_key)
    token = _ACTIVE.set(masker)
    try:
        yield masker
    finally:
        _ACTIVE.reset(token)


def active_masker() -> PromptMasker | None:
    return _ACTIVE.get()


def mask_for_prompt(payload: Any, *, masking_key: bytes | None = None) -> Any:
    """无请求作用域时的便捷封装：只脱敏，不承诺可还原。"""
    return PromptMasker(masking_key).mask(payload)
