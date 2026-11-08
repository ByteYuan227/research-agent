"""Documents you supply for applications that ask for them (for now: an unofficial transcript).

They live in ~/.jobagent/documents and, for the cloud run, in documents.enc on the
jobagent-data branch, encrypted with JOBAGENT_STATE_KEY like the rest of the state.
They are uploaded only to a form field that asks for that document and is required.
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

from .profile import jobagent_home
from .store import seal, unseal

KINDS = ("transcript",)
SUFFIXES = (".pdf",)


def doc_dir() -> Path:
    return jobagent_home() / "documents"


def path(kind: str) -> Path | None:
    for suffix in SUFFIXES:
        p = doc_dir() / f"{kind}{suffix}"
        if p.exists():
            return p
    return None


def available() -> dict[str, Path]:
    return {k: p for k in KINDS if (p := path(k)) is not None}


def add(kind: str, src: Path) -> Path:
    if kind not in KINDS:
        raise ValueError(f"unknown document {kind!r} (known: {', '.join(KINDS)})")
    if src.suffix.lower() not in SUFFIXES:
        raise ValueError("only PDF documents are supported")
    doc_dir().mkdir(parents=True, exist_ok=True)
    dst = doc_dir() / f"{kind}{src.suffix.lower()}"
    dst.write_bytes(src.read_bytes())
    os.chmod(dst, 0o600)
    return dst


def export(out: Path, key: str | None = None) -> dict[str, int]:
    """Encrypt every document into one file. Nothing is written when there are none."""
    docs = available()
    if not docs:
        return {"documents": 0}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in docs.values():
            tar.add(p, arcname=p.name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(seal(buf.getvalue(), key))
    return {"documents": len(docs)}


def restore(src: Path, key: str | None = None) -> dict[str, int]:
    data = unseal(src.read_bytes(), key)
    doc_dir().mkdir(parents=True, exist_ok=True)
    n = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = Path(member.name).name  # flat names only: never write outside the folder
            stem, suffix = Path(name).stem, Path(name).suffix.lower()
            if not member.isfile() or stem not in KINDS or suffix not in SUFFIXES:
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            dst = doc_dir() / name
            dst.write_bytes(f.read())
            os.chmod(dst, 0o600)
            n += 1
    return {"documents": n}
