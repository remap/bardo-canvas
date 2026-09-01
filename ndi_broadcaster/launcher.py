from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import logging
import os
import platform
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
from playwright.async_api import async_playwright

from layout_server.audio import discover_audio_devices, load_audio_config
from layout_server.log_format import log_format

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


def apply_display_mode_override(
    config: BroadcasterConfig,
    display_mode: str | None,
    physical_display_name: str | None,
) -> BroadcasterConfig:
    """Layer --display-mode/--physical-display-name CLI flags onto config.

    Same override-only-if-given shape as resolve_target_url, but for a choice
    an operator wants to flip per run (e.g. "capture the one connected
    monitor instead of a virtual display, just for this test") without
    editing broadcaster.yaml, which stays the checked-in default for
    unattended runs. Neither flag touches capture_backend: sck_display_mode
    is meaningless under "cdp" and _validate_sck_display_mode already ignores
    it there, so overriding it is a no-op rather than a surprise backend
    switch.
    """
    updates: dict[str, str] = {}
    if display_mode is not None:
        updates["sck_display_mode"] = display_mode
    if physical_display_name is not None:
        updates["sck_physical_display_name"] = physical_display_name
    if not updates:
        return config
    return config.model_copy(update=updates)


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

# Chrome's own window-chrome height in --app= mode (a bare window with no
# tab-strip or address bar -- just a minimal native title bar) was found live
# NOT to be a fixed pixel count: measured at 30px on one broadcaster launch
# and 28px on the very next, with nothing else changed and the virtual
# display itself confirmed (via CGDisplayPixelsHigh) to be exactly the
# requested size both times -- some ±1-2px jitter in exactly how much of a
# requested --window-size Chrome's own title bar eats into is apparently
# inherent to this environment, not a one-time measurement error. A single
# hardcoded constant therefore can never land on an exact match run over run:
# see this file's git history around 2026-08-28 for two rounds of "measure,
# hardcode, still off by a pixel or two" before this was replaced.
#
# So this is no longer used as an exact prediction. It's just headroom: the
# window (and the virtual display hosting it, which can never show a window
# taller than itself -- the OS silently clips it back down otherwise) is
# requested this many pixels taller than config.height, comfortably above
# the observed 28-30px range so window.innerHeight is guaranteed >=
# config.height regardless of that run's exact jitter. _run_sck_session then
# measures window.innerHeight on the ACTUAL launched page directly and
# computes crop_top from that measurement -- see its
# _measure_chrome_overhead_px -- rather than assuming any fixed number, which
# is what makes the final capture exact despite the jitter above.
#
# This used to be 87px in plain (tabbed) mode -- --kiosk would remove that
# cleanly too, but was found, live, to force macOS's native fullscreen Space
# transition for any borderless window sized to exactly fill a display --
# with "Displays have separate Spaces" off (System Settings > Desktop & Dock
# > Mission Control, off by default on some Macs), that transition visibly
# disrupts whatever's on the operator's other displays, unacceptable for a
# background broadcaster process. --app= removes the tab-strip/address-bar
# without invoking any fullscreen/Space API, so it doesn't have that problem.
#
# physical_display mode cannot grow its display to add this headroom (real
# hardware has no slack), so the OS can hand back a physical-mode window
# shorter than this headroom requests -- previously a per-frame decode crash
# (config.height assumed available, when Chrome's own chrome could leave
# less). _resolve_sck_crop_geometry now measures the real granted window
# height instead of assuming this constant was honored, and raises one clear
# error at startup if what's left after cropping Chrome's chrome off still
# doesn't cover config.height -- true when a physical display has no slack to
# grow into, which this constant can't fix by itself.
_CHROME_APP_MODE_HEADROOM_PX = 60

