"""Everything you need to apply by hand to the jobs the agent could not finish.

For each job waiting for you (status ``manual``) the bundle holds a folder with
the tailored resume, a cover letter if one was written, the answers prepared for
the form (``answers.md``) and screenshots of how far the agent got. In GitHub
Actions the bundle is encrypted with JOBAGENT_STATE_KEY and stored next to the
state on the jobagent-data branch; ``jobagent materials open`` unpacks it.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from .store import Job, State

KEEP = (".pdf", ".md", ".jpg", ".png", ".txt")
MAX_BYTES = 40 * 1024 * 1024


def _folder_name(job: Job) -> str:
    name = f"{job.company} - {job.title}"
    return re.sub(r"[^\w\-. ]+", "_", name)[:90].strip()


def answers_markdown(job: Job, reason: str = "") -> str:
    lines = [f"# {job.company} - {job.title}", "", f"Apply here: {job.url}", ""]
    if job.score is not None:
        lines += [f"Match score: {job.score}", ""]
    if reason:
        lines += [f"Why it needs you: {reason}", ""]
    app = job.application or {}
    answers = app.get("answers") or {}
    if answers:
        lines += ["## Prepared answers", ""]
        for q, a in answers.items():
            lines += [f"**{q}**", "", str(a), ""]
    return "\n".join(lines)


def pending_manual(state: State) -> list[Job]:
    return sorted((j for j in state.jobs.values() if j.status == "manual"), key=lambda j: -(j.updated_at or 0))


def build_zip(state: State) -> tuple[bytes, int]:
    from .apply import work_dir

    buf = io.BytesIO()
    count = 0
    total = 0
    manifest: dict[str, str] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for job in pending_manual(state):
            folder = _folder_name(job)
            if folder in manifest:  # same company + title twice
                folder = f"{folder} ({len(manifest)})"
            manifest[folder] = job.id
            reason = next((e.detail for e in reversed(state.events) if e.job_id == job.id and e.type == "manual"), "")
            zf.writestr(f"{folder}/answers.md", answers_markdown(job, reason))
            wd = work_dir(job)
            for f in sorted(wd.iterdir()) if wd.exists() else []:
                if f.suffix.lower() not in KEEP or f.name == "description.txt":
                    continue
                size = f.stat().st_size
                if total + size > MAX_BYTES:
                    break
                zf.write(f, f"{folder}/{f.name}")
                total += size
            count += 1
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return buf.getvalue(), count


def export(state: State, path: Path) -> dict[str, Any]:
    from .store import _fernet

    data, count = build_zip(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_fernet().encrypt(data))
    return {"jobs": count, "bytes": len(data), "file": str(path)}


def unpack(path: Path, out: Path) -> dict[str, Any]:
    from .store import _fernet

    data = _fernet().decrypt(path.read_bytes())
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.startswith("/") or ".." in Path(name).parts or name == "manifest.json":
                continue  # never write outside the target folder
            target = out / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))
        names = sorted({n.split("/")[0] for n in zf.namelist() if "/" in n})
    return {"folder": str(out), "jobs": names}


def restore(state: State, path: Path) -> dict[str, Any]:
    """Put files from an earlier bundle back into each job's work folder, so older
    apply-by-hand jobs keep their materials across runs on throwaway CI machines."""
    from .apply import work_dir
    from .store import _fernet

    if not path.exists():
        return {"restored": 0}
    with zipfile.ZipFile(io.BytesIO(_fernet().decrypt(path.read_bytes()))) as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError:
            return {"restored": 0}
        restored = 0
        for name in zf.namelist():
            folder, _, filename = name.partition("/")
            job_id = manifest.get(folder)
            if not filename or "/" in filename or job_id not in state.jobs or filename == "answers.md":
                continue
            target = work_dir(state.jobs[job_id]) / filename
            if not target.exists():
                target.write_bytes(zf.read(name))
                restored += 1
    return {"restored": restored}


def resumes_for_email(state: State, since: float, limit: int = 12) -> list[tuple[Path, str]]:
    """(file, attachment name) for tailored resumes of jobs that became manual since `since`."""
    from .apply import work_dir

    out: list[tuple[Path, str]] = []
    for job in pending_manual(state):
        if (job.updated_at or 0) < since:
            continue
        for f in work_dir(job).glob("*_Resume.pdf"):
            out.append((f, f"{_folder_name(job)} - {f.name}"))
        if len(out) >= limit:
            break
    return out[:limit]


def summary(state: State) -> list[dict[str, Any]]:
    return [{"id": j.id, "company": j.company, "title": j.title, "url": j.url} for j in pending_manual(state)]


__all__ = ["export", "unpack", "pending_manual", "resumes_for_email", "answers_markdown", "summary", "json"]
