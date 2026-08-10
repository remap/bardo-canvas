# Timecode Burn-In Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Burn a configurable `hh:mm:ss:ff` timecode overlay into every frame
sent over NDI, on by default, with negligible per-frame cost and a true
no-op when disabled.

**Architecture:** A new `TimecodeOverlay` class (`ndi_broadcaster/timecode_overlay.py`)
is constructed once inside the existing `_sender_thread_loop`
(`ndi_broadcaster/launcher.py`) — the single point both capture backends
(`cdp` and `sck`) already funnel through before `sender.send()`. It uses
`cv2.putText`/`cv2.addWeighted` (OpenCV, not hand-rolled glyph rendering)
bounded to a small precomputed region, with a snapshot/restore step so
re-sending the same frame object (the common case when nothing new has
captured) never compounds the blend.

**Tech Stack:** `opencv-python-headless` (new base dependency), numpy
(already present).

## Global Constraints

- `timecode_enabled: bool = True` — on by default.
- `timecode_position: Literal["top", "bottom"] = "top"`.
- Disabled must be a true no-op: `TimecodeOverlay.snapshot()` and `.apply()`
  each start with `if not self._enabled: return`, touching zero pixels and
  doing zero cv2 work when disabled.
- No new parameter on `_sender_thread_loop` — `TimecodeOverlay` reads
  `config.timecode_enabled` / `config.timecode_position` / `config.width` /
  `config.height` / `config.fps` directly, so neither `_capture_loop` (cdp)
  nor `_capture_loop_sck` (sck) call sites change at all.
- Fixed sizing (not dynamically scaled per resolution — every config in
  this repo targets 3840×2160): `font_scale=2.0`, `thickness=3`,
  `edge_margin_px=32`, font `cv2.FONT_HERSHEY_SIMPLEX`, blend alpha `0.5`.
- Timecode is elapsed time since the sender thread's first `apply()` call
  (a session stopwatch), non-drop-frame `hh:mm:ss:ff`, `ff` wrapping at
  `config.fps`. Not wall-clock time of day.
- Horizontal placement is always centered; `timecode_position` only picks
  top vs. bottom margin, per the approved design (no other positions in
  scope).
- `opencv-python-headless` (not full `opencv-python`) goes in `pyproject.toml`'s
  base `[project]` dependencies, not an extra — confirmed resolvable at
  `5.0.0.93` via `uv pip install --dry-run "opencv-python-headless"`.

---

## File Structure

- `ndi_broadcaster/config.py` — **modify**: add `timecode_enabled`,
  `timecode_position` to `BroadcasterConfig`.
- `config/broadcaster.yaml` — **modify**: document both fields.
- `pyproject.toml` — **modify**: add `opencv-python-headless`.
- `ndi_broadcaster/timecode_overlay.py` — **new**: `_format_timecode()`,
  `TimecodeOverlay`.
- `ndi_broadcaster/launcher.py` — **modify**: `_sender_thread_loop`
  constructs a `TimecodeOverlay` and calls `snapshot()`/`apply()` at the
  two points identified in Task 3.
- `tests/test_broadcaster_config.py` — **modify**: cover the new fields.
- `tests/test_timecode_overlay.py` — **new**.
- `tests/test_launcher.py` — **modify**: confirm `_sender_thread_loop`
  still behaves correctly with the overlay wired in (disabled-by-config
  case, to avoid needing a real cv2-rendered frame in this file's existing
  fake-sender tests).

---

### Task 1: Config schema and dependency

**Files:**
- Modify: `ndi_broadcaster/config.py`
- Modify: `config/broadcaster.yaml`
- Modify: `pyproject.toml`
- Modify: `tests/test_broadcaster_config.py`

**Interfaces:**
- Produces: `BroadcasterConfig.timecode_enabled: bool`,
  `BroadcasterConfig.timecode_position: Literal["top", "bottom"]`. Task 3
  reads both directly off a `BroadcasterConfig` instance.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_broadcaster_config.py`:

```python
def test_timecode_fields_default_to_enabled_top():
    config = BroadcasterConfig()
    assert config.timecode_enabled is True
    assert config.timecode_position == "top"


