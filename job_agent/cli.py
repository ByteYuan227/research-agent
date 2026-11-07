"""``jobagent``: the tools Claude (or you) use to run the job search.

Every command prints JSON (or Markdown for reports) so an agent can read it.
The daily routine Claude follows is in .claude/skills/job-hunt/SKILL.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from . import inbox, notify, prefilter, sources, vault
from .apply import ApplyError, applied_today, prepare, profile_for, submit, work_dir
from .profile import Preferences, ProfileError, has_profile, jobagent_home, load_profile
from .report import build_report
from .resume import PlanError, build_resume, keyword_coverage, pdf_pages, validate_plan
from .store import (
    Job,
    State,
    decrypt_file,
    encrypt_file,
    generate_key,
    load_state,
    merge_jobs,
    save_state,
    state_path,
)


def out(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def fail(msg: str, code: int = 1) -> int:
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr)
    return code


def _job(state: State, job_id: str) -> Job:
    job = state.jobs.get(job_id)
    if job is None:
        raise SystemExit(fail(f"unknown job id {job_id!r} (run `jobagent candidates`)"))
    return job


def _when(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""


def _brief(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "company": job.company,
        "title": job.title,
        "locations": job.locations[:4],
        "posted": _when(job.posted_at),
        "ats": job.ats,
        "auto_apply": job.auto_apply,
        "status": job.status,
        "score": job.score,
        "url": job.url,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_check(args) -> int:
    report: dict[str, Any] = {"home": str(jobagent_home()), "state": str(state_path())}
    try:
        p = load_profile()
        report["profile"] = "ok"
        report["auto_apply"] = p.preferences.auto_apply
        report["min_score"] = p.preferences.min_score
        report["max_applications_per_day"] = p.preferences.max_applications_per_day
    except ProfileError as exc:
        report["profile"] = str(exc).splitlines()[0] if not has_profile() else str(exc)
    report["email_codes"] = "configured" if inbox.configured() else "not configured (verification codes need you)"
    try:
        from playwright.sync_api import sync_playwright

        from .browser.launch import launch

        with sync_playwright() as pw:
            launch(pw).close()
        report["browser"] = "ok"
    except Exception as exc:  # noqa: BLE001
        report["browser"] = f"unavailable: {str(exc).splitlines()[0][:200]}"
    out(report)
    return 0


def cmd_init(args) -> int:
    """Create ~/.jobagent/profile.yaml from the template (never overwrites)."""
    import shutil

    home = jobagent_home()
    home.mkdir(parents=True, exist_ok=True)
    target = home / "profile.yaml"
    template = Path(__file__).resolve().parents[1] / "examples" / "profile.example.yaml"
    created = False
    if not target.exists():
        shutil.copy(template, target)
        target.chmod(0o600)
        created = True
    result: dict[str, Any] = {"profile": str(target), "created": created}
    if args.sample_resume:
        try:
            profile = load_profile()
        except ProfileError as exc:
            return fail(str(exc))
        result["sample_resume"] = str(build_resume(profile, None, home / "sample"))
    result["next"] = [
        "Edit the profile (or ask Claude Code: 'set up my job agent profile from my resume')",
        "jobagent check",
        "bash scripts/setup_secrets.sh  (stores everything as GitHub secrets)",
    ]
    out(result)
    return 0


def cmd_keygen(args) -> int:
    print(generate_key())
    return 0


def cmd_discover(args) -> int:
    state = load_state()
    state.prune()
    profile = None
    try:
        profile = load_profile()
        prefs = profile.preferences
    except ProfileError:
        prefs = Preferences()
    jobs, errors = sources.discover(prefs)
    new = merge_jobs(state, jobs)
    state.last_run = {
        "started": time.time(),
        "open_jobs": len(jobs),
        "new_jobs": len(new),
        "errors": errors,
        "notes": [],
        "setup_needed": profile is None,
        "candidates": 0,
    }
    result: dict[str, Any] = {"open_jobs": len(jobs), "new_today": len(new), "errors": errors}
    if profile is None:
        result["setup_needed"] = "no profile: discovery only. See docs/job-agent.md"
        save_state(state)
        out(result)
        return 0
    cands, stats = prefilter.select_candidates(jobs, profile, state)
    for c in cands:
        existing = state.jobs.get(c.job.id)
        if existing is None or existing.status in ("new", "candidate"):
            c.job.status = "candidate"
            c.job.first_seen = state.seen.get(c.job.id, time.time())
            state.jobs[c.job.id] = c.job
    state.last_run["candidates"] = len(cands)
    save_state(state)
    result["filtered_out"] = dict(sorted(stats.items(), key=lambda kv: -kv[1]))
    result["candidates"] = [{**_brief(c.job), "keyword_hits": c.why} for c in cands]
    out(result)
    return 0


def cmd_candidates(args) -> int:
    state = load_state()
    jobs = [j for j in state.jobs.values() if j.status == args.status]
    out([_brief(j) for j in sorted(jobs, key=lambda j: -(j.score or 0))])
    return 0


def cmd_enqueue(args) -> int:
    """Queue every candidate for applying without a scoring pass (the APPLY session checks blockers)."""
    state = load_state()
    n = 0
    for j in list(state.jobs.values()):
        if j.status == "candidate":
            state.set_status(j, "scored", "queued without scoring")
            n += 1
    save_state(state)
    out({"queued": n})
    return 0


def cmd_jd(args) -> int:
    from . import ats

    state = load_state()
    job = _job(state, args.job_id)
    cache = work_dir(job) / "description.txt"
    if cache.exists():
        text = cache.read_text(encoding="utf-8")
    else:
        ref = ats.AtsRef(job.ats, job.ats_company, job.ats_job_id, job.ats_region)
        try:
            text = ats.fetch_description(ref, job.url)
        except ats.PostingClosed as exc:
            state.set_status(job, "closed", str(exc))
            save_state(state)
            out({"id": job.id, "status": "closed", "reason": str(exc),
                 "note": "the posting no longer takes applications; it has been recorded as closed, move on"})
            return 0
        except Exception as exc:  # noqa: BLE001
            return fail(f"could not fetch description: {exc}")
        cache.write_text(text, encoding="utf-8")
    header = f"# {job.title} - {job.company}\nLocations: {', '.join(job.locations)}\nPosted: {_when(job.posted_at)}\n"
    if job.sponsorship and job.sponsorship != "Other":
        header += f"Listing note: {job.sponsorship}\n"
    print(header + "\n" + text[: args.max_chars])
    if len(text) > args.max_chars:
        print(f"\n[... truncated {len(text) - args.max_chars} characters]")
    return 0


def cmd_workdir(args) -> int:
    state = load_state()
    job = _job(state, args.job_id)
    out({"work_dir": str(work_dir(job))})
    return 0


def cmd_score(args) -> int:
    state = load_state()
    job = _job(state, args.job_id)
    if not 0 <= args.score <= 100:
        return fail("score must be 0-100")
    job.score = args.score
    job.reason = args.reason
    if args.decision == "skip":
        state.set_status(job, "skipped", f"{args.score}: {args.reason}")
    else:
        state.set_status(job, "scored", f"{args.score}: {args.reason}")
    save_state(state)
    out({"id": job.id, "status": job.status, "score": job.score})
    return 0


def cmd_profile(args) -> int:
    try:
        p = load_profile()
    except ProfileError as exc:
        return fail(str(exc))
    print(json.dumps(p.model_dump(), ensure_ascii=False, indent=1))
    return 0


def cmd_resume(args) -> int:
    state = load_state()
    job = _job(state, args.job_id)
    profile = load_profile()
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8")) if args.plan else None
    try:
        plan = validate_plan(plan, profile)
    except PlanError as exc:
        return fail(f"plan rejected: {exc}")
    pdf = build_resume(profile, plan, work_dir(job))
    pages = pdf_pages(pdf)
    job.resume_plan = plan
    state.jobs[job.id] = job
    save_state(state)
    result: dict[str, Any] = {"pdf": str(pdf), "pages": pages, "warning": "longer than one page" if pages > 1 else None}
    desc = work_dir(job) / "description.txt"
    if desc.exists():  # which of the posting's technologies this resume shows (run `jobagent jd` first)
        result.update(keyword_coverage(profile, desc.read_text(encoding="utf-8"),
                                       (work_dir(job) / "resume.txt").read_text(encoding="utf-8")))
    out(result)
    return 0


def cmd_prepare(args) -> int:
    state = load_state()
    job = _job(state, args.job_id)
    try:
        result = prepare(job, profile_for(job, load_profile()), state.memory)
    except ApplyError as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return fail(f"could not open the application form: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
    state.set_status(job, "applying", "form prepared")
    save_state(state)
    out(result)
    return 0


def cmd_submit(args) -> int:
    import os

    state = load_state()
    job = _job(state, args.job_id)
    # JOBAGENT_DRY_RUN=1 makes rehearsals safe even if the --dry-run flag is forgotten.
    dry_run = args.dry_run or os.environ.get("JOBAGENT_DRY_RUN") == "1"
    try:
        result = submit(job, profile_for(job, load_profile()), state, Path(args.answers), dry_run=dry_run,
                        force=args.force)
    except ApplyError as exc:
        return fail(str(exc))
    save_state(state)
    out({"id": job.id, "status": job.status, **result})
    return 0


def cmd_mark(args) -> int:
    from .apply import clear_submit_marker

    state = load_state()
    job = _job(state, args.job_id)
    if args.status == "scored":  # you checked: it was not submitted, try again
        clear_submit_marker(job)
        job.retries = 0
    state.set_status(job, args.status, args.reason)
    save_state(state)
    out({"id": job.id, "status": job.status})
    return 0


def cmd_budget(args) -> int:
    state = load_state()
    p = load_profile()
    done = applied_today(state, p)
    out({
        "applied_today": done,
        "remaining_today": "unlimited" if p.preferences.max_applications_per_day is None
        else max(0, p.preferences.max_applications_per_day - done),
        "auto_apply": p.preferences.auto_apply,
        "min_score": p.preferences.min_score,
    })
    return 0


def cmd_inbox(args) -> int:
    if not inbox.configured():
        out({"configured": False, "messages": []})
        return 0
    try:
        out({"configured": True, "messages": inbox.recent(args.hours)})
    except Exception as exc:  # noqa: BLE001
        return fail(f"IMAP error: {exc}")
    return 0


def cmd_note(args) -> int:
    state = load_state()
    state.last_run.setdefault("notes", []).append(args.text[:600])
    save_state(state)
    out({"ok": True})
    return 0


def cmd_report(args) -> int:
    state = load_state()
    try:
        profile = load_profile()
    except ProfileError:
        profile = None
    day = dt.date.fromisoformat(args.date) if args.date else None
    md = build_report(state, profile, day, args.detail)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    else:
        print(md)
    return 0


def cmd_notify(args) -> int:
    body = Path(args.report).read_text(encoding="utf-8")
    title = args.title or body.splitlines()[0].lstrip("# ").strip()
    result: dict[str, Any] = {}
    try:
        profile = load_profile()
        to, use_issue = profile.notify.email_to, profile.notify.github_issue
    except ProfileError:
        to, use_issue = "", True
    try:
        if use_issue:
            result["issue"] = notify.github_issue(title, body)
        else:  # email only: nothing about your applications is posted in the repository
            result["issue"] = "off (notify.github_issue: false)"
            result["closed_old_report_issues"] = notify.close_reports()
    except Exception as exc:  # noqa: BLE001
        result["issue_error"] = str(exc)[:300]
    if to:
        from . import materials

        try:
            state_now = load_state()
            since = state_now.last_run.get("started", time.time() - 86400)
            files = materials.resumes_for_email(state_now, since)
            review, applied_files = materials.applied_review(state_now, since)
            if applied_files or "## " in review:
                review_path = Path(args.report).with_name("applications_review.md")
                review_path.write_text(review, encoding="utf-8")
                files = [(review_path, "applications_review.md")] + applied_files[:25] + files
            result["email"] = notify.email_report(title, body, to, files)
            result["email_attachments"] = len(files)
        except Exception as exc:  # noqa: BLE001
            result["email_error"] = str(exc)[:300]
    out(result)
    return 0


def cmd_status(args) -> int:
    state = load_state()
    counts: dict[str, int] = {}
    for j in state.jobs.values():
        counts[j.status] = counts.get(j.status, 0) + 1
    out({"jobs": counts, "seen": len(state.seen), "last_run": state.last_run})
    return 0


def cmd_state(args) -> int:
    path = state_path()
    if args.action == "export":
        if not path.exists():
            save_state(State())
        encrypt_file(path, Path(args.file))
    else:
        src = Path(args.file)
        if not src.exists():
            out({"imported": False, "reason": "no saved state yet"})
            return 0
        decrypt_file(src, path)
    out({args.action: True})
    return 0


def cmd_account(args) -> int:
    state = load_state()
    if args.action == "list":
        out([{"site": a.site, "username": a.username, "created": _when(a.created_at)} for a in state.accounts.values()])
        return 0
    acct = state.accounts.get(vault.site_of(args.site or ""))
    if acct is None:
        return fail(f"no saved account for {args.site!r}")
    if not vault.can_reveal():
        return fail("passwords are never printed in CI; run this on your own computer")
    out({"site": acct.site, "username": acct.username, "password": acct.password})
    return 0


def cmd_web(args) -> int:
    from . import web

    if args.action == "close":
        web.stop()
        out({"closed": True})
        return 0
    state = load_state()
    try:
        profile = load_profile()
        job_id = args.arg if args.action == "open" else web._session().get("job_id")
        if job_id in state.jobs:
            profile = profile_for(state.jobs[job_id], profile)
        if args.action == "open":
            if not args.arg:
                return fail("usage: jobagent web open <job_id>")
            result = web.open_job(state, _job(state, args.arg), profile)
        elif args.action == "look":
            with web.page() as pg:
                result = web.look(pg, web._current_job(state), profile, memory=state.memory)
        elif args.action == "click":
            result = web.click(state, profile, args.arg or "", force=args.force)
        elif args.action == "goto":
            result = web.goto(state, profile, args.arg or "")
        elif args.action == "fill":
            answers = {}
            if args.answers:
                doc = json.loads(Path(args.answers).read_text(encoding="utf-8"))
                answers = doc.get("answers", doc)
            if args.json:
                doc = json.loads(args.json)
                answers.update(doc.get("answers", doc))
            result = web.fill(state, profile, answers, overwrite=args.overwrite)
        elif args.action == "code":
            result = web.email_code(state, profile, args.hint or "")
        elif args.action == "link":
            result = web.email_link(state, profile, args.match or args.arg or "")
        elif args.action == "shot":
            result = {"screenshot": web.screenshot(state, profile, args.arg or "page")}
        elif args.action == "finish":
            result = web.finish(state, profile, args.arg or "", args.reason or "")
        else:
            return fail(f"unknown web action {args.action}")
    except web.WebError as exc:
        save_state(state)
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - report browser errors to Claude instead of crashing
        save_state(state)
        return fail(f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
    save_state(state)
    out(result)
    return 0


COMPANY_WINDOW_DAYS = 30


# Cheapest and most likely to succeed first: one-step forms, then other sites, Workday last.
ATS_TIER = {"greenhouse": 0, "lever": 0, "ashby": 0, "workable": 1, "smartrecruiters": 1, "other": 1, "tiktok": 1,
            "workday": 2}


def queue_order(job: Job, now: float) -> tuple[int, float]:
    """Applicant systems that usually succeed first, then best match, with a head start for fresh postings."""
    age = (now - job.posted_at) / 86400 if job.posted_at else 99
    return ATS_TIER.get(job.ats, 1), -((job.score or 0) + (8 if age < 1 else 4 if age < 3 else 0))


# Postings whose application needs something only you can do (sign in with your own Google account).
NEEDS_YOU = re.compile(r"(^|\.)google\.com/about/careers|careers\.google\.com", re.I)


def cmd_queue(args) -> int:
    """Job ids waiting to be applied to, best first (one per line, for scripts).

    Jobs beyond preferences.max_per_company applications to the same company in 30 days are skipped."""
    from .prefilter import company_key

    state = load_state()
    now = time.time()
    try:
        cap = load_profile().preferences.max_per_company
    except ProfileError:
        cap = None
    since = now - COMPANY_WINDOW_DAYS * 86400
    per_company: dict[str, int] = {}
    for j in state.jobs.values():
        recent = (j.applied_at or 0) >= since if j.status in ("applied", "applied_manual") else \
            j.status == "manual" and j.updated_at >= since
        if recent:
            per_company[company_key(j.company)] = per_company.get(company_key(j.company), 0) + 1
    changed = False
    for j in sorted((j for j in state.jobs.values() if j.status == "scored"), key=lambda j: queue_order(j, now)):
        if NEEDS_YOU.search(j.apply_url or j.url):
            state.set_status(j, "manual", "Google Careers needs you to sign in with your own Google account")
            changed = True
            continue
        key = company_key(j.company)
        if cap is not None and per_company.get(key, 0) >= cap:
            state.set_status(j, "skipped", f"already {per_company[key]} applications to {j.company} in the last "
                                           f"{COMPANY_WINDOW_DAYS} days (preferences.max_per_company = {cap})")
            changed = True
            continue
        per_company[key] = per_company.get(key, 0) + 1
        print(f"{j.id} {j.score or 0}" if args.scores else j.id)
    if changed:
        save_state(state)
    return 0


MAX_RETRIES = 1


def cmd_settle(args) -> int:
    """After an apply session: make sure the job got an outcome."""
    import os

    from .apply import submit_clicked
    from .web import _save_session

    state = load_state()
    job = state.jobs.get(args.job_id)
    if job is None:
        return 0
    _save_session({})
    if job.status in ("scored", "applying"):
        if os.environ.get("JOBAGENT_DRY_RUN") == "1" and not submit_clicked(job):
            # A rehearsal never submits: the job stays in the queue for the first real run.
            state.set_status(job, "scored", "rehearsed (dry run); will apply for real in the next run")
        elif submit_clicked(job):
            # It may have gone through: never apply twice. You check your e-mail and tick it off.
            state.set_status(job, "manual", "a submit button was pressed but the session ended before the result was "
                                            "recorded: check your e-mail for a confirmation before applying again")
        elif args.stopped in ("quota", "auth"):
            state.set_status(job, "scored", f"interrupted ({args.stopped}); will retry next run")
        elif str(args.exit) not in ("0", "") and job.retries < MAX_RETRIES:
            # A timeout or crash (slow site, browser hiccup) gets one more try in the next run.
            job.retries += 1
            state.set_status(job, "scored", f"the session ended early (exit {args.exit}); will retry next run")
        else:
            state.set_status(job, "manual", f"the automatic application did not finish (exit {args.exit})")
        save_state(state)
    out({"id": job.id, "status": job.status})
    return 0


def cmd_sync_checklist(args) -> int:
    """Mark jobs you applied to by hand as applied: boxes ticked in report issues, or
    "done <numbers>" replies to the emailed report."""
    import os

    state = load_state()
    notes: list[str] = []
    checked: list[str] = []
    try:
        checked = notify.checked_jobs_in_reports()
    except Exception as exc:  # noqa: BLE001 - not on GitHub, or no token
        notes.append(str(exc)[:200])
    if inbox.configured():
        try:
            profile = load_profile()
            own = {os.environ.get("JOBAGENT_IMAP_USER", ""), profile.personal.email, profile.notify.email_to}
            numbers = notify.done_numbers_from_inbox(own, time.time() - 21 * 86400)
            checked += [j.id for j in state.jobs.values() if j.manual_no in numbers]
        except Exception as exc:  # noqa: BLE001
            notes.append(f"inbox: {str(exc)[:200]}")
    n = 0
    for jid in dict.fromkeys(checked):
        job = state.jobs.get(jid)
        if job and job.status == "manual":
            job.applied_at = job.applied_at or time.time()
            state.set_status(job, "applied_manual", "marked as applied by you in the daily report")
            n += 1
    save_state(state)
    out({"synced": n, **({"note": "; ".join(notes)} if notes else {})})
    return 0


def cmd_materials(args) -> int:
    """Tailored resumes and prepared answers for jobs you apply to yourself."""
    from . import materials

    if args.action == "export":
        out(materials.export(load_state(), Path(args.file)))
    elif args.action == "restore":
        out(materials.restore(load_state(), Path(args.file)))
    else:
        out(materials.unpack(Path(args.file), Path(args.out)))
    return 0


def cmd_documents(args) -> int:
    """Documents you supply (an unofficial transcript), uploaded only where a form requires them."""
    from . import documents

    try:
        if args.action == "add":
            if not args.file:
                return fail("usage: jobagent documents add transcript <file.pdf>")
            out({"added": str(documents.add(args.kind, Path(args.file)))})
        elif args.action in ("export", "restore"):  # `jobagent documents export documents.enc`
            bundle = Path(args.file or (args.kind if args.kind != "transcript" else "documents.enc"))
            out(documents.export(bundle) if args.action == "export" else documents.restore(bundle))
        else:
            out({k: str(v) for k, v in documents.available().items()})
    except (ValueError, RuntimeError, OSError) as exc:
        return fail(str(exc))
    return 0


def cmd_memory(args) -> int:
    """Answers learned from earlier applications (forget one if it is wrong)."""
    state = load_state()
    if args.forget:
        removed = state.memory.pop(args.forget, None)
        save_state(state)
        out({"forgotten": bool(removed)})
        return 0
    out([{"key": k, **v} for k, v in sorted(state.memory.items(), key=lambda kv: -kv[1].get("uses", 0))])
    return 0


def cmd_export(args) -> int:
    """All tracked applications as CSV (open it in Excel / Google Sheets)."""
    import csv

    state = load_state()
    rows = sorted((j for j in state.jobs.values() if j.status in ("applied", "applied_manual", "manual", "skipped", "closed", "duplicate")),
                  key=lambda j: -(j.applied_at or j.updated_at or 0))
    with open(args.file, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "status", "company", "title", "score", "locations", "url", "reason"])
        for j in rows:
            when = _when(j.applied_at or j.updated_at)
            w.writerow([when, j.status, j.company, j.title, j.score if j.score is not None else "",
                        "; ".join(j.locations), j.url, j.reason])
    out({"file": args.file, "rows": len(rows)})
    return 0


def cmd_first_run(args) -> int:
    """Has the agent ever filled a form (dry run or real)? Used to make the first scheduled run a rehearsal."""
    state = load_state()
    done = any(e.type in ("dry_run", "applied") for e in state.events)
    out({"first_run": not done})
    return 0


def cmd_set_run(args) -> int:
    state = load_state()
    state.last_run[args.key] = args.value
    save_state(state)
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobagent", description="Job search agent tools (JSON in, JSON out).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create ~/.jobagent/profile.yaml from the template")
    s.add_argument("--sample-resume", action="store_true", help="also render your full resume as a PDF to check it")
    s.set_defaults(fn=cmd_init)
    sub.add_parser("check", help="check profile, browser and email setup").set_defaults(fn=cmd_check)
    sub.add_parser("keygen", help="print a new state encryption key").set_defaults(fn=cmd_keygen)
    sub.add_parser("discover", help="fetch open jobs, filter, and list candidates").set_defaults(fn=cmd_discover)

    s = sub.add_parser("candidates", help="list jobs by status")
    s.add_argument("--status", default="candidate")
    s.set_defaults(fn=cmd_candidates)

    s = sub.add_parser("jd", help="print a job description")
    s.add_argument("job_id")
    s.add_argument("--max-chars", type=int, default=4500)
    s.set_defaults(fn=cmd_jd)

    s = sub.add_parser("workdir", help="folder for this job's plan, answers, resume and screenshots")
    s.add_argument("job_id")
    s.set_defaults(fn=cmd_workdir)

    s = sub.add_parser("score", help="record a match score (0-100)")
    s.add_argument("job_id")
    s.add_argument("score", type=int)
    s.add_argument("--reason", required=True)
    s.add_argument("--decision", choices=["apply", "skip"], required=True)
    s.set_defaults(fn=cmd_score)

    sub.add_parser("profile", help="print the candidate profile").set_defaults(fn=cmd_profile)

    s = sub.add_parser("resume", help="build the tailored resume PDF from a plan")
    s.add_argument("job_id")
    s.add_argument("--plan", help="JSON plan file (see job_agent/resume.py); omit for the full resume")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("prepare", help="open the application form and propose answers")
    s.add_argument("job_id")
    s.set_defaults(fn=cmd_prepare)

    s = sub.add_parser("submit", help="fill and submit the application")
    s.add_argument("job_id")
    s.add_argument("--answers", required=True, help='JSON: {"answers": {key: value}, "cover_letter": "..."}')
    s.add_argument("--dry-run", action="store_true", help="fill everything but do not submit")
    s.add_argument("--force", action="store_true",
                   help="submit although an answer differs from the profile's (only after re-reading that question)")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("mark", help="record a decision without applying")
    s.add_argument("job_id")
    s.add_argument("--status", choices=["manual", "skipped", "failed", "scored", "applied_manual", "closed", "duplicate"],
                   required=True,
                   help="scored = try again next run; applied_manual = you applied yourself; "
                        "closed = posting no longer open; duplicate = already applied")
    s.add_argument("--reason", required=True)
    s.set_defaults(fn=cmd_mark)

    sub.add_parser("budget", help="applications left today").set_defaults(fn=cmd_budget)

    s = sub.add_parser("inbox", help="recent emails (read-only) for classifying replies")
    s.add_argument("--hours", type=float, default=26)
    s.set_defaults(fn=cmd_inbox)

    s = sub.add_parser("note", help="add a note to today's report")
    s.add_argument("text")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("report", help="print the daily report (Markdown)")
    s.add_argument("--date")
    s.add_argument("--out")
    s.add_argument("--detail", choices=["public", "full"])
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("notify", help="post the report as a GitHub issue / email")
    s.add_argument("--report", required=True)
    s.add_argument("--title")
    s.set_defaults(fn=cmd_notify)

    sub.add_parser("status", help="counts by status").set_defaults(fn=cmd_status)

    s = sub.add_parser("state", help="encrypt/decrypt state for storage outside this machine")
    s.add_argument("action", choices=["export", "import"])
    s.add_argument("file")
    s.set_defaults(fn=cmd_state)

    s = sub.add_parser("account", help="site accounts the agent created (list / password, on your own machine)")
    s.add_argument("action", choices=["list", "password"])
    s.add_argument("--site")
    s.set_defaults(fn=cmd_account)

    s = sub.add_parser(
        "web",
        help="drive a browser step by step (any application site): open|look|click|fill|goto|code|link|shot|finish|close",
    )
    s.add_argument("action", choices=["open", "look", "click", "fill", "goto", "code", "link", "shot", "finish", "close"])
    s.add_argument("arg", nargs="?",
                   help="job id (open), element ref (click), url (goto), status (finish: applied|manual|closed|duplicate|dry_run)")
    s.add_argument("--answers", help="answers JSON file for fill")
    s.add_argument("--json", help='answers inline for fill, e.g. \'{"field_key": "value"}\'')
    s.add_argument("--overwrite", action="store_true", help="fill also refills fields that already have a value")
    s.add_argument("--hint", help="code: sender/subject hint")
    s.add_argument("--match", help="link: text the verification URL must contain")
    s.add_argument("--reason", help="finish: what happened")
    s.add_argument("--force", action="store_true",
                   help="click: press a submit button although an answer differs from the profile's (re-read it first)")
    s.set_defaults(fn=cmd_web)

    sub.add_parser("enqueue", help="queue every candidate without scoring").set_defaults(fn=cmd_enqueue)

    s = sub.add_parser("queue", help="job ids to apply to, best first")
    s.add_argument("--scores", action="store_true", help="print '<id> <score>' per line")
    s.set_defaults(fn=cmd_queue)

    s = sub.add_parser("settle", help="(used by daily_run.sh) make sure a job got an outcome")
    s.add_argument("job_id")
    s.add_argument("--stopped", default="")
    s.add_argument("--exit", default="0")
    s.set_defaults(fn=cmd_settle)

    sub.add_parser("sync-checklist", help="mark jobs ticked in daily reports as applied by you").set_defaults(
        fn=cmd_sync_checklist)

    s = sub.add_parser("materials", help="resumes + answers for manual applications (encrypted bundle)")
    s.add_argument("action", choices=["export", "open", "restore"])
    s.add_argument("file", help="bundle path (materials.enc)")
    s.add_argument("--out", default="jobagent-materials", help="open: folder to unpack into")
    s.set_defaults(fn=cmd_materials)

    s = sub.add_parser("documents", help="documents you supply (transcript), uploaded only where required")
    s.add_argument("action", choices=["add", "list", "export", "restore"])
    s.add_argument("kind", nargs="?", default="transcript", help="add: the document kind (transcript)")
    s.add_argument("file", nargs="?", help="add: the PDF; export/restore: the encrypted bundle (documents.enc)")
    s.set_defaults(fn=cmd_documents)

    s = sub.add_parser("memory", help="answers learned from earlier applications")
    s.add_argument("--forget", help="question key to forget")
    s.set_defaults(fn=cmd_memory)

    s = sub.add_parser("export", help="all tracked applications as a CSV file")
    s.add_argument("file", nargs="?", default="applications.csv")
    s.set_defaults(fn=cmd_export)

    sub.add_parser("first-run", help="has the agent never filled a form yet?").set_defaults(fn=cmd_first_run)

    s = sub.add_parser("set-run", help="(used by scripts) store a value in the run record")
    s.add_argument("key")
    s.add_argument("value")
    s.set_defaults(fn=cmd_set_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
