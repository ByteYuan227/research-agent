"""Launching Chromium consistently (CI, laptops, sandboxes)."""

from __future__ import annotations

import os
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def launch(pw: Any, headless: bool | None = None):
    """Launch Chromium. JOBAGENT_CHROMIUM may point at a specific browser binary."""
    kwargs: dict[str, Any] = {}
    exe = os.environ.get("JOBAGENT_CHROMIUM")
    if exe:
        kwargs["executable_path"] = exe
        kwargs["args"] = ["--no-sandbox"]
    if headless is None:
        headless = os.environ.get("JOBAGENT_HEADFUL", "") != "1"
    return pw.chromium.launch(headless=headless, **kwargs)


def new_context(browser: Any):
    return browser.new_context(
        viewport={"width": 1280, "height": 1600},
        locale="en-US",
        timezone_id="America/New_York",
        user_agent=USER_AGENT,
    )
