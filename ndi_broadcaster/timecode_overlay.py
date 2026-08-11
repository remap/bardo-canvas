from __future__ import annotations

import time
from typing import Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_TIMECODE_TEMPLATE = "00:00:00:00"
_GLYPH_CHARS = "0123456789:"
_FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
_FONT_SIZE_PX = 96
_EDGE_MARGIN_PX = 32
_BLEND_ALPHA = 0.5
_WHITE = (255, 255, 255, 255)


def _format_timecode(elapsed_seconds: float, fps: int) -> str:
    total_seconds = int(elapsed_seconds)
    ff = int(elapsed_seconds * fps) % fps
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def _render_glyph_atlas(font: ImageFont.FreeTypeFont, cell_width: int, cell_height: int) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Rasterize each possible character once, returning blend-ready arrays.

    Called only from TimecodeOverlay.__init__ (11 calls total per process
    lifetime, never per frame) -- this is the one place PIL actually runs.
    Each entry is (white_contribution, one_minus_alpha): precomputing the
    glyph's constant half of the blend equation here means apply() only has
    to multiply/add against the frame's own (per-frame-varying) background.
    """
    atlas: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for ch in _GLYPH_CHARS:
        image = Image.new("RGBA", (cell_width, cell_height), (0, 0, 0, 0))
        ImageDraw.Draw(image).text((0, 0), ch, font=font, fill=_WHITE)
        glyph = np.array(image, dtype=np.uint8)
        coverage = glyph[:, :, 3:4].astype(np.float32) / 255.0
        alpha_scaled = coverage * _BLEND_ALPHA
        white_contribution = np.broadcast_to(255.0 * alpha_scaled, (cell_height, cell_width, 3))
        one_minus_alpha = 1.0 - alpha_scaled
        atlas[ch] = (white_contribution, one_minus_alpha)
    return atlas


class TimecodeOverlay:
    """Burns an elapsed-time hh:mm:ss:ff timecode into frames before send.

    Uses precomputed glyph bitmaps (rasterized once via Pillow at
    construction time, see _render_glyph_atlas) rather than calling into any
    imaging/vision library per frame. Pillow is already a base dependency of
    this repo (capture_cdp.py uses it for JPEG/PNG decode) with no known
    conflict with the NDI SDK's bundled ffmpeg -- unlike opencv-python-headless,
    whose bundled libavdevice defines Objective-C classes that collide with
    NDI's own bundled ffmpeg and degraded SCK capture reliability (see
    docs/superpowers/specs/2026-08-10-timecode-burn-in-design.md, Section 11).
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
        self._cell_width = self._cell_height = 0
        self._glyphs: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        if not enabled:
            return

        font = ImageFont.truetype(_FONT_PATH, _FONT_SIZE_PX, index=0)
        ascent, descent = font.getmetrics()
        # Menlo is monospaced -- every character's advance width is identical,
        # so one measurement fixes the cell size for the whole template.
        self._cell_width = round(font.getlength("0"))
        self._cell_height = ascent + descent
        self._glyphs = _render_glyph_atlas(font, self._cell_width, self._cell_height)

        text_width = self._cell_width * len(_TIMECODE_TEMPLATE)
        region_width = text_width + 2 * _EDGE_MARGIN_PX
        region_height = self._cell_height + 2 * _EDGE_MARGIN_PX
        self._x0 = (width - region_width) // 2
        self._x1 = self._x0 + region_width
        if position == "top":
            self._y0 = 0
            self._y1 = region_height
        else:
            self._y0 = height - region_height
            self._y1 = height
        self._text_x = _EDGE_MARGIN_PX
        self._text_y = _EDGE_MARGIN_PX

    def snapshot(self, frame: np.ndarray) -> None:
        """Cache a pristine copy of the overlay region from a freshly decoded frame.

        Must run before any apply() has touched this frame -- see apply()'s
        docstring for why repeated sends need a clean baseline to blend from.
        """
        if not self._enabled:
            return
        self._clean_patch = frame[self._y0 : self._y1, self._x0 : self._x1].copy()

    def apply(self, frame: np.ndarray) -> None:
        """Restore the clean patch, then blend in the current elapsed timecode.

        Runs on every send, fresh frame or repeated/stale frame object alike.
        Restoring the clean patch first (rather than drawing on top of
        whatever the previous apply() call left behind) prevents the overlay
        from compounding blends across repeated sends of the same frame
        object -- the common case, since _LatestFrameSlot re-sends the last
        known-good frame rather than stalling NDI output.
        """
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

        for index, ch in enumerate(text):
            white_contribution, one_minus_alpha = self._glyphs[ch]
            gx0 = self._text_x + index * self._cell_width
            gx1 = gx0 + self._cell_width
            gy0 = self._text_y
            gy1 = gy0 + self._cell_height
            # Only the RGB channels are touched -- the frame's own alpha
            # channel (expected opaque) is never read or written here, so
            # there is no equivalent of cv2.putText's alpha-corruption bug.
            cell = region[gy0:gy1, gx0:gx1, :3]
            # A frame smaller than config.width/height (never happens with a
            # real capture backend, but some tests feed in a placeholder
            # frame) clips `cell` below the glyph's precomputed size --
            # clip the glyph arrays to match rather than let the mismatched
            # shapes fail to broadcast.
            rows, cols = cell.shape[:2]
            if rows == 0 or cols == 0:
                continue
            cell_rgb = cell.astype(np.float32)
            blended = white_contribution[:rows, :cols] + cell_rgb * one_minus_alpha[:rows, :cols]
            cell[:] = blended.astype(np.uint8)
