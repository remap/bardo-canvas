from __future__ import annotations

from fractions import Fraction

import numpy as np
from cyndilib.sender import Sender
from cyndilib.video_frame import VideoSendFrame
from cyndilib.wrapper.ndi_structs import FourCC


class VideoSender:
    def __init__(
        self,
        ndi_source_name: str,
        width: int,
        height: int,
        fps: int,
        *,
        open_immediately: bool = True,
    ) -> None:
        self._width = width
        self._height = height
        self._sender = Sender(ndi_source_name)
        self._video_frame = VideoSendFrame()
        self._video_frame.set_resolution(width, height)
        self._video_frame.set_frame_rate(Fraction(fps, 1))
        self._video_frame.set_fourcc(FourCC.RGBA)
        self._sender.set_video_frame(self._video_frame)
        if open_immediately:
            self._sender.open()

    @property
    def sender(self) -> Sender:
        return self._sender

    @property
    def is_open(self) -> bool:
        return self._sender._running

    def open(self) -> None:
        self._sender.open()

    def send(self, frame: np.ndarray) -> None:
        # A mismatched shape does not merely fail this write: cyndilib's internal
        # buffer is left non-null, so every subsequent write raises too. Reject the
        # frame before it can reach write_data at all.
        expected_shape = (self._height, self._width, 4)
        if frame.shape != expected_shape:
            raise ValueError(
                f"frame shape {frame.shape} does not match sender resolution {expected_shape}"
            )
        self._video_frame.write_data(np.ascontiguousarray(frame).reshape(-1))
        self._sender.send_video_async()

    def close(self) -> None:
        self._sender.close()
