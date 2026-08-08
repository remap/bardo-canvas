from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image


def decode_screencast_frame(base64_data: str) -> np.ndarray:
    raw = base64.b64decode(base64_data)
    image = Image.open(io.BytesIO(raw)).convert("RGBA")
    return np.array(image)
