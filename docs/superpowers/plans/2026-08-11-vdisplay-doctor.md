# vdisplay_doctor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone `scan` / `reap` / `probe` CLI that reports zombie virtual displays, reclaims the reclaimable ones, and proves the machine can still create and tear down a virtual display before a broadcast starts.

**Architecture:** All decision logic lives in one pure function, `classify()`, over plain dataclasses — no display server, no subprocesses — so every verdict is unit-testable hermetically. PyObjC-dependent collection is isolated in a separate module (`display_inventory.py`) that the doctor imports lazily, matching the existing pattern where `launcher.py` lazily imports `virtual_display.py` so the `cdp`-only path never hard-requires PyObjC. The reaper and prober take injected signal/list callables, so their retry-and-verify loops are testable without ever creating a real display.

**Tech Stack:** Python 3.13, pyobjc (Quartz + Cocoa), pytest, `ps(1)`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-11-vdisplay-doctor-design.md`. Section references below (§2.1, §4.3, …) point into it.

## Global Constraints

- **Platform:** macOS only. PyObjC imports must never happen at `vdisplay_doctor` module scope (§3) — only inside `display_inventory`, which is imported lazily.
- **`ps` invocation:** exactly `ps -Awwo pid=,ppid=,command=`. Never `comm=` — it yields only the executable path, which makes the `active` verdict unmatchable and would reap live broadcasts (§4.1).
- **Ghost test:** the full conjunction `unit_number == 0 and vendor == 0x756E6B6E and model == 0x76697274 and serial == 0`, with `is_builtin` checked *first*. A bare `unit != 0` filter misclassifies this machine's built-in display (§2.3).
- **Never signal:** any PID that is not live with `vdisplay_helper` in its command (§4.3); any display classified `active` or `foreign_virtual` (§4.2).
- **Probe display name:** exactly `"Layout Driver Probe Display"` — never `config.sck_virtual_display_name` (§5.3).
- **Escalation ceiling:** SIGTERM then SIGKILL. Nothing above that is automated (§2.4).
- **Exit codes:** 0 clean, 1 orphan/zombie present, 2 probe failed, 3 internal error (§5.4).
- **Line length:** 100 (`[tool.ruff]` in `pyproject.toml`). Run `uv run ruff check .` and `uv run ruff format .` before each commit.
- **Test style:** module-level `def test_*` functions with `monkeypatch`, matching `tests/test_virtual_display.py`. No test classes.

## File Structure

| File | Responsibility |
|---|---|
| Create `ndi_broadcaster/vdisplay_doctor.py` | Dataclasses, verdict constants, `parse_ps_output`, `read_process_table`, `classify`, `reap_orphans`, `probe`, table formatting, `main` CLI. No module-scope PyObjC. |
| Create `ndi_broadcaster/display_inventory.py` | The only PyObjC-touching module: `collect_displays()`, `online_display_ids()`, `recover_names_via_system_profiler()`. Module-scope `AppKit`/`Quartz` imports, mirroring `virtual_display.py`. |
| Create `tests/test_vdisplay_doctor.py` | Hermetic: ps parsing, all `classify` rows, both traps, reaper loops, exit codes, config-path agreement. |
| Create `tests/test_display_inventory.py` | `@pytest.mark.live`: real display enumeration sanity. |
| Create `tests/test_vdisplay_doctor_live.py` | `@pytest.mark.live`: full real probe cycle. |
| Modify `pyproject.toml:47-49` | Register the `live` marker; deselect it by default via `addopts`. |

`run.sh`, `main.swift`, `virtual_display.py`, `launcher.py`, and `config.py` are **not** modified (§3).

---

### Task 1: Data types and the process table

**Files:**
- Create: `ndi_broadcaster/vdisplay_doctor.py`
- Test: `tests/test_vdisplay_doctor.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `DisplayRecord`, `ProcessRecord`, `Classification` dataclasses; the six `VERDICT_*` string constants; `GHOST_VENDOR`, `GHOST_MODEL`, `HELPER_BINARY_NAME`, `LAUNCHER_MODULE`, `PROBE_DISPLAY_NAME`; `parse_ps_output(text: str) -> dict[int, ProcessRecord]`; `read_process_table() -> dict[int, ProcessRecord]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_vdisplay_doctor.py`:

```python
from ndi_broadcaster.vdisplay_doctor import (
    ProcessRecord,
    parse_ps_output,
)


def test_parse_ps_output_reads_pid_ppid_and_full_command():
    # Real `ps -Awwo pid=,ppid=,command=` output: right-aligned numeric
    # columns, then the full argument vector, which itself contains spaces.
    text = (
        "51006     1 uv run python -m ndi_broadcaster.launcher\n"
        " 4903     1 /repo/ndi_broadcaster/vdisplay_helper/vdisplay_helper 3840 2160 Some Name\n"
    )

    table = parse_ps_output(text)

    assert table[51006] == ProcessRecord(
        pid=51006, ppid=1, command="uv run python -m ndi_broadcaster.launcher"
    )
    assert table[4903].ppid == 1
    assert table[4903].command.endswith("vdisplay_helper 3840 2160 Some Name")


def test_parse_ps_output_skips_blank_and_malformed_lines():
    text = "\n   \n123 456\n789 1 real-command\n"

    table = parse_ps_output(text)

    assert list(table) == [789]


def test_parse_ps_output_keeps_commands_containing_many_spaces():
    text = "  42   1 python -c import time; time.sleep(6) # pad pad pad\n"

    table = parse_ps_output(text)

    assert table[42].command == "python -c import time; time.sleep(6) # pad pad pad"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_vdisplay_doctor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ndi_broadcaster.vdisplay_doctor'`

