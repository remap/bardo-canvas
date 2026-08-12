# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Drives a 6-screen LED wall from one composited browser page. `config/screens.yaml` is the
source of truth for the wall's geometry; the server hands that layout to the page, the page
draws every screen into a single 3840×2160 canvas, and `ndi_broadcaster` captures that page
and sends it out as one NDI stream plus audio.

Apps are plain static directories that import `/layout-driver.js`. **The framework never
knows what an app draws.** Keep that boundary — app-specific behavior belongs in
`apps/<name>/`, not in `layout_server/` or `static/`.

## Commands

```bash
uv sync                                # base framework
uv sync --extra flux-gallery           # + flux-gallery's torch/diffusers (~2GB)
uv run playwright install chromium     # needed by the broadcaster and noraebang's smoke test

./run.sh                               # framework + broadcaster (defaults to apps/test-pattern/static)
./run.sh apps/static-pages/static      # positional arg selects the app, same as APP_DIR

uv run pytest                          # hermetic Python tests
uv run pytest -m live                  # tests needing a real display server (see below)
uv run pytest tests/test_launcher.py::test_name -v    # single test
node --test static/*.test.mjs apps/noraebang-generative/static/*.test.mjs

uv run ruff check . && uv run ruff format .
```

Never run bare `uv run ruff format .` as part of a scoped change — this tree has
pre-existing formatting drift in unrelated files and it will pull them into your diff.
Format only the files you touched.

`pytest` deselects `live`-marked tests by default via `addopts` in `pyproject.toml`.
They create real macOS virtual displays, so opt in explicitly with `-m live`. A
command-line `-m` overrides the default.

flux-gallery's `test_worker.py`/`test_gemini_expander.py` self-skip unless the
`flux-gallery` extra is installed.

## Architecture

**One instance = one config directory + one port.** `LAYOUT_DRIVER_CONFIG_DIR` and
`LAYOUT_DRIVER_PORT` are what a second concurrent instance overrides; every other path
(`SCREENS_YAML`, `AUDIO_YAML`, `BROADCASTER_YAML`, `LAYOUT_DRIVER_RUNTIME_DIR`) derives
from those but can still be set individually. Both `layout_server/main.py` and
`ndi_broadcaster/launcher.py` resolve their paths against `REPO_ROOT`, not the cwd, so
defaults don't depend on `run.sh` having `cd`'d first.

**Server** (`layout_server/`) — FastAPI over HTTPS with a self-signed cert generated into
`runtime/` on first run. `app.py` wires `screens_api` (layout), `ws_manager` (a broadcast
WebSocket the page subscribes to), `screen_store` (per-screen images for image-mode apps),
`file_watcher` (dev auto-reload), and `screenshot.py`, then mounts `APP_DIR` at `/`. The
self-signed cert means a browser preview shows a warning on first load — expected.

**Page** (`static/layout-driver.js`) — `initLayoutDriver()` fetches the layout and builds
the composited canvas. Apps then opt into one of the modes: `enableStaticPageMode`,
`enableImageMode`, `enableScreenshotResponder`, `enableAutoReload`. `buildRoot()` rescales
the whole canvas on resize, so opening `https://localhost:8443/` in an ordinary browser is
a faithful preview — not a special dev mode — and is safe alongside a running broadcaster.

**Broadcaster** (`ndi_broadcaster/`) — two capture backends selected by
`config/broadcaster.yaml`'s `capture_backend`, both feeding the same
`_LatestFrameSlot`/sender-thread/NDI machinery:

- `sck` (default, macOS only) — a headed Chrome window on an off-screen virtual display,
  captured via ScreenCaptureKit. `capture_sck.py` + `virtual_display.py` +
  `vdisplay_helper/` (a Swift binary using the private `CGVirtualDisplay` API, compiled on
  first use per machine).
- `cdp` — headless Chrome, captured by calling `window.__ndiCaptureDataURL()` through
  Playwright's `page.evaluate()`.

`sck` exists because `cdp` degrades to single-digit fps under sustained load — Playwright's
Node driver accumulates state, a documented upstream limitation, not a bug here. Don't
"fix" it in this repo; `sck` is the answer. Likewise, do not reach for CDP screenshot APIs
(`Page.captureScreenshot`, `Page.startScreencast`): all of them were tried and return solid
black for this app's canvases.

## macOS virtual-display gotchas

These have each cost real debugging time and are easy to reintroduce.

**Only SIGTERM tears a virtual display down.** `vdisplay_helper/main.swift` releases its
`CGVirtualDisplay` in `shutdownAndExit()`, wired to SIGINT/SIGTERM. `SIGKILL` runs no
handler, and `exit()` alone doesn't run Swift's ARC deinit chain — either one leaks a
display into WindowServer that **no API can remove**, since the private surface has no
remove-by-ID call. Always terminate → wait → kill as a last resort
(`virtual_display.py`'s `_terminate_helper`, `launcher.py`'s cleanup). Enough leaked
displays make `CGCompleteDisplayConfiguration` hang for every new one.

**Spin the run loop before reading display state.** `NSScreen.screens()` caches and
refreshes only when the run loop processes the display-reconfiguration notification;
`NSApplication.sharedApplication()` alone is not enough. Without
`Quartz.CFRunLoopRunInMode`, display enumeration returns inconsistent and sometimes flatly
wrong results. See `display_inventory.spin_run_loop` and `virtual_display.wait_for_settled_bounds`.

**The built-in display reports `CGDisplayUnitNumber == 0`.** The widely-cited "filter
ghosts by `unit != 0`" rule therefore misclassifies this laptop's own screen. Ghost
detection must be the full conjunction of unit, vendor, model and serial.

**Use `ps -o command=`, never `comm=`.** `comm=` yields only the executable path, so
`python -m ndi_broadcaster.launcher` is indistinguishable from any other Python process.

Diagnosing leaked displays: `python -m ndi_broadcaster.vdisplay_doctor scan|reap|probe` —
see [`docs/vdisplay-doctor.md`](docs/vdisplay-doctor.md).

Keep the display awake during a broadcast (`caffeinate -d`); virtual-display creation was
observed becoming unreliable while the display slept.

## Conventions

Tests are module-level `def test_*` functions with `monkeypatch` — no test classes. Line
length 100. Comments in this codebase explain *why*, often at length, and cite concrete
evidence; match that rather than trimming them to summaries.

`docs/bugs.md` is a living list of open issues; when one is resolved, move the writeup into
the relevant spec's addenda in `docs/superpowers/specs/` and delete it from `bugs.md`.
Those specs are point-in-time design records with addenda — read the addenda, since several
document reversals of decisions made in the body above them.

Known open issue: broadcaster shutdown can hang indefinitely, leaving the process and its
Chrome/`vdisplay_helper` children alive. See `docs/bugs.md`.
