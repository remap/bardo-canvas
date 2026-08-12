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
