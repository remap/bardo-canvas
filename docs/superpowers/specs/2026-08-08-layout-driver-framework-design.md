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
- `WS /ws` — server→client push channel carrying the `frame` messages above. One-way; the client doesn't send anything meaningful back. On reconnect (network hiccup, browser reload), the client re-fetches every screen's current image via `GET /screens/{id}/image` to resync, since it may have missed pushes while disconnected.
- HTTPS via a self-signed cert. `run.sh` generates one on first run if `LAYOUT_DRIVER_SSL_CERT`/`LAYOUT_DRIVER_SSL_KEY` aren't set and no cert exists at the default path, covering `localhost`/`127.0.0.1`. No auth on any endpoint — this is a trusted-LAN kiosk tool, not internet-facing; documented as an explicit assumption, not an oversight.
- Which app's static bundle is served is controlled by `APP_DIR` (env var, also settable via `run.sh` argument) pointing at `apps/<name>/static/`. The server always additionally serves `/layout-driver.js` and `/api/screens` regardless of which app is active.

### 3.2 `layout-driver.js` (shared client library, served to every app)

- Fetches `/api/screens` on load, builds a fixed 3840×2160 root element containing one absolutely-positioned, exactly-sized container `<div>` per screen (per the computed rects). The root is CSS-`transform: scale()`'d to fit whatever the actual browser window/display size is, so the internal pixel math is always exact regardless of what's showing it.
- Primary API: `LayoutDriver.getScreenContainer(id) → {element, width, height, x, y}` — a plain DOM node an app can mount anything into (a p5.js instance, a `<video>`, a WebGL canvas, hand-rolled 2D drawing).
- Convenience API for push-driven apps: `LayoutDriver.enableImageMode()` creates a `<canvas>` inside each container, opens the `/ws` connection, and on each `frame` message fetches `/screens/{id}/image?v={version}` and draws it into that screen's canvas with a cover-fit crop (like CSS `object-fit: cover` — scale to fill, crop the overflow, keep aspect) and a crossfade over `transition_ms`. This is what makes "generate roughly-screen-shaped images and push them" require zero cropping math in the app itself.
- Apps that only need direct rendering (e.g. the p5 sketches) never call `enableImageMode()` and just use `getScreenContainer`.

### 3.3 `ndi_broadcaster` (generalized from `karaoke-test`)

- Playwright drives headed Chrome in kiosk fullscreen against the layout-server's URL (default `https://localhost:8443/`), after polling `/healthz`.
- Capture backend is config-selectable (`config/broadcaster.yaml: capture_backend: cdp | sck`), default `cdp`:
  - `cdp`: Chrome DevTools Protocol screencast (`Page.startScreencast`), JPEG-decoded via Pillow. No OS permissions, works on any platform Playwright supports.
  - `sck`: macOS ScreenCaptureKit via PyObjC, raw BGRA frames matched by window title. Needs Screen Recording permission and a headed display (physical or dummy plug) attached. Opt-in for deployments that need the extra performance/quality at sustained 4K30.
- Sends via `cyndilib` (`cyndilib.sender.Sender`, `FourCC.RGBA`), fixed 3840×2160 @ 30fps, on a dedicated capture/send thread. Audio support (present in karaoke-test) is dropped from the default path — not needed for this project — but the sender setup keeps the same threading shape so it's a small add-back later if a future app needs it.
- Karaoke-specific logic (Spotify DRM/headed-browser requirement, the karaoke backend proxy chain, hardcoded `"Karaoke-Test"` NDI source name, `1920x1080` baked-in viewport) is stripped; NDI source name, target URL, resolution, and fps all become config/env.

## 4. Data flow examples

**Push-driven (Flux app shape):** worker generates an image sized close to a screen's aspect → `POST /screens/E/image` → server stores bytes + bumps version + broadcasts over `/ws` → browser fetches the new bytes and cover-fit-crops them into screen E's canvas → next captured frame includes it. The app never computes an exact crop rect itself.

**Direct-render (p5 app shape):** on page load, the app's JS asks `LayoutDriver.getScreenContainer('F')` for each screen id, constructs a `new p5(sketchForF, containerF.element)` per screen (six independent p5 instances, each only aware of its own container's `width`/`height`), and each sketch runs its own `draw()` loop. The layout-server and `/ws` channel are irrelevant to this app — it never pushes images.

## 5. Repo structure

```
layout-driver/
  layout_server/          # FastAPI app: screens.yaml loader+validator, push API, WS relay, health
  static/layout-driver.js # shared client library (container/canvas API, push-mode listener)
  ndi_broadcaster/        # generalized capture+broadcast, launcher, config
  config/screens.yaml
  config/broadcaster.yaml
  apps/
    test-pattern/         # zero-dependency static bundle: labeled solid-color rects per screen,
                           # used to smoke-test the whole pipeline before either real app exists
    flux-gallery/          # (spec'd/built after this framework)
    noraebang-generative/  # (spec'd/built after this framework)
  run.sh                  # cert bootstrap, starts layout_server, waits for /healthz, starts broadcaster
  docs/superpowers/specs/
```

## 6. Error handling

- Unknown screen id on push → 404. Non-decodable image bytes → 400.
- WebSocket drop → client auto-reconnects with backoff; on reconnect, re-fetches every screen's latest image to resync (covers missed pushes).
- Broadcaster launcher polls `/healthz` with a timeout before touching Chrome/Playwright; fails loudly (non-zero exit, clear log line) rather than launching against a dead server.
- Config validation (overlapping screen rects, unknown module_size, etc.) fails fast at server startup, not at first request.

## 7. Testing

- `pytest` unit tests for `screens.yaml` loading and rect computation, using the exact fixture table in §2 as expected output, plus an overlap-rejection test.
- A small JS unit test (or plain Node script run in CI) for the cover-fit crop math, covering the three aspect ratios actually present (9:7, 2:1, 4:1).
- Manual pipeline smoke test using `apps/test-pattern/`: run `run.sh`, confirm each screen shows its correctly-positioned/sized labeled rectangle in the captured NDI output before either real app is built. NDI/capture correctness beyond that is verified manually (no practical way to unit test actual OS-level screen capture or NDI output).

## 8. Explicit non-goals

- No auth on the HTTP/WS API (trusted LAN assumption).
- No support for two apps driving different screens simultaneously.
- No audio in the NDI stream by default.
- No direct control of the physical LED wall — this system's output is the NDI stream only.
