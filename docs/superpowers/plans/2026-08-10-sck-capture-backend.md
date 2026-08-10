# SCK Capture Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `capture_backend: sck`, a config-selectable second capture
mechanism for `ndi_broadcaster` using macOS ScreenCaptureKit, which removes
Playwright's Node.js driver process from the sustained 30fps capture path
(the confirmed root cause of the NDI fps degradation documented in framework
spec §3.4a) — without touching the existing `cdp` backend, which stays
default and behaviorally unchanged.

**Architecture:** A new `_capture_loop_sck` in `ndi_broadcaster/launcher.py`
resolves a display (either a fully virtual, software-only display created via
a compiled Swift helper around the private `CGVirtualDisplay` API, or a named
real display matched by substring), launches headed Chromium via Playwright
at that display's exact bounds (Playwright is only used for page load/JS
control here, not sustained capture), then hands frame delivery to a new
`SckCapture` class that attaches an `SCStream` to the Chromium window and
pushes decoded frames into the same `_LatestFrameSlot`/`_sender_thread_loop`
machinery the existing `cdp` backend already uses for NDI output.

**Tech Stack:** Python (pydantic config, PyObjC bindings to ScreenCaptureKit/
Quartz/AppKit/CoreMedia), Swift (compiled on first use via `swiftc`),
Playwright (headed Chromium launch only, not capture).

## Global Constraints

- `capture_backend: cdp` remains the default and its runtime behavior is
  unchanged. `_sender_thread_loop` may gain one new optional parameter
  (`decode_fn`, default `None`) to support the `sck` decode path without
  duplicating the sender loop — its default behavior must decode byte-for-
  byte identically to the current unconditional `decode_captured_frame`
  call, verified by a test that pins this.
- `sck_display_mode: Literal["virtual", "physical"] | None` has **no
  default**. `run()` raises `ValueError` at startup (before any network or
  display work) if `capture_backend == "sck"` and `sck_display_mode` is
  unset, naming both valid values.
- `sck_physical_display_name: str | None` has **no default**. `run()` raises
  `ValueError` at startup if `sck_display_mode == "physical"` and this is
  unset. `find_physical_display` raises `ValueError` listing every
  currently-connected display's actual `localizedName` if the configured
  substring matches none of them.
- Both display modes must resolve to a concrete `DisplayInfo` (real x/y/
  width/height) and pass it explicitly via `--window-position`/
  `--window-size` — window position must never be left unset, even in
  physical mode with only one display connected, to avoid silently
  reproducing the original window-clamping bug this backend exists to avoid.
- The `vdisplay_helper` Swift binary is **never committed** — only
  `main.swift` and `CGVirtualDisplayPrivate.h` are tracked in git. The
  binary is architecture-specific and is built on first use via `swiftc -O
  -o vdisplay_helper main.swift -import-objc-header
  CGVirtualDisplayPrivate.h`, cached next to the source for subsequent runs.
- The five timing/race-condition fixes validated in the proof-of-concept
  spike must be preserved verbatim in the ported Swift/Python code: the
  ~1.5s settle-then-verify-then-retry loop before trusting a virtual
  display's mode, always reading back real `CGDisplayBounds` rather than
  trusting a requested origin, polling via `CFRunLoopRunInMode` (not
  `time.sleep`) so `NSScreen.screens()` actually refreshes, calling
  `NSApplication.sharedApplication()` up front to avoid a `CGS_REQUIRE_INIT`
  crash, and using `no_viewport=True` (not `viewport=None`) to actually
  disable Playwright's forced 1280x720 default viewport.
- PyObjC packages (`pyobjc-core`, `pyobjc-framework-Cocoa`,
  `pyobjc-framework-Quartz`, `pyobjc-framework-ScreenCaptureKit`,
  `pyobjc-framework-CoreMedia`) are added as **base** project dependencies
  (not a new optional extra), gated with a `sys_platform == 'darwin'`
  environment marker, since `ndi_broadcaster` is core framework code.
- `ndi_broadcaster/capture_sck.py` imports PyObjC frameworks (`Foundation`,
  `ScreenCaptureKit`, `CoreMedia`, `Quartz`, `objc`) at module scope because
  it defines `NSObject` subclasses — it must only ever be imported lazily
  (inside a function), never at the top of `launcher.py` or any module the
  test suite imports unconditionally, so a `cdp`-only environment is never
  forced to have these installed to run its own tests.
- Unit tests exist only for logic that's mockable without real hardware:
  config validation, `find_physical_display`'s name-matching, the
  BGRA→RGBA pixel conversion (pure function, no PyObjC types), and
  `ensure_helper_built`/`start_vdisplay_helper`'s subprocess control flow.
  The virtual-display settle loop, the full `SCStream` wiring, and the two
  sample apps are verified live, matching how every other `ndi_broadcaster`
  capture-path change in this repo has been verified — not with mocked
  unit tests standing in for them.

---

## File Structure

- `ndi_broadcaster/config.py` — **modify**: add `sck_display_mode`,
  `sck_virtual_display_name`, `sck_physical_display_name` to
  `BroadcasterConfig`.
- `config/broadcaster.yaml` — **modify**: document the three new fields.
- `pyproject.toml` — **modify**: add PyObjC base dependencies (macOS-only).
- `ndi_broadcaster/vdisplay_helper/main.swift` — **new**: standalone CLI that
  creates a virtual display via the private `CGVirtualDisplay` API.
- `ndi_broadcaster/vdisplay_helper/CGVirtualDisplayPrivate.h` — **new**:
  bridging header with the private API declarations.
- `ndi_broadcaster/virtual_display.py` — **new**: `DisplayInfo` dataclass,
  `ensure_helper_built`, `start_vdisplay_helper`, `wait_for_settled_bounds`.
- `ndi_broadcaster/physical_display.py` — **new**: `find_physical_display`.
- `ndi_broadcaster/capture_sck.py` — **modify** (currently a stub): real
  `SckCapture` implementation using `SCStream`.
- `ndi_broadcaster/launcher.py` — **modify**: `_sender_thread_loop` gains a
  `decode_fn` parameter; new `_capture_loop_sck` and `_validate_sck_display_mode`;
  `run()` branches on `config.capture_backend`.
- `tests/test_broadcaster_config.py` — **modify**: cover the new fields.
- `tests/test_virtual_display.py` — **new**.
- `tests/test_physical_display.py` — **new**.
- `tests/test_capture_sck.py` — **new**: only the pure BGRA→RGBA function.
- `tests/test_launcher.py` — **modify**: replace the now-obsolete
  `test_run_rejects_unimplemented_sck_backend`, add validation + decode_fn
  tests.
- `README.md` — **modify**: document `capture_backend: sck` as available,
  update the "Known limitation" paragraph once live verification confirms
  the degradation is absent (Task 7).

---

### Task 1: Config schema for the `sck` backend

**Files:**
- Modify: `ndi_broadcaster/config.py`
- Modify: `config/broadcaster.yaml`
- Modify: `tests/test_broadcaster_config.py`

**Interfaces:**
- Produces: `BroadcasterConfig.sck_display_mode: Literal["virtual", "physical"] | None`,
  `BroadcasterConfig.sck_virtual_display_name: str`,
  `BroadcasterConfig.sck_physical_display_name: str | None`. All later tasks
  read these three fields directly off a `BroadcasterConfig` instance.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_broadcaster_config.py`:

```python
def test_sck_fields_have_documented_defaults():
    config = BroadcasterConfig()
    assert config.sck_display_mode is None
    assert config.sck_virtual_display_name == "Layout Driver Virtual Display"
    assert config.sck_physical_display_name is None


