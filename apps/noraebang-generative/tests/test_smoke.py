from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from tests.server_test_utils import running_layout_driver_server

APP_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def running_server(tmp_path):
    with running_layout_driver_server(APP_ROOT / "static", tmp_path, dict(os.environ)) as url:
        yield url


def test_page_creates_six_canvases_with_no_console_errors_and_plays_audio(running_server):
    console_errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page.goto(running_server)
        page.wait_for_timeout(3000)

        canvases = page.query_selector_all("canvas")
        assert len(canvases) == 6

        audio_paused = page.evaluate("document.querySelector('audio').paused")
        assert audio_paused is False

        browser.close()

    assert console_errors == []


def test_screenshot_endpoint_returns_the_composited_wall(running_server):
    # flux-gallery's worker depends on /api/screenshot for its full-wall history
    # snapshots, so every app must call enableScreenshotResponder() -- this app
    # didn't, which only surfaced once something actually depended on it working.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(running_server)
        page.wait_for_timeout(3000)

        response = httpx.post(f"{running_server}api/screenshot", verify=False, timeout=5.0)

        browser.close()

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
