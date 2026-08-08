from pathlib import Path

from ndi_broadcaster.config import BroadcasterConfig, load_broadcaster_config

BROADCASTER_YAML = Path(__file__).resolve().parent.parent / "config" / "broadcaster.yaml"


def test_load_broadcaster_config():
    config = load_broadcaster_config(BROADCASTER_YAML)
    assert config == BroadcasterConfig(
        target_url="https://localhost:8443/",
        capture_backend="cdp",
        ndi_source_name="Layout Driver",
        width=3840,
        height=2160,
        fps=30,
        healthz_timeout_seconds=30.0,
    )


def test_broadcaster_config_defaults():
    config = BroadcasterConfig()
    assert config.capture_backend == "cdp"
    assert config.width == 3840
    assert config.height == 2160
    assert config.fps == 30
