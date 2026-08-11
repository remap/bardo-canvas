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

## 11. Addendum (2026-08-10): Section 5 reverted — opencv collides with NDI's bundled ffmpeg

The `cv2.putText()`-based rendering in Section 5 was implemented, reviewed,
merged, then **reverted** after live verification against the flux-gallery
sample app (commits `e8fc16a`, `839fe20`, `8fba372`, `0daabe5`, `4f7b761`;
reverted in `ccf9da3`). Sections 1-10 above remain the accurate design for
the feature's *behavior* (semantics, config, blast radius, snapshot/restore
architecture) — only the rendering mechanism (Section 5) and the dependency
(Section 8) are superseded.

**The problem:** `opencv-python-headless` statically bundles its own
FFmpeg (`libavdevice.61.3.100.dylib`), which defines the Objective-C
classes `AVFFrameReceiver`/`AVFAudioReceiver` for its AVFoundation capture
demuxer. The NDI SDK installed on this machine
(`/Library/Application Support/NewTek/NDI/HX_Driver/libavdevice-ndi.61.dylib`)
bundles a *different* FFmpeg build that defines the *same* class names.
Loading both into one process — which happens the moment `import cv2` runs,
regardless of whether `timecode_enabled` is true or `cv2.putText()` is ever
called — causes the Objective-C runtime to log a duplicate-class warning
("Class X is implemented in both... one of the two will be used. Which one
is undefined.") and, confirmed live, produces real, intermittent
`ScreenCaptureKit` (`SCStream`) frame-delivery failures: capture pushes that
succeeded reliably before `cv2` was imported began failing (0 decodes) after
the first push, in a flaky/non-deterministic pattern (not 100% reproducible
run to run) consistent with "spurious" ObjC method dispatch resolving to
the wrong class's implementation.

**What was ruled out:**
- `OPENCV_VIDEOIO_PRIORITY_FFMPEG=0` / `OPENCV_VIDEOIO_PRIORITY_AVFOUNDATION=0`
  — these only steer which backend `cv2.VideoCapture`/`VideoWriter` picks at
  runtime; they don't prevent the bundled dylib from loading at import time.
  Tested directly against the real broadcaster: the warning still appeared,
  and capture reliability was not restored.
- This is a known, structural, ecosystem-wide problem with no official fix:
  two macOS Python packages that each statically bundle their own FFmpeg
  build will collide if both get loaded into the same process. See
  `PyAV-Org/PyAV#2215` and `pipecat-ai/pipecat#3514`, both closed by their
  maintainers as unfixable from the affected package's side.

**Decision:** drop `opencv-python-headless` entirely. `Pillow` (already a
base dependency, already loaded in this same process via
`capture_cdp.decode_captured_frame`'s `PIL.Image.open()`, with no observed
collision) is the replacement, used only for one-time glyph rasterization —
see Section 12.

## 12. Addendum (2026-08-10): Replacement rendering approach — precomputed glyph bitmaps

The original objection to a Pillow-based design ("PIL is not the fastest
way, and the precompute is a lot of work") was aimed at a design that calls
into PIL *per frame*. That is not necessary for a fixed `hh:mm:ss:ff`
timecode: there are only 11 possible glyphs (`0`-`9` and `:`), all
monospaced, all rendered in a fixed font/size/color. Precomputing every
glyph's RGBA pixel bitmap **once**, at `TimecodeOverlay.__init__` time, and
blitting the ten needed glyphs into the frame per `apply()` call with plain
numpy slicing/blending, removes PIL from the hot path entirely — its
per-call performance is then irrelevant, since it is called exactly 11
times total per broadcaster process lifetime, not once per frame.

Runtime cost per `apply()` call becomes: for each of the 11 characters in
`"hh:mm:ss:ff"`, a numpy slice-assign and an alpha-blend
(`region = self._clean_patch_glyph_slot; blended = glyph_rgba * alpha +
region * (1 - alpha)`) over a small fixed-size rectangle — cheaper than a
single `cv2.putText()` call, no font rasterization, no library call of any
kind, pure array arithmetic. This is the same technique real-time
overlay/subtitle systems and hardware character generators have always
used ("bitmap font" / glyph atlas blitting) — precompute once, blit many.

Other options considered and rejected:
- **`freetype-py`** (direct FreeType bindings): also collision-free (no
  bundled FFmpeg), but strictly more dependency and API surface than
  reusing Pillow, which is already installed and already proven safe in
  this exact process.
- **Fully hand-authored bitmap font** (glyph pixel data as literal
  in-source constants, zero imaging library at all): maximally
  collision-proof, but pure busywork here — Pillow already renders correct
  glyphs at startup with no per-frame cost, so there is no performance
  reason to hand-author pixel data.
- **`Pillow-SIMD`** (drop-in faster Pillow fork): unnecessary — since PIL is
  not in the hot path, its raw throughput doesn't matter for this feature.

This replaces Section 5 and Section 8 (dependency) above; Sections 1-4 and
6-10 (semantics, config, testing shape, non-goals) carry over unchanged to
the reimplementation.

## 13. Addendum (2026-08-10): Measured performance

Rebuilt per Section 12 (commit `d854a69`) and measured directly rather than
estimated.

**Live end-to-end** (real broadcast, sck backend, flux-gallery app,
3840x2160@30fps, virtual display): `timecode_enabled: true` vs `false`,
each run steady-state for ~60s.

| | sends per 5s window | avg send+overlay time | broadcaster process CPU | RSS |
|---|---|---|---|---|
| enabled | 150-151 (30.0-30.2fps) | 6.1-7.6ms | 55-61% | ~514MB |
| disabled | 150-151 (30.0-30.2fps) | 9.4-10.5ms | 58-60% | ~519MB |

No dropped frames in either run (steady ~151 sends/5s = target fps exactly);
CPU/RSS/send-time differences between the two runs are within normal
run-to-run noise (NDI send jitter, thread scheduling, virtual-display
capture jitter) and do not resolve a directional effect at this precision —
the live path is too noisy to isolate the overlay's own cost. Neither run
logged the objc duplicate-class warning or any decode/send failures,
confirming the sck-backend capture-reliability regression from Section 11
is gone.

**Isolated microbenchmark** (`TimecodeOverlay` in-process, 3840x2160,
2000 iterations, warm cache, simulating the real launcher pattern of one
`snapshot()` per fresh decode and `apply()` on every send including
repeated/stale sends):

| call | mean | p99 | notes |
|---|---|---|---|
| `apply()`, enabled | 0.71ms | 0.75ms | runs every frame |
| `apply()`, disabled | 0.00007ms (70ns) | 0.0001ms | true no-op, one boolean check |
| `snapshot()`, enabled | 0.012ms | 0.015ms | runs only on a fresh decode, not every send |
| `__init__()` (glyph rasterization) | 1.15ms | — | paid once per broadcaster process lifetime |

At 30fps (33.33ms/frame budget), `apply()`'s 0.71ms is **~2.1% of the frame
budget** — consistent with the live runs showing no fps degradation (both
sustained exactly target fps with headroom to spare) and explains why the
live end-to-end numbers couldn't resolve a clean before/after delta: the
overlay's real cost is an order of magnitude below the noise floor of the
rest of the pipeline (NDI send itself averaged 6-10ms in these same runs).

**GPU**: not independently measured (`powermetrics` requires interactive
`sudo`, unavailable in this session) — asserted architecturally instead.
`TimecodeOverlay` calls Pillow (CPU rasterization, once at construction)
and plain numpy array arithmetic (CPU) only; it makes no calls into
Metal/OpenGL/any GPU API, so it cannot add GPU load regardless of what the
rest of the pipeline (Chrome's ANGLE Metal compositor, upstream of capture)
is doing.