def test_sck_display_mode_rejects_unknown_value():
    with pytest.raises(ValidationError):
        BroadcasterConfig(sck_display_mode="bogus")
```

Also replace the body of the existing `test_load_broadcaster_config` with:

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
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_broadcaster_config.py -v`
Expected: `test_sck_fields_have_documented_defaults` and
`test_sck_display_mode_rejects_unknown_value` FAIL with
`AttributeError`/no error respectively (the new fields don't exist yet);
`test_load_broadcaster_config` FAILs on the new keyword arguments not being
accepted by `BroadcasterConfig`.

- [ ] **Step 3: Add the fields to `BroadcasterConfig`**

In `ndi_broadcaster/config.py`, replace the class body with:

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

- [ ] **Step 4: Document the fields in `config/broadcaster.yaml`**

Replace the file's contents with:

```yaml
target_url: "https://localhost:8443/"
capture_backend: "cdp"                          # "cdp" | "sck"
sck_display_mode: null                          # "virtual" | "physical" -- required if capture_backend: sck
sck_virtual_display_name: "Layout Driver Virtual Display"  # sck + virtual only
sck_physical_display_name: null                 # sck + physical only -- substring-matched against connected display names
ndi_source_name: "Layout Driver"
width: 3840
height: 2160
fps: 30
healthz_timeout_seconds: 30.0
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_broadcaster_config.py -v`
Expected: PASS (all tests in the file, including the pre-existing ones).

- [ ] **Step 6: Commit**

```bash
git add ndi_broadcaster/config.py config/broadcaster.yaml tests/test_broadcaster_config.py
git commit -m "ndi_broadcaster: add sck display-mode config fields"
```

---

### Task 2: PyObjC base dependencies

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `objc`, `AppKit`/`Foundation`, `Quartz`, `CoreMedia`,
  `ScreenCaptureKit` importable in the project's `.venv` on macOS. Tasks 3-5
  depend on these being installed.

No test code applies to this task — it's a dependency-installation step. Its
"test" is a live import check.

- [ ] **Step 1: Add the dependencies**

In `pyproject.toml`, add to the `dependencies` list (after `"numpy>=2.1"`):

```toml
    "pyobjc-core>=10.3; sys_platform == 'darwin'",
    "pyobjc-framework-Cocoa>=10.3; sys_platform == 'darwin'",
    "pyobjc-framework-Quartz>=10.3; sys_platform == 'darwin'",
    "pyobjc-framework-ScreenCaptureKit>=10.3; sys_platform == 'darwin'",
    "pyobjc-framework-CoreMedia>=10.3; sys_platform == 'darwin'",
```

These are gated to macOS because ScreenCaptureKit/CGVirtualDisplay have no
non-macOS equivalent; `capture_backend: cdp` never needs them.

- [ ] **Step 2: Sync and verify the import**

Run: `uv sync`
Expected: resolves and installs the five packages (confirmed resolvable at
the time this plan was written: all five resolve together at version
`12.2.1`). If a specific version floor fails to resolve, drop the `>=10.3`
constraint down to whatever `uv sync` resolves, rather than pinning blind.

Run: `uv run python3 -c "import objc, AppKit, Quartz, CoreMedia, ScreenCaptureKit; print('ok')"`
Expected: prints `ok` with no traceback.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "ndi_broadcaster: add PyObjC base dependencies for the sck backend"
```

---

### Task 3: Virtual display helper (Swift binary + Python wrapper)

**Files:**
- Create: `ndi_broadcaster/vdisplay_helper/main.swift`
- Create: `ndi_broadcaster/vdisplay_helper/CGVirtualDisplayPrivate.h`
- Create: `ndi_broadcaster/virtual_display.py`
- Create: `tests/test_virtual_display.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `DisplayInfo` (frozen dataclass: `display_id: int, x: int, y:
  int, width: int, height: int`), `ensure_helper_built(helper_dir: Path) ->
  Path`, `start_vdisplay_helper(binary_path: Path, width: int, height: int,
  name: str) -> tuple[subprocess.Popen, DisplayInfo]`,
  `wait_for_settled_bounds(display_id: int, width: int, height: int,
  timeout_s: float = 20.0) -> DisplayInfo`. Task 4 imports `DisplayInfo`
  from this module (does not redefine it). Task 6 calls all three functions.

This task's `wait_for_settled_bounds` and the Swift helper itself are not
unit tested (per Global Constraints) — they're verified live in Task 7. This
task's TDD cycle covers only `ensure_helper_built` and
`start_vdisplay_helper`, which are pure subprocess control flow.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_virtual_display.py`:

```python
import json
import os
import subprocess
from pathlib import Path

import pytest

from ndi_broadcaster.virtual_display import (
    DisplayInfo,
    ensure_helper_built,
    start_vdisplay_helper,
)


def test_ensure_helper_built_skips_compilation_when_binary_is_up_to_date(tmp_path, monkeypatch):
    source = tmp_path / "main.swift"
    source.write_text("// source")
    header = tmp_path / "CGVirtualDisplayPrivate.h"
    header.write_text("// header")
    binary = tmp_path / "vdisplay_helper"
    binary.write_text("compiled")
    os.utime(source, (1000, 1000))
    os.utime(binary, (2000, 2000))

    def fail_if_called(*args, **kwargs):
        pytest.fail("subprocess.run must not be called when the binary is up to date")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    result = ensure_helper_built(tmp_path)

    assert result == binary


def test_ensure_helper_built_compiles_when_binary_is_missing(tmp_path, monkeypatch):
    source = tmp_path / "main.swift"
    source.write_text("// source")
    header = tmp_path / "CGVirtualDisplayPrivate.h"
    header.write_text("// header")
    binary = tmp_path / "vdisplay_helper"
    calls = []

    def fake_run(args, check):
        calls.append(args)
        binary.write_text("compiled")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_helper_built(tmp_path)

    assert result == binary
    assert calls == [
        ["swiftc", "-O", "-o", str(binary), str(source), "-import-objc-header", str(header)]
    ]


def test_ensure_helper_built_recompiles_when_source_is_newer(tmp_path, monkeypatch):
    source = tmp_path / "main.swift"
    source.write_text("// source")
    header = tmp_path / "CGVirtualDisplayPrivate.h"
    header.write_text("// header")
    binary = tmp_path / "vdisplay_helper"
    binary.write_text("stale")
    os.utime(binary, (1000, 1000))
    os.utime(source, (2000, 2000))
    calls = []

    def fake_run(args, check):
        calls.append(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ensure_helper_built(tmp_path)

    assert len(calls) == 1


def test_start_vdisplay_helper_parses_json_report(monkeypatch):
    payload = json.dumps({"displayID": 69732865, "x": 5000, "y": 0, "width": 3840, "height": 2160})

    class _FakeProc:
        def __init__(self):
            self.stdout = _FakeStdout(payload + "\n")

    class _FakeStdout:
        def __init__(self, line):
            self._line = line

        def readline(self):
            return self._line

    fake_proc = _FakeProc()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: fake_proc)

    proc, info = start_vdisplay_helper(Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display")

    assert proc is fake_proc
    assert info == DisplayInfo(display_id=69732865, x=5000, y=0, width=3840, height=2160)


def test_start_vdisplay_helper_raises_when_no_output(monkeypatch):
    class _FakeStdout:
        def readline(self):
            return ""

    class _FakeStderr:
        def read(self):
            return "swiftc binary crashed"

    class _FakeProc:
        stdout = _FakeStdout()
        stderr = _FakeStderr()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _FakeProc())

    with pytest.raises(RuntimeError, match="swiftc binary crashed"):
        start_vdisplay_helper(Path("/fake/vdisplay_helper"), 3840, 2160, "Test Display")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_virtual_display.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ndi_broadcaster.virtual_display'`.

- [ ] **Step 3: Create the Swift helper source**

Create `ndi_broadcaster/vdisplay_helper/CGVirtualDisplayPrivate.h`:

```objc
//
//  CGVirtualDisplayPrivate.h
//
//  Private CGVirtualDisplay* API declarations, lifted from DeskPad
//  (https://github.com/Stengo/DeskPad), originally by Khaos Tian
//  (https://github.com/KhaosT/CGVirtualDisplay). These classes already ship
//  inside the CoreGraphics/SkyLight private framework on-device; nothing is
//  dlopen'd, they just aren't in the public headers.
//

#import <Cocoa/Cocoa.h>
#import <CoreGraphics/CoreGraphics.h>

NS_ASSUME_NONNULL_BEGIN

@class CGVirtualDisplayDescriptor;

@interface CGVirtualDisplayMode : NSObject

@property(readonly, nonatomic) CGFloat refreshRate;
@property(readonly, nonatomic) NSUInteger width;
@property(readonly, nonatomic) NSUInteger height;

- (instancetype)initWithWidth:(NSUInteger)arg1 height:(NSUInteger)arg2 refreshRate:(CGFloat)arg3;

@end

@interface CGVirtualDisplaySettings : NSObject

@property(retain, nonatomic) NSArray<CGVirtualDisplayMode *> *modes;
@property(nonatomic) unsigned int hiDPI;

- (instancetype)init;

@end

@interface CGVirtualDisplay : NSObject

@property(readonly, nonatomic) NSArray *modes; // @synthesize modes=_modes;
@property(readonly, nonatomic) unsigned int hiDPI; // @synthesize hiDPI=_hiDPI;
@property(readonly, nonatomic) CGDirectDisplayID displayID; // @synthesize displayID=_displayID;
@property(readonly, nonatomic) id terminationHandler; // @synthesize terminationHandler=_terminationHandler;
@property(readonly, nonatomic) dispatch_queue_t queue; // @synthesize queue=_queue;
@property(readonly, nonatomic) unsigned int maxPixelsHigh; // @synthesize maxPixelsHigh=_maxPixelsHigh;
@property(readonly, nonatomic) unsigned int maxPixelsWide; // @synthesize maxPixelsWide=_maxPixelsWide;
@property(readonly, nonatomic) CGSize sizeInMillimeters; // @synthesize sizeInMillimeters=_sizeInMillimeters;
@property(readonly, nonatomic) NSString *name; // @synthesize name=_name;
@property(readonly, nonatomic) unsigned int serialNum; // @synthesize serialNum=_serialNum;
@property(readonly, nonatomic) unsigned int productID; // @synthesize productID=_productID;
@property(readonly, nonatomic) unsigned int vendorID; // @synthesize vendorID=_vendorID;

- (instancetype)initWithDescriptor:(CGVirtualDisplayDescriptor *)arg1;
- (BOOL)applySettings:(CGVirtualDisplaySettings *)arg1;

@end

@interface CGVirtualDisplayDescriptor : NSObject

@property(retain, nonatomic) dispatch_queue_t queue; // @synthesize queue=_queue;
@property(retain, nonatomic) NSString *name; // @synthesize name=_name;
@property(nonatomic) unsigned int maxPixelsHigh; // @synthesize maxPixelsHigh=_maxPixelsHigh;
@property(nonatomic) unsigned int maxPixelsWide; // @synthesize maxPixelsWide=_maxPixelsWide;
@property(nonatomic) CGSize sizeInMillimeters; // @synthesize sizeInMillimeters=_sizeInMillimeters;
@property(nonatomic) unsigned int serialNum; // @synthesize serialNum=_serialNum;
@property(nonatomic) unsigned int productID; // @synthesize productID=_productID;
@property(nonatomic) unsigned int vendorID; // @synthesize vendorID=_vendorID;
@property(copy, nonatomic) void (^terminationHandler)(id, CGVirtualDisplay*);

- (instancetype)init;
- (nullable dispatch_queue_t)dispatchQueue;
- (void)setDispatchQueue:(dispatch_queue_t)arg1;

@end

NS_ASSUME_NONNULL_END
```

Create `ndi_broadcaster/vdisplay_helper/main.swift`:

```swift
// vdisplay_helper.swift
//
// Standalone command-line helper that creates a fully virtual (no physical
// hardware) macOS display using the private CGVirtualDisplay* API surface,
// the same one used by DeskPad / BetterDisplay. Declarations come from
// CGVirtualDisplayPrivate.h.
//
// Usage:
//   ./vdisplay_helper <width> <height> [name]
//
// Prints one JSON line to stdout once the display is live:
//   {"displayID": 69732865, "x": 5000, "y": 0, "width": 3840, "height": 2160}
//
// Then blocks (RunLoop.main.run()) holding the display open until it
// receives SIGINT/SIGTERM, at which point it exits (deallocating the
// CGVirtualDisplay object, which tears the display down).

import Cocoa
import CoreGraphics
import Foundation

setbuf(stdout, nil)

let args = CommandLine.arguments
let width = args.count > 1 ? Int(args[1]) ?? 3840 : 3840
let height = args.count > 2 ? Int(args[2]) ?? 2160 : 2160
let displayName = args.count > 3 ? args[3] : "Layout Driver Virtual Display"

// Place the virtual display far to the right of any real display so its
// coordinate space can never overlap the physical desktop, and so the
// origin we hand to Chromium's --window-position is unambiguous.
let desiredOriginX: Int32 = 5000
let desiredOriginY: Int32 = 0

FileHandle.standardError.write("vdisplay_helper: creating \(width)x\(height) virtual display named \(displayName)\n".data(using: .utf8)!)

let descriptor = CGVirtualDisplayDescriptor()
descriptor.setDispatchQueue(DispatchQueue.main)
descriptor.name = displayName
descriptor.maxPixelsWide = UInt32(width)
descriptor.maxPixelsHigh = UInt32(height)
descriptor.sizeInMillimeters = CGSize(width: 1600, height: 900)
// Randomized identity fields avoid colliding with any cached WindowServer
// display-preference record keyed by vendor/product/serial from a prior run
// of this or another virtual-display tool on this machine.
descriptor.productID = UInt32.random(in: 0x1000...0xFFFE)
descriptor.vendorID = UInt32.random(in: 0x1000...0xFFFE)
descriptor.serialNum = UInt32(ProcessInfo.processInfo.processIdentifier)
descriptor.terminationHandler = { _, _ in
    FileHandle.standardError.write("vdisplay_helper: termination handler fired\n".data(using: .utf8)!)
}

let display = CGVirtualDisplay(descriptor: descriptor)

let mode = CGVirtualDisplayMode(width: UInt(width), height: UInt(height), refreshRate: 60.0)
let settings = CGVirtualDisplaySettings()
settings.hiDPI = 0
settings.modes = [mode]

let applied = display.apply(settings)
FileHandle.standardError.write("vdisplay_helper: applySettings returned \(applied), displayID=\(display.displayID)\n".data(using: .utf8)!)

if !applied {
    FileHandle.standardError.write("vdisplay_helper: FATAL applySettings failed\n".data(using: .utf8)!)
    exit(1)
}

// Explicitly select the display mode matching our requested resolution.
// GOTCHA: calling CGConfigureDisplayWithDisplayMode immediately (within
// ~0.5s) after the display is created reports success but is silently
// clobbered a moment later by WindowServer's async "new display attached"
// auto-arrange/default-mode logic. Reading CGDisplayBounds back afterward
// confirms the mode reverted (observed reverting to 1920x1080). The fix:
// wait for the display to settle (~1.5s) before touching mode/origin, then
// verify and retry a few times if it didn't stick.
func currentSize(_ displayID: CGDirectDisplayID) -> (Int, Int) {
    let mode = CGDisplayCopyDisplayMode(displayID)
    return (mode?.width ?? -1, mode?.height ?? -1)
}

func findMode(_ displayID: CGDirectDisplayID, w: Int, h: Int) -> CGDisplayMode? {
    guard let allModes = CGDisplayCopyAllDisplayModes(displayID, nil) as? [CGDisplayMode] else {
        return nil
    }
    return allModes.first { $0.width == w && $0.height == h }
}

Thread.sleep(forTimeInterval: 1.5)

if let allModes = CGDisplayCopyAllDisplayModes(display.displayID, nil) as? [CGDisplayMode] {
    FileHandle.standardError.write("vdisplay_helper: available modes: \(allModes.map { ($0.width, $0.height) })\n".data(using: .utf8)!)
}

var attempt = 0
let maxAttempts = 6
while attempt < maxAttempts {
    attempt += 1
    guard let targetMode = findMode(display.displayID, w: width, h: height) else {
        FileHandle.standardError.write("vdisplay_helper: WARNING no matching CGDisplayMode found for \(width)x\(height) (attempt \(attempt))\n".data(using: .utf8)!)
        Thread.sleep(forTimeInterval: 0.5)
        continue
    }

    var config: CGDisplayConfigRef?
    let beginResult = CGBeginDisplayConfiguration(&config)
    if beginResult == .success, let config = config {
        CGConfigureDisplayOrigin(config, display.displayID, desiredOriginX, desiredOriginY)
        let modeResult = CGConfigureDisplayWithDisplayMode(config, display.displayID, targetMode, nil)
        let completeResult = CGCompleteDisplayConfiguration(config, .permanently)
        FileHandle.standardError.write("vdisplay_helper: attempt \(attempt): configureMode=\(modeResult.rawValue) complete=\(completeResult.rawValue)\n".data(using: .utf8)!)
    } else {
        FileHandle.standardError.write("vdisplay_helper: CGBeginDisplayConfiguration failed -> \(beginResult.rawValue)\n".data(using: .utf8)!)
    }

    Thread.sleep(forTimeInterval: 0.6)
    let (w, h) = currentSize(display.displayID)
    FileHandle.standardError.write("vdisplay_helper: after attempt \(attempt), current size = \(w)x\(h)\n".data(using: .utf8)!)
    if w == width && h == height {
        break
    }
}

// Never trust the requested origin as applied -- always read back the real
// bounds WindowServer settled on.
let bounds = CGDisplayBounds(display.displayID)
FileHandle.standardError.write("vdisplay_helper: final bounds = \(bounds)\n".data(using: .utf8)!)

let payload: [String: Any] = [
    "displayID": display.displayID,
    "x": Int(bounds.origin.x),
    "y": Int(bounds.origin.y),
    "width": Int(bounds.size.width),
    "height": Int(bounds.size.height),
]
if let jsonData = try? JSONSerialization.data(withJSONObject: payload),
   let jsonString = String(data: jsonData, encoding: .utf8) {
    print(jsonString)
}

// Keep the display alive (and the process holding `display` retained) until
// killed.
let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigintSource.setEventHandler {
    FileHandle.standardError.write("vdisplay_helper: SIGINT received, exiting\n".data(using: .utf8)!)
    exit(0)
}
sigintSource.resume()
signal(SIGINT, SIG_IGN)

let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigtermSource.setEventHandler {
    FileHandle.standardError.write("vdisplay_helper: SIGTERM received, exiting\n".data(using: .utf8)!)
    exit(0)
}
sigtermSource.resume()
signal(SIGTERM, SIG_IGN)

withExtendedLifetime(display) {
    RunLoop.main.run()
}
```

- [ ] **Step 4: Create `ndi_broadcaster/virtual_display.py`**

```python
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import AppKit
import Quartz


@dataclass(frozen=True)
class DisplayInfo:
    display_id: int
    x: int
    y: int
    width: int
    height: int


def ensure_helper_built(helper_dir: Path) -> Path:
    """Build vdisplay_helper via swiftc if the compiled binary is missing or
    older than its source, returning the binary's path.

    Not checked into the repo: the binary is architecture-specific and this
    is a one-line build, so it's compiled on first use per machine and
    cached next to the source for subsequent runs.
    """
    binary_path = helper_dir / "vdisplay_helper"
    source_path = helper_dir / "main.swift"
    header_path = helper_dir / "CGVirtualDisplayPrivate.h"
    if binary_path.exists() and binary_path.stat().st_mtime >= source_path.stat().st_mtime:
        return binary_path
    subprocess.run(
        [
            "swiftc",
            "-O",
            "-o",
            str(binary_path),
            str(source_path),
            "-import-objc-header",
            str(header_path),
        ],
        check=True,
    )
    return binary_path


def start_vdisplay_helper(
    binary_path: Path, width: int, height: int, name: str
) -> tuple[subprocess.Popen, DisplayInfo]:
    """Launch the compiled vdisplay_helper and parse its one-line JSON startup report."""
    proc = subprocess.Popen(
        [str(binary_path), str(width), str(height), name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    line = proc.stdout.readline()
    if not line:
        err = proc.stderr.read()
        raise RuntimeError(f"vdisplay_helper produced no stdout; stderr:\n{err}")
    payload = json.loads(line)
    info = DisplayInfo(
        display_id=payload["displayID"],
        x=payload["x"],
        y=payload["y"],
        width=payload["width"],
        height=payload["height"],
    )
    return proc, info


def wait_for_settled_bounds(
    display_id: int, width: int, height: int, timeout_s: float = 20.0
) -> DisplayInfo:
    """Poll CGDisplayBounds + NSScreen enumeration until the virtual display's
    reported frame is stable across consecutive polls and matches (width,
    height), and until NSScreen (the API Chromium/AppKit actually consult for
    window placement) also lists it. Returns the final, settled DisplayInfo.

    GOTCHA: NSScreen.screens() caches its list and only refreshes when the
    process's run loop processes the display-reconfiguration notification. A
    plain time.sleep() polling loop never lets that happen, so
    NSScreen.screens() can appear to never pick up the new virtual display
    even though CGDisplayBounds/CGGetOnlineDisplayList already see it.
    Spinning the run loop (CFRunLoopRunInMode) instead of sleeping fixes it.
    NSApplication's shared instance is instantiated once so AppKit's
    screen-parameter machinery is active in a bare script (not a full .app
    bundle) -- without it, some CoreGraphics/WindowServer calls in this
    module's callers assert-crash with CGS_REQUIRE_INIT.
    """
    AppKit.NSApplication.sharedApplication()

    deadline = time.time() + timeout_s
    last_bounds = None
    stable_count = 0
    while time.time() < deadline:
        Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.5, False)

        bounds = Quartz.CGDisplayBounds(display_id)
        cur = (
            int(bounds.origin.x),
            int(bounds.origin.y),
            int(bounds.size.width),
            int(bounds.size.height),
        )

        in_nsscreen = any(
            screen.deviceDescription().get("NSScreenNumber") == display_id
            for screen in AppKit.NSScreen.screens()
        )

        if cur == last_bounds and cur[2] == width and cur[3] == height and in_nsscreen:
            stable_count += 1
            if stable_count >= 3:
                return DisplayInfo(display_id, *cur)
        else:
            stable_count = 0
        last_bounds = cur

    raise TimeoutError(
        f"virtual display {display_id} did not settle to {width}x{height} "
        f"and appear in NSScreen within {timeout_s}s; last bounds={last_bounds}"
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_virtual_display.py -v`
Expected: PASS (5 tests). `wait_for_settled_bounds` has no test in this file
per the Global Constraints — it's exercised live in Task 7.

- [ ] **Step 6: Commit**

```bash
git add ndi_broadcaster/vdisplay_helper/ ndi_broadcaster/virtual_display.py tests/test_virtual_display.py
git commit -m "ndi_broadcaster: add virtual display helper (Swift + Python wrapper)"
```

---

### Task 4: Physical display resolution

**Files:**
- Create: `ndi_broadcaster/physical_display.py`
- Create: `tests/test_physical_display.py`

**Interfaces:**
- Consumes: `DisplayInfo` from `ndi_broadcaster.virtual_display` (Task 3) —
  imported, not redefined.
- Produces: `find_physical_display(name_substring: str) -> DisplayInfo`,
  raising `ValueError` on no match. Task 6 calls this in the `physical`
  branch of `_capture_loop_sck`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_physical_display.py`:

```python
import pytest

from ndi_broadcaster.physical_display import find_physical_display
from ndi_broadcaster.virtual_display import DisplayInfo


def test_find_physical_display_matches_by_case_insensitive_substring(monkeypatch):
    target = DisplayInfo(display_id=3, x=1920, y=0, width=3840, height=2160)
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [
            ("Built-in Retina Display", DisplayInfo(1, 0, 0, 1728, 1117)),
            ("LG UltraFine 4K", target),
        ],
    )

    assert find_physical_display("ultrafine") == target


