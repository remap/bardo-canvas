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


def load_broadcaster_config(path: Path) -> BroadcasterConfig:
    raw = yaml.safe_load(path.read_text())
    return BroadcasterConfig(**raw)
