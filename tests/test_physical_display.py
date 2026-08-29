import pytest

from ndi_broadcaster.physical_display import (
    find_display_by_name,
    find_physical_display,
    main_screen,
)
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

    assert find_physical_display("ultrafine", 3840, 2160) == target


def test_find_physical_display_raises_listing_known_names(monkeypatch):
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [("Built-in Retina Display", DisplayInfo(1, 0, 0, 1728, 1117))],
    )

    with pytest.raises(ValueError, match="Built-in Retina Display"):
        find_physical_display("Nonexistent Display", 3840, 2160)


def test_find_physical_display_raises_on_resolution_mismatch(monkeypatch):
    # Live gotcha this guards against: CGDisplayBounds reports points, not
    # pixels -- a HiDPI display's default-scaled point resolution (e.g. a 4K
    # display reporting 1920x1080 points) silently mismatches a
    # broadcaster.yaml configured for 3840x2160, which would otherwise
    # produce a half-resolution capture and misaligned Chrome-toolbar crop.
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [("LG UltraFine 4K", DisplayInfo(3, 1920, 0, 1920, 1080))],
    )

    with pytest.raises(ValueError, match="1920x1080.*3840.*2160"):
        find_physical_display("ultrafine", 3840, 2160)


def test_find_display_by_name_matches_case_insensitive_substring(monkeypatch):
    first = DisplayInfo(1, 0, 0, 1728, 1117)
    second = DisplayInfo(3, 1920, 0, 3840, 2160)
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [("Built-in Retina Display", first), ("LG UltraFine 4K", second)],
    )

    assert find_display_by_name("retina") == first
    assert find_display_by_name("UltraFine") == second


def test_find_display_by_name_raises_listing_known_names_when_no_match(monkeypatch):
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [("Built-in Retina Display", DisplayInfo(1, 0, 0, 1728, 1117))],
    )

    with pytest.raises(ValueError, match="Built-in Retina Display"):
        find_display_by_name("Nonexistent Display")


def test_main_screen_returns_the_first_enumerated_display(monkeypatch):
    first = DisplayInfo(1, 0, 0, 1728, 1117)
    second = DisplayInfo(3, 1920, 0, 3840, 2160)
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [("Built-in Retina Display", first), ("LG UltraFine 4K", second)],
    )

    assert main_screen() == first
