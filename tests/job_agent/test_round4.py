"""Round 4: submit safety, profile checks before submitting, closed postings, filters, queue."""

import json
import time

import httpx
import pytest

from job_agent import apply as apply_mod
from job_agent import ats, cli, notify, prefilter, web
from job_agent.browser import forms
from job_agent.report import build_report
from job_agent.resume import build_resume
from job_agent.store import Job, State, load_state, save_state

from .test_browser import _browser_available
from .test_web import _button, browser, site  # noqa: F401 - fixtures


def _job(**kw) -> Job:
    base = dict(id="greenhouse:acme:1", source="test", company="Acme", title="Software Engineer Intern",
                url="https://boards.greenhouse.io/acme/jobs/1", apply_url="https://boards.greenhouse.io/acme/jobs/1",
                ats="greenhouse", ats_company="acme", ats_job_id="1", status="scored", score=80,
                posted_at=time.time() - 5 * 86400, terms=["Summer 2027"], job_type="internship",
                locations=["New York, NY"], category="Software")
    base.update(kw)
    return Job(**base)


# ---------------------------------------------------------------------------
# Answers checked against the profile
# ---------------------------------------------------------------------------


def test_polarity():
    assert forms.polarity("Yes") is True and forms.polarity(True) is True
    assert forms.polarity("No, I will not require sponsorship") is False
    assert forms.polarity("I am not a U.S. citizen") is False
    assert forms.polarity("I am authorized to work") is True
    assert forms.polarity("Decline to self-identify") is None and forms.polarity("China") is None


def test_sponsorship_question_turned_around(profile):
    # The example profile needs sponsorship in the future.
    f = {"key": "q", "kind": "radio", "label": "Are you legally authorized to work in the U.S. without sponsorship?",
         "options": ["Yes", "No"]}
    assert forms.suggest(f, profile, "Acme", {}).value == "No"
    f2 = {**f, "label": "Will you now or in the future require visa sponsorship?"}
    assert forms.suggest(f2, profile, "Acme", {}).value == "Yes"
    f3 = {**f, "label": "Are you legally authorized to work in the United States?"}
    assert forms.suggest(f3, profile, "Acme", {}).value == "Yes"


def test_audit_flags_only_real_contradictions(profile):
    fields = [
        {"key": "spons", "kind": "radio", "label": "Will you now or in the future require sponsorship?",
         "options": ["Yes", "No"], "shown": "No"},
        {"key": "auth", "kind": "select", "label": "Are you legally authorized to work in the United States?",
         "options": ["Yes", "No"], "shown": "Yes"},
        {"key": "email", "kind": "email", "label": "Email", "shown": "someone.else@example.com"},
        {"key": "first", "kind": "text", "label": "First Name", "shown": "alex"},  # case only: fine
        {"key": "phone", "kind": "tel", "label": "Phone", "shown": "(555) 010-0199"},  # not checked
        {"key": "why", "kind": "textarea", "label": "Why us?", "shown": "Because."},
        {"key": "gender", "kind": "select", "label": "Gender", "options": ["Male", "Female", "Decline"], "shown": "Male"},
    ]
    found = {p["key"]: p for p in forms.audit(fields, profile, "Acme")}
    assert set(found) == {"spons", "email"}
    assert found["spons"]["profile_says"] == "Yes" and found["spons"]["from"].startswith("work_authorization")
    # Answers about to be typed are checked the same way.
    assert [p["key"] for p in forms.audit(fields, profile, "Acme", {"auth": "No", "why": "x"})] == ["auth"]


def test_custom_answers_are_protected(profile_data):
    from job_agent.profile import Profile

    profile_data["custom_answers"] = [{"match": "relatives? (work|employed)", "answer": "Yes"}]
    p = Profile.model_validate(profile_data)
    f = {"key": "rel", "kind": "radio", "label": "Do any of your relatives work at Acme?", "options": ["Yes", "No"],
         "shown": "No"}
    assert forms.audit([f], p, "Acme")[0]["from"] == "custom_answers[0]"


def test_sends_application():
    filled_form = 4
    assert web.sends_application("Submit application", {}, 0)
    assert web.sends_application("Done", {"tag": "button", "type": "submit", "in_form": True}, filled_form)
    assert web.sends_application("Done", {"tag": "button", "type": "", "in_form": True}, filled_form)
    assert not web.sends_application("Done", {"tag": "button", "type": "button", "in_form": True}, filled_form)
    assert not web.sends_application("Save and Continue", {"tag": "button", "type": "submit", "in_form": True}, filled_form)
    assert not web.sends_application("Next", {"tag": "button", "type": "submit", "in_form": True}, filled_form)
    assert not web.sends_application("Create Account", {"tag": "button", "type": "submit", "in_form": True}, filled_form)
    assert not web.sends_application("Apply", {"tag": "a"}, 0)  # starts an application
    assert web.sends_application("Apply", {"tag": "button", "type": "submit", "in_form": True}, filled_form)
    assert not web.sends_application("Apply Manually", {"tag": "button"}, filled_form)
    # A link to another page (job pages with pre-filled search boxes) only navigates.
    assert not web.sends_application("Apply", {"tag": "a", "href": "/Recruiting/Jobs/Apply/4640810"}, filled_form)
    assert web.sends_application("Apply", {"tag": "a", "href": "#"}, filled_form)
    assert web.sends_application("Submit", {"tag": "a", "href": "javascript:void(0)"}, 0)


