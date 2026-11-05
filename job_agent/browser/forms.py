"""Reading and filling application forms in a real browser.

The flow for one application:

1. ``open_form``   load the page and run ``extract.js``: every field with its
                   label, options, required flag and description.
2. ``suggest``     deterministic answers from the profile for the standard
                   questions (name, email, work authorization, EEO, ...).
3. Claude answers whatever is left, using only the profile.
4. ``fill_all``    type/select/click every answer into the page.
5. ``submit``      click submit and classify what happens next.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..profile import Profile

EXTRACT_JS = (Path(__file__).with_name("extract.js")).read_text(encoding="utf-8")
DECLINE = "__decline__"  # pick the "decline to answer" option
ACK = "__ack__"  # pick the "I acknowledge / agree / yes" option

OPTIONS_JS = """(key) => {
  const el = document.querySelector(`[data-ja-key="${CSS.escape(key)}"]`);
  if (!el) return null;
  const texts = (nodes) => Array.from(nodes)
    .map(n => (n.innerText || n.textContent || '').replace(/\\s+/g, ' ').trim()).filter(Boolean);
  const ctl = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  if (ctl) {
    const lb = document.getElementById(ctl);
    if (lb) { const o = texts(lb.querySelectorAll('[role=option]')); if (o.length) return o; }
  }
  const box = el.closest('.select__container, .select-shell, [data-field-path], .field-wrapper, .application-question')
    || el.parentElement;
  const own = texts(box.querySelectorAll('[role=option]'));
  if (own.length) return own;
  const visible = Array.from(document.querySelectorAll('[role=listbox]')).filter(l => {
    const r = l.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  if (visible.length) return texts(visible[visible.length - 1].querySelectorAll('[role=option]'));
  return [];
}"""

SUCCESS_RE = re.compile(
    r"thank(s| you) for (applying|your application|your interest|submitting)|"
    r"application (was |has been )?(successfully )?(submitted|received|sent)|"
    r"we(')?ve received your application|successfully (submitted|applied)|your application is (in|complete)",
    re.I,
)
CODE_RE = re.compile(r"(verification|security|confirmation|one[- ]time) code|enter the code|code (was|has been) sent", re.I)
_NOT_ADDRESS = ":not([name*='zip' i]):not([name*='postal' i]):not([id*='zip' i]):not([id*='postal' i]):not([id*='country' i])"
CODE_INPUTS = ", ".join(
    s + _NOT_ADDRESS
    for s in (
        "input[autocomplete='one-time-code']",
        "input[name*='code' i]",
        "input[id*='code' i]",
        "input[aria-label*='code' i]",
        "input[placeholder*='code' i]",
        "input[id*='security' i]",
    )
)


# ---------------------------------------------------------------------------
# Option matching
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = s.replace("’", "'")
    return re.sub(r"[^a-z0-9+' ]+", " ", s).strip()


DECLINE_RE = re.compile(
    r"decline|prefer not|don'?t wish|do not wish|not to (say|answer|disclose|self)|choose not|rather not|i don'?t want",
    re.I,
)


ACK_RE = re.compile(r"acknowledg|confirm|i agree|agree|accept|i have read|i understand|^yes\b", re.I)


def match_option(value: Any, options: list[str]) -> str | None:
    """Pick the option that best matches ``value`` (exact > prefix > contains > word overlap)."""
    if not options:
        return None
    if value == DECLINE:
        return next((o for o in options if DECLINE_RE.search(o)), None)
    if value == ACK:
        return next((o for o in options if ACK_RE.search(o) and not re.search(r"\bnot\b|\bno\b|decline", o, re.I)), None)
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    v = _norm(str(value))
    if not v:
        return None
    normed = [(o, _norm(o)) for o in options]
    for o, n in normed:
        if n == v:
            return o
    for o, n in normed:
        if n.startswith(v) or v.startswith(n) and len(n) >= 2:
            return o
    for o, n in normed:
        if v in n:
            return o
    vw = set(v.split())
    best, best_score = None, 0.0
    for o, n in normed:
        ow = set(n.split())
        if not ow:
            continue
        score = len(vw & ow) / len(vw | ow)
        if score > best_score:
            best, best_score = o, score
    return best if best_score >= 0.34 else None


# ---------------------------------------------------------------------------
# Deterministic suggestions from the profile
# ---------------------------------------------------------------------------


@dataclass
class Suggestion:
    value: Any
    source: str


@dataclass
class Rule:
    pattern: str
    kinds: tuple[str, ...] | None
    fn: Any  # (profile, field, company) -> Suggestion | None
    on: str = "label"  # "label", "key" or "both"


def _yes_no(flag: bool, src: str) -> Suggestion:
    return Suggestion("Yes" if flag else "No", src)


def _location(p: Profile) -> str:
    loc = p.personal.location
    return ", ".join(x for x in (loc.city, loc.state) if x)


def _latest_school(p: Profile):
    return p.education[0]


def _salary(p: Profile, f: dict) -> Suggestion | None:
    if f["kind"] not in ("text", "textarea", "number"):
        return None
    if p.answers.salary_expectation:
        return Suggestion(p.answers.salary_expectation, "answers.salary_expectation")
    return Suggestion("Open / negotiable", "default")


def _in(company: str, names: list[str]) -> bool:
    c = _norm(company)
    return any(_norm(n) and (_norm(n) in c or c in _norm(n)) for n in names)


def degree_aliases(degree: str) -> list[str]:
    """Ways a degree is spelled in dropdowns: 'M.S.' -> ["Master's Degree", 'Master', ...]."""
    d = _norm(degree).replace(" ", "")
    if d.startswith(("ms", "msc", "ma", "meng", "mba", "master", "mphil", "mfa")):
        return ["Master's Degree", "Master's", "Masters", "Master", degree]
    if d.startswith(("bs", "bsc", "ba", "beng", "bachelor", "ab", "bfa", "bba")):
        return ["Bachelor's Degree", "Bachelor's", "Bachelors", "Bachelor", degree]
    if d.startswith(("phd", "dphil", "doctor")):
        return ["Doctor of Philosophy (Ph.D.)", "Ph.D.", "PhD", "Doctorate", degree]
    if d.startswith(("as", "aa", "associate")):
        return ["Associate's Degree", "Associate", degree]
    return [degree]


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]


def _year(s: str) -> str | None:
    m = re.search(r"(19|20)\d\d", s or "")
    return m.group(0) if m else None


def _month(s: str) -> str | None:
    for name in _MONTHS:
        if re.search(rf"\b{name[:3]}", s or "", re.I):
            return name
    return None


def _degree(p: Profile, f: dict) -> Suggestion | None:
    degree = p.education[0].degree
    options = f.get("options") or []
    if not options:
        return Suggestion(degree, "education[0].degree")
    for alias in degree_aliases(degree):
        picked = match_option(alias, options)
        if picked:
            return Suggestion(picked, "education[0].degree")
    return None


def _sponsorship(p: Profile, f: dict) -> Suggestion:
    """Sponsorship questions: 'now' only when the question clearly asks about now; otherwise now-or-future."""
    wa = p.work_authorization
    text = f"{f['label']} {f.get('description', '')}"
    if re.search(r"currently|at this time|right now|today", text, re.I) and not re.search(r"future", text, re.I):
        return _yes_no(wa.requires_sponsorship_now, "work_authorization.requires_sponsorship_now")
    return _yes_no(wa.requires_sponsorship_now or wa.requires_sponsorship_future, "work_authorization.requires_sponsorship_*")


TEXTY = ("text", "email", "tel", "url", "textarea")
CHOICE = ("select", "combobox", "radio", "button_group")

RULES: list[Rule] = [
    Rule(r"^(legal )?first name|given name", TEXTY, lambda p, f, c: Suggestion(p.personal.first_name, "personal.first_name")),
    Rule(r"^(legal )?last name|family name|surname", TEXTY, lambda p, f, c: Suggestion(p.personal.last_name, "personal.last_name")),
    Rule(r"preferred (first )?name", TEXTY,
         lambda p, f, c: Suggestion(p.personal.preferred_name or p.personal.first_name, "personal.preferred_name")),
    Rule(r"^(full )?name$|full (legal )?name|legal name|^your name", TEXTY,
         lambda p, f, c: Suggestion(p.personal.full_name, "personal.full_name")),
    Rule(r"e-?mail", ("text", "email"), lambda p, f, c: Suggestion(p.personal.email, "personal.email")),
    Rule(r"phone|mobile", ("text", "tel"), lambda p, f, c: Suggestion(p.personal.phone, "personal.phone") if p.personal.phone else None),
    Rule(r"^country$|country (do|will) you (intend|plan|expect) to (work|reside|live)|country of residence|"
         r"which country.{0,30}(work|reside|live|based)", ("combobox", "select", "text"),
         lambda p, f, c: Suggestion(p.personal.location.country, "personal.location.country")),
    Rule(r"^(current )?location\b|current (city|location)|city.*state|where are you (currently )?(located|based)", TEXTY + ("combobox",),
         lambda p, f, c: Suggestion(_location(p), "personal.location") if _location(p) else None),
    Rule(r"linkedin", TEXTY, lambda p, f, c: Suggestion(p.personal.linkedin, "personal.linkedin") if p.personal.linkedin else None),
    Rule(r"github", TEXTY, lambda p, f, c: Suggestion(p.personal.github, "personal.github") if p.personal.github else None),
    Rule(r"^(personal )?(website|portfolio)( url| link)?$|portfolio (url|link)|personal (website|site)", TEXTY,
         lambda p, f, c: Suggestion(p.personal.website or p.personal.github, "personal.website") if (p.personal.website or p.personal.github) else None),
    Rule(r"^(school|university|college)( name)?$|which (school|university)", TEXTY + ("combobox", "select"),
         lambda p, f, c: Suggestion(_latest_school(p).school, "education[0].school")),
    Rule(r"^degree$|degree (type|level)|highest degree", TEXTY + CHOICE, lambda p, f, c: _degree(p, f)),
    Rule(r"start (date )?year", ("number", "text") + CHOICE,
         lambda p, f, c: Suggestion(_year(_latest_school(p).start), "education[0].start") if _year(_latest_school(p).start) else None),
    Rule(r"(end|graduation|completion) (date )?year", ("number", "text") + CHOICE,
         lambda p, f, c: Suggestion(_year(_latest_school(p).end), "education[0].end") if _year(_latest_school(p).end) else None),
    Rule(r"start (date )?month", ("text",) + CHOICE,
         lambda p, f, c: Suggestion(_month(_latest_school(p).start), "education[0].start") if _month(_latest_school(p).start) else None),
    Rule(r"(end|graduation|completion) (date )?month", ("text",) + CHOICE,
         lambda p, f, c: Suggestion(_month(_latest_school(p).end), "education[0].end") if _month(_latest_school(p).end) else None),
    Rule(r"^(discipline|major|field of study)$|area of study", TEXTY + ("combobox", "select"),
         lambda p, f, c: Suggestion(_latest_school(p).field, "education[0].field")),
    Rule(r"graduat(ion|e) (date|year)|when (do|will) you graduate|expected graduation", TEXTY + CHOICE,
         lambda p, f, c: Suggestion(p.answers.graduation_date or _latest_school(p).end, "answers.graduation_date")),
    Rule(r"how did you (hear|find|learn)|where did you (hear|find)|referral source|source of application", TEXTY + CHOICE,
         lambda p, f, c: Suggestion(p.answers.how_did_you_hear, "answers.how_did_you_hear")),
    Rule(r"(legally )?(authorized|eligible|permitted) to work|work authorization|right to work", CHOICE + ("checkbox",),
         lambda p, f, c: _yes_no(p.work_authorization.authorized_to_work_in_us, "work_authorization.authorized_to_work_in_us")),
    Rule(r"(require|need|requir).{0,60}(sponsor|visa)|sponsorship", CHOICE + ("checkbox",), lambda p, f, c: _sponsorship(p, f)),
    Rule(r"(at least|over|older than) (the age of )?18|18 years", CHOICE + ("checkbox",),
         lambda p, f, c: _yes_no(p.answers.over_18, "answers.over_18")),
    Rule(r"(willing|open|able) to relocat|relocation", CHOICE + ("checkbox",),
         lambda p, f, c: _yes_no(p.answers.willing_to_relocate, "answers.willing_to_relocate")),
    Rule(r"(previously|ever|in the past) (been )?(employed|worked) (by|for|at)|have you (ever )?(previously )?worked (for|at)|"
         r"in the past worked for|former employee|current or former", CHOICE + ("checkbox",),
         lambda p, f, c: _yes_no(_in(c, p.answers.previously_employed_at), "answers.previously_employed_at")),
    Rule(r"(ever|previously) interviewed", CHOICE + ("checkbox",),
         lambda p, f, c: _yes_no(_in(c, p.answers.previously_interviewed_at), "answers.previously_interviewed_at")),
    Rule(r"earliest (start|available)|start date|when can you start|available to start", TEXTY,
         lambda p, f, c: Suggestion(p.answers.earliest_start_date, "answers.earliest_start_date") if p.answers.earliest_start_date else None),
    Rule(r"salary|compensation expectation|expected (pay|compensation)", None, lambda p, f, c: _salary(p, f)),
    # Voluntary self-identification. Short labels only, so long attestations that
    # merely mention "veteran" or "disability" are not mistaken for EEO questions.
    Rule(r"^(what is your )?gender\b|gender identity", CHOICE,
         lambda p, f, c: _eeo(p.answers.eeo.gender, f, "answers.eeo.gender")),
    Rule(r"hispanic|latin[oa]", CHOICE,
         lambda p, f, c: _eeo(p.answers.eeo.hispanic_latino, f, "answers.eeo.hispanic_latino")),
    Rule(r"^(what is your )?race\b|^ethnicity|race.{0,5}ethnicity", CHOICE,
         lambda p, f, c: _eeo(p.answers.eeo.race, f, "answers.eeo.race")),
    Rule(r"^(protected )?veteran|veteran status", CHOICE,
         lambda p, f, c: _eeo(p.answers.eeo.veteran, f, "answers.eeo.veteran")),
    Rule(r"^disability|disability status", CHOICE,
         lambda p, f, c: _eeo(p.answers.eeo.disability, f, "answers.eeo.disability")),
    Rule(r"arbitrat", ("checkbox", "radio", "button_group", "select", "combobox"),
         lambda p, f, c: Suggestion(True if f["kind"] == "checkbox" else ACK, "answers.accept_arbitration_agreements")
         if p.answers.accept_arbitration_agreements else None),
    Rule(r"privacy (notice|policy|statement)|i have read|i acknowledge|acknowledg", ("checkbox", "radio", "button_group", "select", "combobox"),
         lambda p, f, c: Suggestion(True if f["kind"] == "checkbox" else ACK, "answers.acknowledge_privacy_notices")
         if p.answers.acknowledge_privacy_notices and not re.search(r"arbitrat", f["label"] + " " + f.get("description", ""), re.I)
         else None),
]


def _eeo(choice: str, f: dict, src: str) -> Suggestion | None:
    if len(f.get("label") or "") > 90:
        return None
    return Suggestion(DECLINE if choice == "decline" else choice, src)


SITE_PASSWORD = "__site_password__"  # replaced by the vault at fill time; Claude never sees passwords


def suggest(field: dict[str, Any], profile: Profile, company: str, files: dict[str, Path]) -> Suggestion | None:
    kind = field["kind"]
    label = field.get("label") or ""
    if kind == "password":  # account creation / login: generated and stored by the vault
        return Suggestion(SITE_PASSWORD, "vault")
    if kind == "file":
        text = f"{label} {field['key']}".lower()
        if re.search(r"resume|\bcv\b", text) and "resume" in files:
            return Suggestion(str(files["resume"]), "resume")
        if re.search(r"cover", text) and "cover_letter" in files:
            return Suggestion(str(files["cover_letter"]), "cover_letter")
        if field.get("key") == "resume" and "resume" in files:
            return Suggestion(str(files["resume"]), "resume")
        return None
    text = label if label else field["key"]
    for rule in RULES:
        if rule.kinds is not None and kind not in rule.kinds:
            continue
        if re.search(rule.pattern, text, re.I) or (rule.on != "label" and re.search(rule.pattern, field["key"], re.I)):
            s = rule.fn(profile, field, company)
            if s is None or s.value in ("", None):
                return None
            options = field.get("options") or []
            if kind in ("select", "radio", "button_group", "checkbox_group", "combobox") and options:
                picked = match_option(s.value, options)
                if picked is not None:
                    s = Suggestion(picked, s.source)
                elif kind == "combobox" and len(options) >= 80 and s.value not in (DECLINE, ACK):
                    pass  # long searchable list (schools, cities): type the value and pick the match
                else:
                    return None  # no fitting option: leave it for Claude
            if kind == "checkbox" and not isinstance(s.value, bool):
                s = Suggestion(str(s.value).lower().startswith("y") or s.value == ACK, s.source)
            return s
    return None


# ---------------------------------------------------------------------------
# Browser actions
# ---------------------------------------------------------------------------


def _settle(page, ms: int = 1500) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:  # noqa: BLE001 - pages with long-polling never go idle
        pass
    page.wait_for_timeout(ms)


def open_form(page, url: str) -> dict[str, Any]:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    _settle(page)
    data = page.evaluate(EXTRACT_JS)
    if len(data["fields"]) < 3:
        # Some boards show the description first and the form behind an "Apply" button.
        for name in (r"^apply( now| for this job)?$", r"apply"):
            btn = page.get_by_role("button", name=re.compile(name, re.I)).first
            link = page.get_by_role("link", name=re.compile(name, re.I)).first
            for target in (btn, link):
                try:
                    if target.count() and target.is_visible():
                        target.click(timeout=5000)
                        _settle(page)
                        data = page.evaluate(EXTRACT_JS)
                        if len(data["fields"]) >= 3:
                            return data
                except Exception:  # noqa: BLE001
                    continue
    return data


def locate(page, key: str):
    return page.locator(f'[data-ja-key="{key}"]').first


def combobox_options(page, key: str, limit: int = 80) -> list[str]:
    el = locate(page, key)
    try:
        el.scroll_into_view_if_needed(timeout=3000)
        el.click(timeout=4000)
        page.wait_for_timeout(500)
        opts = page.evaluate(OPTIONS_JS, key) or []
        if not opts:
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(400)
            opts = page.evaluate(OPTIONS_JS, key) or []
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        return opts[:limit]
    except Exception:  # noqa: BLE001
        return []


def _click_option(page, key: str, wanted: str) -> bool:
    opts = page.evaluate(OPTIONS_JS, key) or []
    choice = match_option(wanted, opts) if opts else None
    if choice is None:
        return False
    el = locate(page, key)
    box = el.locator(
        "xpath=ancestor::*[contains(@class,'select__container') or contains(@class,'select-shell') or @data-field-path][1]"
    )
    candidates = [box.get_by_role("option", name=choice, exact=True), page.get_by_role("option", name=choice, exact=True)]
    for cand in candidates:
        try:
            if cand.count():
                cand.first.click(timeout=3000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def fill_field(page, field: dict[str, Any], value: Any) -> str | None:
    """Fill one field. Returns an error message, or None on success."""
    kind = field["kind"]
    key = field["key"]
    el = locate(page, key)
    try:
        if kind == "file":
            el.set_input_files(str(value), timeout=10000)
            page.wait_for_timeout(800)
        elif kind in ("text", "email", "tel", "url", "number", "date", "textarea", "password"):
            el.scroll_into_view_if_needed(timeout=3000)
            el.fill(str(value), timeout=5000)
            if kind == "text" and re.search(r"location|city", field.get("label", "") + key, re.I):
                page.wait_for_timeout(1200)  # location autocompletes (Lever, Ashby)
                opts = page.locator("[role=option], .dropdown-location li, .dropdown-results li")
                if opts.count():
                    try:
                        opts.first.click(timeout=2000)
                    except Exception:  # noqa: BLE001
                        pass
        elif kind == "select":
            choice = match_option(value, field.get("options") or [])
            if choice is None:
                return f"no option matches {value!r}"
            el.select_option(label=choice, timeout=5000)
        elif kind == "combobox":
            values = value if isinstance(value, list) else [value]
            is_place = bool(re.search(r"location|city", field.get("label", "") + " " + key, re.I))
            for v in values:
                wanted = str(v)
                if v in (DECLINE, ACK):
                    opts = field.get("options") or combobox_options(page, key)
                    wanted = match_option(v, opts) or ""
                    if not wanted:
                        return f"no option for {v}"
                el.scroll_into_view_if_needed(timeout=3000)
                el.click(timeout=4000)
                page.wait_for_timeout(300)
                if not _click_option(page, key, wanted):
                    query = wanted.split(",")[0] if is_place else wanted
                    el.fill(query[:60], timeout=3000)
                    page.wait_for_timeout(2000 if is_place else 900)  # place search hits a remote API
                    if not _click_option(page, key, wanted) and not (is_place and _click_option(page, key, query)):
                        page.keyboard.press("Enter")
                page.wait_for_timeout(300)
        elif kind in ("radio", "checkbox_group"):
            values = value if isinstance(value, list) else [value]
            options = field.get("options") or []
            for v in values:
                choice = match_option(v, options)
                if choice is None:
                    return f"no option matches {v!r}"
                err = _set_choice(el, options.index(choice))
                if err:
                    return err
        elif kind == "checkbox":
            want = value if isinstance(value, bool) else str(value).lower() in ("yes", "true", "1", "y") or value == ACK
            err = _toggle(el, want)
            if err:
                return err
        elif kind == "button_group":
            choice = match_option(value, field.get("options") or [])
            if choice is None:
                return f"no option matches {value!r}"
            el.get_by_role("button", name=choice, exact=True).first.click(timeout=4000)
        else:
            return f"unsupported field kind {kind}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    return None


def _toggle(input_loc, want: bool) -> str | None:
    """Set a checkbox, coping with custom-styled inputs that ignore normal clicks."""
    try:
        if input_loc.is_checked() == want:
            return None
    except Exception as exc:  # noqa: BLE001
        return f"checkbox not found: {str(exc).splitlines()[0][:120]}"
    attempts = (
        lambda: input_loc.set_checked(want, timeout=3000),
        lambda: input_loc.set_checked(want, timeout=3000, force=True),
        lambda: input_loc.evaluate("e => e.click()"),
    )
    for attempt in attempts:
        try:
            attempt()
        except Exception:  # noqa: BLE001
            pass
        try:
            if input_loc.is_checked() == want:
                return None
        except Exception:  # noqa: BLE001
            pass
    return "could not change the checkbox"


def _set_choice(container, index: int) -> str | None:
    """Check the index-th radio/checkbox inside a question container."""
    inputs = container.locator("input[type=radio], input[type=checkbox]")
    if inputs.count() <= index:
        # The tag sits on the input itself (single-option groups).
        inputs = container
        index = 0
    target = inputs.nth(index)
    try:
        if target.is_checked():
            return None
    except Exception:  # noqa: BLE001
        pass
    attempts = (
        lambda: target.check(timeout=3000),
        lambda: target.check(timeout=3000, force=True),
        lambda: target.evaluate("e => { const l = e.closest('label') || (e.id && document.querySelector(`label[for=\"${e.id}\"]`)); (l || e).click(); }"),
        lambda: target.evaluate("e => e.click()"),
    )
    for attempt in attempts:
        try:
            attempt()
        except Exception:  # noqa: BLE001
            pass
        try:
            if target.is_checked():
                return None
        except Exception:  # noqa: BLE001
            pass
    return "could not select the option"


@dataclass
class FillReport:
    filled: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)


def fill_all(page, fields: list[dict[str, Any]], answers: dict[str, Any]) -> FillReport:
    report = FillReport()
    # Files first: some forms (Lever, Ashby) re-render after a resume upload.
    ordered = sorted(fields, key=lambda f: 0 if f["kind"] == "file" else 1)
    for f in ordered:
        value = answers.get(f["key"])
        if value is None or value == "" or value == []:
            if f.get("required"):
                report.missing_required.append(f["key"])
            continue
        err = fill_field(page, f, value)
        if err:
            report.errors[f["key"]] = err
        else:
            report.filled.append(f["key"])
    return report


def _visible_challenge(page) -> bool:
    for frame_sel in ("iframe[src*='hcaptcha.com'][src*='challenge']", "iframe[title*='challenge' i]", "iframe[src*='recaptcha/api2/bframe']",
                      "iframe[src*='recaptcha/enterprise/bframe']"):
        loc = page.locator(frame_sel)
        for i in range(loc.count()):
            try:
                box = loc.nth(i).bounding_box()
            except Exception:  # noqa: BLE001
                box = None
            if box and box["width"] > 100 and box["height"] > 100:
                return True
    return False


def visible_errors(page) -> list[str]:
    js = """() => Array.from(document.querySelectorAll(
        '[role=alert], .error, .error-message, [class*="error" i]:not(input):not(form), [aria-invalid=true]'))
      .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
      .map(e => (e.getAttribute('aria-invalid') === 'true'
          ? 'invalid: ' + (e.getAttribute('aria-label') || e.name || e.id)
          : (e.innerText || '').replace(/\\s+/g, ' ').trim()))
      .filter(Boolean).slice(0, 12)"""
    try:
        return [e for e in page.evaluate(js) if len(e) < 300]
    except Exception:  # noqa: BLE001
        return []


def outcome(page, before_url: str, timeout_s: int = 30) -> dict[str, Any]:
    """Classify the page after clicking submit."""
    deadline = time.time() + timeout_s
    last_errors: list[str] = []
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        try:
            text = page.evaluate("() => document.body.innerText.slice(0, 20000)")
        except Exception:  # noqa: BLE001 - navigating
            continue
        if SUCCESS_RE.search(text) or re.search(r"/(confirmation|thank|thanks|success|submitted)\b", page.url):
            return {"status": "submitted", "url": page.url}
        if _visible_challenge(page):
            return {"status": "captcha", "url": page.url}
        if CODE_RE.search(text) and page.locator(CODE_INPUTS).count():
            return {"status": "needs_code", "url": page.url}
        last_errors = visible_errors(page)
    return {"status": "unknown", "url": page.url, "errors": last_errors}


def enter_code(page, code: str) -> bool:
    inputs = page.locator(CODE_INPUTS)
    n = inputs.count()
    if not n:
        return False
    if n >= len(code) > 1:  # one box per character
        for i, ch in enumerate(code):
            inputs.nth(i).fill(ch)
    else:
        inputs.first.fill(code)
    btn = page.get_by_role("button", name=re.compile(r"verify|confirm|submit|continue", re.I)).first
    if btn.count():
        btn.click(timeout=5000)
    else:
        page.keyboard.press("Enter")
    return True
