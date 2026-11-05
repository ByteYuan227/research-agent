"""Reading your mailbox over IMAP: verification codes and replies from employers.

Configure with environment variables (GitHub secrets in CI):

    JOBAGENT_IMAP_USER       you@gmail.com
    JOBAGENT_IMAP_PASSWORD   an app password (Gmail: Google Account > Security > App passwords)
    JOBAGENT_IMAP_HOST       optional, default imap.gmail.com

The agent only reads; it never sends, deletes or marks mail.
"""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import imaplib
import os
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime


@dataclass
class Message:
    uid: str
    sender: str
    subject: str
    date: float
    text: str


def configured() -> bool:
    return bool(os.environ.get("JOBAGENT_IMAP_USER") and os.environ.get("JOBAGENT_IMAP_PASSWORD"))


def _connect() -> imaplib.IMAP4_SSL:
    host = os.environ.get("JOBAGENT_IMAP_HOST", "imap.gmail.com")
    conn = imaplib.IMAP4_SSL(host, 993, timeout=30)
    conn.login(os.environ["JOBAGENT_IMAP_USER"], os.environ["JOBAGENT_IMAP_PASSWORD"])
    conn.select("INBOX", readonly=True)
    return conn


def _body_text(msg: email.message.EmailMessage) -> str:
    from .ats import html_to_text

    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        return ""
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def fetch_since(since_ts: float, limit: int = 60) -> list[Message]:
    """Messages received after ``since_ts`` (newest last). Read-only."""
    since = dt.datetime.fromtimestamp(since_ts - 86400, dt.timezone.utc).strftime("%d-%b-%Y")
    conn = _connect()
    try:
        typ, data = conn.uid("search", None, f"(SINCE {since})")
        uids = (data[0] or b"").split()[-limit:]
        out: list[Message] = []
        for uid in uids:
            typ, parts = conn.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1], policy=email.policy.default)
            try:
                when = parsedate_to_datetime(msg["Date"]).timestamp()
            except (TypeError, ValueError):
                when = 0.0
            if when and when < since_ts:
                continue
            out.append(
                Message(
                    uid=uid.decode(),
                    sender=str(msg.get("From", "")),
                    subject=str(msg.get("Subject", "")),
                    date=when,
                    text=_body_text(msg)[:6000],
                )
            )
        out.sort(key=lambda m: m.date)
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


_TOKEN = re.compile(r"\b(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,10}\b")


def extract_code(text: str) -> str | None:
    """Find a one-time code in an email body."""
    for m in re.finditer(r"code|passcode|one[- ]time|verify|verification", text, re.I):
        window = text[m.end() : m.end() + 300]
        tok = _TOKEN.search(window)
        if tok:
            return tok.group(0)
    for line in text.splitlines():
        line = line.strip()
        if re.fullmatch(r"[A-Za-z0-9]{6,10}", line) and re.search(r"\d", line):
            return line
    return None


CODE_SUBJECT = re.compile(r"code|verif|security|confirm|one[- ]time", re.I)


def wait_for_code(since_ts: float, timeout_s: int = 150, hint: str = "") -> str | None:
    """Poll the inbox for a verification code that arrived after ``since_ts``."""
    if os.environ.get("JOBAGENT_TEST_CODE"):  # end-to-end tests against mock sites
        return os.environ["JOBAGENT_TEST_CODE"]
    if not configured():
        return None
    deadline = time.time() + timeout_s
    hint_re = re.compile(re.escape(hint), re.I) if hint else None
    while time.time() < deadline:
        try:
            msgs = fetch_since(since_ts - 30, limit=25)
        except (imaplib.IMAP4.error, OSError):
            msgs = []
        for msg in reversed(msgs):
            blob = f"{msg.subject}\n{msg.text}"
            if not CODE_SUBJECT.search(msg.subject) and not CODE_SUBJECT.search(msg.text[:500]):
                continue
            if hint_re and not (hint_re.search(msg.sender) or hint_re.search(blob)):
                continue
            code = extract_code(blob)
            if code:
                return code
        time.sleep(10)
    return None


def recent(hours: float = 26) -> list[dict]:
    """Recent mail for Claude to classify (interview invites, rejections, assessments)."""
    if not configured():
        return []
    msgs = fetch_since(time.time() - hours * 3600, limit=80)
    return [
        {
            "from": m.sender[:120],
            "subject": m.subject[:200],
            "date": dt.datetime.fromtimestamp(m.date).isoformat(timespec="minutes") if m.date else "",
            "snippet": re.sub(r"\s+", " ", m.text)[:500],
        }
        for m in msgs
    ]