# ---------------------------------------------------------------------------
# Closed postings
# ---------------------------------------------------------------------------


def test_closed_and_already_applied_texts():
    assert web.CLOSED_RE.search("Sorry, this position is no longer accepting applications.")
    assert web.CLOSED_RE.search("The job you are looking for has expired")  # "this job has expired" variant
    assert web.CLOSED_RE.search("The page you are looking for can't be found")
    assert not web.CLOSED_RE.search("We are accepting applications on a rolling basis.")
    assert web.ALREADY_APPLIED_RE.search("It looks like you have already applied to this job.")
    assert web.ALREADY_APPLIED_RE.search("You have an existing application for this requisition")
    assert not web.ALREADY_APPLIED_RE.search("Apply now")


def _client(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, (code, body) in routes.items():
            if str(request.url).startswith(prefix):
                return httpx.Response(code, text=body)
        return httpx.Response(500, text="unexpected")
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_description_detects_closed_postings(monkeypatch):
    monkeypatch.setattr(ats, "render_page_text", lambda url: "")
    ref = ats.AtsRef("greenhouse", "acme", "1", "us")
    c = _client({"https://boards-api.greenhouse.io/": (404, "{}")})
    with pytest.raises(ats.PostingClosed):
        ats.fetch_description(ref, "https://boards.greenhouse.io/acme/jobs/1", c)

    other = ats.AtsRef("other", None, None, "us")
    with pytest.raises(ats.PostingClosed):
        ats.fetch_description(other, "https://careers.acme.com/1", _client({"https://careers.acme.com/": (410, "")}))
    notice = "<html><body><h1>Intern</h1><p>This position has been filled.</p></body></html>"
    with pytest.raises(ats.PostingClosed):
        ats.fetch_description(other, "https://careers.acme.com/1", _client({"https://careers.acme.com/": (200, notice)}))
    # A single-page app may answer 404 and still render the job: then it is not closed.
    monkeypatch.setattr(ats, "render_page_text", lambda url: "Machine learning intern. " * 60)
    text = ats.fetch_description(other, "https://careers.acme.com/1", _client({"https://careers.acme.com/": (404, "")}))
    assert "Machine learning intern" in text


def test_jd_records_closed_posting(home, monkeypatch, capsys):
    state = State()
    job = _job()
    state.jobs[job.id] = job
    save_state(state)

    def closed(ref, url, client=None):
        raise ats.PostingClosed("greenhouse no longer lists this posting (HTTP 404)")

    monkeypatch.setattr(ats, "fetch_description", closed)
    assert cli.main(["jd", job.id]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "closed"
    assert load_state().jobs[job.id].status == "closed"
    assert cli.main(["candidates"]) == 0  # a closed job is decided: never a candidate again


# ---------------------------------------------------------------------------
# After an apply session: never twice, retry once
# ---------------------------------------------------------------------------


def test_settle_never_retries_after_a_submit_click(home):
    state = State()
    a, b = _job(id="greenhouse:acme:1"), _job(id="greenhouse:acme:2")
    state.jobs[a.id], state.jobs[b.id] = a, b
    save_state(state)
    apply_mod.mark_submit_clicked(a, "Submit application")

    cli.main(["settle", a.id, "--exit", "124"])
    cli.main(["settle", b.id, "--exit", "124"])
    s = load_state()
    assert s.jobs[a.id].status == "manual"  # may have gone through: you check your e-mail
    assert s.jobs[b.id].status == "scored" and s.jobs[b.id].retries == 1  # a timeout gets one more try
    cli.main(["settle", b.id, "--exit", "124"])
    assert load_state().jobs[b.id].status == "manual"  # but only one

    # Claude finished without recording anything: no retry.
    c = _job(id="greenhouse:acme:3")
    s = load_state()
    s.jobs[c.id] = c
    save_state(s)
    cli.main(["settle", c.id, "--exit", "0"])
    assert load_state().jobs[c.id].status == "manual"

    report = build_report(load_state(), None, detail="full")
    assert "可能已经提交" in report

    # You checked: it was not submitted. Sending it back to the queue clears the marker.
    cli.main(["mark", a.id, "--status", "scored", "--reason", "not submitted"])
    assert not apply_mod.submit_clicked(load_state().jobs[a.id])


def test_rehearsed_jobs_stay_queued_for_the_first_real_run(home, monkeypatch, capsys):
    monkeypatch.setenv("JOBAGENT_DRY_RUN", "1")
    state = State()
    a, b = _job(id="greenhouse:acme:1", status="applying"), _job(id="greenhouse:acme:2", status="manual")
    a.application = {"dry_run": True, "outcome": {"status": "dry_run_ok"}}
    state.jobs[a.id], state.jobs[b.id] = a, b
    save_state(state)
    cli.main(["settle", a.id, "--exit", "0"])
    cli.main(["settle", b.id, "--exit", "0"])
    s = load_state()
    assert s.jobs[a.id].status == "scored"  # applied for real tomorrow
    assert s.jobs[b.id].status == "manual"  # a real blocker (e.g. CAPTCHA) found while rehearsing stays
    monkeypatch.delenv("JOBAGENT_DRY_RUN")
    capsys.readouterr()
    cli.main(["queue"])
    assert capsys.readouterr().out.split() == [a.id]


# ---------------------------------------------------------------------------
# Queue: company cap, fresh postings first
# ---------------------------------------------------------------------------


def _profile_with(tmp_path, monkeypatch, profile_data, **prefs):
    import yaml

    profile_data["preferences"].update(prefs)
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(profile_data))
    monkeypatch.setenv("JOBAGENT_PROFILE", str(path))


def test_queue_caps_applications_per_company_when_asked(home, capsys, tmp_path, profile_data, monkeypatch):
    _profile_with(tmp_path, monkeypatch, profile_data, max_per_company=3)
    now = time.time()
    state = State()
    jobs = [
        _job(id="gh:g:applied", company="Google LLC", status="applied", applied_at=now - 10 * 86400),
        _job(id="gh:g:1", company="Google", score=95),
        _job(id="gh:g:2", company="Google", score=90),
        _job(id="gh:g:3", company="google", score=85),
        _job(id="gh:o:1", company="Other Co", score=80, posted_at=now - 3600),  # fresh: head start
        _job(id="gh:o:2", company="Other Co", score=86),
    ]
    for j in jobs:
        state.jobs[j.id] = j
    save_state(state)
    capsys.readouterr()
    assert cli.main(["queue"]) == 0
    queue = capsys.readouterr().out.split()
    assert queue == ["gh:g:1", "gh:g:2", "gh:o:1", "gh:o:2"]
    s = load_state()
    assert s.jobs["gh:g:3"].status == "skipped" and "max_per_company" in s.events[-1].detail


def test_queue_has_no_company_cap_by_default(home, capsys):
    from job_agent.profile import Preferences

    assert Preferences().max_per_company is None
    state = State()
    for i in range(5):
        j = _job(id=f"gh:g:{i}", company="Google", score=90 - i)
        state.jobs[j.id] = j
    save_state(state)
    capsys.readouterr()
    cli.main(["queue"])
    assert len(capsys.readouterr().out.split()) == 5


# ---------------------------------------------------------------------------
# Cheap filters before Claude reads anything
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, reason",
    [
        ("Software Engineer Intern - Summer 2027", None),
        ("SWE Intern (Summer/Fall 2027)", None),
        ("2027 Summer Intern", None),
        ("Software Engineer Intern, Fall 2026", "term in title"),
        ("2026 Software Engineering Intern", "year in title"),
        ("ML Intern - Class of 2028", None),  # a graduation year, not the term
        ("Undergraduate Software Engineering Intern", "undergraduates only"),
        ("PhD Research Intern", "PhD only"),
        ("MS/PhD Research Intern", None),
        ("Software Engineer Intern - Grace Hopper Celebration", "event/conference posting"),
        ("Software Engineering Talent Community", "talent pool, not a specific job"),
        ("Data Science Intern - University of Michigan Students", "for one school's students"),
        ("Data Science Intern - Carnegie Mellon University", None),
    ],
)
def test_title_filters(profile, title, reason):
    job = _job(title=title, terms=[])
    got = prefilter.filter_reason(job, profile, time.time())
    assert (got is None) if reason is None else (got or "").startswith(reason), got


