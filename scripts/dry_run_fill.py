"""Developer check: fill real application forms with the example profile, WITHOUT submitting.

For each URL: open the form, gather dropdown options, compute the profile's
suggested answers, invent placeholder answers for the remaining required
questions, fill everything, and record which fields could not be filled.
The submit button is never clicked.

    python scripts/dry_run_fill.py urls.json out_dir
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from job_agent import ats  # noqa: E402
from job_agent.browser import forms  # noqa: E402
from job_agent.browser.launch import launch, new_context  # noqa: E402
from job_agent.profile import Profile  # noqa: E402
from job_agent.resume import build_resume  # noqa: E402

import yaml  # noqa: E402


def placeholder(f: dict):
    kind = f["kind"]
    opts = f.get("options") or []
    if kind in ("select", "combobox", "radio", "button_group"):
        return opts[0] if opts else None
    if kind == "checkbox_group":
        return opts[:1]
    if kind == "checkbox":
        return True
    if kind == "file":
        return None
    if kind == "date":
        return "2027-05-15"
    return "Placeholder answer for a dry run"


def main() -> None:
    urls = json.loads(Path(sys.argv[1]).read_text())
    out_root = Path(sys.argv[2])
    out_root.mkdir(parents=True, exist_ok=True)
    profile = Profile.model_validate(yaml.safe_load((ROOT / "examples" / "profile.example.yaml").read_text()))
    resume = build_resume(profile, None, out_root / "_resume")
    files = {"resume": resume}
    summary = []
    with sync_playwright() as pw:
        browser = launch(pw)
        context = new_context(browser)
        for url in urls:
            ref = ats.parse_ats(url)
            apply_url = ats.application_url(ref, url)
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", apply_url.split("//", 1)[-1])[:90]
            out = out_root / slug
            out.mkdir(parents=True, exist_ok=True)
            page = context.new_page()
            info = {"url": apply_url}
            try:
                data = forms.open_form(page, apply_url)
                answers = {}
                for f in data["fields"]:
                    if f["kind"] == "combobox" and not f.get("options"):
                        f["options"] = forms.combobox_options(page, f["key"])
                    s = forms.suggest(f, profile, "Example Co", files)
                    if s is not None:
                        f["suggested"] = s.value
                        answers[f["key"]] = s.value
                    elif f.get("required"):
                        v = placeholder(f)
                        if v is not None:
                            answers[f["key"]] = v
                report = forms.fill_all(page, data["fields"], answers)
                page.screenshot(path=str(out / "filled.jpg"), full_page=True, type="jpeg", quality=40)
                info.update(
                    n_fields=len(data["fields"]),
                    filled=len(report.filled),
                    errors=report.errors,
                    missing_required=report.missing_required,
                    suggested={f["key"]: str(f.get("suggested"))[:60] for f in data["fields"] if f.get("suggested") is not None},
                    combobox_options={f["key"]: f.get("options", [])[:8] for f in data["fields"] if f["kind"] == "combobox"},
                    errors_on_page=forms.visible_errors(page),
                )
                (out / "fields.json").write_text(json.dumps(data, indent=1))
            except Exception:  # noqa: BLE001
                info["error"] = traceback.format_exc()[-2500:]
            finally:
                page.close()
            print(json.dumps({k: info.get(k) for k in ("url", "n_fields", "filled", "errors", "missing_required", "error")})[:600], flush=True)
            summary.append(info)
        browser.close()
    (out_root / "summary.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
