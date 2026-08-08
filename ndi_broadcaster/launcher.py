from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from layout_server.audio import discover_audio_devices, load_audio_config

from .audio_capture import AudioSender, resolve_input_device
from .capture_cdp import decode_screencast_frame
from .config import BroadcasterConfig, load_broadcaster_config
from .ndi_sender import VideoSender

logger = logging.getLogger(__name__)


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
            try:
                frame = decode_screencast_frame(
                    params["data"], target_width=config.width, target_height=config.height
                )
                sender.send(frame)
            except Exception:
                logger.exception("Failed to decode/send a captured frame; skipping it")
            finally:
                asyncio.create_task(
                    client.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
                )

        client.on("Page.screencastFrame", on_frame)

        try:
            while not stop_event.is_set():
                await asyncio.sleep(0.1)
        finally:
            await browser.close()


def run(
    config_path: str = "config/broadcaster.yaml",
    audio_config_path: str = "config/audio.yaml",
) -> None:
    config = load_broadcaster_config(Path(config_path))
    wait_for_healthy(
        f"{config.target_url.rstrip('/')}/healthz", timeout_seconds=config.healthz_timeout_seconds
    )

    audio_config = load_audio_config(Path(audio_config_path))
    input_device = resolve_input_device(audio_config, discover_audio_devices().inputs)
    will_attach_audio = audio_config.enabled and input_device is not None

    sender = VideoSender(
        config.ndi_source_name,
        config.width,
        config.height,
        config.fps,
        open_immediately=not will_attach_audio,
    )

    audio_sender: AudioSender | None = None
    try:
        if will_attach_audio:
            audio_sender = AudioSender(sender.sender, input_device)
            sender.open()
            audio_sender.start()
        elif audio_config.enabled:
            print(
                f"Audio input device not found: {audio_config.input_device!r} — continuing without audio"
            )

        stop_event = threading.Event()
        try:
            asyncio.run(_capture_loop(config, sender, stop_event))
        except KeyboardInterrupt:
            stop_event.set()
    finally:
        if audio_sender is not None:
            audio_sender.stop()
        sender.close()


if __name__ == "__main__":
    run()
