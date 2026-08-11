# Layout Driver Framework — Design Spec

Status: approved for implementation planning
Scope: the reusable framework only (screen geometry config, canvas web app, NDI broadcaster). The two validation sample apps (Flux/Gemini image gallery, p5.js generative "noraebang" wall) get their own specs after this framework is built, but their requirements shaped the decisions below.

## 1. Purpose

Drive a 6-screen LED wall from a single logical 3840×2160 canvas rendered in a browser, captured, and broadcast as one 30fps NDI stream. Downstream AV infrastructure (not part of this project) receives the NDI stream and splits it to the physical screens. This repo's job stops at "produce a correct, single, well-composited 4K stream" — it does not talk to the physical wall directly.

The framework must support two very different kinds of app built on top of it, without assuming either:
- **Push-driven apps** (e.g. a Python worker generating images) that periodically POST a new image for one screen.
- **Direct-render apps** (e.g. p5.js sketches) that want to own a canvas per screen and run their own draw loop client-side.

Only one app is active at a time (selected at launch) — the framework does not need to support two apps driving different screens simultaneously.

## 2. Screen geometry

Derived by pixel-measuring `layout.png` (module = 200×200px, confirmed by the panel-count captions baked into the image, e.g. "6×3 (18) · 1200×600px"). The six screens tile a 17×10-module bounding box (3400×2000px), centered inside the 3840×2160 canvas with a 220px left/right margin and 80px top/bottom margin — chosen so "single 4K image" is a literal 3840×2160 frame rather than the tight bounding box.

| Screen | Grid origin (col,row) | Grid size (cols×rows) | Pixel rect (x, y, w, h) |
|---|---|---|---|
| F | (0, 0) | 9×7 | 220, 80, 1800, 1400 |
| B | (9, 0) | 6×3 | 2020, 80, 1200, 600 |
| C | (9, 3) | 6×3 | 2020, 680, 1200, 600 |
| D | (9, 6) | 8×2 | 2020, 1280, 1600, 400 |
| A | (9, 8) | 8×2 | 2020, 1680, 1600, 400 |
| E | (1, 7) | 8×2 | 420, 1480, 1600, 400 |

