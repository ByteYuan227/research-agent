import time

import pytest

from job_agent import ats, prefilter, sources
from job_agent.inbox import extract_code
from job_agent.profile import Profile
from job_agent.report import build_report
from job_agent.resume import PlanError, render_text, validate_plan
from job_agent.store import Job, State, decrypt_file, encrypt_file, generate_key, load_state, merge_jobs, save_state

# ---------------------------------------------------------------- ATS parsing


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://job-boards.greenhouse.io/anthropic/jobs/5438311008", ("greenhouse", "anthropic", "5438311008", "us")),
        ("https://boards.greenhouse.io/parallel/jobs/5275690007?gh_src=x", ("greenhouse", "parallel", "5275690007", "us")),
        ("https://job-boards.eu.greenhouse.io/acme/jobs/42", ("greenhouse", "acme", "42", "eu")),
        ("https://boards.greenhouse.io/embed/job_app?token=8847747002", ("greenhouse", None, "8847747002", "us")),
        ("https://careers.example.com/jobs/?gh_jid=4479712009", ("greenhouse", None, "4479712009", "us")),
        ("https://jobs.lever.co/tri/e7cc38d2-2b08/apply", ("lever", "tri", "e7cc38d2-2b08", "us")),
        ("https://jobs.ashbyhq.com/fab2/d9edb922/application?embed=true", ("ashby", "fab2", "d9edb922", "us")),
        ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternal/job/x/JR1", ("workday", "nvidia", "JR1", "us")),
        ("https://lifeattiktok.com/search/7543", ("tiktok", None, "7543", "us")),
    ],
)
def test_parse_ats(url, expected):
    ref = ats.parse_ats(url)
    assert (ref.ats, ref.company, ref.job_id, ref.region) == expected


def test_application_urls():
    gh = ats.parse_ats("https://boards.greenhouse.io/parallel/jobs/5275690007")
    assert ats.application_url(gh, "") == "https://job-boards.greenhouse.io/parallel/jobs/5275690007"
    lv = ats.parse_ats("https://jobs.lever.co/tri/abc")
    assert ats.application_url(lv, "") == "https://jobs.lever.co/tri/abc/apply"
    ab = ats.parse_ats("https://jobs.ashbyhq.com/fab2/xyz")
    assert ats.application_url(ab, "") == "https://jobs.ashbyhq.com/fab2/xyz/application"
    wd = ats.parse_ats("https://x.wd1.myworkdayjobs.com/a/job/b")
    assert not wd.auto_apply and ats.application_url(wd, "orig") == "orig"


def test_html_to_text():
    text = ats.html_to_text("<h2>About</h2><p>We build <b>agents</b>.</p><ul><li>Python</li><li>CUDA</li></ul><script>x()</script>")
    assert "We build agents." in text and "- Python" in text and "x()" not in text


# ---------------------------------------------------------------- discovery & filtering


def job(**kw) -> Job:
    base = dict(
        id="greenhouse:acme:1",
        source="simplify_internship",
        company="Acme",
        title="Machine Learning Engineer Intern",
        url="https://job-boards.greenhouse.io/acme/jobs/1",
        apply_url="https://job-boards.greenhouse.io/acme/jobs/1",
        ats="greenhouse",
        ats_company="acme",
        ats_job_id="1",
        locations=["San Francisco, CA"],
        posted_at=time.time() - 86400,
        category="AI/ML/Data",
        terms=["Summer 2027"],
        job_type="internship",
    )
    base.update(kw)
    return Job(**base)


def test_simplify_repo_names():
    import datetime as dt

    assert sources.internship_repo(dt.date(2026, 11, 5))[0] == "Summer2027-Internships"
    assert sources.internship_repo(dt.date(2027, 2, 1))[0] == "Summer2027-Internships"


def test_job_type_from_title():
    assert sources.job_type_from_title("Software Engineer Intern") == "internship"
    assert sources.job_type_from_title("Software Engineer, New Grad 2027") == "new_grad"
    assert sources.job_type_from_title("Staff Engineer") == "full_time"