# Both browser.close() and Playwright's own async_playwright() context-manager
# exit (playwright.stop(), an alias for its __aexit__) wait on their
# underlying Node.js driver process with no timeout of their own. Confirmed
# live: a hung driver/Chrome exit blocked this process indefinitely, well
# past every other bounded timeout in this shutdown path -- including the one
# that terminates vdisplay_helper, which sat in that finally block never
# getting a chance to run. _shutdown_with_timeout() bounds both call sites.
_PLAYWRIGHT_SHUTDOWN_TIMEOUT_S = 10.0


async def _shutdown_with_timeout(awaitable: Awaitable[None], description: str) -> None:
    try:
        await asyncio.wait_for(awaitable, timeout=_PLAYWRIGHT_SHUTDOWN_TIMEOUT_S)
    except Exception:
        logger.exception(
            "%s did not complete within %.0fs; abandoning it",
            description,
            _PLAYWRIGHT_SHUTDOWN_TIMEOUT_S,
        )


def _sck_chrome_window_size(config: BroadcasterConfig) -> tuple[int, int]:
    """The (width, height) to launch Chrome's window at for the sck backend.

    Deliberately factored out of _capture_loop_sck so this exact arithmetic
    -- window height = config.height + _CHROME_APP_MODE_HEADROOM_PX -- is
    directly unit-testable without needing to mock Playwright or
    ScreenCaptureKit. This is deliberately more headroom than Chrome's actual
    window-chrome needs (see _CHROME_APP_MODE_HEADROOM_PX's comment on why an
    exact match isn't reliable run over run): _measure_chrome_overhead_px
    measures the real gap on the actual launched window and that measurement,
    not this function's return value, is what SckCapture's crop_top is built
    from.

    Takes config, not the resolved display, even though the window is
    positioned on that display: SckCapture itself is built from
    config.width/height, and (virtual mode) the display is now sized to
    config.width x (config.height + _CHROME_APP_MODE_HEADROOM_PX) specifically
    so this taller window fits without the OS clipping it back down -- see
    _CHROME_APP_MODE_HEADROOM_PX's comment. Physical mode's display is real
    hardware and cannot grow to match -- find_physical_display's resolution
    check still requires display.width/height == config.width/height there --
    so the height this function returns is only ever a request in that mode;
    _resolve_sck_crop_geometry measures what the OS actually granted instead
    of assuming this value was honored.
    """
    return config.width, config.height + _CHROME_APP_MODE_HEADROOM_PX


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
    # requirement for anyone running only the cdp backend. capture_sck's
    # import lives in _run_sck_session instead, closer to its only use.
    from .physical_display import find_physical_display
    from .virtual_display import ensure_helper_built, start_vdisplay_helper, wait_for_settled_bounds

    vdisplay_proc: subprocess.Popen | None = None
    try:
        if config.sck_display_mode == "virtual":
            helper_dir = REPO_ROOT / "ndi_broadcaster" / "vdisplay_helper"
            binary_path = ensure_helper_built(helper_dir)
            # +_CHROME_APP_MODE_HEADROOM_PX: the window placed on this display is
            # requested that much taller than config.height (see
            # _sck_chrome_window_size), and a window can never actually be
            # taller than the display hosting it -- the OS silently clips it
            # back down otherwise. Sizing the display to match is what makes
            # the "request a taller window, crop the top" scheme underneath
            # actually work instead of silently clipping.
            display_height = config.height + _CHROME_APP_MODE_HEADROOM_PX
            vdisplay_proc, info = start_vdisplay_helper(
                binary_path, config.width, display_height, config.sck_virtual_display_name
            )
            display = wait_for_settled_bounds(info.display_id, config.width, display_height)
        else:
            display = find_physical_display(
                config.sck_physical_display_name, config.width, config.height
            )

        window_width, window_height = _sck_chrome_window_size(config)
        playwright_cm = async_playwright()
        playwright = await playwright_cm.start()
        try:
            with tempfile.TemporaryDirectory(prefix="layout-driver-sck-profile-") as profile_dir:
                # launch_persistent_context (not launch() + new_context()):
                # --app= pre-navigates its own window before Playwright's CDP
                # session attaches, and a plain launch() + new_context()'s
                # freshly-created context/page never sees that window at all
                # (confirmed live -- browser.contexts() came back empty right
                # after a --app= launch(), and a second new_context() created
                # an entirely separate, untracked window with the normal
                # tab-strip/toolbar still showing, instead of controlling the
                # --app= one). A persistent context launch attaches to the
                # browser's actual initial session, so the --app= window
                # shows up in context.pages directly -- confirmed live via
                # the same launch args below.
                context = await playwright.chromium.launch_persistent_context(
                    profile_dir,
                    headless=False,
                    # no_viewport=True (not viewport=None) is what actually disables
                    # Playwright's forced 1280x720 default viewport, confirmed live
                    # during the proof-of-concept spike.
                    no_viewport=True,
                    ignore_https_errors=True,
                    permissions=["microphone"],
                    args=[
                        *_chrome_launch_args(),
                        f"--app={config.target_url}",
                        f"--window-position={display.x},{display.y}",
                        # Requested taller than the target resolution to leave
                        # comfortable headroom for --app= mode's title bar --
                        # see _CHROME_APP_MODE_HEADROOM_PX. The exact amount to
                        # crop off the top is measured live below
                        # (_measure_chrome_overhead_px), not assumed from this
                        # headroom value.
                        f"--window-size={window_width},{window_height}",
                        "--ignore-certificate-errors",
                        "--disable-session-crashed-bubble",
                        "--noerrdialogs",
                    ],
                )
                await _run_sck_session(config, context, sender, stop_event)
        finally:
            await _shutdown_with_timeout(playwright.stop(), "Playwright driver shutdown")
    finally:
        if vdisplay_proc is not None:
            vdisplay_proc.terminate()
            try:
                vdisplay_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                vdisplay_proc.kill()


