from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class ScreenGrid(BaseModel):
    col: int
    row: int
    cols: int
    rows: int


class ScreenRect(BaseModel):
    x: int
    y: int
    width: int
    height: int


class ScreenConfig(BaseModel):
    id: str
    name: str
    grid: ScreenGrid
    rect: ScreenRect


class CanvasConfig(BaseModel):
    width: int
    height: int
    fps: int


class LayoutOffset(BaseModel):
    x: int
    y: int


class LayoutConfig(BaseModel):
    canvas: CanvasConfig
    module_size: int
    layout_offset: LayoutOffset
    screens: list[ScreenConfig]

    def screen_by_id(self, screen_id: str) -> ScreenConfig | None:
        return next((screen for screen in self.screens if screen.id == screen_id), None)


class OverlappingScreensError(ValueError):
    pass


def compute_rect(grid: ScreenGrid, module_size: int, offset: LayoutOffset) -> ScreenRect:
    return ScreenRect(
        x=offset.x + grid.col * module_size,
        y=offset.y + grid.row * module_size,
        width=grid.cols * module_size,
        height=grid.rows * module_size,
    )


def _rects_overlap(a: ScreenRect, b: ScreenRect) -> bool:
    return (
        a.x < b.x + b.width
        and b.x < a.x + a.width
        and a.y < b.y + b.height
        and b.y < a.y + a.height
    )


def load_layout_config(path: Path) -> LayoutConfig:
    raw = yaml.safe_load(path.read_text())
    offset = LayoutOffset(**raw["layout_offset"])
    module_size = raw["module_size"]
    canvas = CanvasConfig(**raw["canvas"])

    screens: list[ScreenConfig] = []
    for entry in raw["screens"]:
        grid = ScreenGrid(**entry["grid"])
        rect = compute_rect(grid, module_size, offset)
        screens.append(ScreenConfig(id=entry["id"], name=entry["name"], grid=grid, rect=rect))

    for i, a in enumerate(screens):
        for b in screens[i + 1 :]:
            if _rects_overlap(a.rect, b.rect):
                raise OverlappingScreensError(f"Screens {a.id!r} and {b.id!r} overlap")

    return LayoutConfig(
        canvas=canvas, module_size=module_size, layout_offset=offset, screens=screens
    )