def test_find_physical_display_raises_listing_known_names(monkeypatch):
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [("Built-in Retina Display", DisplayInfo(1, 0, 0, 1728, 1117))],
    )

    with pytest.raises(ValueError, match="Built-in Retina Display"):
        find_physical_display("Nonexistent Display")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_physical_display.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ndi_broadcaster.physical_display'`.

- [ ] **Step 3: Create `ndi_broadcaster/physical_display.py`**

```python
from __future__ import annotations

import AppKit
import Quartz

from .virtual_display import DisplayInfo


def _enumerate_screens() -> list[tuple[str, DisplayInfo]]:
    """Return (localized name, resolved bounds) for every connected display.

    Factored out from find_physical_display so tests can monkeypatch this
    one function instead of the whole NSScreen/Quartz surface.
    """
    AppKit.NSApplication.sharedApplication()
    result: list[tuple[str, DisplayInfo]] = []
    for screen in AppKit.NSScreen.screens():
        display_id = screen.deviceDescription().get("NSScreenNumber")
        bounds = Quartz.CGDisplayBounds(display_id)
        result.append(
            (
                screen.localizedName(),
                DisplayInfo(
                    display_id=int(display_id),
                    x=int(bounds.origin.x),
                    y=int(bounds.origin.y),
                    width=int(bounds.size.width),
                    height=int(bounds.size.height),
                ),
            )
        )
    return result


def find_physical_display(name_substring: str) -> DisplayInfo:
    """Match a connected display by NSScreen.localizedName substring
    (case-insensitive -- the same convention layout_server/audio.py's
    match_device_by_name already uses for audio devices) and return its
    real CGDisplayBounds.

    Raises ValueError naming every connected display's actual name if no
    match is found, so an operator can copy the right value directly from
    the error. Returning real bounds (not just validating the name) matters:
    with multiple displays connected, launching Chromium with no explicit
    --window-position risks it landing on whichever display the OS default
    picks, which could be too small and silently reproduce the original
    window-clamping bug this backend exists to avoid.
    """
    screens = _enumerate_screens()
    lowered = name_substring.lower()
    for name, info in screens:
        if lowered in name.lower():
            return info
    known_names = sorted(name for name, _ in screens)
    raise ValueError(
        f"no connected display matched {name_substring!r}; "
        f"connected display names: {known_names}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_physical_display.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add ndi_broadcaster/physical_display.py tests/test_physical_display.py
git commit -m "ndi_broadcaster: add physical display resolution by name"
```

