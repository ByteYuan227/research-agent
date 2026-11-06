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
