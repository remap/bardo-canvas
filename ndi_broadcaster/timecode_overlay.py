from __future__ import annotations

import time
from typing import Literal

import cv2
import numpy as np

_TIMECODE_TEMPLATE = "00:00:00:00"
_FONT = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE = 2.0
_THICKNESS = 3
_EDGE_MARGIN_PX = 32
_BLEND_ALPHA = 0.5
_WHITE_RGBA = (255, 255, 255, 255)


def _format_timecode(elapsed_seconds: float, fps: int) -> str:
    """Format elapsed time as non-drop-frame hh:mm:ss:ff, wrapping ff at fps."""
    total_seconds = int(elapsed_seconds)
    ff = int(elapsed_seconds * fps) % fps
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


class TimecodeOverlay:
    """Burns a live hh:mm:ss:ff timecode into frames before they're sent.

    Construct once per _sender_thread_loop call. snapshot() must be called
    every time a genuinely new frame is decoded (before any overlay has
    touched it); apply() must be called on every send, fresh frame or
    repeated stale one alike -- see the module-level design note in
    launcher.py's _sender_thread_loop for why the split exists.
    """

    def __init__(
        self,
        enabled: bool,
        position: Literal["top", "bottom"],
        width: int,
        height: int,
        fps: int,
    ) -> None:
        self._enabled = enabled
        self._fps = fps
        self._start: float | None = None
        self._clean_patch: np.ndarray | None = None
        self._x0 = self._y0 = self._x1 = self._y1 = 0
        self._text_x = self._text_y = 0
        if not enabled:
            return

        (text_width, text_height), baseline = cv2.getTextSize(
            _TIMECODE_TEMPLATE, _FONT, _FONT_SCALE, _THICKNESS
        )
        region_width = text_width + 2 * _EDGE_MARGIN_PX
        region_height = text_height + baseline + 2 * _EDGE_MARGIN_PX
        self._x0 = (width - region_width) // 2
        self._x1 = self._x0 + region_width
        if position == "top":
            self._y0 = 0
            self._y1 = region_height
        else:
            self._y0 = height - region_height
            self._y1 = height
        self._text_x = _EDGE_MARGIN_PX
        self._text_y = _EDGE_MARGIN_PX + text_height

    def snapshot(self, frame: np.ndarray) -> None:
        if not self._enabled:
            return
        self._clean_patch = frame[self._y0 : self._y1, self._x0 : self._x1].copy()

    def apply(self, frame: np.ndarray) -> None:
        if not self._enabled:
            return
        if self._clean_patch is None:
            self.snapshot(frame)
        if self._start is None:
            self._start = time.monotonic()
        elapsed = time.monotonic() - self._start
        text = _format_timecode(elapsed, self._fps)

        region = frame[self._y0 : self._y1, self._x0 : self._x1]
        region[:] = self._clean_patch
        overlay = region.copy()
        cv2.putText(
            overlay,
            text,
            (self._text_x, self._text_y),
            _FONT,
            _FONT_SCALE,
            _WHITE_RGBA,
            _THICKNESS,
            cv2.LINE_AA,
        )
        # Explicit assignment into the view, not dst=region -- region is a
        # non-contiguous slice of `frame` (rows and columns both sliced), and
        # this is the portable way to write a computed array back into it.
        region[:] = cv2.addWeighted(overlay, _BLEND_ALPHA, region, 1 - _BLEND_ALPHA, 0)
        # cv2.putText does not treat channel 3 as a normal color to blend on a
        # 4-channel image -- confirmed live: it writes glyph antialiasing
        # coverage directly into alpha (e.g. an edge pixel's alpha drops from
        # 255 to 6), so after the addWeighted blend above, alpha in this
        # region ends up in the 128-255 range instead of uniform 255. Since
        # VideoSender sends FourCC.RGBA and every real decoder here produces
        # fully-opaque frames, this overlay would otherwise be the only
        # source of alpha variation in the whole NDI output -- restoring
        # alpha from the pre-overlay clean patch keeps RGB blended while
        # alpha stays exactly what it was before the overlay touched it.
        region[:, :, 3] = self._clean_patch[:, :, 3]
