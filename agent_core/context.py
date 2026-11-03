"""按字符预算裁剪会话历史与出域数据，避免固定条数与提示词实际窗口不一致。"""

from __future__ import annotations

import json
from collections.abc import Sequence

from agent_core.session import Message

DEFAULT_HISTORY_MAX_CHARS = 2000
DEFAULT_MESSAGE_MAX_CHARS = 600
DEFAULT_PROMPT_ROWS_MAX_CHARS = 4000
_ROLE_OVERHEAD = 12
_ELLIPSIS = "…"


def fit_json_rows(rows: Sequence[object], max_chars: int) -> tuple[str, int]:
    """把结果序列化成 JSON 数组，按字符预算尽量多装整行。

    逐行累加而不是先序列化再切字符串：切在中间会产生非法 JSON，模型会把半截
    数据当成完整数据。一行都装不下时退化为截断首行并明确标注，至少让模型知道
    存在数据；静默发送空数组会让它回答「查询结果为空」，那是错误结论。
    """
    kept: list[str] = []
    used = 2  # 一对方括号
    for row in rows:
        text = json.dumps(row, ensure_ascii=False, default=str)
        separator = 1 if kept else 0
        if used + separator + len(text) > max_chars:
            break
        kept.append(text)
        used += separator + len(text)
    if kept:
        return "[" + ",".join(kept) + "]", len(kept)
    if not rows:
        return "[]", 0
    truncated = json.dumps(rows[0], ensure_ascii=False, default=str)[: max(1, max_chars - 20)]
    return f"{truncated}…（超长单行已截断）", 0
# 不参与问答配对、始终留在窗口里的通道。reference 是记忆检索资料：它必须比
# system 弱（模型对 "system" 标签有训练出来的指令权威），但又不能因为不是
# user/assistant 就被 _turns 当成孤立消息丢掉。
_PRESERVED_ROLES = frozenset({"system", "reference"})


def _clip(text: str, limit: int) -> str:
    """保留前 limit 个字符；超长时用省略号收尾，避免把半句话当完整上下文。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return _ELLIPSIS
    return text[: limit - 1] + _ELLIPSIS


def _cost(message: Message) -> int:
    """按角色名 + 内容 + JSON 结构开销估算字符成本。"""
    return len(str(message.get("content", ""))) + len(str(message.get("role", ""))) + _ROLE_OVERHEAD


def _turns(history: list[Message]) -> list[list[Message]]:
    """把消息还原成完整问答轮；残缺消息跳过，避免出现孤立的 assistant 消息。"""
    turns: list[list[Message]] = []
    index = 0
    while index + 1 < len(history):
        user, assistant = history[index], history[index + 1]
        if user.get("role") == "user" and assistant.get("role") == "assistant":
            turns.append([user, assistant])
            index += 2
        else:
            index += 1
    return turns


def build_history_window(
    history: list[Message],
    *,
    max_chars: int = DEFAULT_HISTORY_MAX_CHARS,
    max_message_chars: int = DEFAULT_MESSAGE_MAX_CHARS,
) -> list[Message]:
    """从最近一轮向前装载完整问答，直到字符预算用尽。

    先按单条上限裁剪，防止一条超长回答吃掉全部预算；再从最新轮向前
    装入，放不下的旧轮次整轮丢弃。最新一轮即使超预算也保留：优先保问题，
    剩余空间再给回答。保留通道（system 与 reference 记忆资料）不参与轮次配对，
    始终保留。
    """
    if max_chars <= 0 or max_message_chars <= 0:
        return []

    normalized: list[Message] = [
        {"role": str(message.get("role", "")), "content": _clip(str(message.get("content", "")), max_message_chars)}
        for message in history
        if message.get("role") and message.get("content")
    ]
    preserved = [message for message in normalized if message["role"] in _PRESERVED_ROLES]
    turns = _turns([message for message in normalized if message["role"] not in _PRESERVED_ROLES])

    selected: list[list[Message]] = []
    # 保留消息始终留下，但必须占用同一份预算；否则长期偏好会无条件
    # 叠加在 max_chars 之上，调用方按预算估算的提示词长度会失效。
    remaining = max_chars - sum(_cost(message) for message in preserved)
    if remaining <= 0:
        return preserved
    for turn in reversed(turns):
        cost = sum(_cost(message) for message in turn)
        if cost <= remaining:
            selected.append(turn)
            remaining -= cost
            continue
        if selected:
            break
        # 最新一轮放不下：先保证问题进入窗口，再用剩余预算装回答。
        user, assistant = turn
        user_limit = remaining - len(str(user["role"])) - _ROLE_OVERHEAD
        if user_limit < 2:
            break
        fitted_user: Message = {"role": "user", "content": _clip(str(user["content"]), user_limit)}
        partial: list[Message] = [fitted_user]
        assistant_limit = (
            remaining
            - _cost(fitted_user)
            - len(str(assistant["role"]))
            - _ROLE_OVERHEAD
        )
        if assistant_limit >= 2:
            partial.append({"role": "assistant", "content": _clip(str(assistant["content"]), assistant_limit)})
        selected.append(partial)
        break

    return [
        *preserved,
        *(message for turn in reversed(selected) for message in turn),
    ]