- [ ] **Step 3: Write the implementation**

Create `ndi_broadcaster/vdisplay_doctor.py`:

```python
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

import subprocess
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

    `name` is None when NSScreen did not list this display, which is itself
    diagnostic: an online display AppKit cannot see is the zombie signature.
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_vdisplay_doctor.py -v`
Expected: 3 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff format ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py
uv run ruff check ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py
git add ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py
git commit -m "vdisplay_doctor: add display/process records and ps table parsing"
```

---

### Task 2: The `classify()` decision function

**Files:**
- Modify: `ndi_broadcaster/vdisplay_doctor.py`
- Test: `tests/test_vdisplay_doctor.py`

**Interfaces:**
- Consumes: Task 1's `DisplayRecord`, `ProcessRecord`, `Classification`, verdict constants.
- Produces: `classify(displays: list[DisplayRecord], processes: dict[int, ProcessRecord], config_display_name: str) -> list[Classification]`.

This is the heart of the tool. Every row of spec §4.2 and both empirical traps from §2.3 get a named test.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vdisplay_doctor.py`:

```python
import pytest

from ndi_broadcaster.vdisplay_doctor import (
    GHOST_MODEL,
    GHOST_VENDOR,
    PROBE_DISPLAY_NAME,
    VERDICT_ACTIVE,
    VERDICT_APPLE_GHOST,
    VERDICT_FOREIGN_VIRTUAL,
    VERDICT_ORPHAN_A,
    VERDICT_REAL,
    VERDICT_ZOMBIE_B,
    DisplayRecord,
    classify,
)

OURS = "Layout Driver Virtual Display"
HELPER_CMD = "/repo/ndi_broadcaster/vdisplay_helper/vdisplay_helper 3840 2160 " + OURS
LAUNCHER_CMD = "/opt/homebrew/.../MacOS/Python -m ndi_broadcaster.launcher"


def _display(**overrides) -> DisplayRecord:
    """A plausible non-builtin, NSScreen-visible display; override per test."""
    base = dict(
        display_id=69732865,
        vendor=0x1234,
        model=0x5678,
        serial=4903,
        unit_number=3,
        is_builtin=False,
        is_active=True,
        is_asleep=False,
        bounds=(6116, 0, 3840, 2160),
        name=OURS,
        in_nsscreen=True,
    )
    base.update(overrides)
    return DisplayRecord(**base)


def _verdicts(displays, processes, name=OURS):
    return [c.verdict for c in classify(displays, processes, name)]


def test_builtin_display_with_unit_number_zero_is_real_not_a_ghost():
    # THE TRAP (spec 2.3): the Apple DTS forum thread recommends filtering on
    # CGDisplayUnitNumber != 0, but this machine's built-in display genuinely
    # reports unit_number == 0. These are its real measured values. A bare
    # `unit != 0` filter calls the laptop's own screen a ghost.
    builtin = _display(
        display_id=1,
        vendor=1552,
        model=41055,
        serial=4251086178,
        unit_number=0,
        is_builtin=True,
        name="Built-in Retina Display",
    )

    assert _verdicts([builtin], {}) == [VERDICT_REAL]


def test_apple_synthetic_ghost_is_ignored():
    ghost = _display(
        display_id=7,
        vendor=GHOST_VENDOR,
        model=GHOST_MODEL,
        serial=0,
        unit_number=0,
        bounds=(0, 0, 640, 480),
        name=None,
        in_nsscreen=False,
    )

    assert _verdicts([ghost], {}) == [VERDICT_APPLE_GHOST]


@pytest.mark.parametrize(
    "field,value",
    [("vendor", 0x1234), ("model", 0x5678), ("serial", 99)],
)
def test_ghost_test_requires_the_full_conjunction(field, value):
    # unit_number == 0 alone must not be enough: a non-builtin display with a
    # real vendor/model/serial is not Apple's ghost.
    nearly = _display(
        unit_number=0,
        vendor=GHOST_VENDOR,
        model=GHOST_MODEL,
        serial=0,
        name=None,
        in_nsscreen=False,
        **{field: value},
    )

    assert _verdicts([nearly], {}) != [VERDICT_APPLE_GHOST]


def test_helper_parented_to_a_live_launcher_is_active_and_never_touched():
    processes = {
        4903: ProcessRecord(pid=4903, ppid=4821, command=HELPER_CMD),
        4821: ProcessRecord(pid=4821, ppid=4800, command=LAUNCHER_CMD),
    }

    [result] = classify([_display(serial=4903)], processes, OURS)

    assert result.verdict == VERDICT_ACTIVE
    assert result.owner_pid == 4903


def test_helper_reparented_to_launchd_is_a_reclaimable_orphan():
    # The bugs.md leak: the launcher was SIGKILLed, so its finally block never
    # ran vdisplay_proc.terminate() and the helper outlived it.
    processes = {4903: ProcessRecord(pid=4903, ppid=1, command=HELPER_CMD)}

    [result] = classify([_display(serial=4903)], processes, OURS)

    assert result.verdict == VERDICT_ORPHAN_A
    assert result.owner_pid == 4903


def test_display_whose_owner_is_gone_is_an_unreclaimable_zombie():
    [result] = classify([_display(serial=4110)], {}, OURS)

    assert result.verdict == VERDICT_ZOMBIE_B
    assert result.owner_pid is None


def test_pid_reuse_guard_refuses_to_attribute_a_recycled_pid():
    # spec 4.3: serial 4903 matches a LIVE pid, but that pid is now some
    # unrelated process. Attributing it would make reap SIGTERM that process.
    processes = {4903: ProcessRecord(pid=4903, ppid=1, command="/usr/bin/ssh-agent -l")}

    [result] = classify([_display(serial=4903)], processes, OURS)

    assert result.verdict == VERDICT_ZOMBIE_B
    assert result.owner_pid is None


def test_real_display_is_not_claimed_when_its_serial_collides_with_a_live_helper():
    # spec 2.3: this machine's real LG panels report serials 149094/149084,
    # numerically adjacent to the PID range. A named display that is not ours
    # must stay `real` no matter what the serial collides with.
    processes = {149094: ProcessRecord(pid=149094, ppid=1, command=HELPER_CMD)}
    lg = _display(display_id=2, serial=149094, unit_number=1, name="LG Ultra HD (2)")

    [result] = classify([lg], processes, OURS)

    assert result.verdict == VERDICT_REAL
    assert result.owner_pid is None


def test_probe_display_name_is_also_recognised_as_ours():
    processes = {4903: ProcessRecord(pid=4903, ppid=1, command=HELPER_CMD)}

    [result] = classify([_display(name=PROBE_DISPLAY_NAME, serial=4903)], processes, OURS)

    assert result.verdict == VERDICT_ORPHAN_A


def test_unattributable_display_invisible_to_nsscreen_is_foreign_not_ours():
    # Could be BetterDisplay or DeskPad, legitimately in use. Report, never signal.
    foreign = _display(name=None, in_nsscreen=False, serial=777)

    [result] = classify([foreign], {}, OURS)

    assert result.verdict == VERDICT_FOREIGN_VIRTUAL
    assert result.owner_pid is None


def test_named_display_not_matching_config_is_real():
    [result] = classify([_display(name="Some Other Monitor")], {}, OURS)

    assert result.verdict == VERDICT_REAL


def test_classify_preserves_input_order_and_length():
    displays = [
        _display(display_id=1, is_builtin=True, name="Built-in Retina Display"),
        _display(display_id=2, name="LG Ultra HD (2)"),
        _display(display_id=3, serial=4110),
    ]

    assert _verdicts(displays, {}) == [VERDICT_REAL, VERDICT_REAL, VERDICT_ZOMBIE_B]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_vdisplay_doctor.py -v`
