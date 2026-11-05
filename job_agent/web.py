"""A browser Claude drives step by step, for application sites without a fixed flow.

Greenhouse, Lever and Ashby forms are filled in one go by ``apply.py``. Every
other site (Workday, iCIMS, SmartRecruiters, TikTok careers, company portals)
needs a sequence of pages: sign up, verify an email, fill several steps, review,
submit. For those, ``jobagent web ...`` keeps one Chromium running between
commands and gives Claude small, safe actions:

    web open <job_id>      start on the posting (or its application page)
    web look               fields (with suggested answers), buttons, alerts, text
    web click <ref>        press a button or link from `look`
    web fill [answers]     fill fields: profile suggestions + Claude's answers
    web code / web link    finish email verification (code or link) from the inbox
    web finish <status>    record applied / manual / failed / dry_run

Safety:
* Personal data is typed only into the employer's own site or a known applicant
  tracking system (prompt-injected pages cannot redirect it elsewhere).
* Passwords come from the vault; Claude never sees them.
* With JOBAGENT_DRY_RUN=1, buttons that submit or create accounts are blocked.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from . import inbox, vault
from .browser import forms
from .browser.launch import USER_AGENT
from .profile import Profile, jobagent_home
from .store import Job, State

PAGE_SUMMARY_JS = (Path(__file__).parent / "browser" / "page_summary.js").read_text(encoding="utf-8")

# Applicant tracking systems and job portals that host applications for many employers.
ATS_DOMAINS = (
    "myworkdayjobs.com", "myworkdaysite.com", "workday.com", "greenhouse.io", "lever.co", "ashbyhq.com",
    "icims.com", "smartrecruiters.com", "workable.com", "jobvite.com", "taleo.net", "oraclecloud.com",
    "successfactors.com", "successfactors.eu", "sapsf.com", "eightfold.ai", "lifeattiktok.com", "tiktok.com",
    "bytedance.com", "ultipro.com", "ukg.com", "paylocity.com", "bamboohr.com", "rippling.com", "dayforcehcm.com",
    "applytojob.com", "breezy.hr", "recruitee.com", "teamtailor.com", "pinpointhq.com", "jazzhr.com", "adp.com",
    "avature.net", "phenompeople.com", "brassring.com", "kenexa.com", "amazon.jobs", "metacareers.com",
    "careers.microsoft.com", "jobs.apple.com", "careers.google.com", "comeet.com", "gem.com", "dover.com",
    "wellfound.com", "ycombinator.com", "workatastartup.com", "hire.trakstar.com", "hrmdirect.com",
)
SIDE_EFFECT_RE = re.compile(
    r"\b(submit|send|create (an |my )?account|sign ?up|register|verify|confirm|finish|complete (my )?application|"
    r"review and submit)\b",
    re.I,
)
COOKIE_RE = re.compile(r"^(accept( all)?( cookies)?|allow all( cookies)?|i accept|i agree|agree|got it|ok(ay)?|accept & close)$", re.I)


class WebError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Session bookkeeping
# ---------------------------------------------------------------------------


def _dir() -> Path:
    d = jobagent_home() / "browser"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _port() -> int:
    return int(os.environ.get("JOBAGENT_CDP_PORT", "9333"))


def _endpoint() -> str:
    return f"http://127.0.0.1:{_port()}"


def _session() -> dict[str, Any]:
    f = _dir() / "session.json"
    return json.loads(f.read_text()) if f.exists() else {}


def _save_session(data: dict[str, Any]) -> None:
    (_dir() / "session.json").write_text(json.dumps(data, indent=1))


def dry_run() -> bool:
    return os.environ.get("JOBAGENT_DRY_RUN") == "1"


# ---------------------------------------------------------------------------
# Browser process
# ---------------------------------------------------------------------------


def _alive() -> bool:
    try:
        return httpx.get(_endpoint() + "/json/version", timeout=2, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


def start() -> None:
    if _alive():
        return
    exe = os.environ.get("JOBAGENT_CHROMIUM")
    if not exe:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
    args = [
        exe, "--headless=new", f"--remote-debugging-port={_port()}", f"--user-data-dir={_dir() / 'profile'}",
        "--no-first-run", "--no-default-browser-check", "--window-size=1280,1600", "--lang=en-US",
        f"--user-agent={USER_AGENT}", "--disable-dev-shm-usage",
        # Like Playwright's own launcher: CI runners (e.g. Ubuntu 24.04) block Chromium's sandbox.
        "--no-sandbox",
    ]
    log = open(_dir() / "browser.log", "ab")  # noqa: SIM115 - handed to the child process
    proc = subprocess.Popen(args, stdout=log, stderr=log, start_new_session=True)
    (_dir() / "browser.pid").write_text(str(proc.pid))
    deadline = time.time() + 30
    while time.time() < deadline:
        if _alive():
            return
        if proc.poll() is not None:
            break
        time.sleep(0.3)
    tail = (_dir() / "browser.log").read_bytes()[-600:].decode(errors="replace")
    raise WebError(f"browser did not start: {tail.strip().splitlines()[-1] if tail.strip() else 'no output'}")


def stop() -> None:
    pid_file = _dir() / "browser.pid"
    if pid_file.exists():
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGTERM)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
        pid_file.unlink(missing_ok=True)


@contextlib.contextmanager
def page() -> Iterator[Any]:
    """The browser's current tab (the newest one, so links that open new tabs are followed)."""
    from playwright.sync_api import sync_playwright

    start()
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(_endpoint())
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        tabs = [t for t in ctx.pages if not t.url.startswith(("devtools://", "chrome://"))]
        yield tabs[-1] if tabs else ctx.new_page()


