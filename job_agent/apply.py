"""Preparing and submitting one application.

``prepare`` opens the real form, reads every question and proposes answers
from the profile. Claude reviews that, answers what is left (only from the
profile), and calls ``submit``, which fills the form in a fresh browser,
checks nothing required is missing, submits, and records the outcome.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import inbox, vault
from .browser import forms
from .browser.launch import launch, new_context
from .profile import Profile, jobagent_home
from .resume import html_to_pdf, render_letter_html, resume_filename
from .store import Job, State

FILE_TOKENS = ("resume", "cover_letter")


class ApplyError(RuntimeError):
    pass


def work_dir(job: Job) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", job.id)
    d = jobagent_home() / "work" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def resume_path(job: Job, profile: Profile) -> Path:
    return work_dir(job) / resume_filename(profile)


SUBMIT_MARKER = "submit_clicked.txt"


def mark_submit_clicked(job: Job, where: str) -> None:
    """Written just before a submit button is pressed. If the session dies before the outcome is
    recorded, the application may have gone through, so it must never be retried automatically."""
    (work_dir(job) / SUBMIT_MARKER).write_text(f"{time.time():.0f} {where[:300]}\n", encoding="utf-8")


def submit_clicked(job: Job) -> bool:
    return (work_dir(job) / SUBMIT_MARKER).exists()


def clear_submit_marker(job: Job) -> None:
    (work_dir(job) / SUBMIT_MARKER).unlink(missing_ok=True)


def day_bounds(tz: str, day: dt.date | None = None) -> tuple[float, float]:
    zone = ZoneInfo(tz)
    day = day or dt.datetime.now(zone).date()
    start = dt.datetime.combine(day, dt.time(0), zone)
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


def applied_today(state: State, profile: Profile) -> int:
    start, end = day_bounds(profile.notify.timezone)
    return len(state.applied_between(start, end))


def _compact(field: dict[str, Any]) -> dict[str, Any]:
    out = {k: field[k] for k in ("key", "kind", "label", "required") if k in field}
    if field.get("options"):
        opts = field["options"]
        out["options"] = [o[:100] for o in opts[:40]] + ([f"... {len(opts) - 40} more"] if len(opts) > 40 else [])
    if field.get("description"):
        out["description"] = field["description"][:400]
    if field.get("suggested") is not None:
        v = field["suggested"]
        out["suggested"] = Path(v).name if field["kind"] == "file" else v
        out["source"] = field.get("source", "")
    return out


def prepare(job: Job, profile: Profile, memory: dict[str, Any] | None = None) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    if not job.auto_apply:
        raise ApplyError(f"{job.ats} applications are not automated; handle this one manually")
    wd = work_dir(job)
    files = {"resume": resume_path(job, profile)}
    if not files["resume"].exists():
        raise ApplyError("build the tailored resume first: jobagent resume <job_id> --plan plan.json")
    with sync_playwright() as pw:
        browser = launch(pw)
        page = new_context(browser).new_page()
        try:
            data = forms.open_form(page, job.apply_url)
            for f in data["fields"]:
                if f["kind"] == "combobox" and not f.get("options"):
                    f["options"] = forms.combobox_options(page, f["key"])
                s = forms.suggest(f, profile, job.company, files, memory)
                if s is not None:
                    f["suggested"], f["source"] = s.value, s.source
            page.screenshot(path=str(wd / "form.jpg"), full_page=True, type="jpeg", quality=45)
        finally:
            browser.close()
    form = {
        "job_id": job.id,
        "url": data["url"],
        "prepared_at": time.time(),
        "captcha": data["captcha"],
        "ai_policy_mentioned": data["ai_policy_mentioned"],
        "has_submit": data["has_submit"],
        "fields": data["fields"],
    }
    (wd / "form.json").write_text(json.dumps(form, indent=1), encoding="utf-8")
    fields = data["fields"]
    needs = [f["key"] for f in fields if f.get("required") and f.get("suggested") in (None, "", [])]
    return {
        "job_id": job.id,
        "company": job.company,
        "title": job.title,
        "form_url": data["url"],
        "ai_policy_mentioned": data["ai_policy_mentioned"],
        "captcha": [k for k, v in data["captcha"].items() if v],
        "n_fields": len(fields),
        "required_without_answer": needs,
        "fields": [_compact(f) for f in fields],
        "answers_file_hint": str(wd / "answers.json"),
    }


def _load_answers(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ApplyError("answers file must be a JSON object")
    if "answers" not in doc:
        doc = {"answers": doc}
    if not isinstance(doc["answers"], dict):
        raise ApplyError('"answers" must be an object mapping field key -> value')
    return doc


def merge_answers(fields: list[dict[str, Any]], doc: dict[str, Any], files: dict[str, Path]) -> tuple[dict[str, Any], list[str]]:
    """Suggestions overlaid with Claude's answers. Returns (answers by key, problems)."""
    by_key = {f["key"]: f for f in fields}
    problems = [f"unknown field key {k!r}" for k in doc["answers"] if k not in by_key]
    merged: dict[str, Any] = {f["key"]: f["suggested"] for f in fields if f.get("suggested") is not None}
    for k, v in doc["answers"].items():
        if k in by_key:
            merged[k] = v
    for f in fields:
        if f["kind"] != "file" or f["key"] not in merged or merged[f["key"]] is None:
            continue
        v = str(merged[f["key"]])
        token = next((t for t in FILE_TOKENS if v == t or Path(v).name == files.get(t, Path("~")).name), None)
        if token is None or token not in files:
            problems.append(f"{f['key']}: files can only be 'resume' or 'cover_letter' (got {Path(v).name!r})")
            merged.pop(f["key"])
        else:
            merged[f["key"]] = str(files[token])
    for f in fields:  # passwords always come from the vault, never from Claude
        if f["kind"] == "password":
            merged[f["key"]] = forms.SITE_PASSWORD
    for f in fields:
        if f.get("required") and merged.get(f["key"]) in (None, "", []):
            problems.append(f"required field unanswered: {f['key']} ({f.get('label', '')[:80]})")
    return merged, problems