All rects were cross-checked against the margins (max extent 220+1800=2020...2020+1600=3620, +220 margin = 3840; 80+1400=1480 for F meeting E's row start; 1680+400=2080, +80 margin = 2160) with zero rounding error, confirming the grid reading is exact.

**Config format:** `config/screens.yaml`, storing only the grid coordinates (col/row/cols/rows) plus a shared `module_size` and `layout_offset` — pixel rects are *computed at load time*, not stored, so there is one source of truth and no risk of the table above drifting from the config:

```yaml
canvas:
  width: 3840
  height: 2160
  fps: 30

module_size: 200
layout_offset: {x: 220, y: 80}

screens:
  - id: F
    name: "Screen F"
    grid: {col: 0, row: 0, cols: 9, rows: 7}
  - id: B
    name: "Screen B"
    grid: {col: 9, row: 0, cols: 6, rows: 3}
  - id: C
    name: "Screen C"
    grid: {col: 9, row: 3, cols: 6, rows: 3}
  - id: D
    name: "Screen D"
    grid: {col: 9, row: 6, cols: 8, rows: 2}
  - id: A
    name: "Screen A"
    grid: {col: 9, row: 8, cols: 8, rows: 2}
  - id: E
    name: "Screen E"
    grid: {col: 1, row: 7, cols: 8, rows: 2}
```

`rect(screen) = {x: layout_offset.x + grid.col*module_size, y: layout_offset.y + grid.row*module_size, width: grid.cols*module_size, height: grid.rows*module_size}`. On load, the server validates no two screens' rects overlap and rejects the config if they do.

## 3. Components

### 3.1 `layout_server` (FastAPI + uvicorn, HTTPS)

Serves the active app's static bundle plus the shared framework assets, and brokers frame updates.

- `GET /api/screens` — the parsed `screens.yaml` with computed pixel rects, as JSON (so the browser never needs a YAML parser).
- `GET /healthz` — liveness check; the NDI broadcaster's launcher polls this before starting Chrome (same pattern karaoke-test uses).
- `POST /screens/{id}/image` — push a new frame for one screen. Body is raw image bytes (`Content-Type: image/png` or `image/jpeg`), optional `?transition_ms=` query param (default 500). Validates the screen id exists (404 if not) and that the bytes decode as an image (400 if not, via Pillow). Stores the latest bytes in memory keyed by screen id, bumps a per-screen monotonic `version`, and broadcasts `{"type": "frame", "screen": id, "version": n, "transition_ms": t}` to all connected WebSocket clients.
- `GET /screens/{id}/image?v=N` — returns the current stored bytes for that screen (404 if nothing has been pushed yet). The `v` query param is a cache-buster the client controls, not interpreted by the server.
- `WS /ws` — primarily a server→client push channel carrying the `frame` messages above; the one exception is the screenshot flow (§3.2a), where the server sends a request and the client sends back a result. On reconnect (network hiccup, browser reload), the client re-fetches every screen's current image via `GET /screens/{id}/image` to resync, since it may have missed pushes while disconnected.
- `POST /api/screenshot` — see §3.2a. Any app calls this to get a PNG of the full composited wall as currently displayed.
- HTTPS via a self-signed cert. `run.sh` generates one on first run if `LAYOUT_DRIVER_SSL_CERT`/`LAYOUT_DRIVER_SSL_KEY` aren't set and no cert exists at the default path, covering `localhost`/`127.0.0.1`. No auth on any endpoint — this is a trusted-LAN kiosk tool, not internet-facing; documented as an explicit assumption, not an oversight.
- Which app's static bundle is served is controlled by `APP_DIR` (env var, also settable via `run.sh` argument) pointing at `apps/<name>/static/`. The server always additionally serves `/layout-driver.js` and `/api/screens` regardless of which app is active.

### 3.2 `layout-driver.js` (shared client library, served to every app)

Plain ES module, no global singleton — apps `import` exactly the named functions they need from `/layout-driver.js`.

- `await initLayoutDriver() → driver`: fetches `/api/screens`, builds a fixed 3840×2160 root element containing one absolutely-positioned, exactly-sized container `<div>` per screen (per the computed rects — themselves read from the fetched config, not hardcoded), opens the shared `/ws` connection, and returns a `driver` object: `{ layoutConfig, getScreenContainer(id) → {element, width, height, x, y}, onMessage(handler) }`. `getScreenContainer` is the primary API — a plain DOM node an app can mount anything into (a p5.js instance, a `<video>`, a WebGL canvas, hand-rolled 2D drawing). `onMessage` registers a handler on the shared WS dispatcher (multiple handlers supported); a handler registered after the socket is already connected is immediately replayed a synthetic `{type: "_connected"}` message, so registration order relative to the socket's own connect timing never causes a missed resync. The root is CSS-`transform: scale()`'d to fit whatever the actual browser window/display size is, so the internal pixel math is always exact regardless of what's showing it.
- Convenience API for push-driven apps: `enableImageMode(driver)` creates a dual-canvas crossfade layer inside each container, and on each `frame` message (delivered via `driver.onMessage`) fetches `/screens/{id}/image?v={version}` and draws it into the inactive canvas with a cover-fit crop (like CSS `object-fit: cover` — scale to fill, crop the overflow, keep aspect) before flipping visibility. This is what makes "generate roughly-screen-shaped images and push them" require zero cropping math in the app itself.
- Apps that only need direct rendering (e.g. the p5 sketches) never call `enableImageMode(driver)` and just use `driver.getScreenContainer(id)`.

### 3.2a Full-wall screenshots (framework capability, called by apps)

Surfaced by the flux-gallery app's need to archive "what did the whole wall look like at this moment," but implemented once in the framework since any app can use it. Composited client-side rather than via a second headless browser instance — avoids running a duplicate Chrome just to take a picture of what the first one is already showing:

1. A caller (any app, any language) does `POST /api/screenshot` against `layout_server`.
2. The server generates a `request_id`, sends `{"type": "screenshot_request", "request_id": ...}` over `/ws` to the connected browser client.
3. `enableScreenshotResponder(driver)` handles this by creating an offscreen `3840×2160` canvas, and for every screen, finding the rendering surface inside that screen's container and `drawImage()`-ing it into the offscreen canvas at that screen's known rect. No cropping math needed here — unlike the push-image path, this is a direct placement, since the source canvas is already exactly the container's size. Finding "the" canvas isn't a bare `container.element.querySelector("canvas")`: `enableImageMode`'s dual-canvas crossfade means only one of its two canvases is the currently-visible one at any moment, so the active canvas is marked with a `data-layout-driver-active` attribute that the responder prefers, falling back to a plain `querySelector("canvas")` for direct-render apps (e.g. a p5 instance) that only ever have one canvas per container.
4. The composited canvas is exported via `toBlob()` and `POST`ed back to `/api/screenshot-result/{request_id}`.
5. The server's original `POST /api/screenshot` call (still pending, held open) resolves with those bytes. If no browser client responds within a short timeout (e.g. 2s) — no kiosk page open, or it's unresponsive — the endpoint returns `504`.

### 3.3 Audio: browser output → OS loopback → broadcaster input → NDI

Audio is a first-class, if optional, path — not an afterthought bolted on later. The shape is exactly karaoke-test's original loopback pattern, generalized:

1. An app that wants sound plays it through a normal `<audio>`/`<video>` element or a Web Audio `AudioContext` in the browser.
2. That element/context is routed to a loopback *output* device (e.g. "BlackHole 2ch") via `setSinkId()`, instead of the real speakers.
3. The OS loopback driver exposes that same device as an *input*; `ndi_broadcaster` captures it with `sounddevice.InputStream` and feeds `cyndilib`'s `AudioSendFrame`, on its own thread (same threading shape karaoke-test already used — this is a re-enable, not a new design).
4. The result is muxed into the single outgoing NDI stream alongside the video.

**Device naming, not indices, everywhere** — PortAudio/CoreAudio device indices shift across reboots and (dis)connects, so both sides of the config refer to devices by (substring-matched, case-insensitive) *name*, resolved to an index/deviceId at runtime, same pattern as karaoke-test's `audio_device` config key.

**Discovery file, written fresh on every `layout_server` startup**: a FastAPI lifespan hook runs `sounddevice.query_devices()` and writes `runtime/audio_devices.json`:

```json
{
  "inputs":  [{"index": 2, "name": "BlackHole 2ch", "max_input_channels": 2}],
  "outputs": [{"index": 2, "name": "BlackHole 2ch", "max_output_channels": 2},
              {"index": 4, "name": "MacBook Pro Speakers", "max_output_channels": 2}]
}
```

Also served at `GET /api/audio-devices`, so the file (for a human editing config) and the API (for tooling/debugging) always agree, and neither goes stale relative to what's actually plugged in.

**Config**: `config/audio.yaml`, one shared file since both sides of the loopback are usually the same physical virtual device, but kept as two independent keys for setups that don't loop back symmetrically:

```yaml
enabled: true
input_device: "BlackHole 2ch"   # broadcaster's sounddevice.InputStream match
output_device: "BlackHole 2ch"  # browser setSinkId() match
```

`layout_server` exposes this (minus nothing sensitive — there's no secret here) at `GET /api/audio-config`. `layout-driver.js` exports `routeAudioElement(el)` (a standalone function, not a `driver` method — it doesn't need screen geometry, just the element): fetches that config, resolves `output_device` against the browser's own `navigator.mediaDevices.enumerateDevices()` (substring match), and calls `el.setSinkId(deviceId)`. Apps that want sound just build a normal media element and pass it to this helper; apps that don't want sound never call it. Chrome only returns real device *labels* from `enumerateDevices()` once some media permission has been granted, so the broadcaster's Playwright launch grants microphone permission on the context up front (`context.grant_permissions(["microphone"])`) — harmless on a trusted local kiosk, and required for `output_device` name matching to work at all.

If `audio.yaml` has `enabled: false`, or a configured device name isn't found in the discovery list, the affected side logs a warning and runs without audio rather than failing the whole app/broadcaster.

### 3.4 `ndi_broadcaster` (generalized from `karaoke-test`)

- Playwright drives headless Chrome against the layout-server's URL (default `https://localhost:8443/`), after polling `/healthz`. See point 1 below for why headless, not headed.
- Capture backend is config-selectable (`config/broadcaster.yaml: capture_backend: cdp | sck`), default `cdp`:
  - `cdp`: every actual CDP screenshot/screencast API (`Page.captureScreenshot` clipped to `#layout-driver-root`, `Page.captureScreenshot` over the whole viewport, `Page.startScreencast`, `HeadlessExperimental.beginFrame`) was tried and abandoned here in turn — the full history is worth keeping since each carried a real, only-visible-under-live-testing failure mode:
    1. `Page.startScreencast` under a **headed** kiosk window (the original design, generalized from `karaoke-test`) — dropped because a headed window's real OS window can be capped smaller than the configured resolution by the physical display, and `buildRoot()`'s `transform: scale()` then produces a captured frame whose scaled-down layout, stretched back up, comes out with screens misplaced and wrongly sized (observed live: a 3456×2234 laptop screen against a 3840×2160 canvas). Separately, capturing a **visible** window via any CDP screenshot API at 30fps was independently found to hammer the compositor enough to cause visible on-screen flashing (confirmed by isolating the same headed launch with the capture loop removed entirely — no flashing without it), which is why the broadcaster is headless at all: nothing here needs a real window (app audio is a plain `<audio>` element, no DRM), and headless has no on-screen presentation to disrupt.
    2. Polling the layout-server's own `POST /api/screenshot` over HTTP, headless — dropped for conflating two unrelated concerns (the continuous NDI feed and flux-gallery's occasional history-snapshot use of that same endpoint) and an unnecessary WebSocket/HTTP round trip, before any correctness problem with it was found.
    3. `page.screenshot()` clipped to `#layout-driver-root`'s `bounding_box()`, headless — avoided (2)'s round trip, but an extended live run showed capture latency climbing steadily over several minutes of sustained 30fps polling (30ms → 200ms+) with sender-thread decode/send cost staying flat and no RSS growth in any process.
    4. `Page.startScreencast` again, headless this time — sidesteps (1)'s aspect-ratio blocker (headless has no physical window to be smaller than anything) and is Chromium's actual purpose-built API for continuous capture (push-based, a frame only on real compositor damage). Looked stable in a short run, but a live test that pushed a confirmed, browser-fetched image update to a screen produced zero screencast frames for over a minute afterward: CDP's screencast can silently *drop* a real compositor repaint rather than merely delay it.
    5. `HeadlessExperimental.beginFrame` (with `--enable-begin-frame-control --run-all-compositor-stages-before-draw`), Chromium's own documented mechanism for deterministic single-frame-at-a-time headless capture — the method doesn't exist at all in this Chromium version's full-browser binary (`'HeadlessExperimental.beginFrame' wasn't found`) and hangs indefinitely via the separate `chrome-headless-shell` binary Playwright selects for headless launches. Effectively removed/unusable in current Chromium.
    6. Root cause, found by direct comparison: every attempt above shared the same failure — a canvas's own `ctx.getImageData()` showed correct, valid pixel data (a pushed solid-color test image) at the exact coordinates a same-instant CDP screenshot showed `(0, 0, 0)`, reproduced with and without GPU acceleration and across both Chromium binaries. This matches a long-documented class of Chromium/Puppeteer/Playwright bug (e.g. `puppeteer/puppeteer#5352`): canvas content that's valid and readable from the page's own JS does not reliably reach Chromium's viewport-level screenshot/screencast capture pipeline. The actual fix sidesteps every CDP screenshot API entirely: `window.__ndiCaptureDataURL()` (`static/layout-driver.js`) composites the same canvases with plain `ctx.drawImage()` and reads the result back with the *canvas's own* `toDataURL()` — proven reliable throughout the investigation. `page.evaluate()` runs this over the same CDP connection Playwright already holds to drive the browser it launched; there is no HTTP server and no network round trip in the 30fps capture loop, only a direct call into the one page already under Playwright's control (the same mechanism used for every other `page.evaluate()` call in this codebase).
    7. That fix alone still failed under sustained load: `compositeToCanvas()` originally called `document.createElement('canvas')` on every single invocation — harmless at its original call rate (at most once per `screenshot_request`, i.e. rare), but at up to `config.fps` calls/sec sustained for minutes, a fresh GPU-backed canvas allocated 30 times a second and never reused leaked badly enough to reproduce both of the above failure modes by a different route: a slow climb in capture latency under Metal (GPU resources exhausted gradually) and an outright renderer crash after about 30 seconds under SwiftShader (`Page.evaluate: Target crashed`, software-side allocation exhausted faster). The fix was to allocate the offscreen canvas once, outside the per-call function, and reuse it every capture — the same pattern already used correctly everywhere else in this codebase for canvases that live for the page's lifetime.
    Paced to `config.fps` via the same deadline-based clock used elsewhere. Verified live with extended runs on both sample apps after the fix: noraebang (continuously-animating p5 sketches, a much higher sustained encode rate since every frame's content is genuinely different) held a stable ~13–16fps indefinitely, with no degradation trend. flux-gallery held a clean ~29–30fps indefinitely *while idle*, but degrades once its worker starts pushing real generated images — see §3.4a for the confirmed root cause and the chosen fix.
  - `sck`: macOS ScreenCaptureKit via PyObjC, raw BGRA frames matched by window title. Needs Screen Recording permission and a headed display (physical or dummy plug) attached. Opt-in for deployments that need the extra performance/quality at sustained 4K30.

### 3.4a Known limitation: NDI capture degradation, caused by using Playwright outside its design envelope

Running flux-gallery's worker alongside the broadcaster causes NDI capture fps to
degrade steadily and indefinitely — starting around 28–30fps, declining over
several minutes to single digits, with no recovery on its own. This is **specific
to flux-gallery**; the framework itself and every other app built on it (verified
with noraebang-generative, and with synthetic repros below) are unaffected.

**What was ruled out**, each via live A/B testing, not just review of the code:

1. Flux/MPS model residency on the GPU (fps stayed clean for minutes with the
   model loaded but idle; degradation began exactly at the first completed
   `generate()` call, not at model load).
2. NDI Video Monitor.app (an unrelated GPU-using receiver happened to be running;
   quitting it made no difference).
3. Killing the flux-gallery worker process outright (fps did not recover within
   several minutes of the process being gone).
4. Recycling the capture browser (§3.4's `BROWSER_RECYCLE_INTERVAL_SECONDS`) —
   confirmed via multiple live tests, including recycles landing well after all
   worker activity had stopped, that this never restores fps.
5. Spotlight indexing / Dropbox sync of the worker's output directory (excluded
   both via `com.dropbox.ignored` and `.metadata_never_index`; degradation was
   identical).
6. Audio capture sharing the same underlying `cyndilib.Sender` handle as video
   (disabled audio entirely; degradation was identical).
7. The worker's HTTP image push mechanism alone (a minimal repro worker with no
   Flux/Gemini/torch, doing only `POST /screens/{id}/image` on a timer, ran
   clean for 3 minutes).
