from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from tests.server_test_utils import running_layout_driver_server

APP_ROOT = Path(__file__).resolve().parent.parent

# id -> the single-letter heading each screen's HTML file renders, and the
# background color set in its <style> block (see static/{id}.html).
EXPECTED_SCREENS = {
    "F": "#2e6ea6",
    "B": "#3f8f4f",
    "C": "#a6572e",
    "D": "#8a3f9f",
    "A": "#c9a227",
    "E": "#2e8f8f",
}


@pytest.fixture
def running_server(tmp_path):
    with running_layout_driver_server(APP_ROOT / "static", tmp_path, dict(os.environ)) as url:
        yield url


def _hex_to_rgb(value: str) -> str:
    value = value.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgb({r}, {g}, {b})"


def test_every_screen_loads_its_own_html_file_at_the_right_position(running_server):
    # This app uses iframes (enableStaticPageMode), not canvases -- verify
    # against the real rendered page, not compositeToCanvas()/__ndiCaptureDataURL(),
    # which can't see iframe content at all (see layout-driver.js's
    # enableStaticPageMode docstring and this app's README).
    console_errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True, no_viewport=True)
        page = context.new_page()
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page.set_viewport_size({"width": 3840, "height": 2160})
        page.goto(running_server, timeout=15000)
        page.wait_for_timeout(2000)

        placements = {p["id"]: p for p in page.evaluate("window.LayoutDriver.measureScreenPlacements()")}
        screens = page.evaluate("window.LayoutDriver.layoutConfig.screens")

        for screen in screens:
            screen_id = screen["id"]
            rect = screen["rect"]
            placement = placements[screen_id]
            assert placement["dx"] == pytest.approx(rect["x"], abs=1)
            assert placement["dy"] == pytest.approx(rect["y"], abs=1)
            assert placement["dWidth"] == pytest.approx(rect["width"], abs=1)
            assert placement["dHeight"] == pytest.approx(rect["height"], abs=1)

            frame = page.frame_locator(f'iframe[src="{screen_id}.html"]')
            assert frame.locator("h1").inner_text() == screen_id
            bg_color = frame.locator("body").evaluate("el => getComputedStyle(el).backgroundColor")
            assert bg_color == _hex_to_rgb(EXPECTED_SCREENS[screen_id])

        browser.close()

    assert console_errors == []


def test_editing_a_screen_file_reloads_the_page_automatically(running_server):
    # enableAutoReload() (layout-driver.js) + the server's file watcher
    # (layout_server/file_watcher.py) together are what makes "edit a file,
    # see it moments later" true without a manual restart -- the whole point
    # of this app for a non-coder. A real page reload resets all JS state,
    # so a marker set before the edit disappearing (rather than some
    # in-place DOM patch) is what actually proves a reload happened, not
    # just that the iframe's own content changed.
    f_html = APP_ROOT / "static" / "F.html"
    original_content = f_html.read_text()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True, no_viewport=True)
            page = context.new_page()
            page.set_viewport_size({"width": 3840, "height": 2160})
            page.goto(running_server, timeout=15000)
            page.wait_for_timeout(1000)

            page.evaluate("window.__reloadMarker = true")
            assert page.evaluate("window.__reloadMarker") is True

            f_html.write_text(original_content.replace(">F<", ">F-edited<"))
            page.wait_for_timeout(1500)

            assert page.evaluate("window.__reloadMarker") is None
            frame = page.frame_locator('iframe[src="F.html"]')
            assert frame.locator("h1").inner_text() == "F-edited"

            browser.close()
    finally:
        f_html.write_text(original_content)
