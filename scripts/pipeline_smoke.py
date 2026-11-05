"""Developer check: run the whole tool chain on live postings with the fictional example profile.

discover -> pick a few Greenhouse/Lever/Ashby candidates -> description ->
score -> tailored resume -> prepare -> submit --dry-run (never submits) -> report.
Required questions the profile cannot answer get placeholder answers, as a
stand-in for what Claude would write.

    python scripts/pipeline_smoke.py <ignored> out_dir
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> tuple[int, str, str]:
    p = subprocess.run([sys.executable, "-m", "job_agent", *args], capture_output=True, text=True, cwd=ROOT)
    return p.returncode, p.stdout, p.stderr


def placeholder(f: dict):
    kind = f["kind"]
    opts = [o for o in (f.get("options") or []) if not o.startswith("... ")]
    if kind in ("select", "combobox", "radio", "button_group"):
        return opts[0] if opts else None
    if kind == "checkbox_group":
        return opts[:1]
    if kind == "checkbox":
        return True
    if kind == "number":
        return "1"
    if kind == "file":
        return "resume"
    return "Placeholder answer for a dry run"


def main() -> None:
    out_root = Path(sys.argv[2])
    out_root.mkdir(parents=True, exist_ok=True)
    os.environ["JOBAGENT_HOME"] = str(out_root / "home")
    os.environ["JOBAGENT_PROFILE"] = str(ROOT / "examples" / "profile.example.yaml")
    log: dict = {}

    code, stdout, stderr = run("discover")
    disc = json.loads(stdout)
    log["discover"] = {k: v for k, v in disc.items() if k != "candidates"}
    log["n_candidates"] = len(disc.get("candidates", []))
    picked, per_ats = [], {}
    for c in disc.get("candidates", []):
        if c["auto_apply"] and per_ats.get(c["ats"], 0) < 3:
            per_ats[c["ats"]] = per_ats.get(c["ats"], 0) + 1
            picked.append(c)
    results = []
    for c in picked:
        jid = c["id"]
        r = {"id": jid, "company": c["company"], "title": c["title"]}
        code, jd, err = run("jd", jid, "--max-chars", "400")
        r["jd_ok"] = code == 0 and len(jd) > 100
        run("score", jid, "85", "--reason", "smoke test", "--decision", "apply")
        code, stdout, err = run("resume", jid)
        r["resume"] = json.loads(stdout) if code == 0 else err[-300:]
        code, stdout, err = run("prepare", jid)
        if code != 0:
            r["prepare_error"] = err[-500:]
            results.append(r)
            continue
        prep = json.loads(stdout)
        r["n_fields"] = prep["n_fields"]
        r["ai_policy"] = prep["ai_policy_mentioned"]
        r["captcha"] = prep["captcha"]
        answers = {}
        for f in prep["fields"]:
            if f.get("required") and "suggested" not in f:
                v = placeholder(f)
                if v is not None:
                    answers[f["key"]] = v
        r["placeholders"] = len(answers)
        path = Path(prep["answers_file_hint"])
        path.write_text(json.dumps({"answers": answers}))
        code, stdout, err = run("submit", jid, "--answers", str(path), "--dry-run")
        r["submit"] = json.loads(stdout) if code == 0 else err[-500:]
        results.append(r)
        print(json.dumps(r)[:700], flush=True)
    log["results"] = results
    code, report, err = run("report", "--detail", "full")
    (out_root / "report.md").write_text(report)
    (out_root / "summary.json").write_text(json.dumps(log, indent=1))
    ok = sum(1 for r in results if isinstance(r.get("submit"), dict) and r["submit"].get("status") == "dry_run_ok")
    print(f"dry_run_ok: {ok}/{len(results)}")


if __name__ == "__main__":
    main()
