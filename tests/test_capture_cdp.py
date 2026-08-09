import io

from PIL import Image

from ndi_broadcaster.capture_cdp import decode_captured_frame


def test_decode_captured_frame_roundtrip():
    original = Image.new("RGB", (16, 8), color=(10, 20, 30))
    buffer = io.BytesIO()
    original.save(buffer, format="JPEG")

    decoded = decode_captured_frame(buffer.getvalue())

    assert decoded.shape == (8, 16, 4)
    assert decoded.dtype.name == "uint8"


def test_decode_captured_frame_resizes_to_target_dimensions():
    original = Image.new("RGB", (16, 8), color=(10, 20, 30))
    buffer = io.BytesIO()
    original.save(buffer, format="JPEG")

    decoded = decode_captured_frame(buffer.getvalue(), target_width=64, target_height=32)

    assert decoded.shape == (32, 64, 4)
    assert decoded.dtype.name == "uint8"


def test_decode_captured_frame_leaves_matching_dimensions_untouched():
    original = Image.new("RGB", (16, 8), color=(10, 20, 30))
    buffer = io.BytesIO()
    original.save(buffer, format="JPEG")

    decoded = decode_captured_frame(buffer.getvalue(), target_width=16, target_height=8)

    assert decoded.shape == (8, 16, 4)
