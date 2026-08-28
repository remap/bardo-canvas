from pathlib import Path

import pytest

from layout_server.config import (
    CanvasConfig,
    LayoutOffset,
    OverlappingScreensError,
    ScreenGrid,
    compute_rect,
    load_layout_config,
)

SCREENS_YAML = Path(__file__).resolve().parent.parent / "config" / "screens.yaml"

EXPECTED_RECTS = {
    "F": (0, 0, 1800, 1400),
    "B": (1800, 0, 1200, 600),
    "C": (1800, 600, 1200, 600),
    "D": (1800, 1200, 1600, 400),
    "A": (1800, 1600, 1600, 400),
    "E": (200, 1400, 1600, 400),
}


def test_load_layout_config_computes_expected_rects():
    config = load_layout_config(SCREENS_YAML)

    assert config.canvas == CanvasConfig(width=3840, height=2160, fps=30)
    assert len(config.screens) == 6

    for screen in config.screens:
        expected_x, expected_y, expected_w, expected_h = EXPECTED_RECTS[screen.id]
        assert screen.rect.x == expected_x
        assert screen.rect.y == expected_y
        assert screen.rect.width == expected_w
        assert screen.rect.height == expected_h


def test_screen_by_id_returns_none_for_unknown_id():
    config = load_layout_config(SCREENS_YAML)
    assert config.screen_by_id("Z") is None
    assert config.screen_by_id("F") is not None


def test_compute_rect_applies_offset_and_module_size():
    grid = ScreenGrid(col=2, row=3, cols=4, rows=5)
    rect = compute_rect(grid, module_size=200, offset=LayoutOffset(x=10, y=20))
    assert rect.x == 10 + 2 * 200
    assert rect.y == 20 + 3 * 200
    assert rect.width == 4 * 200
    assert rect.height == 5 * 200


def test_overlapping_screens_are_rejected(tmp_path):
    overlapping_yaml = tmp_path / "overlapping.yaml"
    overlapping_yaml.write_text(
        """
canvas: {width: 3840, height: 2160, fps: 30}
module_size: 200
layout_offset: {x: 0, y: 0}
screens:
  - id: "1"
    name: "One"
    grid: {col: 0, row: 0, cols: 4, rows: 4}
  - id: "2"
    name: "Two"
    grid: {col: 2, row: 2, cols: 4, rows: 4}
"""
    )
    with pytest.raises(OverlappingScreensError):
        load_layout_config(overlapping_yaml)