def test_simplify_terms_win_over_the_title(profile):
    # The listing says Summer 2027 even though the title says 2026 (e.g. a req opened last year).
    job = _job(title="2026 Software Engineering Intern", terms=["Summer 2027"])
    assert prefilter.filter_reason(job, profile, time.time()) is None


def test_graduate_only_roles_for_a_bachelor(profile_data):
    from job_agent.profile import Profile

    profile_data["education"] = [e for e in profile_data["education"] if e["degree"].startswith("B")]
    p = Profile.model_validate(profile_data)
    assert prefilter.degree_level(p) == "bachelor"
    job = _job(title="Machine Learning Intern (Master's students)", terms=[])
    assert prefilter.filter_reason(job, p, time.time()) == "graduate students only"
    job = _job(title="Undergraduate Software Engineering Intern", terms=[])
    assert prefilter.filter_reason(job, p, time.time()) is None


def test_contract_marketplaces_are_not_applications(profile):
    job = _job(url="https://work.mercor.com/jobs/list_1", apply_url="https://work.mercor.com/jobs/list_1", ats="other")
    assert prefilter.filter_reason(job, profile, time.time()) == "contract marketplace, not a job application"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def test_report_counts_closed_and_flags_unconfirmed(profile):
    state = State()
    now = time.time()
    closed = _job(id="a", status="scored")
    state.set_status(closed, "closed", "posting removed")
    dup = _job(id="b")
    state.set_status(dup, "duplicate", "already applied")
    ok = _job(id="c", company="Beta", applied_at=now,
              application={"mode": "browser", "confirmed_by_page": False})
    state.set_status(ok, "applied", "submitted")
    report = build_report(state, profile)
    assert "岗位已关闭或之前投过：2" in report
    assert "未看到确认页" in report


# ---------------------------------------------------------------------------
# In a real browser
# ---------------------------------------------------------------------------

needs_browser = pytest.mark.skipif(not _browser_available(), reason="Chromium not available")


def _wd_job(base, page):
    return Job(id="url:mockcorp", source="test", company="Mock Corp", title="Software Engineer Intern",
               url=f"{base}/wd/{page}", apply_url=f"{base}/wd/{page}", ats="workday", status="scored", score=88)


