from pathlib import Path

from layout_server.main import REPO_ROOT, _log_format, resolve_settings


def test_resolve_settings_defaults():
    settings = resolve_settings({})
    assert settings.host == "0.0.0.0"
    assert settings.port == 8443
    assert settings.app_static_dir == REPO_ROOT / "apps" / "test-pattern" / "static"
    assert settings.screens_yaml == REPO_ROOT / "config" / "screens.yaml"
    assert settings.audio_yaml == REPO_ROOT / "config" / "audio.yaml"
    assert settings.runtime_dir == REPO_ROOT / "runtime"
    assert settings.cert_path == REPO_ROOT / "runtime" / "cert.pem"
    assert settings.key_path == REPO_ROOT / "runtime" / "key.pem"


def test_resolve_settings_env_overrides():
    settings = resolve_settings(
        {
            "LAYOUT_DRIVER_HOST": "127.0.0.1",
            "LAYOUT_DRIVER_PORT": "9000",
            "APP_DIR": "/tmp/custom-app",
            "LAYOUT_DRIVER_RUNTIME_DIR": "/tmp/custom-runtime",
        }
    )
    assert settings.host == "127.0.0.1"
    assert settings.port == 9000
    assert settings.app_static_dir == Path("/tmp/custom-app")
    assert settings.runtime_dir == Path("/tmp/custom-runtime")
    assert settings.cert_path == Path("/tmp/custom-runtime/cert.pem")


def test_log_format_without_port_matches_original_format():
    assert _log_format({}) == "%(asctime)s %(levelname)s %(name)s: %(message)s"


def test_log_format_with_port_adds_prefix():
    assert _log_format({"LAYOUT_DRIVER_PORT": "8444"}) == (
        "%(asctime)s [:8444] %(levelname)s %(name)s: %(message)s"
    )
