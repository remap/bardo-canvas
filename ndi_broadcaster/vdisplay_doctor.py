"""Detect, reclaim, and pre-flight-check macOS virtual displays created by
vdisplay_helper.

Deliberately imports no PyObjC at module scope: everything that touches
AppKit/Quartz lives in display_inventory, imported lazily at the call site.
That keeps this module's decision logic importable (and unit-testable) with no
display server, and mirrors how launcher.py already defers its PyObjC imports
so the cdp-only path never hard-requires them.

See docs/superpowers/specs/2026-08-11-vdisplay-doctor-design.md.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

# macOS synthesizes its own 640x480 ghost display, confirmed by Apple DTS in
# developer.apple.com/forums/thread/787154. The numbers are legacy Classic-era
# four-character codes: "unkn" and "virt". It appears and disappears on its own
# (notably when a physical display powers off) and is NOT a leak of ours.
GHOST_VENDOR = 0x756E6B6E  # "unkn"
GHOST_MODEL = 0x76697274  # "virt"

# Substring-matched against a process's full argument vector, never against
# `ps -o comm=` -- see read_process_table().
HELPER_BINARY_NAME = "vdisplay_helper"
LAUNCHER_MODULE = "ndi_broadcaster.launcher"

# Distinct from config.sck_virtual_display_name on purpose: a probe display can
# then never be mistaken for a broadcast display in scan output, and a leaked
# probe is immediately attributable to the probe.
PROBE_DISPLAY_NAME = "Layout Driver Probe Display"

VERDICT_REAL = "real"
VERDICT_APPLE_GHOST = "apple_ghost"
VERDICT_ACTIVE = "active"
VERDICT_ORPHAN_A = "orphan_a"
VERDICT_ZOMBIE_B = "zombie_b"
VERDICT_FOREIGN_VIRTUAL = "foreign_virtual"

# The two verdicts that make a machine unfit to start a run.
ACTIONABLE_VERDICTS = frozenset({VERDICT_ORPHAN_A, VERDICT_ZOMBIE_B})


@dataclass(frozen=True)
class DisplayRecord:
    """One entry from CGGetOnlineDisplayList, plus the AppKit-side name.

    `name` is None when NSScreen did not list this display AND no name could
    be recovered via the system_profiler fallback (see display_inventory.py);
    it can therefore be populated even when `in_nsscreen` is False.
    `in_nsscreen`, not `name`, is the authoritative signal for whether AppKit
    could see the display -- the zombie signature is `in_nsscreen is False`.
    """

    display_id: int
    vendor: int
    model: int
    serial: int
    unit_number: int
    is_builtin: bool
    is_active: bool
    is_asleep: bool
    bounds: tuple[int, int, int, int]
    name: str | None
    in_nsscreen: bool


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    ppid: int
    command: str


@dataclass(frozen=True)
class Classification:
    display: DisplayRecord
    verdict: str
    owner_pid: int | None
    detail: str


def parse_ps_output(text: str) -> dict[int, ProcessRecord]:
    """Parse `ps -Awwo pid=,ppid=,command=` output into a pid-keyed table.

    Split with maxsplit=2 so the command keeps its own spaces; a full argument
    vector like `python -m ndi_broadcaster.launcher` is exactly what the
    `active` verdict matches against.
    """
    table: dict[int, ProcessRecord] = {}
    for line in text.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            continue
        pid_text, ppid_text, command = parts
        try:
            pid = int(pid_text)
            ppid = int(ppid_text)
        except ValueError:
            continue
        table[pid] = ProcessRecord(pid=pid, ppid=ppid, command=command)
    return table


def read_process_table() -> dict[int, ProcessRecord]:
    """Snapshot every process once.

    `command=` (the full argument vector), never `comm=`: verified that `comm=`
    reports only the executable path, so a launcher started as
    `python -m ndi_broadcaster.launcher` shows up as an anonymous
    .../MacOS/Python with no trace of the module name. Matching that would make
    the `active` verdict unmatchable and every live broadcast's helper would be
    misclassified as an orphan and reaped mid-show. `-ww` defeats ps's
    width-based truncation, which does not apply when stdout is a pipe but does
    when run interactively.

    One call rather than one per candidate display, so a scan stays fast
    regardless of how many processes are running.
    """
    completed = subprocess.run(
        ["ps", "-Awwo", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    return parse_ps_output(completed.stdout)


def _attributed_owner(serial: int, processes: dict[int, ProcessRecord]) -> ProcessRecord | None:
    """Map a display's serial back to the vdisplay_helper that created it.

    main.swift sets descriptor.serialNum to its own PID, so CGDisplaySerialNumber
    returns the creating helper's PID. PIDs are recycled, though, so the mapping
    is trusted only when that PID is live AND is actually a vdisplay_helper --
    otherwise a display created by a long-dead helper would attribute to
    whatever unrelated process inherited its PID, and reap would signal that
    process. Failing to reclaim a display is recoverable; SIGTERMing a stranger
    is not.
    """
    proc = processes.get(serial)
    if proc is None:
        return None
    if HELPER_BINARY_NAME not in proc.command:
        return None
    return proc


def classify(
    displays: list[DisplayRecord],
    processes: dict[int, ProcessRecord],
    config_display_name: str,
) -> list[Classification]:
    """Assign one verdict per display. Pure: no I/O, no PyObjC, no subprocesses.

    Rows are evaluated in the order documented in the spec's 4.2, first match
    wins. The ordering is load-bearing: is_builtin must be tested before the
    ghost test, because this machine's built-in display reports
    unit_number == 0 and would otherwise be called a ghost.
    """
    our_names = {config_display_name, PROBE_DISPLAY_NAME}
    results: list[Classification] = []

    for display in displays:
        if display.is_builtin:
            results.append(Classification(display, VERDICT_REAL, None, "built-in display"))
            continue

        if (
            display.unit_number == 0
            and display.vendor == GHOST_VENDOR
            and display.model == GHOST_MODEL
            and display.serial == 0
        ):
            results.append(
                Classification(display, VERDICT_APPLE_GHOST, None, "macOS synthetic ghost display")
            )
            continue

        owner = _attributed_owner(display.serial, processes)

        # A known name is authoritative: it is what system_profiler and NSScreen
        # both report, and it cannot be spoofed by a serial/PID collision. Only
        # fall back to serial attribution when the name is unavailable, which is
        # itself the zombie signature (online but invisible to AppKit).
        if display.name is not None:
            is_ours = display.name in our_names
        else:
            is_ours = owner is not None

        if not is_ours:
            if not display.in_nsscreen:
                results.append(
                    Classification(
                        display,
                        VERDICT_FOREIGN_VIRTUAL,
                        None,
                        "not ours and invisible to NSScreen -- another tool's display",
                    )
                )
            else:
                results.append(Classification(display, VERDICT_REAL, None, "physical display"))
            continue

        if owner is None:
            results.append(
                Classification(
                    display,
                    VERDICT_ZOMBIE_B,
                    None,
                    f"owner pid {display.serial} is not a live {HELPER_BINARY_NAME}; "
                    "no API exists to remove it",
                )
            )
            continue

        parent = processes.get(owner.ppid)
        if parent is not None and LAUNCHER_MODULE in parent.command:
            results.append(
                Classification(
                    display,
                    VERDICT_ACTIVE,
                    owner.pid,
                    f"helper {owner.pid} is serving launcher {parent.pid}",
                )
            )
        else:
            results.append(
                Classification(
                    display,
                    VERDICT_ORPHAN_A,
                    owner.pid,
                    f"helper {owner.pid} reparented to ppid {owner.ppid}; its launcher is gone",
                )
            )

    return results


REAPED_SIGTERM = "reaped_sigterm"
REAPED_SIGKILL = "reaped_sigkill"
UNRECLAIMABLE = "unreclaimable"


@dataclass(frozen=True)
class ReapResult:
    display_id: int
    owner_pid: int
    outcome: str
    elapsed_s: float


def signal_process(pid: int, sig: int) -> None:
    """Send one signal, treating an already-exited target as a no-op."""
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def reap_orphans(
    classifications: list[Classification],
    *,
    signal_process: Callable[[int, int], None],
    list_display_ids: Callable[[], set[int]],
    verify_timeout_s: float = 5.0,
    poll_interval_s: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[ReapResult]:
    """SIGTERM, then SIGKILL, each orphan_a owner -- verifying after each that
    the DISPLAY is gone, not merely that the process exited.

    Those are different claims. main.swift's pre-fix bug was precisely a helper
    that exited cleanly while leaking its display in WindowServer, so checking
    the process would check nothing. Only orphan_a is ever signalled: `active`
    displays are serving a live broadcast, `foreign_virtual` displays belong to
    another tool, and `zombie_b` has no owner left to signal.
    """
    results: list[ReapResult] = []

    for item in classifications:
        if item.verdict != VERDICT_ORPHAN_A or item.owner_pid is None:
            continue

        display_id = item.display.display_id
        owner_pid = item.owner_pid
        started = monotonic()
        outcome = UNRECLAIMABLE

        for sig, success in (
            (signal.SIGTERM, REAPED_SIGTERM),
            (signal.SIGKILL, REAPED_SIGKILL),
        ):
            try:
                signal_process(owner_pid, sig)
            except ProcessLookupError:
                pass

            deadline = monotonic() + verify_timeout_s
            while monotonic() < deadline:
                if display_id not in list_display_ids():
                    outcome = success
                    break
                sleep(poll_interval_s)
            if outcome != UNRECLAIMABLE:
                break

        results.append(
            ReapResult(
                display_id=display_id,
                owner_pid=owner_pid,
                outcome=outcome,
                elapsed_s=monotonic() - started,
            )
        )

    return results
