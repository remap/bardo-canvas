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


def test_bgra_buffer_to_rgba_bytes_bounds_the_bottom_when_target_height_given():
    # 1x4 image: crop_top=1 drops row0, target_height=2 additionally drops
    # row3 -- the leftover headroom margin below the composited wall (see
    # launcher.py's _CHROME_APP_MODE_HEADROOM_PX) -- keeping only row1/row2.
    row0 = bytes([1, 2, 3, 255])  # dropped: above crop_top
    row1 = bytes([10, 20, 30, 255])
    row2 = bytes([40, 50, 60, 255])
    row3 = bytes([70, 80, 90, 255])  # dropped: past crop_top + target_height
    raw = row0 + row1 + row2 + row3

    result = bgra_buffer_to_rgba_bytes(
        raw, width=1, height=4, bytes_per_row=4, crop_top=1, target_height=2
    )

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


def test_build_stream_config_defaults_native_capture_height_to_height_plus_crop_top():
    # With no explicit native_capture_height (the old, no-headroom-margin
    # assumption), the SCStream must be asked for exactly height + crop_top
    # rows, since _StreamOutput crops crop_top rows back off every delivered
    # frame with no bottom bound. A real SCStreamConfiguration is safe to
    # build here -- unlike start(), this needs no target window and no
    # Screen Recording permission.
    capture = SckCapture(
        "Layout Driver Broadcaster", width=3840, height=2160, fps=30, on_frame=lambda b: None, crop_top=87
    )

    config = capture._build_stream_config()

    assert config.width() == 3840
    assert config.height() == 2160 + 87


def test_build_stream_config_uses_explicit_native_capture_height_when_given():
    # launcher.py's sck path always passes this explicitly: the window is
    # launched with headroom well beyond height + crop_top (see
    # _CHROME_APP_MODE_HEADROOM_PX), and SCStreamConfiguration must request
    # that real, full native height -- not height + crop_top -- or
    # ScreenCaptureKit would have to resize what it captures to fit,
    # reintroducing exactly the soft misalignment this scheme avoids.
    capture = SckCapture(
        "Layout Driver Broadcaster",
        width=3840,
        height=2160,
        fps=30,
        on_frame=lambda b: None,
        crop_top=28,
        native_capture_height=2220,
    )

    config = capture._build_stream_config()

    assert config.width() == 3840
    assert config.height() == 2220
