from __future__ import annotations

import json
import queue
import subprocess
import threading
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
    # Both files are compile inputs (the header via -import-objc-header
    # below) -- checking only main.swift's mtime leaves a stale binary in
    # place if only the header changes.
    newest_input_mtime = max(source_path.stat().st_mtime, header_path.stat().st_mtime)
    if binary_path.exists() and binary_path.stat().st_mtime >= newest_input_mtime:
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
    binary_path: Path, width: int, height: int, name: str, timeout_s: float = 15.0
) -> tuple[subprocess.Popen, DisplayInfo]:
    """Launch the compiled vdisplay_helper and parse its one-line JSON startup report.

    The helper normally reports within a few seconds (settle + retry), but a
    WindowServer/permission stall could otherwise block this readline()
    indefinitely with no diagnostic -- every other bounded wait in this
    startup path (healthz, settle, window lookup, startCapture) already has
    an explicit timeout. Reading on a background thread (rather than e.g.
    select() on the pipe) keeps this testable against a plain fake stdout
    object, not just a real OS pipe.
    """
    proc = subprocess.Popen(
        [str(binary_path), str(width), str(height), name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    line_queue: queue.Queue[str] = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: line_queue.put(proc.stdout.readline()), daemon=True).start()
    try:
        line = line_queue.get(timeout=timeout_s)
    except queue.Empty:
        proc.kill()
        raise TimeoutError(
            f"vdisplay_helper did not report its startup status within {timeout_s}s"
        ) from None
    try:
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
    except BaseException:
        # Every path out of here leaves `proc` alive but unreturned, so no
        # caller-side finally can reach it -- launcher.py's own cleanup is
        # keyed on the tuple assignment that never happened. Killing here is
        # the only place the leak can be closed.
        proc.kill()
        raise
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

    deadline = time.monotonic() + timeout_s
    last_bounds = None
    stable_count = 0
    while time.monotonic() < deadline:
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
