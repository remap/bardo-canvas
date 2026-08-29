from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class BroadcasterConfig(BaseModel):
    target_url: str = "https://localhost:8443/"
    capture_backend: Literal["cdp", "sck"] = "cdp"
    sck_display_mode: Literal["virtual", "physical"] | None = None
    sck_virtual_display_name: str = "Layout Driver Virtual Display"
    sck_physical_display_name: str | None = None
    ndi_source_name: str = "Layout Driver"
    width: int = 3840
    height: int = 2160
    fps: int = 30
    healthz_timeout_seconds: float = 30.0
    timecode_enabled: bool = True
    timecode_position: Literal["top", "bottom"] = "top"
    # Opt-in only: the sck backend's broadcast Chrome runs a persistent
    # profile no other browser instance can share BroadcastChannel with (or
    # any other same-profile web API) -- an app whose own control surface
    # needs that, e.g. yt-matrix's /layout-control, sets this to open as a
    # second window in that SAME profile instead of asking an operator to
    # find it another way. None (the default) opens nothing. cdp backend
    # ignores this entirely (headless, no window to place).
    control_window_url: str | None = None
    # Name-based, not index-based -- NSScreen.screens()[0] is only "the
    # screen with the menu bar" at the moment of the call, not a stable
    # physical position (confirmed live: it did not reliably resolve to the
    # intended monitor). None falls back to that same unreliable "main
    # screen" reading; set this (substring match, same convention as
    # sck_physical_display_name) for a display that's actually predictable.
    control_display_name: str | None = None
    control_window_width: int = 1200
    control_window_height: int = 600


def load_broadcaster_config(path: Path) -> BroadcasterConfig:
    raw = yaml.safe_load(path.read_text())
    return BroadcasterConfig(**raw)