---

### Task 5: SCK capture backend (`SckCapture`)

**Files:**
- Modify: `ndi_broadcaster/capture_sck.py` (replace the stub)
- Create: `tests/test_capture_sck.py`

**Interfaces:**
- Produces: `ScreenCaptureKitUnavailableError` (unchanged from the stub),
  `bgra_buffer_to_rgba_bytes(raw: bytes, width: int, height: int,
  bytes_per_row: int) -> bytes` (pure function), `SckCapture(window_title_hint:
  str, width: int, height: int, fps: int, on_frame: Callable[[bytes], None])`
  with `.start()` and `.stop()`. `on_frame` is called once per captured
  frame with tightly-packed RGBA bytes of exactly `height * width * 4`
  bytes. Task 6 constructs `SckCapture` with `on_frame=frame_slot.put`
  (`_LatestFrameSlot.put`, Task 6's existing type) — this is why the
  constructor takes a plain callback instead of importing `_LatestFrameSlot`
  directly: `launcher.py` already imports from `capture_sck.py`, so the
  reverse import would be circular.

Per the Global Constraints, `SckCapture`'s actual `SCStream` wiring is
verified live (Task 7), not unit tested. Only the pure BGRA→RGBA conversion
is unit tested here.

- [ ] **Step 1: Write the failing test**

Create `tests/test_capture_sck.py`:

```python
import pytest

pytest.importorskip("ScreenCaptureKit")

from ndi_broadcaster.capture_sck import bgra_buffer_to_rgba_bytes


def test_bgra_buffer_to_rgba_bytes_reorders_channels_and_strips_row_padding():
    # 2x1 image; bytes_per_row is padded to 12 bytes (one extra BGRA pixel of
    # padding) to prove the stride is stripped rather than corrupting the row.
    pixel0 = bytes([10, 20, 30, 255])  # B, G, R, A
    pixel1 = bytes([40, 50, 60, 255])
    padding = bytes([0, 0, 0, 0])
    raw = pixel0 + pixel1 + padding

    result = bgra_buffer_to_rgba_bytes(raw, width=2, height=1, bytes_per_row=12)

    assert result == bytes([30, 20, 10, 255, 60, 50, 40, 255])
```

`pytest.importorskip("ScreenCaptureKit")` matches this project's existing
precedent (flux-gallery's tests skip themselves unless the extra is
installed) — this file only runs where the macOS-only PyObjC dependencies
from Task 2 are actually present.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_capture_sck.py -v`
Expected: FAIL — `bgra_buffer_to_rgba_bytes` doesn't exist yet (the stub
only has `ScreenCaptureKitUnavailableError` and `SckCapture.latest_frame`
raising `NotImplementedError`).

- [ ] **Step 3: Replace `ndi_broadcaster/capture_sck.py`**

```python
from __future__ import annotations

"""
macOS-only capture backend using ScreenCaptureKit via PyObjC, matching a
window by title substring. Selected via config/broadcaster.yaml:
capture_backend: sck. Requires Screen Recording permission and a headed
display (real or virtual -- see ndi_broadcaster/virtual_display.py and
ndi_broadcaster/physical_display.py for how the display is resolved before
this class is ever constructed).

Ported from a validated proof-of-concept spike (framework spec Sec 3.4a):
SCStream delivered continuously changing frames at a sustained ~29.3fps
average with zero repeated frames over a 5-minute run, independent of
Playwright's own driver process -- the actual bottleneck this backend
removes from the capture path.
"""

import logging
import threading
import time
from collections.abc import Callable

import numpy as np

logger = logging.getLogger(__name__)

# 'BGRA' as a FourCC integer -- matches DeskPad's CGDisplayStream usage and
# the validated spike; ScreenCaptureKit delivers pixel buffers in this format.
_BGRA_PIXEL_FORMAT = 1111970369


class ScreenCaptureKitUnavailableError(RuntimeError):
    pass


try:
    import objc
    from Foundation import NSObject

    import CoreMedia as CM
    import Quartz
    import ScreenCaptureKit as SCK
except ImportError as exc:  # pragma: no cover - exercised only off-macOS
    raise ScreenCaptureKitUnavailableError(
        "capture_backend: sck requires macOS + pyobjc-framework-ScreenCaptureKit"
    ) from exc


def bgra_buffer_to_rgba_bytes(raw: bytes, width: int, height: int, bytes_per_row: int) -> bytes:
    """Convert a raw BGRA CVPixelBuffer read (with possible row padding) into
    tightly packed RGBA bytes of exactly (height, width, 4).

    Pure function, no PyObjC types -- testable with synthetic byte buffers.
    ScreenCaptureKit pixel buffers are frequently padded to a stride wider
    than width * 4 bytes; slicing by bytes_per_row before reshaping strips
    that padding rather than corrupting the image with it.
    """
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(height, bytes_per_row // 4, 4)
    arr = arr[:, :width, [2, 1, 0, 3]]  # BGRA -> RGBA
    return np.ascontiguousarray(arr).tobytes()


def _get_shareable_content(timeout_s: float = 10.0):
    result: dict = {}
    done = threading.Event()

    def handler(content, error):
        result["content"] = content
        result["error"] = error
        done.set()

    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(handler)
    if not done.wait(timeout_s):
        raise TimeoutError("SCShareableContent did not respond")
    if result["error"] is not None:
        raise RuntimeError(f"SCShareableContent error: {result['error']}")
    return result["content"]


def _find_target_window(content, title_hint: str):
    matches = [w for w in content.windows() if title_hint in (w.title() or "")]
    if not matches:
        found_titles = sorted({w.title() for w in content.windows() if w.title()})
        raise ValueError(
            f"no window found with title containing {title_hint!r}; "
            f"window titles found: {found_titles}"
        )
    return matches[0]


class _StreamOutput(NSObject):
    def initWithOnFrame_(self, on_frame: Callable[[bytes], None]):
        self = objc.super(_StreamOutput, self).init()
        if self is None:
            return None
        self._on_frame = on_frame
        self._frame_count = 0
        self._lock = threading.Lock()
        self._last_log = time.monotonic()
        return self

    def stream_didOutputSampleBuffer_ofType_(self, stream, sampleBuffer, outputType):
        if not CM.CMSampleBufferIsValid(sampleBuffer):
            return
        pixel_buffer = CM.CMSampleBufferGetImageBuffer(sampleBuffer)
        if pixel_buffer is None:
            return

        Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)
        try:
            width = Quartz.CVPixelBufferGetWidth(pixel_buffer)
            height = Quartz.CVPixelBufferGetHeight(pixel_buffer)
            bytes_per_row = Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer)
            base_address = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
            raw = bytes(base_address.as_buffer(bytes_per_row * height))
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)

        try:
            self._on_frame(bgra_buffer_to_rgba_bytes(raw, width, height, bytes_per_row))
        except Exception:
            logger.exception("SCK frame callback raised; dropping this frame")

        with self._lock:
            self._frame_count += 1
            now = time.monotonic()
            if now - self._last_log >= 5.0:
                logger.info(
                    "SCK capture: %.1f fps in the last %.1fs",
                    self._frame_count / (now - self._last_log),
                    now - self._last_log,
                )
                self._frame_count = 0
                self._last_log = now


