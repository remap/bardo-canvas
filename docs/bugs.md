# Known issues

Open bugs and unresolved investigations, kept separate from the design specs
in `docs/superpowers/` because those are point-in-time design records —
this is a living list. When an entry here gets resolved, move the writeup
into the relevant design spec's addenda (matching the existing pattern in
`docs/superpowers/specs/2026-08-10-sck-capture-backend-design.md`) and
delete it from here.

## Open

### Broadcaster shutdown can hang indefinitely on `sck` (and likely `cdp`)

**Status:** partially fixed, not resolved. Reproducible.

**Symptom:** stopping the broadcaster (`SIGTERM` to `ndi_broadcaster.launcher`,
including via `run.sh`'s own cleanup or a plain `kill`) sometimes never
completes. The process, `vdisplay_helper`, and the Playwright Node.js
driver subprocess all stay alive well past every timeout in the shutdown
path. Confirmed reproducible on the `sck` backend; not yet tested
specifically on `cdp`, but both loops share the same shutdown structure and
`_capture_loop` (cdp) has the identical `async_playwright()` lifecycle, so
it's likely equally exposed.

**What's confirmed:**

1. `ndi_broadcaster/launcher.py` previously had no `SIGTERM` handler at all
   (Python only auto-converts `SIGINT` to `KeyboardInterrupt`, not
   `SIGTERM`). Fixed in `3c59e2e`: `run()` now installs a handler that
   raises `KeyboardInterrupt`, reusing the existing shutdown path. Verified
   this part works — after `SIGTERM`, the sender thread's periodic logging
   stops promptly, confirming `stop_event` is actually being set.

2. Past that point, shutdown can still stall. Live `sample`-based stack
   traces (twice, in two separate incidents) show:
   - The main thread parked in asyncio's own event-loop wait
     (`select_kqueue_control_impl` / `kevent`), i.e. the loop itself is
     alive and not deadlocked at the Python level.
   - A **separate thread** blocked in `os_waitpid` → `__wait4`, a real,
     uninterruptible OS-level blocking call.
   - `vdisplay_helper` and the Playwright Node.js driver process
     (`.venv/.../playwright/driver/node .../cli.js run-driver`) both still
     alive as direct children of the launcher process, well past when they
     should have been torn down.

3. Root-caused the `__wait4` thread: Python's default asyncio child-watcher
   implementation on this platform manages subprocesses (including the one
   `playwright.chromium.launch()` and `async_playwright()`'s own driver
   process create via `asyncio.create_subprocess_exec`) using a **background
   thread per child that calls a blocking `os.waitpid()`** — this is normal,
   expected asyncio internals, not a bug in Playwright or this repo. That
   thread cannot be interrupted by cancelling an `await` from the outside;
   only the child process actually exiting unblocks it.

4. Attempted fix (`<pending commit>`): wrapped both `browser.close()` and
   `playwright.stop()` (Playwright's own `async_playwright()` context-manager
   exit — an alias for `Connection.__aexit__` → `stop_async()` →
   `PipeTransport.wait_until_stopped()` → an internal
   `await self._proc.communicate()`) in `asyncio.wait_for(..., timeout=10)`,
   reasoning that even if the underlying OS wait can't be interrupted, the
   *awaiting coroutine* should still be cancellable, letting Python-level
   control flow proceed to the outer `finally` (which terminates
   `vdisplay_helper`) instead of hanging forever.

   **This did not work.** Retested live after the fix: the exact same hang
   reproduced, with `vdisplay_helper` still alive as a child process,
   meaning code execution never got past the wrapped `playwright.stop()`
   call at all — well beyond the 10s timeout, and beyond the ~30s worst-case
   cumulative bound across all the sequential timeouts in the shutdown
   chain. `asyncio.wait_for()`'s cancellation is not actually unblocking
   this await, for a reason not yet understood.

**What's NOT yet understood:**

- Why `asyncio.wait_for()` isn't timing out the wrapped coroutine. Possible
  leads not yet checked: whether `Connection.stop_async()` or
  `asyncio.subprocess.Process.communicate()` shield themselves from
  cancellation somewhere in their implementation; whether the specific
  event loop / child-watcher combination in use here (macOS,
  `asyncio.run()` called fresh per capture-loop invocation) has a known
  limitation around cancelling subprocess-communicate awaits; whether the
  timeout constant is actually being read correctly at the call site
  (a unit test confirms `_shutdown_with_timeout` works in isolation against
  a plain `asyncio.sleep()`-based hang, so the primitive itself is sound —
  the gap must be specific to interacting with the real Playwright/asyncio
  subprocess machinery, not the wrapper logic itself).
- Could not get a live async-aware Python stack trace of the actual stuck
  coroutine chain to confirm exactly which `await` is holding — `py-spy`
  requires root on this machine (`sudo`) and non-interactive elevation
  wasn't available at investigation time. A `sudo py-spy dump --pid <pid>`
  or `--native` capture the next time this reproduces would likely resolve
  the open question directly.
- Whether the same hang independently occurs in `_capture_loop`'s (`cdp`)
  shutdown path, which shares the identical `async_playwright()` lifecycle
  and now has the identical `_shutdown_with_timeout` wrapping, but hasn't
  been separately live-tested for this specific failure mode.

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

**Where the code lives:** `ndi_broadcaster/launcher.py`,
`_shutdown_with_timeout()`, and its two call sites in `_capture_loop_sck`
and `_capture_loop`.

## Resolved (see design spec addenda for full writeups)

- **sck backend zombie virtual displays / intermittent
  `CGCompleteDisplayConfiguration` hangs at startup** — see
  `docs/superpowers/specs/2026-08-10-sck-capture-backend-design.md` §10.
  Two real bugs fixed (leak-on-shutdown, missing color primaries); one
  failure mode still has no known terminal-only remedy (waiting it out —
  once overnight — was the only thing observed to clear it). Distinct from
  the shutdown-hang bug above: that one is about *starting* a new virtual
  display; this repo's bug above is about the launcher process failing to
  *exit* at all.
- **`enableImageMode()` positioning bug / duplicate draw paths** — see
  `docs/superpowers/specs/2026-08-08-layout-driver-framework-design.md` §10.
- **`start_vdisplay_helper` leaking a live virtual display on malformed or
  incomplete startup JSON** — see
  `docs/superpowers/specs/2026-08-10-sck-capture-backend-design.md` §10 (the
  third entry). Found and fixed (`b1c5971`) while building
  `vdisplay_doctor`'s `probe` subcommand; distinct from the two `main.swift`
  bugs earlier in that same section — this one was in the Python-side
  `ndi_broadcaster/virtual_display.py`.