def test_timecode_position_rejects_unknown_value():
    with pytest.raises(ValidationError):
        BroadcasterConfig(timecode_position="middle")
```

Also update the existing `test_load_broadcaster_config` to include the two
new fields in its expected `BroadcasterConfig(...)`:

```python
def test_load_broadcaster_config():
    config = load_broadcaster_config(BROADCASTER_YAML)
    assert config == BroadcasterConfig(
        target_url="https://localhost:8443/",
        capture_backend="cdp",
        sck_display_mode=None,
        sck_virtual_display_name="Layout Driver Virtual Display",
        sck_physical_display_name=None,
        ndi_source_name="Layout Driver",
        width=3840,
        height=2160,
        fps=30,
        healthz_timeout_seconds=30.0,
        timecode_enabled=True,
        timecode_position="top",
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_broadcaster_config.py -v`
Expected: `test_timecode_fields_default_to_enabled_top` and
`test_timecode_position_rejects_unknown_value` FAIL (fields don't exist
yet). `test_load_broadcaster_config` may or may not fail at this stage —
`BroadcasterConfig` uses pydantic's default `extra="ignore"`, so passing
the two new keyword arguments before the fields exist is silently accepted
rather than rejected; the two new tests are what actually prove TDD rigor
here.

- [ ] **Step 3: Add the fields to `BroadcasterConfig`**

In `ndi_broadcaster/config.py`, add after `healthz_timeout_seconds`:

```python
class BroadcasterConfig(BaseModel):
    target_url: str = "https://localhost:8443/"
    capture_backend: Literal["cdp", "sck"] = "cdp"
    sck_display_mode: Literal["virtual", "physical"] | None = None
    sck_virtual_display_name: str = "Layout Driver Virtual Display"
    sck_physical_display_name: str | None = None
    ndi_source_name: str = "Layout Driver"
    width: int = 3840
    height: int = 2160
    fps: int = 30
    healthz_timeout_seconds: float = 30.0
    timecode_enabled: bool = True
    timecode_position: Literal["top", "bottom"] = "top"
```

- [ ] **Step 4: Document the fields in `config/broadcaster.yaml`**

Append to the end of `config/broadcaster.yaml`:

```yaml
timecode_enabled: true                          # burn-in hh:mm:ss:ff overlay for monitoring
timecode_position: "top"                        # "top" | "bottom"
```

- [ ] **Step 5: Add the dependency**

In `pyproject.toml`, add to the `dependencies` list (after `"numpy>=2.1"`,
before the PyObjC entries):

```toml
    "opencv-python-headless>=5.0.0",
```

Run: `uv sync`
Expected: resolves and installs `opencv-python-headless` (confirmed
resolvable at `5.0.0.93` at the time this plan was written — if that exact
floor fails to resolve, drop to whatever `uv sync` actually resolves rather
than pinning blind).

Run: `uv run python3 -c "import cv2; print(cv2.__version__)"`
Expected: prints a version string with no traceback.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_broadcaster_config.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 7: Commit**

```bash
git add ndi_broadcaster/config.py config/broadcaster.yaml pyproject.toml uv.lock tests/test_broadcaster_config.py
git commit -m "ndi_broadcaster: add timecode config fields and opencv-python-headless"
```

---

### Task 2: `TimecodeOverlay` (rendering + snapshot/restore)

**Files:**
- Create: `ndi_broadcaster/timecode_overlay.py`
- Create: `tests/test_timecode_overlay.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure numpy/cv2 module).
- Produces: `_format_timecode(elapsed_seconds: float, fps: int) -> str`;
  `TimecodeOverlay(enabled: bool, position: Literal["top", "bottom"],
  width: int, height: int, fps: int)` with `.snapshot(frame: np.ndarray) ->
  None` and `.apply(frame: np.ndarray) -> None`, both mutating `frame` in
  place. Task 3 constructs one `TimecodeOverlay` per `_sender_thread_loop`
  call and calls these two methods at fixed points in that loop.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_timecode_overlay.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_timecode_overlay.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ndi_broadcaster.timecode_overlay'`.

