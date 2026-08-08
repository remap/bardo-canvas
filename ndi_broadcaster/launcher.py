from __future__ import annotations

import asyncio
import threading
import time

import httpx
from playwright.async_api import async_playwright

from .capture_cdp import decode_screencast_frame
from .config import BroadcasterConfig, load_broadcaster_config
from .ndi_sender import VideoSender


class HealthCheckTimeoutError(RuntimeError):
    pass


def wait_for_healthy(url: str, timeout_seconds: float, poll_interval_seconds: float = 0.5) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2.0, verify=False)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(poll_interval_seconds)
    raise HealthCheckTimeoutError(
        f"{url} did not become healthy within {timeout_seconds}s"
    ) from last_error


async def _capture_loop(
    config: BroadcasterConfig, sender: VideoSender, stop_event: threading.Event
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False, args=["--kiosk"])
        context = await browser.new_context(
            viewport={"width": config.width, "height": config.height},
            ignore_https_errors=True,
            permissions=["microphone"],
        )
        page = await context.new_page()
        await page.goto(config.target_url)

        client = await context.new_cdp_session(page)
        await client.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 80, "maxWidth": config.width, "maxHeight": config.height},
        )

        def on_frame(params: dict) -> None:
            frame = decode_screencast_frame(params["data"])
            sender.send(frame)
            asyncio.create_task(
                client.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            )

        client.on("Page.screencastFrame", on_frame)

        while not stop_event.is_set():
            await asyncio.sleep(0.1)

        await browser.close()


def run(config_path: str = "config/broadcaster.yaml") -> None:
    from pathlib import Path

    config = load_broadcaster_config(Path(config_path))
    wait_for_healthy(
        f"{config.target_url.rstrip('/')}/healthz", timeout_seconds=config.healthz_timeout_seconds
    )

    sender = VideoSender(config.ndi_source_name, config.width, config.height, config.fps)
    stop_event = threading.Event()
    try:
        asyncio.run(_capture_loop(config, sender, stop_event))
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        sender.close()


if __name__ == "__main__":
    run()
