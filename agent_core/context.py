"""按字符预算裁剪会话历史，避免固定条数与提示词实际窗口不一致。"""

from __future__ import annotations

from agent_core.session import Message

DEFAULT_HISTORY_MAX_CHARS = 2000
DEFAULT_MESSAGE_MAX_CHARS = 600
_ROLE_OVERHEAD = 12
_ELLIPSIS = "…"


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
    剩余空间再给回答。system 消息（例如长期偏好）不参与轮次配对，始终保留。
    """
    if max_chars <= 0 or max_message_chars <= 0:
        return []

    normalized: list[Message] = [
        {"role": str(message.get("role", "")), "content": _clip(str(message.get("content", "")), max_message_chars)}
        for message in history
        if message.get("role") and message.get("content")
    ]
    system_messages = [message for message in normalized if message["role"] == "system"]
    turns = _turns([message for message in normalized if message["role"] != "system"])

    selected: list[list[Message]] = []
    remaining = max_chars
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
        *system_messages,
        *(message for turn in reversed(selected) for message in turn),
    ]
