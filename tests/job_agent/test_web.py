"""The step-by-step browser agent on a mock multi-page application site (Workday-like)."""

import functools
import json
import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from job_agent import web
from job_agent.apply import work_dir
from job_agent.resume import build_resume
from job_agent.store import Job, State

from .test_browser import _browser_available

SITE = Path(__file__).with_name("mock_site")
pytestmark = pytest.mark.skipif(not _browser_available(), reason="Chromium not available")


@pytest.fixture(scope="module")
def site():
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a, **k: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def browser(home, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        monkeypatch.setenv("JOBAGENT_CDP_PORT", str(s.getsockname()[1]))
    yield
    web.stop()


def _button(view, text):
    for b in view["buttons"]:
        if b["text"].lower() == text.lower():
            return b["ref"]
    raise AssertionError(f"no button {text!r} in {[b['text'] for b in view['buttons']]}")


def _job(site):
    return Job(id="url:mockcorp", source="test", company="Mock Corp", title="Software Engineer Intern",
               url=f"{site}/wd/job.html", apply_url=f"{site}/wd/job.html", ats="workday", status="scored", score=88)


def test_full_multistep_application(site, profile, browser, monkeypatch):
    job = _job(site)
    state = State()
    state.jobs[job.id] = job
    build_resume(profile, None, work_dir(job))
    monkeypatch.setattr(web.inbox, "wait_for_code", lambda since, **kw: "AB12CD34")

    v = web.open_job(state, job, profile)
    v = web.click(state, profile, _button(v, "Apply"))
    v = web.click(state, profile, _button(v, "Apply Manually"))
    kinds = {f["key"]: f for f in v["fields"]}
    assert kinds["pw"]["suggested"] == "__site_password__" and kinds["terms"]["suggested"] is True

    r = web.fill(state, profile, None)
    assert not r["errors"] and not r["required_empty"], r
    v = web.click(state, profile, _button(r, "Create Account"))
    assert v["asks_for_code"]
    v = web.email_code(state, profile)
    assert "My Information" in v["headings"]
    hdyh = next(f for f in v["fields"] if f["kind"] == "listbox")
    assert "Job Board" in hdyh["options"] and hdyh["suggested"] == "Job Board"

    r = web.fill(state, profile, None)
    assert not r["errors"] and not r["required_empty"], r
    v = web.click(state, profile, _button(r, "Save and Continue"))
    assert set(v["required_empty"]) == {"resume", "why", "felony"}
    suggested = {f["key"]: f.get("suggested") for f in v["fields"]}
    assert suggested["felony"] == "No" and suggested["resume"].endswith("_Resume.pdf") and suggested["why"] is None

    r = web.fill(state, profile, {"why": "I build ML infrastructure in Python and C++."})
    assert not r["errors"] and not r["required_empty"], r
    v = web.click(state, profile, _button(r, "Next"))
    v = web.click(state, profile, _button(v, "Submit"))
    assert v["looks_submitted"]
    web.finish(state, profile, "applied", "submitted through the mock portal")

    assert state.jobs[job.id].status == "applied"
    acct = state.accounts["127.0.0.1"]
    with web.page() as pg:
        sent = {k: json.loads(pg.evaluate(f"localStorage.getItem('{k}')")) for k in ("account", "step1", "step2")}
    assert sent["account"] == {"email": profile.personal.email, "password": acct.password}
    assert sent["step1"] == {"hdyh": "Job Board", "first": "Alex", "last": "Chen", "phone": profile.personal.phone, "cit": "China"}
    assert sent["step2"]["felony"] == "no" and sent["step2"]["resume"].endswith("_Resume.pdf")


def test_dry_run_blocks_side_effects(site, profile, browser, monkeypatch):
    monkeypatch.setenv("JOBAGENT_DRY_RUN", "1")
    job = _job(site)
    state = State()
    state.jobs[job.id] = job
    v = web.open_job(state, job, profile)
    v = web.click(state, profile, _button(v, "Apply"))
    v = web.click(state, profile, _button(v, "Apply Manually"))
    web.fill(state, profile, None)
    with pytest.raises(web.WebError, match="dry run"):
        web.click(state, profile, _button(v, "Create Account"))
    assert state.accounts == {}  # no real account password was even generated
    with pytest.raises(web.WebError):
        web.finish(state, profile, "applied", "")
    web.finish(state, profile, "dry_run", "stopped at account creation")
    assert state.jobs[job.id].application["dry_run"] is True


def test_personal_data_only_goes_to_employer_or_ats(site, profile, browser):
    job = _job(site)
    assert web.allowed(f"{site}/wd/step1.html", job)
    assert web.allowed("https://acme.wd5.myworkdayjobs.com/en-US/External/job/x", job)
    assert web.allowed("https://jobs.lever.co/acme/1/apply", job)
    assert not web.allowed("https://evil.example.com/collect", job)
    company_job = job.model_copy(update={"url": "https://careers.acme.com/jobs/1", "apply_url": "https://careers.acme.com/jobs/1"})
    assert web.allowed("https://apply.acme.com/step", company_job)
    assert not web.allowed("https://acme.com.evil.io/step", company_job)
    state = State()
    state.jobs[job.id] = job
    web.open_job(state, job, profile)
    with pytest.raises(web.WebError):
        web.goto(state, profile, "https://evil.example.com/collect")


def test_form_inside_an_iframe(site, profile, browser):
    job = _job(site).model_copy(update={"url": f"{site}/wd/embedded.html", "apply_url": f"{site}/wd/embedded.html"})
    state = State()
    state.jobs[job.id] = job
    v = web.open_job(state, job, profile)
    assert v["form_in_frame"] and v["form_in_frame"].endswith("/wd/step1.html")
    assert {f["key"] for f in v["fields"]} >= {"hdyh", "first", "last", "phone", "cit"}
    r = web.fill(state, profile, None)
    assert not r["errors"] and not r["required_empty"], r
    v = web.click(state, profile, _button(r, "Save and Continue"))
    assert "My Experience" in v["text"]


def test_honeypot_never_filled_and_cookie_banner_dismissed(site, profile, browser):
    job = _job(site).model_copy(update={"url": f"{site}/wd/honeypot.html", "apply_url": f"{site}/wd/honeypot.html"})
    state = State()
    state.jobs[job.id] = job
    v = web.open_job(state, job, profile)
    assert {f["key"] for f in v["fields"]} == {"email"}  # the honeypot is invisible to the agent
    assert "Accept Cookies" not in [b["text"] for b in v["buttons"]]  # banner accepted on open
    web.fill(state, profile, {"website": "https://example.com"})  # even if asked, it is not a field
    with web.page() as pg:
        assert pg.evaluate("document.getElementById('website').value") == ""
