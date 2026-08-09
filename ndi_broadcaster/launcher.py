from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import threading
import time
from pathlib import Path

import httpx
import numpy as np
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


class _LatestFrameSlot:
    """A single-slot, thread-safe handoff of the most recent captured frame.

    Deliberately not a queue: a backlog of stale frames is worthless for a live
    wall, so a new frame simply replaces any un-taken predecessor.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: str | None = None

    def put(self, data: str) -> None:
        with self._lock:
            self._data = data

    def take(self) -> str | None:
        with self._lock:
            data, self._data = self._data, None
            return data


def _sender_thread_loop(
    frame_slot: _LatestFrameSlot,
    sender: VideoSender,
    config: BroadcasterConfig,
    stop_event: threading.Event,
) -> None:
    """Decode and send frames off the event loop, at a steady clock.

    CDP only emits screencast frames on compositor updates, so a static wall would
    otherwise stop producing NDI output entirely. Re-sending the last decoded frame
    holds the configured frame rate regardless of page activity.
    """
    frame_interval = 1.0 / config.fps
    last_frame: np.ndarray | None = None
    next_deadline = time.monotonic()
    while not stop_event.is_set():
        data = frame_slot.take()
        if data is not None:
            try:
                last_frame = decode_screencast_frame(
                    data, target_width=config.width, target_height=config.height
                )
            except Exception:
                logger.exception("Failed to decode a captured frame; skipping it")
        if last_frame is not None:
            try:
                sender.send(last_frame)
            except Exception:
                logger.exception("Failed to send a frame; skipping it")
        next_deadline += frame_interval
        # Sleep only the time still owed on this frame's budget: sender.send() already
        # self-clocks, so a flat sleep of frame_interval would halve the output rate.
        remaining = next_deadline - time.monotonic()
        if remaining > 0:
            stop_event.wait(remaining)
        else:
            next_deadline = time.monotonic()


def resolve_target_url(config: BroadcasterConfig, env: dict[str, str]) -> BroadcasterConfig:
    """Apply the LAYOUT_DRIVER_TARGET_URL override, if set.

    run.sh derives this from the host/port the server actually started on, so a
    LAYOUT_DRIVER_PORT override reaches the broadcaster instead of leaving it
    pointed at the hardcoded target_url in config/broadcaster.yaml.
    """
    override = env.get("LAYOUT_DRIVER_TARGET_URL")
    if not override:
        return config
    return config.model_copy(update={"target_url": override})


def _chrome_launch_args() -> list[str]:
    """Kiosk for a chrome-free full-screen wall; autoplay-policy so app audio
    (noraebang's track) starts without a user gesture there is nobody to make."""
    return ["--kiosk", "--autoplay-policy=no-user-gesture-required"]


SCREENSHOT_POLL_INTERVAL_SECONDS = 1.0


async def _capture_loop(
    config: BroadcasterConfig, sender: VideoSender, stop_event: threading.Event
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False, args=_chrome_launch_args())
        context = await browser.new_context(
            viewport={"width": config.width, "height": config.height},
            ignore_https_errors=True,
            permissions=["microphone"],
        )
        page = await context.new_page()
        await page.goto(config.target_url)

        frame_slot = _LatestFrameSlot()

        sender_thread = threading.Thread(
            target=_sender_thread_loop,
            args=(frame_slot, sender, config, stop_event),
            daemon=True,
        )
        sender_thread.start()

        # The visible kiosk window's CSS layout (buildRoot()'s absolute-positioned
        # containers under a transform: scale() root, sized to fit whatever the real
        # window/viewport happens to be) is a completely separate layout computation
        # from /api/screenshot's compositor (computeCompositePlacements(), drawing
        # straight onto a fixed 3840x2160 offscreen canvas). They only agree when the
        # real viewport's aspect ratio exactly matches the canvas's -- on a display
        # that doesn't (observed live: a 3456x2234 laptop screen against a 3840x2160
        # canvas), the CSS-scaled/letterboxed window content, once stretched back up
        # to the configured capture resolution, comes out with screens misplaced and
        # wrongly sized. Rather than capturing that CSS-dependent rendering, pull
        # frames from the same compositor /api/screenshot already uses -- same engine,
        # a second output -- so there is only one source of truth for screen layout.
        screenshot_url = f"{config.target_url.rstrip('/')}/api/screenshot"
        try:
            async with httpx.AsyncClient(verify=False, timeout=5.0) as http_client:
                while not stop_event.is_set():
                    try:
                        response = await http_client.post(screenshot_url)
                        response.raise_for_status()
                        frame_slot.put(base64.b64encode(response.content).decode("ascii"))
                    except Exception:
                        logger.exception("Failed to pull a wall screenshot for NDI; will retry")
                    await asyncio.sleep(SCREENSHOT_POLL_INTERVAL_SECONDS)
        finally:
            stop_event.set()
            try:
                sender_thread.join(timeout=5.0)
            finally:
                await browser.close()


def run(
    config_path: str = "config/broadcaster.yaml",
    audio_config_path: str = "config/audio.yaml",
) -> None:
    config = load_broadcaster_config(Path(config_path))
    config = resolve_target_url(config, dict(os.environ))
    if config.capture_backend == "sck":
        raise NotImplementedError(
            "The 'sck' capture backend is not implemented yet; "
            "set capture_backend: cdp in config/broadcaster.yaml"
        )

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
            try:
                audio_sender = AudioSender(sender.sender, input_device)
                sender.open()
                audio_sender.start()
            except Exception:
                # A busy device, an unsupported sample rate, etc. must not take the
                # video stream down with it — degrade to video-only.
                logger.exception(
                    "Failed to start audio capture (%s); continuing with video only",
                    audio_config.input_device,
                )
                if audio_sender is not None:
                    with contextlib.suppress(Exception):
                        audio_sender.stop()
                    audio_sender = None
                if not sender.is_open:
                    sender.open()
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
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run()