class FrameView:
    """An embedded frame that also offers the page-level pieces forms.py uses (keyboard, title)."""

    def __init__(self, frame: Any) -> None:
        self._frame = frame
        self.keyboard = frame.page.keyboard
        self.context = frame.page.context

    def __getattr__(self, name: str) -> Any:
        return getattr(self._frame, name)

    def title(self) -> str:
        return self._frame.page.title()


COUNT_CONTROLS_JS = "() => document.querySelectorAll('input:not([type=hidden]), select, textarea').length"


def target(pg, job: Job | None) -> Any:
    """Where the application form lives: the page itself, or a form embedded in an iframe
    (iCIMS portals, Greenhouse boards embedded on company sites) on an allowed domain."""
    try:
        main_n = pg.main_frame.evaluate(COUNT_CONTROLS_JS)
    except Exception:  # noqa: BLE001
        return pg
    best, best_n = None, main_n
    for frame in pg.frames:
        if frame == pg.main_frame or not frame.url.startswith("http") or not allowed(frame.url, job):
            continue
        try:
            n = frame.evaluate(COUNT_CONTROLS_JS)
        except Exception:  # noqa: BLE001
            continue
        if n > best_n:  # embedded application forms (iCIMS) can start with a single e-mail field
            best, best_n = frame, n
    return FrameView(best) if best is not None else pg


def _settle(pg, ms: int = 1200) -> None:
    try:
        pg.wait_for_load_state("domcontentloaded", timeout=15000)
        pg.wait_for_load_state("networkidle", timeout=8000)
    except Exception:  # noqa: BLE001 - some pages never go idle
        pass
    pg.wait_for_timeout(ms)


# ---------------------------------------------------------------------------
# Where personal data may be typed
# ---------------------------------------------------------------------------


def _site(host: str) -> str:
    host = host.lower().strip(".")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "ac", "org", "net", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def allowed(url: str, job: Job | None) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if any(host == d or host.endswith("." + d) for d in ATS_DOMAINS):
        return True
    if job is not None:
        own = {_site(urlparse(u).hostname or "") for u in (job.url, job.apply_url) if u}
        return _site(host) in own
    return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _current_job(state: State) -> Job:
    jid = _session().get("job_id")
    if not jid or jid not in state.jobs:
        raise WebError("no job open; run `jobagent web open <job_id>` first")
    return state.jobs[jid]


def _files(job: Job, profile: Profile) -> dict[str, Path]:
    from .apply import resume_path, work_dir

    files = {}
    r = resume_path(job, profile)
    if r.exists():
        files["resume"] = r
    cl = work_dir(job) / "Cover_Letter.pdf"
    if cl.exists():
        files["cover_letter"] = cl
    return files


