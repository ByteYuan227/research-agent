"""The candidate profile: who you are, what you have done, and what you want.

Everything the agent writes on your behalf comes from this file. It is the
single source of truth for resumes and application answers, so the agent never
has to (and is not allowed to) invent facts.

It contains personal data, so it never lives in the repository: locally it is
``~/.jobagent/profile.yaml``; in GitHub Actions it comes from the
``JOBAGENT_PROFILE_YAML`` secret.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Location(_Model):
    city: str = ""
    state: str = ""
    country: str = "United States"
    zip: str = ""

    def display(self) -> str:
        return ", ".join(p for p in (self.city, self.state) if p) or self.country


class Personal(_Model):
    first_name: str
    last_name: str
    preferred_name: str = ""
    email: str
    phone: str = ""
    location: Location = Field(default_factory=Location)
    linkedin: str = ""
    github: str = ""
    website: str = ""
    pronouns: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class WorkAuthorization(_Model):
    authorized_to_work_in_us: bool
    requires_sponsorship_now: bool
    requires_sponsorship_future: bool
    status: str = ""  # e.g. "F-1 student (eligible for CPT/OPT)", "U.S. citizen"
    us_citizen: bool = False
    security_clearance: str = ""  # leave empty if none


class Bullet(_Model):
    id: str
    text: str
    tags: list[str] = Field(default_factory=list)


class Education(_Model):
    id: str
    school: str
    degree: str  # e.g. "B.S.", "M.S."
    field: str
    start: str = ""
    end: str = ""  # "May 2027" or "Expected May 2027"
    gpa: str = ""
    location: str = ""
    coursework: list[str] = Field(default_factory=list)
    bullets: list[Bullet] = Field(default_factory=list)


class Experience(_Model):
    id: str
    company: str
    title: str
    location: str = ""
    start: str = ""
    end: str = ""  # "Present" for a current role
    bullets: list[Bullet] = Field(default_factory=list)


class Project(_Model):
    id: str
    name: str
    link: str = ""
    tech: list[str] = Field(default_factory=list)
    date: str = ""
    bullets: list[Bullet] = Field(default_factory=list)


class Summary(_Model):
    id: str
    text: str


class Eeo(_Model):
    """Voluntary self-identification. 'decline' picks the 'decline to answer' option."""

    gender: str = "decline"
    race: str = "decline"
    hispanic_latino: str = "decline"
    veteran: str = "decline"
    disability: str = "decline"


class Answers(_Model):
    """Standard answers for application questions. Extra keys are allowed."""

    model_config = ConfigDict(extra="allow")

    how_did_you_hear: str = "Online job board"
    earliest_start_date: str = ""
    graduation_date: str = ""
    willing_to_relocate: bool = True
    open_to_remote: bool = True
    open_to_onsite: bool = True
    salary_expectation: str = ""  # leave empty to let the agent answer "negotiable"
    over_18: bool = True
    previously_employed_at: list[str] = Field(default_factory=list)  # company names
    previously_interviewed_at: list[str] = Field(default_factory=list)  # company names
    # Consent questions. Privacy-notice acknowledgements are routine; arbitration
    # agreements waive legal rights, so the agent only accepts them if you opt in.
    acknowledge_privacy_notices: bool = True
    accept_arbitration_agreements: bool = False
    # Short free-text answers ("Why us?") written from your profile and the job
    # description. Never used where the employer restricts AI-written answers.
    write_short_answers: bool = True
    eeo: Eeo = Field(default_factory=Eeo)


class CompanyBoard(_Model):
    name: str
    ats: Literal["greenhouse", "lever", "ashby"]
    token: str  # board token / site / org slug as it appears in the job URL
    region: Literal["us", "eu"] = "us"


class Sources(_Model):
    simplify_internships: bool = True
    simplify_new_grad: bool = True
    companies: list[CompanyBoard] = Field(default_factory=list)


class Preferences(_Model):
    job_types: list[Literal["internship", "new_grad", "full_time"]] = Field(default_factory=lambda: ["internship"])
    terms: list[str] = Field(default_factory=list)  # e.g. ["Summer 2027"]; empty = any
    categories: list[str] = Field(default_factory=lambda: ["AI/ML/Data", "Software"])
    title_include: list[str] = Field(default_factory=list)  # regexes; empty = any title
    title_exclude: list[str] = Field(
        default_factory=lambda: [r"\bsenior\b", r"\bstaff\b", r"\bprincipal\b", r"\bmanager\b", r"\bdirector\b"]
    )
    keywords: list[str] = Field(default_factory=list)  # used to rank candidates before scoring
    locations: list[str] = Field(default_factory=list)  # substrings like "CA", "New York", "Remote"; empty = any US
    exclude_companies: list[str] = Field(default_factory=list)
    posted_within_days: int = 14
    min_score: int = Field(75, ge=0, le=100)
    max_applications_per_day: int = Field(10, ge=0, le=50)
    max_candidates_per_run: int = Field(40, ge=1, le=200)
    auto_apply: bool = True
    sources: Sources = Field(default_factory=Sources)

    @field_validator("title_include", "title_exclude")
    @classmethod
    def _valid_regex(cls, v: list[str]) -> list[str]:
        import re

        for pattern in v:
            re.compile(pattern)
        return v


class Notify(_Model):
    github_username: str = ""  # @-mentioned in the daily report issue
    email_to: str = ""  # optional: also email the report (needs SMTP secrets)
    language: Literal["zh", "en"] = "zh"
    timezone: str = "America/New_York"  # what "today" means for daily limits and reports


class Profile(_Model):
    personal: Personal
    work_authorization: WorkAuthorization
    education: list[Education] = Field(min_length=1)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    summaries: list[Summary] = Field(default_factory=list)
    answers: Answers = Field(default_factory=Answers)
    preferences: Preferences = Field(default_factory=Preferences)
    notify: Notify = Field(default_factory=Notify)

    @model_validator(mode="after")
    def _unique_ids(self) -> "Profile":
        seen: set[str] = set()
        for item_id in self.all_ids():
            if item_id in seen:
                raise ValueError(f"duplicate id in profile: {item_id!r}")
            seen.add(item_id)
        return self

    def all_ids(self) -> list[str]:
        ids: list[str] = []
        for group in (self.education, self.experience, self.projects):
            for item in group:
                ids.append(item.id)
                ids.extend(b.id for b in item.bullets)
        ids.extend(s.id for s in self.summaries)
        return ids

    def bullet_index(self) -> dict[str, Bullet]:
        index: dict[str, Bullet] = {}
        for group in (self.education, self.experience, self.projects):
            for item in group:
                for b in item.bullets:
                    index[b.id] = b
        return index


class ProfileError(Exception):
    pass


def jobagent_home() -> Path:
    return Path(os.environ.get("JOBAGENT_HOME", Path.home() / ".jobagent"))


def load_profile_data() -> dict[str, Any] | None:
    raw = os.environ.get("JOBAGENT_PROFILE_YAML")
    if raw and raw.strip():
        return yaml.safe_load(raw)
    path = Path(os.environ.get("JOBAGENT_PROFILE", jobagent_home() / "profile.yaml"))
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return None


def load_profile() -> Profile:
    data = load_profile_data()
    if data is None:
        raise ProfileError(
            "No profile found. Set JOBAGENT_PROFILE_YAML, or create ~/.jobagent/profile.yaml "
            "(start from examples/profile.example.yaml)."
        )
    try:
        return Profile.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError
        raise ProfileError(f"Profile is invalid:\n{exc}") from exc


def has_profile() -> bool:
    return load_profile_data() is not None