- [ ] **Step 3: Create `ndi_broadcaster/timecode_overlay.py`**

```python
from __future__ import annotations

import time
from typing import Literal

import cv2
import numpy as np

_TIMECODE_TEMPLATE = "00:00:00:00"
_FONT = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE = 2.0
_THICKNESS = 3
_EDGE_MARGIN_PX = 32
_BLEND_ALPHA = 0.5
_WHITE_RGBA = (255, 255, 255, 255)


def _format_timecode(elapsed_seconds: float, fps: int) -> str:
    """Format elapsed time as non-drop-frame hh:mm:ss:ff, wrapping ff at fps."""
    total_seconds = int(elapsed_seconds)
    ff = int(elapsed_seconds * fps) % fps
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


class TimecodeOverlay:
    """Burns a live hh:mm:ss:ff timecode into frames before they're sent.

    Construct once per _sender_thread_loop call. snapshot() must be called
    every time a genuinely new frame is decoded (before any overlay has
    touched it); apply() must be called on every send, fresh frame or
    repeated stale one alike -- see the module-level design note in
    launcher.py's _sender_thread_loop for why the split exists.
    """

    def __init__(
        self,
        enabled: bool,
        position: Literal["top", "bottom"],
        width: int,
        height: int,
        fps: int,
    ) -> None:
        self._enabled = enabled
        self._fps = fps
        self._start: float | None = None
        self._clean_patch: np.ndarray | None = None
        self._x0 = self._y0 = self._x1 = self._y1 = 0
        self._text_x = self._text_y = 0
        if not enabled:
            return

        (text_width, text_height), baseline = cv2.getTextSize(
            _TIMECODE_TEMPLATE, _FONT, _FONT_SCALE, _THICKNESS
        )
        region_width = text_width + 2 * _EDGE_MARGIN_PX
        region_height = text_height + baseline + 2 * _EDGE_MARGIN_PX
        self._x0 = (width - region_width) // 2
        self._x1 = self._x0 + region_width
        if position == "top":
            self._y0 = 0
            self._y1 = region_height
        else:
            self._y0 = height - region_height
            self._y1 = height
        self._text_x = _EDGE_MARGIN_PX
        self._text_y = _EDGE_MARGIN_PX + text_height

    def snapshot(self, frame: np.ndarray) -> None:
        if not self._enabled:
            return
        self._clean_patch = frame[self._y0 : self._y1, self._x0 : self._x1].copy()

    def apply(self, frame: np.ndarray) -> None:
        if not self._enabled:
            return
        if self._clean_patch is None:
            self.snapshot(frame)
        if self._start is None:
            self._start = time.monotonic()
        elapsed = time.monotonic() - self._start
        text = _format_timecode(elapsed, self._fps)

        region = frame[self._y0 : self._y1, self._x0 : self._x1]
        region[:] = self._clean_patch
        overlay = region.copy()
        cv2.putText(
            overlay,
            text,
            (self._text_x, self._text_y),
            _FONT,
            _FONT_SCALE,
            _WHITE_RGBA,
            _THICKNESS,
            cv2.LINE_AA,
        )
        # Explicit assignment into the view, not dst=region -- region is a
        # non-contiguous slice of `frame` (rows and columns both sliced), and
        # this is the portable way to write a computed array back into it.
        region[:] = cv2.addWeighted(overlay, _BLEND_ALPHA, region, 1 - _BLEND_ALPHA, 0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_timecode_overlay.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add ndi_broadcaster/timecode_overlay.py tests/test_timecode_overlay.py
git commit -m "ndi_broadcaster: add TimecodeOverlay (cv2-based, snapshot/restore blend)"
```

---

### Task 3: Wire into `_sender_thread_loop` + live verification

**Files:**
- Modify: `ndi_broadcaster/launcher.py:97-171` (`_sender_thread_loop`)
- Modify: `tests/test_launcher.py`

**Interfaces:**
- Consumes: `TimecodeOverlay` (Task 2) — constructed once per
  `_sender_thread_loop` call.

- [ ] **Step 1: Write the failing tests**

