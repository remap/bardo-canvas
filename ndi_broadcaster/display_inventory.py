"""The only module here that touches AppKit/Quartz.

Kept separate from vdisplay_doctor so that module's decision logic stays
importable and unit-testable with no display server, and so PyObjC never
becomes a hard requirement for anyone running only the cdp backend -- the same
reason launcher.py defers its virtual_display import.
"""

from __future__ import annotations

import json
import subprocess

import AppKit
import Quartz

from .vdisplay_doctor import (
    NAME_SOURCE_NONE,
    NAME_SOURCE_NSSCREEN,
    NAME_SOURCE_SYSTEM_PROFILER,
    DisplayRecord,
)

_MAX_DISPLAYS = 32


def spin_run_loop(seconds: float = 0.3) -> None:
    """Let AppKit process pending display-reconfiguration notifications.

    NSScreen.screens() caches its list and refreshes only when the process's
    run loop handles that notification, and the sck design spec's section 10
    records the sharper version: NSScreen.screens() and CGGetActiveDisplayList
    return inconsistent, sometimes flatly wrong results when queried without
    first spinning a CFRunLoop, and NSApplication.sharedApplication() alone is
    not sufficient. Skipping this makes every verdict non-reproducible, which
    would defeat the entire point of this tool.
    """
    AppKit.NSApplication.sharedApplication()
    Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, seconds, False)


def _online_ids() -> list[int]:
    # Online, not Active: a zombie can be online while inactive, and would be
    # invisible to CGGetActiveDisplayList.
    _err, ids, count = Quartz.CGGetOnlineDisplayList(_MAX_DISPLAYS, None, None)
    return list(ids[:count])


def online_display_ids() -> set[int]:
    """Just the ID set, for the reaper's and prober's disappearance polling."""
    spin_run_loop()
    return set(_online_ids())


def recover_names_via_system_profiler(display_ids: list[int]) -> dict[int, str]:
    """Best-effort display names for displays NSScreen cannot see.

    Only worth its ~1-3s cost in the one case that needs it (see
    collect_displays). system_profiler does not report CGDirectDisplayIDs, so
    names are matched positionally against `display_ids` -- best-effort by
    construction, and used only to label a display that would otherwise be
    reported unnamed. Records built from these names carry
    name_source=NAME_SOURCE_SYSTEM_PROFILER precisely so that classify() can
    weight a guessed pairing correctly: enough to report a display as ours
    (pinned to the report-only zombie_b verdict), never enough to signal one
    (spec §4.5).

    Takes the caller's already-collected ID list rather than re-querying
    Quartz: this function reads no display state itself, so it has no
    run-loop-spin precondition, and it can't pair names against a different
    online-list snapshot than the one the caller is building records from
    (system_profiler's subprocess takes 1-3s, during which a second query
    could disagree with the first).
    """
    try:
        completed = subprocess.run(
            ["system_profiler", "-json", "SPDisplaysDataType"],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=True,
        )
        payload = json.loads(completed.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}

    names: list[str] = []
    for gpu in payload.get("SPDisplaysDataType", []):
        for entry in gpu.get("spdisplays_ndrvs", []):
            name = entry.get("_name")
            if name:
                names.append(name)
    return dict(zip(display_ids, names, strict=False))


def collect_displays() -> list[DisplayRecord]:
    """Snapshot every online display, with AppKit's name where available."""
    spin_run_loop()

    ids = _online_ids()
    nsscreen_names: dict[int, str] = {}
    for screen in AppKit.NSScreen.screens():
        number = screen.deviceDescription().get("NSScreenNumber")
        if number is not None:
            nsscreen_names[int(number)] = str(screen.localizedName())

    # An online display AppKit cannot see is precisely the zombie signature,
    # and precisely the case where the fast path has no name to report. Only
    # then pay for system_profiler; a healthy machine never does.
    fallback_names: dict[int, str] = {}
    if len(ids) > len(nsscreen_names):
        fallback_names = recover_names_via_system_profiler(ids)

    records: list[DisplayRecord] = []
    for display_id in ids:
        bounds = Quartz.CGDisplayBounds(display_id)
        # Recorded rather than inferred downstream: `name` alone cannot say
        # whether it was keyed by display ID (NSScreen) or guessed
        # positionally (system_profiler), and classify() must not treat the
        # second as evidence of ownership. See DisplayRecord's docstring.
        if display_id in nsscreen_names:
            name_source = NAME_SOURCE_NSSCREEN
        elif display_id in fallback_names:
            name_source = NAME_SOURCE_SYSTEM_PROFILER
        else:
            name_source = NAME_SOURCE_NONE
        records.append(
            DisplayRecord(
                display_id=int(display_id),
                vendor=int(Quartz.CGDisplayVendorNumber(display_id)),
                model=int(Quartz.CGDisplayModelNumber(display_id)),
                serial=int(Quartz.CGDisplaySerialNumber(display_id)),
                unit_number=int(Quartz.CGDisplayUnitNumber(display_id)),
                is_builtin=bool(Quartz.CGDisplayIsBuiltin(display_id)),
                is_active=bool(Quartz.CGDisplayIsActive(display_id)),
                is_asleep=bool(Quartz.CGDisplayIsAsleep(display_id)),
                bounds=(
                    int(bounds.origin.x),
                    int(bounds.origin.y),
                    int(bounds.size.width),
                    int(bounds.size.height),
                ),
                name=nsscreen_names.get(display_id, fallback_names.get(display_id)),
                in_nsscreen=display_id in nsscreen_names,
                name_source=name_source,
            )
        )
    return records
