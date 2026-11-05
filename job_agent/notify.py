"""Delivering the daily report: a GitHub issue (with an @mention) and/or an email."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import httpx


def github_issue(title: str, body: str) -> str | None:
    """Open an issue in the current repository. Returns its URL (None if not running in GitHub Actions)."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    r = httpx.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": title, "body": body},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("html_url")


def email_report(subject: str, body_markdown: str, to: str) -> bool:
    """Send the report through SMTP (Gmail app password works). Returns False if SMTP is not configured."""
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
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    return True
