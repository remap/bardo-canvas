# Fitting a Chrome window exactly to a ScreenCaptureKit capture

This is a standalone writeup of a problem and fix that generalizes beyond this
repo: capturing a headed Chrome/Chromium window via macOS ScreenCaptureKit
(SCK) at an *exact* target pixel resolution, with no soft misalignment at
internal content boundaries. If you're building your own SCK-based capture
tool and hitting a fractional scale/offset you can't quite explain, this is
likely it.

The concrete implementation lives in this repo at
`ndi_broadcaster/launcher.py` (`_capture_loop_sck`, `_measure_chrome_overhead_px`,
`_measure_sck_crop_top`, `_sck_chrome_window_size`, `_CHROME_APP_MODE_HEADROOM_PX`)
and `ndi_broadcaster/capture_sck.py` (`SckCapture`, `bgra_buffer_to_rgba_bytes`).

## The problem

You want a Chrome window's rendered content to land in your NDI/capture
output at an exact resolution — say 3840×2160 — with no scaling, no
antialiasing at internal seams, pixel N in the page ending up at pixel N in
the output.

Two things stand between you and that:

1. **Chrome's own window chrome takes vertical space.** Even in the most
   minimal windowed mode, some number of pixels above the page content is
   consumed by browser UI (tab strip, address bar, or — in `--app=` mode — a
   bare title bar). If your JS measures `window.innerHeight` and it's less
   than your target canvas height, whatever's watching for the DOM to fit its
   window (e.g. a CSS `transform: scale(...)` that fits content to the
   viewport) will silently shrink your entire composited content to fit —
   which reads as "everything is very slightly too small," not "there's a
   toolbar," and is easy to miss without a pixel-exact test pattern.

2. **A window can never be taller than the display hosting it.** If you
   compensate for (1) by requesting a window taller than your target
   resolution (`target_height + toolbar_height`) so you can crop the excess
   off afterward, the *display* itself must also be at least that tall, or
   the OS silently clips the window back down to the display's real height —
   and you're back to (1), just by a different route.

Both failure modes produce the same symptom: the composited content is
slightly smaller than expected, uniformly, with no black bar or visible
seam anywhere obvious — because whatever's fitting content to the (short)
viewport absorbs the shortfall as a shrink rather than surfacing it as a
broken layout. If you're testing by eye, this is very easy to miss; it took
a synthetic pixel-grid test pattern with 1px unaliased tripwires at content
boundaries to surface it at all in this repo (see "how to actually verify
this" below).

## Why an exact hardcoded toolbar-height constant doesn't work

The natural fix looks like: measure the toolbar height once, hardcode it,
request `target_height + toolbar_height`, crop that many rows off the top.

This mostly works, but two things break it in practice:

- **`--kiosk` mode**, which removes window chrome entirely, was found (in
  this repo, live) to force macOS's native fullscreen Space transition for
  any borderless window sized to exactly fill a display. With "Displays have
  separate Spaces" off (a common macOS setting), that transition visibly
  disrupts whatever's on the operator's *other*, real displays — unacceptable
  for a background capture process. `--app=<url>` mode is the fix for this
  specifically: it gives a bare window (no tab strip, no address bar, just a
  minimal native title bar) without invoking any fullscreen/Space API.

- **The exact chrome height isn't a fixed constant, even for a fixed Chrome
  version.** In this repo's testing, the same `--app=` window's chrome
  height was measured at 30px on one broadcaster launch and 28px on the very
  next — nothing else changed, and the display itself was confirmed (via
  `CGDisplayPixelsHigh`) to be exactly the requested size both times. Some
  ±1-2px jitter in exactly how much of a requested window size Chrome's own
  title bar eats into appears to be inherent, at least in a headed-window
  virtual-display environment. Chasing an exact constant is a losing game;
  it will read as "fixed" for a while and then drift again for no apparent
  reason.

## The fix

Three parts, working together:

1. **Launch via `--app=<url>`, not a normal tabbed window**, to minimize (and
   make more consistent) the chrome height in the first place. Use
   `launch_persistent_context()` (Playwright), not `launch()` +
   `new_context()` + `new_page()` + `goto()` — a `--app=` window navigates
   itself before your automation framework's CDP session attaches, and a
   freshly created context/page from a plain `launch()` call never sees that
   window at all (confirmed live: `browser.contexts()` came back empty right
   after a `--app=` launch, and creating a second context just opened a
   second, separate, untracked window with the normal tab strip still
   showing). A persistent-context launch attaches to the browser's actual
   initial session, so the `--app=` window shows up in `context.pages[0]`
   directly.

2. **Give the window (and the display hosting it) generous headroom, not an
   exact target.** Launch at `target_height + HEADROOM` where `HEADROOM` is
   comfortably above the worst-case chrome height you've observed (this repo
   uses 60px against an observed 28-30px range) — enough that
   `window.innerHeight >= target_height` is guaranteed regardless of that
   run's exact jitter. Since Chrome adds essentially zero *horizontal*
   chrome in `--app=` mode, width can be requested exactly at the target
   with no headroom needed. If you're driving a virtual display yourself
   (rather than a real, fixed-resolution monitor), grow it by the same
   amount — this is what actually prevents the OS from clipping the taller
   window back down.

