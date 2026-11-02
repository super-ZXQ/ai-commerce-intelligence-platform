"""长期记忆的领域类型、确定性偏好提取与情节摘要/召回辅助。

P1 偏好：用户说“记住 / 以后 / 默认”时才提取，且值必须命中白名单，
避免把一次性筛选条件误存成长期偏好，也不接收自由文本。
P2 情节：单轮问答压缩成卡片，仅在用户回指历史时召回，供向量或关键词检索。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MEMORY_LABELS: dict[str, str] = {
    "default_platform": "默认平台",
    "default_metric": "默认指标",
    "default_dimension": "默认拆解维度",
    "role": "用户角色",
}

MEMORY_VALUES: dict[str, tuple[str, ...]] = {
    "default_platform": ("APP", "微信公众号", "Web网站", "其他"),
    "default_metric": ("付款金额", "订单量", "客单价", "退款率"),
    "default_dimension": ("平台", "商品", "用户"),
    "role": ("运营", "财务", "分析师", "管理层"),
}

_ALIASES: dict[str, dict[str, str]] = {
    "default_platform": {
        "app": "APP",
        "微信公众号": "微信公众号",
        "公众号": "微信公众号",
        "web网站": "Web网站",
        "web": "Web网站",
        "网站": "Web网站",
        "其他平台": "其他",
    },
    "default_metric": {
        "付款金额": "付款金额",
        "销售额": "付款金额",
        "成交额": "付款金额",
        "订单量": "订单量",
        "订单数": "订单量",
        "客单价": "客单价",
        "退款率": "退款率",
    },
    "default_dimension": {
        "平台": "平台",
        "商品": "商品",
        "用户": "用户",
    },
    "role": {
        "数据分析师": "分析师",
        "分析师": "分析师",
        "运营": "运营",
        "财务": "财务",
        "管理层": "管理层",
    },
}

_EXPLICIT_MARKERS = ("记住", "请记住", "帮我记住", "以后", "今后", "默认")
_STRONG_MARKERS = ("记住", "请记住", "帮我记住")
_INTERROGATIVE_PATTERN = re.compile(r"是什么|有哪些|怎么|如何|多少|哪个|哪一|是否|能不能|可以吗")


@dataclass(frozen=True, slots=True)
class MemoryFact:
    key: str
    value: str
    confidence: float


@dataclass(frozen=True, slots=True)
class MemoryTurn:
    owner: str
    thread_id: str
    request_id: str
    question: str


@dataclass(frozen=True, slots=True)
class EpisodeSummary:
    episode_id: int
    summary: str
    score: float
    created_at: str


@dataclass(frozen=True, slots=True)
class EpisodeTurn:
    owner: str
    thread_id: str
    request_id: str
    question: str
    answer: str
    intent: str
    sql: str | None = None


_EPISODE_CUES = ("上次", "之前", "前面", "刚才", "上一轮", "历史", "那个分析", "继续上次")


def needs_episode_recall(query: str) -> bool:
    """只有用户明确回指历史时才检索情节，避免每轮都做向量查询。"""
    return any(cue in query for cue in _EPISODE_CUES)


def build_episode_summary(turn: EpisodeTurn) -> str:
    """把单轮问答压缩成情节卡片；不保存完整对话和结果集。"""
    question = re.sub(r"\s+", " ", turn.question).strip()
    answer = re.sub(r"\s+", " ", turn.answer).strip()
    if not question or not answer or answer.startswith("⚠️"):
        return ""
    sql = re.sub(r"\s+", " ", turn.sql or "").strip()
    parts = [f"问题：{question[:160]}", f"结论：{answer[:240]}"]
    if sql:
        parts.append(f"SQL：{sql[:300]}")
    return "\n".join(parts)[:800]


def _terms(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text.lower())
    latin = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    stop_terms = {"什么", "定义", "含义", "如何", "怎么", "计算", "公式", "标准", "范围", "哪些"}
    bigrams = {chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))}
    return latin | (bigrams - stop_terms)


def keyword_score(query: str, text: str) -> float:
    """向量不可用时的降级打分：中文 bigram + 英文 token 重叠率。"""
    query_terms = _terms(query)
    if not query_terms:
        return 0.0
    return len(query_terms & _terms(text)) / len(query_terms)


def format_memory_context(
    facts: list[MemoryFact],
    *,
    episodes: list[EpisodeSummary] | None = None,
    max_chars: int = 600,
) -> str:
    """把偏好与历史情节渲染成提示词片段；始终标注为参考信息。"""
    if (not facts and not episodes) or max_chars <= 0:
        return ""
    sections: list[str] = []
    if facts:
        lines = [f"- {MEMORY_LABELS.get(fact.key, fact.key)}：{fact.value}" for fact in facts]
        sections.append("用户长期偏好（仅作参考；当前问题明确指定时，以当前问题为准）：\n" + "\n".join(lines))
    if episodes:
        lines = []
        for episode in episodes[:3]:
            summary = re.sub(r"\s+", " ", episode.summary).strip()[:160]
            suffix = f"（相关度 {episode.score:.2f}）" if episode.score > 0 else ""
            lines.append(f"- {episode.created_at}{suffix}：{summary}")
        sections.append("相关历史分析（仅作参考；当前问题优先）：\n" + "\n".join(lines))
    text = "\n".join(sections)
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return text[: max_chars - 1] + "…"


def _matches(text: str, alias: str) -> bool:
    """英文别名要求词边界，避免 app 命中 happy 之类的无关单词。"""
    if alias.isascii() and alias.isalpha():
        return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) is not None
    return alias in text


def extract_memories(query: str) -> list[MemoryFact]:
    """从显式偏好语句中提取白名单记忆；没有明确标记时一律不写。"""
    text = re.sub(r"\s+", " ", query.strip().lower())
    if not text or not any(marker in text for marker in _EXPLICIT_MARKERS):
        return []
    # 疑问句不是稳定偏好，避免“默认平台是什么”“默认看订单量吗”被误存。
    if re.search(r"[?？]", text) or re.search(r"(?:吗|呢)$", text) or _INTERROGATIVE_PATTERN.search(text):
        return []

    confidence = 0.95 if any(marker in text for marker in _STRONG_MARKERS) else 0.80
    facts: list[MemoryFact] = []
    platform_values = {
        canonical
        for alias, canonical in _ALIASES["default_platform"].items()
        if _matches(text, alias)
    }

    for key, aliases in _ALIASES.items():
        matched = {
            canonical
            for alias, canonical in aliases.items()
            if _matches(text, alias)
        }
        # “平台”既可能是偏好值，也可能是拆解维度；同时出现时不做维度猜测。
        if key == "default_dimension" and platform_values:
            matched.discard("平台")
        if len(matched) != 1:
            continue
        facts.append(MemoryFact(key=key, value=matched.pop(), confidence=confidence))

    return facts
