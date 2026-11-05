"""Browser tests against a local mock application form (no network)."""

import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from job_agent import apply as apply_mod
from job_agent.browser import forms
from job_agent.browser.launch import launch
from job_agent.resume import build_resume
from job_agent.store import Job, State

SITE = Path(__file__).with_name("mock_site")


def _browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            launch(pw).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _browser_available(), reason="Chromium not available")


@pytest.fixture(scope="module")
def site():
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a, **k: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_extract_suggest_fill_submit(site, profile, tmp_path):
    from playwright.sync_api import sync_playwright

    resume = build_resume(profile, None, tmp_path)
    with sync_playwright() as pw:
        browser = launch(pw)
        page = browser.new_page()
        data = forms.open_form(page, f"{site}/form.html")
        fields = {f["key"]: f for f in data["fields"]}
        assert fields["q_spons"]["kind"] == "radio" and fields["q_spons"]["required"]
        assert fields["relocate"]["kind"] == "button_group" and fields["relocate"]["required"]
        assert fields["privacy"]["kind"] == "checkbox"
        assert "relocate_hidden" not in fields

        answers = {}
        for f in data["fields"]:
            s = forms.suggest(f, profile, "Mock Co", {"resume": resume})
            if s is not None:
                answers[f["key"]] = s.value
        assert answers["first_name"] == "Alex" and answers["q_auth"] == "Yes"
        assert answers["gender"] == "Decline to self-identify"
        assert answers["q_spons"] == "Yes"  # example profile needs sponsorship in the future
        assert answers["privacy"] is True
        assert "why" not in answers and "references" not in answers  # left for Claude / the human

        answers["why"] = "I build LLM inference systems and want to work on them at Mock Co."
        report = forms.fill_all(page, data["fields"], answers)
        assert not report.errors and not report.missing_required, (report.errors, report.missing_required)

        page.locator("[data-ja-submit]").click()
        assert forms.outcome(page, "")["status"] == "submitted"
        sent = json.loads(page.evaluate("localStorage.getItem('submitted')"))
        browser.close()
    assert sent["first_name"] == "Alex" and sent["email"] == "alex.chen.example@gmail.com"
    assert sent["q_spons"] == "yes" and sent["relocate"] == "yes" and sent["privacy"] == "on"
    assert sent["gender"] == "Decline to self-identify" and sent["resume"] == resume.name


def _job(url: str) -> Job:
    return Job(
        id="greenhouse:mock:1", source="test", company="Mock Co", title="ML Intern", url=url, apply_url=url,
        ats="greenhouse", ats_company="mock", ats_job_id="1", status="scored", score=90,
    )


@pytest.mark.parametrize("hash_, needs_code", [("", False), ("#code", True)])
def test_prepare_and_submit_flow(site, profile, home, monkeypatch, hash_, needs_code):
    job = _job(f"{site}/form.html{hash_}")
    state = State()
    state.jobs[job.id] = job
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.prepare(job, profile)  # resume must be built first
    build_resume(profile, None, apply_mod.work_dir(job))

    prepared = apply_mod.prepare(job, profile)
    assert set(prepared["required_without_answer"]) == {"why"}

    answers = apply_mod.work_dir(job) / "answers.json"
    answers.write_text(json.dumps({"answers": {"references": None}}))
    assert apply_mod.submit(job, profile, state, answers)["status"] == "incomplete"

    if needs_code:
        monkeypatch.setattr(apply_mod.inbox, "wait_for_code", lambda since, **kw: "AB12CD34")
    answers.write_text(json.dumps({"answers": {"why": "Grounded answer."}}))
    dry = apply_mod.submit(job, profile, state, answers, dry_run=True)
    assert dry["status"] == "dry_run_ok"
    assert state.jobs[job.id].status == "scored"

    result = apply_mod.submit(job, profile, state, answers)
    assert result["status"] == "submitted", result
    assert state.jobs[job.id].status == "applied"
    assert state.jobs[job.id].application["answers"]["Why do you want to join Mock Co?"] == "Grounded answer."
    assert apply_mod.applied_today(state, profile) == 1


def test_file_answers_are_restricted(site, profile, home):
    job = _job(f"{site}/form.html")
    state = State()
    build_resume(profile, None, apply_mod.work_dir(job))
    apply_mod.prepare(job, profile)
    answers = apply_mod.work_dir(job) / "answers.json"
    answers.write_text(json.dumps({"answers": {"why": "x", "resume": "/etc/passwd"}}))
    result = apply_mod.submit(job, profile, state, answers, dry_run=True)
    assert result["status"] == "incomplete" and any("resume" in p for p in result["problems"])


def test_account_creation_uses_vault_password(site, profile, home, monkeypatch):
    from job_agent import vault

    job = _job(f"{site}/signup.html")
    state = State()
    state.jobs[job.id] = job
    build_resume(profile, None, apply_mod.work_dir(job))
    prepared = apply_mod.prepare(job, profile)
    pw_fields = [f for f in prepared["fields"] if f["kind"] == "password"]
    assert len(pw_fields) == 2 and all(f["suggested"] == "__site_password__" for f in pw_fields)

    answers = apply_mod.work_dir(job) / "answers.json"
    # Claude cannot choose the password: an attempted override is ignored.
    answers.write_text(json.dumps({"answers": {"password": "hunter2"}}))
    result = apply_mod.submit(job, profile, state, answers)
    assert result["status"] == "submitted", result

    acct = state.accounts["127.0.0.1"]
    assert acct.username == profile.personal.email and len(acct.password) == 20 and acct.password != "hunter2"
    shown = state.jobs[job.id].application["answers"]
    assert acct.password not in json.dumps(shown) and "encrypted vault" in shown["Password"]

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert not vault.can_reveal()
