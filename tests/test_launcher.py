import asyncio
import http.server
import platform
import signal
import socketserver
import textwrap
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from ndi_broadcaster.config import BroadcasterConfig
from ndi_broadcaster.launcher import (
    _CHROME_APP_MODE_HEADROOM_PX,
    REPO_ROOT,
    HealthCheckTimeoutError,
    _chrome_launch_args,
    _decode_raw_rgba_frame,
    _LatestFrameSlot,
    _measure_actual_window_height,
    _measure_chrome_overhead_px,
    _open_control_window,
    _raise_keyboard_interrupt,
    _resolve_sck_crop_geometry,
    _sck_chrome_window_size,
    _sender_thread_loop,
    _shutdown_with_timeout,
    _validate_sck_display_mode,
    apply_display_mode_override,
    build_arg_parser,
    resolve_launcher_paths,
    resolve_target_url,
    run,
    wait_for_healthy,
)

# Not ndi_broadcaster.virtual_display.DisplayInfo: that module imports PyObjC
# frameworks at module scope (sys_platform == 'darwin' only), and this test
# file must stay importable on every platform -- test_chrome_launch_args_omit_metal_off_macos
# below exists specifically to run on non-macOS. _sck_chrome_window_size only
# ever reads display.width, so a plain namespace is a sufficient stand-in.
_FakeDisplay = SimpleNamespace


def test_resolve_launcher_paths_defaults():
    paths = resolve_launcher_paths({})
    assert paths.broadcaster_yaml == REPO_ROOT / "config" / "broadcaster.yaml"
    assert paths.audio_yaml == REPO_ROOT / "config" / "audio.yaml"


def test_resolve_launcher_paths_env_overrides():
    paths = resolve_launcher_paths(
        {"BROADCASTER_YAML": "/tmp/instance/broadcaster.yaml", "AUDIO_YAML": "/tmp/instance/audio.yaml"}
    )
    assert str(paths.broadcaster_yaml) == "/tmp/instance/broadcaster.yaml"
    assert str(paths.audio_yaml) == "/tmp/instance/audio.yaml"


class _HealthyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def test_wait_for_healthy_succeeds():
    with socketserver.TCPServer(("127.0.0.1", 0), _HealthyHandler) as server:
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            wait_for_healthy(f"http://127.0.0.1:{port}/healthz", timeout_seconds=5.0)
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_wait_for_healthy_times_out():
    with pytest.raises(HealthCheckTimeoutError):
        wait_for_healthy(
            "http://127.0.0.1:1/healthz", timeout_seconds=1.0, poll_interval_seconds=0.2
        )


def test_resolve_target_url_without_override_is_unchanged():
    config = BroadcasterConfig(target_url="https://localhost:8443/")

    assert resolve_target_url(config, {}) == config
    assert resolve_target_url(config, {"LAYOUT_DRIVER_TARGET_URL": ""}) == config


def test_resolve_target_url_applies_override():
    config = BroadcasterConfig(target_url="https://localhost:8443/", fps=25)

    resolved = resolve_target_url(config, {"LAYOUT_DRIVER_TARGET_URL": "https://localhost:9443/"})

    assert resolved.target_url == "https://localhost:9443/"
    # Every other field survives the override.
    assert resolved.fps == 25
    assert resolved == config.model_copy(update={"target_url": "https://localhost:9443/"})


def test_apply_display_mode_override_without_flags_is_unchanged():
    config = BroadcasterConfig(capture_backend="sck", sck_display_mode="virtual")

    assert apply_display_mode_override(config, None, None) == config


def test_apply_display_mode_override_applies_display_mode():
    config = BroadcasterConfig(capture_backend="sck", sck_display_mode="virtual", fps=25)

    resolved = apply_display_mode_override(config, "physical", None)

    assert resolved.sck_display_mode == "physical"
    # Every other field survives the override.
    assert resolved.fps == 25
    assert resolved == config.model_copy(update={"sck_display_mode": "physical"})


