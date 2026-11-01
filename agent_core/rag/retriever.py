"""无需外部服务的 Markdown 知识库检索器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agent_core.models import AgentSource


@dataclass(frozen=True, slots=True)
class _Chunk:
    filename: str
    section: str
    content: str


def _terms(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text.lower())
    latin = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    stop_terms = {"什么", "定义", "含义", "如何", "怎么", "计算", "公式", "标准", "范围", "哪些"}
    bigrams = {chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))}
    return latin | (bigrams - stop_terms)


class MarkdownKnowledgeRetriever:
    """按章节检索 Markdown；作为零成本基线，也便于离线评测。"""

    def __init__(self, knowledge_dir: str | Path):
        self.knowledge_dir = Path(knowledge_dir)
        self._chunks: list[_Chunk] | None = None

    def _load(self) -> list[_Chunk]:
        if self._chunks is not None:
            return self._chunks
        chunks: list[_Chunk] = []
        for path in sorted(self.knowledge_dir.glob("*.md")):
            section = path.stem
            body: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("## "):
                    if body:
                        chunks.append(_Chunk(path.name, section, "\n".join(body).strip()))
                    section = line.lstrip("# ").strip()
                    body = []
                else:
                    body.append(line)
            if body:
                chunks.append(_Chunk(path.name, section, "\n".join(body).strip()))
        self._chunks = [chunk for chunk in chunks if chunk.content]
        return self._chunks

    async def retrieve(self, query: str, top_k: int = 3) -> list[AgentSource]:
        query_terms = _terms(query)
        if not query_terms:
            return []
        ranked: list[tuple[float, _Chunk]] = []
        for chunk in self._load():
            chunk_terms = _terms(f"{chunk.section}\n{chunk.content}")
            overlap = len(query_terms & chunk_terms)
            if overlap:
                score = overlap / max(1, len(query_terms))
                ranked.append((score, chunk))
        ranked.sort(key=lambda item: (-item[0], item[1].filename, item[1].section))
        return [
            AgentSource(
                filename=chunk.filename,
                section=chunk.section,
                doc_type="markdown",
                score=round(score, 4),
                snippet=re.sub(r"\s+", " ", chunk.content)[:240],
            )
            for score, chunk in ranked[:top_k]
        ]

    def status(self) -> dict:
        """返回知识库可观测状态；只暴露计数与错误类别，不暴露服务器路径。"""
        try:
            count = len(self._load())
            return {"ok": True, "count": count, "error": None}
        except Exception as exc:  # noqa: BLE001 - 状态检查不能反向中断服务
            return {"ok": False, "count": 0, "error": type(exc).__name__}
