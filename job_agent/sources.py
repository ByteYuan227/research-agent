"""Where jobs come from.

* SimplifyJobs keeps community-maintained lists of US internships and new-grad
  roles on GitHub, with links straight to each company's application page.
* Any company that hosts its board on Greenhouse, Lever or Ashby can be added
  to ``preferences.sources.companies`` and is read through the public API.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
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
    if re.search(r"new ?grad|university grad|recent grad|early career|entry[- ]level|graduate (program|engineer)|"
                 r"\b(engineer|developer|swe|sde)\s*(i|1)\b|\bassociate (software|engineer|developer)|\bjunior\b|\bjr\.? ",
                 t):
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


SPEEDYAPPLY_NEW_GRAD = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md"
_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'href="([^"]+)"')


def _cell(text: str) -> str:
    return html.unescape(_TAG.sub("", text)).replace("**", "").strip()


def speedyapply_jobs(client: httpx.Client, url: str = SPEEDYAPPLY_NEW_GRAD, now: float | None = None) -> list[Job]:
    """The SpeedyApply new grad list: Markdown tables of Company | Position | Location | [Salary] | Posting | Age."""
    now = now or time.time()
    r = client.get(url)
    r.raise_for_status()
    jobs: list[Job] = []
    header: list[str] | None = None
    for line in r.text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if line.startswith("#"):
                header = None
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] == "Company":
            header = cells
            continue
        if not header or len(cells) != len(header) or set(line) <= set("|-: "):
            continue
        row = dict(zip(header, cells))
        href = _HREF.search(row.get("Posting", ""))
        if not href:
            continue
        age = re.match(r"(\d+)\s*d", _cell(row.get("Age", "")))
        location = _cell(row.get("Location", ""))
        jobs.append(_from_url(
            html.unescape(href.group(1)), source="speedyapply_new_grad", company=_cell(row.get("Company", "")),
            title=_cell(row.get("Position", "")), locations=[location] if location else [],
            posted_at=now - int(age.group(1)) * 86400 if age else None, category="Software", job_type="new_grad",
        ))
    return jobs


# Career boards of companies that hire new grad software engineers (checked to exist). Only
# entry-level titles survive the job-type filter, so the rest of each board costs nothing.
POPULAR_BOARDS = (
    [("greenhouse", t) for t in (
        "stripe databricks airbnb pinterest robinhood discord figma coinbase cloudflare datadog mongodb roblox lyft "
        "instacart reddit twitch dropbox asana gitlab duolingo affirm brex samsara verkada scaleai anthropic okta "
        "twilio squarespace janestreet imc point72 chime gusto nuro waymo applovin nextdoor toast elastic pagerduty "
        "newrelic zscaler ziprecruiter rubrik akunacapital sofi braze virtu towerresearchcapital singlestore "
        "purestorage tanium riotgames gemini flexport faire appian").split()]
    + [("lever", t) for t in "palantir spotify zoox".split()]
    + [("ashby", t) for t in ("openai ramp notion linear replit perplexity cursor elevenlabs supabase cohere "
                              "character harvey sierra decagon").split()]
)


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
    if src.speedyapply_new_grad and "new_grad" in prefs.job_types:
        try:
            batches.append(speedyapply_jobs(client))
        except httpx.HTTPError as exc:
            errors.append(f"speedyapply new grad: {exc}")
    boards = list(src.companies)
    if src.popular_boards:
        own = {(b.ats, b.token) for b in boards}
        boards += [CompanyBoard(name=tok.title(), ats=kind, token=tok) for kind, tok in POPULAR_BOARDS if (kind, tok) not in own]
    for board in boards:
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