def test_apply_display_mode_override_applies_physical_display_name():
    config = BroadcasterConfig(capture_backend="sck", sck_display_mode="virtual")

    resolved = apply_display_mode_override(config, "physical", "SyncMaster")

    assert resolved.sck_display_mode == "physical"
    assert resolved.sck_physical_display_name == "SyncMaster"


def test_build_arg_parser_defaults_to_none():
    args = build_arg_parser().parse_args([])

    assert args.display_mode is None
    assert args.physical_display_name is None


def test_build_arg_parser_parses_display_mode_and_name():
    args = build_arg_parser().parse_args(
        ["--display-mode", "physical", "--physical-display-name", "SyncMaster"]
    )

    assert args.display_mode == "physical"
    assert args.physical_display_name == "SyncMaster"


def test_build_arg_parser_rejects_unknown_display_mode():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--display-mode", "bogus"])


def test_raise_keyboard_interrupt_raises():
    with pytest.raises(KeyboardInterrupt):
        _raise_keyboard_interrupt(signal.SIGTERM, None)


def test_shutdown_with_timeout_awaits_a_quick_coroutine():
    completed = []

    async def quick():
        completed.append(1)

    asyncio.run(_shutdown_with_timeout(quick(), "test"))

    assert completed == [1]


def test_shutdown_with_timeout_does_not_hang_forever(monkeypatch):
    # Live-observed bug: Playwright's own browser.close() / playwright.stop()
    # have no timeout of their own and can hang indefinitely (a hung Node.js
    # driver or Chrome process waiting on os.waitpid()), blocking this
    # process's shutdown well past every other bounded timeout in the
    # cleanup chain. This must resolve quickly regardless of how long the
    # awaitable would otherwise hang.
    monkeypatch.setattr("ndi_broadcaster.launcher._PLAYWRIGHT_SHUTDOWN_TIMEOUT_S", 0.05)

    async def hangs_forever():
        await asyncio.sleep(60)

    start = time.monotonic()
    asyncio.run(_shutdown_with_timeout(hangs_forever(), "test"))
    elapsed = time.monotonic() - start

    assert elapsed < 5.0


def test_shutdown_with_timeout_swallows_other_exceptions():
    async def raises():
        raise RuntimeError("simulated Playwright shutdown failure")

    asyncio.run(_shutdown_with_timeout(raises(), "test"))  # must not raise


def test_run_installs_sigterm_handler(tmp_path, monkeypatch):
    # Without this, run.sh's plain `kill "$BROADCASTER_PID"` (SIGTERM, no
    # Python default handler) kills the process before the existing
    # `except KeyboardInterrupt` cleanup path -- and everything nested
    # inside it, including the sck backend's vdisplay_helper subprocess
    # termination -- ever runs. Confirmed live: this orphaned
    # vdisplay_helper and leaked its virtual display.
    original_handler = signal.getsignal(signal.SIGTERM)
    config_path = tmp_path / "broadcaster.yaml"
    config_path.write_text('target_url: "https://localhost:8443/"\n')
    monkeypatch.setattr(
        "ndi_broadcaster.launcher.wait_for_healthy",
        lambda *args, **kwargs: (_ for _ in ()).throw(HealthCheckTimeoutError("stop early")),
    )
    try:
        with pytest.raises(HealthCheckTimeoutError):
            run(config_path=str(config_path))
        assert signal.getsignal(signal.SIGTERM) is _raise_keyboard_interrupt
    finally:
        signal.signal(signal.SIGTERM, original_handler)


def test_run_uses_overridden_target_url(tmp_path, monkeypatch):
    config_path = tmp_path / "broadcaster.yaml"
    config_path.write_text('target_url: "https://localhost:8443/"\n')
    monkeypatch.setenv("LAYOUT_DRIVER_TARGET_URL", "https://localhost:9443/")
    checked: list[str] = []

    def fake_wait(url, **kwargs):
        checked.append(url)
        raise HealthCheckTimeoutError("stop here, before any browser launches")

    monkeypatch.setattr("ndi_broadcaster.launcher.wait_for_healthy", fake_wait)

    with pytest.raises(HealthCheckTimeoutError):
        run(config_path=str(config_path))

    assert checked == ["https://localhost:9443/healthz"]