def _conflict_problems(conflicts: list[dict[str, Any]]) -> list[str]:
    return [
        f"{c['key']}: answer {c['answer']!r} contradicts the profile ({c['from']} says {c['profile_says']!r}) - "
        f"question: {c['label'][:100]}"
        for c in conflicts
    ]


def submit(job: Job, profile: Profile, state: State, answers_file: Path, dry_run: bool = False,
           force: bool = False) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    wd = work_dir(job)
    form_path = wd / "form.json"
    if not form_path.exists():
        raise ApplyError("run `jobagent prepare` first")
    form = json.loads(form_path.read_text(encoding="utf-8"))
    doc = _load_answers(answers_file)

    files = {"resume": resume_path(job, profile)}
    if not files["resume"].exists():
        raise ApplyError("resume PDF missing; run `jobagent resume` first")
    letter = (doc.get("cover_letter") or "").strip()
    if letter:
        files["cover_letter"] = html_to_pdf(render_letter_html(profile, letter), wd / "Cover_Letter.pdf")

    merged, problems = merge_answers(form["fields"], doc, files)
    known = {f["key"] for f in form["fields"]}
    conflicts = forms.audit(form["fields"], profile, job.company, {k: v for k, v in doc["answers"].items() if k in known})
    if conflicts and not force:
        problems += _conflict_problems(conflicts) + [
            "re-read those questions: if the profile's answer is right, use it; only if the question really asks "
            "something else, run submit again with --force"
        ]
    if problems:
        return {"status": "incomplete", "problems": problems}

    prefs = profile.preferences
    if not dry_run:
        if not prefs.auto_apply:
            raise ApplyError("auto_apply is false in the profile; use --dry-run or mark the job manual")
        cap = prefs.max_applications_per_day
        if cap is not None and applied_today(state, profile) >= cap:
            raise ApplyError("daily application limit reached")

    fill_values = dict(merged)
    password_keys = [k for k, v in merged.items() if v == forms.SITE_PASSWORD]
    if password_keys:
        account = vault.get_or_create(state, form["url"], profile.personal.email)
        for k in password_keys:
            fill_values[k] = account.password

    result: dict[str, Any]
    with sync_playwright() as pw:
        browser = launch(pw)
        page = new_context(browser).new_page()
        try:
            data = forms.open_form(page, form["url"])
            current = {f["key"]: f for f in data["fields"]}
            for f in form["fields"]:  # keep option lists gathered during prepare
                if f["key"] in current and f.get("options") and not current[f["key"]].get("options"):
                    current[f["key"]]["options"] = f["options"]
            gone = [k for k in merged if k not in current]
            fill = forms.fill_and_verify(page, list(current.values()), fill_values)
            page.screenshot(path=str(wd / "filled.jpg"), full_page=True, type="jpeg", quality=45)
            # Read the form back: a dropdown that picked the wrong option must not be submitted.
            shown_wrong = [] if force else forms.audit(page.evaluate(forms.EXTRACT_JS)["fields"], profile, job.company)
            if fill.errors or fill.missing_required or gone or fill.unverified or shown_wrong:
                result = {
                    "status": "fill_failed",
                    "errors": fill.errors,
                    "missing_required": fill.missing_required,
                    "value_did_not_stick": fill.unverified,
                    "fields_no_longer_on_form": gone,
                    "form_shows_answers_contradicting_profile": _conflict_problems(shown_wrong),
                }
            elif dry_run:
                result = {"status": "dry_run_ok", "filled": len(fill.filled)}
            else:
                before = time.time()
                mark_submit_clicked(job, form["url"])
                page.locator("[data-ja-submit]").first.click(timeout=10000)
                result = forms.outcome(page, form["url"])
                if result["status"] == "needs_code":
                    code = inbox.wait_for_code(before, hint=job.ats)
                    if code and forms.enter_code(page, code):
                        result = forms.outcome(page, form["url"])
                        result["verification"] = "email code entered"
                    else:
                        result = {"status": "needs_code", "detail": "verification code not found in inbox"}
                page.screenshot(path=str(wd / "after_submit.jpg"), full_page=True, type="jpeg", quality=45)
        finally:
            browser.close()

    labels = {f["key"]: f.get("label", f["key"]) for f in form["fields"]}
    def shown(v: Any) -> Any:
        if v == forms.SITE_PASSWORD:
            return "(password generated and stored in the encrypted vault)"
        return Path(v).name if isinstance(v, str) and v.startswith("/") else v

    given = {labels[k][:120]: shown(v) for k, v in merged.items() if k in labels}
    if result["status"] in ("submitted", "dry_run_ok"):
        from .memory import remember

        remember(state, form["fields"], doc["answers"], job.company)
    job.attempts += 1
    job.application = {"answers": given, "outcome": result, "at": time.time(), "dry_run": dry_run}
    if dry_run:
        state.jobs[job.id] = job
        state.log(job.id, "dry_run", result["status"])
    elif result["status"] == "submitted":
        job.applied_at = time.time()
        job.result = "submitted"
        state.set_status(job, "applied", job.apply_url)
    elif result["status"] in ("captcha", "needs_code", "unknown"):
        reason = {
            "captcha": "the site asked for a CAPTCHA",
            "needs_code": "the site sent a verification code that could not be read automatically",
            "unknown": "submitted, but no confirmation page was detected - please check your email",
        }[result["status"]]
        job.result = result["status"]
        state.set_status(job, "manual", reason)
    else:  # could not fill or submit: keep everything for the candidate to apply by hand
        job.result = result["status"]
        state.set_status(job, "manual", "automatic application failed: " + json.dumps(result)[:300])
    return result
