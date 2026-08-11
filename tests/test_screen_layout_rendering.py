from __future__ import annotations

import os

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from tests.server_test_utils import REPO_ROOT, running_layout_driver_server


@pytest.fixture
def running_server(tmp_path):
    # flux-gallery is the only app currently using enableImageMode() (per-screen
    # canvases positioned via CSS, the code path the position:relative bug lived
    # in) -- noraebang-generative renders directly via p5.js and never exercises
    # this positioning logic at all.
    app_dir = REPO_ROOT / "apps" / "flux-gallery" / "static"
    with running_layout_driver_server(app_dir, tmp_path, dict(os.environ)) as url:
        yield url


def test_every_screen_renders_at_its_configured_rect(running_server):
    # Live-observed bug: enableImageMode() set every screen container's CSS
    # position to "relative", overriding buildRoot()'s "absolute" -- this
    # broke real rendering for every screen but the first (position:relative's
    # top/left are an offset from normal document flow, not an absolute
    # coordinate), while container.style.top/left themselves still read back
    # as the correct configured values, and /api/screenshot's own compositing
    # used to trust those configured values directly rather than checking real
    # layout -- so nothing caught it. This asserts against actual rendered
    # position (getBoundingClientRect(), via driver.measureScreenPlacements(),
    # the same measurement /api/screenshot and the cdp capture path now both
    # use) for every screen, not just the first, and not just the CSS string.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True, no_viewport=True)
        page = context.new_page()
        page.set_viewport_size({"width": 3840, "height": 2160})
        page.goto(running_server)
        page.wait_for_timeout(2000)

        result = page.evaluate(
            """
            () => ({
                placements: window.LayoutDriver.measureScreenPlacements(),
                screens: window.LayoutDriver.layoutConfig.screens,
            })
            """
        )
        browser.close()

    placements_by_id = {p["id"]: p for p in result["placements"]}
    assert set(placements_by_id) == {s["id"] for s in result["screens"]}

    for screen in result["screens"]:
        placement = placements_by_id[screen["id"]]
        rect = screen["rect"]
        # Sub-pixel tolerance for getBoundingClientRect()'s floating point.
        assert placement["dx"] == pytest.approx(rect["x"], abs=1)
        assert placement["dy"] == pytest.approx(rect["y"], abs=1)
        assert placement["dWidth"] == pytest.approx(rect["width"], abs=1)
        assert placement["dHeight"] == pytest.approx(rect["height"], abs=1)
