from pathlib import Path

import numpy as np
import pytest

from ndi_broadcaster.timecode_overlay import _FONT_PATH, TimecodeOverlay, _format_timecode

requires_menlo = pytest.mark.skipif(
    not Path(_FONT_PATH).exists(), reason="Menlo.ttc is only guaranteed present on macOS"
)


def test_format_timecode_zero():
    assert _format_timecode(0.0, fps=30) == "00:00:00:00"


def test_format_timecode_one_second():
    assert _format_timecode(1.0, fps=30) == "00:00:01:00"


def test_format_timecode_over_an_hour():
    assert _format_timecode(3661.5, fps=30) == "01:01:01:15"


def test_disabled_overlay_construction_never_touches_the_font():
    # If this didn't skip font loading, it would raise on a non-macOS box.
    overlay = TimecodeOverlay(enabled=False, position="top", width=1000, height=300, fps=30)
    frame = np.random.default_rng(0).integers(0, 256, size=(300, 1000, 4), dtype=np.uint8)
    before = frame.copy()
    overlay.snapshot(frame)
    overlay.apply(frame)
    assert np.array_equal(frame, before)


@requires_menlo
@pytest.mark.parametrize("position", ["top", "bottom"])
def test_enabled_overlay_bounded_blast_radius(position, monkeypatch):
    monkeypatch.setattr("ndi_broadcaster.timecode_overlay.time.monotonic", lambda: 0.0)
    overlay = TimecodeOverlay(enabled=True, position=position, width=1000, height=300, fps=30)
    frame = np.full((300, 1000, 4), 128, dtype=np.uint8)
    before = frame.copy()
    overlay.snapshot(frame)
    overlay.apply(frame)

    outside_actual = frame.copy()
    outside_actual[overlay._y0 : overlay._y1, overlay._x0 : overlay._x1] = 128
    assert np.array_equal(outside_actual, before)

    region = frame[overlay._y0 : overlay._y1, overlay._x0 : overlay._x1]
    region_before = before[overlay._y0 : overlay._y1, overlay._x0 : overlay._x1]
    assert not np.array_equal(region, region_before)


@requires_menlo
def test_apply_does_not_alter_the_alpha_channel(monkeypatch):
    monkeypatch.setattr("ndi_broadcaster.timecode_overlay.time.monotonic", lambda: 0.0)
    overlay = TimecodeOverlay(enabled=True, position="top", width=1000, height=300, fps=30)
    frame = np.full((300, 1000, 4), 200, dtype=np.uint8)
    overlay.snapshot(frame)
    overlay.apply(frame)
    assert np.all(frame[:, :, 3] == 200)


@requires_menlo
def test_repeated_apply_on_the_same_frame_does_not_drift(monkeypatch):
    monkeypatch.setattr("ndi_broadcaster.timecode_overlay.time.monotonic", lambda: 0.0)
    overlay = TimecodeOverlay(enabled=True, position="top", width=1000, height=300, fps=30)
    frame = np.full((300, 1000, 4), 128, dtype=np.uint8)
    overlay.snapshot(frame)
    overlay.apply(frame)
    first_apply = frame.copy()
    overlay.apply(frame)
    assert np.array_equal(frame, first_apply)
