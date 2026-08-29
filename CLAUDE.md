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

**`control_window_url`** (sck only, `config/broadcaster.yaml`) opts a second, positioned
window into the SAME Chrome profile the broadcast window runs in — `_open_control_window`
in `launcher.py`, called once the `--app=` page has loaded. `None` (the default) opens
nothing; most apps have no second window to open. Same profile is the entire point: it's
what lets a page like yt-matrix's `/layout-control` reach the broadcast page over
`BroadcastChannel`, which only bridges tabs within one browser process — a control page
opened in an operator's own separate everyday browser, or the `cdp` backend's headless
instance, can never connect to this one.

Placement is via CDP (`Browser.getWindowForTarget` + `Browser.setWindowBounds`), not
`window.open()`'s popup features — confirmed live those get silently clamped to the
*calling* window's own screen when targeting a genuinely different display, a deliberate
Chrome restriction (cross-screen placement from page script needs the Window Management
API's explicit, user-granted permission, which nothing here can obtain). CDP operates
through the browser's own automation protocol, one level above that page-script sandbox
restriction, so it can move a window to any connected display regardless.

The target display is resolved by name (`control_display_name`, substring-matched against
`NSScreen.localizedName` via `physical_display.find_display_by_name` — same convention as
`sck_physical_display_name`), not by index: `NSScreen.screens()[0]` is only guaranteed to be
"the screen containing the menu bar" *at the moment of the call* (Apple's own documented
behavior), not a stable physical position — confirmed live an index-0 lookup did not
reliably resolve to the intended monitor. `None` (the default) falls back to
`physical_display.main_screen()`, i.e. that same unreliable reading; set
`control_display_name` explicitly for anything that needs to be predictable.

The window itself is sized to `control_window_width`/`control_window_height` (default
1200×600), centered within the resolved display's bounds — not sized to the full display,
which was the original bug (a control window that filled whichever screen it landed on).

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

**`window.open()`'s popup features (`left`/`top`) were tried first and don't work for
cross-screen placement** — confirmed live: Chrome silently clamps them to the calling
window's own screen when the target is a genuinely different display. That's what drove
the switch to CDP's `Browser.setWindowBounds` above. Even CDP placement is worth
re-verifying after any change here: this exact codebase already found the analogous
"requested window bounds" path for the *broadcast* window jitters a few pixels run over run
(see `_CHROME_APP_MODE_HEADROOM_PX`'s comment), and a CDP-driven resize of a window flush
against a display's edge can shift its width unexpectedly. Verify the control window
actually lands fully on `control_display_name`'s display (not clipped, not straddling two
displays) at the configured `control_window_width`/`control_window_height` on the real
machine before relying on it in production.

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