class _StreamDelegate(NSObject):
    def stream_didStopWithError_(self, stream, error):
        logger.warning("SCK stream stopped with error: %s", error)


class SckCapture:
    def __init__(
        self,
        window_title_hint: str,
        width: int,
        height: int,
        fps: int,
        on_frame: Callable[[bytes], None],
    ) -> None:
        self._window_title_hint = window_title_hint
        self._width = width
        self._height = height
        self._fps = fps
        self._on_frame = on_frame
        self._stream = None
        self._output = None

    def start(self) -> None:
        content = _get_shareable_content()
        window = _find_target_window(content, self._window_title_hint)

        content_filter = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)

        config = SCK.SCStreamConfiguration.alloc().init()
        config.setWidth_(self._width)
        config.setHeight_(self._height)
        config.setMinimumFrameInterval_(CM.CMTimeMake(1, self._fps))
        config.setQueueDepth_(8)
        config.setShowsCursor_(False)
        config.setPixelFormat_(_BGRA_PIXEL_FORMAT)

        self._output = _StreamOutput.alloc().initWithOnFrame_(self._on_frame)
        delegate = _StreamDelegate.alloc().init()
        self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, config, delegate
        )
        added_ok = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._output, SCK.SCStreamOutputTypeScreen, None, objc.NULL
        )
        if not added_ok:
            raise RuntimeError("SCStream.addStreamOutput_type_sampleHandlerQueue_error_ failed")

        start_done = threading.Event()
        start_result: dict = {}

        def start_handler(error):
            start_result["error"] = error
            start_done.set()

        self._stream.startCaptureWithCompletionHandler_(start_handler)
        if not start_done.wait(10.0):
            raise TimeoutError("SCStream startCapture did not complete in time")
        if start_result["error"] is not None:
            raise RuntimeError(f"SCStream startCapture error: {start_result['error']}")

    def stop(self) -> None:
        if self._stream is None:
            return
        stop_done = threading.Event()
        self._stream.stopCaptureWithCompletionHandler_(lambda error: stop_done.set())
        stop_done.wait(10.0)
        self._stream = None
        self._output = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_capture_sck.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full test suite to confirm nothing else broke**

