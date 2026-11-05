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
      "skills": {"Languages": ["Python", "C++"], "ML": ["PyTorch"]}
    }
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape

from .profile import Profile

SECTIONS = ("education", "experience", "projects", "skills")


class PlanError(ValueError):
    pass


def validate_plan(plan: dict[str, Any] | None, profile: Profile) -> dict[str, Any]:
    """Return a normalised plan, or raise PlanError explaining what is not in the profile."""
    plan = dict(plan or {})
    unknown = set(plan) - {"summary", "sections", *SECTIONS, "notes"}
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
            item["bullets"] = [bullets[b].text for b in entry["bullets"]]
            rows.append(item)
        return rows

    summary = next((s.text for s in profile.summaries if s.id == plan["summary"]), "")
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
  @page { size: Letter; margin: 0.5in 0.55in; }
  body { font-family: "Helvetica Neue", Helvetica, Arial, "Liberation Sans", sans-serif; font-size: 10.2pt;
         line-height: 1.32; color: #111; margin: 0; }
  h1 { font-size: 19pt; margin: 0; letter-spacing: 0.3px; }
  .contact { margin-top: 2px; font-size: 9.6pt; color: #333; }
  .contact span + span::before { content: "  |  "; color: #888; }
  h2 { font-size: 10.6pt; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1px solid #222;
       margin: 11px 0 5px; padding-bottom: 1px; }
  .row { display: flex; justify-content: space-between; gap: 12px; }
  .row b { font-weight: 700; }
  .sub { font-style: italic; }
  .entry { margin-bottom: 6px; page-break-inside: avoid; }
  ul { margin: 2px 0 0 0; padding-left: 15px; }
  li { margin: 1px 0; }
  .summary { margin: 6px 0 0; }
  .skills div { margin: 1px 0; }
  a { color: inherit; text-decoration: none; }
</style></head><body>
<h1>{{ p.first_name }} {{ p.last_name }}</h1>
<div class="contact">
  {% if p.location.city %}<span>{{ p.location.display() }}</span>{% endif %}
  {% if p.phone %}<span>{{ p.phone }}</span>{% endif %}
  <span>{{ p.email }}</span>
  {% if p.linkedin %}<span>{{ p.linkedin | shorturl }}</span>{% endif %}
  {% if p.github %}<span>{{ p.github | shorturl }}</span>{% endif %}
  {% if p.website %}<span>{{ p.website | shorturl }}</span>{% endif %}
</div>
{% if summary %}<p class="summary">{{ summary }}</p>{% endif %}
{% for section in sections %}
{% if section == "education" and education %}
<h2>Education</h2>
{% for e in education %}<div class="entry">
  <div class="row"><b>{{ e.school }}</b><span>{{ e.location }}</span></div>
  <div class="row"><span class="sub">{{ e.degree }} in {{ e.field }}{% if e.gpa %}; GPA {{ e.gpa }}{% endif %}</span>
    <span>{{ e.start }}{% if e.start and e.end %} – {% endif %}{{ e.end }}</span></div>
  {% if e.coursework %}<div>Coursework: {{ e.coursework | join(", ") }}</div>{% endif %}
  {% if e.bullets %}<ul>{% for b in e.bullets %}<li>{{ b }}</li>{% endfor %}</ul>{% endif %}
</div>{% endfor %}
{% elif section == "experience" and experience %}
<h2>Experience</h2>
{% for e in experience %}<div class="entry">
  <div class="row"><b>{{ e.title }}, {{ e.company }}</b><span>{{ e.start }}{% if e.start and e.end %} – {% endif %}{{ e.end }}</span></div>
  {% if e.location %}<div class="sub">{{ e.location }}</div>{% endif %}
  {% if e.bullets %}<ul>{% for b in e.bullets %}<li>{{ b }}</li>{% endfor %}</ul>{% endif %}
</div>{% endfor %}
{% elif section == "projects" and projects %}
<h2>Projects</h2>
{% for e in projects %}<div class="entry">
  <div class="row"><b>{{ e.name }}{% if e.tech %}<span style="font-weight:400"> | {{ e.tech | join(", ") }}</span>{% endif %}</b>
    <span>{% if e.link %}{{ e.link | shorturl }}{% else %}{{ e.date }}{% endif %}</span></div>
  {% if e.bullets %}<ul>{% for b in e.bullets %}<li>{{ b }}</li>{% endfor %}</ul>{% endif %}
</div>{% endfor %}
{% elif section == "skills" and skills %}
<h2>Skills</h2>
<div class="skills">{% for group, items in skills.items() %}<div><b>{{ group }}:</b> {{ items | join(", ") }}</div>{% endfor %}</div>
{% endif %}
{% endfor %}
</body></html>
"""


def _shorturl(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url or "").rstrip("/")


def render_html(profile: Profile, plan: dict[str, Any] | None = None) -> str:
    env = Environment(autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters["shorturl"] = _shorturl
    return env.from_string(TEMPLATE).render(**resume_context(profile, plan or {}))


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


def build_resume(profile: Profile, plan: dict[str, Any] | None, out_dir: Path) -> Path:
    html = render_html(profile, plan)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "resume.html").write_text(html, encoding="utf-8")
    (out_dir / "resume.txt").write_text(render_text(profile, plan), encoding="utf-8")
    return html_to_pdf(html, out_dir / resume_filename(profile))


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
