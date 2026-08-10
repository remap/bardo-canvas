import http.server
import platform
import socketserver
import textwrap
import threading
import time

import numpy as np
import pytest

from ndi_broadcaster.config import BroadcasterConfig
from ndi_broadcaster.launcher import (
    REPO_ROOT,
    HealthCheckTimeoutError,
    _chrome_launch_args,
    _decode_raw_rgba_frame,
    _LatestFrameSlot,
    _log_format,
    _sender_thread_loop,
    _validate_sck_display_mode,
    resolve_launcher_paths,
    resolve_target_url,
    run,
    wait_for_healthy,
)


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


def test_log_format_without_port_matches_original_format():
    assert _log_format({}) == "%(asctime)s %(levelname)s %(name)s: %(message)s"


def test_log_format_with_port_adds_prefix():
    assert _log_format({"LAYOUT_DRIVER_PORT": "8444"}) == (
        "%(asctime)s [:8444] %(levelname)s %(name)s: %(message)s"
    )


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


class _FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, frame):
        # Copy, not append-by-reference: a real NDI SDK send copies into its
        # own buffer, and _sender_thread_loop reuses the same mutable frame
        # object across every send (TimecodeOverlay mutates `last_frame` in
        # place). Storing references would make every entry in `self.sent`
        # literally the same object, so a test comparing sent[0] to sent[-1]
        # after the thread finishes would always see the final, fully-mutated
        # state on both sides -- unable to fail even if a real drift
        # regression were reintroduced. Copying here gives each entry an
        # independent snapshot of the frame's content at the moment of that
        # specific send call.
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
    # timecode_enabled=False: this test's degenerate 1x1 `decoded` frame is
    # only meant to exercise decode_fn wiring, not the overlay -- with the
    # default (enabled) config, TimecodeOverlay.apply()'s region would fall
    # entirely outside a 1x1 frame and cv2.putText would raise on the
    # resulting zero-size slice.
    config = BroadcasterConfig(timecode_enabled=False)
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


def test_sender_thread_loop_applies_timecode_overlay_when_enabled():
    # width/height=800x600 (not a degenerate 1x1) is deliberate: the overlay
    # region has fixed margins (edge_margin_px=32 on each side) regardless of
    # frame size, so a too-small frame could make cv2.putText/addWeighted
    # operate on a zero-size slice and raise -- caught by _sender_thread_loop's
    # own try/except around the send block, which would make sender.send()
    # silently never run and this test's "at least one send happened"
    # assertion fail for a reason unrelated to what it's supposed to check.
    # 800x600 is comfortably larger than the overlay's rendered region.
    frame_slot = _LatestFrameSlot()
    frame_slot.put(b"\x01\x02\x03\x04")
    sender = _FakeSender()
    config = BroadcasterConfig(timecode_enabled=True, width=800, height=600)
    stop_event = threading.Event()
    decoded = np.full((600, 800, 4), 100, dtype=np.uint8)
    clean_reference = decoded.copy()

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

    assert len(sender.sent) >= 1
    # TimecodeOverlay mutates `decoded` in place before _FakeSender.send()
    # copies it, so compare against the independent clean_reference copy
    # taken before the thread ran -- proves the real TimecodeOverlay (not a
    # stub) actually drew something in the top strip.
    assert not np.array_equal(sender.sent[0][:100, :], clean_reference[:100, :])


def test_sender_thread_loop_timecode_does_not_drift_across_repeated_sends(monkeypatch):
    # Integration-level guard for the snapshot()/apply() split: snapshot()
    # must run ONLY in the "a new frame was just decoded" branch of
    # _sender_thread_loop, never in the "repeated/stale frame" send branch.
    # test_timecode_overlay.py's own repeated-apply test proves TimecodeOverlay
    # itself doesn't compound blends, but says nothing about whether the loop
    # actually calls snapshot() at the right place -- if someone later moved
    # the snapshot() call next to apply() (both in the send block), that unit
    # test would stay green while live output silently drifted whiter on
    # every static/repeated frame. Here, only one frame ever arrives, so most
    # of the loop's sends are "repeated/stale frame" sends, not fresh decodes.
    times = iter([100.0] * 200)  # frozen instant; large pool since both
    # TimecodeOverlay.apply() and _sender_thread_loop's own pacing logic call
    # time.monotonic() every loop iteration
    monkeypatch.setattr("ndi_broadcaster.timecode_overlay.time.monotonic", lambda: next(times))

    frame_slot = _LatestFrameSlot()
    frame_slot.put(b"\x01\x02\x03\x04")  # only one frame ever arrives
    sender = _FakeSender()
    config = BroadcasterConfig(timecode_enabled=True, width=800, height=600, fps=30)
    stop_event = threading.Event()
    decoded = np.full((600, 800, 4), 100, dtype=np.uint8)

    thread = threading.Thread(
        target=_sender_thread_loop,
        args=(frame_slot, sender, config, stop_event),
        kwargs={"decode_fn": lambda data: decoded},
        daemon=True,
    )
    thread.start()
    time.sleep(0.15)
    stop_event.set()
    thread.join(timeout=2.0)

    assert len(sender.sent) >= 2
    # _FakeSender.send() now copies each frame, so sender.sent[0] and
    # sender.sent[-1] are independent snapshots taken at genuinely different
    # moments in time -- a real regression (snapshot() moved into the send
    # branch) would make these diverge.
    assert np.array_equal(sender.sent[0], sender.sent[-1])


def test_sender_thread_loop_skips_timecode_overlay_when_disabled():
    frame_slot = _LatestFrameSlot()
    frame_slot.put(b"\x01\x02\x03\x04")
    sender = _FakeSender()
    config = BroadcasterConfig(timecode_enabled=False)
    stop_event = threading.Event()
    decoded = np.zeros((1, 1, 4), dtype=np.uint8)
    clean_reference = decoded.copy()

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

    assert len(sender.sent) >= 1
    # Comparing against an independent copy taken before the thread ran
    # (rather than against `decoded` itself, which the loop may still mutate
    # after this point) actually exercises the disabled path -- mirrors
    # clean_reference in the enabled-path test above.
    assert np.array_equal(sender.sent[0], clean_reference)


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
