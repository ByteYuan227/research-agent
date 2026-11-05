"""The agent loop.

One research run looks like this:

    messages = [user question]
    loop:
        response = Claude(messages, tools)          # streamed
        messages += [assistant response]            # append-only, never edited
        if Claude asked for client tools:
            messages += [tool results]               # run them locally
        elif the server paused a long tool turn:
            continue                                 # just re-send
        else:
            return the final text as the report

Everything else in this file is bookkeeping around that loop: budgets, usage
accounting, error recovery and progress callbacks for the UI.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .prompts import SYSTEM_PROMPT, TRUNCATED_CONTINUE_MESSAGE, WRAP_UP_MESSAGE, build_user_prompt
from .tools import CLIENT_TOOLS, Notebook, server_tools

DEFAULT_MODEL = "claude-opus-5-5"
# Server-side refusal fallback: if a safety classifier declines a turn, the API
# re-runs it on a fallback model Anthropic picks for that refusal category.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens: (input, output, cache write, cache read).
PRICING = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
}
WEB_SEARCH_USD_PER_1K = 10.00


@dataclass
class AgentConfig:
    model: str = DEFAULT_MODEL
    effort: str = "high"  # low | medium | high | xhigh | max
    max_turns: int = 30  # model calls per run, including pause_turn resumes
    max_tokens: int = 64000
    searches_per_turn: int = 5
    fetches_per_turn: int = 5
    wrap_up_turns_left: int = 3  # when this many turns remain, ask Claude to write the report
    use_fallbacks: bool = True


class Hooks:
    """Progress callbacks. Subclass and override what you want to display."""

    def on_turn_start(self, turn: int, max_turns: int) -> None: ...
    def on_text(self, text: str) -> None: ...
    def on_thinking(self, text: str) -> None: ...
    def on_block(self, block: Any) -> None: ...
    def on_tool_result(self, name: str, tool_input: dict, content: str, is_error: bool) -> None: ...
    def on_notice(self, message: str) -> None: ...


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0

    def add(self, usage: Any) -> None:
        if usage is None:
            return
        for name in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            setattr(self, name, getattr(self, name) + (getattr(usage, name, 0) or 0))
        server = getattr(usage, "server_tool_use", None)
        if server is not None:
            self.web_search_requests += getattr(server, "web_search_requests", 0) or 0
            self.web_fetch_requests += getattr(server, "web_fetch_requests", 0) or 0

    def estimated_cost(self, model: str) -> float | None:
        prices = PRICING.get(model)
        if prices is None:
            return None
        p_in, p_out, p_write, p_read = prices
        tokens = (
            self.input_tokens * p_in
            + self.output_tokens * p_out
            + self.cache_creation_input_tokens * p_write
            + self.cache_read_input_tokens * p_read
        ) / 1_000_000
        return tokens + self.web_search_requests * WEB_SEARCH_USD_PER_1K / 1000


@dataclass
class ResearchResult:
    question: str
    report: str
    status: str  # completed | max_turns | refusal
    turns: int
    notebook: Notebook
    usage: Usage
    citations: list[dict[str, str]] = field(default_factory=list)
    messages: list[Any] = field(default_factory=list)


class ResearchAgent:
    def __init__(
        self,
        client: Any | None = None,
        config: AgentConfig | None = None,
        hooks: Hooks | None = None,
    ) -> None:
        self.client = client if client is not None else anthropic.Anthropic()
        self.config = config or AgentConfig()
        self.hooks = hooks or Hooks()
        self.tools = server_tools(self.config.searches_per_turn, self.config.fetches_per_turn) + CLIENT_TOOLS

    # ------------------------------------------------------------------ run

    def run(self, question: str, today: str | None = None) -> ResearchResult:
        cfg = self.config
        today = today or _dt.date.today().isoformat()
        notebook = Notebook()
        usage = Usage()
        messages: list[Any] = [{"role": "user", "content": build_user_prompt(question, today)}]
        report_parts: list[str] = []
        citations: dict[str, str] = {}
        wrap_up_sent = False

        def result(status: str, turns: int) -> ResearchResult:
            return ResearchResult(
                question=question,
                report="".join(report_parts).strip(),
                status=status,
                turns=turns,
                notebook=notebook,
                usage=usage,
                citations=[{"url": u, "title": t} for u, t in citations.items()],
                messages=messages,
            )

        for turn in range(1, cfg.max_turns + 1):
            self.hooks.on_turn_start(turn, cfg.max_turns)
            response = self._call_model(messages)
            usage.add(response.usage)
            # Append the full content (thinking, server tool blocks, fallback
            # blocks...) exactly as returned. History is append-only.
            messages.append({"role": "assistant", "content": response.content})
            stop = response.stop_reason

            for block in response.content:
                if _type(block) == "fallback":
                    self.hooks.on_notice("当前模型拒绝了这一轮请求，已自动切换到备用模型继续。")

            if stop == "refusal":
                self.hooks.on_notice("模型拒绝回答这个问题，研究提前结束。")
                return result("refusal", turn)

            if stop == "pause_turn":
                # A long server-tool turn (many searches) was paused; re-sending
                # the conversation lets the server continue where it stopped.
                continue

            tool_uses = [b for b in response.content if _type(b) == "tool_use"]

            if stop == "max_tokens":
                if tool_uses:
                    # A truncated tool call may be incomplete: never run it.
                    messages.append({"role": "user", "content": [
                        _tool_result(b.id, "输出被截断，这次工具调用没有执行。请把输出写短一些后重试。", True)
                        for b in tool_uses
                    ]})
                else:
                    self._collect_text(response.content, report_parts, citations)
                    messages.append({"role": "user", "content": TRUNCATED_CONTINUE_MESSAGE})
                continue

            if tool_uses:
                results = []
                for block in tool_uses:
                    outcome = notebook.run(block.name, block.input)
                    self.hooks.on_tool_result(block.name, block.input, outcome.content, outcome.is_error)
                    results.append(_tool_result(block.id, outcome.content, outcome.is_error))
                # All results for one assistant turn go back in a single message.
                messages.append({"role": "user", "content": results})

                turns_left = cfg.max_turns - turn
                if not wrap_up_sent and turns_left <= cfg.wrap_up_turns_left:
                    # Operator instruction appended mid-conversation, instead
                    # of editing the system prompt (keeps the cache prefix).
                    messages.append({"role": "system", "content": WRAP_UP_MESSAGE})
                    wrap_up_sent = True
                    self.hooks.on_notice(f"只剩 {turns_left} 轮，已提醒 Agent 收尾并撰写报告。")
                continue

            # end_turn without tool calls: this text is the final report.
            self._collect_text(response.content, report_parts, citations)
            return result("completed", turn)

        self.hooks.on_notice("达到最大轮数，返回目前为止的输出。")
        return result("max_turns", cfg.max_turns)

    # ------------------------------------------------------------ internals

    def _call_model(self, messages: list[Any]) -> Any:
        cfg = self.config
        params: dict[str, Any] = dict(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=SYSTEM_PROMPT,
            tools=self.tools,
            messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": cfg.effort},
            # Automatic prompt caching: each turn re-reads the growing history
            # from cache instead of paying full price for it again.
            cache_control={"type": "ephemeral"},
        )
        if cfg.use_fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"

        json_retries = 0
        while True:
            try:
                with self.client.beta.messages.stream(**params) as stream:
                    for event in stream:
                        kind = event.type
                        if kind == "text":
                            self.hooks.on_text(event.text)
                        elif kind == "thinking":
                            self.hooks.on_thinking(event.thinking)
                        elif kind == "content_block_stop":
                            self.hooks.on_block(event.content_block)
                    return stream.get_final_message()
            except ValueError:
                # The SDK could not parse a streamed tool input at all. The
                # block never completed, so there is nothing to answer; re-issue
                # the request a couple of times. API errors are not ValueError.
                json_retries += 1
                if json_retries > 2:
                    raise
                self.hooks.on_notice("工具参数解析失败，正在重试这一轮。")

    @staticmethod
    def _collect_text(content: list[Any], parts: list[str], citations: dict[str, str]) -> None:
        for block in content:
            if _type(block) != "text":
                continue
            parts.append(block.text)
            for cite in getattr(block, "citations", None) or []:
                url = getattr(cite, "url", None)
                if url and url not in citations:
                    citations[url] = getattr(cite, "title", None) or ""


def _type(block: Any) -> str | None:
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _tool_result(tool_use_id: str, content: str, is_error: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        result["is_error"] = True
    return result
