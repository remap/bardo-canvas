import pytest

pytest.importorskip("ScreenCaptureKit")

from ndi_broadcaster.capture_sck import (
    SckCapture,
    _wait_for_target_window,
    bgra_buffer_to_rgba_bytes,
)


def test_bgra_buffer_to_rgba_bytes_reorders_channels_and_strips_row_padding():
    # 2x1 image; bytes_per_row is padded to 12 bytes (one extra BGRA pixel of
    # padding) to prove the stride is stripped rather than corrupting the row.
    pixel0 = bytes([10, 20, 30, 255])  # B, G, R, A
    pixel1 = bytes([40, 50, 60, 255])
    padding = bytes([0, 0, 0, 0])
    raw = pixel0 + pixel1 + padding

    result = bgra_buffer_to_rgba_bytes(raw, width=2, height=1, bytes_per_row=12)

    assert result == bytes([30, 20, 10, 255, 60, 50, 40, 255])


def test_bgra_buffer_to_rgba_bytes_crops_rows_off_the_top():
    # 1x3 image (bytes_per_row exactly matches width, no padding); crop_top=1
    # must drop the first row entirely, keeping only the bottom two.
    row0 = bytes([1, 2, 3, 255])  # dropped
    row1 = bytes([10, 20, 30, 255])
    row2 = bytes([40, 50, 60, 255])
    raw = row0 + row1 + row2

    result = bgra_buffer_to_rgba_bytes(raw, width=1, height=3, bytes_per_row=4, crop_top=1)

    assert result == bytes([30, 20, 10, 255, 60, 50, 40, 255])


class _FakeWindow:
    def __init__(self, title):
        self._title = title

    def title(self):
        return self._title


class _FakeContent:
    def __init__(self, titles):
        self._titles = titles

    def windows(self):
        return [_FakeWindow(t) for t in self._titles]


def test_wait_for_target_window_retries_until_the_title_propagates(monkeypatch):
    # Reproduces a real, live-observed race: document.title set via
    # page.evaluate() takes a moment to propagate into SCShareableContent's
    # window snapshot, so the first poll still sees the page's original
    # <title> and only the next one sees the renamed window.
    contents = [
        _FakeContent(["Layout Driver — Test Pattern"]),
        _FakeContent(["Layout Driver Broadcaster"]),
    ]
    calls = []

    def fake_get_shareable_content():
        calls.append(1)
        return contents.pop(0)

    monkeypatch.setattr(
        "ndi_broadcaster.capture_sck._get_shareable_content", fake_get_shareable_content
    )

    window = _wait_for_target_window("Layout Driver Broadcaster", timeout_s=5.0, poll_interval_s=0.01)

    assert window.title() == "Layout Driver Broadcaster"
    assert len(calls) == 2


def test_wait_for_target_window_raises_after_timeout(monkeypatch):
    monkeypatch.setattr(
        "ndi_broadcaster.capture_sck._get_shareable_content",
        lambda: _FakeContent(["Some Other Window"]),
    )

    with pytest.raises(ValueError, match="Some Other Window"):
        _wait_for_target_window("Layout Driver Broadcaster", timeout_s=0.05, poll_interval_s=0.01)


def test_build_stream_config_requests_height_plus_crop_top():
    # The other link in the Chrome-toolbar-crop chain (see
    # launcher.py's _sck_chrome_window_size and _CHROME_TOOLBAR_HEIGHT_PX):
    # the SCStream must be asked for exactly height + crop_top rows, since
    # _StreamOutput crops crop_top rows back off every delivered frame. A
    # real SCStreamConfiguration is safe to build here -- unlike start(),
    # this needs no target window and no Screen Recording permission.
    capture = SckCapture(
        "Layout Driver Broadcaster", width=3840, height=2160, fps=30, on_frame=lambda b: None, crop_top=87
    )

    config = capture._build_stream_config()

    assert config.width() == 3840
    assert config.height() == 2160 + 87