Run: `uv run pytest -v`
Expected: all tests PASS (this replaces a stub whose only prior consumer was
its own now-removed `NotImplementedError`-raising `latest_frame` method — no
other module called it yet).

- [ ] **Step 6: Commit**

```bash
git add ndi_broadcaster/capture_sck.py tests/test_capture_sck.py
git commit -m "ndi_broadcaster: implement SckCapture with SCStream wiring"
```

---

### Task 6: Wire `sck` into the launcher

**Files:**
- Modify: `ndi_broadcaster/launcher.py`
- Modify: `tests/test_launcher.py`

**Interfaces:**
- Consumes: `DisplayInfo`, `ensure_helper_built`, `start_vdisplay_helper`,
  `wait_for_settled_bounds` (Task 3); `find_physical_display` (Task 4);
  `SckCapture` (Task 5); `BroadcasterConfig.sck_display_mode`,
  `.sck_virtual_display_name`, `.sck_physical_display_name` (Task 1).
- Produces: `_validate_sck_display_mode(config: BroadcasterConfig) -> None`,
  `_capture_loop_sck(config, sender, stop_event) -> None` (async, same
  signature shape as the existing `_capture_loop`), `run()` now dispatches
  to either loop based on `config.capture_backend`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_launcher.py`, add `import threading`, `import time`, and
`import numpy as np` near the top (alongside the existing `import http.server`
/ `import platform` / etc.), and add `_sender_thread_loop` and
`_validate_sck_display_mode` into the existing
`from ndi_broadcaster.launcher import (...)` tuple, so it reads:

```python
from ndi_broadcaster.launcher import (
    REPO_ROOT,
    HealthCheckTimeoutError,
    _chrome_launch_args,
    _LatestFrameSlot,
    _log_format,
    _sender_thread_loop,
    _validate_sck_display_mode,
    resolve_launcher_paths,
    resolve_target_url,
    run,
    wait_for_healthy,
)
```

Replace the existing `test_run_rejects_unimplemented_sck_backend` (it tested
behavior this task removes) with:

```python
def test_validate_sck_display_mode_noop_for_cdp():
    _validate_sck_display_mode(BroadcasterConfig(capture_backend="cdp"))  # must not raise


