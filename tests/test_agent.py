from types import SimpleNamespace as NS

from research_agent.agent import AgentConfig, Hooks, ResearchAgent, Usage
from research_agent.prompts import TRUNCATED_CONTINUE_MESSAGE, WRAP_UP_MESSAGE

from .fakes import FakeClient, message, server_search, text, tool_use

PLAN = {"sub_questions": [{"question": "有哪些公司在招", "status": "in_progress"}]}
FINDING = {
    "claim": "A 公司在招 Agent 实习生",
    "evidence": "招聘页写明……",
    "source_url": "https://a.example/jobs",
    "source_title": "A 公司招聘",
    "confidence": "high",
    "sub_question": 1,
}


def make_agent(script, **cfg):
    client = FakeClient(script)
    return ResearchAgent(client=client, config=AgentConfig(**cfg)), client


def test_full_run_with_client_tools():
    agent, client = make_agent([
        message([server_search("agent 实习"), tool_use("t1", "update_plan", PLAN)], "tool_use", searches=1),
        message([tool_use("t2", "record_finding", FINDING), tool_use("t3", "review_findings", {})], "tool_use"),
        message([text("# 报告\n\n结论 [1]")]),
    ])

    result = agent.run("AI Agent 实习岗位调研", today="2026-11-05")

    assert result.status == "completed"
    assert result.turns == 3
    assert result.report == "# 报告\n\n结论 [1]"
    assert len(result.notebook.findings) == 1
    assert result.notebook.sources() == [("https://a.example/jobs", "A 公司招聘")]
    assert result.usage.web_search_requests == 1
    assert result.usage.input_tokens == 300

    # Both tool results of turn 2 go back in ONE user message, matched by id.
    second_reply = client.requests[2]["messages"][-1]
    assert second_reply["role"] == "user"
    assert [r["tool_use_id"] for r in second_reply["content"]] == ["t2", "t3"]
    assert "A 公司在招" in second_reply["content"][1]["content"]

    # The date goes in the user turn, not the (cached) system prompt.
    assert "2026-11-05" in client.requests[0]["messages"][0]["content"]
    assert "2026-11-05" not in client.requests[0]["system"]


def test_request_shape():
    agent, client = make_agent([message([text("done")])])
    agent.run("q")
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert req["output_config"] == {"effort": "high"}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["fallbacks"] == "default"
    assert req["betas"] == ["server-side-fallback-2026-07-01"]
    types = [t.get("type", t.get("name")) for t in req["tools"]]
    assert types[:2] == ["web_search_20260209", "web_fetch_20260209"]
    assert {"update_plan", "record_finding", "review_findings"} <= set(types)
    for tool in req["tools"][2:]:
        assert tool["eager_input_streaming"] is True


def test_no_fallbacks_option():
    agent, client = make_agent([message([text("done")])], use_fallbacks=False)
    agent.run("q")
    assert "fallbacks" not in client.requests[0]
    assert "betas" not in client.requests[0]


def test_pause_turn_is_resent_without_new_user_message():
    agent, client = make_agent([
        message([server_search("x")], "pause_turn"),
        message([text("报告")]),
    ])
    result = agent.run("q")
    assert result.status == "completed"
    # The second request ends with the paused assistant turn itself.
    assert client.requests[1]["messages"][-1]["role"] == "assistant"


def test_invalid_tool_input_returns_error_result():
    agent, client = make_agent([
        message([tool_use("t1", "record_finding", {"claim": "缺少来源"})], "tool_use"),
        message([text("报告")]),
    ])
    result = agent.run("q")
    reply = client.requests[1]["messages"][-1]["content"][0]
    assert reply["is_error"] is True
    assert "source_url" in reply["content"]
    assert result.notebook.findings == []


def test_unknown_tool_returns_error_result():
    agent, client = make_agent([
        message([tool_use("t1", "delete_everything", {})], "tool_use"),
        message([text("报告")]),
    ])
    agent.run("q")
    reply = client.requests[1]["messages"][-1]["content"][0]
    assert reply["is_error"] is True


def test_wrap_up_message_is_appended_once_near_budget():
    looping = [message([tool_use(f"t{i}", "review_findings", {})], "tool_use") for i in range(4)]
    agent, client = make_agent(looping + [message([text("报告")])], max_turns=5, wrap_up_turns_left=3)
    result = agent.run("q")
    assert result.status == "completed"
    system_msgs = [
        m for m in client.requests[-1]["messages"] if isinstance(m, dict) and m["role"] == "system"
    ]
    assert system_msgs == [{"role": "system", "content": WRAP_UP_MESSAGE}]
    # It is placed right after the tool results of turn 2 (5 - 2 = 3 turns left).
    msgs = client.requests[2]["messages"]
    assert msgs[-1]["role"] == "system" and msgs[-2]["role"] == "user"


def test_max_turns_stops_the_loop():
    looping = [message([tool_use(f"t{i}", "review_findings", {})], "tool_use") for i in range(3)]
    agent, _ = make_agent(looping, max_turns=3)
    result = agent.run("q")
    assert result.status == "max_turns"
    assert result.turns == 3


def test_refusal_stops_without_running_tools():
    agent, _ = make_agent([message([tool_use("t1", "record_finding", FINDING)], "refusal")])
    result = agent.run("q")
    assert result.status == "refusal"
    assert result.notebook.findings == []


def test_truncated_tool_call_is_not_executed():
    agent, client = make_agent([
        message([tool_use("t1", "record_finding", FINDING)], "max_tokens"),
        message([text("报告")]),
    ])
    result = agent.run("q")
    assert result.notebook.findings == []
    assert client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_truncated_report_is_continued_and_joined():
    agent, client = make_agent([
        message([text("第一部分")], "max_tokens"),
        message([text("第二部分")]),
    ])
    result = agent.run("q")
    assert result.report == "第一部分第二部分"
    assert client.requests[1]["messages"][-1] == {"role": "user", "content": TRUNCATED_CONTINUE_MESSAGE}


def test_citations_are_collected():
    cite = NS(url="https://b.example", title="B")
    agent, _ = make_agent([message([text("事实", citations=[cite, cite])])])
    result = agent.run("q")
    assert result.citations == [{"url": "https://b.example", "title": "B"}]


def test_hooks_receive_progress():
    events = []

    class Recorder(Hooks):
        def on_text(self, t):
            events.append(("text", t))

        def on_block(self, b):
            events.append(("block", b.type))

        def on_tool_result(self, name, tool_input, content, is_error):
            events.append(("tool", name, is_error))

    client = FakeClient([
        message([server_search("x"), tool_use("t1", "update_plan", PLAN)], "tool_use"),
        message([text("报告")]),
    ])
    ResearchAgent(client=client, hooks=Recorder()).run("q")
    assert ("block", "server_tool_use") in events
    assert ("tool", "update_plan", False) in events
    assert ("text", "报告") in events


def test_usage_cost_estimate():
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, web_search_requests=10)
    assert round(u.estimated_cost("claude-opus-5-5"), 4) == round(4.0 + 2.0 + 0.1, 4)
    assert u.estimated_cost("unknown-model") is None
