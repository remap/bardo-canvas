from __future__ import annotations

import numpy as np
import sounddevice as sd
from cyndilib.audio_frame import AudioSendFrame
from cyndilib.sender import Sender

from layout_server.audio import AudioConfig, AudioDevice, match_device_by_name


def resolve_input_device(config: AudioConfig, devices: list[AudioDevice]) -> AudioDevice | None:
    if not config.enabled:
        return None
    return match_device_by_name(config.input_device, devices)


class AudioSender:
    def __init__(
        self, sender: Sender, device: AudioDevice, sample_rate: int = 48000, channels: int = 2
    ) -> None:
        self._audio_frame = AudioSendFrame()
        self._audio_frame.sample_rate = sample_rate
        self._audio_frame.num_channels = channels

        # cyndilib's Sender.set_audio_frame() raises "Cannot add frame while
        # sender is open" if the sender was already opened (as it is by the
        # time launcher.py's run() gets here, since VideoSender.__init__
        # already called sender.open() for the video frame). Sender.open()
        # is also what actually attaches/allocates the audio frame's internal
        # buffers (Sender.set_audio_frame() alone does not), so a sender that
        # was already running has to be briefly closed and reopened to pick
        # up the newly-attached audio frame; the existing video frame is
        # reattached the same way when open() runs again.
        was_running = sender._running
        if was_running:
            sender.close()
        sender.set_audio_frame(self._audio_frame)
        if was_running:
            sender.open()
        self._sender = sender

        self._stream = sd.InputStream(
            device=device.index,
            channels=channels,
            samplerate=sample_rate,
            callback=self._on_audio,
        )

    def _on_audio(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        # sounddevice delivers (num_samples, num_channels) float32; cyndilib's
        # AudioSendFrame.write_data expects a 2-d float32 array/memoryview shaped
        # (num_channels, num_samples), not raw bytes.
        data = np.ascontiguousarray(indata.T, dtype=np.float32)
        self._audio_frame.write_data(data)
        self._sender.send_audio()

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.close()