def test_validate_sck_display_mode_requires_mode():
    with pytest.raises(ValueError, match="sck_display_mode"):
        _validate_sck_display_mode(BroadcasterConfig(capture_backend="sck"))


def test_validate_sck_display_mode_virtual_does_not_require_physical_name():
    _validate_sck_display_mode(
        BroadcasterConfig(capture_backend="sck", sck_display_mode="virtual")
    )  # must not raise


def test_validate_sck_display_mode_physical_requires_name():
    with pytest.raises(ValueError, match="sck_physical_display_name"):
        _validate_sck_display_mode(
            BroadcasterConfig(capture_backend="sck", sck_display_mode="physical")
        )


def test_run_requires_sck_display_mode_when_backend_is_sck(tmp_path, monkeypatch):
    config_path = tmp_path / "broadcaster.yaml"
    config_path.write_text(
        textwrap.dedent("""
            target_url: "https://localhost:8443/"
            capture_backend: sck
        """)
    )
    monkeypatch.setattr(
        "ndi_broadcaster.launcher.wait_for_healthy",
        lambda *args, **kwargs: pytest.fail("wait_for_healthy must not run when sck config is invalid"),
    )

    with pytest.raises(ValueError, match="sck_display_mode"):
        run(config_path=str(config_path))


def test_run_requires_sck_physical_display_name_when_mode_is_physical(tmp_path, monkeypatch):
    config_path = tmp_path / "broadcaster.yaml"
    config_path.write_text(
        textwrap.dedent("""
            target_url: "https://localhost:8443/"
            capture_backend: sck
            sck_display_mode: physical
        """)
    )
    monkeypatch.setattr(
        "ndi_broadcaster.launcher.wait_for_healthy",
        lambda *args, **kwargs: pytest.fail("wait_for_healthy must not run when sck config is invalid"),
    )

    with pytest.raises(ValueError, match="sck_physical_display_name"):
        run(config_path=str(config_path))


class _FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)


def test_sender_thread_loop_defaults_to_decode_captured_frame(monkeypatch):
    calls = []

    def fake_decode_captured_frame(data, target_width=None, target_height=None):
        calls.append((data, target_width, target_height))
        return np.zeros((1, 1, 4), dtype=np.uint8)

    monkeypatch.setattr("ndi_broadcaster.launcher.decode_captured_frame", fake_decode_captured_frame)

    frame_slot = _LatestFrameSlot()
    frame_slot.put(b"fake-image-bytes")
    sender = _FakeSender()
    config = BroadcasterConfig(width=10, height=20)
    stop_event = threading.Event()

    thread = threading.Thread(
        target=_sender_thread_loop, args=(frame_slot, sender, config, stop_event), daemon=True
    )
    thread.start()
    time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=2.0)

    assert calls == [(b"fake-image-bytes", 10, 20)]


def test_sender_thread_loop_uses_custom_decode_fn():
    frame_slot = _LatestFrameSlot()
    frame_slot.put(b"\x01\x02\x03\x04")
    sender = _FakeSender()
    config = BroadcasterConfig()
    stop_event = threading.Event()
    decoded = np.zeros((1, 1, 4), dtype=np.uint8)
    calls = []

    def fake_decode(data):
        calls.append(data)
        return decoded

    thread = threading.Thread(
        target=_sender_thread_loop,
        args=(frame_slot, sender, config, stop_event),
        kwargs={"decode_fn": fake_decode},
        daemon=True,
    )
    thread.start()
    time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=2.0)

    assert calls == [b"\x01\x02\x03\x04"]
    assert len(sender.sent) >= 1
    assert np.array_equal(sender.sent[0], decoded)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_launcher.py -v`
Expected: FAIL — `_validate_sck_display_mode` doesn't exist yet; the two
`test_run_requires_sck_*` tests get `NotImplementedError` instead of
`ValueError`; the `decode_fn`-related tests fail because
`_sender_thread_loop` doesn't accept that keyword yet.

- [ ] **Step 3: Add `decode_fn` to `_sender_thread_loop`**

In `ndi_broadcaster/launcher.py`, change the import line

```python
from collections.abc import Callable
```

(add this new import near the top, alongside the existing `from dataclasses
import dataclass` / `from pathlib import Path` block), and change the
function signature and its first decode step:

```python
def _sender_thread_loop(
    frame_slot: _LatestFrameSlot,
    sender: VideoSender,
    config: BroadcasterConfig,
    stop_event: threading.Event,
    decode_fn: Callable[[bytes], np.ndarray] | None = None,
) -> None:
    """Decode and send frames off the event loop, at a steady clock.

    The capture loop is throttled to config.fps and may occasionally fall behind (a
    slow encode, a retry after an error), so a static/delayed wall must not stop NDI
    output entirely. Re-sending the last decoded frame holds the configured frame
    rate regardless of capture-loop timing.

    decode_fn defaults to the cdp path's JPEG/PNG decode; the sck path passes a
    lightweight raw-BGRA-to-RGBA reshape instead (see _decode_raw_rgba_frame),
    since its frames already arrive as tightly packed RGBA bytes with no
    compression to undo.
    """
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
    while not stop_event.is_set():
        data = frame_slot.take()
        if data is not None:
            decode_start = time.monotonic()
            try:
                last_frame = decode(data)
            except Exception:
                logger.exception("Failed to decode a captured frame; skipping it")
            decodes_since_log += 1
            decode_seconds_since_log += time.monotonic() - decode_start
```

(The rest of the function body — the send step, the periodic log, the sleep
scheduling — is unchanged from its current implementation.)

- [ ] **Step 4: Add `_validate_sck_display_mode` and `_capture_loop_sck`**

Add these new functions to `ndi_broadcaster/launcher.py`, placed after
`_chrome_launch_args()` and before `_capture_loop`:

```python
SCK_WINDOW_TITLE = "Layout Driver Broadcaster"


def _validate_sck_display_mode(config: BroadcasterConfig) -> None:
    """Fail at startup rather than mid-capture on a missing sck field.

    Mirrors _validate_backend_selection's fail-fast convention in
    flux-gallery's worker.py: a missing required field for the selected mode
    is a config error, not something to guess at silently.
    """
    if config.capture_backend != "sck":
        return
    if config.sck_display_mode is None:
        raise ValueError(
            "capture_backend: sck requires sck_display_mode to be set to "
            "'virtual' or 'physical'"
        )
    if config.sck_display_mode == "physical" and not config.sck_physical_display_name:
        raise ValueError(
            "sck_display_mode: physical requires sck_physical_display_name to be set"
        )


def _decode_raw_rgba_frame(width: int, height: int) -> Callable[[bytes], np.ndarray]:
    def decode(data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4)

    return decode


