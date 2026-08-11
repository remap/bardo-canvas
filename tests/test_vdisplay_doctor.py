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
