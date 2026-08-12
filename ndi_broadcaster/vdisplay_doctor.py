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

import argparse
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import BroadcasterConfig, load_broadcaster_config

# macOS synthesizes its own 640x480 ghost display, confirmed by Apple DTS in
# developer.apple.com/forums/thread/787154. The numbers are legacy Classic-era
# four-character codes: "unkn" and "virt". It appears and disappears on its own
# (notably when a physical display powers off) and is NOT a leak of ours.
GHOST_VENDOR = 0x756E6B6E  # "unkn"
GHOST_MODEL = 0x76697274  # "virt"

# Matched against the basename of argv[0] only -- never as a substring of the
# whole argument vector, and never against `ps -o comm=` (see
# read_process_table() for why the full vector is collected).
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


# Where a DisplayRecord's `name` came from. The distinction is load-bearing in
# classify(): only NAME_SOURCE_NSSCREEN is evidence strong enough to decide
# ours/not-ours (§4.5).
NAME_SOURCE_NSSCREEN = "nsscreen"
NAME_SOURCE_SYSTEM_PROFILER = "system_profiler"
NAME_SOURCE_NONE = "none"


@dataclass(frozen=True)
class DisplayRecord:
    """One entry from CGGetOnlineDisplayList, plus the AppKit-side name.

    `name` is None when NSScreen did not list this display AND no name could
    be recovered via the system_profiler fallback (see display_inventory.py);
    it can therefore be populated even when `in_nsscreen` is False.
    `in_nsscreen`, not `name`, is the authoritative signal for whether AppKit
    could see the display -- the zombie signature is `in_nsscreen is False`.

    `name_source` records which of the two produced `name`, because they carry
    very different weight. An NSScreen name is keyed by NSScreenNumber, i.e.
    by CGDirectDisplayID, so it is definitionally the right display's name. A
    system_profiler name is paired *positionally* against the online-ID list
    (system_profiler emits no CGDirectDisplayID at all), across two orderings
    nothing guarantees agree -- and it is produced only in the zombie case
    this tool exists for. classify() therefore treats it as a label to print,
    never as evidence of whose display this is (§4.5).
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
    name_source: str


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


def _is_helper_command(command: str) -> bool:
    """True only when argv[0]'s basename IS the helper binary.

    Deliberately not a substring test over the whole argument vector: that
    would be satisfied by `swiftc -O -o .../vdisplay_helper main.swift` -- a
    command this repo's own ensure_helper_built() spawns -- as well as by any
    editor or grep that happens to mention the path while working in this
    tree. Since a nameless display attributes purely on serial, one such
    live PID colliding with a dead helper's recycled PID would promote a
    zombie_b to orphan_a and get that unrelated process SIGTERMed (§4.3).
    """
    parts = command.split(maxsplit=1)
    if not parts:
        return False
    return Path(parts[0]).name == HELPER_BINARY_NAME


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
    if not _is_helper_command(proc.command):
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

        # Trust in a name is ASYMMETRIC, because the two harms are asymmetric
        # (§4.5). An NSScreen name is authoritative: NSScreen keys it by
        # NSScreenNumber (the CGDirectDisplayID), so it is certainly this
        # display's own name, and it cannot be spoofed by a serial/PID
        # collision -- every verdict stays reachable from it.
        #
        # A system_profiler name is a positional guess about *which* display it
        # belongs to. It is still enough to REPORT a display as ours, but never
        # enough to SIGNAL one:
        #
        #   INVARIANT: no signal is ever sent on the strength of a
        #   system_profiler name.
        #
        # So such a display is pinned to zombie_b below, short of orphan_a and
        # active, which are the two verdicts that lead to a signal. zombie_b is
        # safe to reach on weak evidence precisely because reap_orphans skips
        # it: the cost of a false positive is a false FAIL on a pre-flight
        # gate, which fails in the safe direction -- it stops a run rather than
        # permitting a doomed one. Refusing to report on this name instead
        # would silently miss the flagship case this tool exists for, since a
        # SIGKILLed helper's display is invisible to NSScreen and the
        # system_profiler fallback is the ONLY thing that ever names it.
        name_is_ours = display.name is not None and display.name in our_names
        report_only = name_is_ours and display.name_source == NAME_SOURCE_SYSTEM_PROFILER

        if display.name is not None and display.name_source == NAME_SOURCE_NSSCREEN:
            is_ours = name_is_ours
        else:
            is_ours = report_only or owner is not None

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

        if report_only:
            # Deliberately ahead of the owner check: even a live, §4.3-guarded
            # helper does not promote this display, because the only reason it
            # is ours at all is a positionally-guessed name. Reported, never
            # signalled. The detail says so, since this is a materially weaker
            # claim than the serial-attributed zombie below and an operator
            # deciding whether to reboot deserves to know which one they have.
            results.append(
                Classification(
                    display,
                    VERDICT_ZOMBIE_B,
                    None,
                    f"named {display.name!r} by system_profiler's positional fallback, not by "
                    "NSScreen -- ours by a guessed name only, so never signalled; "
                    "no API exists to remove it",
                )
            )
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


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    timings: dict[str, float]
    failure_phase: str | None
    message: str


def probe(
    width: int,
    height: int,
    *,
    helper_dir: Path,
    teardown_timeout_s: float = 5.0,
    poll_interval_s: float = 0.25,
) -> ProbeResult:
    """Create, settle, and tear down one throwaway virtual display.

    Answers the question that actually protects a run -- can this machine still
    configure a virtual display? -- rather than the weaker "does the display
    list look clean", since a dirty list may be unfixable (there is no
    remove-by-ID call anywhere in the private CGVirtualDisplay API).

    Calls the production startup path unmodified, so it doubles as a regression
    test for main.swift's shutdownAndExit() ARC teardown. Every wait is
    bounded: 15s and 20s come from start_vdisplay_helper and
    wait_for_settled_bounds, plus teardown_timeout_s here. Per-phase timings
    are reported individually so a *slowing*
    CGCompleteDisplayConfiguration is visible before it becomes a hanging one.
    """
    # Imported inside the function, so monkeypatching these module attributes in
    # tests takes effect (the import re-reads them on every call) and so a
    # PyObjC-free environment can still import this module. online_display_ids
    # is imported later still, just before the teardown poll, so a failure in
    # build/create/settle never needs PyObjC at all.
    from .virtual_display import (
        _terminate_helper,
        ensure_helper_built,
        start_vdisplay_helper,
        wait_for_settled_bounds,
    )

    timings: dict[str, float] = {}
    proc = None
    display_id: int | None = None

    def _fail(phase: str, message: str) -> ProbeResult:
        return ProbeResult(ok=False, timings=timings, failure_phase=phase, message=message)

    try:
        started = time.monotonic()
        try:
            binary_path = ensure_helper_built(helper_dir)
        except (OSError, subprocess.SubprocessError) as exc:
            return _fail("build", f"could not build vdisplay_helper: {exc}")
        timings["build"] = time.monotonic() - started

        started = time.monotonic()
        try:
            proc, info = start_vdisplay_helper(binary_path, width, height, PROBE_DISPLAY_NAME)
        except (TimeoutError, RuntimeError, ValueError, KeyError, OSError) as exc:
            return _fail("create", str(exc))
        display_id = info.display_id
        timings["create"] = time.monotonic() - started

        started = time.monotonic()
        try:
            wait_for_settled_bounds(display_id, width, height)
        except TimeoutError as exc:
            return _fail("settle", str(exc))
        timings["settle"] = time.monotonic() - started

        from .display_inventory import online_display_ids

        started = time.monotonic()
        _terminate_helper(proc, teardown_timeout_s)
        deadline = time.monotonic() + teardown_timeout_s
        try:
            while time.monotonic() < deadline:
                if display_id not in online_display_ids():
                    timings["teardown"] = time.monotonic() - started
                    proc = None
                    return ProbeResult(
                        ok=True,
                        timings=timings,
                        failure_phase=None,
                        message="create/settle/teardown all succeeded",
                    )
                time.sleep(poll_interval_s)
        except Exception as exc:  # noqa: BLE001 -- deliberately blind: online_display_ids()
            # calls into a private, undocumented CoreGraphics/Quartz API, so its
            # failure mode has no narrower type to name here. A transient error
            # must still come back as a ProbeResult, not an unhandled traceback --
            # Task 6's CLI depends on probe() always returning rather than raising.
            timings["teardown"] = time.monotonic() - started
            return _fail("teardown", f"error while polling for display teardown: {exc}")
        timings["teardown"] = time.monotonic() - started
        return _fail(
            "teardown",
            f"display {display_id} still online {teardown_timeout_s}s after the helper "
            "was terminated -- shutdownAndExit()'s ARC release may have regressed",
        )
    finally:
        # Never leave a probe display behind, whichever phase failed -- and
        # SIGTERM, not SIGKILL, because only the handler tears the display
        # down (§2.1). A failed `settle` is the case that matters: `create`
        # already succeeded and returned a real displayID, so the display is
        # definitely live, and SIGKILLing here would leave the health gate
        # manufacturing exactly the zombie_b it exists to detect -- which
        # probe's own "refuses a poisoned machine" guard would then read as a
        # reason to refuse the next run.
        if proc is not None:
            _terminate_helper(proc, teardown_timeout_s)


EXIT_CLEAN = 0
EXIT_DIRTY = 1
EXIT_PROBE_FAILED = 2
EXIT_ERROR = 3

REPO_ROOT = Path(__file__).resolve().parent.parent

LADDER = """
rung 1  SIGTERM owner  -> verify display gone   [auto, attempted]
rung 2  SIGKILL owner  -> verify display gone   [auto, attempted]
------------------------------- stop -------------------------------
rung 3  wait it out (minutes..overnight)        [advise]
rung 4  log out / log back in                   [advise]
rung 5  reboot                                  [advise]

