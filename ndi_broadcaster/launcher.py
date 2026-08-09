from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import threading
import time
from pathlib import Path

import httpx
import numpy as np
from playwright.async_api import async_playwright

from layout_server.audio import discover_audio_devices, load_audio_config

from .audio_capture import AudioSender, resolve_input_device
from .capture_cdp import decode_captured_frame
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
        self._data: bytes | None = None

    def put(self, data: bytes) -> None:
        with self._lock:
            self._data = data

    def take(self) -> bytes | None:
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

    The capture loop is throttled to config.fps and may occasionally fall behind (a
    slow encode, a retry after an error), so a static/delayed wall must not stop NDI
    output entirely. Re-sending the last decoded frame holds the configured frame
    rate regardless of capture-loop timing.
    """
    frame_interval = 1.0 / config.fps
    last_frame: np.ndarray | None = None
    next_deadline = time.monotonic()
    while not stop_event.is_set():
        data = frame_slot.take()
        if data is not None:
            try:
                last_frame = decode_captured_frame(
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
    """autoplay-policy so app audio (noraebang's track) starts without a user
    gesture there is nobody to make.

    On macOS, Playwright's bundled headless Chromium defaults to the SwiftShader
    software renderer (confirmed live via CDP SystemInfo.getInfo: gpu_compositing,
    rasterization, and 2d_canvas all "disabled_software"/"unavailable_software") --
    every p5.js canvas draw and every wall screenshot was being rasterized entirely
    on the CPU. --use-angle=metal switches to the real GPU via Apple's Metal API
    (confirmed live: same query afterwards reports "ANGLE Metal Renderer: Apple
    M4 Max" with 2d_canvas/gpu_compositing/rasterization all "enabled"), which is
    the majority of why NDI capture latency was inconsistent and the feed choppy.
    ANGLE's Metal backend is macOS-only; other platforms fall back to Chromium's
    own default backend selection rather than guessing a working flag blind.
    """
    args = ["--autoplay-policy=no-user-gesture-required"]
    if platform.system() == "Darwin":
        args.append("--use-angle=metal")
    return args


# Set by buildRoot() in static/layout-driver.js -- created by every app via
# initLayoutDriver(), so this works with no per-app opt-in required.
_LAYOUT_ROOT_SELECTOR = "#layout-driver-root"


async def _capture_loop(
    config: BroadcasterConfig, sender: VideoSender, stop_event: threading.Event
) -> None:
    async with async_playwright() as playwright:
        # Headless: nothing in this app (audio is a plain <audio> element, no DRM)
        # needs a real window, and capturing the wall via CDP screenshots of a
        # *visible* window at 30fps was hammering the compositor enough to cause
        # visible on-screen flashing -- confirmed live by isolating the same headed
        # launch with the capture loop removed entirely: no flashing without it.
        # Headless has no on-screen presentation to disrupt, so there's nothing to
        # flash; screenshot capture, content, and audio routing are all unaffected.
        browser = await playwright.chromium.launch(headless=True, args=_chrome_launch_args())
        context = await browser.new_context(
            viewport={"width": config.width, "height": config.height},
            # Pin this explicitly rather than rely on Playwright's (already 1.0)
            # default: an element screenshot's pixel dimensions are the element's CSS
            # size times this factor, and a silent mismatch here is exactly the kind of
            # display-dependent bug this capture path exists to avoid.
            device_scale_factor=1,
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

        # #layout-driver-root's CSS width/height are always the canvas's fixed
        # 3840x2160 -- its transform: scale() (buildRoot(), sized to fit whatever the
        # real window/display happens to be) never touches those, only how it's
        # painted. A uniform scale on both dimensions can't change the aspect ratio,
        # so root's rendered bounding box always keeps the exact canvas aspect ratio
        # regardless of window/display size -- unlike capturing the whole viewport,
        # which includes whatever letterboxing/margin surrounds root and previously
        # caused screens to come out misplaced and wrongly sized on a display whose
        # aspect ratio doesn't match the canvas's (observed live: a 3456x2234 laptop
        # screen against the 3840x2160 canvas). Screenshotting root directly, at
        # device_scale_factor=1, gives back exactly that aspect ratio at whatever
        # (smaller) resolution it's currently rendered at; resizing that up to the
        # configured capture resolution in decode_captured_frame is then a clean,
        # non-distorting upscale, not a stretch across mismatched aspect ratios.
        # locator.screenshot() runs Playwright's full actionability pipeline first --
        # waiting for the element to be "stable" (not moving/resizing) and scrolled
        # into view -- on every single call. Called back-to-back in a tight capture
        # loop, that repeated scroll/layout recalculation was visible as flashing in
        # the (now-removed) visible kiosk window. page.screenshot() with an explicit
        # clip rect is a single raw CDP capture with none of that.
        root_locator = page.locator(_LAYOUT_ROOT_SELECTOR)
        await root_locator.wait_for(state="attached", timeout=30_000)

        # bounding_box() is a lightweight getBoundingClientRect() query, but it's still
        # a full CDP round trip -- doing it on every frame doubled the number of CDP
        # calls per frame for no benefit, since headless has no physical window a human
        # could resize: once attached, root's box cannot change for the process's
        # lifetime. Cache it, and only re-derive it if a capture ever actually fails
        # (self-healing against the case where that assumption turns out to be wrong).
        box = await root_locator.bounding_box(timeout=5_000)
        if box is None:
            raise RuntimeError(f"{_LAYOUT_ROOT_SELECTOR} has no bounding box")

        # Paced to config.fps rather than looped as fast as each call resolves: an
        # unthrottled loop was issuing CDP screenshot captures far faster than the
        # video's actual frame rate, hammering the renderer -- observed live as visible
        # flashing in the (now-removed) visible kiosk window.
        frame_interval = 1.0 / config.fps
        next_deadline = time.monotonic()

        # Real achieved-rate visibility: the configured fps is a pacing ceiling, not a
        # guarantee -- a slow capture (encode cost, CDP round-trip latency) silently
        # produces a lower actual rate, which reads as choppy/stepped motion on the
        # output even though the sender thread keeps re-sending at a steady clock.
        captures_since_log = 0
        capture_seconds_since_log = 0.0
        last_fps_log = time.monotonic()

        try:
            while not stop_event.is_set():
                capture_start = time.monotonic()
                try:
                    screenshot_bytes = await page.screenshot(
                        clip=box, type="jpeg", quality=85, timeout=5_000
                    )
                    frame_slot.put(screenshot_bytes)
                except Exception:
                    logger.exception("Failed to capture the wall for NDI; re-deriving clip and retrying")
                    try:
                        box = await root_locator.bounding_box(timeout=5_000)
                        if box is None:
                            raise RuntimeError(f"{_LAYOUT_ROOT_SELECTOR} has no bounding box")
                    except Exception:
                        logger.exception("Failed to re-derive the capture clip; will keep retrying")

                captures_since_log += 1
                capture_seconds_since_log += time.monotonic() - capture_start
                now = time.monotonic()
                if now - last_fps_log >= 5.0:
                    logger.info(
                        "NDI capture: %.1f fps achieved (target %d), %.1fms avg capture latency",
                        captures_since_log / (now - last_fps_log),
                        config.fps,
                        (capture_seconds_since_log / captures_since_log) * 1000,
                    )
                    captures_since_log = 0
                    capture_seconds_since_log = 0.0
                    last_fps_log = now

                next_deadline += frame_interval
                remaining = next_deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                else:
                    next_deadline = time.monotonic()
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
