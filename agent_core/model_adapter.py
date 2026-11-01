"""兼容 OpenAI 接口的共享模型适配器。"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Sequence

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from typing import Literal

from agent_core.models import AgentIntent, AgentSource, ModelResponse
from agent_core.provider_policy import (
    build_http_client,
    build_http_clients,
    validate_model_name,
    validate_provider_url,
)
from agent_core.session import Message
from agent_core.streaming import emit_token


class _SQLPlan(BaseModel):
    sql: str = Field(description="一条只读 SQL SELECT 或 WITH 查询，不包含 Markdown")


class _AnalysisChoice(BaseModel):
    action: Literal["platform", "product", "finish"]


def _response(message) -> ModelResponse:
    usage = getattr(message, "usage_metadata", None) or {}
    return ModelResponse(
        content=str(message.content),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_tokens=usage.get("total_tokens"),
    )


class OpenAIModelAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        business_context: str,
        allowed_hosts: Sequence[str] = (),
        resolver: Callable[..., list] = socket.getaddrinfo,
        timeout: float = 120.0,
        max_retries: int = 1,
    ):
        self._api_key = api_key
        self._business_context = business_context
        self._allowed_hosts = tuple(allowed_hosts)
        self._resolver = resolver
        self._timeout = timeout
        self._max_retries = max_retries
        self._http_client = None
        self._http_async_client = None
        # 构造时先做不解析 DNS 的静态校验，尽早拒绝 http/内网字面量；
        # 真正连接前再解析一次，降低 DNS 重绑定风险。
        self._base_url = validate_provider_url(
            base_url,
            allowed_hosts=self._allowed_hosts,
            resolve_dns=False,
        )
        self._model = validate_model_name(model)

    def _clients(self):
        if self._http_client is None or self._http_async_client is None:
            self._http_client, self._http_async_client = build_http_clients(self._timeout)
        return self._http_client, self._http_async_client

    def _validated_base_url(self) -> str:
        return validate_provider_url(
            self._base_url,
            allowed_hosts=self._allowed_hosts,
            resolve_dns=True,
            resolver=self._resolver,
        )

    async def aclose(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
        if self._http_async_client is not None:
            await self._http_async_client.aclose()
        self._http_client = None
        self._http_async_client = None

    def _client(self, *, stream_usage: bool = False) -> ChatOpenAI:
        if not self._api_key:
            raise RuntimeError("模型未配置")
        http_client, http_async_client = self._clients()
        return ChatOpenAI(
            api_key=self._api_key,
            base_url=self._validated_base_url(),
            model=self._model,
            temperature=0,
            timeout=self._timeout,
            max_retries=self._max_retries,
            stream_usage=stream_usage,
            http_client=http_client,
            http_async_client=http_async_client,
        )

    async def choose_refund_analysis(self, query: str, evidence: list[dict]) -> ModelResponse:
        """根据已完成的聚合结果选择下一项固定工具。"""
        prompt = f"""你是电商退款分析助手。根据证据选择下一步，仅输出 JSON：{{"action":"platform|product|finish"}}。
问题：{query}
已完成工具及数据：{json.dumps(evidence, ensure_ascii=False, default=str)}
整体对比已经完成。平台或商品可各查一次；已查过的工具不可重复。
用户明确要求按平台或商品分析时，必须先选择尚未执行的对应工具；要求两者时依次执行两者，即使样本较少也应查询并说明局限。
若未指定拆解维度且证据已足够或样本不足，选择 finish。不要把相关性表述为因果。"""
        response = await self._client().with_structured_output(
            _AnalysisChoice, method="json_mode", include_raw=True,
        ).ainvoke(prompt)
        parsed = response.get("parsed") if isinstance(response, dict) else None
        if parsed is None:
            raise ValueError("模型未返回合法的分析选择")
        raw = response.get("raw")
        result = _response(raw) if raw is not None else ModelResponse(content="")
        result.content = parsed.action
        return result

    async def generate_sql(
        self,
        query: str,
        schema: str,
        history: list[Message],
        sources: list[AgentSource],
        previous_error: str | None,
    ) -> ModelResponse:
        source_context = "\n".join(f"{item.section}: {item.snippet}" for item in sources)
        prompt = f"""你是只读电商 Text-to-SQL 模块。必须只输出 JSON 对象，固定格式为 {{"sql":"SELECT ..."}}。
{self._business_context}