Expected: FAIL with `ImportError: cannot import name 'classify'`

- [ ] **Step 3: Write the implementation**

Append to `ndi_broadcaster/vdisplay_doctor.py`:

```python
def _attributed_owner(
    serial: int, processes: dict[int, ProcessRecord]
) -> ProcessRecord | None:
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
            results.append(
                Classification(display, VERDICT_REAL, None, "built-in display")
            )
            continue

        if (
            display.unit_number == 0
            and display.vendor == GHOST_VENDOR
            and display.model == GHOST_MODEL
            and display.serial == 0
        ):
            results.append(
                Classification(
                    display, VERDICT_APPLE_GHOST, None, "macOS synthetic ghost display"
                )
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
                results.append(
                    Classification(display, VERDICT_REAL, None, "physical display")
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
                    f"helper {owner.pid} reparented to ppid {owner.ppid}; "
                    "its launcher is gone",
                )
            )

    return results
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_vdisplay_doctor.py -v`
Expected: all passed (16 tests, counting the 3 parametrize cases)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff format ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py
uv run ruff check ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py
git add ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py
git commit -m "vdisplay_doctor: classify displays into real/ghost/active/orphan/zombie"
```

---

### Task 3: PyObjC display collection

**Files:**
- Create: `ndi_broadcaster/display_inventory.py`
- Create: `tests/test_display_inventory.py`
- Modify: `pyproject.toml:47-49`

**Interfaces:**
- Consumes: Task 1's `DisplayRecord`.
- Produces: `collect_displays() -> list[DisplayRecord]`, `online_display_ids() -> set[int]`, `recover_names_via_system_profiler() -> dict[int, str]`, `spin_run_loop(seconds: float = 0.3) -> None`.

- [ ] **Step 1: Register the `live` marker**

Modify `pyproject.toml`. Replace:

```toml
[tool.pytest.ini_options]
pythonpath = ["apps/flux-gallery"]
addopts = "--import-mode=importlib"
```

with:

```toml
[tool.pytest.ini_options]
pythonpath = ["apps/flux-gallery"]
# Tests marked `live` need a real WindowServer and create real virtual
# displays, so they are deselected by default and opted into with
# `pytest -m live`. A command-line -m overrides this one (verified).
addopts = "--import-mode=importlib -m 'not live'"
markers = ["live: requires a real display server; not run by default"]
```

- [ ] **Step 2: Write the failing live test**

Create `tests/test_display_inventory.py`:

```python
"""Live checks against the real WindowServer. Run with `pytest -m live`."""

