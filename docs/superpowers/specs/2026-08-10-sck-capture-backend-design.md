# SCK Capture Backend — Design Spec

## 1. Purpose

`ndi_broadcaster`'s only working capture mechanism (`capture_backend: cdp`)
drives Chromium via Playwright's `page.evaluate()`, called up to 30 times a
second, sustained for hours. Playwright's own Node.js driver process — the
persistent process bridging every Python call to Chromium over CDP — is
[documented by its own maintainers](https://github.com/microsoft/playwright/issues/15400)
to accumulate unreleased state under exactly this kind of sustained,
high-frequency use, because Playwright is built for short-lived per-test
sessions, not multi-hour production automation. This is the confirmed root
cause of the NDI capture fps degradation described in the framework spec's
§3.4a — not disk I/O, not the NDI SDK, not Flux/GPU, not which
image-generation backend is in use.

This spec implements `capture_backend: sck`, a second capture mechanism using
macOS ScreenCaptureKit that removes Playwright's driver from the sustained
30fps path entirely. Playwright is kept for what it's actually good at — page
load, JS control, the WebSocket sync — which is exactly the kind of short-lived
operation it's designed for.

Two proof-of-concept spikes already validated the approach end to end (see
framework spec §3.4a for full results): a headed Chromium window can reach the
true, unclamped target resolution on a fully software-only virtual display
(no physical hardware, via the private `CGVirtualDisplay` API used by
open-source tools like [DeskPad](https://github.com/Stengo/DeskPad)), and
`SCStream` delivers real, continuously changing frames at a sustained ~29.3fps
average with zero repeated frames over a 5-minute run. This spec turns that
validated spike code into the real, config-selectable capture backend.

## 2. Scope

In scope: `ndi_broadcaster/capture_sck.py` (currently a stub raising
`NotImplementedError`), a new virtual-display helper (Swift binary + Python
wrapper), config additions, and wiring `ndi_broadcaster/launcher.py` to branch
capture logic on `capture_backend`. Testing both sample apps (flux-gallery,
noraebang-generative) against the new backend, specifically watching for the
*absence* of the degradation `cdp` shows under the same workloads.

Out of scope: removing or changing the `cdp` backend, which stays exactly as
it is and remains the default. The sender/capture process split validated in
the second proof-of-concept spike (decoupling the NDI connection from capture-process
restarts) is a separate, complementary piece of work, not built here.

## 3. Architecture

```
capture_backend: sck
        |
        v
sck_display_mode: virtual ---------------------+
        |                                       |
        v                                       v
  launch vdisplay_helper              sck_display_mode: physical
  (Swift, CGVirtualDisplay)                     |
        |                                       |
        v                                       |
  wait for settled bounds                       |
  (ndi_broadcaster/virtual_display.py)          |
        |                                       |
        +---------------------+-----------------+
                              |
                              v
              launch headed Chromium via Playwright,
              window-size = config.width x config.height,
              window-position = resolved display's real bounds
              (settled virtual-display bounds, or the matched
              physical display's bounds -- never left unset)
                              |
                              v
              locate window via SCShareableContent.windows()
              (title match -- display-agnostic, works either mode)
                              |
                              v
              SCStream attached via SCContentFilter,
              BGRA frames -> _LatestFrameSlot.put()
              (ndi_broadcaster/capture_sck.py)
                              |
                              v
      existing sender thread, NDI send, audio -- UNCHANGED
```

The `cdp` backend's `_capture_loop` in `launcher.py` is untouched. A new
function, `_capture_loop_sck`, handles the `sck` path and is selected by
`config.capture_backend` in `run()`. Both eventually call the same
`_sender_thread_loop`/`_LatestFrameSlot` machinery — the only thing that
changes is what fills the slot.

## 4. Config

New fields on `BroadcasterConfig` (`ndi_broadcaster/config.py`):

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
```

- `sck_display_mode` has no default — it's `None` unless explicitly set, and
  `run()` raises `ValueError` at startup if `capture_backend == "sck"` and
  `sck_display_mode` is unset, naming both valid values. This mirrors the
  project's existing fail-fast conventions (`_validate_screen_ids` in
  flux-gallery's worker, `_validate_backend_selection` in the FAL backend
  work) rather than silently guessing a mode for a live deployment.
- `sck_virtual_display_name` has a sensible default since any name works for
  a freshly created virtual display — no ambiguity to fail fast on.
- `sck_physical_display_name` has no default and is required (same fail-fast
  treatment) when `sck_display_mode == "physical"`. It's matched by substring
  against connected displays' `NSScreen.localizedName` values — the same
  substring-matching convention `layout_server/audio.py`'s device matching
  already uses for audio devices, not a new pattern. If no connected display's
  name contains the configured substring, `run()` raises `ValueError` listing
  every currently-connected display's actual name, so an operator can copy
  the right value directly from the error rather than guessing.
- `capture_backend: cdp` (the default) never touches any of the three new
  fields — they're `sck`-only and validated only when `sck` is selected.

`config/broadcaster.yaml` documents all three:

```yaml
capture_backend: "cdp"                          # "cdp" | "sck"
sck_display_mode: null                          # "virtual" | "physical" -- required if capture_backend: sck
sck_virtual_display_name: "Layout Driver Virtual Display"  # sck + virtual only
sck_physical_display_name: null                 # sck + physical only -- substring-matched against connected display names
```

## 5. Components

### 5.1 `ndi_broadcaster/vdisplay_helper/` (new directory)

`main.swift` and `CGVirtualDisplayPrivate.h`, ported from the validated spike
essentially verbatim (the private `CGVirtualDisplay`/`CGVirtualDisplayDescriptor`/
`CGVirtualDisplaySettings`/`CGVirtualDisplayMode` declarations, originally from
[KhaosT/CGVirtualDisplay](https://github.com/KhaosT/CGVirtualDisplay) via
DeskPad's usage of them). Takes `<width> <height> <name>` as arguments, prints
one JSON status line (`displayID`, `x`, `y`, `width`, `height`) once the
display is live, then blocks holding the display open until it receives
SIGTERM/SIGINT.

Includes the five timing/race-condition fixes the spike found necessary (all
documented in framework spec §3.4a and preserved as comments in the ported
source): the ~1.5s settle-then-verify-then-retry loop before trusting the
display's mode (WindowServer's own auto-arrange logic silently reverts an
immediate manual override), never assuming the requested origin is honored
(always read back `CGDisplayBounds`), and the `NSApplication.sharedApplication()`
call needed up front to avoid a `CGS_REQUIRE_INIT` hard crash.

**Not checked in as a compiled binary** — only the Swift source and header.
The binary is architecture-specific (this spike built for arm64) and
unnecessary repo bloat for a one-line build. `ndi_broadcaster/virtual_display.py`
(below) builds it on first use via `swiftc -O -o vdisplay_helper main.swift
-import-objc-header CGVirtualDisplayPrivate.h` if the compiled binary isn't
already present next to the source, caching it there for subsequent runs.

### 5.2 `ndi_broadcaster/virtual_display.py` (new)

Python wrapper around the helper, ported from the spike's `vdisplay_utils.py`:

```python
@dataclass(frozen=True)
class DisplayInfo:
    display_id: int
    x: int
    y: int
    width: int
    height: int


def ensure_helper_built(helper_dir: Path) -> Path:
    """Build vdisplay_helper via swiftc if the compiled binary is missing or
    older than its source, returning the binary's path."""


def start_vdisplay_helper(
    binary_path: Path, width: int, height: int, name: str
) -> tuple[subprocess.Popen, DisplayInfo]:
    """Launch the helper, parse its one-line JSON startup report."""


def wait_for_settled_bounds(
    display_id: int, width: int, height: int, timeout_s: float = 20.0
) -> DisplayInfo:
    """Poll until the display's bounds are stable across consecutive polls,
    match (width, height), and the display appears in NSScreen -- the exact
    settle logic and CFRunLoopRunInMode-based polling the spike found
    necessary (NSScreen.screens() doesn't reflect a new display under plain
    time.sleep() polling)."""
```

### 5.3 `ndi_broadcaster/physical_display.py` (new)

Small, physical-mode-only counterpart to display selection — no helper
process, no virtual display creation, just finding the target display's
bounds by name:

```python
def find_physical_display(name_substring: str) -> DisplayInfo:
    """Match a connected display by NSScreen.localizedName substring
    (case-insensitive) and return its actual CGDisplayBounds as the same
    DisplayInfo shape virtual-display resolution uses. Raises ValueError
    naming every connected display's actual name if no match is found --
    mirrors _validate_screen_ids' "message names what's actually available"
    convention.

    Returning real bounds (not just validating the name) matters: with
    multiple displays connected, launching Chromium with no explicit
    --window-position risks it landing on whichever display the OS picks by
    default, which could be too small and silently reproduce the original
    clamping problem this backend exists to avoid. Both display modes must
    resolve to concrete bounds and pass them to --window-position explicitly.
    """
```

### 5.4 `ndi_broadcaster/capture_sck.py` (implement the existing stub)

The existing file already has `ScreenCaptureKitUnavailableError` and a
`SckCapture` class shell. Replace the `NotImplementedError` body with a real
implementation ported from the spike's working `capture_sck.py`:

```python
class SckCapture:
    def __init__(self, window_title_hint: str, width: int, height: int) -> None: ...

    def start(self) -> None:
        """Look up the window via SCShareableContent (raising if not found,
        naming the titles that were found instead), build an SCContentFilter
        + SCStreamConfiguration (BGRA, minimumFrameInterval matching config.fps),
        and start the SCStream with a delegate that converts each delivered
        CVPixelBuffer to the same frame format _LatestFrameSlot.put() already
        expects from the cdp path."""

    def stop(self) -> None: ...
```

The `CVPixelBuffer` → frame-slot conversion reuses the spike's proven
lock/read/unlock pattern (`CVPixelBufferLockBaseAddress`, read the raw BGRA
buffer, unlock) — no PNG-saving or hashing (that was spike-only validation
instrumentation), just the raw bytes handed to `frame_slot.put()`.

### 5.5 `ndi_broadcaster/launcher.py` (modify)

Add `_capture_loop_sck(config, sender, stop_event)`, structured like the
existing `_capture_loop` (creates the `_LatestFrameSlot` and sender thread the
same way) but for setup: validate `sck_display_mode` is set (fail fast if
not), branch to either `start_vdisplay_helper`+`wait_for_settled_bounds` or
`find_physical_display` depending on the mode, launch headed Chromium via
Playwright with `no_viewport=True` and `--window-size`/`--window-position`
args derived from whichever display was resolved, then hand off to
`SckCapture` for the actual frame pump. `run()` picks `_capture_loop_sck` vs.
`_capture_loop` based on `config.capture_backend`.

## 6. Data flow example

**`sck` + `virtual` (dev/test):** `run()` sees `capture_backend: sck`,
`sck_display_mode: virtual` → builds/launches `vdisplay_helper 3840 2160
"Layout Driver Virtual Display"` → waits for settled bounds → launches headed
Chromium at those bounds → `SckCapture` finds the window via title,
attaches `SCStream` → frames flow into `_LatestFrameSlot` → existing sender
thread sends over NDI, unchanged.

**`sck` + `physical` (deployment with a real/dummy-plug display):** same, but
`find_physical_display(config.sck_physical_display_name)` replaces the
virtual-display creation step entirely, returning that display's real bounds
for `--window-position`/`--window-size` exactly as the virtual path does; no
Swift helper process exists in this mode.

## 7. Error handling

- `sck_display_mode` unset with `capture_backend: sck` → `ValueError` at
  startup naming both valid values.
- `sck_physical_display_name` unset or unmatched with `sck_display_mode:
  physical` → `ValueError` at startup listing every connected display's
  actual name.
- Virtual display fails to settle within its timeout → propagate the
  spike's `TimeoutError` with the last observed bounds, don't retry
  silently or fall back to a different mode.
- Target window not found via `SCShareableContent` after Chromium launches →
  raise naming the window titles that *were* found, matching the "what's
  actually available" convention used throughout this codebase.

## 8. Testing

- Unit-testable without any display/window: config validation
  (`sck_display_mode` required-when-`sck`, `sck_physical_display_name`
  required-when-`physical`), and `find_physical_display`'s name-matching
  logic (mockable `NSScreen.screens()`).
- Not unit-testable, verified live (matching how every other `ndi_broadcaster`
  capture-path change in this repo has been verified): the full
  `vdisplay_helper` → settle → Chromium-launch → `SCStream` → frame-slot
  chain, and the two sample apps run against it.
- **Decisive test**: run flux-gallery and noraebang-generative against
  `capture_backend: sck` / `sck_display_mode: virtual` for extended periods
  (long enough to have shown clear degradation under `cdp` in prior testing)
  and confirm fps stays clean where `cdp` did not, under the same real
  workloads (real Flux/FAL generation pushes for flux-gallery, real
  continuous p5.js rendering for noraebang).

## 9. Non-goals

- Multi-hour endurance testing of the `sck` path itself is not required to
  land this feature, but is explicitly flagged (per both proof-of-concept
  reports) as something to do before relying on it for a real multi-hour
  show, separately from this implementation work.
- The sender/capture process split (Unix-socket handover) validated in the
  second proof-of-concept spike is not implemented here.
- Removing, deprecating, or changing default behavior of `capture_backend:
  cdp` is explicitly out of scope — it remains the default, unchanged.