In `tests/test_launcher.py`, add these two tests. `TimecodeOverlay` itself
does not need to be imported here — both tests exercise it indirectly
through `_sender_thread_loop`, using the file's existing `_FakeSender` and
`_LatestFrameSlot`:

```python
def test_sender_thread_loop_applies_timecode_overlay_when_enabled():
    # width/height=800x600 (not a degenerate 1x1) is deliberate: the overlay
    # region has fixed margins (edge_margin_px=32 on each side) regardless of
    # frame size, so a too-small frame could make cv2.putText/addWeighted
    # operate on a zero-size slice and raise -- caught by _sender_thread_loop's
    # own try/except around the send block, which would make sender.send()
    # silently never run and this test's "at least one send happened"
    # assertion fail for a reason unrelated to what it's supposed to check.
    # 800x600 is comfortably larger than the overlay's rendered region.
    frame_slot = _LatestFrameSlot()
    frame_slot.put(b"\x01\x02\x03\x04")
    sender = _FakeSender()
    config = BroadcasterConfig(timecode_enabled=True, width=800, height=600)
    stop_event = threading.Event()
    decoded = np.full((600, 800, 4), 100, dtype=np.uint8)
    clean_reference = decoded.copy()

    thread = threading.Thread(
        target=_sender_thread_loop,
        args=(frame_slot, sender, config, stop_event),
        kwargs={"decode_fn": lambda data: decoded},
        daemon=True,
    )
    thread.start()
    time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=2.0)

    assert len(sender.sent) >= 1
    # TimecodeOverlay mutates `decoded` in place (it's the same object
    # `sender.sent` holds references to), so compare against the independent
    # clean_reference copy taken before the thread ran -- proves the real
    # TimecodeOverlay (not a stub) actually drew something in the top strip.
    assert not np.array_equal(sender.sent[0][:100, :], clean_reference[:100, :])


def test_sender_thread_loop_skips_timecode_overlay_when_disabled():
    frame_slot = _LatestFrameSlot()
    frame_slot.put(b"\x01\x02\x03\x04")
    sender = _FakeSender()
    config = BroadcasterConfig(timecode_enabled=False)
    stop_event = threading.Event()
    decoded = np.zeros((1, 1, 4), dtype=np.uint8)

    thread = threading.Thread(
        target=_sender_thread_loop,
        args=(frame_slot, sender, config, stop_event),
        kwargs={"decode_fn": lambda data: decoded},
        daemon=True,
    )
    thread.start()
    time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=2.0)

    assert len(sender.sent) >= 1
    assert np.array_equal(sender.sent[0], decoded)
```

Note: the first test above passes `width=1, height=1` specifically so
`TimecodeOverlay`'s region math (`(width - region_width) // 2`, etc.) never
has to be reasoned about against a real 3840×2160 frame in this file —
Task 2 already covers the real rendering geometry in isolation.
`config.width`/`config.height` don't need to match the actual `decoded`
array's shape for this test's purpose (it never asserts on the sent
pixels), only `sender.send()`'s own shape check would care, and `_FakeSender`
has none.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_launcher.py -v -k timecode`
Expected: `test_sender_thread_loop_applies_timecode_overlay_when_enabled`
FAILs (nothing modifies `decoded`, so it equals `clean_reference`).
`test_sender_thread_loop_skips_timecode_overlay_when_disabled` may already
pass at this stage (nothing modifies the frame either way yet) — that's
fine; Step 3 wiring it in for real, plus Task 2's dedicated tests, are what
actually prove the disabled path works.

- [ ] **Step 3: Wire `TimecodeOverlay` into `_sender_thread_loop`**

In `ndi_broadcaster/launcher.py`, add to the imports (alongside the
existing `from .capture_cdp import decode_captured_frame`):

```python
from .timecode_overlay import TimecodeOverlay
```

Replace this block in `_sender_thread_loop` (currently `launcher.py:116-128`):

```python
    decode = decode_fn or (
        lambda data: decode_captured_frame(
            data, target_width=config.width, target_height=config.height
        )
    )
    frame_interval = 1.0 / config.fps
    last_frame: np.ndarray | None = None
    next_deadline = time.monotonic()
    decodes_since_log = 0
    decode_seconds_since_log = 0.0
    sends_since_log = 0
    send_seconds_since_log = 0.0
    last_log = time.monotonic()
