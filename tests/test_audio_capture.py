from layout_server.audio import AudioConfig, AudioDevice
from ndi_broadcaster.audio_capture import resolve_input_device


def test_resolve_input_device_finds_configured_device():
    config = AudioConfig(enabled=True, input_device="BlackHole 2ch", output_device="BlackHole 2ch")
    devices = [
        AudioDevice(index=2, name="BlackHole 2ch", max_input_channels=2),
        AudioDevice(index=0, name="MacBook Pro Microphone", max_input_channels=1),
    ]
    device = resolve_input_device(config, devices)
    assert device is not None
    assert device.index == 2


def test_resolve_input_device_returns_none_when_disabled():
    config = AudioConfig(enabled=False, input_device="BlackHole 2ch", output_device="BlackHole 2ch")
    devices = [AudioDevice(index=2, name="BlackHole 2ch", max_input_channels=2)]
    assert resolve_input_device(config, devices) is None


def test_resolve_input_device_returns_none_when_not_found():
    config = AudioConfig(
        enabled=True, input_device="Nonexistent Device", output_device="BlackHole 2ch"
    )
    devices = [AudioDevice(index=2, name="BlackHole 2ch", max_input_channels=2)]
    assert resolve_input_device(config, devices) is None
