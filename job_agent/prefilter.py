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

# Postings that are not ordinary applications.
EVENT_ONLY = re.compile(
    r"grace hopper|\bghc\b|afrotech|society of women engineers|\bswe (conference|career fair)|\bnsbe\b|\bshpe\b|"
    r"\bsase (conference|national)|\btapia\b|career fair|hackathon",
    re.I,
)
TALENT_POOL = re.compile(
    r"talent (community|pool|network)|general (application|interest)|future (opportunities|consideration|openings)|"
    r"expression of interest|join our network",
    re.I,
)
# Contract marketplaces (AI-training gigs, freelancing): signing up is not a job application.
GIG_SITES = ("mercor.com", "toptal.com", "upwork.com", "fiverr.com", "turing.com", "outlier.ai", "dataannotation.tech",
             "micro1.ai", "remotasks.com", "alignerr.com", "braintrust.dev", "usebraintrust.com")

SEASONS = r"spring|summer|fall|autumn|winter"
TERM_IN_TITLE = re.compile(
    rf"((?:{SEASONS})(?:\s*(?:/|&|\+|,|-|and|or)\s*(?:{SEASONS}))*)\s*'?(?:20)?(\d{{2}})\b|\b20(\d{{2}})\s+((?:{SEASONS}))",
    re.I,
)
YEAR_IN_TITLE = re.compile(r"\b20(2\d|3\d)\b")
GRAD_CONTEXT = re.compile(r"grad|class of|degree|expected", re.I)
UNDERGRAD_ONLY = re.compile(r"\bundergrad(uate)?s?\b", re.I)
GRAD_ONLY = re.compile(r"\bmaster'?s\b|\bmba\b|graduate student|\bgrad student", re.I)
PHD = re.compile(r"\bph\.?\s?d\b|doctoral", re.I)
ANY_OTHER_DEGREE = re.compile(r"\bm\.?s\.?\b|\bmaster|\bmba\b|\bbachelor|\bb\.?s\.?\b|undergrad|\bgraduate student", re.I)
# "Software Engineering Intern - University of Michigan students": a role reserved for one school.
ONE_SCHOOL = re.compile(r"\b(?:University of [A-Z][\w&.-]*(?: [A-Z][\w&.-]*)?|[A-Z][\w&.-]* State University)\b")


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


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def company_key(company: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\b(inc|corp|corporation|llc|ltd|co|plc|lp)\b", "", _norm(company))).strip()


def dedupe_key(job: Job) -> tuple[str, str]:
    return company_key(job.company), _norm(job.title)


def _terms_in_title(title: str) -> list[tuple[str, str]]:
    """(season, two-digit year) pairs a title names as its term, e.g. "Intern - Summer/Fall 2027"."""
    out = []
    for m in TERM_IN_TITLE.finditer(title):
        if GRAD_CONTEXT.search(title[max(0, m.start() - 25): m.start()]):
            continue  # "Graduating Spring 2027" is not the internship's term
        seasons, year = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        for season in re.findall(SEASONS, seasons, re.I):
            out.append(("fall" if season.lower() == "autumn" else season.lower(), year))
    return out


def _wanted_terms(terms: list[str]) -> list[tuple[str, str]] | None:
    """Parsed preferences.terms, or None when some entry is not "<Season> <year>" (then nothing is filtered)."""
    out = []
    for t in terms:
        m = re.fullmatch(rf"\s*({SEASONS})\s+(?:20)?(\d{{2}})\s*", t, re.I)
        if not m:
            return None
        out.append(("fall" if m.group(1).lower() == "autumn" else m.group(1).lower(), m.group(2)))
    return out


def term_conflict(title: str, wanted: list[str]) -> str | None:
    want = _wanted_terms(wanted)
    if not want:
        return None
    named = _terms_in_title(title)
    if named:
        return None if any(n in want for n in named) else f"term in title ({title})"
    years = {m.group(1) for m in YEAR_IN_TITLE.finditer(title)
             if not GRAD_CONTEXT.search(title[max(0, m.start() - 25): m.start()])}
    if years and not years & {y for _, y in want}:
        return f"year in title ({title})"
    return None


def degree_level(profile: Profile) -> str:
    """The degree the candidate is studying for (or holds most recently): phd, master or bachelor."""
    def year(e) -> int:
        found = re.findall(r"(?:19|20)\d\d", e.end or "") or re.findall(r"(?:19|20)\d\d", e.start or "")
        return int(found[-1]) if found else 0

    if not profile.education:
        return "bachelor"
    latest = max(profile.education, key=year)
    d = latest.degree.lower()
    if re.search(r"ph\.?\s?d|doctor", d):
        return "phd"
    if re.search(r"\bm\.?\s?(s|a|eng|sc|phil|ba|fe|cs)\b\.?|master|mba", d):
        return "master"
    return "bachelor"


def gig_site(job: Job) -> bool:
    from urllib.parse import urlparse

    hosts = {(urlparse(u).hostname or "").lower() for u in (job.url, job.apply_url) if u}
    return any(h == d or h.endswith("." + d) for h in hosts for d in GIG_SITES)


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
    level = degree_level(profile)
    if PHD.search(title) and level != "phd" and not ANY_OTHER_DEGREE.search(title):
        return "PhD only"
    if UNDERGRAD_ONLY.search(title) and level != "bachelor" and not (GRAD_ONLY.search(title) or PHD.search(title)):
        return "undergraduates only"
    if GRAD_ONLY.search(title) and level == "bachelor" and not UNDERGRAD_ONLY.search(title) \
            and not re.search(r"bachelor", title, re.I):
        return "graduate students only"
    if p.terms and job.terms and not any(t.lower() in " ".join(job.terms).lower() for t in p.terms):
        return f"term {job.terms}"
    if p.terms and not (job.terms and any(t.lower() in " ".join(job.terms).lower() for t in p.terms)):
        conflict = term_conflict(title, p.terms)
        if conflict:
            return conflict
    if EVENT_ONLY.search(title):
        return "event/conference posting"
    if TALENT_POOL.search(title):
        return "talent pool, not a specific job"
    if gig_site(job):
        return "contract marketplace, not a job application"
    school = ONE_SCHOOL.search(title)
    if school and _norm(school.group(0)) not in _norm(job.company) and not any(
            _norm(school.group(0)) in _norm(e.school) or _norm(e.school) in _norm(school.group(0)) for e in profile.education):
        return f"for one school's students ({school.group(0)})"
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
