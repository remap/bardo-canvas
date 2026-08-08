from __future__ import annotations

from fractions import Fraction

import numpy as np
from cyndilib.sender import Sender
from cyndilib.video_frame import VideoSendFrame
from cyndilib.wrapper.ndi_structs import FourCC


class VideoSender:
    def __init__(self, ndi_source_name: str, width: int, height: int, fps: int) -> None:
        self._sender = Sender(ndi_source_name)
        self._video_frame = VideoSendFrame()
        self._video_frame.set_resolution(width, height)
        self._video_frame.set_frame_rate(Fraction(fps, 1))
        self._video_frame.set_fourcc(FourCC.RGBA)
        self._sender.set_video_frame(self._video_frame)
        self._sender.open()

    def send(self, frame: np.ndarray) -> None:
        self._video_frame.write_data(np.ascontiguousarray(frame).reshape(-1))
        self._sender.send_video_async()

    def close(self) -> None:
        self._sender.close()
