"""Tools for the research agent.

There are two kinds of tools here:

* Server tools (web_search, web_fetch) run on Anthropic's servers. We only
  declare them; their results come back inside Claude's response.
* Client tools (update_plan, record_finding, review_findings) run in this
  process. Claude asks for them with a ``tool_use`` block, we execute them and
  send back a ``tool_result`` block.

The client tools all operate on a ``Notebook``: the agent's working memory for
one research run (the plan plus every finding and its source).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# ---------------------------------------------------------------------------
# Server tools
# ---------------------------------------------------------------------------


def server_tools(max_searches: int, max_fetches: int) -> list[dict[str, Any]]:
    """Anthropic-hosted web tools. ``max_uses`` caps calls per API request."""
    return [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches},
        {
            "type": "web_fetch_20260209",
            "name": "web_fetch",
            "max_uses": max_fetches,
            "citations": {"enabled": True},
            "max_content_tokens": 20000,
        },
    ]


# ---------------------------------------------------------------------------
# Client tool input schemas (validated with pydantic before running)
# ---------------------------------------------------------------------------

Status = Literal["pending", "in_progress", "done"]
Confidence = Literal["high", "medium", "low"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubQuestion(_Strict):
    question: str = Field(min_length=1)
    status: Status = "pending"


class UpdatePlanInput(_Strict):
    sub_questions: list[SubQuestion] = Field(min_length=1, max_length=10)


class RecordFindingInput(_Strict):
    claim: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_title: str = ""
    confidence: Confidence = "medium"
    sub_question: int | None = None


class ReviewFindingsInput(_Strict):
    pass


CLIENT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "update_plan",
        "description": (
            "创建或更新研究计划。传入完整的子问题列表（每次都传全部子问题，而不是只传变化的部分），"
            "每个子问题带状态：pending（未开始）、in_progress（进行中）、done（已完成）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sub_questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "子问题"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                        },
                        "required": ["question", "status"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["sub_questions"],
            "additionalProperties": False,
        },
    },
    {
        "name": "record_finding",
        "description": (
            "把一条关键发现记入研究笔记。每条发现只写一个事实、数据或观点，并附上支撑它的来源。"
            "写报告时会根据笔记引用来源，所以 source_url 必须是你实际看到的页面地址。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim": {"type": "string", "description": "发现的内容，一句话"},
                "evidence": {"type": "string", "description": "来源里支撑这条发现的原文摘录或数据"},
                "source_url": {"type": "string", "description": "来源页面 URL"},
                "source_title": {"type": "string", "description": "来源页面标题"},
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "可信度：多个独立来源印证或一手来源为 high",
                },
                "sub_question": {
                    "type": "integer",
                    "description": "这条发现对应的子问题编号（从 1 开始），可选",
                },
            },
            "required": ["claim", "evidence", "source_url", "confidence"],
            "additionalProperties": False,
        },
    },
    {
        "name": "review_findings",
        "description": "返回研究计划和笔记里的全部发现（按子问题分组，带来源编号）。写最终报告之前调用。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

# Requests are streamed, so let tool inputs stream as they are generated too.
# With this on, the API no longer validates inputs for us; ``Notebook.run``
# validates every input with pydantic before executing anything.
for _tool in CLIENT_TOOLS:
    _tool["eager_input_streaming"] = True

_INPUT_MODELS: dict[str, type[_Strict]] = {
    "update_plan": UpdatePlanInput,
    "record_finding": RecordFindingInput,
    "review_findings": ReviewFindingsInput,
}


# ---------------------------------------------------------------------------
# Notebook: state the client tools read and write
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    id: int
    claim: str
    evidence: str
    source_url: str
    source_title: str
    confidence: str
    sub_question: int | None


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False


@dataclass
class Notebook:
    plan: list[SubQuestion] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    # -- dispatch ----------------------------------------------------------

    def run(self, name: str, raw_input: Any) -> ToolOutcome:
        """Validate ``raw_input`` and run the client tool called ``name``."""
        model = _INPUT_MODELS.get(name)
        if model is None:
            return ToolOutcome(f"未知工具：{name}", is_error=True)
        if not isinstance(raw_input, dict):
            return ToolOutcome(
                json.dumps({"INVALID_JSON": str(raw_input)}, ensure_ascii=False), is_error=True
            )
        try:
            args = model.model_validate(raw_input)
        except ValidationError as exc:
            return ToolOutcome(f"参数不合法，请修正后重试：{_short_errors(exc)}", is_error=True)
        return getattr(self, f"_{name}")(args)

    # -- tools -------------------------------------------------------------

    def _update_plan(self, args: UpdatePlanInput) -> ToolOutcome:
        self.plan = list(args.sub_questions)
        done = sum(q.status == "done" for q in self.plan)
        return ToolOutcome(f"计划已更新：共 {len(self.plan)} 个子问题，已完成 {done} 个。")

    def _record_finding(self, args: RecordFindingInput) -> ToolOutcome:
        if args.sub_question is not None and self.plan and not 1 <= args.sub_question <= len(self.plan):
            return ToolOutcome(
                f"sub_question 应在 1 到 {len(self.plan)} 之间，收到 {args.sub_question}。", is_error=True
            )
        finding = Finding(id=len(self.findings) + 1, **args.model_dump())
        self.findings.append(finding)
        return ToolOutcome(f"已记录发现 #{finding.id}（笔记中共 {len(self.findings)} 条）。")

    def _review_findings(self, _args: ReviewFindingsInput) -> ToolOutcome:
        if not self.findings:
            return ToolOutcome("笔记还是空的，还没有记录任何发现。")
        sources = self.sources()
        source_no = {url: i for i, (url, _) in enumerate(sources, start=1)}
        lines: list[str] = []
        groups: dict[int | None, list[Finding]] = {}
        for f in self.findings:
            groups.setdefault(f.sub_question, []).append(f)
        for key in sorted(groups, key=lambda k: (k is None, k or 0)):
            if key is not None and 1 <= key <= len(self.plan):
                lines.append(f"## 子问题 {key}：{self.plan[key - 1].question}")
            else:
                lines.append("## 未归类")
            for f in groups[key]:
                lines.append(
                    f"- #{f.id} [{source_no[f.source_url]}] ({f.confidence}) {f.claim}\n  证据：{f.evidence}"
                )
        lines.append("\n## 来源编号")
        lines += [f"[{i}] {title or url} - {url}" for i, (url, title) in enumerate(sources, start=1)]
        return ToolOutcome("\n".join(lines))

    # -- helpers -----------------------------------------------------------

    def sources(self) -> list[tuple[str, str]]:
        """Unique (url, title) pairs in the order they were first cited."""
        seen: dict[str, str] = {}
        for f in self.findings:
            if f.source_url not in seen or (not seen[f.source_url] and f.source_title):
                seen[f.source_url] = f.source_title
        return list(seen.items())

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": [q.model_dump() for q in self.plan],
            "findings": [f.__dict__ for f in self.findings],
            "sources": [{"url": u, "title": t} for u, t in self.sources()],
        }


def _short_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
