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

- Playwright drives headed Chrome in kiosk fullscreen against the layout-server's URL (default `https://localhost:8443/`), after polling `/healthz`.
- Capture backend is config-selectable (`config/broadcaster.yaml: capture_backend: cdp | sck`), default `cdp`:
  - `cdp`: Chrome DevTools Protocol screencast (`Page.startScreencast`), JPEG-decoded via Pillow. No OS permissions, works on any platform Playwright supports.
  - `sck`: macOS ScreenCaptureKit via PyObjC, raw BGRA frames matched by window title. Needs Screen Recording permission and a headed display (physical or dummy plug) attached. Opt-in for deployments that need the extra performance/quality at sustained 4K30.
- Sends via `cyndilib` (`cyndilib.sender.Sender`, `FourCC.RGBA`), fixed 3840×2160 @ 30fps, on a dedicated capture/send thread, plus the loopback-captured `AudioSendFrame` thread described in §3.3 when `audio.yaml` has `enabled: true`.
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