async def _measure_chrome_overhead_px(page, config: BroadcasterConfig, window_height: int) -> int:
    """How many rows of Chrome's own window-chrome sit above the page content,
    given a window actually sized window_width(=config.width) x window_height.

    Not a fixed constant -- see _CHROME_APP_MODE_HEADROOM_PX's comment: this
    was found live to jitter by a couple pixels between otherwise-identical
    launches, so it's measured fresh on the actual window about to be
    captured instead of assumed. window.innerWidth is asserted equal to
    config.width (not just measured) because if that one ever starts
    drifting too, crop_top alone can no longer make the capture exact --
    that would need a horizontal crop this function doesn't compute, and
    silently proceeding would produce a capture wrong in a way this whole
    function was written to prevent.
    """
    inner_width, inner_height = await page.evaluate("[window.innerWidth, window.innerHeight]")
    if inner_width != config.width:
        raise RuntimeError(
            f"--app= window's innerWidth ({inner_width}) != config.width ({config.width}); "
            "unlike innerHeight, this code has no horizontal crop to compensate for that."
        )
    overhead = window_height - inner_height
    logger.info(
        "sck: measured %dpx of window chrome above the page content (window %dx%d, inner %dx%d)",
        overhead,
        config.width,
        window_height,
        inner_width,
        inner_height,
    )
    return overhead


async def _measure_actual_window_height(page) -> int:
    """The real height Chrome's broadcast window was actually granted, read
    back via CDP -- the same Browser.getWindowBounds call
    _open_control_window already uses to confirm its own window's placement,
    applied here to the main window instead.

    _sck_chrome_window_size's returned height is only a request passed to
    --window-size. A virtual display sized to match it (see
    _capture_loop_sck) always grants it in full, so the request and the
    reality happen to agree there -- but a physical display cannot grow
    past its own real height (find_physical_display already requires it to
    equal config.width/height exactly), so the OS can hand back a shorter
    window there instead of the taller one requested for headroom (see
    _CHROME_APP_MODE_HEADROOM_PX). Measuring what was actually granted,
    rather than assuming the request was honored, is what keeps
    _resolve_sck_crop_geometry's arithmetic correct in both cases.
    """
    cdp = await page.context.new_cdp_session(page)
    window_info = await cdp.send("Browser.getWindowForTarget")
    bounds = await cdp.send("Browser.getWindowBounds", {"windowId": window_info["windowId"]})
    return bounds["bounds"]["height"]


