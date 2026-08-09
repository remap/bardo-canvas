import http.server
import platform
import socketserver
import textwrap
import threading

import pytest

from ndi_broadcaster.config import BroadcasterConfig
from ndi_broadcaster.launcher import (
    REPO_ROOT,
    HealthCheckTimeoutError,
    _chrome_launch_args,
    _LatestFrameSlot,
    _log_format,
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


def test_run_rejects_unimplemented_sck_backend(tmp_path, monkeypatch):
    config_path = tmp_path / "broadcaster.yaml"
    config_path.write_text(
        textwrap.dedent("""
            target_url: "https://localhost:8443/"
            capture_backend: sck
        """)
    )
    # If the backend check did not come first, run() would try to reach the network.
    monkeypatch.setattr(
        "ndi_broadcaster.launcher.wait_for_healthy",
        lambda *args, **kwargs: pytest.fail("wait_for_healthy must not run for sck"),
    )

    with pytest.raises(NotImplementedError, match="sck"):
        run(config_path=str(config_path))


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
