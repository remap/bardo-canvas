from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image


def decode_screencast_frame(
    base64_data: str, target_width: int | None = None, target_height: int | None = None
) -> np.ndarray:
    """Decode a CDP screencast frame to an RGBA array.

    CDP's ``maxWidth``/``maxHeight`` are upper bounds, not guarantees, so a display
    smaller than the configured capture resolution delivers undersized frames. When
    target dimensions are supplied the decoded image is resized to match them, so a
    wrong-shaped frame can never reach the NDI sender.
    """
    raw = base64.b64decode(base64_data)
    image = Image.open(io.BytesIO(raw)).convert("RGBA")
    if (
        target_width is not None
        and target_height is not None
        and image.size != (target_width, target_height)
    ):
        image = image.resize((target_width, target_height))
    return np.array(image)
