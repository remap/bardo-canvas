from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def running_server(tmp_path):
    # flux-gallery is the only app currently using enableImageMode() (per-screen
    # canvases positioned via CSS, the code path the position:relative bug lived
    # in) -- noraebang-generative renders directly via p5.js and never exercises
    # this positioning logic at all.
    port = _find_free_port()
    env = {
        **os.environ,
        "APP_DIR": str(REPO_ROOT / "apps" / "flux-gallery" / "static"),
        "LAYOUT_DRIVER_HOST": "127.0.0.1",
        "LAYOUT_DRIVER_PORT": str(port),
        "LAYOUT_DRIVER_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "layout_server.main"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        url = f"https://127.0.0.1:{port}/healthz"
        deadline = time.monotonic() + 15
        healthy = False
        while time.monotonic() < deadline:
            try:
                if httpx.get(url, verify=False, timeout=1.0).status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        if not healthy:
            raise RuntimeError("server did not become healthy in time")
        yield f"https://127.0.0.1:{port}/"
    finally:
        process.terminate()
        process.wait(timeout=5)


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
