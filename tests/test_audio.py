from pathlib import Path

from layout_server.audio import (
    AudioConfig,
    AudioDevice,
    discover_audio_devices,
    load_audio_config,
    match_device_by_name,
    write_audio_devices_file,
)

AUDIO_YAML = Path(__file__).resolve().parent.parent / "config" / "audio.yaml"


def test_load_audio_config():
    config = load_audio_config(AUDIO_YAML)
    assert config == AudioConfig(
        enabled=True, input_device="BlackHole 2ch", output_device="BlackHole 2ch"
    )


def test_match_device_by_name_exact_case_insensitive():
    devices = [
        AudioDevice(index=0, name="BlackHole 2ch", max_input_channels=2),
        AudioDevice(index=1, name="MacBook Pro Speakers", max_output_channels=2),
    ]
    match = match_device_by_name("blackhole 2ch", devices)
    assert match is not None
    assert match.index == 0


def test_match_device_by_name_substring():
    devices = [AudioDevice(index=0, name="BlackHole 2ch", max_input_channels=2)]
    match = match_device_by_name("blackhole", devices)
    assert match is not None
    assert match.index == 0


def test_match_device_by_name_not_found_returns_none():
    devices = [AudioDevice(index=0, name="BlackHole 2ch", max_input_channels=2)]
    assert match_device_by_name("nonexistent device", devices) is None


def test_match_device_by_name_empty_name_returns_none():
    devices = [AudioDevice(index=0, name="BlackHole 2ch", max_input_channels=2)]
    assert match_device_by_name("", devices) is None


def test_discover_audio_devices_splits_inputs_and_outputs(monkeypatch):
    fake_devices = [
        {"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2},
        {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0},
    ]

    class _FakeSoundDevice:
        @staticmethod
        def query_devices():
            return fake_devices

    monkeypatch.setattr("layout_server.audio.sd", _FakeSoundDevice)

    devices = discover_audio_devices()

    assert [d.name for d in devices.inputs] == ["BlackHole 2ch", "MacBook Pro Microphone"]
    assert [d.name for d in devices.outputs] == ["BlackHole 2ch", "MacBook Pro Speakers"]
    assert devices.inputs[0].index == 0
    assert devices.outputs[1].index == 1


def test_write_audio_devices_file(tmp_path):
    from layout_server.audio import AudioDeviceList

    devices = AudioDeviceList(
        inputs=[AudioDevice(index=0, name="BlackHole 2ch", max_input_channels=2)],
        outputs=[AudioDevice(index=0, name="BlackHole 2ch", max_output_channels=2)],
    )
    out_path = tmp_path / "audio_devices.json"
    write_audio_devices_file(devices, out_path)

    assert out_path.exists()
    assert "BlackHole 2ch" in out_path.read_text()