async def _resolve_sck_crop_geometry(page, config: BroadcasterConfig) -> tuple[int, int]:
    """The (crop_top, native_capture_height) SckCapture should use for the
    window as actually launched -- both measured against the real granted
    window height (_measure_actual_window_height), not the taller height
    _sck_chrome_window_size requested for headroom.

    The window is requested at config.height + _CHROME_APP_MODE_HEADROOM_PX --
    generous headroom, not a precise target (see that constant's comment) --
    specifically so it's never clipped by the display no matter how the real
    chrome overhead jitters this run. On a virtual display, sized to match
    that request, the headroom is granted in full: it becomes real, unused
    black space below the composited wall inside the window (rescale() pins
    scale to 1.0 via the width ratio, so the wall itself is never stretched
    to fill it), and native_capture_height crops that leftover margin off
    the bottom of every frame -- pure numpy slicing on an already-delivered
    pixel buffer, with no OS window manager involved. On a physical display
    there is no such margin to grant: the window comes back at most as tall
    as the display itself, headroom included, so this measures what was
    really granted instead of assuming the request.

    An earlier version of this function resized the actual OS window down to
    remove virtual mode's leftover margin (via CDP's Browser.setWindowBounds),
    so SckCapture could crop only the top as it always had. That was
    abandoned: live testing found this specific window (positioned flush
    against the virtual display's exact width and height, zero slack on any
    edge) came back from a resize with its width *also* shifted by -20px,
    reproducibly, regardless of what bounds were requested -- a small-scale
    test window with real margin on a real display did not reproduce this, so
    it looks tied to being flush against the display edge, not a general CDP
    quirk, but chasing it further wasn't worth it.

    Raises RuntimeError if what's left after cropping Chrome's own chrome off
    the top is still shorter than config.height -- true on a physical display
    with no slack to grow into, and something to know at startup rather than
    guess at from a per-frame decode crash: nothing downstream of this point
    can recover from a window that has no room for the configured height.
    """
    actual_height = await _measure_actual_window_height(page)
    crop_top = await _measure_chrome_overhead_px(page, config, actual_height)
    available = actual_height - crop_top
    if available < config.height:
        raise RuntimeError(
            f"broadcast window is {actual_height}px tall and Chrome's own "
            f"window chrome takes {crop_top}px of that, leaving {available}px "
            f"of content -- {config.height - available}px short of the "
            f"configured height ({config.height}). A virtual display can grow "
            "to make room for this; a physical one can't (its resolution must "
            "already match config.height exactly -- see find_physical_display). "
            "Lower height in broadcaster.yaml by at least that much, or use "
            "--display-mode virtual instead."
        )
    return crop_top, actual_height


_CONTROL_WINDOW_APPEAR_TIMEOUT_S = 10.0