def look(pg, job: Job, profile: Profile, gather_options: bool = True, memory: dict[str, Any] | None = None) -> dict[str, Any]:
    from .apply import _compact

    top = pg
    pg = target(top, job)
    data = pg.evaluate(forms.EXTRACT_JS)
    files = _files(job, profile)
    for f in data["fields"]:
        if gather_options and f["kind"] in ("combobox", "listbox") and not f.get("options") and not f.get("filled"):
            f["options"] = forms.combobox_options(pg, f["key"], limit=60)
        s = forms.suggest(f, profile, job.company, files, memory)
        if s is not None:
            f["suggested"], f["source"] = s.value, s.source
    summary = pg.evaluate(PAGE_SUMMARY_JS)
    fields = []
    for f in data["fields"]:
        c = _compact(f)
        c["filled"] = bool(f.get("filled"))
        fields.append(c)
    text = summary["text"]
    return {
        "url": top.url,
        "form_in_frame": pg.url if pg is not top else None,
        "title": top.title(),
        "headings": summary["headings"],
        "alerts": summary["alerts"],
        "fields": fields,
        "required_empty": [f["key"] for f in fields if f.get("required") and not f["filled"]],
        "buttons": summary["clickables"],
        "captcha_challenge": forms._visible_challenge(top),
        "looks_submitted": bool(forms.SUCCESS_RE.search(text)),
        "asks_for_code": bool(forms.CODE_RE.search(text)),
        "text": text[:2500],
        "may_enter_personal_data": allowed(pg.url, job),
        "dry_run": dry_run(),
    }


def dismiss_cookie_banner(pg) -> bool:
    """Accept the cookie banner if one covers the page (it would intercept clicks otherwise)."""
    for name in ("button", "link"):
        for el in pg.get_by_role(name).all()[:80]:
            try:
                text = (el.inner_text(timeout=500) or "").strip()
                if COOKIE_RE.match(text) and el.is_visible():
                    el.click(timeout=3000)
                    pg.wait_for_timeout(500)
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def open_job(state: State, job: Job, profile: Profile) -> dict[str, Any]:
    _save_session({"job_id": job.id, "opened": time.time(), "last_action": time.time()})
    with page() as pg:
        pg.goto(job.apply_url or job.url, wait_until="domcontentloaded", timeout=60000)
        _settle(pg)
        dismiss_cookie_banner(pg)
        return look(pg, job, profile, memory=state.memory)


def click(state: State, profile: Profile, ref: str) -> dict[str, Any]:
    job = _current_job(state)
    with page() as pg:
        button = target(pg, job).locator(f'[data-ja-ref="{ref}"]').first
        if not button.count():
            raise WebError(f"no element {ref!r} on this page; run `jobagent web look` again")
        label = (button.inner_text(timeout=2000) or button.get_attribute("value") or "").strip()
        filled = 0
        try:
            fields = target(pg, job).evaluate(forms.EXTRACT_JS)["fields"]
            filled = sum(1 for f in fields if f.get("filled") and f["kind"] not in ("file",))
        except Exception:  # noqa: BLE001
            pass
        # "Apply" starts an application on a job page (empty search boxes at most), but submits
        # one at the end of a form that has been filled in.
        submitting = SIDE_EFFECT_RE.search(label) or (re.search(r"\bapply\b", label, re.I) and filled >= 2)
        if dry_run() and submitting:
            raise WebError(
                f"dry run: not pressing {label!r} (it would submit or create something). "
                "This is as far as a rehearsal goes - record it with `jobagent web finish dry_run`."
            )
        before = len(pg.context.pages)
        s = _session()
        s["last_action"] = time.time()
        _save_session(s)
        _robust_click(button)
        _settle(pg)
    with page() as pg:  # a new tab may have opened
        result = look(pg, job, profile, memory=state.memory)
        if not result["fields"] and len(result["buttons"]) < 3:  # single-page apps still loading
            pg.wait_for_timeout(3000)
            result = look(pg, job, profile, memory=state.memory)
        result["opened_new_tab"] = len(pg.context.pages) > before
        return result


def _robust_click(button: Any) -> None:
    """Click, coping with overlays that swallow pointer events (Workday's click_filter layer)."""
    attempts = (
        lambda: button.click(timeout=6000),
        lambda: button.click(timeout=4000, force=True),
        lambda: button.evaluate("e => e.click()"),
    )
    last: Exception | None = None
    for attempt in attempts:
        try:
            attempt()
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise WebError(f"could not click: {str(last).splitlines()[0][:200] if last else 'unknown'}")


def goto(state: State, profile: Profile, url: str) -> dict[str, Any]:
    job = _current_job(state)
    if not allowed(url, job):
        raise WebError(f"{urlparse(url).hostname} is not the employer's site or a known applicant system")
    with page() as pg:
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        _settle(pg)
        return look(pg, job, profile, memory=state.memory)


