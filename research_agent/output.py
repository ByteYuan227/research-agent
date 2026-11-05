"""Saving a research run to disk."""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

from .agent import ResearchResult


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^\w一-鿿]+", "-", text).strip("-").lower()
    return slug[:max_len].rstrip("-") or "research"


def save_result(result: ResearchResult, out_dir: Path, model: str, save_transcript: bool = False) -> Path:
    """Write report.md, findings.json (and optionally transcript.json). Returns the run folder."""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = out_dir / f"{stamp}-{slugify(result.question)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "report.md").write_text(result.report + "\n", encoding="utf-8")

    meta = {
        "question": result.question,
        "status": result.status,
        "model": model,
        "turns": result.turns,
        "usage": result.usage.__dict__,
        "estimated_cost_usd": result.usage.estimated_cost(model),
        "api_citations": result.citations,
        **result.notebook.to_dict(),
    }
    (run_dir / "findings.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if save_transcript:
        transcript = [_jsonable(m) for m in result.messages]
        (run_dir / "transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return run_dir


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
