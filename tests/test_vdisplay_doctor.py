from pathlib import Path

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
    ProcessRecord,
    classify,
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


OURS = "Layout Driver Virtual Display"
HELPER_CMD = "/repo/ndi_broadcaster/vdisplay_helper/vdisplay_helper 3840 2160 " + OURS
LAUNCHER_CMD = "/opt/homebrew/.../MacOS/Python -m ndi_broadcaster.launcher"


def _display(**overrides) -> DisplayRecord:
    """A plausible non-builtin, NSScreen-visible display; override per test."""
    base = {
        "display_id": 69732865,
        "vendor": 0x1234,
        "model": 0x5678,
        "serial": 4903,
        "unit_number": 3,
        "is_builtin": False,
        "is_active": True,
        "is_asleep": False,
        "bounds": (6116, 0, 3840, 2160),
        "name": OURS,
        "in_nsscreen": True,
    }
    base.update(overrides)
    return DisplayRecord(**base)


def _verdicts(displays, processes, name=OURS):
    return [c.verdict for c in classify(displays, processes, name)]


def test_builtin_real_values_are_not_mistaken_for_a_ghost():
    # spec 2.3: the Apple DTS forum thread recommends filtering on
    # CGDisplayUnitNumber != 0, but this machine's built-in display genuinely
    # reports unit_number == 0. These are its real measured values. A bare
    # `unit != 0` filter would call the laptop's own screen a ghost. The full
    # ghost conjunction (unit == 0 AND vendor AND model AND serial ALL matching)
    # protects against this.
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


def test_is_builtin_is_checked_before_the_ghost_test():
    # Synthetic on purpose: a real builtin display never reports the ghost
    # signature. This record satisfies is_builtin AND the full ghost
    # conjunction at once, so the verdict is decided by branch order alone --
    # which is what makes it the only test that would fail if the two
    # branches in classify() were swapped.
    conflicting = _display(
        display_id=1,
        is_builtin=True,
        unit_number=0,
        vendor=GHOST_VENDOR,
        model=GHOST_MODEL,
        serial=0,
        name="Built-in Retina Display",
    )

    assert _verdicts([conflicting], {}) == [VERDICT_REAL]


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
    overrides = {
        "unit_number": 0,
        "vendor": GHOST_VENDOR,
        "model": GHOST_MODEL,
        "serial": 0,
        "name": None,
        "in_nsscreen": False,
    }
    overrides[field] = value
    nearly = _display(**overrides)

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


from ndi_broadcaster.vdisplay_doctor import probe


def test_probe_terminates_the_helper_when_a_phase_fails_so_no_probe_display_leaks(monkeypatch):
    # The failure path is the one that matters, and SIGTERM is the only signal
    # that helps: spec 5.2 -- "'the process exited' and 'the display was torn
    # down' are different claims... Verifying the process is not verifying the
    # fix." By the time `settle` fails, `create` has already returned a real
    # displayID, so the display is definitely live; SIGKILL runs no handler in
    # main.swift, so killing here would leave the health gate manufacturing a
    # zombie_b every time it fails. Fakes stand in for the real startup path,
    # so this runs with no display server.
    import ndi_broadcaster.virtual_display as vd
    from ndi_broadcaster.virtual_display import DisplayInfo

    class _FakeProc:
        def __init__(self):
            self.killed = False
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return 0

    fake_proc = _FakeProc()
    monkeypatch.setattr(vd, "ensure_helper_built", lambda helper_dir: Path("/fake/helper"))
    monkeypatch.setattr(
        vd,
        "start_vdisplay_helper",
        lambda *a, **k: (fake_proc, DisplayInfo(display_id=999, x=0, y=0, width=1920, height=1080)),
    )

    def boom(*_args, **_kwargs):
        raise TimeoutError("display 999 did not settle")

    monkeypatch.setattr(vd, "wait_for_settled_bounds", boom)

    result = probe(1920, 1080, helper_dir=Path("/fake"))

    assert not result.ok
    assert result.failure_phase == "settle"
    assert fake_proc.terminated, "a failed probe must tear its display down, not orphan it"
    assert not fake_proc.killed, "SIGKILL runs no handler, so it would leak the probe display"


