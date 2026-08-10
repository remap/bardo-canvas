import pytest

pytest.importorskip("cv2")

import numpy as np

from ndi_broadcaster.timecode_overlay import TimecodeOverlay, _format_timecode


def test_format_timecode_at_zero_elapsed():
    assert _format_timecode(0.0, fps=30) == "00:00:00:00"


def test_format_timecode_wraps_frame_count_at_the_second_boundary():
    # 1.0s at 30fps is frame 30 of the elapsed stream, which wraps to ff=00
    # of the next second, not ff=30.
    assert _format_timecode(1.0, fps=30) == "00:00:01:00"


def test_format_timecode_hours_minutes_seconds_frames():
    # 3661.5s = 1h 1m 1s + 0.5s; at 30fps, 0.5s into the current second is
    # frame 15 (30 * 3661.5 = 109845; 109845 % 30 = 15).
    assert _format_timecode(3661.5, fps=30) == "01:01:01:15"


def test_timecode_overlay_disabled_leaves_frame_byte_for_byte_unchanged():
    overlay = TimecodeOverlay(enabled=False, position="top", width=3840, height=2160, fps=30)
    frame = np.full((2160, 3840, 4), 100, dtype=np.uint8)
    original = frame.copy()

    overlay.snapshot(frame)
    overlay.apply(frame)

    assert np.array_equal(frame, original)


def test_timecode_overlay_top_position_only_touches_the_top_strip():
    overlay = TimecodeOverlay(enabled=True, position="top", width=3840, height=2160, fps=30)
    frame = np.full((2160, 3840, 4), 100, dtype=np.uint8)

    overlay.snapshot(frame)
    overlay.apply(frame)

    # Far from a "top" overlay -- the bottom half must be completely untouched.
    assert np.array_equal(frame[1080:, :], np.full((1080, 3840, 4), 100, dtype=np.uint8))
    # Somewhere in the top strip, the overlay must have drawn something.
    assert not np.array_equal(frame[:200, :], np.full((200, 3840, 4), 100, dtype=np.uint8))


def test_timecode_overlay_bottom_position_only_touches_the_bottom_strip():
    overlay = TimecodeOverlay(enabled=True, position="bottom", width=3840, height=2160, fps=30)
    frame = np.full((2160, 3840, 4), 100, dtype=np.uint8)

    overlay.snapshot(frame)
    overlay.apply(frame)

    # Far from a "bottom" overlay -- the top half must be completely untouched.
    assert np.array_equal(frame[:1080, :], np.full((1080, 3840, 4), 100, dtype=np.uint8))
    # Somewhere in the bottom strip, the overlay must have drawn something.
    assert not np.array_equal(frame[1960:, :], np.full((200, 3840, 4), 100, dtype=np.uint8))


def test_timecode_overlay_apply_does_not_alter_the_alpha_channel():
    # Live-observed bug: cv2.putText does not treat channel 3 as a normal
    # color to blend on a 4-channel image -- it writes glyph antialiasing
    # coverage directly into alpha (an edge pixel's alpha can drop from 255
    # to 6). After the 0.5 addWeighted blend, alpha in the overlay region
    # ended up in the 128-255 range instead of staying uniformly opaque.
    # Since VideoSender sends FourCC.RGBA and every real decoder produces
    # fully-opaque frames, this overlay must never introduce alpha variation.
    overlay = TimecodeOverlay(enabled=True, position="top", width=3840, height=2160, fps=30)
    frame = np.full((2160, 3840, 4), 100, dtype=np.uint8)
    frame[:, :, 3] = 255

    overlay.snapshot(frame)
    overlay.apply(frame)

    assert np.all(frame[:200, :, 3] == 255)


def test_timecode_overlay_repeated_apply_does_not_compound_the_blend(monkeypatch):
    # GOTCHA this guards against: _sender_thread_loop re-sends the same frame
    # object, unmodified, whenever nothing new has been captured. If apply()
    # blended on top of its own previous output instead of restoring a clean
    # base first, repeated sends of a static frame would drift whiter each
    # time. Freezing time.monotonic() means both calls render the identical
    # digits, so a correct implementation must produce byte-identical output.
    times = iter([100.0] * 5)
    monkeypatch.setattr("ndi_broadcaster.timecode_overlay.time.monotonic", lambda: next(times))

    overlay = TimecodeOverlay(enabled=True, position="top", width=3840, height=2160, fps=30)
    frame = np.full((2160, 3840, 4), 100, dtype=np.uint8)
    overlay.snapshot(frame)

    overlay.apply(frame)
    after_first = frame[:200, :].copy()
    overlay.apply(frame)
    after_second = frame[:200, :].copy()

    assert np.array_equal(after_first, after_second)