```

with:

```python
    decode = decode_fn or (
        lambda data: decode_captured_frame(
            data, target_width=config.width, target_height=config.height
        )
    )
    # Constructed once per loop, not per frame: TimecodeOverlay.__init__ does
    # the one-time cv2.getTextSize() region-geometry math; snapshot()/apply()
    # are the only calls in the hot path, and both are true no-ops when
    # config.timecode_enabled is False.
    timecode_overlay = TimecodeOverlay(
        enabled=config.timecode_enabled,
        position=config.timecode_position,
        width=config.width,
        height=config.height,
        fps=config.fps,
    )
    frame_interval = 1.0 / config.fps
    last_frame: np.ndarray | None = None
    next_deadline = time.monotonic()
    decodes_since_log = 0
    decode_seconds_since_log = 0.0
    sends_since_log = 0
    send_seconds_since_log = 0.0
    last_log = time.monotonic()
```

Then replace the decode/send block (currently `launcher.py:131-146`):

```python
        data = frame_slot.take()
        if data is not None:
            decode_start = time.monotonic()
            try:
                last_frame = decode(data)
            except Exception:
                logger.exception("Failed to decode a captured frame; skipping it")
            decodes_since_log += 1
            decode_seconds_since_log += time.monotonic() - decode_start
        if last_frame is not None:
            send_start = time.monotonic()
            try:
                sender.send(last_frame)
            except Exception:
                logger.exception("Failed to send a frame; skipping it")
            sends_since_log += 1
            send_seconds_since_log += time.monotonic() - send_start
```

with:

```python
        data = frame_slot.take()
        if data is not None:
            decode_start = time.monotonic()
            try:
                last_frame = decode(data)
                # snapshot() must run against a frame no overlay has ever
                # touched -- this is that moment. Only reached on a genuine
                # new decode, never on a repeated/stale send below.
                timecode_overlay.snapshot(last_frame)
            except Exception:
                logger.exception("Failed to decode a captured frame; skipping it")
            decodes_since_log += 1
            decode_seconds_since_log += time.monotonic() - decode_start
        if last_frame is not None:
            send_start = time.monotonic()
            try:
                # apply() runs on every send, fresh frame or the same
                # repeated frame object alike, so the burned-in clock keeps
                # ticking even when nothing new has been captured.
                timecode_overlay.apply(last_frame)
                sender.send(last_frame)
            except Exception:
                logger.exception("Failed to send a frame; skipping it")
            sends_since_log += 1
            send_seconds_since_log += time.monotonic() - send_start
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_launcher.py -v`
Expected: PASS (all tests in the file, including the two new ones).

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all tests PASS.

- [ ] **Step 6: Live verification**

Not unit-testable (visual appearance/legibility, per the spec's Testing
section) — run the framework against any app and confirm by eye or via a
captured frame:

```bash
./run.sh
```

Then, in a separate terminal, fetch and inspect a frame — e.g. reuse the
existing `/api/screenshot` endpoint against the plain composited page (this
exercises the page's own compositing, not the NDI-bound frame; to check the
actual NDI-bound output specifically, temporarily point an NDI monitor at
the "Layout Driver" source, or add a throwaway debug dump the way earlier
work in this session did — write the current `last_frame` to a PNG file
once, inspect it, then remove the throwaway code before committing
anything).

Expected: a semi-transparent white `hh:mm:ss:ff` counting up from
`00:00:00:00`, centered horizontally, in the configured position
(`top` by default), legible against whatever content is behind it,
advancing steadily whether or not the underlying content is changing.

Confirm `capture_backend: cdp` (config/broadcaster.yaml's committed default)
is unaffected in every other respect — same content, same fps, only the
new overlay visible.

- [ ] **Step 7: Commit**

```bash
git add ndi_broadcaster/launcher.py tests/test_launcher.py
git commit -m "ndi_broadcaster: burn timecode into every frame before send"
```
