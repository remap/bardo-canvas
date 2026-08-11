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
    _CHROME_TOOLBAR_HEIGHT_PX,
    REPO_ROOT,
    HealthCheckTimeoutError,
    _chrome_launch_args,
    _decode_raw_rgba_frame,
    _LatestFrameSlot,
    _log_format,
    _sck_chrome_window_size,
    _sender_thread_loop,
    _validate_sck_display_mode,
    resolve_launcher_paths,
    resolve_target_url,
    run,
    wait_for_healthy,
)
from ndi_broadcaster.virtual_display import DisplayInfo


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


def test_sck_chrome_window_height_matches_toolbar_crop():
    # The load-bearing invariant: Chrome's window must be exactly
    # _CHROME_TOOLBAR_HEIGHT_PX taller than config.height, since SckCapture
    # is constructed with crop_top=_CHROME_TOOLBAR_HEIGHT_PX and crops
    # exactly that many rows off the top of every captured frame. Drift
    # between the two leaves a toolbar sliver or a black bar on the wall.
    display = DisplayInfo(display_id=1, x=100, y=200, width=3840, height=2160)
    config = BroadcasterConfig(width=3840, height=2160)

    width, height = _sck_chrome_window_size(display, config)

    assert width == display.width
    assert height == config.height + _CHROME_TOOLBAR_HEIGHT_PX


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
