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


def test_find_physical_display_accepts_a_display_taller_than_configured(monkeypatch):
    # The relaxed case this fix exists for: config.height is deliberately set
    # smaller than the real display so there's room for Chrome's own window
    # chrome (see _resolve_sck_crop_geometry in launcher.py) -- a taller real
    # display must match, not be rejected as a mismatch.
    target = DisplayInfo(display_id=4, x=1728, y=0, width=3840, height=2160)
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [("SyncMaster", target)],
    )

    assert find_physical_display("SyncMaster", 3840, 2128) == target


def test_find_physical_display_raises_when_shorter_than_configured(monkeypatch):
    # A display genuinely shorter than the configured content height can
    # never work regardless of chrome -- there's nothing left to crop from.
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [("SyncMaster", DisplayInfo(4, 1728, 0, 3840, 2100))],
    )

    with pytest.raises(ValueError, match="3840x2100.*3840.*2160"):
        find_physical_display("SyncMaster", 3840, 2160)


def test_find_physical_display_raises_on_width_mismatch_even_when_tall_enough(monkeypatch):
    # Unlike height, width has no crop mechanism anywhere downstream -- it
    # stays a hard equality check even when the display is plenty tall.
    monkeypatch.setattr(
        "ndi_broadcaster.physical_display._enumerate_screens",
        lambda: [("SyncMaster", DisplayInfo(4, 1728, 0, 1920, 2160))],
    )

    with pytest.raises(ValueError, match="1920x2160.*3840.*2128"):
        find_physical_display("SyncMaster", 3840, 2128)


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