async def _open_control_window(page, config: BroadcasterConfig) -> None:
    """Open config.control_window_url as a second, positioned window in the
    SAME Playwright-controlled Chrome profile the broadcast window lives in.

    Same profile matters beyond convenience: it is what lets a page like
    yt-matrix's /layout-control reach the broadcast page over
    BroadcastChannel, which only bridges tabs within one browser process --
    a control page opened in an operator's own separate everyday browser can
    never connect to this one.

    Placement is via CDP's Browser.setWindowBounds, not window.open()'s
    left/top popup features -- confirmed live those get silently clamped to
    the CALLING window's own screen for a *different* display, a deliberate
    Chrome restriction (cross-screen placement from page script needs the
    Window Management API's explicit, user-granted permission, which nothing
    here can obtain). CDP operates through the browser's own automation
    protocol, one level above the page-script sandbox that restriction
    exists in, so it can move a window to any connected display regardless.
    """
    if not config.control_window_url:
        return
    from .physical_display import find_display_by_name, main_screen

    display = (
        find_display_by_name(config.control_display_name)
        if config.control_display_name
        else main_screen()
    )
    width = config.control_window_width
    height = config.control_window_height
    left = display.x + (display.width - width) // 2
    top = display.y + (display.height - height) // 2
    logger.info(
        "opening control window %r on display at %d,%d (%dx%d), centered at %d,%d (%dx%d)",
        config.control_window_url,
        display.x,
        display.y,
        display.width,
        display.height,
        left,
        top,
        width,
        height,
    )

    context = page.context
    existing_pages = set(context.pages)
    # A bare window.open(), no popup feature string: those features are what
    # get the cross-screen clamp in the first place, and CDP does the actual
    # placement below regardless of what Chrome would have honoured here.
    await page.evaluate("(url) => { window.open(url, '_blank'); }", config.control_window_url)

    deadline = time.monotonic() + _CONTROL_WINDOW_APPEAR_TIMEOUT_S
    control_page = None
    while time.monotonic() < deadline:
        new_pages = [p for p in context.pages if p not in existing_pages]
        if new_pages:
            control_page = new_pages[0]
            break
        await asyncio.sleep(0.1)
    if control_page is None:
        raise RuntimeError(
            f"control window for {config.control_window_url!r} did not appear within "
            f"{_CONTROL_WINDOW_APPEAR_TIMEOUT_S}s"
        )
    await control_page.wait_for_load_state("domcontentloaded")

    cdp = await context.new_cdp_session(control_page)
    window_info = await cdp.send("Browser.getWindowForTarget")
    window_id = window_info["windowId"]

    # Two calls, not one: a window created via window.open() can still carry
    # a maximized/other non-normal state, and some Chrome builds silently
    # drop the left/top half of setWindowBounds when windowState changes in
    # the same call that repositions -- normalize state first, then move.
    await cdp.send(
        "Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}}
    )
    await cdp.send(
        "Browser.setWindowBounds",
        {
            "windowId": window_id,
            "bounds": {
                "left": left,
                "top": top,
                "width": width,
                "height": height,
                "windowState": "normal",
            },
        },
    )
    applied = await cdp.send("Browser.getWindowBounds", {"windowId": window_id})
    logger.info(
        "control window bounds after placement: %r (requested left=%d top=%d)",
        applied,
        left,
        top,
    )


async def _run_sck_session(
    config: BroadcasterConfig,
    context,
    sender: VideoSender,
    stop_event: threading.Event,
) -> None:
    from .capture_sck import SckCapture

    try:
        # --app= already navigated this page to config.target_url before
        # Playwright ever attached -- there is nothing to page.goto() here.
        page = context.pages[0]
        await page.wait_for_load_state("load")
        # A framework-controlled, app-independent title: apps each set
        # their own <title>, so SCShareableContent window matching can't
        # rely on any single app's page title.
        await page.evaluate("title => { document.title = title; }", SCK_WINDOW_TITLE)

        await _open_control_window(page, config)

        crop_top, native_capture_height = await _resolve_sck_crop_geometry(page, config)

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
                crop_top=crop_top,
                native_capture_height=native_capture_height,
            )
            capture.start()
            while not stop_event.is_set():
                await asyncio.sleep(0.5)
        finally:
            # stop_event first, unconditionally: it cannot raise, and
            # everything after it (sender thread join, context close)
            # must still run even if capture.stop() itself raises (e.g.
            # stopCaptureWithCompletionHandler_ on a stream that never
            # finished starting).
            stop_event.set()
            if capture is not None:
                with contextlib.suppress(Exception):
                    capture.stop()
            sender_thread.join(timeout=5.0)
    finally:
        await _shutdown_with_timeout(context.close(), "context.close()")


