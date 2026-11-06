"""Persistent state: every job the agent has seen and what it decided.

Locally the state is a JSON file in ``~/.jobagent``. In GitHub Actions it is
round-tripped through an encrypted file (Fernet, key in a repository secret)
on a dedicated branch, so a public repository never exposes where you applied.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from .profile import jobagent_home

# Job lifecycle:
#   new -> candidate -> scored -> (skipped | applying -> applied | manual | closed | duplicate)
#   manual -> applied_manual   (you applied yourself and ticked it in the report)
#   closed: the posting no longer takes applications; duplicate: the site says you already applied
DECIDED = {"skipped", "applied", "manual", "failed", "applied_manual", "closed", "duplicate"}
SEEN_TTL_DAYS = 60
EVENT_TTL_DAYS = 120


class Job(BaseModel):
    id: str
    source: str
    company: str
    title: str
    url: str
    apply_url: str
    ats: str
    ats_company: str | None = None
    ats_job_id: str | None = None
    ats_region: str = "us"
    locations: list[str] = Field(default_factory=list)
    posted_at: float | None = None
    category: str = ""
    sponsorship: str = ""
    terms: list[str] = Field(default_factory=list)
    degrees: list[str] = Field(default_factory=list)
    job_type: str = ""

    # Tracking
    status: str = "new"
    first_seen: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    score: int | None = None
    reason: str = ""
    applied_at: float | None = None
    attempts: int = 0
    retries: int = 0  # apply sessions that died without an outcome (timeout, crash) and were retried
    result: str = ""
    manual_no: int | None = None  # short number on the apply-by-hand list ("done 12" in an email reply)
    resume_plan: dict[str, Any] | None = None
    application: dict[str, Any] | None = None  # answers given and the outcome; private

    @property
    def auto_apply(self) -> bool:
        from .ats import AtsRef

        return AtsRef(self.ats, self.ats_company, self.ats_job_id, self.ats_region).auto_apply


class Event(BaseModel):
    ts: float
    job_id: str
    type: str
    detail: str = ""


class Account(BaseModel):
    """A login the agent created on a job site. Lives only in encrypted state."""

    site: str
    username: str
    password: str
    created_at: float = Field(default_factory=time.time)
    notes: str = ""


class State(BaseModel):
    version: int = 1
    jobs: dict[str, Job] = Field(default_factory=dict)  # jobs that reached "candidate" or beyond
    seen: dict[str, float] = Field(default_factory=dict)  # job id -> first seen (for "new today")
    events: list[Event] = Field(default_factory=list)
    accounts: dict[str, Account] = Field(default_factory=dict)
    # Answers Claude gave to questions the profile does not cover, reused next time
    # the same question appears. Your custom_answers always take priority.
    memory: dict[str, dict[str, Any]] = Field(default_factory=dict)
    last_run: dict[str, Any] = Field(default_factory=dict)
    manual_seq: int = 0  # last number given to a job on the apply-by-hand list

    # -- helpers -------------------------------------------------------------

    def log(self, job_id: str, type_: str, detail: str = "") -> None:
        self.events.append(Event(ts=time.time(), job_id=job_id, type=type_, detail=detail[:500]))

    def set_status(self, job: Job, status: str, detail: str = "") -> None:
        if status == "manual" and job.manual_no is None:
            self.manual_seq += 1
            job.manual_no = self.manual_seq
        job.status = status
        job.updated_at = time.time()
        self.jobs[job.id] = job
        self.log(job.id, status, detail)

    def events_between(self, start: float, end: float) -> list[Event]:
        return [e for e in self.events if start <= e.ts < end]

    def applied_between(self, start: float, end: float) -> list[Job]:
        return [j for j in self.jobs.values() if j.applied_at and start <= j.applied_at < end]

    def prune(self, now: float | None = None) -> None:
        now = now or time.time()
        seen_cut = now - SEEN_TTL_DAYS * 86400
        self.seen = {k: v for k, v in self.seen.items() if v >= seen_cut or k in self.jobs}
        ev_cut = now - EVENT_TTL_DAYS * 86400
        self.events = [e for e in self.events if e.ts >= ev_cut]
        # Forget undecided candidates that went stale; keep every decision.
        for jid, job in list(self.jobs.items()):
            if job.status not in DECIDED and job.updated_at < seen_cut:
                del self.jobs[jid]


def state_path() -> Path:
    return Path(os.environ.get("JOBAGENT_STATE", jobagent_home() / "state.json"))


def load_state(path: Path | None = None) -> State:
    path = path or state_path()
    if not path.exists():
        return State()
    return State.model_validate_json(path.read_text(encoding="utf-8"))


def save_state(state: State, path: Path | None = None) -> Path:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=1), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def merge_jobs(state: State, jobs: Iterable[Job]) -> list[Job]:
    """Record sightings; returns the jobs seen for the first time."""
    new: list[Job] = []
    now = time.time()
    for job in jobs:
        if job.id not in state.seen:
            state.seen[job.id] = now
            new.append(job)
    return new


# ---------------------------------------------------------------------------
# Encryption for storing state outside this machine
# ---------------------------------------------------------------------------


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _fernet(key: str | None = None):
    from cryptography.fernet import Fernet

    key = key or os.environ.get("JOBAGENT_STATE_KEY", "")
    if not key:
        raise RuntimeError("JOBAGENT_STATE_KEY is not set (create one with: python -m job_agent keygen)")
    return Fernet(key.encode())


def encrypt_file(src: Path, dst: Path, key: str | None = None) -> None:
    dst.write_bytes(_fernet(key).encrypt(src.read_bytes()))


def decrypt_file(src: Path, dst: Path, key: str | None = None) -> None:
    data = _fernet(key).decrypt(src.read_bytes())
    json.loads(data)  # refuse to write something that is not state JSON
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
