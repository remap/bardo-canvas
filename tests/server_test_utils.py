"""Shared helper for tests that need a real, running layout_server instance
(as opposed to FastAPI's in-process TestClient) -- e.g. for Playwright-based
tests that need actual HTTP/WebSocket connections. Used by both this repo's
own tests and individual apps' test suites (apps/*/tests/), which is why
this lives at the repo root rather than under a single app.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_layout_driver_server(app_dir: Path, tmp_path: Path, env: dict[str, str]):
    port = _find_free_port()
    process_env = {
        **env,
        "APP_DIR": str(app_dir),
        "LAYOUT_DRIVER_HOST": "127.0.0.1",
        "LAYOUT_DRIVER_PORT": str(port),
        "LAYOUT_DRIVER_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "layout_server.main"],
        cwd=str(REPO_ROOT),
        env=process_env,
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
