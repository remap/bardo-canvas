from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import platform
import subprocess
import threading
import time
from collections.abc import Callable
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
from .timecode_overlay import TimecodeOverlay

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
    decode_fn: Callable[[bytes], np.ndarray] | None = None,
) -> None:
    """Decode and send frames off the event loop, at a steady clock.

    The capture loop is throttled to config.fps and may occasionally fall behind (a
    slow encode, a retry after an error), so a static/delayed wall must not stop NDI
    output entirely. Re-sending the last decoded frame holds the configured frame
    rate regardless of capture-loop timing.

    decode_fn defaults to the cdp path's JPEG/PNG decode; the sck path passes a
    lightweight raw-BGRA-to-RGBA reshape instead (see _decode_raw_rgba_frame),
    since its frames already arrive as tightly packed RGBA bytes with no
    compression to undo.
    """
    decode = decode_fn or (
        lambda data: decode_captured_frame(
            data, target_width=config.width, target_height=config.height
        )
    )
    # Constructed once per loop, not per frame: __init__ does the one-time
    # glyph rasterization; snapshot()/apply() are the only calls in the hot
    # path, and both are true no-ops when config.timecode_enabled is False.
    timecode_overlay = TimecodeOverlay(
        enabled=config.timecode_enabled,
        position=config.timecode_position,
        width=config.width,
        height=config.height,
        fps=config.fps,
    )
    frame_interval = 1.0 / config.fps
    last_frame: np.ndarray | None = None
    next_deadline = time.monotonic()
    decodes_since_log = 0
    decode_seconds_since_log = 0.0
    decode_failures_since_log = 0
    sends_since_log = 0
    send_seconds_since_log = 0.0
    send_failures_since_log = 0
    last_log = time.monotonic()
    while not stop_event.is_set():
        data = frame_slot.take()
        if data is not None:
            decode_start = time.monotonic()
            try:
                last_frame = decode(data)
                # snapshot() must run against a frame no overlay has ever
                # touched -- this is that moment. Only reached on a genuine
                # new decode, never on a repeated/stale send below.
                timecode_overlay.snapshot(last_frame)
                decodes_since_log += 1
                decode_seconds_since_log += time.monotonic() - decode_start
            except Exception:
                # Full traceback only for the first failure in a 5s window --
                # a persistent failure (e.g. SCK delivering a buffer of the
                # wrong shape) would otherwise write one full traceback per
                # frame for the rest of the broadcast. The periodic summary
                # below still reports the failure count every window, so a
                # sustained failure stays visible without flooding the log.
                if decode_failures_since_log == 0:
                    logger.exception("Failed to decode a captured frame; skipping it")
                decode_failures_since_log += 1
        if last_frame is not None:
            send_start = time.monotonic()
            try:
                # apply() runs on every send, fresh frame or the same
                # repeated frame object alike, so the burned-in clock keeps
                # ticking even when nothing new has been captured.
                timecode_overlay.apply(last_frame)
                sender.send(last_frame)
                sends_since_log += 1
                send_seconds_since_log += time.monotonic() - send_start
            except Exception:
                if send_failures_since_log == 0:
                    logger.exception("Failed to send a frame; skipping it")
                send_failures_since_log += 1
        now = time.monotonic()
        if now - last_log >= 5.0:
            decode_avg = (decode_seconds_since_log / decodes_since_log * 1000) if decodes_since_log else 0.0
            send_avg = (send_seconds_since_log / sends_since_log * 1000) if sends_since_log else 0.0
            logger.info(
                "NDI sender: %d decodes (%.1fms avg, %d failed), %d sends (%.1fms avg, %d failed) in the last %.1fs",
                decodes_since_log,
                decode_avg,
                decode_failures_since_log,
                sends_since_log,
                send_avg,
                send_failures_since_log,
                now - last_log,
            )
            decodes_since_log = 0
            decode_seconds_since_log = 0.0
            decode_failures_since_log = 0
            sends_since_log = 0
            send_seconds_since_log = 0.0
            send_failures_since_log = 0
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