import pytest

from ndi_broadcaster.vdisplay_doctor import VERDICT_APPLE_GHOST, classify

pytestmark = pytest.mark.live


def test_collect_displays_returns_at_least_the_builtin_display():
    from ndi_broadcaster.display_inventory import collect_displays

    displays = collect_displays()

    assert displays, "no online displays reported at all"
    assert all(d.display_id > 0 for d in displays)


def test_no_real_display_is_misclassified_as_apple_ghost():
    # Guards the spec-2.3 trap against the real machine: with no broadcast
    # running, every display present must be real -- in particular the builtin
    # one, which reports unit_number == 0.
    from ndi_broadcaster.display_inventory import collect_displays
    from ndi_broadcaster.vdisplay_doctor import read_process_table

    results = classify(collect_displays(), read_process_table(), "Layout Driver Virtual Display")

    builtins = [c for c in results if c.display.is_builtin]
    assert builtins, "expected a built-in display on this machine"
    assert all(c.verdict != VERDICT_APPLE_GHOST for c in builtins)


def test_online_display_ids_agrees_with_collect_displays():
    from ndi_broadcaster.display_inventory import collect_displays, online_display_ids

    assert online_display_ids() == {d.display_id for d in collect_displays()}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_display_inventory.py -v -m live`
Expected: FAIL with `ModuleNotFoundError: No module named 'ndi_broadcaster.display_inventory'`

Also confirm the marker wiring works — run: `uv run pytest tests/test_display_inventory.py -v`
Expected: `3 deselected`, no failures.

- [ ] **Step 4: Write the implementation**

Create `ndi_broadcaster/display_inventory.py`:

```python
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

from .vdisplay_doctor import DisplayRecord

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


def recover_names_via_system_profiler() -> dict[int, str]:
    """Best-effort display names for displays NSScreen cannot see.

    Only worth its ~1-3s cost in the one case that needs it (see
    collect_displays). system_profiler does not report CGDirectDisplayIDs, so
    names are matched positionally against the online list -- best-effort by
    construction, and used only to label a display that would otherwise be
    reported unnamed.
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
    return dict(zip(_online_ids(), names, strict=False))


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
        fallback_names = recover_names_via_system_profiler()

    records: list[DisplayRecord] = []
    for display_id in ids:
        bounds = Quartz.CGDisplayBounds(display_id)
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
            )
        )
    return records
```

- [ ] **Step 5: Run the live tests to verify they pass**

Run: `uv run pytest tests/test_display_inventory.py -v -m live`
Expected: 3 passed

Then confirm the default run still excludes them and nothing else broke — run: `uv run pytest -q`
Expected: all existing tests pass, live tests deselected.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format ndi_broadcaster/display_inventory.py tests/test_display_inventory.py
uv run ruff check ndi_broadcaster/display_inventory.py tests/test_display_inventory.py
git add ndi_broadcaster/display_inventory.py tests/test_display_inventory.py pyproject.toml
git commit -m "vdisplay_doctor: collect real display inventory via Quartz/AppKit"
```

---

### Task 4: The reaper

**Files:**
- Modify: `ndi_broadcaster/vdisplay_doctor.py`
- Test: `tests/test_vdisplay_doctor.py`

**Interfaces:**
- Consumes: Task 2's `classify` output (`list[Classification]`).
- Produces: `ReapResult` dataclass with fields `display_id: int`, `owner_pid: int`, `outcome: str`, `elapsed_s: float`; the outcome constants `REAPED_SIGTERM`, `REAPED_SIGKILL`, `UNRECLAIMABLE`; and
  `reap_orphans(classifications, *, signal_process, list_display_ids, verify_timeout_s=5.0, poll_interval_s=0.25, sleep=time.sleep, monotonic=time.monotonic) -> list[ReapResult]`.

The success criterion is that the *display* disappeared, not that the process exited. Those are different claims, and `main.swift`'s pre-fix behaviour was exactly a process that exited cleanly while leaking its display — so verifying the process would verify nothing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vdisplay_doctor.py`:

```python
import signal as signal_module

from ndi_broadcaster.vdisplay_doctor import (
    REAPED_SIGKILL,
    REAPED_SIGTERM,
    UNRECLAIMABLE,
    Classification,
    reap_orphans,
)


def _orphan(display_id=69732865, owner_pid=4903):
    return Classification(
        display=_display(display_id=display_id, serial=owner_pid),
        verdict=VERDICT_ORPHAN_A,
        owner_pid=owner_pid,
        detail="test orphan",
    )


class _FakeWorld:
    """Records signals, and drops displays only when the fake helper complies."""

    def __init__(self, present, obeys=signal_module.SIGTERM):
        self.present = set(present)
        self.obeys = obeys
        self.signals = []

    def signal_process(self, pid, sig):
        self.signals.append((pid, sig))
        if self.obeys is not None and sig == self.obeys:
            self.present.discard(69732865)

    def list_display_ids(self):
        return set(self.present)


def _run(world, classifications):
    return reap_orphans(
        classifications,
        signal_process=world.signal_process,
        list_display_ids=world.list_display_ids,
        verify_timeout_s=1.0,
        poll_interval_s=0.1,
        sleep=lambda _s: None,
        monotonic=_FakeClock(),
    )


class _FakeClock:
    """Advances 0.1s per call so timeout loops terminate without real waiting."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.1
        return self.now


