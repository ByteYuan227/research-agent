"""Developer check on live sites that need the step-by-step browser (Workday, iCIMS, TikTok, ...).

For a sample of current postings it (1) fetches the job description the way
`jobagent jd` does and (2) opens the posting with `jobagent web`, presses the
first "Apply" button and records what the next page asks for. It never types
anything into a site and runs in dry-run mode, so nothing can be submitted and
no account can be created.

    python scripts/web_smoke.py <ignored> out_dir
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["JOBAGENT_DRY_RUN"] = "1"

import httpx  # noqa: E402
import yaml  # noqa: E402

from job_agent import ats, sources, web  # noqa: E402
from job_agent.profile import Profile  # noqa: E402
from job_agent.store import State  # noqa: E402

WANT = {"workday": 5, "tiktok": 3, "smartrecruiters": 2, "workable": 2, "other": 6}


def main() -> None:
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    os.environ["JOBAGENT_HOME"] = str(out / "home")
    profile = Profile.model_validate(yaml.safe_load((ROOT / "examples" / "profile.example.yaml").read_text()))
    client = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": ats.USER_AGENT})
    jobs = sources.simplify_jobs("internship", client)
    random.seed(11)
    random.shuffle(jobs)
    recent = [j for j in jobs if j.posted_at and time.time() - j.posted_at < 20 * 86400]
    picked, count = [], {}
    for j in recent:
        if count.get(j.ats, 0) < WANT.get(j.ats, 0):
            count[j.ats] = count.get(j.ats, 0) + 1
            picked.append(j)
    results = []
    for job in picked:
        r = {"ats": job.ats, "company": job.company, "title": job.title, "url": job.url}
        try:
            text = ats.fetch_description(ats.AtsRef(job.ats, job.ats_company, job.ats_job_id, job.ats_region), job.url)
            r["jd_chars"] = len(text)
            r["jd_start"] = re.sub(r"\s+", " ", text[:240])
        except Exception as exc:  # noqa: BLE001
            r["jd_error"] = repr(exc)[:300]
        state = State()
        state.jobs[job.id] = job
        try:
            v = web.open_job(state, job, profile)
            r["first_page"] = {"title": v["title"], "fields": len(v["fields"]), "buttons": [b["text"] for b in v["buttons"][:25]]}
            apply_btn = next((b for b in v["buttons"] if re.fullmatch(r"(apply|apply now|apply for this job|i'?m interested)", b["text"], re.I)), None)
            if apply_btn:
                v = web.click(state, profile, apply_btn["ref"])
                r["after_apply"] = {
                    "url": v["url"][:160], "title": v["title"], "headings": v["headings"][:6],
                    "fields": [{k: f.get(k) for k in ("kind", "label", "required")} for f in v["fields"][:30]],
                    "buttons": [b["text"] for b in v["buttons"][:30]], "captcha": v["captcha_challenge"],
                }
                manual = next((b for b in v["buttons"] if re.search(r"apply manually|manual", b["text"], re.I)), None)
                if manual:
                    v = web.click(state, profile, manual["ref"])
                    r["after_manual"] = {
                        "url": v["url"][:160], "headings": v["headings"][:6],
                        "fields": [{k: f.get(k) for k in ("kind", "label", "required", "suggested")} for f in v["fields"][:30]],
                        "buttons": [b["text"] for b in v["buttons"][:30]],
                    }
            web.screenshot(state, profile, "smoke")
        except Exception:  # noqa: BLE001
            r["web_error"] = traceback.format_exc()[-1200:]
        finally:
            web.stop()
        print(json.dumps({k: r.get(k) for k in ("ats", "company", "jd_chars", "jd_error")}), flush=True)
        results.append(r)
    (out / "summary.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
