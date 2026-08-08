from __future__ import annotations

import logging

import numpy as np
import sounddevice as sd
from cyndilib.audio_frame import AudioSendFrame
from cyndilib.sender import Sender

from layout_server.audio import AudioConfig, AudioDevice, match_device_by_name

logger = logging.getLogger(__name__)


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

        # The caller (launcher.py's run()) must construct the VideoSender with
        # open_immediately=False and call .open() only after this constructor
        # runs, so set_audio_frame() below never hits cyndilib's "Cannot add
        # frame while sender is open" guard.
        sender.set_audio_frame(self._audio_frame)
        self._sender = sender

        self._stream = sd.InputStream(
            device=device.index,
            channels=channels,
            samplerate=sample_rate,
            blocksize=1024,
            callback=self._on_audio,
        )

    def _on_audio(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        # sounddevice delivers (num_samples, num_channels) float32; cyndilib's
        # AudioSendFrame.write_data expects a 2-d float32 array/memoryview shaped
        # (num_channels, num_samples), not raw bytes.
        try:
            data = np.ascontiguousarray(indata.T, dtype=np.float32)
            self._audio_frame.write_data(data)
            self._sender.send_audio()
        except Exception:
            logger.exception("Failed to write/send an audio block; skipping it")

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.close()
