"""A scripted stand-in for ``anthropic.Anthropic`` so the loop can be tested offline."""

from __future__ import annotations

import copy
from types import SimpleNamespace as NS
from typing import Any


def text(t: str, citations: list | None = None) -> NS:
    return NS(type="text", text=t, citations=citations)


def tool_use(id: str, name: str, input: dict) -> NS:
    return NS(type="tool_use", id=id, name=name, input=input)


def server_search(query: str) -> NS:
    return NS(type="server_tool_use", id="srv_" + query, name="web_search", input={"query": query})


def message(content: list, stop_reason: str = "end_turn", **usage: int) -> NS:
    usage_ns = NS(
        input_tokens=usage.get("input_tokens", 100),
        output_tokens=usage.get("output_tokens", 50),
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        server_tool_use=NS(web_search_requests=usage.get("searches", 0), web_fetch_requests=0),
    )
    return NS(content=content, stop_reason=stop_reason, usage=usage_ns)


class _Stream:
    def __init__(self, msg: NS) -> None:
        self._msg = msg

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self):
        for block in self._msg.content:
            if block.type == "text":
                yield NS(type="text", text=block.text)
            yield NS(type="content_block_stop", content_block=block)

    def get_final_message(self) -> NS:
        return self._msg


class FakeClient:
    """Returns the scripted messages in order and records every request."""

    def __init__(self, script: list[NS]) -> None:
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **params: Any) -> _Stream:
        # Snapshot messages: the agent keeps appending to the same list.
        self.requests.append({**params, "messages": copy.copy(params["messages"])})
        if not self._script:
            raise AssertionError("agent made more model calls than scripted")
        return _Stream(self._script.pop(0))
