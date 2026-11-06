"""Tailored resumes that cannot contain invented facts.

Claude tailors a resume by writing a *plan*: which summary, which entries and
which bullets to include, in what order, and which skills to list. A plan only
refers to ids from the profile, so every line of the output is something you
wrote yourself. ``validate_plan`` rejects anything else.

Plan format (all keys optional; omitted sections use everything in the profile):

    {
      "summary": "sum_ml",
      "sections": ["education", "experience", "projects", "skills"],
      "education": [{"id": "edu_msu", "bullets": []}],
      "experience": [{"id": "exp_acme", "bullets": ["acme_1", "acme_3"]}],
      "projects": [{"id": "proj_agent", "bullets": ["agent_1"]}],
      "skills": {"Languages": ["Python", "C++"], "ML": ["PyTorch"]},
      "rewrites": {"acme_1": "the same fact, worded the way the job description words it"},
      "summary_text": "optional one-line summary, built only from facts in the profile"
    }

Rewrites and summary_text are checked: no numbers that the original does not
have, and no technology/organisation names that appear nowhere in the profile.
"""

from __future__ import annotations

import base64
import functools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape
from markupsafe import Markup

from .profile import Profile

SECTIONS = ("education", "experience", "projects", "skills")


class PlanError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Fact checks for reworded text (rewrites, summary_text)
# ---------------------------------------------------------------------------

