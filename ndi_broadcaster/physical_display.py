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


def find_physical_display(name_substring: str, expected_width: int, expected_height: int) -> DisplayInfo:
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

    Also requires the matched display's bounds to equal
    (expected_width, expected_height) exactly. Virtual-display mode gets
    this for free (wait_for_settled_bounds polls until the virtual display's
    bounds match config.width/height), but nothing enforces it for a real
    display -- CGDisplayBounds reports *points*, not pixels, so a HiDPI
    display in its default scaled mode commonly reports a smaller point
    resolution than config.width/height expects. Launcher.py's Chrome-window
    sizing and SckCapture's crop math both assume the captured window is
    exactly config.width x config.height; a silent mismatch there means a
    silent resolution downgrade and the crop math cutting the wrong number
    of rows off the top of every frame, rather than a loud failure here.
    """
    screens = _enumerate_screens()
    lowered = name_substring.lower()
    for name, info in screens:
        if lowered in name.lower():
            if info.width != expected_width or info.height != expected_height:
                raise ValueError(
                    f"display {name!r} reports {info.width}x{info.height} points, "
                    f"but broadcaster.yaml configures width={expected_width} "
                    f"height={expected_height}. CGDisplayBounds reports points, not "
                    "pixels -- a HiDPI/Retina display's point resolution is often "
                    "smaller than its native pixel resolution. Set width/height in "
                    "broadcaster.yaml to match this display's reported point "
                    "resolution exactly."
                )
            return info
    known_names = sorted(name for name, _ in screens)
    raise ValueError(
        f"no connected display matched {name_substring!r}; "
        f"connected display names: {known_names}"
    )
