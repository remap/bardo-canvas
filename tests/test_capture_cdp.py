import base64
import io

from PIL import Image

from ndi_broadcaster.capture_cdp import decode_screencast_frame


def test_decode_screencast_frame_roundtrip():
    original = Image.new("RGB", (16, 8), color=(10, 20, 30))
    buffer = io.BytesIO()
    original.save(buffer, format="JPEG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

    decoded = decode_screencast_frame(encoded)

    assert decoded.shape == (8, 16, 4)
    assert decoded.dtype.name == "uint8"