8. The worker's `POST /api/screenshot` call specifically (same minimal repro,
   extended to also call it every cycle — still ran clean for 3 more minutes).
9. Raw PyTorch/MPS usage in a concurrent process, independent of Flux (a
   synthetic repro doing large bf16 matmuls on MPS, both idle-resident and in
   active compute bursts sized like a real diffusion step, ran clean for 5
   minutes).
10. Our own sender loop's lack of backpressure (added explicit backoff so a slow
    `send()` could never be met with an immediate zero-gap retry — the
    degradation curve was unchanged, and native profiling showed the loop
    was never actually busy-spinning in practice).
11. `cyndilib`'s async send path specifically (`Sender.send_video_async()`) vs.
    its synchronous equivalent (`Sender.send_video()`, a different code path
    into the SDK) — both degrade identically.
12. Gemini API activity in isolation (a repro calling the real `GeminiExpander`
    on the same cadence as the real worker, pushing a static test image with
    no disk writes, held a clean ~30fps through 17 real API calls over 2
    minutes).
13. **Disk I/O volume/frequency** (`disk_history.save_and_prune`, called twice
    per worker cycle). This looked confirmed for a time — a repro doing real
    `GeminiExpander` calls plus real file writes (~1-2MB PNGs, the real
    directory-listing-and-prune logic), with **zero** Flux/FAL/MPS involvement,
    reproduced the exact degradation curve, and repeating it with the write
    target moved to `/tmp` (outside the Dropbox-synced repo tree) made no
    difference. But disabling `disk_history`'s writes entirely in the real
    worker (FAL backend, real cadence) **did not fix the degradation** — it
    degraded identically with zero disk writes. The disk-I/O repro's real flaw:
    it also pushed genuinely complex, freshly-generated image content on every
    cycle, which turned out to be the actual confound (item 14).