表结构：
{schema}

知识来源：
{source_context or '无'}

最近会话：{json.dumps(history[-6:], ensure_ascii=False)}
用户问题：{query}
上次脱敏错误：{previous_error or '无'}

必须生成单条 SELECT/WITH 查询，不得写库，不得查询个人隐私；不要输出 Markdown 或 JSON 之外的文字。"""
        # OpenAI 兼容提供方对 JSON Schema 的实现并不完全一致。显式使用
        # JSON Mode，再交给 Pydantic 校验，避免模型返回 Markdown SQL。
        response = await self._client().with_structured_output(
            _SQLPlan,
            method="json_mode",
            include_raw=True,
        ).ainvoke(prompt)
        parsed = response.get("parsed") if isinstance(response, dict) else None
        if parsed is None:
            raise ValueError("模型未返回合法的结构化 SQL")
        raw = response.get("raw")
        model_response = _response(raw) if raw is not None else ModelResponse(content="")
        model_response.content = parsed.sql
        return model_response

    def _answer_prompt(
        self,
        query: str,
        intent: AgentIntent,
        history: list[Message],
        sources: list[AgentSource],
        sql: str | None,
        rows: list[dict],
    ) -> str:
        source_context = "\n".join(
            f"[{item.filename} / {item.section}] {item.snippet}" for item in sources
        )
        return f"""你是电商数据分析助手。根据已验证的工具结果用中文简洁回答，不得编造。
问题：{query}
意图：{intent}
最近会话：{json.dumps(history[-6:], ensure_ascii=False)}
引用知识：{source_context or '无'}
SQL：{sql or '未执行'}
结果：{json.dumps(rows[:50], ensure_ascii=False, default=str)}
若结果为空，请明确说明。知识问答先用中文业务术语解释，再按需附公式；
如果结果是多步退款分析，严格引用各步骤数据，说明比较窗口和分子分母；数据只能显示关联，不能证明退款原因；数据不足时明确说明。
只要使用了引用知识，答案末尾必须使用“【来源】文件名 / 章节”列出至少一个实际来源。"""

    async def answer(
        self,
        query: str,
        intent: AgentIntent,
        history: list[Message],
        sources: list[AgentSource],
        sql: str | None,
        rows: list[dict],
    ) -> ModelResponse:
        if not self._api_key:
            if sources:
                summary = "\n".join(f"- {item.section}：{item.snippet}" for item in sources)
                return ModelResponse(f"当前未配置模型，已返回最相关的业务知识：\n{summary}")
            return ModelResponse("⚠️ AI功能未配置，请配置模型 API Key。")
        prompt = self._answer_prompt(query, intent, history, sources, sql, rows)
        return _response(await self._client().ainvoke(prompt))

    async def astream_answer(
        self,
        query: str,
        intent: AgentIntent,
        history: list[Message],
        sources: list[AgentSource],
        sql: str | None,
        rows: list[dict],
    ) -> ModelResponse:
        """流式版 answer：增量经 agent_core.streaming.emit_token 转发。

        未设置 token 钩子（如非流式调用方）时行为与 answer 等价，
        因此可作为 answer_generator 直接注入 Runtime。
        """
        if not self._api_key:
            return await self.answer(query, intent, history, sources, sql, rows)
        prompt = self._answer_prompt(query, intent, history, sources, sql, rows)
        chunks: list[str] = []
        usage: dict = {}
        # stream_usage=True 让最后一个 chunk 携带 usage_metadata，token 统计与 answer 对齐。
        async for chunk in self._client(stream_usage=True).astream(prompt):
            delta = chunk.content if isinstance(chunk.content, str) else ""
            if delta:
                chunks.append(delta)
                emit_token(delta)
            meta = getattr(chunk, "usage_metadata", None)
            if meta:
                usage = meta
        return ModelResponse(
            content="".join(chunks),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
        )

    def test_connection(self) -> None:
        if not self._api_key:
            raise RuntimeError("模型未配置")
        # 同步探活只需要 sync client，避免创建后无人关闭的 AsyncClient。
        http_client = build_http_client(15)
        try:
            ChatOpenAI(
                api_key=self._api_key,
                base_url=self._validated_base_url(),
                model=self._model,
                temperature=0,
                timeout=15,
                max_retries=0,
                max_tokens=1,
                http_client=http_client,
            ).invoke("仅回复 OK")
        finally:
            http_client.close()