@pytest.mark.parametrize(
    "kw, reason",
    [
        ({}, None),
        ({"job_type": "new_grad"}, "job type new_grad"),
        ({"category": "Hardware"}, "category Hardware"),
        ({"category": "Data Science, AI & Machine Learning"}, None),
        ({"posted_at": time.time() - 40 * 86400}, "too old"),
        ({"title": "Senior ML Engineer Intern"}, "title excluded"),
        ({"title": "Research Scientist Intern - PhD"}, "PhD only"),
        ({"terms": ["Fall 2027"]}, "term"),
        ({"sponsorship": "Does Not Offer Sponsorship"}, "Does Not Offer Sponsorship"),
        ({"title": "Software Intern (TS/SCI Clearance)"}, "citizenship/clearance required"),
        ({"locations": ["Toronto, ON"]}, "outside the US"),
        ({"locations": ["Toronto, ON", "Remote in USA"]}, None),
    ],
)
def test_filter_reason(profile, kw, reason):
    got = prefilter.filter_reason(job(**kw), profile, time.time())
    if reason is None:
        assert got is None
    else:
        assert got is not None and got.startswith(reason)


def test_select_candidates_dedupes_and_skips_decided(profile):
    state = State()
    a = job(id="a", title="ML Intern")
    b = job(id="b", title="ML Intern")  # same company + title: duplicate posting
    c = job(id="c", title="AI Infrastructure Intern")
    d = job(id="d", title="Inference Intern")
    state.jobs["d"] = job(id="d", title="Inference Intern", status="applied")
    cands, stats = prefilter.select_candidates([a, b, c, d], profile, state)
    assert [x.job.id for x in cands] == ["c", "a"] or {x.job.id for x in cands} == {"a", "c"}
    assert stats["duplicate"] == 1 and stats["already decided"] == 1


def test_merge_jobs_marks_new():
    state = State()
    assert len(merge_jobs(state, [job(id="x"), job(id="y")])) == 2
    assert [j.id for j in merge_jobs(state, [job(id="x"), job(id="z")])] == ["z"]


# ---------------------------------------------------------------- state


def test_state_roundtrip_and_encryption(tmp_path):
    state = State()
    state.set_status(job(), "applied", "ok")
    path = save_state(state, tmp_path / "state.json")
    key = generate_key()
    encrypt_file(path, tmp_path / "state.enc", key)
    assert b"Acme" not in (tmp_path / "state.enc").read_bytes()
    decrypt_file(tmp_path / "state.enc", tmp_path / "back.json", key)
    back = load_state(tmp_path / "back.json")
    assert back.jobs["greenhouse:acme:1"].status == "applied"
    with pytest.raises(Exception):
        decrypt_file(tmp_path / "state.enc", tmp_path / "x.json", generate_key())


def test_prune_keeps_decisions():
    state = State()
    old = time.time() - 100 * 86400
    state.jobs["old-candidate"] = job(id="old-candidate", status="candidate", updated_at=old)
    state.jobs["old-applied"] = job(id="old-applied", status="applied", updated_at=old)
    state.seen = {"gone": old, "old-applied": old}
    state.prune()
    assert set(state.jobs) == {"old-applied"} and set(state.seen) == {"old-applied"}


# ---------------------------------------------------------------- resume plans


def test_plan_only_uses_profile_ids(profile):
    plan = validate_plan({"summary": "sum_infra", "experience": [{"id": "exp_infra", "bullets": ["infra_3", "infra_1"]}]}, profile)
    assert plan["experience"] == [{"id": "exp_infra", "bullets": ["infra_3", "infra_1"]}]
    text = render_text(profile, plan)
    assert text.index("Triton kernel") < text.index("Cut LLM inference latency")
    assert "Built an evaluation harness" not in text


@pytest.mark.parametrize(
    "plan",
    [
        {"summary": "sum_made_up"},
        {"experience": [{"id": "exp_infra", "bullets": ["ta_1"]}]},  # bullet from another entry
        {"experience": [{"id": "exp_nope"}]},
        {"skills": {"Languages": ["Rust"]}},
        {"experience": [{"id": "exp_infra", "bullets": ["infra_1", "infra_1"]}]},
        {"free_text": "I led a team of 50"},
    ],
)
def test_plan_rejects_anything_not_in_profile(profile, plan):
    with pytest.raises(PlanError):
        validate_plan(plan, profile)


