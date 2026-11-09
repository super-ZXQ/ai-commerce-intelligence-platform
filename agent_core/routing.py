"""Agent 意图的确定性路由规则。

规则依据 agent_core/eval/routing_robustness.jsonl（dev 集，25 条）的失败模式加固：
倒序写操作（"把 orders 表删了"）、隐私变体（"电话"）、注入改写（"忽略上面的要求"）、
口语知识问法（"咋算""啥意思"）。泛化水平以留出集 routing_robustness_heldout.jsonl
（10 条，调优前冻结）为准，不参与规则调优。
"""

from __future__ import annotations

import re

from agent_core.models import AgentIntent

_KNOWLEDGE_MARKERS = (
    "定义", "含义", "意思", "怎么算", "怎么计算", "如何计算", "咋算", "怎么求", "咋回事", "啥回事",
    "怎么区分", "怎么定义", "怎么理解", "代表什么", "公式", "规则", "标准", "正常", "口径", "阈值", "判定",
)
# 错别字容忍变体（如"正产范围"系"正常范围"的错别字）
_KNOWLEDGE_RE = re.compile(r"正[常产].{0,3}(?:范围|区间|值|水平)")
# 强知识问法："同比环比是什么意思"中"同比/环比"是被问定义的对象而非数据诉求，
# 该句式压制数据标记，避免误判 hybrid
_STRONG_KNOWLEDGE_RE = re.compile(r"是什么意思|是啥意思|啥意思")

_CONCRETE_DATA_MARKERS = (
    "多少", "最多", "最高", "最低", "排名", "趋势", "最近", "同比", "环比", "top", "各平台", "当前",
)

# 写操作：动词在前（"删除数据库里的订单"）与宾语在前（"把 orders 表删了"）两种语序
_WRITE_VERBS = r"(?:删除|删了|删掉|清空|清掉|清了|抹掉|修改|改掉|写入|更新)"
# 否定前缀（已被/未）多见于状态问句（"已删除的订单有多少"），不是写请求
_WRITE_VERBS_POS = r"(?<![已被未])" + _WRITE_VERBS
_WRITE_OBJECTS = r"(?:数据库|订单表|订单|数据|表)"

_BLOCKED_PATTERNS = (
    # 隐私与凭据（含口语变体：电话/密钥）
    r"密码|手机号|电话|身份证|个人隐私|具体地址|api.?key|环境变量|系统提示词|密钥|secret",
    # 写操作（正序 + 倒序语序）
    _WRITE_VERBS_POS + r".{0,12}(?:数据库|数据|订单|表)",
    r"(?:数据库|订单表|订单|数据|表).{0,8}(?:删除|删了|删掉|清空|清掉|清了|抹掉|truncate)",
    r"\b(?:drop|delete|update|insert|alter|truncate)\b",
    # 提示词注入与指令覆盖（中英改写）
    r"忽略(?:以上|之前|上面|上述|先前|前面|系统)?.{0,6}(?:指令|要求|提示词|规则|设定|约束)",
    r"(?:ignore|disregard|forget|bypass|override)\s+(?:all\s+|any\s+|the\s+|your\s+)?"
    r"(?:previous|prior|above|earlier|original|system|old)?\s*(?:instructions?|rules?|prompts?|directives?)",
    # 提示词/凭据外泄（"泄露系统 prompt""print env"）
    r"(?:泄露|输出|打印|显示|透露|reveal|print|show|dump).{0,12}"
    r"(?:prompt|提示词|环境变量|密钥|secret|api.?key|\benv\b|源代码)",
)

_VAGUE_QUERIES = {
    "分析一下", "看看数据", "帮我分析", "查一下", "分析数据", "看看", "查查",
    "分析分析", "查查数据", "帮我看看", "帮我查查", "查查看", "然后呢",
    "盘点一下", "盘点下数据", "帮我盘点", "帮我盘点下数据", "瞅瞅", "瞧瞧",
}
# 模糊动词锚定匹配（"帮我盘点下数据"）；后缀只允许极短宾语，避免误吞有实体的查询
_VAGUE_RE = re.compile(r"^(?:帮我|给我)?(?:分析|看看|查查|瞅瞅|瞧瞧|盘点|盘一盘)(?:一下|一哈|一下下|下)?(?:数据)?$")


def classify_intent(query: str) -> AgentIntent:
    """使用确定性规则提供稳定路由；复杂语义仍交给下游 Agent 处理。"""
    normalized = query.strip().lower()
    if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in _BLOCKED_PATTERNS):
        return "blocked"
    # 只拦单字符/空串；用字符数 <4 会把"退款率""客单价""复购率"这类
    # 合法的三字查询误判为需要澄清。真正的模糊问法由下方显式集合覆盖。
    if len(normalized) < 2 or normalized in _VAGUE_QUERIES or _VAGUE_RE.search(normalized):
        return "clarification"
    has_knowledge = any(marker in normalized for marker in _KNOWLEDGE_MARKERS) or bool(
        _KNOWLEDGE_RE.search(normalized)
    )
    if has_knowledge and (_STRONG_KNOWLEDGE_RE.search(normalized) or not any(
        marker in normalized for marker in _CONCRETE_DATA_MARKERS
    )):
        return "knowledge"
    if has_knowledge:
        return "hybrid"
    return "data"