@needs_browser
def test_submit_refused_while_the_page_contradicts_the_profile(site, profile, browser):  # noqa: F811
    job = _wd_job(site, "final.html")
    state = State()
    state.jobs[job.id] = job
    web.open_job(state, job, profile)
    r = web.fill(state, profile, {"spons": "No"})  # a wrong answer (the profile needs sponsorship)
    assert not r["errors"], r
    with pytest.raises(web.WebError, match="contradict the profile"):
        web.click(state, profile, _button(r, "Done"))  # a plain type=submit button is recognised
    assert not apply_mod.submit_clicked(job)

    r = web.fill(state, profile, None, overwrite=True)  # put the profile's answers back
    v = web.click(state, profile, _button(r, "Done"))
    assert v["looks_submitted"] and apply_mod.submit_clicked(job)
    web.finish(state, profile, "applied", "thank-you page")
    app = state.jobs[job.id].application
    assert state.jobs[job.id].status == "applied" and app["confirmed_by_page"] is True
    assert app["answers"]["Email"] == profile.personal.email  # every value typed is kept for review
    with web.page() as pg:
        sent = json.loads(pg.evaluate("localStorage.getItem('final')"))
    assert sent == {"email": profile.personal.email, "spons": "yes"}


@needs_browser
def test_dry_run_recognises_unlabelled_submit_buttons(site, profile, browser, monkeypatch):  # noqa: F811
    monkeypatch.setenv("JOBAGENT_DRY_RUN", "1")
    job = _wd_job(site, "final.html")
    state = State()
    state.jobs[job.id] = job
    web.open_job(state, job, profile)
    r = web.fill(state, profile, None)
    with pytest.raises(web.WebError, match="dry run"):
        web.click(state, profile, _button(r, "Done"))


@needs_browser
def test_closed_posting_page(site, profile, browser):  # noqa: F811
    job = _wd_job(site, "closed.html")
    state = State()
    state.jobs[job.id] = job
    v = web.open_job(state, job, profile)
    assert v["looks_closed"] and not v["already_applied"] and v["saved_account_for_this_site"] is False
    web.finish(state, profile, "closed", "no longer accepting applications")
    assert state.jobs[job.id].status == "closed"


@needs_browser
def test_form_flow_refuses_answers_that_contradict_the_profile(site, profile, home):  # noqa: F811
    url = f"{site}/form.html"
    job = Job(id="greenhouse:mock:1", source="test", company="Mock Co", title="ML Intern", url=url, apply_url=url,
              ats="greenhouse", ats_company="mock", ats_job_id="1", status="scored", score=90)
    state = State()
    state.jobs[job.id] = job
    build_resume(profile, None, apply_mod.work_dir(job))
    apply_mod.prepare(job, profile)
    answers = apply_mod.work_dir(job) / "answers.json"
    answers.write_text(json.dumps({"answers": {"why": "Grounded answer.", "q_spons": "No"}}))
    result = apply_mod.submit(job, profile, state, answers, dry_run=True)
    assert result["status"] == "incomplete"
    assert any("q_spons" in p and "contradicts the profile" in p for p in result["problems"])
    assert apply_mod.submit(job, profile, state, answers, dry_run=True, force=True)["status"] == "dry_run_ok"
    answers.write_text(json.dumps({"answers": {"why": "Grounded answer."}}))
    assert apply_mod.submit(job, profile, state, answers, dry_run=True)["status"] == "dry_run_ok"


@needs_browser
def test_typing_into_a_combobox_never_submits_the_form(site):  # noqa: F811
    from playwright.sync_api import sync_playwright

    from job_agent.browser.launch import launch, new_context

    with sync_playwright() as pw:
        b = launch(pw)
        page = new_context(b).new_page()
        page.goto(f"{site}/enter.html")
        fields = {f["key"]: f for f in page.evaluate(forms.EXTRACT_JS)["fields"]}
        assert fields["school"]["kind"] == "combobox"
        relocate = next(f for f in fields.values() if f["kind"] == "button_group")
        report = forms.fill_all(page, [fields["first_name"], fields["school"], relocate],
                                {"first_name": "Alex", "school": "Carnegie Mellon University", relocate["key"]: "Yes"})
        assert not report.errors, report.errors
        assert page.evaluate("window.submitted") is False
        assert page.evaluate("document.querySelector('[data-option=yes]').getAttribute('aria-pressed')") == "true"
        b.close()


def test_report_counts_only_claude_scores(profile):
    state = State()
    a, b, c = _job(id="a"), _job(id="b"), _job(id="c", company="Gamma")
    state.set_status(a, "scored", "88: strong match")             # jobagent score
    state.set_status(b, "skipped", "40: weak match")              # jobagent score
    state.set_status(a, "scored", "rehearsed (dry run); will apply for real in the next run")  # settle
    state.set_status(c, "skipped", "already applied elsewhere")   # jobagent mark
    report = build_report(state, profile, detail="full")
    assert "Claude 打分：2 个" in report
    assert "Gamma - Software Engineer Intern：already applied elsewhere" in report


# ---------------------------------------------------------------------------
# Report by email only (public repositories)
# ---------------------------------------------------------------------------


def _email_profile(profile_data, tmp_path, monkeypatch):
    import yaml

    from job_agent.profile import Profile

    profile_data["notify"].update(github_issue=False, email_to="me@example.com")
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(profile_data))
    monkeypatch.setenv("JOBAGENT_PROFILE", str(path))
    return Profile.model_validate(profile_data)


def test_manual_jobs_get_stable_numbers():
    state = State()
    a, b = _job(id="a"), _job(id="b")
    state.set_status(a, "manual", "captcha")
    state.set_status(b, "manual", "captcha")
    state.set_status(a, "scored", "retry")
    state.set_status(a, "manual", "captcha again")
    assert (a.manual_no, b.manual_no, state.manual_seq) == (1, 2, 2)


