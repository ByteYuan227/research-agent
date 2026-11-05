"""Developer probe: open real application forms and record their structure.

Never submits anything. For each URL it saves the ATS API response, the fields
the generic extractor finds, the options of every dropdown, the form HTML and a
screenshot, so the form filler can be developed against real markup.

    python scripts/probe_forms.py urls.json out_dir
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_agent import ats  # noqa: E402

EXTRACT_JS = (Path(__file__).resolve().parents[1] / "job_agent" / "browser" / "extract.js").read_text()


def slug(url: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", url.split("//", 1)[-1])[:90]


def probe(page, url: str, out: Path) -> dict:
    ref = ats.parse_ats(url)
    info: dict = {"url": url, "ref": ref.__dict__, "apply_url": ats.application_url(ref, url)}
    try:
        if ref.ats == "greenhouse" and ref.company and ref.job_id:
            (out / "api.json").write_text(json.dumps(ats.greenhouse_job(ref.company, ref.job_id, ref.region), indent=1))
        elif ref.ats == "lever":
            (out / "api.json").write_text(json.dumps(ats.lever_job(ref.company, ref.job_id, ref.region), indent=1))
        elif ref.ats == "ashby":
            (out / "api.json").write_text(json.dumps(ats.ashby_job(ref.company, ref.job_id), indent=1))
        desc = ats.fetch_description(ref, url)
        (out / "description.txt").write_text(desc)
        info["description_chars"] = len(desc)
    except Exception as exc:  # noqa: BLE001
        info["api_error"] = repr(exc)

    page.goto(info["apply_url"], wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(2000)
    info["final_url"] = page.url
    data = page.evaluate(EXTRACT_JS)
    # Open each combobox to learn how its options render.
    for f in data["fields"]:
        if f["kind"] != "combobox":
            continue
        try:
            el = page.locator(f'[data-ja-key="{f["key"]}"]')
            el.click(timeout=3000)
            page.wait_for_timeout(500)
            opts = page.locator("[role='option']").all_inner_texts()
            f["probed_options"] = opts[:40]
            page.keyboard.press("Escape")
        except Exception as exc:  # noqa: BLE001
            f["probe_error"] = repr(exc)[:200]
    (out / "fields.json").write_text(json.dumps(data, indent=1))
    form_html = page.evaluate(
        "() => { const f = document.querySelector('form') || document.querySelector('main') || document.body;"
        " return f.outerHTML.slice(0, 600000); }"
    )
    (out / "form.html").write_text(form_html)
    page.screenshot(path=str(out / "page.jpg"), full_page=True, type="jpeg", quality=40)
    info["n_fields"] = len(data["fields"])
    info["captcha"] = data["captcha"]
    return info


def main() -> None:
    urls = json.loads(Path(sys.argv[1]).read_text())
    out_root = Path(sys.argv[2])
    out_root.mkdir(parents=True, exist_ok=True)
    summary = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 1600}, locale="en-US")
        for url in urls:
            out = out_root / slug(url)
            out.mkdir(parents=True, exist_ok=True)
            page = context.new_page()
            try:
                info = probe(page, url, out)
            except Exception:  # noqa: BLE001
                info = {"url": url, "error": traceback.format_exc()[-2000:]}
            finally:
                page.close()
            print(json.dumps(info)[:400], flush=True)
            summary.append(info)
        browser.close()
    (out_root / "summary.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
