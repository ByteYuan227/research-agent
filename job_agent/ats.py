"""Recognise applicant-tracking systems (ATS) from job URLs and talk to their public APIs.

Greenhouse, Lever and Ashby publish job boards through public, unauthenticated
JSON APIs, and host a standard application form for every posting. Those are
the systems this agent can apply through automatically. Everything else is
recognised (so the report can say *why* a job needs a human) but not automated.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

AUTO_APPLY_ATS = {"greenhouse", "lever", "ashby"}
USER_AGENT = "job-agent/0.1 (+https://github.com/ByteYuan227/research-agent)"


@dataclass(frozen=True)
class AtsRef:
    ats: str  # greenhouse | lever | ashby | workday | smartrecruiters | workable | tiktok | other
    company: str | None  # board token / site / org slug, if known
    job_id: str | None
    region: str = "us"  # greenhouse / lever have EU-hosted boards

    @property
    def auto_apply(self) -> bool:
        if self.ats == "greenhouse":
            return bool(self.job_id)
        return self.ats in AUTO_APPLY_ATS and bool(self.company and self.job_id)


def parse_ats(url: str) -> AtsRef:
    try:
        u = urlparse(url)
    except ValueError:
        return AtsRef("other", None, None)
    host = (u.netloc or "").lower().split(":")[0]
    parts = [p for p in u.path.split("/") if p]
    q = {k: v[0] for k, v in parse_qs(u.query).items() if v}

    if host.endswith("greenhouse.io"):
        region = "eu" if ".eu." in host else "us"
        if parts[:2] == ["embed", "job_app"]:
            return AtsRef("greenhouse", q.get("for"), q.get("token"), region)
        if len(parts) >= 3 and parts[1] == "jobs":
            return AtsRef("greenhouse", parts[0], re.sub(r"\D.*$", "", parts[2]) or None, region)
        if "gh_jid" in q:
            return AtsRef("greenhouse", parts[0] if parts else None, q["gh_jid"], region)
        return AtsRef("greenhouse", parts[0] if parts else None, None, region)
    if "gh_jid" in q:  # Greenhouse board embedded on a company career site
        return AtsRef("greenhouse", None, q["gh_jid"])
    if host in ("jobs.lever.co", "jobs.eu.lever.co"):
        region = "eu" if ".eu." in host else "us"
        return AtsRef("lever", parts[0] if parts else None, parts[1] if len(parts) > 1 else None, region)
    if host == "jobs.ashbyhq.com":
        return AtsRef("ashby", parts[0] if parts else None, parts[1] if len(parts) > 1 else None)
    if host.endswith("myworkdayjobs.com") or host.endswith("myworkdaysite.com"):
        return AtsRef("workday", host.split(".")[0], parts[-1] if parts else None)
    if host == "jobs.smartrecruiters.com":
        return AtsRef("smartrecruiters", parts[0] if parts else None, parts[1] if len(parts) > 1 else None)
    if host == "apply.workable.com":
        return AtsRef("workable", parts[0] if parts else None, parts[2] if len(parts) > 2 else None)
    if host.endswith("lifeattiktok.com") or host.endswith("bytedance.com") or host.endswith("tiktok.com"):
        return AtsRef("tiktok", None, parts[-1] if parts else None)
    return AtsRef("other", host or None, None)


def application_url(ref: AtsRef, original_url: str) -> str:
    """The page that hosts the application form."""
    if ref.ats == "greenhouse" and ref.company and ref.job_id:
        host = "job-boards.eu.greenhouse.io" if ref.region == "eu" else "job-boards.greenhouse.io"
        return f"https://{host}/{ref.company}/jobs/{ref.job_id}"
    if ref.ats == "greenhouse" and ref.job_id:
        return f"https://boards.greenhouse.io/embed/job_app?token={ref.job_id}"
    if ref.ats == "lever" and ref.company and ref.job_id:
        host = "jobs.eu.lever.co" if ref.region == "eu" else "jobs.lever.co"
        return f"https://{host}/{ref.company}/{ref.job_id}/apply"
    if ref.ats == "ashby" and ref.company and ref.job_id:
        return f"https://jobs.ashbyhq.com/{ref.company}/{ref.job_id}/application"
    return original_url


# ---------------------------------------------------------------------------
# Public APIs
# ---------------------------------------------------------------------------


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def html_to_text(raw: str) -> str:
    """Rough HTML -> readable text, good enough for an LLM to read a job description."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    for block in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "ul", "ol", "tr"]):
        block.insert_after("\n")
    text = soup.get_text()
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def greenhouse_job(board: str, job_id: str, region: str = "us", client: httpx.Client | None = None) -> dict[str, Any]:
    host = "boards-api.eu.greenhouse.io" if region == "eu" else "boards-api.greenhouse.io"
    c = client or _client()
    r = c.get(f"https://{host}/v1/boards/{board}/jobs/{job_id}", params={"questions": "true"})
    r.raise_for_status()
    return r.json()


