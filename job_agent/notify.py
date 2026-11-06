"""Delivering the daily report: a GitHub issue (with an @mention) and/or an email.

Each day's issue replaces the previous one (older report issues are closed), and
the checklist of jobs to apply to by hand is carried forward. Boxes you tick in a
report are picked up by the next run (`jobagent sync-checklist`).
"""

from __future__ import annotations

import mimetypes
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

import httpx

REPORT_PREFIX = "求职日报"
CHECKED_RE = re.compile(r"^\s*[-*]\s*\[[xX]\].*?<!--\s*job:(\S+?)\s*-->", re.M)


def _gh() -> tuple[httpx.Client, str]:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise RuntimeError("not running in GitHub Actions (GITHUB_TOKEN / GITHUB_REPOSITORY missing)")
    client = httpx.Client(
        base_url=f"https://api.github.com/repos/{repo}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    return client, repo


def _open_reports(client: httpx.Client) -> list[dict]:
    r = client.get("/issues", params={"state": "open", "per_page": 50, "creator": "github-actions[bot]"})
    r.raise_for_status()
    return [i for i in r.json() if "pull_request" not in i and i.get("title", "").startswith(REPORT_PREFIX)]


def checked_jobs_in_reports() -> list[str]:
    """Job ids whose checkbox you ticked in an open daily report."""
    client, _ = _gh()
    ids: list[str] = []
    for issue in _open_reports(client):
        ids += CHECKED_RE.findall(issue.get("body") or "")
    return ids


def github_issue(title: str, body: str) -> str | None:
    """Open today's report issue and close the earlier ones. Returns its URL (None outside GitHub Actions)."""
    try:
        client, _ = _gh()
    except RuntimeError:
        return None
    previous = _open_reports(client)
    r = client.post("/issues", json={"title": title, "body": body})
    r.raise_for_status()
    created = r.json()
    for issue in previous:
        if issue["number"] != created["number"]:
            client.patch(f"/issues/{issue['number']}", json={"state": "closed", "state_reason": "completed"})
    return created.get("html_url")


def close_reports() -> int:
    """Close every open report issue (the report now goes by email only). Returns how many."""
    client, _ = _gh()
    n = 0
    for issue in _open_reports(client):
        client.patch(f"/issues/{issue['number']}", json={"state": "closed", "state_reason": "completed"})
        n += 1
    return n


# A reply to the emailed report: "done 3 7" (or "已投 3 7") on a line of its own.
DONE_RE = re.compile(r"^\s*(?:done|applied|已投递?|投了)\s*[:：]?\s*((?:#?\d+[\s,，、]*)+)$", re.I)
QUOTE_START_RE = re.compile(r"^(On .{5,200} wrote:|在.{2,200}写道[:：]?|-{3,} ?Original Message|From: )", re.I)


def done_numbers(text: str) -> set[int]:
    """Numbers marked done in the reply's own text (quoted earlier messages are ignored)."""
    found: set[int] = set()
    for line in text.splitlines():
        if QUOTE_START_RE.match(line.strip()):
            break
        if line.lstrip().startswith(">"):
            continue
        m = DONE_RE.match(line)
        if m:
            found |= {int(n) for n in re.findall(r"\d+", m.group(1))}
    return found


def done_numbers_from_inbox(own_addresses: set[str], since_ts: float) -> set[int]:
    """Numbers you marked done by replying to an emailed report (only replies sent from your own address)."""
    from email.utils import parseaddr

    from . import inbox

    found: set[int] = set()
    own = {a.lower() for a in own_addresses if a}
    for msg in inbox.fetch_since(since_ts, limit=200):
        if parseaddr(msg.sender)[1].lower() not in own or "求职日报" not in msg.subject:
            continue
        found |= done_numbers(msg.text)
    return found


def email_report(subject: str, body_markdown: str, to: str, attachments: list[tuple[Path, str]] | None = None) -> bool:
    """Send the report through SMTP (a Gmail app password works). False if SMTP is not configured."""
    user = os.environ.get("JOBAGENT_SMTP_USER") or os.environ.get("JOBAGENT_IMAP_USER")
    password = os.environ.get("JOBAGENT_SMTP_PASSWORD") or os.environ.get("JOBAGENT_IMAP_PASSWORD")
    if not (user and password and to):
        return False
    host = os.environ.get("JOBAGENT_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("JOBAGENT_SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(re.sub(r"\s*<!--.*?-->", "", body_markdown))  # job ids are for issue checkboxes only
    total = 0
    for path, filename in attachments or []:
        data = path.read_bytes()
        total += len(data)
        if total > 18 * 1024 * 1024:  # stay under Gmail's 25 MB limit
            break
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    return True
