# vdisplay_doctor — Design Spec

## 1. Purpose

`docs/bugs.md` records an unresolved broadcaster shutdown hang whose
operational workaround ends with three manual steps: `SIGKILL` the launcher,
separately `SIGTERM` any orphaned `vdisplay_helper` to avoid leaking its
virtual display, then "confirm via `NSApplication.sharedApplication()` + a
brief `CFRunLoopRunInMode` spin + `NSScreen.screens()` that no zombie remains
before restarting."

That last step is currently ad-hoc: a hand-typed one-off script, run from
memory, whose failure mode is silent. The
`2026-08-10-sck-capture-backend-design.md` §10 addendum records what it costs
to get wrong — accumulated zombie virtual displays made
`CGCompleteDisplayConfiguration` hang for every subsequently created virtual
display, blocking `sck`/`virtual` entirely, and in the worst observed case the
stuck state only cleared after an overnight wait.

This spec builds `vdisplay_doctor`: a small, standalone tool that answers
"is this machine fit to start a broadcast?" and "did the last run leak
anything?" reproducibly, in seconds, and that reclaims the one class of leak
that is actually reclaimable.

## 2. Prior research and what it rules out

Four findings from the private-API surface and from published reports shaped
this design. They are recorded here because two of them rule out approaches
that look obvious.

**2.1 There is no API to remove a virtual display you do not own.**
`ndi_broadcaster/vdisplay_helper/CGVirtualDisplayPrivate.h` declares the
complete `CGVirtualDisplay` interface: `initWithDescriptor:` and
`applySettings:`. There is no `terminate`, no remove-by-display-ID, and no
ownership-transfer call anywhere on the class. Teardown happens **only** via
ARC deinit inside the process holding the last strong reference — exactly what
`main.swift`'s `shutdownAndExit()` does. Consequence: "kill a zombie display"
is not expressible as an operation. The only available levers are reaping the
owning process, or resetting WindowServer wholesale.

**Invariant (SIGTERM-only teardown).** ARC-deinit-only teardown is not merely
an implementation note; it is a rule about which signals this codebase may
send. `shutdownAndExit()` is wired to `SIGINT` and `SIGTERM` via
`DispatchSource`, and it is the *only* code path that sets `display = nil` and
so drops the CGVirtualDisplay's refcount to zero. **`SIGKILL` cannot be caught
and runs no handler at all.** Therefore:

> **`SIGTERM` is the only signal that tears a virtual display down. `SIGKILL`
> manufactures an unreclaimable `zombie_b` (§4.2): a live display in
> WindowServer whose owning process is dead, which by §2.1 nothing can remove.
> `SIGKILL` is permissible only as an *escalation*, after a `SIGTERM` has
> actually been sent and given time to work.**

The counter-intuitive corollary is worth stating plainly, because it is the
part that gets coded wrong: on any path where a helper has already created its
display, `proc.kill()` is **strictly worse than doing nothing**. Leaving the
helper alive produces `orphan_a`, which `reap` reclaims in seconds; killing it
produces `zombie_b`, whose remedy ladder starts at "wait it out" and tops out
at a reboot (§2.4). The correct shape everywhere — `start_vdisplay_helper`'s
failure paths, `probe`'s cleanup, `launcher.py`'s `finally`, and §5.2's reap
ladder — is `terminate()` → bounded `wait()` → `kill()` only on timeout. This
is precisely why §5.2's ladder is SIGTERM-then-SIGKILL and never SIGKILL
alone: rung 2 exists only for a helper that has proven it will not honour rung
1, at which point the display is already lost either way.

**2.2 `serialNum` already carries the owning PID.** `main.swift` sets
`descriptor.serialNum = UInt32(ProcessInfo.processInfo.processIdentifier)`.
`CGDisplaySerialNumber(displayID)` therefore returns the PID of the
`vdisplay_helper` that created that display. This is a stronger attribution key
than matching the display name, and unlike name-matching it stays correct when
several instances run concurrently (see
`2026-08-09-multi-instance-broadcaster-design.md`). It is what makes the
reclaimable/unreclaimable split in §4 decidable rather than guessed.