No API exists to remove a virtual display you do not own: the private
CGVirtualDisplay surface has no remove-by-ID call, and teardown happens only
via ARC deinit inside the owning process. Restarting WindowServer, colorsyncd,
or DisplaysExt were all tried live and none cleared this state.
""".strip()


def broadcaster_yaml_path(env: dict[str, str]) -> Path:
    """Resolve BROADCASTER_YAML the same way launcher.resolve_launcher_paths does.

    Deliberately re-derived rather than imported: launcher.py imports
    playwright, numpy, httpx and cyndilib at module scope, which is far too
    heavy for a tool whose whole point is a sub-second answer. The duplication
    is one env var with one default, and a test asserts the two agree so they
    cannot drift.
    """
    return Path(env.get("BROADCASTER_YAML", str(REPO_ROOT / "config" / "broadcaster.yaml")))


def _resolve_display_name(explicit: str | None, config: BroadcasterConfig | None) -> str:
    """--name wins, so scan/reap work with no config file present at all.

    Takes an already-loaded config (or None) rather than loading its own, so
    main() can guard the config load exactly once and report a single,
    correct exit code no matter which command needed it. A failed load is
    only fatal here when the name is actually needed and unavailable --
    --name alone must still be enough to run scan/reap with no config file
    present at all.
    """
    if explicit is not None:
        return explicit
    if config is None:
        raise ValueError("no --name given and broadcaster.yaml could not be loaded")
    return config.sck_virtual_display_name


# Indirected through module-level functions so tests can monkeypatch them
# without importing PyObjC.
def _collect_displays() -> list[DisplayRecord]:
    from .display_inventory import collect_displays

    return collect_displays()


def _online_display_ids() -> set[int]:
    from .display_inventory import online_display_ids

    return online_display_ids()


def format_table(classifications: list[Classification]) -> str:
    header = f"{'display':<10} {'verdict':<16} {'owner':<7} name"
    lines = [header]
    for item in classifications:
        owner = "-" if item.owner_pid is None else str(item.owner_pid)
        name = item.display.name or "(unnamed)"
        lines.append(f"{item.display.display_id:<10} {item.verdict:<16} {owner:<7} {name}")
    return "\n".join(lines)


def _summarise(classifications: list[Classification]) -> str:
    counts: dict[str, int] = {}
    for item in classifications:
        counts[item.verdict] = counts.get(item.verdict, 0) + 1
    orphans = counts.get(VERDICT_ORPHAN_A, 0)
    zombies = counts.get(VERDICT_ZOMBIE_B, 0)
    if orphans or zombies:
        return f"FAIL  {orphans} orphaned (reclaimable), {zombies} zombie (unreclaimable)"
    return (
        f"OK  {counts.get(VERDICT_REAL, 0)} real, "
        f"{counts.get(VERDICT_ACTIVE, 0)} active, 0 orphaned"
    )


def _scan(display_name: str) -> tuple[list[Classification], int]:
    classifications = classify(_collect_displays(), read_process_table(), display_name)
    print(format_table(classifications))
    for item in classifications:
        if item.verdict == VERDICT_FOREIGN_VIRTUAL:
            print(f"WARN  display {item.display.display_id}: {item.detail}")
        if item.display.is_asleep:
            # Section 10 of the sck spec records display sleep correlating with
            # CGVirtualDisplay creation becoming unreliable.
            print(
                f"WARN  display {item.display.display_id} is asleep; "
                "consider `caffeinate -d` for the duration of a broadcast"
            )
    print(_summarise(classifications))
    dirty = any(item.verdict in ACTIONABLE_VERDICTS for item in classifications)
    return classifications, EXIT_DIRTY if dirty else EXIT_CLEAN


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ndi_broadcaster.vdisplay_doctor",
        description="Detect, reclaim, and pre-flight-check virtual displays.",
    )
    parser.add_argument("command", choices=["scan", "reap", "probe"])
    parser.add_argument(
        "--name",
        default=None,
        help="virtual display name to treat as ours (default: from broadcaster.yaml)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="let `probe` run even when orphans or zombies are already present",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on a usage error, which is this tool's documented
        # code for "probe failed -- the machine is not fit to start a run"
        # (§5.4). A mistyped flag must not raise that alarm, so remap it to
        # EXIT_ERROR. --help's exit 0 is left exactly as argparse produced it.
        if exc.code in (0, None):
            raise
        return EXIT_ERROR

    try:
        return _dispatch(args)
    except Exception as exc:  # noqa: BLE001 -- see below
        # Every I/O source under _dispatch can fail in a way argparse never
        # sees: read_process_table() runs `ps` with check=True, _collect_displays()
        # imports PyObjC (absent on exactly the machines the lazy-import
        # architecture exists to accommodate) and then calls into Quartz.
        # Uncaught, each of those exits 1 -- indistinguishable from EXIT_DIRTY,
        # so a caller scripting `scan || reap` would read "orphans present"
        # when the truth is "the tool broke". Exception, not BaseException:
        # Ctrl-C must still interrupt rather than be reported as an internal
        # error.
        print(f"ERROR  {type(exc).__name__}: {exc}")
        return EXIT_ERROR


def _dispatch(args: argparse.Namespace) -> int:
    # Loaded exactly once, up front, and guarded here -- not at each of the two
    # places (name resolution, probe's width/height) that would otherwise load
    # it separately. That closes the exit-code collision an unguarded second
    # load caused: any of OSError (missing file), yaml.YAMLError (malformed
    # YAML), or pydantic's ValidationError (a ValueError subclass) must become
    # EXIT_ERROR, never an uncaught traceback that exits 1 and is
    # indistinguishable from EXIT_DIRTY.
    config: BroadcasterConfig | None = None
    config_error: Exception | None = None
    try:
        config = load_broadcaster_config(broadcaster_yaml_path(dict(os.environ)))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        config_error = exc

    try:
        display_name = _resolve_display_name(args.name, config)
    except ValueError:
        print(f"ERROR  could not resolve the virtual display name: {config_error}")
        return EXIT_ERROR

    classifications, scan_code = _scan(display_name)

    if args.command == "scan":
        return scan_code

    if args.command == "reap":
        results = reap_orphans(
            classifications,
            signal_process=signal_process,
            list_display_ids=_online_display_ids,
        )
        for result in results:
            print(
                f"{result.outcome}  display {result.display_id} "
                f"(owner {result.owner_pid}) after {result.elapsed_s:.1f}s"
            )
        remaining = [item for item in classifications if item.verdict == VERDICT_ZOMBIE_B] + [
            r for r in results if r.outcome == UNRECLAIMABLE
        ]
        if remaining:
            print(LADDER)
            return EXIT_DIRTY
        return EXIT_CLEAN

    if scan_code == EXIT_DIRTY and not args.force:
        print(
            "ERROR  refusing to probe: orphans or zombies are already present, so a "
            "probe would report nothing trustworthy and risks adding to the "
            "accumulation that causes the startup hang. Re-run with --force to override."
        )
        return EXIT_DIRTY

    if config is None:
        print(
            "ERROR  could not load broadcaster.yaml (needed for probe's width/height): "
            f"{config_error}"
        )
        return EXIT_ERROR

    result = probe(
        config.width,
        config.height,
        helper_dir=REPO_ROOT / "ndi_broadcaster" / "vdisplay_helper",
    )
    for phase, seconds in result.timings.items():
        print(f"{phase:<9} {seconds:.1f}s")
    print(("OK  " if result.ok else f"FAIL [{result.failure_phase}]  ") + result.message)
    return EXIT_CLEAN if result.ok else EXIT_PROBE_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