def test_done_replies_are_parsed_from_your_own_text_only():
    reply = """done 3 7
已投 #12、15

On Thu, Nov 6, 2026 at 8:00 PM Me <me@example.com> wrote:
> 投完后回复这封邮件，第一行写 done 加上编号
> done 99
"""
    assert notify.done_numbers(reply) == {3, 7, 12, 15}
    assert notify.done_numbers("> done 4\nthanks") == set()
    assert notify.done_numbers("I am done with 3 of them") == set()  # only "done <numbers>" lines


def test_done_replies_only_from_your_address(monkeypatch):
    from job_agent import inbox

    msgs = [
        inbox.Message("1", "Me <me@example.com>", "Re: 求职日报 2026-11-06", time.time(), "done 1 2"),
        inbox.Message("2", "Someone <x@evil.example>", "Re: 求职日报 2026-11-06", time.time(), "done 3"),
        inbox.Message("3", "me@example.com", "Unrelated", time.time(), "done 4"),
    ]
    monkeypatch.setattr(inbox, "fetch_since", lambda since, limit=60: msgs)
    assert notify.done_numbers_from_inbox({"me@example.com"}, 0) == {1, 2}


def test_email_only_report_and_sync(home, tmp_path, profile_data, monkeypatch, capsys):
    profile = _email_profile(profile_data, tmp_path, monkeypatch)
    state = State()
    a, b = _job(id="gh:a:1", company="Alpha"), _job(id="gh:b:2", company="Beta")
    state.set_status(a, "manual", "CAPTCHA")
    state.set_status(b, "manual", "CAPTCHA")
    save_state(state)

    report = build_report(load_state(), profile)
    assert "**#1**" in report and "**#2**" in report and "回复这封邮件" in report and "在这里勾选" not in report

    sent = {}
    monkeypatch.setattr(notify, "github_issue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no issue")))
    monkeypatch.setattr(notify, "close_reports", lambda: 1)
    monkeypatch.setattr(notify, "email_report", lambda subject, body, to, files: sent.update(body=body, to=to) or True)
    path = tmp_path / "report.md"
    path.write_text(report)
    capsys.readouterr()
    cli.main(["notify", "--report", str(path)])
    result = json.loads(capsys.readouterr().out)
    assert result["closed_old_report_issues"] == 1 and result["email"] is True and sent["to"] == "me@example.com"

    monkeypatch.setenv("JOBAGENT_IMAP_USER", "me@example.com")
    monkeypatch.setenv("JOBAGENT_IMAP_PASSWORD", "x")
    monkeypatch.setattr(notify, "checked_jobs_in_reports", lambda: [])
    monkeypatch.setattr(notify, "done_numbers_from_inbox", lambda own, since: {2} if "me@example.com" in own else set())
    cli.main(["sync-checklist"])
    s = load_state()
    assert s.jobs["gh:b:2"].status == "applied_manual" and s.jobs["gh:a:1"].status == "manual"


def test_email_body_has_no_job_ids(monkeypatch):
    import smtplib

    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): captured["body"] = msg.get_content()

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("JOBAGENT_IMAP_USER", "me@example.com")
    monkeypatch.setenv("JOBAGENT_IMAP_PASSWORD", "x")
    assert notify.email_report("求职日报", "- [ ] **#1** Acme <!-- job:gh:acme:1 -->\n", "me@example.com")
    assert "job:" not in captured["body"] and "**#1** Acme" in captured["body"]


def test_public_actions_log_names_no_jobs(tmp_path, monkeypatch):
    import subprocess as sp

    from .test_daily_run import ROOT, _setup

    env, home = _setup(tmp_path, monkeypatch, "unused")
    (tmp_path / "bin" / "claude").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$2" == *"Mode SCORE"* ]]; then\n'
        "  jobagent score greenhouse:acme:1 90 --reason r --decision apply > /dev/null\n"
        "  jobagent score greenhouse:acme:2 85 --reason r --decision skip > /dev/null\n"
        "fi\n"
        "echo ok; exit 0\n"
    )
    p = sp.run(["bash", str(ROOT / "scripts" / "daily_run.sh")], env=env, capture_output=True, text=True, timeout=300)
    assert "[apply-1]" in p.stdout and "acme" not in p.stdout.lower(), p.stdout


def test_salary_answer_depends_on_posted_pay(home, profile_data):
    from job_agent.profile import Profile

    profile_data["answers"].update(salary_expectation="$100,000", salary_expectation_if_posted="$120,000")
    p = Profile.model_validate(profile_data)
    field = {"key": "salary", "kind": "text", "label": "What are your salary expectations?"}
    posted, silent = _job(id="gh:pay:1"), _job(id="gh:pay:2")
    (apply_mod.work_dir(posted) / "description.txt").write_text("The base pay range is $110,000 - $150,000 per year.")
    (apply_mod.work_dir(silent) / "description.txt").write_text("Competitive pay and great benefits.")
    assert forms.suggest(field, apply_mod.profile_for(posted, p), "Acme", {}).value == "$120,000"
    assert forms.suggest(field, apply_mod.profile_for(silent, p), "Acme", {}).value == "$100,000"
    assert apply_mod.PAY_RE.search("$45/hour") and apply_mod.PAY_RE.search("$95K base")
    assert not apply_mod.PAY_RE.search("We raised $2 billion")


