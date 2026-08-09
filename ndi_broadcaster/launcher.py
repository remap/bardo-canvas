from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import platform
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
from playwright.async_api import async_playwright

from layout_server.audio import discover_audio_devices, load_audio_config

from .audio_capture import AudioSender, resolve_input_device
from .capture_cdp import decode_captured_frame
from .config import BroadcasterConfig, load_broadcaster_config
from .ndi_sender import VideoSender

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LauncherPaths:
    broadcaster_yaml: Path
    audio_yaml: Path


def resolve_launcher_paths(env: dict[str, str]) -> LauncherPaths:
    """Resolve this process's config paths, anchored to REPO_ROOT by default.

    Anchoring to REPO_ROOT (rather than a bare relative path) matches
    layout_server/main.py's resolve_settings() -- it means these defaults no
    longer depend on run.sh's `cd` into the repo root happening first. AUDIO_YAML
    reuses the exact env var name layout_server/main.py already reads for the
    same file: server and broadcaster must agree on one instance's audio device
    pair, so they share one name rather than each having their own.
    """
    return LauncherPaths(
        broadcaster_yaml=Path(
            env.get("BROADCASTER_YAML", str(REPO_ROOT / "config" / "broadcaster.yaml"))
        ),
        audio_yaml=Path(env.get("AUDIO_YAML", str(REPO_ROOT / "config" / "audio.yaml"))),
    )


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
    decodes_since_log = 0
    decode_seconds_since_log = 0.0
    sends_since_log = 0
    send_seconds_since_log = 0.0
    last_log = time.monotonic()
    while not stop_event.is_set():
        data = frame_slot.take()
        if data is not None:
            decode_start = time.monotonic()
            try:
                last_frame = decode_captured_frame(
                    data, target_width=config.width, target_height=config.height
                )
            except Exception:
                logger.exception("Failed to decode a captured frame; skipping it")
            decodes_since_log += 1
            decode_seconds_since_log += time.monotonic() - decode_start
        if last_frame is not None:
            send_start = time.monotonic()
            try:
                sender.send(last_frame)
            except Exception:
                logger.exception("Failed to send a frame; skipping it")
            sends_since_log += 1
            send_seconds_since_log += time.monotonic() - send_start
        now = time.monotonic()
        if now - last_log >= 5.0:
            decode_avg = (decode_seconds_since_log / decodes_since_log * 1000) if decodes_since_log else 0.0
            send_avg = (send_seconds_since_log / sends_since_log * 1000) if sends_since_log else 0.0
            logger.info(
                "NDI sender: %d decodes (%.1fms avg), %d sends (%.1fms avg) in the last %.1fs",
                decodes_since_log,
                decode_avg,
                sends_since_log,
                send_avg,
                now - last_log,
            )
            decodes_since_log = 0
            decode_seconds_since_log = 0.0
            sends_since_log = 0
            send_seconds_since_log = 0.0
            last_log = now
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
            # Headless has no physical monitor to be smaller than this -- unlike the
            # old headed kiosk window, Playwright's requested viewport is authoritative
            # here, so #layout-driver-root's rescale() always resolves to scale=1 and
            # root exactly fills the viewport. That's what makes capturing the whole
            # viewport (via screencast, below) equivalent to capturing root directly.
            viewport={"width": config.width, "height": config.height},
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

        # Every CDP screenshot/screencast API tried here -- Page.captureScreenshot
        # (clipped to #layout-driver-root, and over the whole viewport),
        # Page.startScreencast, HeadlessExperimental.beginFrame (removed in this
        # Chromium version's full-browser binary; hangs indefinitely via the
        # headless-shell binary) -- was independently confirmed live to return solid
        # black for canvases that unambiguously have real content: a canvas's own
        # ctx.getImageData() showed correct pixels (a pushed solid-color test image) at
        # the exact coordinates a same-instant CDP screenshot showed (0, 0, 0),
        # reproduced with and without GPU acceleration and across both Chromium
        # binaries Playwright can select. This matches a long-documented class of
        # Chromium/Puppeteer/Playwright bug (e.g. puppeteer/puppeteer#5352): canvas
        # content that's valid and readable from the page's own JS does not reliably
        # reach Chromium's own viewport-level screenshot/screencast capture pipeline.
        #
        # window.__ndiCaptureDataURL() (static/layout-driver.js) sidesteps every CDP
        # screenshot API entirely: it composites the same canvases with plain
        # ctx.drawImage() and reads the result back with the *canvas's own*
        # toDataURL() -- proven reliable throughout that investigation, for both this
        # app's fetch-and-draw-once canvases and noraebang's continuously-animating
        # ones. page.evaluate() runs this over the same CDP connection Playwright
        # already holds to drive this browser -- there is no HTTP server, no network
        # round trip, and no second browser tab involved; it's a direct call into the
        # one page this loop already controls, the same mechanism Playwright uses for
        # every other page.evaluate() call in this codebase and its tests.
        frame_interval = 1.0 / config.fps
        next_deadline = time.monotonic()

        captures_since_log = 0
        capture_seconds_since_log = 0.0
        last_fps_log = time.monotonic()

        try:
            while not stop_event.is_set():
                capture_start = time.monotonic()
                try:
                    data_url = await page.evaluate(
                        "window.__ndiCaptureDataURL && window.__ndiCaptureDataURL()"
                    )
                    if data_url:
                        _, _, b64_data = data_url.partition(",")
                        frame_slot.put(base64.b64decode(b64_data))
                except Exception:
                    logger.exception("Failed to capture the wall for NDI; will retry")

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
    config_path: str | None = None,
    audio_config_path: str | None = None,
) -> None:
    env = dict(os.environ)
    paths = resolve_launcher_paths(env)
    if config_path is None:
        config_path = str(paths.broadcaster_yaml)
    if audio_config_path is None:
        audio_config_path = str(paths.audio_yaml)

    config = load_broadcaster_config(Path(config_path))
    config = resolve_target_url(config, env)
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


def _log_format(env: dict[str, str]) -> str:
    """A port prefix distinguishes one instance's log lines from another's when
    multiple instances run in the same terminal/log aggregator. Omitted when
    LAYOUT_DRIVER_PORT isn't set, so output outside run.sh is unchanged.
    """
    port = env.get("LAYOUT_DRIVER_PORT")
    prefix = f"[:{port}] " if port else ""
    return f"%(asctime)s {prefix}%(levelname)s %(name)s: %(message)s"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format=_log_format(dict(os.environ)))
    run()
