"""Agent 流式执行封装（无 streamlit 依赖，便于单元测试）。

从 AgentExecutor.astream_events 事件流中提取两类实时信号：
1. 工具步骤：on_tool_start / on_tool_end → 回调 on_step，用于进度提示
2. 答案 token：on_chat_model_stream → 经 FinalAnswerDetector 探测
   "Final Answer:" 前缀后，回调 on_token，用于逐 token 渲染

流式仅为 UI 增强，正确性仍以重组的 response dict 为准：
根链路（parent_ids 为空）的 on_chain_end.data.output 与 agent.invoke()
返回结构完全一致，现有后处理（SQL 抽取/图表/RAG 引用来源）零改动。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from langchain_core.agents import AgentAction

FINAL_PREFIX = "Final Answer:"

# 工具名 → 用户可读的进度标签（app.py 渲染用）
TOOL_STEP_LABELS = {
    "sql_db_list_tables": "查看数据表清单",
    "sql_db_schema": "查看表结构",
    "sql_db_query": "执行 SQL 查询",
    "query_business_knowledge": "检索业务知识库",
}


class FinalAnswerDetector:
    """逐 token 探测 "Final Answer:" 前缀的状态机。

    ReAct 循环中每次 LLM 调用都会流出一段 token：
    - 思考步以 "Thought:"/"Action:" 开头，偏离前缀 → rejected，整段忽略
    - 最终步以 "Final Answer:" 开头 → matched，其后 token 实时下发
    - 前缀本身可能跨多个 token（如 "Final" + " Answer:"），在仍是
      候选前缀时继续等待而不是立即判负
    """

    def __init__(self) -> None:
        self.buffer = ""
        self.state = "pending"  # pending | matched | rejected
        self.answer = ""
        self._emitted = False  # 是否已下发首个有效 token

    def feed(self, token: str) -> str | None:
        """喂入一个 token；属于最终答案时返回增量文本，否则返回 None。"""
        if self.state == "rejected":
            return None
        if self.state == "matched":
            if not self._emitted:
                # 前缀后紧邻的空白不下发，保持与 invoke() 解析结果一致
                token = token.lstrip()
                if not token:
                    return None
                self._emitted = True
            self.answer += token
            return token
        # pending：继续累积并判断是否命中/偏离前缀
        self.buffer += token
        stripped = self.buffer.lstrip()
        if stripped.startswith(FINAL_PREFIX):
            self.state = "matched"
            rest = stripped[len(FINAL_PREFIX):].lstrip()
            if rest:
                self._emitted = True
                self.answer = rest
                return rest
            return None
        if FINAL_PREFIX.startswith(stripped):
            return None  # 仍是候选前缀，等待下一个 token
        self.state = "rejected"
        return None


def _chunk_text(chunk: Any) -> str:
    """提取 AIMessageChunk 中的文本；str 与 content-blocks 两种格式都兼容。"""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


async def _astream(
    agent: Any,
    prompt: str,
    on_step: Callable[[dict], None] | None,
    on_token: Callable[[str], None] | None,
) -> dict:
    detectors: dict[str, FinalAnswerDetector] = {}
    tool_runs: dict[str, dict] = {}
    steps: list[tuple[str, Any, Any]] = []  # (tool, tool_input, observation)
    answer_parts: list[str] = []
    root_output: dict | None = None

    async for ev in agent.astream_events({"input": prompt}, version="v2"):
        kind = ev["event"]
        run_id = ev.get("run_id", "")
        if kind == "on_tool_start":
            tool_runs[run_id] = {"tool": ev["name"], "input": ev.get("data", {}).get("input")}
            if on_step:
                on_step({
                    "tool": ev["name"],
                    "label": TOOL_STEP_LABELS.get(ev["name"], ev["name"]),
                    "status": "running",
                })
        elif kind == "on_tool_end":
            info = tool_runs.pop(run_id, {"tool": ev["name"], "input": None})
            obs = ev.get("data", {}).get("output")
            steps.append((info["tool"], info["input"], obs))
            if on_step:
                on_step({
                    "tool": info["tool"],
                    "label": TOOL_STEP_LABELS.get(info["tool"], info["tool"]),
                    "status": "done",
                })
        elif kind == "on_chat_model_stream":
            token = _chunk_text(ev.get("data", {}).get("chunk"))
            if not token:
                continue
            # 每个 LLM run（思考步/最终步）独立探测，互不污染
            det = detectors.get(run_id)
            if det is None:
                det = FinalAnswerDetector()
                detectors[run_id] = det
            delta = det.feed(token)
            if delta:
                answer_parts.append(delta)
                if on_token:
                    on_token(delta)
        elif kind == "on_chain_end" and not ev.get("parent_ids"):
            out = ev.get("data", {}).get("output")
            if isinstance(out, dict) and "output" in out:
                root_output = out

    if root_output is not None:
        return root_output

    # 兜底：根链路事件缺失时，用工具调用与流式答案重建 response dict
    intermediate = [
        (
            AgentAction(
                tool=tool,
                tool_input=tool_input if tool_input is not None else "",
                log="",
            ),
            obs,
        )
        for tool, tool_input, obs in steps
    ]
    return {"output": "".join(answer_parts), "intermediate_steps": intermediate}


def stream_agent_response(
    agent: Any,
    prompt: str,
    on_step: Callable[[dict], None] | None = None,
    on_token: Callable[[str], None] | None = None,
) -> dict:
    """流式执行 agent；返回与 agent.invoke() 相同结构的 response dict。"""
    return asyncio.run(_astream(agent, prompt, on_step, on_token))
