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
    street: str = ""  # Workday and some portals require a mailing address
    street2: str = ""
    city: str = ""
    state: str = ""  # two-letter code, e.g. PA
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
    permanent_resident: bool = False  # green card holder
    citizenships: list[str] = Field(default_factory=list)  # e.g. ["China"]
    visa_type: str = ""  # e.g. "F-1", "H-1B", "" for citizens / permanent residents
    # Export-control "U.S. person" (ITAR/EAR): citizen, permanent resident, refugee or asylee.
    export_control_us_person: bool | None = None  # None = derive from citizen/permanent resident
    security_clearance: str = ""  # leave empty if none

    @property
    def us_person(self) -> bool:
        if self.export_control_us_person is not None:
            return self.export_control_us_person
        return self.us_citizen or self.permanent_resident


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


class Declarations(_Model):
    """Background questions answered in advance, so applications can go out unattended."""

    criminal_record: bool = False  # ever convicted of a felony / crime
    pending_charges: bool = False
    consent_background_check: bool = True
    consent_drug_test: bool = True
    non_compete: bool = False  # bound by a non-compete / non-solicit agreement
    relatives_at_company: bool = False  # family member working at the company
    government_official: bool = False  # current/former government official (conflict-of-interest questions)


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
    # Optional: the answer to use instead when the job description states its pay: a figure
    # (e.g. "$120,000") or "midpoint" (the middle of the posted range, e.g. $120K-$180K -> $150,000).
    salary_expectation_if_posted: str = ""
    # GPA only where a form requires it (never on the resume, optional GPA questions left blank).
    gpa_only_when_required: bool = False
    over_18: bool = True
    previously_employed_at: list[str] = Field(default_factory=list)  # company names
    previously_interviewed_at: list[str] = Field(default_factory=list)  # company names
    # Consent questions: privacy notices, terms, arbitration and other agreements are
    # accepted automatically. Factual statements ("I am a U.S. citizen") are never
    # auto-accepted; they are answered from the profile.
    # "Would you like to be considered for other roles / join our talent community / get job
    # alerts / receive text updates?" -> yes.
    accept_opt_ins: bool = True
    acknowledge_privacy_notices: bool = True
    accept_agreements: bool = True
    # Free-text answers ("Why us?", cover letters) written by Claude from the profile and
    # the job description. Skipped only where the employer explicitly forbids AI-written answers.
    write_answers: bool = True
    eeo: Eeo = Field(default_factory=Eeo)


class CustomAnswer(_Model):
    """Your own answer to a recurring question, e.g. {match: "felony|convicted", answer: "No"}."""

    match: str  # regular expression (case-insensitive) matched against the question text
    answer: str | bool | list[str]
    note: str = ""

    @field_validator("match")
    @classmethod
    def _valid(cls, v: str) -> str:
        import re

        re.compile(v)
        return v


class CompanyBoard(_Model):
    name: str
    ats: Literal["greenhouse", "lever", "ashby"]
    token: str  # board token / site / org slug as it appears in the job URL
    region: Literal["us", "eu"] = "us"


class Sources(_Model):
    simplify_internships: bool = True
    simplify_new_grad: bool = True
    speedyapply_new_grad: bool = True  # github.com/speedyapply/2027-SWE-College-Jobs (new grad, USA)
    popular_boards: bool = False  # ~60 company career boards (Stripe, Databricks, Palantir...): entry-level roles
    companies: list[CompanyBoard] = Field(default_factory=list)
    extra_urls: list[str] = Field(default_factory=list)  # specific postings you want considered


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
    max_applications_per_day: int | None = Field(None, ge=0)  # None = no limit
    # Optional: applications to one company within 30 days (best matches first). None = no limit.
    max_per_company: int | None = Field(None, ge=1)
    max_candidates_per_run: int = Field(150, ge=1, le=2000)
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
    # false = no GitHub issue, the report only goes by email (keeps where you apply private in a
    # public repository); jobs you applied to by hand are then marked by replying "done <numbers>".
    github_issue: bool = True
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
    declarations: Declarations = Field(default_factory=Declarations)
    # Your answers to recurring questions; they take priority over every built-in rule.
    custom_answers: list[CustomAnswer] = Field(default_factory=list)
    # Free-form facts about you that Claude may use when a question is not covered above.
    about_me: list[str] = Field(default_factory=list)
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