async def _capture_loop_sck(
    config: BroadcasterConfig, sender: VideoSender, stop_event: threading.Event
) -> None:
    # Imported lazily: capture_sck/virtual_display/physical_display all
    # import PyObjC frameworks at module scope, which must not become a hard
    # requirement for anyone running only the cdp backend.
    from .capture_sck import SckCapture
    from .physical_display import find_physical_display
    from .virtual_display import ensure_helper_built, start_vdisplay_helper, wait_for_settled_bounds

    vdisplay_proc: subprocess.Popen | None = None
    if config.sck_display_mode == "virtual":
        helper_dir = REPO_ROOT / "ndi_broadcaster" / "vdisplay_helper"
        binary_path = ensure_helper_built(helper_dir)
        vdisplay_proc, info = start_vdisplay_helper(
            binary_path, config.width, config.height, config.sck_virtual_display_name
        )
        display = wait_for_settled_bounds(info.display_id, config.width, config.height)
    else:
        display = find_physical_display(config.sck_physical_display_name)

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=False,
                args=[
                    *_chrome_launch_args(),
                    f"--window-position={display.x},{display.y}",
                    f"--window-size={display.width},{display.height}",
                    "--ignore-certificate-errors",
                    "--disable-session-crashed-bubble",
                    "--disable-infobars",
                    "--noerrdialogs",
                    "--no-first-run",
                ],
            )
            context = await browser.new_context(
                # no_viewport=True (not viewport=None) is what actually disables
                # Playwright's forced 1280x720 default viewport, confirmed live
                # during the proof-of-concept spike.
                no_viewport=True,
                ignore_https_errors=True,
                permissions=["microphone"],
            )
            page = await context.new_page()
            await page.goto(config.target_url)
            # A framework-controlled, app-independent title: apps each set
            # their own <title>, so SCShareableContent window matching can't
            # rely on any single app's page title.
            await page.evaluate(f"document.title = {SCK_WINDOW_TITLE!r}")

            frame_slot = _LatestFrameSlot()
            sender_thread = threading.Thread(
                target=_sender_thread_loop,
                args=(frame_slot, sender, config, stop_event),
                kwargs={"decode_fn": _decode_raw_rgba_frame(config.width, config.height)},
                daemon=True,
            )
            sender_thread.start()

            capture = SckCapture(
                SCK_WINDOW_TITLE, config.width, config.height, config.fps, on_frame=frame_slot.put
            )
            capture.start()
            try:
                while not stop_event.is_set():
                    await asyncio.sleep(0.5)
            finally:
                capture.stop()
                stop_event.set()
                sender_thread.join(timeout=5.0)
                await browser.close()
    finally:
        if vdisplay_proc is not None:
            vdisplay_proc.terminate()
            try:
                vdisplay_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                vdisplay_proc.kill()
```

Add `import subprocess` to the top-level imports of `launcher.py` (alongside
the existing `import threading`, `import time`, etc.).

- [ ] **Step 5: Wire `run()` to branch on `capture_backend`**

Replace this block in `run()`:

```python
    config = load_broadcaster_config(Path(config_path))
    config = resolve_target_url(config, env)
    if config.capture_backend == "sck":
        raise NotImplementedError(
            "The 'sck' capture backend is not implemented yet; "
            "set capture_backend: cdp in config/broadcaster.yaml"
        )
```

with:

```python
    config = load_broadcaster_config(Path(config_path))
    config = resolve_target_url(config, env)
    _validate_sck_display_mode(config)
```

And replace this line further down:

```python
        stop_event = threading.Event()
        try:
            asyncio.run(_capture_loop(config, sender, stop_event))
        except KeyboardInterrupt:
            stop_event.set()
```

with:

```python
        stop_event = threading.Event()
        capture_loop = _capture_loop_sck if config.capture_backend == "sck" else _capture_loop
        try:
            asyncio.run(capture_loop(config, sender, stop_event))
        except KeyboardInterrupt:
            stop_event.set()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_launcher.py -v`
Expected: PASS (all tests, including the two rewritten `test_run_requires_*`
tests and the two new `_sender_thread_loop` tests).

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -v`
Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add ndi_broadcaster/launcher.py tests/test_launcher.py
git commit -m "ndi_broadcaster: wire capture_backend: sck into the launcher"
```

---

### Task 7: Live verification against both sample apps

This task has no unit-testable deliverable — per the Global Constraints, the
full `vdisplay_helper` → settle → Chromium-launch → `SCStream` → frame-slot
chain, and the sample apps themselves, are verified live, matching how every
other `ndi_broadcaster` capture-path change in this repo has been verified.
This task is performed directly (not dispatched to an implementer subagent):
it requires an interactive Screen Recording permission grant on first run
and judgment reading live fps/log output over a multi-minute window.

**Files:**
- Modify: `config/broadcaster.yaml` (temporarily, for local testing only —
  not committed with `capture_backend: sck` as the checked-in default,
  which must remain `cdp` per the Global Constraints)
- Modify: `README.md` (once results are in)

- [ ] **Step 1: Smoke-test the virtual display path standalone**

Set `capture_backend: sck` and `sck_display_mode: virtual` in a local,
uncommitted copy of `config/broadcaster.yaml` (or via a `BROADCASTER_YAML`
env override pointed at a scratch file, per `resolve_launcher_paths`).

Run the framework + broadcaster against `apps/test-pattern/static/` (the
simplest app, no worker needed):

```bash
./run.sh
```

Expected: a virtual display is created (visible via System Settings →
Displays, or `system_profiler SPDisplaysDataType`), a headed Chromium window
appears on it (not on the real desktop), and an NDI monitor (e.g. NDI Tools'
Studio Monitor) shows the test pattern. Grant Screen Recording permission to
the terminal/Python process if macOS prompts for it — this is a one-time,
interactive step that cannot be scripted.

- [ ] **Step 2: Run flux-gallery against `sck` for the duration that
      previously showed clear `cdp` degradation**

```bash
APP_DIR=$(pwd)/apps/flux-gallery/static ./run.sh
```

and in a second terminal:

```bash
GEMINI_API_KEY=... FLUX_BACKEND=fal FAL_KEY=... apps/flux-gallery/run.sh
```

Watch the broadcaster's `NDI capture` (or, on this path, `SCK capture`) fps
log lines for at least as long as the degradation previously took to appear
under `cdp` (documented in framework spec §3.4a as visible within a couple
of minutes). Expected: fps stays at or near the configured target
throughout, with no progressive slowdown.

- [ ] **Step 3: Run noraebang-generative against `sck` for a comparable
      duration**

```bash
APP_DIR=$(pwd)/apps/noraebang-generative/static ./run.sh
```

Expected: same — sustained fps with no degradation, continuously changing
content (p5.js animation) confirmed via the NDI monitor.

- [ ] **Step 4: Update documentation with the results**

If both runs confirm the absence of degradation, update `README.md`'s
flux-gallery section: change the "Known limitation" paragraph to note that
`capture_backend: sck` is now available and verified as a fix, while noting
`cdp` remains the default (a config change, not a default-behavior change,
is required to use it). Update framework spec §3.4a similarly if the spec
document is still being kept current for this investigation.

- [ ] **Step 5: Restore `config/broadcaster.yaml` to `capture_backend: cdp`**

Confirm `config/broadcaster.yaml` in git still has `capture_backend: "cdp"`
(Task 1 already set this) — any local scratch edits made during this task's
manual testing must not be committed.

- [ ] **Step 6: Commit the documentation update**

```bash
git add README.md
git commit -m "docs: verify sck capture backend resolves the NDI fps degradation"
```
