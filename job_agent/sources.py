"""Where jobs come from.

* SimplifyJobs keeps community-maintained lists of US internships and new-grad
  roles on GitHub, with links straight to each company's application page.
* Any company that hosts its board on Greenhouse, Lever or Ashby can be added
  to ``preferences.sources.companies`` and is read through the public API.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

from . import ats
from .profile import CompanyBoard, Preferences
from .store import Job

SIMPLIFY_RAW = "https://raw.githubusercontent.com/SimplifyJobs/{repo}/dev/.github/scripts/listings.json"
NEW_GRAD_REPO = "New-Grad-Positions"


def internship_repo(today: dt.date | None = None) -> list[str]:
    """Candidate repo names, most likely first (recruiting for summer N starts in summer N-1)."""
    today = today or dt.date.today()
    year = today.year + 1 if today.month >= 7 else today.year
    return [f"Summer{year}-Internships", f"Summer{year - 1 if today.month >= 7 else year + 1}-Internships"]


def job_id_for(ref: ats.AtsRef, url: str, fallback: str | None = None) -> str:
    if ref.ats in ("greenhouse", "lever", "ashby") and ref.job_id:
        return f"{ref.ats}:{ref.company or '-'}:{ref.job_id}"
    if fallback:
        return fallback
    return "url:" + hashlib.sha1(url.encode()).hexdigest()[:16]


def job_type_from_title(title: str) -> str:
    t = title.lower()
    if re.search(r"\bintern(ship)?\b|\bco-?op\b", t):
        return "internship"
    if re.search(r"new ?grad|university grad|recent grad|early career|entry[- ]level|graduate (program|engineer)", t):
        return "new_grad"
    return "full_time"


def _from_url(url: str, **fields: Any) -> Job:
    ref = ats.parse_ats(url)
    return Job(
        id=job_id_for(ref, url, fields.pop("fallback_id", None)),
        url=url,
        apply_url=ats.application_url(ref, url),
        ats=ref.ats,
        ats_company=ref.company,
        ats_job_id=ref.job_id,
        ats_region=ref.region,
        **fields,
    )


def _fetch_json(client: httpx.Client, url: str) -> Any | None:
    r = client.get(url)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def simplify_jobs(kind: str, client: httpx.Client) -> list[Job]:
    """kind: 'internship' or 'new_grad'."""
    repos = internship_repo() if kind == "internship" else [NEW_GRAD_REPO]
    data = None
    for repo in repos:
        data = _fetch_json(client, SIMPLIFY_RAW.format(repo=repo))
        if data is not None:
            break
    if not data:
        return []
    jobs = []
    for item in data:
        if not item.get("active") or not item.get("is_visible", True):
            continue
        url = item.get("url") or ""
        if not url.startswith("http"):
            continue
        jobs.append(
            _from_url(
                url,
                fallback_id=f"simplify:{item.get('id')}",
                source=f"simplify_{kind}",
                company=item.get("company_name", ""),
                title=item.get("title", ""),
                locations=item.get("locations") or [],
                posted_at=float(item.get("date_posted") or 0) or None,
                category=item.get("category") or "",
                sponsorship=item.get("sponsorship") or "",
                terms=[t for t in (item.get("terms") or []) if t and t != "N/A"],
                degrees=item.get("degrees") or [],
                job_type=kind,
            )
        )
    return jobs


def _ms(ts: Any) -> float | None:
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return None
    return v / 1000 if v > 1e12 else v


def _iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def company_jobs(board: CompanyBoard, client: httpx.Client) -> list[Job]:
    jobs: list[Job] = []
    if board.ats == "greenhouse":
        for item in ats.greenhouse_board(board.token, board.region, client):
            url = item.get("absolute_url") or f"https://job-boards.greenhouse.io/{board.token}/jobs/{item['id']}"
            ref = ats.AtsRef("greenhouse", board.token, str(item["id"]), board.region)
            title = item.get("title", "")
            jobs.append(
                Job(
                    id=job_id_for(ref, url),
                    source="greenhouse",
                    company=board.name,
                    title=title,
                    url=url,
                    apply_url=ats.application_url(ref, url),
                    ats="greenhouse",
                    ats_company=board.token,
                    ats_job_id=str(item["id"]),
                    ats_region=board.region,
                    locations=[(item.get("location") or {}).get("name", "")],
                    posted_at=_iso(item.get("first_published") or item.get("updated_at")),
                    job_type=job_type_from_title(title),
                )
            )
    elif board.ats == "lever":
        for item in ats.lever_board(board.token, board.region, client):
            ref = ats.AtsRef("lever", board.token, item["id"], board.region)
            url = item.get("hostedUrl") or ""
            title = item.get("text", "")
            cats = item.get("categories") or {}
            jobs.append(
                Job(
                    id=job_id_for(ref, url),
                    source="lever",
                    company=board.name,
                    title=title,
                    url=url,
                    apply_url=ats.application_url(ref, url),
                    ats="lever",
                    ats_company=board.token,
                    ats_job_id=item["id"],
                    ats_region=board.region,
                    locations=cats.get("allLocations") or [cats.get("location", "")],
                    posted_at=_ms(item.get("createdAt")),
                    job_type=job_type_from_title(title),
                )
            )
    elif board.ats == "ashby":
        for item in ats.ashby_board(board.token, client):
            if item.get("isListed") is False:
                continue
            ref = ats.AtsRef("ashby", board.token, item["id"])
            url = item.get("jobUrl") or ""
            title = item.get("title", "")
            locs = [item.get("location", "")] + [
                s.get("location", "") for s in item.get("secondaryLocations") or [] if isinstance(s, dict)
            ]
            if item.get("isRemote"):
                locs.append("Remote")
            jobs.append(
                Job(
                    id=job_id_for(ref, url),
                    source="ashby",
                    company=board.name,
                    title=title,
                    url=url,
                    apply_url=ats.application_url(ref, url),
                    ats="ashby",
                    ats_company=board.token,
                    ats_job_id=item["id"],
                    locations=[l for l in locs if l],
                    posted_at=_iso(item.get("publishedAt")),
                    job_type=job_type_from_title(title),
                )
            )
    return jobs


def discover(prefs: Preferences, client: httpx.Client | None = None) -> tuple[list[Job], list[str]]:
    """All currently open jobs from every enabled source, de-duplicated. Returns (jobs, errors)."""
    client = client or httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": ats.USER_AGENT})
    errors: list[str] = []
    batches: list[Iterable[Job]] = []
    src = prefs.sources
    if src.simplify_internships and "internship" in prefs.job_types:
        try:
            batches.append(simplify_jobs("internship", client))
        except httpx.HTTPError as exc:
            errors.append(f"simplify internships: {exc}")
    if src.simplify_new_grad and ("new_grad" in prefs.job_types or "full_time" in prefs.job_types):
        try:
            batches.append(simplify_jobs("new_grad", client))
        except httpx.HTTPError as exc:
            errors.append(f"simplify new grad: {exc}")
    for board in src.companies:
        try:
            batches.append(company_jobs(board, client))
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            errors.append(f"{board.name} ({board.ats}:{board.token}): {exc}")
        time.sleep(0.2)
    extra = os.environ.get("JOBAGENT_EXTRA_JOBS")
    if extra:  # a JSON list of Job objects: for tests, or jobs found elsewhere
        try:
            batches.append([Job.model_validate(j) for j in json.loads(Path(extra).read_text(encoding="utf-8"))])
        except (OSError, ValueError) as exc:
            errors.append(f"JOBAGENT_EXTRA_JOBS: {exc}")
    for url in src.extra_urls:
        batches.append([_from_url(url, source="manual", company=ats.parse_ats(url).company or "", title=url,
                                  job_type=prefs.job_types[0] if prefs.job_types else "")])
    seen: dict[str, Job] = {}
    for batch in batches:
        for job in batch:
            seen.setdefault(job.id, job)
    return list(seen.values()), errors
