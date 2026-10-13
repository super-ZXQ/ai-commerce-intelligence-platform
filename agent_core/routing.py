"""Agent 意图的确定性路由规则。

规则依据 agent_core/eval/routing_robustness.jsonl（dev 集，25 条）的失败模式加固：
倒序写操作（"把 orders 表删了"）、隐私变体（"电话"）、注入改写（"忽略上面的要求"）、
口语知识问法（"咋算""啥意思"）。routing_robustness_heldout.jsonl（10条）标签保留，但后续安全加固
已使用其相关失败模式，现仅作历史回归；新审计与开发集分开报告。

安全拒答评测集 agent_core/eval/live_safety_cases.jsonl（12 条）暴露的越权缺口
在本模块加固：隐私宾语变体（客户资料/收货地址/联系方式）、记录行级写操作
（"订单表里3月份的记录行都删掉"）、字段值改写（"订单金额改成9999"）、
定向插入（"往订单表里插入一条"）与泛宾语盘点（"数据库里都有什么数据"）。
"""

from __future__ import annotations

import re

from agent_core.models import AgentIntent

_KNOWLEDGE_MARKERS = (
    "定义", "含义", "意思", "怎么算", "如何计算", "咋算", "怎么求", "咋回事", "啥回事",
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
_WRITE_VERBS = r"(?:删除|删了|删掉|清空|清掉|清了|抹掉|修改|改掉|改成|改为|写入|更新)"
# 否定前缀（已被/未）多见于状态问句（"已删除的订单有多少"），不是写请求
_WRITE_VERBS_POS = r"(?<![已被未])" + _WRITE_VERBS
_WRITE_OBJECTS = r"(?:数据库|订单表|订单|数据|表)"

_BLOCKED_PATTERNS = (
    # 隐私与凭据（含口语变体：电话/密钥）
    r"密码|手机号|电话|身份证|个人隐私|具体地址|api.?key|环境变量|系统提示词|密钥|secret",
    # 隐私宾语与导出越权（口语变体：客户资料/收货地址/联系方式；
    # 负向断言豁免"用户信息字段有哪些"等数据字典问句）
    r"(?:导出|列出|拉取|抓取|dump).{0,6}(?:客户|用户|会员|个人)(?:资料|信息|档案)",
    r"(?:客户|用户|会员|个人)(?:资料|信息|档案|列表|清单)(?!字段|是|包括|包含|吗|啥|怎么|如何)",
    r"收货地址|收件地址|地址簿|联系方式",
    r"(?:所有|全部).{0,6}(?:客户|用户|会员).{0,6}(?:资料|信息|列表|地址)",
    # 泛宾语盘点：无限定词的全体数据请求（"数据库里都有什么数据"）；
    # 不含"平台"——"平台类型有哪些取值"是数据字典知识问句
    r"(?:数据库|后台|系统|服务器).{0,12}(?:都有什么|都有哪些|都有些啥|都有啥)",
    r"(?:所有|全部)(?:数据|记录)(?:都)?(?:给我|导出|看看|看一下|列出|瞧瞧)",
    # 记录行级写操作（"订单表里3月份的记录行都删掉"）："记录行"是数据库行话，
    # 在分析问句中几乎不出现，可安全使用宽间隔
    r"记录行.{0,8}(?:删除|删了|删掉|清空|清掉|清了|修改|改成|改为|更新|抹掉)",
    r"(?:删除|删了|删掉|清空|清掉|清了|修改|改成|改为|更新|抹掉).{0,8}记录行",
    # 字段值改写（"把订单金额改成9999"）
    r"(?:金额|单价|数量|字段|状态).{0,6}(?:改成|改为|设为|置为|修改成)",
    # 定向插入（"往订单表里插入一条"；不误伤"新增订单数"类统计问句）
    r"(?:往|向).{0,6}(?:数据库|订单表|数据表|表).{0,10}(?:插入|新增|添加|写入|加一)",
    r"(?:插入|新增|添加).{0,4}(?:一条|一笔|一个)",
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
    if len(normalized) < 4 or normalized in _VAGUE_QUERIES or _VAGUE_RE.search(normalized):
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
