from __future__ import annotations

import AppKit
import Quartz

from .virtual_display import DisplayInfo


def _enumerate_screens() -> list[tuple[str, DisplayInfo]]:
    """Return (localized name, resolved bounds) for every connected display.

    Factored out from find_physical_display so tests can monkeypatch this
    one function instead of the whole NSScreen/Quartz surface.
    """
    AppKit.NSApplication.sharedApplication()
    result: list[tuple[str, DisplayInfo]] = []
    for screen in AppKit.NSScreen.screens():
        display_id = screen.deviceDescription().get("NSScreenNumber")
        bounds = Quartz.CGDisplayBounds(display_id)
        result.append(
            (
                screen.localizedName(),
                DisplayInfo(
                    display_id=int(display_id),
                    x=int(bounds.origin.x),
                    y=int(bounds.origin.y),
                    width=int(bounds.size.width),
                    height=int(bounds.size.height),
                ),
            )
        )
    return result


def find_physical_display(name_substring: str) -> DisplayInfo:
    """Match a connected display by NSScreen.localizedName substring
    (case-insensitive -- the same convention layout_server/audio.py's
    match_device_by_name already uses for audio devices) and return its
    real CGDisplayBounds.

    Raises ValueError naming every connected display's actual name if no
    match is found, so an operator can copy the right value directly from
    the error. Returning real bounds (not just validating the name) matters:
    with multiple displays connected, launching Chromium with no explicit
    --window-position risks it landing on whichever display the OS default
    picks, which could be too small and silently reproduce the original
    window-clamping bug this backend exists to avoid.
    """
    screens = _enumerate_screens()
    lowered = name_substring.lower()
    for name, info in screens:
        if lowered in name.lower():
            return info
    known_names = sorted(name for name, _ in screens)
    raise ValueError(
        f"no connected display matched {name_substring!r}; "
        f"connected display names: {known_names}"
    )