def test_sponsorship_question_mentioning_visa_status(profile):
    """Greenhouse's standard wording mentions "visa status"; it is still a sponsorship question."""
    f = {"key": "q", "kind": "radio", "options": ["Yes", "No"],
         "label": "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status)?"}
    s = forms.suggest(f, profile, "Acme", {})
    assert s is not None and s.value == "Yes" and s.source.startswith("work_authorization.requires_sponsorship")
    visa = {"key": "v", "kind": "text", "label": "What is your current visa status?"}
    assert forms.suggest(visa, profile, "Acme", {}).value == "F-1"


@pytest.mark.parametrize("text, want", [
    ("The base pay range for this position is $120,000 - $180,000 per year.", "$150,000"),
    ("Pay: $120K–$180K plus equity", "$150,000"),
    ("$129,300/year in our lowest geographic market up to $223,600/year in our highest geographic market", "$176,000"),
    ("Hourly rate: $45 - $55 per hour", "$50/hour"),
    ("Base salary of $150,000.", "$150,000"),
    ("We raised $2 billion. Competitive pay.", None),
])
def test_posted_pay_midpoint(text, want):
    assert apply_mod.posted_pay_midpoint(text) == want


def test_salary_midpoint_of_the_posted_range(home, profile_data):
    from job_agent.profile import Profile

    profile_data["answers"].update(salary_expectation="$100,000", salary_expectation_if_posted="midpoint")
    p = Profile.model_validate(profile_data)
    field = {"key": "salary", "kind": "text", "label": "Desired salary"}
    posted, silent = _job(id="gh:mid:1"), _job(id="gh:mid:2")
    (apply_mod.work_dir(posted) / "description.txt").write_text("Base pay: $120,000 - $180,000.")
    (apply_mod.work_dir(silent) / "description.txt").write_text("Great team.")
    assert forms.suggest(field, apply_mod.profile_for(posted, p), "Acme", {}).value == "$150,000"
    assert forms.suggest(field, apply_mod.profile_for(silent, p), "Acme", {}).value == "$100,000"


def test_skills_and_fit_answers_are_never_reused():
    from job_agent.memory import rememberable

    for label in ["What programming languages are you proficient in?", "List your technical skills",
                  "Which tools and frameworks have you used?", "Why are you a good fit for this role?",
                  "Describe your experience with distributed systems"]:
        assert not rememberable({"kind": "text", "label": label}, "Java, Python", "Acme"), label
    assert rememberable({"kind": "radio", "label": "Are you open to working in the office 3 days a week?"}, "Yes", "Acme")


# ---------------------------------------------------------------------------
# GPA and transcript only where a form requires them
# ---------------------------------------------------------------------------


def _gpa_profile(profile_data):
    from job_agent.profile import Profile

    profile_data["education"][0]["gpa"] = "3.28"
    profile_data["answers"]["gpa_only_when_required"] = True
    return Profile.model_validate(profile_data)


def test_gpa_only_where_required(profile_data, tmp_path):
    from job_agent.resume import render_html

    p = _gpa_profile(profile_data)
    req = {"key": "g", "kind": "text", "label": "GPA", "required": True}
    assert forms.suggest(req, p, "Acme", {}).value == "3.28"
    assert forms.suggest({**req, "required": False}, p, "Acme", {}) is None
    assert forms.withheld({**req, "required": False}, p)
    band = {"key": "b", "kind": "select", "label": "Cumulative GPA", "required": True,
            "options": ["Below 3.0", "3.0 - 3.49", "3.5 - 3.79", "3.8+"]}
    assert forms.suggest(band, p, "Acme", {}).value == "3.0 - 3.49"
    assert forms.gpa_band(3.9, band["options"]) == "3.8+" and forms.gpa_band(2.5, band["options"]) == "Below 3.0"
    assert "3.28" not in render_html(p)  # never on the resume


def test_transcript_only_where_required(home, profile_data, tmp_path, monkeypatch):
    from job_agent import documents

    p = _gpa_profile(profile_data)
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF-1.4 transcript")
    documents.add("transcript", pdf)
    files = {"resume": tmp_path / "r.pdf", **documents.available()}
    req = {"key": "t", "kind": "file", "label": "Unofficial Transcript", "required": True}
    assert forms.suggest(req, p, "Acme", files).value.endswith("transcript.pdf")
    assert forms.suggest({**req, "required": False}, p, "Acme", files) is None

    fields = [req, {**req, "key": "t2", "required": False}, {"key": "g", "kind": "text", "label": "GPA", "required": False}]
    merged, problems = apply_mod.merge_answers(fields, {"answers": {"t": "transcript", "t2": "transcript", "g": "3.28"}},
                                               files, p)
    assert merged["t"].endswith("transcript.pdf") and "t2" not in merged and "g" not in merged and not problems

    monkeypatch.setenv("JOBAGENT_STATE_KEY", __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode())
    bundle = tmp_path / "documents.enc"
    assert documents.export(bundle) == {"documents": 1} and b"transcript" not in bundle.read_bytes()
    documents.path("transcript").unlink()
    assert documents.restore(bundle) == {"documents": 1} and documents.path("transcript").read_bytes().startswith(b"%PDF")


# ---------------------------------------------------------------------------
# More new grad sources
# ---------------------------------------------------------------------------

