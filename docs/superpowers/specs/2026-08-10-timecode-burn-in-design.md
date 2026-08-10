# Timecode Burn-In — Design Spec

## 1. Purpose

A configurable `hh:mm:ss:ff` timecode overlay burned into every frame sent
over NDI, for monitoring/debugging live broadcasts (confirming the feed is
actually live and ticking, spotting frozen output at a glance). On by
default. Must add negligible CPU cost to the existing 30fps send loop, and
be a true no-op when disabled.

## 2. Scope

In scope: the overlay itself, its config, and its integration into the one
shared send path both capture backends (`cdp` and `sck`) already funnel
through. Out of scope: any capture-backend-specific code (this lives
entirely after decode, before send, so it never needs to know which backend
produced the frame).

## 3. Architecture

```
_sender_thread_loop (launcher.py)
        │
        ▼
  data = frame_slot.take()
        │
   ┌────┴────┐
   │ if data │  new frame decoded this iteration
   │ arrived │  -> last_frame = decode(data)
   └────┬────┘  -> timecode_overlay.snapshot(last_frame)
        │
        ▼
  timecode_overlay.apply(last_frame)   <- runs every iteration, fresh or
        │                                 repeated frame alike
        ▼
  sender.send(last_frame)
```

Both `snapshot()` and `apply()` start with `if not self._enabled: return`,
so a disabled overlay costs one boolean check per call and touches no
pixels.

## 4. Why a snapshot/restore step, not just draw-and-blend

`_sender_thread_loop` re-sends the same `last_frame` object, unmodified,
whenever no new frame has arrived since the last send (`_LatestFrameSlot`'s
whole design: hold the last known-good frame rather than stall NDI output).
This is the common case, not an edge case — anything that isn't
continuously animating (a static image, an idle test pattern) re-sends the
identical frame object dozens of times a second.

If `apply()` simply drew fresh text over the frame's existing pixels and
wrote the 50%-blended result back in place, every repeated send would blend
again on top of the *previous* blend — the overlay region would drift
whiter and the old digits would visibly ghost through the new ones within a
second or two of static content.

Fix: cache a small, pristine copy of the overlay's rectangular region the
moment a frame is *freshly decoded* (`snapshot()`), before any overlay has
ever touched it. Every `apply()` call restores that clean patch into the
frame first, then draws and blends on top of the restored (clean) pixels.
Every blend starts from the same true baseline no matter how many times the
frame gets re-sent. The cached patch is a few KB (the overlay region only,
not the full frame), so this is cheap regardless of the target resolution.

## 5. Rendering: OpenCV, not hand-rolled glyphs

Uses `cv2.putText()` (`opencv-python-headless`, new dependency — no GUI
backend needed, just array-drawing functions) rather than PIL or a custom
precomputed-glyph bitmap cache. `cv2.putText()` operates directly on numpy
arrays with no PIL round-trip, is implemented in optimized C++, and is the
standard tool for exactly this task (real-time video overlays — the same
pattern used for on-screen FPS counters). This replaces what would
otherwise be a hand-rolled font-rendering system with a well-tested library
call.

Per `apply()` call, bounded to the small overlay region only (not the full
frame):

```python
region = frame[y0:y1, x0:x1]
region[:] = self._clean_patch  # restore -- undo any previous blend
overlay = region.copy()
cv2.putText(
    overlay, text, (text_x, text_y),
    cv2.FONT_HERSHEY_SIMPLEX, self._font_scale,
    (255, 255, 255, 255), self._thickness, cv2.LINE_AA,
)
region[:] = cv2.addWeighted(overlay, 0.5, region, 0.5, 0)
```

(`region[:] = cv2.addWeighted(...)` rather than passing `dst=region`
directly — `region` is a non-contiguous view into `frame`, and explicit
assignment is the portable way to write a computed array back into a view
regardless of what OpenCV's `dst` parameter tolerates internally.)

The region's fixed pixel bounds are computed once at startup via
`cv2.getTextSize("00:00:00:00", ...)` (all digits in this font are already
uniform-width, so the string's rendered size is deterministic) — centered
horizontally, with a fixed margin from the top or bottom edge depending on
`timecode_position`.

Fixed sizing defaults (tuned for this repo's standard 3840×2160 target, not
dynamically scaled — see Non-goals): `font_scale=2.0`, `thickness=3`,
`edge_margin_px=32`. `TimecodeOverlay` is constructed inside
`_sender_thread_loop` itself, directly from `config.timecode_enabled` /
`config.timecode_position` / `config.width` / `config.height` / `config.fps`
— no new parameter on `_sender_thread_loop`, so both capture backends' call
sites are untouched.

## 6. Timecode semantics

Elapsed time since the sender thread starts producing frames (captured as
`time.monotonic()` on the first `apply()` call), formatted as non-drop-frame
`hh:mm:ss:ff` counting up from `00:00:00:00` — a stopwatch/session timer,
not wall-clock time of day. `ff` is the frame index within the current
second (`int(elapsed * fps) % fps`), so it wraps at `config.fps`.

## 7. Config

New fields on `BroadcasterConfig` (`ndi_broadcaster/config.py`):

```python
timecode_enabled: bool = True
timecode_position: Literal["top", "bottom"] = "top"
```

`config/broadcaster.yaml` documents both:

```yaml
timecode_enabled: true                          # burn-in hh:mm:ss:ff overlay for monitoring
timecode_position: "top"                        # "top" | "bottom"
```

## 8. Dependency

Add `opencv-python-headless` to the project's base dependencies (not an
extra) in `pyproject.toml` — `ndi_broadcaster` is core framework code, same
convention already used for the `sck` backend's PyObjC dependencies.
Headless (not full `opencv-python`) because only array-drawing functions
are needed, never a display window.

## 9. Testing

- `_format_timecode(elapsed_seconds, fps) -> str`: pure function, unit
  tested directly with fixed inputs (e.g. `elapsed=0.0` → `"00:00:00:00"`,
  `elapsed=3661.5` at `fps=30` → `"01:01:01:15"`).
- `TimecodeOverlay` disabled (`timecode_enabled=False`): `apply()` on a
  frame leaves it byte-for-byte unchanged — assert via `np.array_equal`.
- `TimecodeOverlay` enabled: after `snapshot()` + `apply()`, assert pixels
  *outside* the computed region are byte-for-byte unchanged, and pixels
  *inside* the region differ from the pre-overlay values. This verifies
  correctness (bounded blast radius, overlay actually draws something)
  without needing to visually inspect rendered glyphs in a unit test.
- Not unit tested: actual visual appearance/legibility of the rendered
  digits — verified live once, matching how every other rendering-visible
  change in this repo has been confirmed (a quick real broadcast run,
  checked by eye or via a captured frame).

## 10. Non-goals

- No horizontal-position options beyond centered (per this spec) — the
  user's request explicitly scoped position options to "top"/"bottom" "for
  now."
- No dynamic font-size scaling based on `config.width`/`config.height` —
  every config in this repo targets 3840×2160; a fixed default size is
  legible there and revisited only if a different resolution is actually
  needed.
- No frame-drop/timing-correction logic tied to the timecode itself (e.g.
  detecting and logging when `ff` skips) — this is a visual monitoring aid,
  not an instrumentation/metrics feature.