def test_reap_sigterms_the_owner_and_confirms_the_display_disappeared():
    world = _FakeWorld(present={69732865})

    [result] = _run(world, [_orphan()])

    assert result.outcome == REAPED_SIGTERM
    assert world.signals == [(4903, signal_module.SIGTERM)]
    assert 69732865 not in world.list_display_ids()


def test_reap_escalates_to_sigkill_when_sigterm_is_ignored():
    world = _FakeWorld(present={69732865}, obeys=signal_module.SIGKILL)

    [result] = _run(world, [_orphan()])

    assert result.outcome == REAPED_SIGKILL
    assert world.signals == [
        (4903, signal_module.SIGTERM),
        (4903, signal_module.SIGKILL),
    ]


def test_reap_reports_unreclaimable_when_the_display_never_disappears():
    # Must not hang, and must not claim success just because signals were sent.
    world = _FakeWorld(present={69732865}, obeys=None)

    [result] = _run(world, [_orphan()])

    assert result.outcome == UNRECLAIMABLE
    assert world.signals == [
        (4903, signal_module.SIGTERM),
        (4903, signal_module.SIGKILL),
    ]


def test_reap_never_signals_active_or_zombie_or_foreign_displays():
    world = _FakeWorld(present={1, 2, 3})
    untouchable = [
        Classification(_display(display_id=1), VERDICT_ACTIVE, 4903, "live broadcast"),
        Classification(_display(display_id=2), VERDICT_ZOMBIE_B, None, "owner gone"),
        Classification(_display(display_id=3), VERDICT_FOREIGN_VIRTUAL, None, "someone else's"),
        Classification(_display(display_id=4), VERDICT_REAL, None, "physical"),
    ]

    results = _run(world, untouchable)

    assert results == []
    assert world.signals == []


def test_reap_tolerates_an_owner_that_already_exited():
    world = _FakeWorld(present={69732865})

    def raise_process_lookup(pid, sig):
        world.signals.append((pid, sig))
        raise ProcessLookupError

    world.signal_process = raise_process_lookup

    [result] = _run(world, [_orphan()])

    assert result.outcome == UNRECLAIMABLE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_vdisplay_doctor.py -v -k reap`
Expected: FAIL with `ImportError: cannot import name 'reap_orphans'`

- [ ] **Step 3: Write the implementation**

Add `import signal` and `import time` to the imports at the top of `ndi_broadcaster/vdisplay_doctor.py`, then append:

```python
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
```

Also add to the top-of-file imports: `import os`, `import signal`, `import time`, and `from collections.abc import Callable`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_vdisplay_doctor.py -v`
Expected: all passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff format ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py
uv run ruff check ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py
git add ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py
git commit -m "vdisplay_doctor: reap reclaimable orphans, verifying the display is gone"
```

---

### Task 5: The probe

**Files:**
- Modify: `ndi_broadcaster/vdisplay_doctor.py`
- Create: `tests/test_vdisplay_doctor_live.py`
- Test: `tests/test_vdisplay_doctor.py`

**Interfaces:**
- Consumes: Task 3's `online_display_ids`; `PROBE_DISPLAY_NAME`.
- Produces: `ProbeResult` dataclass with fields `ok: bool`, `timings: dict[str, float]`, `failure_phase: str | None`, `message: str`; and
  `probe(width, height, *, helper_dir, teardown_timeout_s=5.0, poll_interval_s=0.25) -> ProbeResult`.

- [ ] **Step 1: Write the failing hermetic test**

Append to `tests/test_vdisplay_doctor.py`:

```python
from ndi_broadcaster.vdisplay_doctor import PROBE_DISPLAY_NAME, ProbeResult


def test_probe_uses_the_dedicated_probe_name_never_the_broadcast_name():
    # A probe display must never be confusable with a real broadcast display,
    # so that a leaked probe is immediately attributable to the probe.
    assert PROBE_DISPLAY_NAME == "Layout Driver Probe Display"
    assert PROBE_DISPLAY_NAME != OURS


