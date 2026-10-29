"""与Web框架无关的 Agent 领域类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AgentIntent = Literal["data", "knowledge", "hybrid", "clarification", "blocked"]


@dataclass(slots=True)
class AgentSource:
    filename: str
    section: str = ""
    doc_type: str = ""
    score: float = 0.0
    snippet: str = ""


@dataclass(slots=True)
class AgentStep:
    name: str
    status: Literal["success", "error"]
    duration_ms: int
    summary: str


@dataclass(slots=True)
class AgentUsage:
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(slots=True)
class ModelResponse:
    """模型文本及提供方返回的可选用量。"""

    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(slots=True)
class AgentResult:
    answer: str
    sql: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    intent: AgentIntent | None = None
    sources: list[AgentSource] = field(default_factory=list)
    steps: list[AgentStep] = field(default_factory=list)
    usage: AgentUsage | None = None
    sql_error: str | None = None
    analysis_evidence: list[dict[str, Any]] = field(default_factory=list)
