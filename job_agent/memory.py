"""Answers learned from earlier applications.

When Claude answers a question the profile does not cover (a dropdown, a yes/no,
a short fact), the answer is remembered under a normalised form of the question
and suggested automatically the next time, so the same question gets the same
answer everywhere and costs no tokens. Essays and anything naming the company
are never remembered. Your custom_answers always win over memory.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .store import State

REMEMBER_KINDS = {"select", "combobox", "listbox", "radio", "button_group", "checkbox", "checkbox_group",
                  "text", "email", "tel", "url", "number", "date"}
# Never remembered, even if Claude answered them: personal, sensitive or job-specific facts.
SENSITIVE_RE = re.compile(
    r"gender|race|ethnic|hispanic|latin[oa]|veteran|disab|health|medical|pregnan|religio|sexual|orientation|"
    r"criminal|convict|felony|salary|compensation|pay (rate|range)|\bssn\b|social security|date of birth|birth ?date|"
    r"\bage\b|address|zip|postal|passport|driver'?s licen[cs]e|bank|account number|password|reference|"
    r"phone|e-?mail|name",
    re.I,
)
ESSAY_RE = re.compile(r"\bwhy\b|describe|tell us|explain|cover letter|anything else|additional information|"
                      r"what (excit|interest|motivat)|how would you|share (a|an|your)", re.I)


def question_key(label: str, company: str = "") -> str:
    q = label.lower()
    if company:
        q = q.replace(company.lower(), "<company>")
    q = re.sub(r"[^a-z0-9<>]+", " ", q)
    q = re.sub(r"\b(required|optional|please|select one)\b", " ", q)
    return re.sub(r"\s+", " ", q).strip()[:200]


def rememberable(field: dict[str, Any], value: Any, company: str) -> bool:
    label = field.get("label") or ""
    if field.get("kind") not in REMEMBER_KINDS or not label or ESSAY_RE.search(label) or SENSITIVE_RE.search(label):
        return False
    if isinstance(value, str):
        if len(value) > 120 or value.startswith("/") or value.startswith("__"):
            return False
        if company and company.lower() in value.lower():
            return False
    return value not in (None, "", [])


def remember(state: State, fields: list[dict[str, Any]], answers: dict[str, Any], company: str) -> list[str]:
    """Store Claude's answers (keys in `answers`) for questions worth reusing. Returns the new questions."""
    by_key = {f["key"]: f for f in fields}
    learned = []
    for k, v in answers.items():
        f = by_key.get(k)
        if f is None or f.get("suggested") is not None or not rememberable(f, v, company):
            continue
        qk = question_key(f["label"], company)
        if not qk:
            continue
        entry = state.memory.get(qk)
        if entry is None:
            learned.append(f["label"][:160])
        state.memory[qk] = {"question": f["label"][:200], "answer": v, "kind": f["kind"],
                            "uses": (entry or {}).get("uses", 0) + 1, "updated": time.time(),
                            "learned_from": (entry or {}).get("learned_from") or company}
    if learned:
        state.last_run.setdefault("learned", []).extend(learned)
    return learned


def recall(memory: dict[str, dict[str, Any]] | None, field: dict[str, Any], company: str) -> Any:
    if not memory or not field.get("label"):
        return None
    entry = memory.get(question_key(field["label"], company))
    return None if entry is None else entry.get("answer")
