from pathlib import Path

import pytest
from pydantic import ValidationError

from ndi_broadcaster.config import BroadcasterConfig, load_broadcaster_config

BROADCASTER_YAML = Path(__file__).resolve().parent.parent / "config" / "broadcaster.yaml"


def test_load_broadcaster_config():
    config = load_broadcaster_config(BROADCASTER_YAML)
    assert config == BroadcasterConfig(
        target_url="https://localhost:8443/",
        capture_backend="sck",
        sck_display_mode="virtual",
        sck_virtual_display_name="Layout Driver Virtual Display",
        sck_physical_display_name=None,
        ndi_source_name="Layout Driver",
        width=3840,
        height=2160,
        fps=30,
        healthz_timeout_seconds=30.0,
        timecode_enabled=True,
        timecode_position="top",
    )


def test_broadcaster_config_defaults():
    config = BroadcasterConfig()
    assert config.capture_backend == "cdp"
    assert config.width == 3840
    assert config.height == 2160
    assert config.fps == 30


def test_capture_backend_accepts_sck():
    # 'sck' is a real, valid backend name — just not implemented yet, which the
    # launcher reports separately. The config model must not reject it.
    assert BroadcasterConfig(capture_backend="sck").capture_backend == "sck"


def test_capture_backend_rejects_unknown_value():
    with pytest.raises(ValidationError):
        BroadcasterConfig(capture_backend="bogus")


def test_sck_fields_have_documented_defaults():
    config = BroadcasterConfig()
    assert config.sck_display_mode is None
    assert config.sck_virtual_display_name == "Layout Driver Virtual Display"
    assert config.sck_physical_display_name is None


def test_sck_display_mode_rejects_unknown_value():
    with pytest.raises(ValidationError):
        BroadcasterConfig(sck_display_mode="bogus")


def test_timecode_fields_default_to_enabled_top():
    config = BroadcasterConfig()
    assert config.timecode_enabled is True
    assert config.timecode_position == "top"


def test_timecode_position_rejects_unknown_value():
    with pytest.raises(ValidationError):
        BroadcasterConfig(timecode_position="middle")
