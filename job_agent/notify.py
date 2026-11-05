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
    msg.set_content(body_markdown)
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
