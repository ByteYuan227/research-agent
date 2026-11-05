from research_agent.tools import CLIENT_TOOLS, Notebook


def finding(**over):
    base = {
        "claim": "c",
        "evidence": "e",
        "source_url": "https://x.example",
        "confidence": "high",
    }
    return {**base, **over}


def test_update_plan_replaces_plan():
    nb = Notebook()
    out = nb.run("update_plan", {"sub_questions": [{"question": "a", "status": "done"}, {"question": "b", "status": "pending"}]})
    assert not out.is_error
    assert "已完成 1 个" in out.content
    nb.run("update_plan", {"sub_questions": [{"question": "only", "status": "pending"}]})
    assert [q.question for q in nb.plan] == ["only"]


def test_record_finding_numbers_and_dedupes_sources():
    nb = Notebook()
    nb.run("record_finding", finding(source_title=""))
    nb.run("record_finding", finding(claim="c2", source_title="X 站"))
    nb.run("record_finding", finding(source_url="https://y.example"))
    assert [f.id for f in nb.findings] == [1, 2, 3]
    # The later title fills in the earlier empty one.
    assert nb.sources() == [("https://x.example", "X 站"), ("https://y.example", "")]


def test_record_finding_validates():
    nb = Notebook()
    assert nb.run("record_finding", finding(confidence="very")).is_error
    assert nb.run("record_finding", finding(extra_field=1)).is_error
    assert nb.run("record_finding", "not a dict").is_error
    nb.run("update_plan", {"sub_questions": [{"question": "a", "status": "pending"}]})
    assert nb.run("record_finding", finding(sub_question=5)).is_error
    assert nb.findings == []


def test_review_findings_groups_by_sub_question():
    nb = Notebook()
    assert "空的" in nb.run("review_findings", {}).content
    nb.run("update_plan", {"sub_questions": [{"question": "薪资", "status": "pending"}, {"question": "要求", "status": "pending"}]})
    nb.run("record_finding", finding(claim="要求会 Python", sub_question=2))
    nb.run("record_finding", finding(claim="日薪 400", sub_question=1, source_url="https://y.example"))
    nb.run("record_finding", finding(claim="其他"))
    text = nb.run("review_findings", {}).content
    assert text.index("子问题 1：薪资") < text.index("子问题 2：要求") < text.index("未归类")
    assert "[2] https://y.example" in text


def test_schemas_match_pydantic_models():
    names = {t["name"] for t in CLIENT_TOOLS}
    assert names == {"update_plan", "record_finding", "review_findings"}
    for tool in CLIENT_TOOLS:
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
