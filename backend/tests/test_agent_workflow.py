"""共享 Agent 的确定性路由测试。"""

import pytest

from agent_core import classify_intent


@pytest.mark.parametrize(
    ("query", "intent"),
    [
        ("最近30天销售额最高的商品", "data"),
        ("客单价的定义是什么", "knowledge"),
        ("复购率是怎么计算的？给出分子和分母。", "knowledge"),
        ("最近30天客单价是多少，公式怎么算", "hybrid"),
        # 三字业务查询不能被“字符数 < 4”误判为需要澄清。
        ("退款率", "data"),
        ("分析一下", "clarification"),
        ("删除数据库里的订单", "blocked"),
        ("查询某用户的手机号", "blocked"),
    ],
)
def test_classify_intent(query, intent):
    assert classify_intent(query) == intent