14. Chrome's own `willReadFrequently` canvas-optimization heuristic (which can
    silently switch a frequently-read `<canvas>` from GPU-accelerated to
    CPU-backed rendering — a real, documented Chromium behavior, see citations
    below) — tested directly by explicitly setting
    `getContext("2d", { willReadFrequently: true })` on the offscreen
    compositing canvas in `static/layout-driver.js`. No measurable effect;
    reverted.

Native profiling (macOS `sample`, corroborated by `py-spy`) had pointed at
`cyndilib`'s `_send_video_async` spending nearly all its time inside a
`sleep_for()` in `libndi.dylib` (Vizrt's closed-source NDI runtime), and for a
time this was believed to be the root cause. That was also **wrong** — the
sender thread's own logged `send()` timing (already emitted every 5s) shows
only a modest ~1.5-1.8x slowdown (14-16ms → 21-26ms) that plateaus, nowhere
near the 10-15x collapse in the actual symptom (capture latency: 30ms →
100-500ms). The profiling wasn't fabricated — `send()` genuinely is a large
fraction of the sender thread's own per-frame budget even at baseline (14ms of
a 33ms budget), so it dominates a profile of *that thread* — but that's a
different claim from "the NDI SDK call is what's slow," and the sender
thread's own send-call *rate* never drops even while fully degraded (still
~30/s, confirmed from logs), meaning the real NDI network output never stops;
it just repeats stale frames. All 10-15x of the actual magnitude lives
upstream, in the capture loop's `page.evaluate()` round trip.