def test_validate_sck_display_mode_noop_for_cdp():
    _validate_sck_display_mode(BroadcasterConfig(capture_backend="cdp"))  # must not raise


def test_validate_sck_display_mode_requires_mode():
    with pytest.raises(ValueError, match="sck_display_mode"):
        _validate_sck_display_mode(BroadcasterConfig(capture_backend="sck"))


def test_validate_sck_display_mode_virtual_does_not_require_physical_name():
    _validate_sck_display_mode(
        BroadcasterConfig(capture_backend="sck", sck_display_mode="virtual")
    )  # must not raise


def test_validate_sck_display_mode_physical_requires_name():
    with pytest.raises(ValueError, match="sck_physical_display_name"):
        _validate_sck_display_mode(
            BroadcasterConfig(capture_backend="sck", sck_display_mode="physical")
        )


def test_sck_chrome_window_height_leaves_headroom_above_config_height():
    # This is headroom, not an exact crop target (see _CHROME_APP_MODE_HEADROOM_PX's
    # comment: the real title-bar height was found to jitter run over run, so
    # crop_top is measured live by _measure_chrome_overhead_px instead of assumed
    # from this constant). All this asserts is the window is requested taller by
    # exactly that headroom, which is what keeps it from being clipped by a virtual
    # display sized to config.height + _CHROME_APP_MODE_HEADROOM_PX.
    config = BroadcasterConfig(width=3840, height=2160)

    width, height = _sck_chrome_window_size(config)

    assert width == config.width
    assert height == config.height + _CHROME_APP_MODE_HEADROOM_PX


def test_measure_chrome_overhead_px_computes_gap_from_given_window_height():
    config = BroadcasterConfig(width=3840, height=2160)

    class _FakePage:
        async def evaluate(self, _script):
            return [config.width, 2190]

    overhead = asyncio.run(_measure_chrome_overhead_px(_FakePage(), config, window_height=2220))

    assert overhead == 30


def test_measure_chrome_overhead_px_raises_on_unexpected_inner_width():
    config = BroadcasterConfig(width=3840, height=2160)

    class _FakePage:
        async def evaluate(self, _script):
            return [config.width - 5, 2160]

    with pytest.raises(RuntimeError, match="innerWidth"):
        asyncio.run(_measure_chrome_overhead_px(_FakePage(), config, window_height=2220))


def test_open_control_window_is_a_noop_when_url_is_unset():
    config = BroadcasterConfig(control_window_url=None)

    class _FakePage:
        @property
        def context(self):
            raise AssertionError("must not touch context when control_window_url is unset")

    asyncio.run(_open_control_window(_FakePage(), config))


class _FakeCdpSession:
    def __init__(self, window_id=42, window_bounds_height=None):
        self.sent = []
        self._window_id = window_id
        self._window_bounds_height = window_bounds_height

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": self._window_id}
        if method == "Browser.getWindowBounds" and self._window_bounds_height is not None:
            return {"bounds": {"height": self._window_bounds_height}}
        return {}


class _FakeControlPage:
    def __init__(self):
        self.load_state_waits = []

    async def wait_for_load_state(self, state):
        self.load_state_waits.append(state)


class _FakeContext:
    def __init__(self, cdp=None):
        self.pages = []
        self.cdp_sessions_requested_for = []
        self._cdp = cdp if cdp is not None else _FakeCdpSession()

    async def new_cdp_session(self, page):
        self.cdp_sessions_requested_for.append(page)
        return self._cdp


class _FakeBroadcastPage:
    """Simulates window.open() by appending a new page to context.pages --
    the real signal _open_control_window polls context.pages for."""

    def __init__(self, context):
        self.context = context
        self.evaluated = []

    async def evaluate(self, script, arg):
        self.evaluated.append((script, arg))
        self.context.pages.append(_FakeControlPage())


