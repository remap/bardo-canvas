# vdisplay_doctor

Detects leaked virtual displays, reclaims the ones that can be reclaimed, and
tells you whether this machine is fit to start a broadcast.

macOS only. Standalone — it is never invoked by `run.sh`, and it never touches a
display that a live broadcast is using, so it is safe to run at any time.

```bash
python -m ndi_broadcaster.vdisplay_doctor scan     # ~0.5s, read-only
python -m ndi_broadcaster.vdisplay_doctor reap     # reclaim orphans, verify
python -m ndi_broadcaster.vdisplay_doctor probe    # ~5s health gate
```

## Why this exists

The `sck`/`virtual` capture backend creates its off-screen display through the
private `CGVirtualDisplay` API. That API's entire surface is
`initWithDescriptor:` and `applySettings:` — there is **no remove-by-display-ID
call, and no way to take ownership of someone else's display**. A virtual
display is torn down only by ARC releasing the `CGVirtualDisplay` object inside
the process that created it, which is what `vdisplay_helper`'s
`shutdownAndExit()` does on SIGTERM.

So when a helper dies without running that path, the display outlives it and
nothing can remove it. Enough of those accumulate and
`CGCompleteDisplayConfiguration` starts hanging for *every* subsequently created
virtual display, which blocks `sck`/`virtual` entirely. That failure has been
observed on this machine taking anywhere from minutes to overnight to clear on
its own, with no terminal-only remedy found.

This tool exists because "is a zombie blocking me?" used to be an overnight
guess. It is now a five-second answer.

## The verdicts

`scan` prints one row per online display. Six verdicts, evaluated in order,
first match wins:

| Verdict | Meaning | Action |
|---|---|---|
| `real` | A physical display, including the built-in one | Ignored |
| `apple_ghost` | macOS's own synthetic 640×480 ghost | Ignored |
| `active` | Ours, helper alive, serving a live broadcast | **Never touched** |
| `orphan_a` | Ours, helper alive, its launcher is gone | **Reclaimable** — `reap` clears it |
| `zombie_b` | Ours, owning process dead | **Unreclaimable** — reported only |
| `foreign_virtual` | Not ours; another tool's display (BetterDisplay, DeskPad) | **Never touched** |

Two of these deserve explanation.

**`orphan_a` is the failure this tool actually fixes.** It means the broadcaster
died or was `SIGKILL`ed without running its cleanup, but the helper process
survived it. Because the helper is still alive, `SIGTERM` still reaches its
teardown path and the display goes away. `reap` sends that signal and then
**polls the display list until the display is actually gone** — not merely until
the process exits. Those are different claims, and only the first is the one
that matters.

**`zombie_b` cannot be fixed by anything.** Its owner is dead, so there is
nothing left to signal and no API to call. `reap` reports it and prints the
escalation ladder rather than pretending otherwise.

### Two identification details worth knowing

`vdisplay_helper` stores its own PID in the display's `serialNum`, so
`CGDisplaySerialNumber()` hands back the PID that created any given display.
That is what makes the orphan/zombie split decidable rather than guessed, and it
stays correct when several broadcaster instances run at once.

A PID is only trusted when it is live *and* the basename of its command's
`argv[0]` is `vdisplay_helper`. PIDs get recycled, and without that guard a
display created by a long-dead helper could attribute to whatever unrelated
process inherited its PID — including the `swiftc` invocation this repo's own
`ensure_helper_built()` spawns. Failing to reclaim a display is recoverable;
`SIGTERM`ing a stranger is not.

## The commands

### `scan` — read-only

Lists every online display with its verdict. Signals nothing, creates nothing,
and is safe mid-broadcast.

```
$ python -m ndi_broadcaster.vdisplay_doctor scan
display    verdict          owner   name
1          real             -       Built-in Retina Display
2          real             -       LG Ultra HD (2)
3          real             -       LG Ultra HD (1)
OK  3 real, 0 active, 0 orphaned
```

### `reap` — reclaim what can be reclaimed

Scans, then for each `orphan_a`: `SIGTERM` the owner and poll until the display
leaves the display list (up to 5s). If it survives that, `SIGKILL` and re-verify.
If it still survives, or if any `zombie_b` exists, it prints the ladder and exits
`1`:

```
rung 1  SIGTERM owner  -> verify display gone   [auto, attempted]
rung 2  SIGKILL owner  -> verify display gone   [auto, attempted]
------------------------------- stop -------------------------------
rung 3  wait it out (minutes..overnight)        [advise]
rung 4  log out / log back in                   [advise]
rung 5  reboot                                  [advise]
```

Nothing above rung 2 is automated. Restarting `WindowServer` via `launchctl` is
documented elsewhere as unreliable and potentially unrecoverable without a
reboot, and restarting `com.apple.colorsync.displayservices` and the
`DisplaysExt` process were both tried live during this investigation and neither
cleared the stuck state.

### `probe` — the pre-run health gate

Creates a real throwaway virtual display, waits for it to settle, tears it down,
and verifies it actually left WindowServer — then reports per-phase timings:

```
$ python -m ndi_broadcaster.vdisplay_doctor probe
build     0.0s
create    2.7s
settle    2.0s
teardown  0.6s
OK  create/settle/teardown all succeeded
```

This answers the question that actually protects a broadcast. "Is the display
list clean?" is the weaker question, because a dirty list may be unfixable;
"can this machine still create and configure a virtual display?" is directly
observable, and a *slowing* `CGCompleteDisplayConfiguration` shows up in these
timings before it becomes a *hanging* one.

Three things about `probe` worth knowing:

- It uses the name `Layout Driver Probe Display`, never the configured broadcast
  name, so a probe display can never be confused with a real one and a leaked
  probe is instantly attributable.
- It runs the production startup path unmodified (`ensure_helper_built` →
  `start_vdisplay_helper` → `wait_for_settled_bounds`), which makes it a
  regression test for `main.swift`'s ARC-teardown fix rather than a parallel
  reimplementation of it.
- It **refuses to run** if `scan` already finds an orphan or zombie. A probe
  against an already-stuck WindowServer reports nothing trustworthy and risks
  adding to the accumulation that causes the hang. `--force` overrides.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean; for `probe`, the full cycle succeeded |
| `1` | Orphan or zombie present, or still present after `reap` |
| `2` | `probe` failed or timed out — the machine is not fit to start a run |
| `3` | Internal error (unreadable or malformed `broadcaster.yaml`, `ps` failure, Quartz fault) |

`1` and `2` are deliberately distinct so a script can tell "dirty machine" from
"machine is broken" — they call for different responses. `3` is separate again so
a tool failure is never silently read as a dirty machine.

## Recipes

**Before a long broadcast.** `probe` — if it exits `0`, the machine can still
create and tear down a virtual display.

**After a clean stop.** `scan` — confirms nothing leaked.

**After the broadcaster's shutdown hang** (see `docs/bugs.md`). Order matters:

```bash
kill -9 <launcher pid>
python -m ndi_broadcaster.vdisplay_doctor reap
python -m ndi_broadcaster.vdisplay_doctor probe
```

Reap **after** killing the launcher, not before. While the launcher is still
alive — even hung — its helper is correctly classified `active` and left alone,
because nothing can distinguish a hung launcher from a healthy broadcast by
process tree alone, and guessing wrong kills a live show. Seeing `active` in
`scan` when you believe nothing is broadcasting is itself the signal that the
launcher is still alive and is the thing to kill.

**With no config file present.** `--name` overrides the display name from
`broadcaster.yaml`, so `scan` and `reap` work standalone:

```bash
python -m ndi_broadcaster.vdisplay_doctor scan --name "Layout Driver Virtual Display"
```

## Limitations, stated plainly

- **A `zombie_b` cannot be reclaimed, by this tool or any tool.** The lever does
  not exist. What the tool gives you is certainty about *which* problem you have,
  in seconds, instead of a slow guess.
- **A hung-but-alive launcher blocks reaping**, by design — see the recipe above.
- **A `zombie_b` identified only by a `system_profiler`-recovered name is a
  weaker claim** than a serial-attributed one. `system_profiler` reports no
  display IDs, so names are paired positionally, and that fallback runs only when
  a display is invisible to `NSScreen` — precisely the zombie case. Such a name
  is treated as evidence of ownership but never as authority to signal: it can
  reach the report-only `zombie_b`, never the signalling `orphan_a`/`active`. The
  cost is a possible false FAIL, chosen deliberately over a missed detection,
  since a false FAIL stops a run rather than permitting a doomed one. `probe
  --force` is the escape hatch, and the `zombie_b` row's detail line names its
  own provenance so you can judge it.
- **Display sleep makes `CGVirtualDisplay` creation unreliable** independently of
  any of this. `caffeinate -d` for the duration of a broadcast is cheap
  insurance.

## Testing

```bash
uv run pytest                 # hermetic; live tests deselected by default
uv run pytest -m live         # creates a real virtual display and tears it down
```

The hermetic tests use synthetic display records and fake process tables, so
every verdict is covered with no display server. The `live` tests exercise real
Quartz enumeration and one full create/settle/teardown cycle — they are opt-in
because they create real virtual displays.

## Where things live

| | |
|---|---|
| Decision logic, reaper, probe, CLI | `ndi_broadcaster/vdisplay_doctor.py` |
| The only AppKit/Quartz code | `ndi_broadcaster/display_inventory.py` |
| The Swift helper it inspects | `ndi_broadcaster/vdisplay_helper/main.swift` |
| Design and rationale | `docs/superpowers/specs/2026-08-11-vdisplay-doctor-design.md` |
| The zombie-display investigation | `docs/superpowers/specs/2026-08-10-sck-capture-backend-design.md` §10 |
| Open issues | `docs/bugs.md` |

`vdisplay_doctor.py` deliberately imports no PyObjC at module scope — everything
touching AppKit/Quartz lives in `display_inventory.py` and is imported lazily, so
the decision logic stays importable and testable without a display server.