**Trigger, confirmed via clean isolation:** pushing a genuinely complex
(photographic) image to a screen while `page.evaluate()`-based capture runs,
independent of what produced that image. A repro with **zero** network calls,
zero Gemini, zero disk writes, zero torch import — a two-line loop pushing the
same static real photo over HTTP every 2 seconds — reproduced the identical
degradation, with the fastest onset of any test (already degraded within 15
seconds). Simple synthetic images (solid colors) never trigger it at any push
rate tested.

**Root mechanism, confirmed via external documentation:** Playwright's own
Node.js driver process — the persistent process that bridges Python calls to
Chromium over CDP, sitting underneath every `page.evaluate()` call — is
[documented by Playwright's own maintainers](https://github.com/microsoft/playwright/issues/15400)
to accumulate unreleased per-context/per-call state, because Playwright is
designed for short-lived, per-test sessions that get closed and thrown away in
seconds to minutes, not sustained, multi-hour, high-frequency automation. From
that thread (still open, with a comment dated 2026 confirming it's still
happening): "*Playwright is focused on testing scenarios, where contexts are
constantly closed and re-opened on a per-test basis... If the context is not
closed, then all the debugging metadata is piling up*" — and a separate
commenter's direct measurement found that even properly closing and recreating
contexts only *delays* the growth; only killing the Node process itself clears
it. This directly explains the one asymmetry nothing else did: browser/context
recycling (`BROWSER_RECYCLE_INTERVAL_SECONDS`, item 4) never fixed the
degradation because it leaves the same underlying Node driver process alive
across recycles; a full restart of `ndi_broadcaster.launcher` fixes it
instantly because that's the only thing that kills the driver process too. It
also explains the content-complexity correlation: more data marshaled through
more `page.evaluate()` calls (a larger base64 data-URL payload for a real
photo vs. a trivial one for a solid color) means faster accumulation. This is
a well-known, years-old, **externally unresolved** Playwright limitation — not
a bug in our code, `libndi.dylib`, disk I/O, Dropbox, or Chrome's canvas
rendering heuristics. Using Playwright/CDP for a sustained 30fps capture loop
for hours at a time is fundamentally outside what the tool is built for.

**Chosen path forward — two independently validated approaches, complementary
rather than competing:**

1. **Replace the capture mechanism with macOS ScreenCaptureKit (SCK)**,
   decoupling the 30fps pixel pump from Playwright's Node driver entirely
   (Playwright is kept only for page load/control, which is exactly the kind
   of short-lived operation it's designed for). SCK needs a real, headed,
   on-screen window; the target 3840×2160 doesn't fit the built-in display on
   the current dev machine, so a proof of concept validated creating a fully
   virtual (software-only, no physical hardware) display via the private
   `CGVirtualDisplay` API — the same one used by open-source tools
   [DeskPad](https://github.com/Stengo/DeskPad) and
   [BetterDisplay](https://github.com/waydabber/BetterDisplay) — as a compiled
   Swift CLI helper. Confirmed end to end: a headed Chromium window placed on
   that virtual display reached the true, unclamped 3840×2160 size (verified
   three independent ways), and `SCStream` delivered real, continuously
   changing content at a sustained ~29.3fps average over a 5-minute run (8804
   frames, 296/296 sampled frames distinct — zero repeats). Full spike
   writeup, code, and five specific WindowServer/AppKit timing gotchas (mode
   selection races, origin placement, `NSScreen` cache staleness, a
   `CGS_REQUIRE_INIT` crash) are preserved for implementation reference. Not
   yet validated: multi-hour endurance (only a 5-minute run was done).
2. **Decouple the NDI sender from the capture process entirely**, via two
   separate OS processes connected by a local Unix domain socket, so that
   restarting the capture side (whether on a timer, pre-emptively, or after a
   crash) never touches the sender's live NDI connection. A separate proof of
   concept validated this over a 33-minute run: an independent NDI receiver
   script (not trusting the sender's own bookkeeping) recorded ~59,000 real
   frames with zero gap events through 9 scripted capture-process restarts
   *and* an unplanned ~2.5-minute outage, with a measured restart-to-fresh-frame
   latency of 416-588ms (mean 532ms) — during which the sender repeats the
   last known frame (a brief freeze, not a dropped connection or black
   screen) rather than a real interruption. The same 18-minute run against
   noraebang's continuous p5.js content showed no degradation trend, though
   the PoC report is explicit that 18 minutes doesn't rule out a slower decline
   over the multi-hour sessions where the original degradation was observed.
   This pattern is a straightforward extension of `_LatestFrameSlot`'s
   existing "repeat the last frame rather than stall" design across a process
   boundary instead of a thread boundary, and remains useful as a safety net
   around approach 1's own capture-process restarts/failures, independent of
   which capture mechanism is in use. Not yet implemented in the real
   framework.

Investigation citations: [microsoft/playwright#15400](https://github.com/microsoft/playwright/issues/15400)
(the confirmed root mechanism); [Chromium issue 40502896](https://issues.chromium.org/issues/40502896)
("`context.drawImage()` slows down massively when drawing a few large
images" — a real, documented Chromium bug matching the symptom shape,
investigated as a candidate mechanism alongside `willReadFrequently` before
the Playwright driver explanation was found; kept for reference, not the
confirmed cause here); [Chrome's `willReadFrequently` canvas heuristic](https://developer.chrome.com/blog/canvas2d)
(tested directly per item 14 above, no effect).

- Sends via `cyndilib` (`cyndilib.sender.Sender`, `FourCC.RGBA`), fixed 3840×2160 @ 30fps, on a dedicated capture/send thread, plus the loopback-captured `AudioSendFrame` thread described in §3.3 when `audio.yaml` has `enabled: true`.
- On macOS, Chrome is launched with `--use-angle=metal`. Playwright's bundled headless Chromium otherwise defaults to the SwiftShader software renderer (confirmed live via CDP's `SystemInfo.getInfo`: `gpu_compositing`/`rasterization`/`2d_canvas` all `disabled_software`/`unavailable_software`) — every canvas draw and every wall capture was being rasterized entirely on the CPU, which was the majority cause of an inconsistent, well-below-target capture rate (observed: oscillating 12–28fps against the 30fps target) that read as choppy on the NDI feed. `--use-angle=metal` switches to the real GPU via Apple's Metal API (confirmed live afterwards: same query reports the real GPU's Metal renderer, all three features `enabled`), bringing capture to a steady ~28–30fps. ANGLE's Metal backend is macOS-only, so this flag is added conditionally; other platforms fall back to Chromium's own default backend selection.
- Karaoke-specific logic (Spotify DRM/headed-browser requirement, the karaoke backend proxy chain, hardcoded `"Karaoke-Test"` NDI source name, `1920x1080` baked-in viewport) is stripped; NDI source name, target URL, resolution, and fps all become config/env.

## 4. Data flow examples

**Push-driven (Flux app shape):** worker generates an image sized close to a screen's aspect → `POST /screens/E/image` → server stores bytes + bumps version + broadcasts over `/ws` → browser fetches the new bytes and cover-fit-crops them into screen E's canvas → next captured frame includes it. The app never computes an exact crop rect itself.

**Direct-render (p5 app shape):** on page load, the app's JS awaits `initLayoutDriver()`, then calls `driver.getScreenContainer('F')` for each screen id, constructs a `new p5(sketchForF, containerF.element)` per screen (six independent p5 instances, each only aware of its own container's `width`/`height`), and each sketch runs its own `draw()` loop. The layout-server and `/ws` channel are irrelevant to this app — it never pushes images.

**Audio (either app shape, opt-in):** app creates an `<audio>` element and calls `routeAudioElement(el)` → element's sink is set to the configured loopback output device → OS loopback presents the same audio as an input device → `ndi_broadcaster`'s `InputStream` captures it → muxed into the NDI stream as its audio channel.

**Screenshot (either app shape, opt-in):** app calls `POST /api/screenshot` → server round-trips a request over `/ws` to the browser → browser composites all 6 screens' current canvases into one PNG and posts it back → server returns those bytes to the original caller.

## 5. Repo structure

```
layout-driver/
  layout_server/          # FastAPI app: screens.yaml loader+validator, push API, WS relay, health
  static/layout-driver.js # shared client library (container/canvas API, push-mode listener)
  ndi_broadcaster/        # generalized capture+broadcast, launcher, config
  config/screens.yaml
  config/broadcaster.yaml
  config/audio.yaml
  runtime/audio_devices.json   # regenerated on every layout_server startup, not committed
  apps/
    test-pattern/          # zero-dependency static bundle: labeled solid-color rects per screen,
                            # used to smoke-test the whole pipeline before either real app exists
    flux-gallery/          # see 2026-08-08-flux-gallery-app-design.md
    noraebang-generative/  # see 2026-08-08-noraebang-generative-app-design.md
  run.sh                  # cert bootstrap, starts layout_server, waits for /healthz, starts broadcaster
  docs/superpowers/specs/
```

Apps are strictly isolated under their own `apps/<name>/` directory (own static assets, own config, own optional backend process, own `run.sh` if they need one) — nothing app-specific ever lives in `layout_server/`, `ndi_broadcaster/`, or `static/`. Both sample apps' specs were written after this one but fed back into it (§3.2a, §3.3) rather than requiring framework changes once built.

## 6. Error handling

- Unknown screen id on push → 404. Non-decodable image bytes → 400.
- WebSocket drop → client auto-reconnects with backoff; on reconnect, re-fetches every screen's latest image to resync (covers missed pushes).
- Broadcaster launcher polls `/healthz` with a timeout before touching Chrome/Playwright; fails loudly (non-zero exit, clear log line) rather than launching against a dead server.
- Config validation (overlapping screen rects, unknown module_size, etc.) fails fast at server startup, not at first request.
- A configured audio device name (input or output) not found in the discovery list logs a warning and disables audio for that side rather than crashing the server, broadcaster, or app.
- `POST /api/screenshot` with no connected browser client, or one that doesn't respond within the timeout, returns `504` rather than hanging indefinitely.

## 7. Testing

- `pytest` unit tests for `screens.yaml` loading and rect computation, using the exact fixture table in §2 as expected output, plus an overlap-rejection test.
- A small JS unit test (run manually via `node --test`, no CI pipeline wired up) for the cover-fit crop math, covering the three aspect ratios actually present (9:7, 2:1, 4:1).
- A `pytest` unit test for the device-name-matching function (exact match, case-insensitive substring match, not-found → `None`/fallback), run against a fixture device list — no real audio hardware needed.
- A JS unit test for the screenshot compositing placement math (given the §2 screen-rect fixtures and stub canvases, assert each is drawn at its correct offset in the 3840×2160 composite) — direct placement only, no cropping, so this is simpler than the cover-fit test above. The end-to-end request/response round trip over `/ws` is covered by the manual pipeline smoke test below.
- Manual pipeline smoke test using `apps/test-pattern/`: run `run.sh`, confirm each screen shows its correctly-positioned/sized labeled rectangle in the captured NDI output before either real app is built. NDI/capture correctness beyond that is verified manually (no practical way to unit test actual OS-level screen capture or NDI output).

## 8. Explicit non-goals

- No auth on the HTTP/WS API (trusted LAN assumption).
- No support for two apps driving different screens simultaneously.
- No audio mixing/ducking across multiple simultaneous sources — one loopback input is captured as-is; an app that wants to mix multiple sounds does so itself (e.g. multiple `<audio>` elements or a Web Audio graph) before it reaches the loopback output.
- No direct control of the physical LED wall — this system's output is the NDI stream only.

## 9. Tech stack

Chosen to keep the implementation small and readable, favoring current, actively-maintained tooling over legacy defaults:

- **Python 3.13**, managed with `uv` (dependency resolution + venv + running — replaces pip/venv/poetry with one fast tool). Modern typing throughout: PEP 695 generic/type-alias syntax (`type ScreenId = str`) where it helps, `dataclasses`/Pydantic v2 models for config (not raw dicts), structural `match`/`case` for the small WS-message-type dispatch. FastAPI's `lifespan` context manager for startup/shutdown (device discovery, cert bootstrap check) rather than the deprecated `on_event` hooks.
- **`ruff`** for lint + format (single fast tool, replaces flake8+black+isort). No CI pipeline or standalone type checker (`pyright`/`mypy`) is set up as of this writing — `uv run ruff check`/`ruff format --check` plus the test suites are run manually; a type checker would be a reasonable future addition if this grows a CI workflow, not something currently enforced.
- **`sounddevice`** for audio device enumeration/capture (same library karaoke-test already uses — proven for this exact job), **`cyndilib`** for NDI send, **Pillow** for image decode/validate, **Playwright** for browser automation.
- **JavaScript**: no bundler/build step — native ES modules (`<script type="module">`), `fetch`, `WebSocket`, `structuredClone`; kept dependency-free except **p5.js** (current major version, ESM import, instance mode) for the generative sample app. Avoiding build tooling here is itself the "keep it simple" choice for a small kiosk page, not a gap.
- **YAML** (via `PyYAML`) for all config files, matching the existing gentree/karaoke-test convention.

## 10. Addendum (2026-08-11): a positioning bug that two independent draw paths hid from each other

`enableImageMode()` (`static/layout-driver.js`) set every screen container's
CSS `position` to `"relative"`, overriding `buildRoot()`'s `"absolute"`. A
`position:relative` element's `top`/`left` are an offset from its normal
document-flow position, not an absolute coordinate — so every screen after
the first (screens stack in DOM order: F, B, C, D, A, E per
`screens.yaml`) rendered shifted down by the combined height of every
container before it. Confirmed live via `getBoundingClientRect()`: screen B
rendered at `y=1480` instead of its configured `80`, C at `2680` instead of
`680`, and so on — F looked correct only because nothing precedes it in
flow. `container.style.top`/`left` themselves still read back as the
correct configured values throughout, which is exactly why this went
undetected for as long as it did: nothing had ever compared configured
position against *actual rendered* position.

The reason it stayed invisible specifically to this framework's own
verification tooling: `/api/screenshot` and the `cdp` capture path's
`__ndiCaptureDataURL()` both composited from `computeCompositePlacements()`
(`static/geometry.js`), which derived placement from `screen.rect` — the
config value — never the real DOM. Two independent paths were answering
"where is screen X" with two different mechanisms that were supposed to
agree, and only one of them (the real rendered page, which is what `sck`
and any real screen capture actually see) was broken. The screenshot path
stayed "correct" by construction, which made it worse than useless as a
check.

Fixed by removing the `position:relative` override (this section's Non-goal
in §8 does not cover CSS regressions, but the general principle behind
"screen rect is the single source of truth for where things go" — §2 — was
being violated in practice by having a second, DOM-independent placement
calculation) — and eliminated the second draw path structurally rather than
just fixing this one instance of drift: `computeCompositePlacements()` is
removed from `geometry.js`; `driver.measureScreenPlacements()`
(`layout-driver.js`) reads actual `getBoundingClientRect()` values
(normalized by the current preview-scale factor from `rescale()`) instead,
and both `compositeToCanvas()` and the screenshot responder's `composite()`
call it. There is now exactly one way to answer "where is screen X
rendered," and if real layout is ever broken again, every consumer breaks
the same way, visibly, instead of only the one nobody happened to be
capturing from.

Regression-tested with a real browser: `tests/test_screen_layout_rendering.py`
loads the actual flux-gallery page via Playwright and asserts every
screen's rendered position matches its configured rect — not the CSS style
string, which stayed "correct" the entire time this bug was live.
Adversarially verified during development: reintroducing the
`position:relative` line made this test fail with the exact broken values
observed live; removing it again restored a pass.
