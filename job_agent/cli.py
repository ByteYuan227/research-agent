"""``jobagent``: the tools Claude (or you) use to run the job search.

Every command prints JSON (or Markdown for reports) so an agent can read it.
The daily routine Claude follows is in .claude/skills/job-hunt/SKILL.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import inbox, notify, prefilter, sources
from .apply import ApplyError, applied_today, prepare, resume_path, submit, work_dir
from .profile import Preferences, ProfileError, has_profile, jobagent_home, load_profile
from .report import build_report
from .resume import PlanError, build_resume, validate_plan
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
    pages = pdf.read_bytes().count(b"/Type /Page") - pdf.read_bytes().count(b"/Type /Pages")
    job.resume_plan = plan
    state.jobs[job.id] = job
    save_state(state)
    out({"pdf": str(pdf), "pages": pages, "warning": "longer than one page" if pages > 1 else None})
    return 0


def cmd_prepare(args) -> int:
    state = load_state()
    job = _job(state, args.job_id)
    try:
        result = prepare(job, load_profile())
    except ApplyError as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return fail(f"could not open the application form: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
    state.set_status(job, "applying", "form prepared")
    save_state(state)
    out(result)
    return 0


def cmd_submit(args) -> int:
    state = load_state()
    job = _job(state, args.job_id)
    try:
        result = submit(job, load_profile(), state, Path(args.answers), dry_run=args.dry_run)
    except ApplyError as exc:
        return fail(str(exc))
    save_state(state)
    out({"id": job.id, "status": job.status, **result})
    return 0


def cmd_mark(args) -> int:
    state = load_state()
    job = _job(state, args.job_id)
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
        "remaining_today": max(0, p.preferences.max_applications_per_day - done),
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
        result["issue"] = notify.github_issue(title, body)
    except Exception as exc:  # noqa: BLE001
        result["issue_error"] = str(exc)[:300]
    try:
        profile = load_profile()
        to = profile.notify.email_to
    except ProfileError:
        to = ""
    if to:
        try:
            result["email"] = notify.email_report(title, body, to)
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


def cmd_set_run(args) -> int:
    state = load_state()
    state.last_run[args.key] = args.value
    save_state(state)
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobagent", description="Job search agent tools (JSON in, JSON out).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="check profile, browser and email setup").set_defaults(fn=cmd_check)
    sub.add_parser("keygen", help="print a new state encryption key").set_defaults(fn=cmd_keygen)
    sub.add_parser("discover", help="fetch open jobs, filter, and list candidates").set_defaults(fn=cmd_discover)

    s = sub.add_parser("candidates", help="list jobs by status")
    s.add_argument("--status", default="candidate")
    s.set_defaults(fn=cmd_candidates)

    s = sub.add_parser("jd", help="print a job description")
    s.add_argument("job_id")
    s.add_argument("--max-chars", type=int, default=7000)
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
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("mark", help="record a decision without applying")
    s.add_argument("job_id")
    s.add_argument("--status", choices=["manual", "skipped", "failed"], required=True)
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

    s = sub.add_parser("set-run", help=argparse.SUPPRESS)
    s.add_argument("key")
    s.add_argument("value")
    s.set_defaults(fn=cmd_set_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