**2.3 macOS synthesizes its own ghost display, which is not our leak.** An
Apple DTS engineer confirmed on the developer forums
([thread 787154](https://developer.apple.com/forums/thread/787154)) that macOS
fabricates a 640x480 ghost screen with `CGDisplayUnitNumber() == 0`
(`kCGNullDirectDisplay`), vendor number `0x756E6B6E` (`"unkn"`), model number
`0x76697274` (`"virt"`), and serial `0`. It appears and disappears on its own,
notably when a physical display powers off. Counting it as a leak would make
this tool report zombies on a healthy machine — filtering it is a correctness
requirement, not a nicety.

**The filter that thread recommends (`CGDisplayUnitNumber != 0`) is wrong on
this machine, and must not be used on its own.** Enumerating the real display
set here produced:

```
id  unit  vendor  model   serial      builtin  active  name
1   0     1552    41055   4251086178  1        1       Built-in Retina Display
2   1     7789    23305   149094      0        1       LG Ultra HD (2)
3   2     7789    23305   149084      0        1       LG Ultra HD (1)
```

The **built-in display legitimately reports `unit_number == 0`**. Filtering on
that alone would classify this laptop's own screen as a ghost. The ghost test
must therefore be the full conjunction — `unit_number == 0` **and**
`vendor == 0x756E6B6E` **and** `model == 0x76697274` **and** `serial == 0` —
and `is_builtin` must be checked before it (§4.2).

The same enumeration also shows why §2.2's `serial` → PID mapping cannot be the
sole attribution key: the two real LG panels report serials 149094 and 149084,
numerically adjacent to the macOS PID range (default max 99999). A future
display could plausibly report a serial that collides with a live PID, so
attribution requires the name match and the §4.3 guard as well.

**2.4 The remedy ladder tops out at logging out.** No published account
describes reclaiming a stuck virtual display in-session. Restarting
`com.apple.WindowServer` via `launchctl` is documented as unreliable and
potentially unrecoverable without a reboot; deleting
`/Library/Preferences/com.apple.windowserver.displays.plist` and the
`ByHost` equivalents
([Plugable KB](https://kb.plugable.com/docking-stations-and-video/how-do-i-remove-display-configurations-or-reset-display-persistence-in-macos))
resets *persisted arrangement* on next login, not live WindowServer state.
§10 additionally records that restarting `com.apple.colorsync.displayservices`
and the `DisplaysExt` ExtensionKit process were both tried live and neither
cleared the stuck state. This design therefore automates no rung above
`SIGKILL`, and does not ship flags for the known-ineffective steps.

**2.5 The reframe this produces.** Given 2.1 and 2.4, "is the display list
clean?" is the wrong question to build a gate around, because a dirty list may
be unfixable. The question that actually protects a run is **"can this machine
create, configure, and tear down a virtual display right now?"** — which is
directly observable, since the §10 failure mode was `CGCompleteDisplayConfiguration`
hanging. §5.3's `probe` answers exactly that, in seconds, leaving no residue.

## 3. Scope

In scope: a new `ndi_broadcaster/vdisplay_doctor.py` module with a `scan` /
`reap` / `probe` CLI, hermetic unit tests, and one opt-in live test.

Out of scope, deliberately:

- `run.sh` is not modified. This is a standalone tool, invoked by hand before
  and after runs as needed.
- No destructive escalation rungs (§2.4).
- No changes to `main.swift`, `launcher.py`, or `config.py`. `probe` consumes
  the existing startup path as-is; that it does so unmodified is what makes it
  a regression test for the §10 `shutdownAndExit()` fix rather than a parallel
  reimplementation of it. (`virtual_display.py` was originally listed here too.
  It has since been amended, but only to make its own failure paths honour
  §2.1's SIGTERM-only invariant — the create/settle/teardown sequence `probe`
  exercises is unchanged.)

## 4. Classification

The decision logic is one pure function with no I/O, so every verdict below is
unit-testable without a display server:

```python
def classify(
    displays: list[DisplayRecord],
    processes: dict[int, ProcessRecord],
    config_display_name: str,
) -> list[Classification]: ...
```

### 4.1 Inputs

Gathered by thin collectors, each separately testable and each replaceable by a
fake in tests:

`DisplayRecord`, one per entry from `CGGetOnlineDisplayList` — the *online*
list, not `CGGetActiveDisplayList`, because a zombie can be online while
inactive and would be invisible to the active list:

| Field | Source |
|---|---|
| `display_id` | `CGGetOnlineDisplayList` |
| `vendor`, `model`, `serial` | `CGDisplayVendorNumber` / `ModelNumber` / `SerialNumber` |
| `unit_number` | `CGDisplayUnitNumber` |
| `is_builtin`, `is_active`, `is_asleep` | `CGDisplayIsBuiltin` / `IsActive` / `IsAsleep` |
| `bounds` | `CGDisplayBounds` |
| `name` | `NSScreen.localizedName`, best-effort (§4.4) |

`ProcessRecord`, from a single `ps -Awwo pid=,ppid=,command=` invocation:
`pid`, `ppid`, `command`. One call, not one per candidate, so a scan stays fast
regardless of process count.

`command=` (full argument vector), **not** `comm=`: verified empirically that
`comm=` reports only the executable path, so a launcher started as
`python -m ndi_broadcaster.launcher` shows up as
`/opt/homebrew/.../MacOS/Python` with no trace of the module name. Matching
`comm=` would make the `active` verdict in §4.2 unmatchable, and every live
broadcast's helper would be misclassified `orphan_a` and reaped mid-show.
`-ww` defeats ps's width-based truncation, which does not apply when stdout is
a pipe but does when run interactively.

Also verified: `uv run python -m ndi_broadcaster.launcher` produces a
**two-level** process tree — `uv run …` (ppid 1) forks the real
`Python … -m ndi_broadcaster.launcher`. `vdisplay_helper`'s direct parent is
the inner Python process, so a single PPID hop is sufficient to reach the
launcher and no chain-walking is needed.

### 4.2 Verdicts

Evaluated **in this order**; the first matching row wins. The built-in display
is protected by the full ghost conjunction (all four conditions: `unit_number`,
`vendor`, `model`, and `serial`), but checking `is_builtin` first is
defense-in-depth (§2.3).

A display counts as **ours** when its `name` matches `config_display_name` (or
the probe name, §5.3), or when its `serial` attributes to a live
`vdisplay_helper` under the §4.3 guard.

| # | Verdict | Test | Action |
|---|---|---|---|
| 1 | `real` | `is_builtin` | Ignored |
| 2 | `apple_ghost` | `unit_number == 0` **and** `vendor == 0x756E6B6E` **and** `model == 0x76697274` **and** `serial == 0` | Ignored entirely (§2.3) |
| 3 | `active` | ours; owner PID live; owner's PPID is a live `ndi_broadcaster.launcher` | **Left alone** — serving a live broadcast |
| 4 | `orphan_a` | ours; owner PID live; owner's PPID is gone (reparented to 1) | **Reclaimable** — SIGTERM (§5.2) |
| 5 | `zombie_b` | ours; owner PID dead or fails the §4.3 guard | **Unreclaimable** — report the ladder |
| 6 | `foreign_virtual` | not ours, and absent from the `NSScreen` list | **Reported, never signalled** — may be BetterDisplay/DeskPad, not ours to touch |
| 7 | `real` | everything else | Ignored |

`orphan_a` is the `bugs.md` leak: the launcher was `SIGKILL`ed, so its `finally`
block never ran `vdisplay_proc.terminate()` (`launcher.py:445-450`), and the
helper outlived it and reparented to launchd. The helper's own graceful path is
confirmed reliable in isolation, so signalling it works. `zombie_b` is the §10
"Unresolved" state, where by §2.1 nothing can be done.

**Ordering limitation, by design.** While the launcher is hung but still
*alive* — the exact state `bugs.md` describes before you `SIGKILL` it — its
helper's PPID is a live launcher, so the display classifies `active` and `reap`
will not touch it. There is no reliable way to distinguish "hung launcher" from
"healthy broadcast" by process tree alone, and guessing wrong kills a live show.
This matches `bugs.md`'s workaround ordering: `SIGKILL` the launcher **first**,
then `reap`. `scan` makes this legible rather than mysterious — seeing `active`
when you believe nothing is broadcasting tells you the launcher is still alive
and is the thing to kill.

### 4.3 Safety rule: PID-reuse guard

`serial` → PID attribution is trusted **only** when that PID is live *and* its
`command` names the `vdisplay_helper` binary. PIDs are recycled; a display created by a
long-dead helper whose PID now belongs to an unrelated process would otherwise
cause this tool to signal that unrelated process. When the PID is live but is
not a `vdisplay_helper`, the display downgrades to `zombie_b` — reported, never
signalled. Losing the ability to reclaim a display is an acceptable cost;
SIGTERMing an arbitrary process is not.

### 4.4 Safety rule: the run-loop spin

`NSScreen.screens()` caches, and refreshes only when the process's run loop
processes the display-reconfiguration notification. §10 records the sharper
version of this: `NSScreen.screens()` and `CGGetActiveDisplayList` return
"inconsistent, sometimes flatly wrong results when queried without first
spinning a `CFRunLoop`", and `NSApplication.sharedApplication()` alone is not
sufficient. The display collector therefore calls
`AppKit.NSApplication.sharedApplication()` and then
`Quartz.CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.3, False)` before reading
anything, reusing the pattern already proven in
`virtual_display.py:118-124`. Skipping this would make the tool's verdicts
non-reproducible, defeating its entire purpose.

### 4.5 Slow-path name recovery

A `zombie_b` may be absent from `NSScreen` entirely, leaving `name` unavailable
and attribution resting on `serial` alone. When — and only when — the online
display count exceeds the `NSScreen` count, some display exists that AppKit
cannot see, which is precisely the zombie signature and precisely the case
where the fast path lacks a name. Only then does the collector spend the
~1-3s on `system_profiler SPDisplaysDataType -json` to recover names; §10
confirms zombies remain visible there long after their helper processes exit.
A healthy machine never pays this cost, keeping `scan` sub-second in the common
case.

## 5. Commands

### 5.1 `scan` — read-only

Collects, classifies, prints a table. Signals nothing, creates nothing. Safe to
run at any time including mid-broadcast. Target runtime ~0.5s.

```
$ python -m ndi_broadcaster.vdisplay_doctor scan
display  verdict  owner  name
1        real     -      Built-in Retina Display
2        real     -      LG Ultra HD (2)
3        real     -      LG Ultra HD (1)
OK  3 real, 0 virtual, 0 orphaned  (0.4s)
```

A dirty machine, showing all three actionable verdicts at once:

```
$ python -m ndi_broadcaster.vdisplay_doctor scan
display    verdict    owner   name
1          real       -       Built-in Retina Display
69732865   active     4821    Layout Driver Virtual Display
69732866   orphan_a   4903    Layout Driver Virtual Display
69732867   zombie_b   4110*   Layout Driver Virtual Display
                              * no such process
FAIL  1 orphaned (reclaimable), 1 zombie (unreclaimable)  (1.9s)
```

### 5.2 `reap`

Runs `scan`, then for each `orphan_a`, in order:

1. `SIGTERM` the owner PID.
2. Poll the online display list until that `display_id` is **gone**, up to 5s.

Step 2 is the actual success criterion. "The process exited" and "the display
was torn down" are different claims, and only the second is what matters —
`main.swift`'s pre-fix behaviour was precisely a process that exited cleanly
while leaking its display. Verifying the process is not verifying the fix.

3. Still present after 5s: `SIGKILL`, re-verify.

The ladder starts at `SIGTERM` and reaches `SIGKILL` only after it, because of
§2.1's SIGTERM-only invariant: `SIGTERM` runs `shutdownAndExit()` and is the
only thing that tears the display down, while `SIGKILL` runs no handler and
would convert a reclaimable `orphan_a` into an unreclaimable `zombie_b`. Rung 2
is a last resort for a helper that has demonstrably ignored rung 1 — at which
point the display is lost either way and at least the process is reclaimed.
The same terminate → bounded wait → kill shape is required of every other
teardown path in the codebase (§2.1).


4. Still present, or any `zombie_b` exists: print the ladder, exit 1.

```
rung 1  SIGTERM owner  -> verify display gone   [auto]
rung 2  SIGKILL owner  -> verify display gone   [auto]
──────────────────────── stop ────────────────────────
rung 3  wait it out (minutes..overnight)        [advise]
rung 4  log out / log back in                   [advise]
rung 5  reboot                                  [advise]
```

`active` displays are never signalled (§4.2), which is what makes `reap` safe
to run without first checking whether a broadcast is in progress.

### 5.3 `probe` — the health gate

Exercises the real startup path end to end and tears it down again:
`ensure_helper_built` → `start_vdisplay_helper` → `wait_for_settled_bounds` →
`SIGTERM` → verify the display is gone from the online list. Because it calls
the production functions unmodified, a regression in `shutdownAndExit()`'s ARC
teardown fails this probe.

- **Distinct name.** Uses `"Layout Driver Probe Display"`, never
  `config.sck_virtual_display_name`. A probe display can then never be confused
  with a broadcast display in `scan` output or in `system_profiler`, and a
  leaked probe is immediately attributable to the probe.
- **Configured resolution.** Uses `config.width` x `config.height`. The fixed
  ~1.5s settle plus the retry loop in `main.swift` dominate the runtime;
  pixel count does not. Probing at 3840x2160 tests what actually runs.
- **Refuses a poisoned machine.** If `scan` already finds `orphan_a` or
  `zombie_b`, `probe` exits without creating anything: a probe against a
  WindowServer that is already stuck reports nothing trustworthy, and risks
  adding to the accumulation §10 identifies as the trigger. `--force`
  overrides.
- **Sleep warning.** Warns if any display reports `is_asleep`, per §10's
  observed correlation between display sleep and unreliable
  `CGVirtualDisplay` creation.
- **Per-phase timings.** Reported individually so a *slowing*
  `CGCompleteDisplayConfiguration` is visible before it becomes a *hanging*
  one.

```
$ python -m ndi_broadcaster.vdisplay_doctor probe
build     0.0s (cached)
create    2.1s
settle    1.6s
teardown  0.5s
OK  create/settle/teardown all succeeded  (4.2s)
```

Every wait in `probe` is bounded; it inherits the existing 15s and 20s
timeouts from `start_vdisplay_helper` and `wait_for_settled_bounds`, and adds
a 5s bound on teardown verification.

### 5.4 Exit codes

| Code | Meaning |
|---|---|
| 0 | Clean — nothing orphaned; for `probe`, the full cycle succeeded |
| 1 | `orphan_a` or `zombie_b` present (`scan`), or still present after `reap` |
| 2 | `probe` failed or timed out — the machine is not fit to start a run |
| 3 | Internal error |

1 and 2 are distinct so a caller can tell "dirty machine" from "machine is
broken", which are different decisions.

## 6. Testing

### 6.1 `tests/test_vdisplay_doctor.py` — hermetic

Synthetic `DisplayRecord` lists and fake process tables; no AppKit, no display
server, no subprocesses. Covers every row of §4.2 plus:

- The PID-reuse guard (§4.3) refusing to signal a live non-`vdisplay_helper`
  PID, downgrading to `zombie_b`.
- The `apple_ghost` filter, including that a healthy machine reporting
  Apple's ghost yields exit 0.
- **The builtin-`unit_number == 0` trap (§2.3):** a builtin display with
  `unit_number == 0` and a real vendor/model/serial must classify `real`, not
  `apple_ghost`. Seeded from the actual values measured on this machine
  (`unit=0 vendor=1552 model=41055 serial=4251086178 builtin=1`), so the test
  fails if anyone reintroduces the bare `unit != 0` filter.
- A real display whose serial happens to fall in the PID range and collides
  with a live PID must still classify `real`, not be attributed to us.
- An `active` display never being signalled.
- The reaper's verify-disappearance loop, driven by a fake lister that stops
  reporting the display only after the signal.
- The SIGTERM → SIGKILL escalation, when the fake lister ignores SIGTERM.
- A display that never disappears: must report unreclaimable and exit 1,
  not hang and not claim success.
- Exit-code mapping for each §5.4 case.

### 6.2 `tests/test_vdisplay_doctor_live.py` — opt-in

One `@pytest.mark.live` test running the real `probe` against the real
WindowServer, asserting the cycle completes and leaves no display behind. Adds
to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = ["live: requires a real display server; not run by default"]
addopts = "--import-mode=importlib -m 'not live'"
```

The default `pytest` run therefore stays fast and hermetic; opt in with
`pytest -m live`.

## 7. Known limitation

By §2.1 and §2.4 this tool cannot reclaim a `zombie_b`, and no tool can — the
lever does not exist in the private API or anywhere else. What it delivers is
narrower and still worth having:

- `orphan_a`, the reclaimable form of the `bugs.md` leak, is caught and cleared
  automatically and verifiably.
- `probe` establishes before a run whether the machine can actually create and
  configure a virtual display, rather than discovering it 15s into a failed
  startup.
- "Is a zombie blocking me?" becomes a reproducible five-second answer instead
  of an overnight guess.

When `bugs.md`'s shutdown hang is eventually fixed, `orphan_a` should stop
occurring in practice; `scan` and `probe` remain useful as the pre-flight gate,
and a recurrence of `orphan_a` becomes a signal that the shutdown fix
regressed.