def test_open_control_window_places_it_via_cdp_setwindowbounds(monkeypatch):
    config = BroadcasterConfig(
        control_window_url="https://localhost:8444/layout-control",
        control_display_name="Retina",
        control_window_width=1200,
        control_window_height=600,
    )
    fake_display = _FakeDisplay(x=1920, y=0, width=1080, height=1920)
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display.find_display_by_name",
        lambda name: (
            fake_display if name == "Retina" else (_ for _ in ()).throw(AssertionError(name))
        ),
    )

    context = _FakeContext()
    page = _FakeBroadcastPage(context)

    asyncio.run(_open_control_window(page, config))

    # window.open() itself carries no popup feature string -- CDP does the
    # actual placement below, since those features are exactly what get
    # clamped to the calling window's own screen for a different display.
    assert len(page.evaluated) == 1
    script, url = page.evaluated[0]
    assert "window.open" in script
    assert "left=" not in script
    assert url == "https://localhost:8444/layout-control"

    assert context.cdp_sessions_requested_for == [context.pages[0]]
    methods = [method for method, _ in context._cdp.sent]
    # State is normalized in its own call before the positioned bounds are
    # sent -- some Chrome builds drop left/top when windowState changes in
    # the same call that repositions -- and the result is read back after.
    assert methods == [
        "Browser.getWindowForTarget",
        "Browser.setWindowBounds",
        "Browser.setWindowBounds",
        "Browser.getWindowBounds",
    ]
    _, normalize_params = context._cdp.sent[1]
    assert normalize_params == {"windowId": 42, "bounds": {"windowState": "normal"}}
    _, bounds_params = context._cdp.sent[2]
    # Centered within fake_display, at the configured fixed size -- not the
    # full display bounds (that was the original bug: a 1920x1080+ display
    # produced a control window that filled the whole screen).
    assert bounds_params == {
        "windowId": 42,
        "bounds": {
            "left": fake_display.x + (fake_display.width - 1200) // 2,
            "top": fake_display.y + (fake_display.height - 600) // 2,
            "width": 1200,
            "height": 600,
            "windowState": "normal",
        },
    }
    assert context.pages[0].load_state_waits == ["domcontentloaded"]


def test_open_control_window_falls_back_to_main_screen_when_name_unset(monkeypatch):
    config = BroadcasterConfig(
        control_window_url="https://localhost:8444/layout-control",
        control_display_name=None,
        control_window_width=1200,
        control_window_height=600,
    )
    fake_display = _FakeDisplay(x=0, y=0, width=1920, height=1080)
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display.find_display_by_name",
        lambda _name: (_ for _ in ()).throw(
            AssertionError("must not be called when name is unset")
        ),
    )
    monkeypatch.setattr("ndi_broadcaster.physical_display.main_screen", lambda: fake_display)

    context = _FakeContext()
    page = _FakeBroadcastPage(context)

    asyncio.run(_open_control_window(page, config))

    _, bounds_params = context._cdp.sent[2]
    assert bounds_params["bounds"]["left"] == (1920 - 1200) // 2
    assert bounds_params["bounds"]["top"] == (1080 - 600) // 2
    assert bounds_params["bounds"]["width"] == 1200
    assert bounds_params["bounds"]["height"] == 600


def test_open_control_window_raises_if_the_window_never_appears(monkeypatch):
    monkeypatch.setattr("ndi_broadcaster.launcher._CONTROL_WINDOW_APPEAR_TIMEOUT_S", 0.05)
    config = BroadcasterConfig(
        control_window_url="https://localhost:8444/layout-control",
        control_display_name=None,
    )
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display.main_screen",
        lambda: _FakeDisplay(x=0, y=0, width=1920, height=1080),
    )

    class _NeverOpensPage:
        def __init__(self):
            self.context = _FakeContext()

        async def evaluate(self, _script, _arg):
            pass  # Simulates window.open() silently failing to produce a new page.

    with pytest.raises(RuntimeError, match="did not appear"):
        asyncio.run(_open_control_window(_NeverOpensPage(), config))