async def _capture_loop(
    config: BroadcasterConfig, sender: VideoSender, stop_event: threading.Event
) -> None:
    playwright_cm = async_playwright()
    playwright = await playwright_cm.start()
    try:
        # Headless: nothing in this app (audio is a plain <audio> element, no DRM)
        # needs a real window, and capturing the wall via CDP screenshots of a
        # *visible* window at 30fps was hammering the compositor enough to cause
        # visible on-screen flashing -- confirmed live by isolating the same headed
        # launch with the capture loop removed entirely: no flashing without it.
        # Headless has no on-screen presentation to disrupt, so there's nothing to
        # flash; screenshot capture, content, and audio routing are all unaffected.
        browser = await playwright.chromium.launch(headless=True, args=_chrome_launch_args())
        context = await browser.new_context(
            # Headless has no physical monitor to be smaller than this, so Playwright's
            # requested viewport is authoritative here, and #layout-driver-root's
            # rescale() always resolves to scale=1 and root exactly fills the viewport.
            # That's what makes capturing the whole viewport (via screencast, below)
            # equivalent to capturing root directly.
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
                await _shutdown_with_timeout(browser.close(), "browser.close()")
    finally:
        await _shutdown_with_timeout(playwright.stop(), "Playwright driver shutdown")


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    """SIGTERM handler: reuses the existing SIGINT/KeyboardInterrupt shutdown path.

    Without this, run.sh's `kill "$BROADCASTER_PID"` (plain SIGTERM, no
    handler installed by default in Python for that signal) kills this
    process immediately -- the `except KeyboardInterrupt` below, and every
    capture loop's `finally` cleanup nested inside it (which is what
    terminates the sck backend's vdisplay_helper subprocess and tears down
    its virtual display), never runs. Confirmed live: killing the launcher
    process directly left vdisplay_helper running as an orphan, needing a
    separate, manual kill to avoid leaking its virtual display.
    """
    raise KeyboardInterrupt


def run(
    config_path: str | None = None,
    audio_config_path: str | None = None,
    display_mode: str | None = None,
    physical_display_name: str | None = None,
) -> None:
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    env = dict(os.environ)
    paths = resolve_launcher_paths(env)
    if config_path is None:
        config_path = str(paths.broadcaster_yaml)
    if audio_config_path is None:
        audio_config_path = str(paths.audio_yaml)

    config = load_broadcaster_config(Path(config_path))
    config = resolve_target_url(config, env)
    config = apply_display_mode_override(config, display_mode, physical_display_name)
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
            logger.warning(
                "Audio input device not found: %r — continuing without audio",
                audio_config.input_device,
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


def build_arg_parser() -> argparse.ArgumentParser:
    """Shared by this module's own __main__ and by any app-specific entry
    point that calls run() directly instead (e.g. yt-matrix's
    broadcast-yt-layout.py, which forwards these same two flags into
    run()'s display_mode/physical_display_name kwargs) -- one flag
    definition, not a copy per entry point.
    """
    parser = argparse.ArgumentParser(description="Layout-driver NDI broadcaster")
    parser.add_argument(
        "--display-mode",
        choices=["virtual", "physical"],
        default=None,
        help=(
            "Override config/broadcaster.yaml's sck_display_mode for this run only "
            "(sck backend only; ignored under cdp). 'physical' captures a real "
            "connected display instead of creating an off-screen one -- pair with "
            "--physical-display-name unless broadcaster.yaml already sets "
            "sck_physical_display_name."
        ),
    )
    parser.add_argument(
        "--physical-display-name",
        default=None,
        help=(
            "Case-insensitive substring match against a connected display's name "
            "(e.g. 'SyncMaster' -- see `python -m ndi_broadcaster.vdisplay_doctor "
            "scan` for connected names). Only takes effect with --display-mode "
            "physical; the matched display's reported resolution must equal "
            "broadcaster.yaml's width/height exactly."
        ),
    )
    return parser


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format=log_format(dict(os.environ)))
    args = build_arg_parser().parse_args()
    run(display_mode=args.display_mode, physical_display_name=args.physical_display_name)
