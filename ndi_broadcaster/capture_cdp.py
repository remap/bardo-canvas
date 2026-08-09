from __future__ import annotations

import io
import logging
import os
import time

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)
_logged_first_frame = False
_warned_size_mismatch = False
_DEBUG_DUMP_DIR = os.environ.get("NDI_DEBUG_DUMP_DIR")
_last_debug_dump = 0.0


def decode_captured_frame(
    image_bytes: bytes, target_width: int | None = None, target_height: int | None = None
) -> np.ndarray:
    """Decode a captured (JPEG/PNG) frame to an RGBA array.

    Headless Chrome has no physical display to be smaller than the configured capture
    resolution, so a mismatch here would indicate a real bug rather than the display-size
    quirk this guarded against under headed capture -- kept as a safety net regardless,
    since a wrong-shaped frame must never reach the NDI sender.
    """
    global _logged_first_frame, _warned_size_mismatch, _last_debug_dump
    image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    if not _logged_first_frame:
        logger.info("First captured frame is %dx%d", *image.size)
        _logged_first_frame = True
    if (
        target_width is not None
        and target_height is not None
        and image.size != (target_width, target_height)
    ):
        if not _warned_size_mismatch:
            logger.warning(
                "Captured frame %dx%d does not match configured capture resolution "
                "%dx%d; stretching to fit. This usually means the display running "
                "the kiosk window is smaller than the configured resolution.",
                image.size[0],
                image.size[1],
                target_width,
                target_height,
            )
            _warned_size_mismatch = True
        image = image.resize((target_width, target_height))
    if _DEBUG_DUMP_DIR is not None and time.monotonic() - _last_debug_dump > 5.0:
        _last_debug_dump = time.monotonic()
        image.save(os.path.join(_DEBUG_DUMP_DIR, "latest-ndi-frame.png"))
    return np.array(image)