SPEEDY_MD = """## 2027 USA SWE New Graduate Positions
### FAANG+
| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://www.twitch.tv"><strong>Twitch</strong></a> | Software Dev Engineer I - Security Platform | San Francisco, CA | $193k/yr | <a href="https://job-boards.greenhouse.io/twitch/jobs/8929545002"><img src="x.png" alt="Apply"/></a> | 3d |
### Other
| Company | Position | Location | Posting | Age |
|---|---|---|---|---|
| <a href="https://acme.example"><strong>Acme &amp; Co</strong></a> | Software Engineer, New Grad | Remote in USA | <a href="https://jobs.lever.co/acme/123abc"><img src="x.png"/></a> | 0d |
| broken row without a link | x | y | z | 1d |
"""


def test_speedyapply_new_grad_list():
    from job_agent import sources

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SPEEDY_MD)))
    now = 1_800_000_000.0
    jobs = sources.speedyapply_jobs(client, now=now)
    assert [(j.id, j.company, j.title) for j in jobs] == [
        ("greenhouse:twitch:8929545002", "Twitch", "Software Dev Engineer I - Security Platform"),
        ("lever:acme:123abc", "Acme & Co", "Software Engineer, New Grad"),
    ]
    assert jobs[0].posted_at == now - 3 * 86400 and jobs[1].job_type == "new_grad" and jobs[1].locations == ["Remote in USA"]


@pytest.mark.parametrize("title, kind", [
    ("Software Engineer I", "new_grad"), ("Software Dev Engineer I - Security", "new_grad"),
    ("Associate Software Engineer", "new_grad"), ("Engineer 1 - Automation", "new_grad"),
    ("Software Engineer II", "full_time"), ("Senior Software Engineer", "full_time"),
    ("Software Engineer Intern", "internship"),
])
def test_entry_level_titles(title, kind):
    from job_agent.sources import job_type_from_title

    assert job_type_from_title(title) == kind


