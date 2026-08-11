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