def test_probe_reports_the_phase_that_failed_when_the_helper_never_starts(monkeypatch):
    import ndi_broadcaster.virtual_display as vd

    monkeypatch.setattr(vd, "ensure_helper_built", lambda helper_dir: Path("/fake/helper"))

    def never_starts(*_args, **_kwargs):
        raise TimeoutError("vdisplay_helper did not report its startup status within 15.0s")

    monkeypatch.setattr(vd, "start_vdisplay_helper", never_starts)

    result = probe(1920, 1080, helper_dir=Path("/fake"))

    assert not result.ok
    assert result.failure_phase == "create"
    assert "did not report" in result.message


def test_probe_returns_a_failure_result_instead_of_raising_when_the_teardown_poll_errors(
    monkeypatch,
):
    # Task 6's CLI depends on probe() always returning a ProbeResult rather
    # than raising -- a transient Quartz/CoreGraphics error while polling for
    # teardown must not become an unhandled traceback.
    import ndi_broadcaster.display_inventory as di
    import ndi_broadcaster.virtual_display as vd
    from ndi_broadcaster.virtual_display import DisplayInfo

    class _FakeProc:
        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    fake_proc = _FakeProc()
    monkeypatch.setattr(vd, "ensure_helper_built", lambda helper_dir: Path("/fake/helper"))
    monkeypatch.setattr(
        vd,
        "start_vdisplay_helper",
        lambda *a, **k: (fake_proc, DisplayInfo(display_id=999, x=0, y=0, width=1920, height=1080)),
    )
    monkeypatch.setattr(vd, "wait_for_settled_bounds", lambda *a, **k: None)

    def boom():
        raise RuntimeError("CoreGraphics call failed")

    monkeypatch.setattr(di, "online_display_ids", boom)

    result = probe(1920, 1080, helper_dir=Path("/fake"))

    assert not result.ok
    assert result.failure_phase == "teardown"
    assert "CoreGraphics call failed" in result.message


from ndi_broadcaster.vdisplay_doctor import (
    EXIT_CLEAN,
    EXIT_DIRTY,
    EXIT_ERROR,
    EXIT_PROBE_FAILED,
    ProbeResult,
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
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", dict)

    assert main(["scan", "--name", OURS]) == EXIT_CLEAN


def test_scan_exits_dirty_when_a_zombie_is_present(monkeypatch):
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor._collect_displays",
        lambda: [_display(serial=4110)],
    )
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", dict)

    assert main(["scan", "--name", OURS]) == EXIT_DIRTY


def test_reap_exits_dirty_when_a_zombie_is_present(monkeypatch):
    # A zombie has no owner to signal, so this exercises the classifications
    # half of main()'s `remaining` union (zombie_b), not reap_orphans's own
    # outcome -- see test_reap_exits_dirty_when_an_orphan_cannot_be_reclaimed
    # for the other half.
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor._collect_displays",
        lambda: [_display(serial=4110)],
    )
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", dict)
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor._online_display_ids", lambda: {69732865})

    assert main(["reap", "--name", OURS]) == EXIT_DIRTY