def test_popular_boards_are_added_once(profile_data, monkeypatch):
    from job_agent import sources
    from job_agent.profile import Profile

    profile_data["preferences"]["job_types"] = ["new_grad"]
    profile_data["preferences"]["sources"] = {"simplify_internships": False, "simplify_new_grad": False,
                                              "speedyapply_new_grad": False, "popular_boards": True,
                                              "companies": [{"name": "Stripe", "ats": "greenhouse", "token": "stripe"}]}
    p = Profile.model_validate(profile_data)
    seen = []
    monkeypatch.setattr(sources, "company_jobs", lambda board, client: seen.append((board.ats, board.token)) or [])
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    sources.discover(p.preferences, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert seen.count(("greenhouse", "stripe")) == 1 and ("lever", "palantir") in seen and len(seen) == len(sources.POPULAR_BOARDS)


@pytest.mark.parametrize("label", [
    "Would you like to be considered for other roles at Acme?",
    "Join our talent community", "Would you like to receive job alerts?",
    "Do you want to receive text message updates about your application?", "Opt-in to SMS notifications",
])
def test_want_questions_are_answered_yes(profile, label):
    s = forms.suggest({"key": "o", "kind": "radio", "label": label, "options": ["Yes", "No"]}, profile, "Acme", {})
    assert s is not None and s.value == "Yes"
    box = forms.suggest({"key": "b", "kind": "checkbox", "label": label}, profile, "Acme", {})
    assert box is not None and box.value is True


def test_want_rule_leaves_facts_alone(profile):
    s = forms.suggest({"key": "s", "kind": "radio", "options": ["Yes", "No"],
                       "label": "Will you now or in the future require sponsorship?"}, profile, "Acme", {})
    assert s.source.startswith("work_authorization")


def test_own_site_password_is_used(monkeypatch):
    from job_agent import vault

    state = State()
    monkeypatch.delenv("JOBAGENT_SITE_PASSWORD", raising=False)
    generated = vault.get_or_create(state, "https://acme.wd5.myworkdayjobs.com/x", "me@example.com").password
    monkeypatch.setenv("JOBAGENT_SITE_PASSWORD", "Example.Pass1")
    # an account the agent already created keeps its password; new accounts use yours
    assert vault.get_or_create(state, "https://acme.wd5.myworkdayjobs.com/y", "me@example.com").password == generated
    assert vault.get_or_create(state, "https://other.example.com", "me@example.com").password == "Example.Pass1"
    assert generated != "Example.Pass1"


def test_how_did_you_hear_falls_back_to_a_generic_option(profile):
    f = {"key": "h", "kind": "select", "label": "How did you hear about this job?",
         "options": ["LinkedIn", "Recruiter", "BrickFest", "Career fair", "Referral"]}
    assert forms.suggest(f, profile, "Databricks", {}).value == "LinkedIn"
    f["options"] = ["Referral", "Company website", "Other"]
    assert forms.suggest(f, profile, "Acme", {}).value == "Company website"


def test_previous_employer_named_in_the_question(profile_data):
    from job_agent.profile import Profile

    profile_data["answers"]["previously_employed_at"] = ["Amazon"]
    p = Profile.model_validate(profile_data)
    q = {"key": "q", "kind": "radio", "options": ["Yes", "No"], "label": "Have you previously worked for Amazon or any of its subsidiaries?"}
    assert forms.suggest(q, p, "Twitch", {}).value == "Yes"
    assert forms.suggest({**q, "label": "Have you previously worked for Twitch?"}, p, "Twitch", {}).value == "No"


def test_applied_review_lists_every_answer(home):
    from job_agent import materials

    state = State()
    j = _job(id="gh:r:1", company="Acme", applied_at=time.time(),
             application={"answers": {"Email": "me@example.com", "Why Acme?": "Because..."}, "cover_letter": "Dear team"})
    state.set_status(j, "applied", "ok")
    old = _job(id="gh:r:2", company="Old", applied_at=time.time() - 5 * 86400, application={"answers": {"x": "y"}})
    state.set_status(old, "applied", "ok")
    review, files = materials.applied_review(state, time.time() - 3600)
    assert "## Acme" in review and "**Why Acme?**: Because..." in review and "Dear team" in review and "Old" not in review


@needs_browser
def test_same_label_buttons_are_both_listed(site, profile, browser):  # noqa: F811
    job = _wd_job(site, "signin.html")
    state = State()
    state.jobs[job.id] = job
    v = web.open_job(state, job, profile)
    signins = [b for b in v["buttons"] if b["text"].startswith("Sign In")]
    assert [b["text"] for b in signins] == ["Sign In", "Sign In (form)"]
    assert signins[1]["id"] == "signInSubmitButton"
    ext = next(f for f in v["fields"] if f["label"] == "Phone Extension")
    assert ext.get("suggested") is None  # never the whole phone number


def test_queue_puts_one_step_forms_first_and_google_to_you(home, capsys):
    now = time.time()
    state = State()
    for j in (_job(id="wd:1", ats="workday", score=95, url="https://x.wd5.myworkdayjobs.com/1", apply_url="https://x.wd5.myworkdayjobs.com/1"),
              _job(id="gh:1", ats="greenhouse", score=60),
              _job(id="g:1", company="Google", ats="other", url="https://www.google.com/about/careers/applications/jobs/results/1",
                   apply_url="https://www.google.com/about/careers/applications/jobs/results/1")):
        state.jobs[j.id] = j
    save_state(state)
    capsys.readouterr()
    cli.main(["queue"])
    assert capsys.readouterr().out.split() == ["gh:1", "wd:1"]
    assert load_state().jobs["g:1"].status == "manual"


def test_queue_sends_sites_that_never_complete_to_you_and_caps_workday(home, capsys, monkeypatch):
    monkeypatch.setenv("JOBAGENT_WORKDAY_PER_RUN", "3")
    state = State()
    jobs = [_job(id=f"wd:{i}", ats="workday", score=90 - i, url=f"https://x.wd5.myworkdayjobs.com/{i}",
                 apply_url=f"https://x.wd5.myworkdayjobs.com/{i}") for i in range(5)]
    jobs += [_job(id="ic:1", ats="other", url="https://careers-acme.icims.com/jobs/1/job", apply_url="https://careers-acme.icims.com/jobs/1/job"),
             _job(id="sr:1", ats="smartrecruiters", url="https://jobs.smartrecruiters.com/Acme/1", apply_url="https://jobs.smartrecruiters.com/Acme/1"),
             _job(id="az:1", ats="other", url="https://www.amazon.jobs/en/jobs/1", apply_url="https://www.amazon.jobs/en/jobs/1"),
             _job(id="gh:1", ats="greenhouse", score=50)]
    for j in jobs:
        state.jobs[j.id] = j
    save_state(state)
    capsys.readouterr()
    cli.main(["queue"])
    assert capsys.readouterr().out.split() == ["gh:1", "wd:0", "wd:1", "wd:2"]
    after = load_state().jobs
    assert {after[i].status for i in ("ic:1", "sr:1", "az:1")} == {"manual"}
    assert after["wd:4"].status == "scored"  # waits for a later run

    monkeypatch.setenv("JOBAGENT_TRY_ALL_SITES", "1")
    monkeypatch.setenv("JOBAGENT_WORKDAY_PER_RUN", "10")
    capsys.readouterr()
    cli.main(["queue"])
    assert capsys.readouterr().out.split() == ["gh:1", "wd:0", "wd:1", "wd:2", "wd:3", "wd:4"]


@needs_browser
def test_workday_hidden_resume_input_is_listed_and_uploaded(site, profile, browser):  # noqa: F811
    from job_agent.apply import resume_path

    job = _wd_job(site, "experience.html")
    state = State()
    state.jobs[job.id] = job
    pdf = resume_path(job, profile)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 resume")
    v = web.open_job(state, job, profile)
    files = [f for f in v["fields"] if f["kind"] == "file"]
    assert [f["label"] for f in files] == ["Resume/CV", "Resume/CV"], files
    assert all(f.get("suggested") == pdf.name for f in files), files
    r = web.fill(state, profile, {files[0]["key"]: "resume"})
    assert files[0]["key"] in r["filled"] and not r["errors"] and not r["value_did_not_stick"], r


@needs_browser
def test_workday_dropdowns_behind_click_filter_long_lists_and_search(site, profile, browser):  # noqa: F811
    job = _wd_job(site, "myinfo.html")
    state = State()
    state.jobs[job.id] = job
    v = web.open_job(state, job, profile)
    keys = {f["label"]: f["key"] for f in v["fields"]}
    r = web.fill(state, profile, {keys["State"]: "Maryland", keys["Phone Device Type"]: "Mobile",
                                  keys["Field of Study"]: "Computer Science",
                                  keys["How Did You Hear About Us?"]: "Online job board"})
    assert not r["errors"] and not r["value_did_not_stick"], r
    with web.page() as pg:
        assert pg.evaluate("window.chosen") == {"address--countryRegion": "Maryland", "phoneNumber--phoneType": "Mobile",
                                                "fos": "Computer Science", "source": "Indeed"}
