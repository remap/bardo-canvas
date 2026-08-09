from __future__ import annotations

import base64
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


def decode_screencast_frame(
    base64_data: str, target_width: int | None = None, target_height: int | None = None
) -> np.ndarray:
    """Decode a CDP screencast frame to an RGBA array.

    CDP's ``maxWidth``/``maxHeight`` are upper bounds, not guarantees, so a display
    smaller than the configured capture resolution delivers undersized frames. When
    target dimensions are supplied the decoded image is resized to match them, so a
    wrong-shaped frame can never reach the NDI sender.
    """
    global _logged_first_frame, _warned_size_mismatch, _last_debug_dump
    raw = base64.b64decode(base64_data)
    image = Image.open(io.BytesIO(raw)).convert("RGBA")
    if not _logged_first_frame:
        logger.info("First captured screencast frame is %dx%d", *image.size)
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