SCK_WINDOW_TITLE = "Layout Driver Broadcaster"

# Chrome's own tab-strip + address-bar height in a plain headed window (single
# tab, no bookmarks bar) -- measured live via window.outerHeight minus
# window.innerHeight, consistently 87px across repeated runs of this Chrome
# version on macOS, for both Playwright's bundled Chrome for Testing and real
# Chrome. --kiosk removes it cleanly, but was found, live, to force macOS's
# native fullscreen Space transition for any borderless window sized to
# exactly fill a display -- with "Displays have separate Spaces" off (System
# Settings > Desktop & Dock > Mission Control, off by default on some Macs),
# that transition visibly disrupts whatever's on the operator's other
# displays, which is unacceptable for a background broadcaster process.
# Requesting a window this many pixels taller than the target resolution,
# then cropping that strip off the top of every captured frame (SckCapture's
# crop_top), gets an exact-resolution capture with zero window-manager side
# effects. If this ever drifts (a Chrome update changes the toolbar height),
# the visible symptom is a thin sliver of toolbar or black bar at the very
# top of the captured image -- remeasure via outerHeight - innerHeight in a
# real headed launch and update this constant.
_CHROME_TOOLBAR_HEIGHT_PX = 87


def _sck_chrome_window_size(config: BroadcasterConfig) -> tuple[int, int]:
    """The (width, height) to launch Chrome's window at for the sck backend.

    Deliberately factored out of _capture_loop_sck so this exact arithmetic
    -- window height = config.height + _CHROME_TOOLBAR_HEIGHT_PX -- is
    directly unit-testable against the same crop_top value passed to
    SckCapture, without needing to mock Playwright or ScreenCaptureKit. The
    two must always move together: SckCapture crops exactly crop_top rows
    off the top of every captured frame, so a window shorter or taller than
    config.height + crop_top leaves a toolbar sliver or a black bar at the
    top of every frame on the live wall.

    Takes config, not the resolved display, even though the window is
    positioned on that display: _validate_sck_display_mode and (in physical
    mode) find_physical_display's resolution check both make display.width/
    height == config.width/height a hard precondition before this is ever
    called, and SckCapture itself is built from config.width/height too --
    anchoring this function to the same source keeps that one invariant
    self-evident instead of incidentally true.
    """
    return config.width, config.height + _CHROME_TOOLBAR_HEIGHT_PX


def _validate_sck_display_mode(config: BroadcasterConfig) -> None:
    """Fail at startup rather than mid-capture on a missing sck field.

    Mirrors _validate_backend_selection's fail-fast convention in
    flux-gallery's worker.py: a missing required field for the selected mode
    is a config error, not something to guess at silently.
    """
    if config.capture_backend != "sck":
        return
    if config.sck_display_mode is None:
        raise ValueError(
            "capture_backend: sck requires sck_display_mode to be set to "
            "'virtual' or 'physical'"
        )
    if config.sck_display_mode == "physical" and not config.sck_physical_display_name:
        raise ValueError(
            "sck_display_mode: physical requires sck_physical_display_name to be set"
        )


def _decode_raw_rgba_frame(width: int, height: int) -> Callable[[bytes], np.ndarray]:
    def decode(data: bytes) -> np.ndarray:
        # np.frombuffer wraps the immutable `bytes` object directly, producing
        # a read-only array -- confirmed live: cyndilib's write_data() needs a
        # writable buffer and raises "buffer source array is read-only"
        # without this copy. decode_captured_frame's np.array(pil_image) never
        # hit this because PIL always allocates a fresh, writable array.
        return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4).copy()

    return decode


