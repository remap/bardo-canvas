from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

APP_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_ROOT.parent.parent


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def running_server(tmp_path):
    port = _find_free_port()
    env = {
        **os.environ,
        "APP_DIR": str(APP_ROOT / "static"),
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
    # The broadcaster's NDI capture pipeline polls /api/screenshot for every app it
    # drives, so every app must call enableScreenshotResponder() -- this app didn't,
    # which only surfaced once something actually depended on the endpoint working.
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