def test_reap_exits_dirty_when_an_orphan_cannot_be_reclaimed(monkeypatch, capsys):
    # Unlike the zombie test above, this display classifies as orphan_a, so
    # reap_orphans's real SIGTERM/SIGKILL escalation runs. The owner never
    # complies (list_display_ids never drops the id), so the outcome must be
    # UNRECLAIMABLE and main() must report EXIT_DIRTY from the *results* half
    # of its `remaining` union -- the half the old zombie-only test never
    # touched. Wraps the real reap_orphans with tighter timeouts so the test
    # stays fast without stubbing away the logic under test; signal_process is
    # stubbed only so no real OS signal is sent to a possibly-reused pid.
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor._collect_displays",
        lambda: [_display(serial=4903)],
    )
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor.read_process_table",
        lambda: {4903: ProcessRecord(pid=4903, ppid=1, command=HELPER_CMD)},
    )
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.signal_process", lambda pid, sig: None)
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor._online_display_ids", lambda: {69732865})

    real_reap_orphans = reap_orphans

    def fast_reap_orphans(classifications, *, signal_process, list_display_ids):
        return real_reap_orphans(
            classifications,
            signal_process=signal_process,
            list_display_ids=list_display_ids,
            verify_timeout_s=0.05,
            poll_interval_s=0.01,
            sleep=lambda _seconds: None,
        )

    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.reap_orphans", fast_reap_orphans)

    exit_code = main(["reap", "--name", OURS])
    out = capsys.readouterr().out

    assert exit_code == EXIT_DIRTY
    # The outcome itself must be reflected, not just the exit code, so this
    # cannot pass if reap_orphans is ever bypassed or its result ignored.
    assert UNRECLAIMABLE in out
    assert "69732865" in out


def test_probe_refuses_to_run_on_a_dirty_machine(monkeypatch):
    # A probe against an already-stuck WindowServer reports nothing
    # trustworthy, and risks adding to the accumulation that causes the hang.
    called = []
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor._collect_displays",
        lambda: [_display(serial=4110)],
    )
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", dict)
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
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", dict)
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor.probe",
        lambda *a, **k: ProbeResult(ok=False, timings={}, failure_phase="settle", message="nope"),
    )

    assert main(["probe", "--name", OURS, "--force"]) == EXIT_PROBE_FAILED


def test_probe_exit_code_distinguishes_broken_from_dirty(monkeypatch):
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor._collect_displays", list)
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", dict)
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor.probe",
        lambda *a, **k: ProbeResult(
            ok=True, timings={"build": 0.0}, failure_phase=None, message="ok"
        ),
    )

    assert main(["probe", "--name", OURS]) == EXIT_CLEAN


def _healthy_machine(monkeypatch) -> None:
    monkeypatch.setattr(
        "ndi_broadcaster.vdisplay_doctor._collect_displays",
        lambda: [_display(display_id=1, is_builtin=True, name="Built-in Retina Display")],
    )
    monkeypatch.setattr("ndi_broadcaster.vdisplay_doctor.read_process_table", dict)


def test_probe_exits_with_an_internal_error_when_broadcaster_yaml_is_missing(monkeypatch, tmp_path):
    # probe needs config.width/config.height even though --name bypassed
    # config-driven name resolution. Before the fix this load happened
    # unguarded, so a missing file raised uncaught and Python's default exit
    # code (1) was indistinguishable from EXIT_DIRTY.
    _healthy_machine(monkeypatch)
    monkeypatch.setenv("BROADCASTER_YAML", str(tmp_path / "does-not-exist.yaml"))

    assert main(["probe", "--name", OURS]) == EXIT_ERROR


def test_probe_exits_with_an_internal_error_when_broadcaster_yaml_is_malformed(
    monkeypatch, tmp_path
):
    # yaml.YAMLError is not a ValueError subclass, so a bare
    # `except (OSError, ValueError)` guard lets a syntax error in the YAML
    # escape uncaught -- also colliding with EXIT_DIRTY's exit code.
    _healthy_machine(monkeypatch)
    bad_yaml = tmp_path / "broken.yaml"
    bad_yaml.write_text("key: [unterminated\n")
    monkeypatch.setenv("BROADCASTER_YAML", str(bad_yaml))

    assert main(["probe", "--name", OURS]) == EXIT_ERROR


def test_scan_with_name_succeeds_even_when_broadcaster_yaml_is_missing(monkeypatch, tmp_path):
    # Pins the property that must not regress: --name alone is enough for
    # scan/reap to run with no config file present at all, because the config
    # load is only fatal when the value it would supply is actually needed.
    _healthy_machine(monkeypatch)
    monkeypatch.setenv("BROADCASTER_YAML", str(tmp_path / "does-not-exist.yaml"))

    assert main(["scan", "--name", OURS]) == EXIT_CLEAN
