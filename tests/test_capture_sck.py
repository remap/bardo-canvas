import pytest

pytest.importorskip("ScreenCaptureKit")

from ndi_broadcaster.capture_sck import _wait_for_target_window, bgra_buffer_to_rgba_bytes


def test_bgra_buffer_to_rgba_bytes_reorders_channels_and_strips_row_padding():
    # 2x1 image; bytes_per_row is padded to 12 bytes (one extra BGRA pixel of
    # padding) to prove the stride is stripped rather than corrupting the row.
    pixel0 = bytes([10, 20, 30, 255])  # B, G, R, A
    pixel1 = bytes([40, 50, 60, 255])
    padding = bytes([0, 0, 0, 0])
    raw = pixel0 + pixel1 + padding

    result = bgra_buffer_to_rgba_bytes(raw, width=2, height=1, bytes_per_row=12)

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