def test_profile_rejects_duplicate_ids(profile_data):
    profile_data["projects"][0]["bullets"][0]["id"] = "infra_1"
    with pytest.raises(Exception):
        Profile.model_validate(profile_data)


# ---------------------------------------------------------------- email codes


@pytest.mark.parametrize(
    "body, code",
    [
        ("Copy and paste this code into the security code field on your application: aB3dE9fG", "aB3dE9fG"),
        ("Your verification code is 482913. It expires in 10 minutes.", "482913"),
        ("Hello,\n\nUse the following one-time code\n\n  7XK29Q  \n\nThanks", "7XK29Q"),
        ("Thanks for applying to Acme! We will be in touch.", None),
    ],
)
def test_extract_code(body, code):
    assert extract_code(body) == code


# ---------------------------------------------------------------- report


def test_report_public_hides_reasons(profile):
    state = State()
    j = job(score=88, reason="Strong CUDA match with the infra internship")
    j.applied_at = time.time()
    state.set_status(j, "applied", "url")
    m = job(id="m", company="Beta", title="AI Intern", score=80)
    state.set_status(m, "manual", "Workday 需要手动申请")
    state.last_run = {"candidates": 5, "notes": ["🎉 Gamma: interview invite"]}
    public = build_report(state, profile, detail="public")
    assert "Acme" in public and "Beta" in public and "Workday 需要手动申请" in public
    assert "Strong CUDA match" not in public
    assert "@ByteYuan227" in public and "Gamma: interview invite" in public
    assert "Strong CUDA match" in build_report(state, profile, detail="full")


# ---------------------------------------------------------------- answer rules


def _ask(profile, label, kind="radio", options=("Yes", "No"), desc=""):
    from job_agent.browser.forms import suggest

    s = suggest({"key": "k", "kind": kind, "label": label, "options": list(options), "description": desc}, profile, "Acme", {})
    return s.value if s else None


@pytest.mark.parametrize(
    "label, expected",
    [
        ("Are you a U.S. citizen?", "No"),
        ("Are you a U.S. citizen, a lawful permanent resident, a refugee or asylee?", "No"),
        ("Are you a national of Cuba, Iran, North Korea or Syria?", "No"),
        ("Are you a U.S. person under export control regulations (ITAR/EAR)?", "No"),
        ("Have you ever been convicted of a felony?", "No"),
        ("Do you consent to a background check?", "Yes"),
        ("Will you now or in the future require visa sponsorship?", "Yes"),
        ("Are you legally authorized to work in the United States?", "Yes"),
    ],
)
def test_identity_and_declaration_rules(profile, label, expected):
    assert _ask(profile, label) == expected


def test_citizen_profile(profile_data):
    profile_data["work_authorization"].update(us_citizen=True, requires_sponsorship_future=False, citizenships=["United States"])
    p = Profile.model_validate(profile_data)
    assert _ask(p, "Are you a U.S. citizen?") == "Yes"
    assert _ask(p, "Are you a U.S. person under export control regulations?") == "Yes"
    assert _ask(p, "Will you now or in the future require sponsorship?") == "No"


def test_custom_answers_override_rules(profile_data):
    profile_data["custom_answers"] = [{"match": "convicted", "answer": "Yes"}, {"match": "languages", "answer": ["English", "Mandarin"]}]
    p = Profile.model_validate(profile_data)
    assert _ask(p, "Have you ever been convicted of a crime?") == "Yes"  # beats declarations
    assert _ask(p, "Which languages do you speak?", kind="checkbox_group", options=("English", "French", "Mandarin")) == ["English", "Mandarin"]
    assert _ask(p, "Which languages do you speak?", kind="text", options=()) == "English, Mandarin"


def test_agreements_accepted_but_not_factual_attestations(profile):
    assert _ask(profile, "I agree to the Terms of Service and the arbitration agreement", kind="checkbox", options=()) is True
    assert _ask(profile, "I certify my answers were written without the use of AI tools", kind="checkbox", options=()) is None
    assert _ask(profile, "I certify that I am a U.S. citizen", kind="checkbox", options=()) is False


def test_no_daily_cap_by_default(profile):
    assert profile.preferences.max_applications_per_day is None


# ---------------------------------------------------------------- JD tailoring, memory, export


