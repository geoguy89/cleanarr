"""Take the README screenshots from the demo library in tests/demo.py.

Nothing here is a real library: the shows, films and servers are made up.

Needs ffmpeg and Playwright with Chromium.

Run: python tools/screenshots.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CLEANARR_WEB", str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "server"))

from demo import Demo  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = ROOT / "docs" / "images"
WIDE = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}


def shot(page, name: str, **kw) -> None:
    page.wait_for_timeout(900)
    page.screenshot(path=str(OUT / f"{name}.jpg"), type="jpeg", quality=82, **kw)
    print(f"  {name}.jpg")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    demo = Demo()
    url = demo.up()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport=WIDE, color_scheme="dark")
            page = ctx.new_page()

            def go(view):
                page.goto(f"{url}/#/{view}")
                page.wait_for_load_state("networkidle")

            go("home")
            shot(page, "home")
            go("shows")
            shot(page, "shows")
            page.locator("#view-shows button.open", has_text="The Quiet Harbour").click()
            page.locator(".season-toggle").first.click()
            shot(page, "episodes")
            page.keyboard.press("Escape")
            go("cleaned")
            shot(page, "cleaned")
            page.locator("#view-cleaned .row", has_text="S01E05").locator("[data-action=open-job]").click()
            page.locator(".detection").first.wait_for()
            page.locator(".detection", has_text="Dick,").locator("summary").click()
            shot(page, "details")
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")
            go("queue")
            shot(page, "queue")
            go("upcoming")
            shot(page, "upcoming")
            go("settings/words")
            page.locator("#s-words").screenshot(path=str(OUT / "settings.jpg"), type="jpeg", quality=82)
            print("  settings.jpg")
            ctx.close()

            ctx = browser.new_context(viewport=PHONE, color_scheme="dark", device_scale_factor=2)
            page = ctx.new_page()
            go("home")
            shot(page, "phone")
            ctx.close()
            browser.close()
    finally:
        demo.stop()


if __name__ == "__main__":
    main()