_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]*[A-Za-z0-9+#]|[A-Za-z]")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text)}


def profile_corpus(profile: Profile) -> str:
    parts: list[str] = []
    for group in (profile.education, profile.experience, profile.projects):
        for item in group:
            parts.append(" ".join(str(v) for v in item.model_dump(exclude={"bullets", "id"}).values()))
            parts.extend(b.text for b in item.bullets)
    parts.extend(s for group in profile.skills.values() for s in group)
    parts.extend(s.text for s in profile.summaries)
    parts.extend(profile.about_me)
    parts.append(profile.work_authorization.status)
    return "\n".join(parts)


def _terms(text: str) -> list[str]:
    """Words that look like names of technologies, products or organisations."""
    out = []
    for m in _WORD.finditer(text):
        w = m.group(0).rstrip(".")
        sentence_start = m.start() == 0 or text[: m.start()].rstrip().endswith((".", ";", ":"))
        techy = any(c.isdigit() or c in "+#" for c in w) or any(c.isupper() for c in w[1:])
        proper = w[:1].isupper() and not sentence_start
        if (techy or proper) and len(w) > 1:
            out.append(w)
    return out


def check_rewrite(new: str, source: str, corpus: str, max_len: int) -> list[str]:
    """Problems with reworded text: anything not supported by `source` (numbers) or the profile (terms)."""
    problems = []
    extra = _numbers(new) - _numbers(source)
    if extra:
        problems.append(f"numbers not in the original: {sorted(extra)}")
    vocab = {w.lower().rstrip(".") for w in _WORD.findall(corpus)}
    unknown = sorted({t for t in _terms(new) if t.lower() not in vocab})
    if unknown:
        problems.append(f"terms not in the profile: {unknown}")
    if len(new) > max_len:
        problems.append(f"too long ({len(new)} > {max_len} characters)")
    return problems


def validate_plan(plan: dict[str, Any] | None, profile: Profile) -> dict[str, Any]:
    """Return a normalised plan, or raise PlanError explaining what is not in the profile."""
    plan = dict(plan or {})
    unknown = set(plan) - {"summary", "summary_text", "rewrites", "sections", *SECTIONS, "notes"}
    if unknown:
        raise PlanError(f"unknown plan keys: {sorted(unknown)}")
    errors: list[str] = []
    out: dict[str, Any] = {}

    summaries = {s.id for s in profile.summaries}
    summary = plan.get("summary")
    if summary is not None and summary not in summaries:
        errors.append(f"summary {summary!r} is not one of {sorted(summaries) or 'the profile summaries (none defined)'}")
    out["summary"] = summary

    sections = plan.get("sections") or list(SECTIONS)
    bad = [s for s in sections if s not in SECTIONS]
    if bad:
        errors.append(f"unknown sections {bad}")
    out["sections"] = [s for s in sections if s in SECTIONS]

    for section in ("education", "experience", "projects"):
        items = {i.id: i for i in getattr(profile, section)}
        chosen = plan.get(section)
        if chosen is None:
            out[section] = [{"id": i.id, "bullets": [b.id for b in i.bullets]} for i in items.values()]
            continue
        norm = []
        for entry in chosen:
            if isinstance(entry, str):
                entry = {"id": entry}
            item = items.get(entry.get("id"))
            if item is None:
                errors.append(f"{section}: unknown id {entry.get('id')!r}")
                continue
            own = {b.id for b in item.bullets}
            bullets = entry.get("bullets")
            if bullets is None:
                bullets = [b.id for b in item.bullets]
            stray = [b for b in bullets if b not in own]
            if stray:
                errors.append(f"{section} {item.id}: bullets {stray} do not belong to it")
            if len(set(bullets)) != len(bullets):
                errors.append(f"{section} {item.id}: duplicate bullets")
            norm.append({"id": item.id, "bullets": [b for b in bullets if b in own]})
        out[section] = norm

    skills = plan.get("skills")
    if skills is None:
        out["skills"] = {k: list(v) for k, v in profile.skills.items()}
    else:
        allowed = {s.lower() for group in profile.skills.values() for s in group}
        norm_skills: dict[str, list[str]] = {}
        for group, items in skills.items():
            stray = [s for s in items if s.lower() not in allowed]
            if stray:
                errors.append(f"skills not in profile: {stray}")
            kept = [s for s in items if s.lower() in allowed]
            if kept:
                norm_skills[group] = kept
        out["skills"] = norm_skills

    # Reworded text must keep the same facts.
    corpus = profile_corpus(profile)
    bullets = profile.bullet_index()
    rewrites = plan.get("rewrites") or {}
    if not isinstance(rewrites, dict):
        errors.append("rewrites must map bullet id -> text")
        rewrites = {}
    for bid, text in rewrites.items():
        if bid not in bullets:
            errors.append(f"rewrite for unknown bullet {bid!r}")
            continue
        original = bullets[bid].text
        for problem in check_rewrite(str(text), original, corpus, int(len(original) * 1.5) + 40):
            errors.append(f"rewrite {bid}: {problem}")
    out["rewrites"] = {k: str(v).strip() for k, v in rewrites.items()}
    summary_text = (plan.get("summary_text") or "").strip()
    if summary_text:
        for problem in check_rewrite(summary_text, corpus, corpus, 420):
            errors.append(f"summary_text: {problem}")
    out["summary_text"] = summary_text

    if errors:
        raise PlanError("; ".join(errors))
    return out


def resume_context(profile: Profile, plan: dict[str, Any]) -> dict[str, Any]:
    plan = validate_plan(plan, profile)
    bullets = profile.bullet_index()

    def pick(section: str) -> list[dict[str, Any]]:
        items = {i.id: i for i in getattr(profile, section)}
        rows = []
        for entry in plan[section]:
            item = items[entry["id"]].model_dump()
            item["bullets"] = [plan["rewrites"].get(b) or bullets[b].text for b in entry["bullets"]]
            rows.append(item)
        return rows

    summary = plan["summary_text"] or next((s.text for s in profile.summaries if s.id == plan["summary"]), "")
    return {
        "p": profile.personal,
        "summary": summary,
        "sections": plan["sections"],
        "education": pick("education"),
        "experience": pick("experience"),
        "projects": pick("projects"),
        "skills": plan["skills"],
    }


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{{ p.first_name }} {{ p.last_name }} - Resume</title>
<style>
  {{ font_faces }}
  @page { size: Letter; margin: {{ fit.margin_v }}in {{ fit.margin_h }}in; }
  body { font-family: "CMU Serif", "Latin Modern Roman", "Times New Roman", serif; font-size: {{ fit.font }}pt;
         line-height: {{ fit.line }}; color: #000; margin: 0; }
  .name { text-align: center; font-size: 2.35em; font-weight: 700; line-height: 1.1; }
  .contact { text-align: center; margin-top: 2px; }
  .contact span + span::before { content: " | "; }
  .contact a { color: inherit; text-decoration: underline; }
  h2 { font-size: 1.24em; font-weight: 700; border-bottom: 0.8px solid #000; margin: {{ fit.gap }}px 0 3px;
       padding-bottom: 1px; line-height: 1.15; }
  .row { display: flex; justify-content: space-between; gap: 14px; }
  .row > span:last-child { white-space: nowrap; }
  .it { font-style: italic; }
  .entry { margin: 0 0 {{ fit.entry }}px 0.15in; page-break-inside: avoid; }
  ul { margin: 1px 0 0 0; padding-left: 0.2in; }
  li { margin: 0; padding-left: 1px; }
  .summary { margin: 4px 0 0; }
  .skills { margin-left: 0.15in; }
  .skills div { margin: 0; }
  a { color: inherit; }
</style></head><body data-fit="{{ fit.index }}">
<div class="name">{{ p.first_name }} {{ p.last_name }}</div>
<div class="contact">
  {% if p.location.city and show_location %}<span>{{ p.location.display() }}</span>{% endif %}
  {% if p.phone %}<span>{{ p.phone }}</span>{% endif %}
  <span><a href="mailto:{{ p.email }}">{{ p.email }}</a></span>
  {% if p.linkedin %}<span><a href="{{ p.linkedin }}">{{ p.linkedin | shorturl }}</a></span>{% endif %}
  {% if p.github %}<span><a href="{{ p.github }}">{{ p.github | shorturl }}</a></span>{% endif %}
  {% if p.website %}<span><a href="{{ p.website }}">{{ p.website | shorturl }}</a></span>{% endif %}
</div>
{% if summary %}<p class="summary">{{ summary }}</p>{% endif %}
{% for section in sections %}
{% if section == "education" and education %}
<h2>Education</h2>
{% for e in education %}<div class="entry">
  <div class="row"><b>{{ e.school }}</b><span>{{ e.location }}</span></div>
  <div class="row it"><span>{{ e.degree }} in {{ e.field }}{% if e.gpa %}; GPA: {{ e.gpa }}{% endif %}</span>
    <span>{{ e.start }}{% if e.start and e.end %} – {% endif %}{{ e.end }}</span></div>
  {% if e.coursework or e.bullets %}<ul>
    {% for b in e.bullets %}<li>{{ b | label }}</li>{% endfor %}
    {% if e.coursework %}<li><b>Coursework:</b> {{ e.coursework | join(", ") }}</li>{% endif %}
  </ul>{% endif %}
</div>{% endfor %}
{% elif section == "experience" and experience %}
<h2>Experience</h2>
{% for e in experience %}<div class="entry">
  <div class="row"><b>{{ e.company }}</b><span>{{ e.location }}</span></div>
  <div class="row it"><span>{{ e.title }}</span><span>{{ e.start }}{% if e.start and e.end %} – {% endif %}{{ e.end }}</span></div>
  {% if e.bullets %}<ul>{% for b in e.bullets %}<li>{{ b | ts }}</li>{% endfor %}</ul>{% endif %}
</div>{% endfor %}
{% elif section == "projects" and projects %}
<h2>Projects</h2>
{% for e in projects %}<div class="entry">
  <div class="row"><span><b>{{ e.name }}</b>{% if e.tech %} | <i>{{ e.tech | join(", ") }}</i>{% endif %}</span>
    <span>{% if e.link %}<a href="{{ e.link }}">{{ e.link | shorturl }}</a>{% else %}{{ e.date }}{% endif %}</span></div>
  {% if e.bullets %}<ul>{% for b in e.bullets %}<li>{{ b | ts }}</li>{% endfor %}</ul>{% endif %}
</div>{% endfor %}
{% elif section == "skills" and skills %}
<h2>Technical Skills</h2>
<div class="skills">{% for group, items in skills.items() %}<div><b>{{ group }}</b>: {{ items | join(", ") }}</div>{% endfor %}</div>
{% endif %}
{% endfor %}
</body></html>
"""


def _shorturl(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url or "").rstrip("/")


FONT_DIR = Path(__file__).with_name("resume_fonts")


@functools.lru_cache(maxsize=1)
def _font_faces() -> str:
    """CMU Serif (Computer Modern, as in LaTeX resumes) embedded in the page, so the PDF looks
    the same on any machine. Falls back to system serif fonts if the files are missing."""
    rules = []
    for weight, style, name in ((400, "normal", "500-roman"), (400, "italic", "500-italic"),
                                (700, "normal", "700-roman"), (700, "italic", "700-italic")):
        f = FONT_DIR / f"cmu-serif-{name}.woff2"
        if f.exists():
            data = base64.b64encode(f.read_bytes()).decode()
            rules.append(f'@font-face {{ font-family: "CMU Serif"; font-weight: {weight}; font-style: {style}; '
                         f'src: url(data:font/woff2;base64,{data}) format("woff2"); }}')
    return "\n  ".join(rules)


def _typeset(text: str) -> str:
    """'~2s' -> '∼2s': in Computer Modern the ASCII tilde is a raised accent, as in LaTeX."""
    return re.sub(r"~(?=\s?[\d$])", "∼", text)


def _label(text: str) -> Markup:
    """'Honors: Dean's List' -> '<b>Honors:</b> Dean's List' (a short label before a colon is bold)."""
    text = _typeset(text)
    m = re.match(r"^([A-Z][A-Za-z&/ ]{1,30}):\s+(.*)$", text)
    if not m:
        return Markup.escape(text)
    return Markup("<b>{}:</b> {}").format(m.group(1), m.group(2))


@dataclass(frozen=True)
class Fit:
    index: int
    font: float  # pt
    line: float
    margin_v: float  # in
    margin_h: float
    gap: int  # px above section headings
    entry: int  # px below entries


# Tighter settings tried in turn when the resume runs past one page, before anything is cut.
FIT_STEPS = [
    Fit(0, 10.5, 1.2, 0.5, 0.5, 8, 5),
    Fit(1, 10.2, 1.16, 0.45, 0.5, 6, 4),
    Fit(2, 9.9, 1.13, 0.4, 0.45, 5, 3),
    Fit(3, 9.6, 1.1, 0.35, 0.42, 4, 2),
    Fit(4, 9.3, 1.08, 0.33, 0.4, 3, 2),
]


def render_html(profile: Profile, plan: dict[str, Any] | None = None, fit: Fit | None = None) -> str:
    env = Environment(autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters["shorturl"] = _shorturl
    env.filters["label"] = _label
    env.filters["ts"] = _typeset
    ctx = resume_context(profile, plan or {})
    return env.from_string(TEMPLATE).render(**ctx, fit=fit or FIT_STEPS[0], font_faces=Markup(_font_faces()),
                                            show_location=not profile.personal.phone)


def render_text(profile: Profile, plan: dict[str, Any] | None = None) -> str:
    """Plain-text resume for 'paste your resume' boxes."""
    ctx = resume_context(profile, plan or {})
    p = ctx["p"]
    lines = [p.full_name, " | ".join(x for x in (p.location.display(), p.phone, p.email, p.linkedin, p.github) if x)]
    if ctx["summary"]:
        lines += ["", ctx["summary"]]
    for section in ctx["sections"]:
        rows = ctx.get(section)
        if not rows:
            continue
        lines += ["", section.upper()]
        if section == "skills":
            lines += [f"{g}: {', '.join(items)}" for g, items in rows.items()]
            continue
        for e in rows:
            if section == "education":
                lines.append(f"{e['school']} - {e['degree']} in {e['field']} ({e['start']} - {e['end']})".replace("( - )", ""))
            elif section == "experience":
                lines.append(f"{e['title']}, {e['company']} ({e['start']} - {e['end']})")
            else:
                lines.append(e["name"] + (f" ({', '.join(e['tech'])})" if e.get("tech") else ""))
            lines += [f"- {b}" for b in e["bullets"]]
    return "\n".join(lines).strip() + "\n"


def html_to_pdf(html: str, out: Path) -> Path:
    from playwright.sync_api import sync_playwright

    from .browser.launch import launch

    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = launch(pw, headless=True)
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=str(out), format="Letter", print_background=True, prefer_css_page_size=True)
        browser.close()
    return out


def resume_filename(profile: Profile) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", profile.personal.full_name).strip("_") or "Resume"
    return f"{name}_Resume.pdf"


# Skill names that are also everyday English words: matched only with the profile's own capitalisation.
_COMMON_WORDS = {"go", "react", "rest", "spring", "flask", "rust", "ruby", "express", "swift", "dart", "agile", "git"}


def _skill_terms(profile: Profile) -> list[str]:
    """Technologies and concepts the profile can back up: skills, project tech, coursework.
    "AWS (EC2, ECS, S3)" also yields AWS, EC2, ECS, S3; "Go (Golang)" yields Go and Golang."""
    raw: list[str] = []
    for items in profile.skills.values():
        raw += items
    for pr in profile.projects:
        raw += pr.tech
    for e in profile.education:
        raw += e.coursework
    terms: list[str] = []
    for item in raw:
        m = re.match(r"^(.*?)\s*\((.*)\)\s*$", item)
        parts = [m.group(1)] + [x.strip() for x in m.group(2).split(",")] if m else [item]
        for t in parts:
            t = t.strip()
            if len(t) >= 2 and t.lower() not in {x.lower() for x in terms}:
                terms.append(t)
    return terms


def _term_re(term: str) -> re.Pattern:
    flags = 0 if term.lower() in _COMMON_WORDS or (term.isupper() and len(term) <= 4) else re.I
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9+#])", flags)


def keyword_coverage(profile: Profile, jd_text: str, resume_text: str) -> dict[str, list[str]]:
    """Terms the job description asks for that the profile supports, split by whether this
    resume already shows them. Only the profile's own terms are considered: nothing is invented."""
    covered, missing = [], []
    for term in _skill_terms(profile):
        rx = _term_re(term)
        if rx.search(jd_text):
            (covered if rx.search(resume_text) else missing).append(term)
    return {"jd_terms_in_resume": covered, "jd_terms_in_profile_but_not_in_resume": missing}


def pdf_pages(pdf: Path) -> int:
    data = pdf.read_bytes()
    return data.count(b"/Type /Page") - data.count(b"/Type /Pages")


def build_resume(profile: Profile, plan: dict[str, Any] | None, out_dir: Path) -> Path:
    """Render the resume PDF. If it runs past one page, tighten the layout step by step
    (down to 9.3pt) before giving up; the caller still sees the page count."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "resume.txt").write_text(render_text(profile, plan), encoding="utf-8")
    pdf = out_dir / resume_filename(profile)
    html = ""
    for fit in FIT_STEPS:
        html = render_html(profile, plan, fit)
        html_to_pdf(html, pdf)
        if pdf_pages(pdf) <= 1:
            break
    (out_dir / "resume.html").write_text(html, encoding="utf-8")
    return pdf


def render_letter_html(profile: Profile, text: str) -> str:
    env = Environment(autoescape=select_autoescape(["html"]))
    tpl = env.from_string(
        """<!doctype html><html><head><meta charset="utf-8"><style>
        @page { size: Letter; margin: 0.9in; } body { font-family: Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.45; }
        p { margin: 0 0 10px; } .h { margin-bottom: 18px; }</style></head><body>
        <div class="h"><b>{{ p.full_name }}</b><br>{{ p.email }}{% if p.phone %} | {{ p.phone }}{% endif %}</div>
        {% for para in paras %}<p>{{ para }}</p>{% endfor %}</body></html>"""
    )
    paras = [x.strip() for x in re.split(r"\n\s*\n", text) if x.strip()]
    return tpl.render(p=profile.personal, paras=paras)