class _FakeBroadcastWindowPage:
    """A page with both .context.new_cdp_session (for
    _measure_actual_window_height) and .evaluate (for
    _measure_chrome_overhead_px) -- the two things _resolve_sck_crop_geometry
    needs from the real broadcast window's page."""

    def __init__(self, granted_height, inner_width, inner_height):
        self.context = _FakeContext(cdp=_FakeCdpSession(window_bounds_height=granted_height))
        self._inner_width = inner_width
        self._inner_height = inner_height

    async def evaluate(self, _script):
        return [self._inner_width, self._inner_height]


def test_measure_actual_window_height_reads_back_via_cdp():
    page = _FakeBroadcastWindowPage(granted_height=2160, inner_width=3840, inner_height=2132)

    height = asyncio.run(_measure_actual_window_height(page))

    assert height == 2160


def test_resolve_sck_crop_geometry_on_a_virtual_display_where_the_request_is_granted_in_full():
    # Virtual mode: the display is sized to match _sck_chrome_window_size's
    # request exactly, so the granted height equals the requested one -- this
    # is the headroom scheme working as designed, unchanged from before this
    # measurement was added.
    config = BroadcasterConfig(width=3840, height=2160)
    _, requested_height = _sck_chrome_window_size(config)
    page = _FakeBroadcastWindowPage(
        granted_height=requested_height, inner_width=3840, inner_height=requested_height - 28
    )

    crop_top, native_capture_height = asyncio.run(_resolve_sck_crop_geometry(page, config))

    assert crop_top == 28
    assert native_capture_height == requested_height


def test_resolve_sck_crop_geometry_on_a_physical_display_that_could_not_grant_the_full_request():
    # Physical mode: the OS hands back a window no taller than the real
    # display (2160), short of the height+headroom request (2220) -- the bug
    # this function exists to fix. window chrome (28px) still leaves enough
    # content (2132px) to cover config.height... just barely not, in this
    # case (see the next test for when it does cover it).
    config = BroadcasterConfig(width=3840, height=2100)
    page = _FakeBroadcastWindowPage(granted_height=2160, inner_width=3840, inner_height=2132)

    crop_top, native_capture_height = asyncio.run(_resolve_sck_crop_geometry(page, config))

    assert crop_top == 28
    assert native_capture_height == 2160  # the real granted height, not the 2220 that was requested


def test_resolve_sck_crop_geometry_raises_when_chrome_leaves_no_room_for_config_height():
    # The exact live failure this fix targets: a physical display sized to
    # exactly config.height (2160, required by find_physical_display) can
    # never fit config.height of content once Chrome's own chrome (28px)
    # takes some of that same real estate -- there is no display to grow
    # into, unlike virtual mode. Previously this surfaced as a per-frame
    # decode crash instead of one clear diagnosis at startup.
    config = BroadcasterConfig(width=3840, height=2160)
    page = _FakeBroadcastWindowPage(granted_height=2160, inner_width=3840, inner_height=2132)

    with pytest.raises(RuntimeError, match="28px short of the configured height"):
        asyncio.run(_resolve_sck_crop_geometry(page, config))


def test_run_requires_sck_display_mode_when_backend_is_sck(tmp_path, monkeypatch):
    config_path = tmp_path / "broadcaster.yaml"
    config_path.write_text(
        textwrap.dedent("""
            target_url: "https://localhost:8443/"
            capture_backend: sck
        """)
    )
    monkeypatch.setattr(
        "ndi_broadcaster.launcher.wait_for_healthy",
        lambda *args, **kwargs: pytest.fail("wait_for_healthy must not run when sck config is invalid"),
    )

    with pytest.raises(ValueError, match="sck_display_mode"):
        run(config_path=str(config_path))


