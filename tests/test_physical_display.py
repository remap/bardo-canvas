import pytest

from ndi_broadcaster.physical_display import find_physical_display
from ndi_broadcaster.virtual_display import DisplayInfo


def test_find_physical_display_matches_by_case_insensitive_substring(monkeypatch):
    target = DisplayInfo(display_id=3, x=1920, y=0, width=3840, height=2160)
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [
            ("Built-in Retina Display", DisplayInfo(1, 0, 0, 1728, 1117)),
            ("LG UltraFine 4K", target),
        ],
    )

    assert find_physical_display("ultrafine") == target


def test_find_physical_display_raises_listing_known_names(monkeypatch):
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [("Built-in Retina Display", DisplayInfo(1, 0, 0, 1728, 1117))],
    )

    with pytest.raises(ValueError, match="Built-in Retina Display"):
        find_physical_display("Nonexistent Display")