def greenhouse_board(board: str, region: str = "us", client: httpx.Client | None = None) -> list[dict[str, Any]]:
    host = "boards-api.eu.greenhouse.io" if region == "eu" else "boards-api.greenhouse.io"
    c = client or _client()
    r = c.get(f"https://{host}/v1/boards/{board}/jobs", params={"content": "false"})
    r.raise_for_status()
    return r.json().get("jobs", [])


def lever_job(site: str, job_id: str, region: str = "us", client: httpx.Client | None = None) -> dict[str, Any]:
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    c = client or _client()
    r = c.get(f"https://{host}/v0/postings/{site}/{job_id}")
    r.raise_for_status()
    return r.json()


def lever_board(site: str, region: str = "us", client: httpx.Client | None = None) -> list[dict[str, Any]]:
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    c = client or _client()
    r = c.get(f"https://{host}/v0/postings/{site}", params={"mode": "json"})
    r.raise_for_status()
    return r.json()


def ashby_board(org: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    c = client or _client()
    r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", params={"includeCompensation": "true"})
    r.raise_for_status()
    return r.json().get("jobs", [])


def ashby_job(org: str, job_id: str, client: httpx.Client | None = None) -> dict[str, Any] | None:
    for job in ashby_board(org, client):
        if job.get("id") == job_id:
            return job
    return None


def workday_parts(url: str) -> tuple[str, str, str, str] | None:
    """(host, tenant, site, path from /job/...) for a myworkdayjobs.com posting URL."""
    u = urlparse(url)
    host = (u.netloc or "").lower()
    parts = [p for p in u.path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]  # locale prefix, e.g. en-US
    if not host or not parts or "job" not in parts:
        return None
    site = parts[0]
    rest = "/".join(parts[parts.index("job"):])
    return host, host.split(".")[0], site, rest


def workday_job(url: str, client: httpx.Client | None = None) -> dict[str, Any] | None:
    """Posting details from Workday's public candidate API (the page itself is rendered by JavaScript)."""
    parts = workday_parts(url)
    if parts is None:
        return None
    host, tenant, site, rest = parts
    c = client or _client()
    r = c.get(f"https://{host}/wday/cxs/{tenant}/{site}/{rest}", headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json().get("jobPostingInfo")


def jsonld_description(raw_html: str) -> str:
    """The description from a schema.org JobPosting block, which most career sites embed for search engines."""
    import json

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("JobPosting", ["JobPosting"]) and item.get("description"):
                title = item.get("title", "")
                return f"{title}\n{html_to_text(html.unescape(item['description']))}".strip()
    return ""


def render_page_text(url: str) -> str:
    """Last resort for career sites that build the page with JavaScript: render it and read the text."""
    from playwright.sync_api import sync_playwright

    from .browser.launch import launch, new_context

    with sync_playwright() as pw:
        browser = launch(pw)
        try:
            page = new_context(browser).new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(1500)
            text = page.evaluate("() => (document.querySelector('main, [role=main], article') || document.body).innerText")
        finally:
            browser.close()
    return re.sub(r"\n{3,}", "\n\n", text or "").strip()


class PostingClosed(Exception):
    """The posting is gone: its ATS says so, or the page says it no longer takes applications."""


CLOSED_TEXT = re.compile(
    r"no longer (accepting (new )?applications|available|open|active|posted)|not accepting (new )?applications|"
    r"(job|position|posting|requisition|opening) (has been|was|is) (closed|filled|removed|expired|no longer)|"
    r"(this|the) (job|position|posting|opening) (has )?(expired|closed)|job not found|position not found|"
    r"\b(job|position|posting|opening|requisition)\b[^.]{0,40}\b(has expired|has been (closed|filled|removed))",
    re.I,
)


def _gone(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (404, 410)


def fetch_description(ref: AtsRef, url: str, client: httpx.Client | None = None) -> str:
    """Plain-text job description: ATS API, then embedded JSON-LD, then page HTML, then a rendered page.

    Raises PostingClosed when the ATS no longer has the posting (HTTP 404/410) or the only text
    is a "no longer accepting applications" notice."""
    c = client or _client()
    try:
        if ref.ats == "greenhouse" and ref.company and ref.job_id:
            data = greenhouse_job(ref.company, ref.job_id, ref.region, c)
            parts = [data.get("title", ""), (data.get("location") or {}).get("name", "")]
            parts.append(html_to_text(html.unescape(data.get("content", ""))))
            return "\n".join(p for p in parts if p)
        if ref.ats == "lever" and ref.company and ref.job_id:
            data = lever_job(ref.company, ref.job_id, ref.region, c)
            parts = [data.get("text", ""), (data.get("categories") or {}).get("location", "")]
            parts.append(data.get("descriptionPlain") or html_to_text(data.get("description", "")))
            for section in data.get("lists", []):
                parts.append(section.get("text", ""))
                parts.append(html_to_text(section.get("content", "")))
            parts.append(data.get("additionalPlain") or html_to_text(data.get("additional", "")))
            return "\n".join(p for p in parts if p)
        if ref.ats == "ashby" and ref.company and ref.job_id:
            data = ashby_job(ref.company, ref.job_id, c)
            if data:
                parts = [data.get("title", ""), data.get("location", "")]
                comp = (data.get("compensation") or {}).get("compensationTierSummary")
                if comp:
                    parts.append(f"Compensation: {comp}")
                parts.append(data.get("descriptionPlain") or html_to_text(data.get("descriptionHtml", "")))
                return "\n".join(p for p in parts if p)
        if ref.ats == "workable" and ref.company and ref.job_id:
            r = c.get(f"https://apply.workable.com/api/v2/accounts/{ref.company}/jobs/{ref.job_id}")
            if r.status_code == 200:
                data = r.json()
                parts = [data.get("title", ""), str((data.get("location") or {}).get("city", ""))]
                for section in ("description", "requirements", "benefits"):
                    if data.get(section):
                        parts.append(html_to_text(data[section]))
                text = "\n".join(p for p in parts if p)
                if len(text) > 300:
                    return text
        if ref.ats == "workday":
            data = workday_job(url, c)
            if data and data.get("jobDescription"):
                parts = [data.get("title", ""), data.get("location", ""), data.get("timeType", "")]
                parts.append(html_to_text(data["jobDescription"]))
                return "\n".join(p for p in parts if p)
    except httpx.HTTPError as exc:
        if _gone(exc) and ref.ats in ("greenhouse", "lever", "workday"):
            raise PostingClosed(f"{ref.ats} no longer lists this posting (HTTP {exc.response.status_code})") from exc
    except (ValueError, KeyError):
        pass
    text = ""
    page_gone = 0
    try:
        r = c.get(url)
        r.raise_for_status()
        text = jsonld_description(r.text) or html_to_text(r.text)
    except httpx.HTTPError as exc:
        if _gone(exc):  # single-page apps sometimes answer 404 and still render the job: look first
            page_gone = exc.response.status_code
    if len(text) < 600:  # probably a JavaScript app: render it
        try:
            rendered = render_page_text(url)
            if len(rendered) > len(text):
                text = rendered
        except Exception:  # noqa: BLE001
            pass
    if page_gone and len(text) < 600:
        raise PostingClosed(f"the posting page is gone (HTTP {page_gone})")
    if not text:
        raise httpx.HTTPError(f"could not read the job description at {url}")
    if len(text) < 1500 and CLOSED_TEXT.search(text):
        raise PostingClosed("the posting page says it no longer accepts applications")
    return text