def test_probe_result_reports_failure_phase_and_is_not_ok():
    result = ProbeResult(
        ok=False, timings={"create": 2.1}, failure_phase="settle", message="timed out"
    )

    assert not result.ok
    assert result.failure_phase == "settle"
```

- [ ] **Step 2: Write the failing live test**

Create `tests/test_vdisplay_doctor_live.py`:

```python
"""Full real create-settle-teardown cycle. Run with `pytest -m live`.

This is the regression test for main.swift's shutdownAndExit() ARC fix: it
calls the production startup path unmodified, so if releasing the
CGVirtualDisplay before exit() ever stops working, the teardown phase here
fails instead of silently leaking a display.
"""

from pathlib import Path

import pytest

from ndi_broadcaster.vdisplay_doctor import PROBE_DISPLAY_NAME, probe

pytestmark = pytest.mark.live

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_DIR = REPO_ROOT / "ndi_broadcaster" / "vdisplay_helper"


def test_probe_completes_a_full_cycle_and_leaves_nothing_behind():
    from ndi_broadcaster.display_inventory import collect_displays

    before = {d.display_id for d in collect_displays()}

    result = probe(1920, 1080, helper_dir=HELPER_DIR)

    assert result.ok, f"probe failed in {result.failure_phase}: {result.message}"
    assert set(result.timings) == {"build", "create", "settle", "teardown"}

    after = {d.display_id for d in collect_displays()}
    assert after == before, "probe leaked a display"
    assert not [d for d in collect_displays() if d.name == PROBE_DISPLAY_NAME]