async def _capture_loop_sck(
    config: BroadcasterConfig, sender: VideoSender, stop_event: threading.Event
) -> None:
    # Imported lazily: capture_sck/virtual_display/physical_display all
    # import PyObjC frameworks at module scope, which must not become a hard
    # requirement for anyone running only the cdp backend.
    from .capture_sck import SckCapture
    from .physical_display import find_physical_display
    from .virtual_display import ensure_helper_built, start_vdisplay_helper, wait_for_settled_bounds

    vdisplay_proc: subprocess.Popen | None = None
    try:
        if config.sck_display_mode == "virtual":
            helper_dir = REPO_ROOT / "ndi_broadcaster" / "vdisplay_helper"
            binary_path = ensure_helper_built(helper_dir)
            vdisplay_proc, info = start_vdisplay_helper(
                binary_path, config.width, config.height, config.sck_virtual_display_name
            )
            display = wait_for_settled_bounds(info.display_id, config.width, config.height)
        else:
            display = find_physical_display(
                config.sck_physical_display_name, config.width, config.height
            )

        window_width, window_height = _sck_chrome_window_size(config)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=False,
                args=[
                    *_chrome_launch_args(),
                    f"--window-position={display.x},{display.y}",
                    # Requested taller than the target resolution to compensate
                    # for Chrome's own tab-strip/address-bar height -- see
                    # _CHROME_TOOLBAR_HEIGHT_PX. SckCapture crops that many rows
                    # off the top of every captured frame, so the delivered
                    # frame is still exactly config.width x config.height.
                    f"--window-size={window_width},{window_height}",
                    "--ignore-certificate-errors",
                    "--disable-session-crashed-bubble",
                    "--disable-infobars",
                    "--noerrdialogs",
                    "--no-first-run",
                ],
            )
            context = await browser.new_context(
                # no_viewport=True (not viewport=None) is what actually disables
                # Playwright's forced 1280x720 default viewport, confirmed live
                # during the proof-of-concept spike.
                no_viewport=True,
                ignore_https_errors=True,
                permissions=["microphone"],
            )
            page = await context.new_page()
            await page.goto(config.target_url)
            # A framework-controlled, app-independent title: apps each set
            # their own <title>, so SCShareableContent window matching can't
            # rely on any single app's page title.
            await page.evaluate("title => { document.title = title; }", SCK_WINDOW_TITLE)

            frame_slot = _LatestFrameSlot()
            sender_thread = threading.Thread(
                target=_sender_thread_loop,
                args=(frame_slot, sender, config, stop_event),
                kwargs={"decode_fn": _decode_raw_rgba_frame(config.width, config.height)},
                daemon=True,
            )
            sender_thread.start()

            # capture construction/start() is inside this try too: either can
            # raise (window lookup timeout, addStreamOutput failure, startCapture
            # timeout/error) after frames may have already begun flowing, and the
            # sender thread + browser must still be torn down in that case rather
            # than leaking a daemon thread stuck in sender.send() past this
            # function's return.
            capture: SckCapture | None = None
            try:
                capture = SckCapture(
                    SCK_WINDOW_TITLE,
                    config.width,
                    config.height,
                    config.fps,
                    on_frame=frame_slot.put,
                    crop_top=_CHROME_TOOLBAR_HEIGHT_PX,
                )
                capture.start()
                while not stop_event.is_set():
                    await asyncio.sleep(0.5)
            finally:
                # stop_event first, unconditionally: it cannot raise, and
                # everything after it (sender thread join, browser close)
                # must still run even if capture.stop() itself raises (e.g.
                # stopCaptureWithCompletionHandler_ on a stream that never
                # finished starting).
                stop_event.set()
                if capture is not None:
                    with contextlib.suppress(Exception):
                        capture.stop()
                sender_thread.join(timeout=5.0)
                await browser.close()
    finally:
        if vdisplay_proc is not None:
            vdisplay_proc.terminate()
            try:
                vdisplay_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                vdisplay_proc.kill()


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
    _validate_sck_display_mode(config)

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
        capture_loop = _capture_loop_sck if config.capture_backend == "sck" else _capture_loop
        try:
            asyncio.run(capture_loop(config, sender, stop_event))
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