def test_rewrites_keep_the_facts(profile):
    from job_agent.resume import PlanError, render_text, validate_plan

    plan = validate_plan({"rewrites": {"infra_1": "Reduced LLM serving latency by 35% with continuous batching and a "
                                                  "paged KV-cache in a PyTorch/CUDA stack."},
                          "summary_text": "M.S. student building LLM inference systems with PyTorch and Triton."}, profile)
    text = render_text(profile, plan)
    assert "Reduced LLM serving latency by 35%" in text and text.startswith("Alex Chen")
    for bad in ({"rewrites": {"infra_1": "Cut LLM inference latency by 50%."}},          # new number
                {"rewrites": {"infra_1": "Cut LLM inference latency by 35% at Meta."}},   # new organisation
                {"summary_text": "Engineer with 5 years of experience at Google."}):
        with pytest.raises(PlanError):
            validate_plan(bad, profile)


def test_memory_reuses_answers_but_not_essays(profile):
    from job_agent import memory
    from job_agent.browser.forms import suggest

    state = State()
    fields = [
        {"key": "q1", "kind": "radio", "label": "Are you comfortable working on-site 5 days a week?", "options": ["Yes", "No"]},
        {"key": "q2", "kind": "textarea", "label": "Why do you want to work at Acme?"},
        {"key": "q3", "kind": "text", "label": "Which time zone are you in?"},
    ]
    learned = memory.remember(state, fields, {"q1": "Yes", "q2": "Because Acme ...", "q3": "Eastern"}, "Acme")
    assert len(learned) == 2 and len(state.memory) == 2
    later = {"key": "x", "kind": "button_group", "label": "Are you comfortable working on-site 5 days a week?",
             "options": ["Yes", "No"]}
    s = suggest(later, profile, "Other Co", {}, state.memory)
    assert s.value == "Yes" and s.source == "memory"
    assert suggest({"key": "y", "kind": "textarea", "label": "Why do you want to work at Other Co?"}, profile,
                   "Other Co", {}, state.memory) is None


def test_export_csv(tmp_path, home):
    import csv

    from job_agent.cli import main
    from job_agent.store import save_state

    state = State()
    j = job(score=90)
    j.applied_at = time.time()
    state.set_status(j, "applied", "ok")
    save_state(state)
    out = tmp_path / "apps.csv"
    assert main(["export", str(out)]) == 0
    rows = list(csv.DictReader(out.open(encoding="utf-8-sig")))
    assert rows[0]["company"] == "Acme" and rows[0]["status"] == "applied" and rows[0]["score"] == "90"


def test_init_creates_profile_once(tmp_path, monkeypatch):
    from job_agent.cli import main

    monkeypatch.setenv("JOBAGENT_HOME", str(tmp_path / "h"))
    monkeypatch.delenv("JOBAGENT_PROFILE", raising=False)
    monkeypatch.delenv("JOBAGENT_PROFILE_YAML", raising=False)
    assert main(["init"]) == 0
    p = tmp_path / "h" / "profile.yaml"
    assert p.exists()
    p.write_text("edited")
    assert main(["init"]) == 0 and p.read_text() == "edited"  # never overwritten


def test_materials_survive_across_runs(tmp_path, home, monkeypatch, profile):
    from job_agent import materials
    from job_agent.apply import work_dir
    from job_agent.store import generate_key

    monkeypatch.setenv("JOBAGENT_STATE_KEY", generate_key())
    state = State()
    j = job(id="url:old", company="Old Co", title="Intern")
    state.set_status(j, "manual", "CAPTCHA")
    (work_dir(j) / "Alex_Chen_Resume.pdf").write_bytes(b"%PDF-old")
    bundle = tmp_path / "materials.enc"
    assert materials.export(state, bundle)["jobs"] == 1

    # Next run on a fresh machine: the work folder is gone until the bundle is restored.
    import shutil

    shutil.rmtree(work_dir(j))
    assert materials.restore(state, bundle) == {"restored": 1}
    assert (work_dir(j) / "Alex_Chen_Resume.pdf").read_bytes() == b"%PDF-old"
    out = materials.unpack(bundle, tmp_path / "out")
    assert out["jobs"] == ["Old Co - Intern"] and not (tmp_path / "out" / "manifest.json").exists()