def test_run_requires_sck_physical_display_name_when_mode_is_physical(tmp_path, monkeypatch):
    config_path = tmp_path / "broadcaster.yaml"
    config_path.write_text(
        textwrap.dedent("""
            target_url: "https://localhost:8443/"
            capture_backend: sck
            sck_display_mode: physical
        """)
    )
    monkeypatch.setattr(
        "ndi_broadcaster.launcher.wait_for_healthy",
        lambda *args, **kwargs: pytest.fail("wait_for_healthy must not run when sck config is invalid"),
    )

    with pytest.raises(ValueError, match="sck_physical_display_name"):
        run(config_path=str(config_path))


def test_run_display_mode_cli_flag_overrides_yaml_virtual_default(tmp_path, monkeypatch):
    # broadcaster.yaml says virtual (no physical name needed); the CLI flag
    # flips it to physical for this run only, which should surface the same
    # "requires sck_physical_display_name" validation virtual never hits --
    # proving the override reached config before _validate_sck_display_mode,
    # not just that the flag was accepted.
    config_path = tmp_path / "broadcaster.yaml"
    config_path.write_text(
        textwrap.dedent("""
            target_url: "https://localhost:8443/"
            capture_backend: sck
            sck_display_mode: virtual
        """)
    )
    monkeypatch.setattr(
        "ndi_broadcaster.launcher.wait_for_healthy",
        lambda *args, **kwargs: pytest.fail("wait_for_healthy must not run when sck config is invalid"),
    )

    with pytest.raises(ValueError, match="sck_physical_display_name"):
        run(config_path=str(config_path), display_mode="physical")


def test_run_physical_display_name_cli_flag_satisfies_validation(tmp_path, monkeypatch):
    config_path = tmp_path / "broadcaster.yaml"
    config_path.write_text(
        textwrap.dedent("""
            target_url: "https://localhost:8443/"
            capture_backend: sck
            sck_display_mode: physical
        """)
    )
    checked: list[str] = []

    def fake_wait(url, **kwargs):
        checked.append(url)
        raise HealthCheckTimeoutError("stop here, before any browser launches")

    monkeypatch.setattr("ndi_broadcaster.launcher.wait_for_healthy", fake_wait)

    with pytest.raises(HealthCheckTimeoutError):
        run(config_path=str(config_path), physical_display_name="SyncMaster")

    # Reaching wait_for_healthy at all proves _validate_sck_display_mode
    # passed, which requires sck_physical_display_name to be set -- the CLI
    # flag, since broadcaster.yaml itself never sets one.
    assert checked == ["https://localhost:8443/healthz"]


class _FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, frame):
        # A copy, not the same reference: _sender_thread_loop's overlay
        # mutates `frame` in place and reuses the same object across
        # repeated sends, so storing by reference would make every entry in
        # `sent` alias the same, later-mutated array.
        self.sent.append(frame.copy())


def test_sender_thread_loop_defaults_to_decode_captured_frame(monkeypatch):
    calls = []

    def fake_decode_captured_frame(data, target_width=None, target_height=None):
        calls.append((data, target_width, target_height))
        return np.zeros((1, 1, 4), dtype=np.uint8)

    monkeypatch.setattr("ndi_broadcaster.launcher.decode_captured_frame", fake_decode_captured_frame)

    frame_slot = _LatestFrameSlot()
    frame_slot.put(b"fake-image-bytes")
    sender = _FakeSender()
    config = BroadcasterConfig(width=10, height=20)
    stop_event = threading.Event()

    thread = threading.Thread(
        target=_sender_thread_loop, args=(frame_slot, sender, config, stop_event), daemon=True
    )
    thread.start()
    time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=2.0)

    assert calls == [(b"fake-image-bytes", 10, 20)]


def test_sender_thread_loop_uses_custom_decode_fn():
    frame_slot = _LatestFrameSlot()
    frame_slot.put(b"\x01\x02\x03\x04")
    sender = _FakeSender()
    config = BroadcasterConfig()
    stop_event = threading.Event()
    decoded = np.zeros((1, 1, 4), dtype=np.uint8)
    calls = []

    def fake_decode(data):
        calls.append(data)
        return decoded

    thread = threading.Thread(
        target=_sender_thread_loop,
        args=(frame_slot, sender, config, stop_event),
        kwargs={"decode_fn": fake_decode},
        daemon=True,
    )
    thread.start()
    time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=2.0)

    assert calls == [b"\x01\x02\x03\x04"]
    assert len(sender.sent) >= 1
    assert np.array_equal(sender.sent[0], decoded)