def fill(state: State, profile: Profile, answers: dict[str, Any] | None, overwrite: bool = False) -> dict[str, Any]:
    """Fill the current page: profile suggestions for empty fields, then Claude's answers."""
    job = _current_job(state)
    answers = dict(answers or {})
    with page() as top:
        pg = target(top, job)
        if not allowed(pg.url, job):
            raise WebError(f"refusing to type personal data into {urlparse(pg.url).hostname}")
        data = pg.evaluate(forms.EXTRACT_JS)
        files = _files(job, profile)
        by_key = {f["key"]: f for f in data["fields"]}
        unknown = [k for k in answers if k not in by_key]
        values: dict[str, Any] = {}
        for f in data["fields"]:
            needed = f["key"] in answers or overwrite or not f.get("filled")
            if needed and f["kind"] in ("combobox", "listbox") and not f.get("options"):
                f["options"] = forms.combobox_options(pg, f["key"], limit=200)
            if f["key"] in answers:
                values[f["key"]] = answers[f["key"]]
            elif overwrite or not f.get("filled"):
                s = forms.suggest(f, profile, job.company, files, state.memory)
                if s is not None:
                    values[f["key"]] = s.value
        for k, v in list(values.items()):
            f = by_key[k]
            if f["kind"] == "password" or v == forms.SITE_PASSWORD:
                # Rehearsals must not create accounts: never generate or type a real password.
                values[k] = ("DryRun-Not-A-Real-Password-1!" if dry_run()
                             else vault.get_or_create(state, top.url, profile.personal.email).password)
            elif f["kind"] == "file":
                token = str(v)
                path = files.get(token) or next((p for p in files.values() if p.name == Path(token).name), None)
                if path is None:
                    values.pop(k)
                    unknown.append(f"{k}: files can only be 'resume' or 'cover_letter' (build them first)")
                else:
                    values[k] = str(path)
        report = forms.fill_and_verify(pg, [f for f in data["fields"] if f["key"] in values], values)
        if answers:
            from .memory import remember

            ok = {k: v for k, v in answers.items() if k in report.filled and k not in report.errors}
            remember(state, data["fields"], ok, job.company)
        s = _session()
        s["last_action"] = time.time()
        _save_session(s)
        after = look(top, job, profile, gather_options=False, memory=state.memory)
    return {
        "filled": report.filled,
        "errors": report.errors,
        "value_did_not_stick": report.unverified,
        "problems": unknown,
        "required_empty": after["required_empty"],
        "alerts": after["alerts"],
        "buttons": after["buttons"],
    }


def email_code(state: State, profile: Profile, hint: str = "") -> dict[str, Any]:
    job = _current_job(state)
    since = _session().get("last_action", time.time()) - 90
    code = inbox.wait_for_code(since, hint=hint)
    if not code:
        raise WebError("no verification code arrived (is JOBAGENT_IMAP_* configured?)")
    with page() as pg:
        if not forms.enter_code(target(pg, job), code) and not forms.enter_code(pg, code):
            raise WebError("found a code but no field to type it into")
        _settle(pg)
        return look(pg, job, profile, memory=state.memory)


URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def email_link(state: State, profile: Profile, match: str) -> dict[str, Any]:
    """Open the newest verification/activation link that arrived since the last action."""
    job = _current_job(state)
    since = _session().get("last_action", time.time()) - 300
    deadline = time.time() + 150
    while time.time() < deadline:
        for msg in reversed(inbox.fetch_since(since, limit=25)):
            for url in URL_RE.findall(msg.text):
                url = url.rstrip(".,;")
                if match.lower() in url.lower() and allowed(url, job):
                    return goto(state, profile, url)
        time.sleep(10)
    raise WebError(f"no email link containing {match!r} arrived")


def screenshot(state: State, profile: Profile, name: str = "page") -> str:
    from .apply import work_dir

    job = _current_job(state)
    path = work_dir(job) / f"{name}.jpg"
    with page() as pg:
        pg.screenshot(path=str(path), full_page=True, type="jpeg", quality=45)
    return str(path)


def finish(state: State, profile: Profile, status: str, reason: str) -> dict[str, Any]:
    job = _current_job(state)
    try:
        screenshot(state, profile, "final")
    except Exception:  # noqa: BLE001
        pass
    if status == "applied":
        if dry_run():
            raise WebError("dry run: use `jobagent web finish dry_run`")
        job.applied_at = time.time()
        job.result = "submitted"
        job.application = {"mode": "browser", "at": time.time(), "note": reason[:300]}
        state.set_status(job, "applied", reason or job.apply_url)
    elif status == "dry_run":
        job.application = {"mode": "browser", "at": time.time(), "dry_run": True, "note": reason[:300]}
        state.jobs[job.id] = job
        state.log(job.id, "dry_run", "dry_run_ok")
    elif status in ("manual", "failed"):
        job.result = status
        state.set_status(job, "manual", reason)
    else:
        raise WebError("status must be applied, manual, failed or dry_run")
    _save_session({})
    return {"id": job.id, "status": job.status}