```

- [ ] **Step 3: Run both to verify they fail**

Run: `uv run pytest tests/test_vdisplay_doctor.py tests/test_vdisplay_doctor_live.py -v -m "live or not live"`
Expected: FAIL with `ImportError: cannot import name 'probe'`

- [ ] **Step 4: Write the implementation**

Append to `ndi_broadcaster/vdisplay_doctor.py`:

```python
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
    from .display_inventory import online_display_ids
    from .virtual_display import (
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
            proc, info = start_vdisplay_helper(
                binary_path, width, height, PROBE_DISPLAY_NAME
            )
        except (TimeoutError, RuntimeError, ValueError) as exc:
            return _fail("create", str(exc))
        display_id = info.display_id
        timings["create"] = time.monotonic() - started

        started = time.monotonic()
        try:
            wait_for_settled_bounds(display_id, width, height)
        except TimeoutError as exc:
            return _fail("settle", str(exc))
        timings["settle"] = time.monotonic() - started

        started = time.monotonic()
        proc.terminate()
        try:
            proc.wait(timeout=teardown_timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
        deadline = time.monotonic() + teardown_timeout_s
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
        timings["teardown"] = time.monotonic() - started
        return _fail(
            "teardown",
            f"display {display_id} still online {teardown_timeout_s}s after the helper "
            "was terminated -- shutdownAndExit()'s ARC release may have regressed",
        )
    finally:
        # Never leave a probe display behind, whichever phase failed.
        if proc is not None:
            proc.kill()
```

Add `from pathlib import Path` to the top-of-file imports.

- [ ] **Step 5: Run both to verify they pass**

Run: `uv run pytest tests/test_vdisplay_doctor.py -v`
Expected: all passed

Run: `uv run pytest tests/test_vdisplay_doctor_live.py -v -m live`
Expected: 1 passed. Then verify no residue by hand:
`uv run python -c "from ndi_broadcaster.display_inventory import collect_displays; print([(d.display_id, d.name) for d in collect_displays()])"`
Expected: only the real displays.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py tests/test_vdisplay_doctor_live.py
uv run ruff check ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py tests/test_vdisplay_doctor_live.py
git add ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py tests/test_vdisplay_doctor_live.py
git commit -m "vdisplay_doctor: add create-settle-teardown probe as the pre-run health gate"
```

---

### Task 6: The CLI

**Files:**
- Modify: `ndi_broadcaster/vdisplay_doctor.py`
- Test: `tests/test_vdisplay_doctor.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: `EXIT_CLEAN = 0`, `EXIT_DIRTY = 1`, `EXIT_PROBE_FAILED = 2`, `EXIT_ERROR = 3`; `broadcaster_yaml_path(env: dict[str, str]) -> Path`; `format_table(classifications) -> str`; `main(argv: list[str] | None = None) -> int`; a `__main__` block.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vdisplay_doctor.py`:

```python
from pathlib import Path

from ndi_broadcaster.vdisplay_doctor import (
    EXIT_CLEAN,
    EXIT_DIRTY,
    EXIT_PROBE_FAILED,
    broadcaster_yaml_path,
    format_table,
    main,
)


def test_broadcaster_yaml_path_matches_the_launchers_own_resolution():
    # This module deliberately re-derives one env-var default rather than
    # importing launcher.py (which pulls in playwright, numpy and cyndilib at
    # module scope, far too heavy for a sub-second scan). This test pins the
    # duplication so the two cannot drift apart silently.
    from ndi_broadcaster.launcher import resolve_launcher_paths

    for env in ({}, {"BROADCASTER_YAML": "/tmp/custom.yaml"}):
        assert broadcaster_yaml_path(env) == resolve_launcher_paths(env).broadcaster_yaml


def test_format_table_shows_verdict_owner_and_name_for_each_display():
    rows = [
        Classification(
            _display(display_id=1, is_builtin=True, name="Built-in Retina Display"),
            VERDICT_REAL,
            None,
            "built-in display",
        ),
        Classification(_display(display_id=2, serial=4903), VERDICT_ORPHAN_A, 4903, "reparented"),
    ]

    text = format_table(rows)

    assert "Built-in Retina Display" in text
    assert VERDICT_ORPHAN_A in text
    assert "4903" in text


def test_scan_exits_clean_on_a_healthy_machine(monkeypatch):
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor._collect_displays",
        lambda: [_display(display_id=1, is_builtin=True, name="Built-in Retina Display")],
    )
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", lambda: {})

    assert main(["scan", "--name", OURS]) == EXIT_CLEAN


def test_scan_exits_dirty_when_a_zombie_is_present(monkeypatch):
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor._collect_displays",
        lambda: [_display(serial=4110)],
    )
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", lambda: {})

    assert main(["scan", "--name", OURS]) == EXIT_DIRTY


def test_reap_exits_dirty_when_an_unreclaimable_zombie_remains(monkeypatch):
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor._collect_displays",
        lambda: [_display(serial=4110)],
    )
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", lambda: {})
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor._online_display_ids", lambda: {69732865}
    )

    assert main(["reap", "--name", OURS]) == EXIT_DIRTY


def test_probe_refuses_to_run_on_a_dirty_machine(monkeypatch):
    # A probe against an already-stuck WindowServer reports nothing
    # trustworthy, and risks adding to the accumulation that causes the hang.
    called = []
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor._collect_displays",
        lambda: [_display(serial=4110)],
    )
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", lambda: {})
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor.probe",
        lambda *a, **k: called.append(1),
    )

    assert main(["probe", "--name", OURS]) == EXIT_DIRTY
    assert called == []


def test_probe_force_overrides_the_dirty_machine_refusal(monkeypatch):
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor._collect_displays",
        lambda: [_display(serial=4110)],
    )
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", lambda: {})
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor.probe",
        lambda *a, **k: ProbeResult(
            ok=False, timings={}, failure_phase="settle", message="nope"
        ),
    )

    assert main(["probe", "--name", OURS, "--force"]) == EXIT_PROBE_FAILED


def test_probe_exit_code_distinguishes_broken_from_dirty(monkeypatch):
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor._collect_displays", lambda: [])
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", lambda: {})
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor.probe",
        lambda *a, **k: ProbeResult(
            ok=True, timings={"build": 0.0}, failure_phase=None, message="ok"
        ),
    )

    assert main(["probe", "--name", OURS]) == EXIT_CLEAN
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_vdisplay_doctor.py -v`
Expected: FAIL with `ImportError: cannot import name 'main'`

- [ ] **Step 3: Write the implementation**

Append to `ndi_broadcaster/vdisplay_doctor.py`:

```python
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


def _resolve_display_name(explicit: str | None) -> str:
    """--name wins, so scan/reap work with no config file present at all."""
    if explicit is not None:
        return explicit
    return load_broadcaster_config(
        broadcaster_yaml_path(dict(os.environ))
    ).sck_virtual_display_name


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


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

    try:
        display_name = _resolve_display_name(args.name)
    except (OSError, ValueError) as exc:
        print(f"ERROR  could not resolve the virtual display name: {exc}")
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
        remaining = [
            item for item in classifications if item.verdict == VERDICT_ZOMBIE_B
        ] + [r for r in results if r.outcome == UNRECLAIMABLE]
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

    config = load_broadcaster_config(broadcaster_yaml_path(dict(os.environ)))
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
```

Add `import argparse` to the top-of-file imports, and
`from .config import load_broadcaster_config` — light (yaml + pydantic only),
unlike importing `launcher`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_vdisplay_doctor.py -v`
Expected: all passed

- [ ] **Step 5: Exercise all three commands for real**

```bash
uv run python -m ndi_broadcaster.vdisplay_doctor scan   ; echo "exit=$?"
uv run python -m ndi_broadcaster.vdisplay_doctor probe  ; echo "exit=$?"
uv run python -m ndi_broadcaster.vdisplay_doctor reap   ; echo "exit=$?"
```

Expected on a healthy machine: `scan` lists the three real displays and exits 0;
`probe` prints four phase timings and exits 0; `reap` finds nothing to do and exits 0.
Then confirm the probe left nothing behind:
`uv run python -m ndi_broadcaster.vdisplay_doctor scan` — same display count as the first run.

- [ ] **Step 6: Full suite, lint, and commit**

```bash
uv run pytest -q
uv run pytest -q -m live
uv run ruff format .
uv run ruff check .
git add ndi_broadcaster/vdisplay_doctor.py tests/test_vdisplay_doctor.py
git commit -m "vdisplay_doctor: add the scan/reap/probe CLI with distinct exit codes"
```

---

### Task 7: Cross-reference the tool from the docs

**Files:**
- Modify: `docs/bugs.md:98-104`
- Modify: `README.md`

**Interfaces:**
- Consumes: the CLI from Task 6.
- Produces: no code.

The `bugs.md` workaround currently ends in a hand-typed verification step. Now that a tool does it, the workaround should name the tool — otherwise the next person to hit the hang re-derives it.

- [ ] **Step 1: Update the operational workaround in `docs/bugs.md`**

Replace the "Current operational workaround" paragraph (`docs/bugs.md:98-104`) with:

```markdown
**Current operational workaround:** if a stop hangs, `SIGKILL` the launcher
process, then run `python -m ndi_broadcaster.vdisplay_doctor reap` — it finds
the orphaned `vdisplay_helper` (attributed via the creator PID `main.swift`
stores in `descriptor.serialNum`), `SIGTERM`s it, and verifies the display
actually left WindowServer rather than just that the process exited. Order
matters: reap *after* killing the launcher, because while the launcher is still
alive its helper is correctly classified `active` and left alone. Before
restarting, `python -m ndi_broadcaster.vdisplay_doctor probe` confirms the
machine can still create and tear down a virtual display. See
`docs/superpowers/specs/2026-08-11-vdisplay-doctor-design.md`.
```

- [ ] **Step 2: Add a README section**

Add to `README.md`, near the existing broadcaster documentation:

```markdown
### Checking for zombie virtual displays

The `sck`/`virtual` backend creates a virtual display via a private macOS API
that has no remove-by-ID call — a display can only be torn down by the process
that created it. If that process is killed uncleanly the display can outlive
it, and enough accumulated zombies make every subsequent virtual display hang
at creation.

```bash
python -m ndi_broadcaster.vdisplay_doctor scan    # read-only, ~0.5s, safe any time
python -m ndi_broadcaster.vdisplay_doctor reap    # SIGTERM orphaned helpers, verify
python -m ndi_broadcaster.vdisplay_doctor probe   # ~5-8s create/teardown health gate
```

Exit codes: `0` clean, `1` orphan or zombie present, `2` probe failed, `3` error.

Run `probe` before a long broadcast to confirm the machine is fit, and `reap`
after an unclean stop. `reap` never touches a display that is serving a live
broadcast, so it is safe to run at any time.
```

- [ ] **Step 3: Commit**

```bash
git add docs/bugs.md README.md
git commit -m "docs: point the zombie-display workaround at vdisplay_doctor"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §2.2 serial→PID attribution | 2 (`_attributed_owner`) |
| §2.3 ghost conjunction + builtin trap | 2 (`classify`, two named tests), 3 (live guard) |
| §4.1 inputs, `command=` not `comm=` | 1 (`read_process_table`), 3 (`collect_displays`) |
| §4.2 all seven verdict rows | 2 |
| §4.2 hung-launcher ordering limitation | 7 (documented in `bugs.md`) |
| §4.3 PID-reuse guard | 2 (`_attributed_owner`, named test) |
| §4.4 run-loop spin | 3 (`spin_run_loop`) |
| §4.5 system_profiler slow path | 3 (`collect_displays`, gated on the count mismatch) |
| §5.1 `scan` | 6 |
| §5.2 `reap` + ladder | 4 (loop), 6 (`LADDER` output) |
| §5.3 `probe` incl. distinct name, configured resolution, dirty-machine refusal, sleep warning, per-phase timings | 5 (probe), 6 (refusal, sleep warning) |
| §5.4 exit codes | 6 |
| §6.1 hermetic tests | 1, 2, 4, 5, 6 |
| §6.2 live marker + opt-in | 3 (`pyproject.toml`), 5 (live probe test) |
| §7 known limitation | 6 (`LADDER` text), 7 (docs) |

No gaps.

**Placeholder scan:** No TBD/TODO, no "add error handling", no "similar to Task N". Every code step contains runnable code.

**Type consistency:** `DisplayRecord`, `ProcessRecord`, `Classification`, `ReapResult`, `ProbeResult` are each defined once (Tasks 1, 4, 5) and used with matching field names throughout. `classify` keeps its Task 2 signature at both call sites (Tasks 3, 6). `reap_orphans`' injected `signal_process`/`list_display_ids` names match the Task 6 call site. `online_display_ids` (Task 3) is what Tasks 5 and 6 consume.

**One deviation from the spec worth flagging at review:** §4.2 defines "ours" as a name match *or* a serial attribution. Task 2 implements a stricter rule — when a name is available it is authoritative, and serial attribution is used only when the name is `None`. This is what makes `test_real_display_is_not_claimed_when_its_serial_collides_with_a_live_helper` pass, and it can only ever shrink the set of displays the tool will signal, never grow it. Its one cost: a `zombie_b` whose name cannot be recovered even via the §4.5 slow path is reported `foreign_virtual` instead. Both are report-only verdicts, so no action changes — but `foreign_virtual` does not set exit 1, so such a display would not fail a `scan`. Accepted as the safer trade; noted here rather than buried.