class _FakeTimecodeOverlay:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.snapshot_calls = 0
        self.apply_calls = 0

    def snapshot(self, frame):
        self.snapshot_calls += 1

    def apply(self, frame):
        self.apply_calls += 1


def test_sender_thread_loop_wires_timecode_overlay_snapshot_and_apply(monkeypatch):
    # A fake overlay (rather than the real Pillow-backed one) keeps this test
    # about the *wiring contract* -- construct once, snapshot() only on a
    # genuine new decode, apply() on every send -- not about font rendering.
    # The actual blend/no-drift math is covered by test_timecode_overlay.py.
    fake_overlays = []

    def fake_timecode_overlay(**kwargs):
        overlay = _FakeTimecodeOverlay(**kwargs)
        fake_overlays.append(overlay)
        return overlay

    monkeypatch.setattr("ndi_broadcaster.launcher.TimecodeOverlay", fake_timecode_overlay)

    frame_slot = _LatestFrameSlot()
    frame_slot.put(b"frame-1")  # only one frame ever arrives -- the rest are repeated sends
    sender = _FakeSender()
    config = BroadcasterConfig(
        width=800, height=600, fps=30, timecode_enabled=True, timecode_position="bottom"
    )
    stop_event = threading.Event()
    decoded = np.zeros((1, 1, 4), dtype=np.uint8)

    thread = threading.Thread(
        target=_sender_thread_loop,
        args=(frame_slot, sender, config, stop_event),
        kwargs={"decode_fn": lambda data: decoded},
        daemon=True,
    )
    thread.start()
    time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=2.0)

    assert len(fake_overlays) == 1
    overlay = fake_overlays[0]
    assert overlay.init_kwargs == {
        "enabled": True,
        "position": "bottom",
        "width": 800,
        "height": 600,
        "fps": 30,
    }
    assert overlay.snapshot_calls == 1
    assert len(sender.sent) > 1  # confirms the stale frame really was re-sent, not just decoded once
    assert overlay.apply_calls == len(sender.sent)


def test_decode_raw_rgba_frame_reshapes_and_returns_a_writable_array():
    # Live-observed bug: np.frombuffer wraps the immutable `bytes` object
    # directly, producing a read-only array. cyndilib's write_data() needs a
    # writable buffer and raises "buffer source array is read-only" without
    # an explicit copy -- this test pins that the returned array is writable,
    # not just correctly shaped.
    decode = _decode_raw_rgba_frame(width=2, height=1)
    data = bytes([1, 2, 3, 4, 5, 6, 7, 8])

    frame = decode(data)

    assert frame.shape == (1, 2, 4)
    assert frame.flags.writeable
    assert np.array_equal(frame, np.array([[[1, 2, 3, 4], [5, 6, 7, 8]]], dtype=np.uint8))


def test_latest_frame_slot_starts_empty():
    assert _LatestFrameSlot().take() is None


def test_latest_frame_slot_keeps_only_the_latest_value():
    slot = _LatestFrameSlot()
    slot.put(b"first")
    slot.put(b"second")

    assert slot.take() == b"second"
    # No backlog: the superseded frame is gone, not queued behind the latest one.
    assert slot.take() is None


def test_chrome_launch_args_include_autoplay_policy():
    args = _chrome_launch_args()
    assert "--autoplay-policy=no-user-gesture-required" in args


@pytest.mark.skipif(platform.system() != "Darwin", reason="ANGLE Metal is macOS-only")
def test_chrome_launch_args_use_metal_on_macos():
    assert "--use-angle=metal" in _chrome_launch_args()


@pytest.mark.skipif(platform.system() == "Darwin", reason="covered by the macOS-only test above")
def test_chrome_launch_args_omit_metal_off_macos():
    assert "--use-angle=metal" not in _chrome_launch_args()
