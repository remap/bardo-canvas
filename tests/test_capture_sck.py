import pytest

pytest.importorskip("ScreenCaptureKit")

from ndi_broadcaster.capture_sck import bgra_buffer_to_rgba_bytes


def test_bgra_buffer_to_rgba_bytes_reorders_channels_and_strips_row_padding():
    # 2x1 image; bytes_per_row is padded to 12 bytes (one extra BGRA pixel of
    # padding) to prove the stride is stripped rather than corrupting the row.
    pixel0 = bytes([10, 20, 30, 255])  # B, G, R, A
    pixel1 = bytes([40, 50, 60, 255])
    padding = bytes([0, 0, 0, 0])
    raw = pixel0 + pixel1 + padding

    result = bgra_buffer_to_rgba_bytes(raw, width=2, height=1, bytes_per_row=12)

    assert result == bytes([30, 20, 10, 255, 60, 50, 40, 255])