3. **Measure the real chrome height live, every launch, on the actual window
   you're about to capture — never assume a constant.** After the page
   loads: `const [w, h] = [window.innerWidth, window.innerHeight]`. Assert
   `w == target_width` (if that one ever starts drifting too, you need a
   horizontal crop this scheme doesn't have). Compute
   `chrome_height = requested_window_height - h`. That's your exact
   `crop_top` for *this* launch, whatever it happens to be this time.

That leaves one more gap: giving the window headroom means the composited
content no longer fills the window bottom-to-top — there's now unused space
below it too (`HEADROOM - measured_chrome_height`), and if your capture
config only crops from the top, every frame carries that leftover margin as
extra rows.

**Resist the urge to fix this by resizing the actual OS window after
measuring** (e.g. via CDP `Browser.setWindowBounds`). This looks like the
obvious move — shrink the window down to `target_height + measured_chrome_
height` so only a top-crop is needed, matching whatever capture API you're
using. In this repo's testing it introduced a *new*, worse problem: on a
window positioned flush against the display's exact width and height (zero
slack on any edge — the normal case for a dedicated capture display), a
partial or even full-bounds `setWindowBounds` call reproducibly shifted the
window's *width* by -20px as well, for reasons not fully understood, and
regardless of what bounds were explicitly requested. A small test window
with real margin around it on a real (non-virtual) display did not
reproduce this — it appears tied to being flush against the display edge
specifically — but the takeaway either way is: **don't resize the window at
all.** Leave it at its full launched size, and instead:

- Configure your capture API (in this repo, `SCStreamConfiguration`) to
  request the window's actual, full, un-resized native size. Requesting
  anything smaller forces the capture API itself to resize what it delivers
  to fit — reintroducing exactly the soft, sub-pixel misalignment this whole
  scheme exists to avoid.
- Crop **both** ends of the raw delivered pixel buffer yourself: `crop_top`
  rows off the top (the measured chrome height) and everything past
  `crop_top + target_height` off the bottom (the leftover headroom margin).
  This is plain array slicing on bytes you already have in hand — no OS
  window manager involved, so no window-manager quirk can reintroduce this
  problem. (`bgra_buffer_to_rgba_bytes`'s `target_height` parameter in this
  repo is exactly this.)

## How to actually verify this

Don't trust it by eye. A uniform few-percent scale error at 3840×2160 is
genuinely hard to see, and — per the failure mode above — doesn't look like
an obvious bug even when it's there.

Build a synthetic test page: a grid of uniquely-numbered, uniformly-colored
squares at some fixed pixel size (e.g. 200×200), with **no drawn border or
gridline** (a stroked border can itself mask or fake exactly the kind of
seam you're checking for) and a small number of 1px black crosshairs
(drawn via `fillRect` at integer coordinates, not `ctx.stroke()` — a
1px-wide *stroked* line straddles two pixel columns and renders as a
blurred 2px line, which would make a perfectly good capture look aliased)
at a few content boundaries. Composite the whole thing, capture it through
your real pipeline (not a shortcut screenshot API that bypasses your actual
capture backend — those can look perfect while the real capture path is
still broken), and zoom into the crosshairs. Any blur, break, doubling, or
offset there is direct, unambiguous evidence of a real problem — not an
artifact of the test pattern itself. This repo's version of that page is
`apps/ndi-grid-test/static/index.html`; see the main README for how to run
it.

It's also worth adding a small diagnostic overlay to the same page — plain
text reporting `window.innerWidth`/`innerHeight` against your target canvas
size — as a `position: fixed` element that's a **sibling** of your
composited root, not a descendant of it. Anything inside the composited
root gets whatever CSS scale-to-fit transform is correcting for a chrome
mismatch, so it would report scaled-down numbers instead of the real ones.
A sibling element reports the window's true, unscaled state, and — if your
capture path only ever grabs the composited root's own canvas rather than
the whole rendered window — never shows up in a screenshot-style capture at
all, only in whatever's watching the real OS-level window. That asymmetry
is itself diagnostic: if a "screenshot" style capture looks perfect but the
real broadcast doesn't, the bug is specifically in the gap between the two,
which is exactly this window-fit problem.
