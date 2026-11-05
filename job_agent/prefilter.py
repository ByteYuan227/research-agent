"""Cheap, deterministic filtering and ranking before Claude reads anything.

Thousands of postings are open at any time; Claude only reads the few dozen
that survive these rules, ranked by a keyword heuristic.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .profile import Profile
from .store import DECIDED, Job, State

NON_US = re.compile(
    r"\b(canada|toronto|vancouver|montr[eé]al|ontario|quebec|waterloo|ottawa|calgary|"
    r"united kingdom|\buk\b|london|england|ireland|dublin|germany|berlin|munich|france|paris|"
    r"netherlands|amsterdam|switzerland|zurich|spain|poland|india|bangalore|bengaluru|hyderabad|"
    r"singapore|japan|tokyo|china|beijing|shanghai|shenzhen|hong kong|taiwan|korea|seoul|"
    r"australia|sydney|melbourne|israel|tel aviv|mexico|brazil)\b",
    re.I,
)
NO_SPONSORSHIP = {"Does Not Offer Sponsorship", "U.S. Citizenship is Required"}
CLEARANCE = re.compile(r"\b(ts/sci|top secret|security clearance|clearance required|secret clearance)\b", re.I)


@dataclass
class Candidate:
    job: Job
    rank: float
    why: list[str]


def _is_us(locations: list[str]) -> bool:
    locs = [l for l in locations if l]
    if not locs:
        return True
    return any(not NON_US.search(l) or re.search(r"\b(US|USA|United States|Remote)\b", l) for l in locs)


CATEGORY_ALIASES = {
    "software engineering": "Software",
    "data science, ai & machine learning": "AI/ML/Data",
    "product management": "Product",
}


def _category(c: str) -> str:
    return CATEGORY_ALIASES.get(c.lower(), c)


def dedupe_key(job: Job) -> tuple[str, str]:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()  # noqa: E731
    company = re.sub(r"\b(inc|corp|corporation|llc|ltd|co)\b", "", norm(job.company)).strip()
    return company, norm(job.title)


def filter_reason(job: Job, profile: Profile, now: float) -> str | None:
    """Why this job is not worth Claude's time, or None if it passes."""
    p = profile.preferences
    title = job.title or ""
    if p.job_types and job.job_type and job.job_type not in p.job_types:
        return f"job type {job.job_type}"
    if p.categories and job.category and _category(job.category) not in {_category(c) for c in p.categories}:
        return f"category {job.category}"
    if job.posted_at and now - job.posted_at > p.posted_within_days * 86400:
        return "too old"
    if any(c.lower() == job.company.lower() for c in p.exclude_companies):
        return "excluded company"
    if p.title_include and not any(re.search(rx, title, re.I) for rx in p.title_include):
        return "title not included"
    for rx in p.title_exclude:
        if re.search(rx, title, re.I):
            return f"title excluded ({rx})"
    degrees = " ".join(e.degree for e in profile.education).lower()
    if re.search(r"\bph\.?d\b", title, re.I) and not re.search(r"ph\.?d|doctor", degrees):
        return "PhD only"
    if p.terms and job.terms and not any(t.lower() in " ".join(job.terms).lower() for t in p.terms):
        return f"term {job.terms}"
    wa = profile.work_authorization
    if (wa.requires_sponsorship_now or wa.requires_sponsorship_future) and job.sponsorship in NO_SPONSORSHIP:
        return job.sponsorship
    if not wa.us_citizen and (job.sponsorship == "U.S. Citizenship is Required" or CLEARANCE.search(title)):
        return "citizenship/clearance required"
    if p.locations:
        locs = " | ".join(job.locations).lower()
        if not any(s.lower() in locs for s in p.locations):
            return "location"
    elif not _is_us(job.locations):
        return "outside the US"
    return None


def rank(job: Job, profile: Profile, now: float) -> tuple[float, list[str]]:
    p = profile.preferences
    title = job.title.lower()
    score = 0.0
    why: list[str] = []
    for kw in p.keywords:
        if kw.lower() in title:
            score += 3
            why.append(kw)
    if job.posted_at:
        age_days = max(0.0, (now - job.posted_at) / 86400)
        score += max(0.0, 3 - age_days / 3)  # fresher postings first
    if job.auto_apply:
        score += 1
    return score, why


def select_candidates(jobs: list[Job], profile: Profile, state: State, now: float | None = None) -> tuple[list[Candidate], dict[str, int]]:
    """Jobs that pass the rules and have no decision yet, best first. Also returns filter stats."""
    now = now or time.time()
    stats: dict[str, int] = {}
    out: list[Candidate] = []
    # The same role is often listed several times (one per location or source).
    decided_keys = {dedupe_key(j) for j in state.jobs.values() if j.status in DECIDED or j.status == "scored"}
    taken: set[tuple[str, str]] = set()
    for job in jobs:
        existing = state.jobs.get(job.id)
        if existing and (existing.status in DECIDED or existing.status == "scored"):
            stats["already decided"] = stats.get("already decided", 0) + 1
            continue
        key = dedupe_key(job)
        if key in decided_keys or key in taken:
            stats["duplicate"] = stats.get("duplicate", 0) + 1
            continue
        reason = filter_reason(job, profile, now)
        if reason:
            key = reason.split(" (")[0]
            stats[key] = stats.get(key, 0) + 1
            continue
        taken.add(key)
        r, why = rank(job, profile, now)
        out.append(Candidate(job, r, why))
    out.sort(key=lambda c: c.rank, reverse=True)
    return out[: profile.preferences.max_candidates_per_run], stats
