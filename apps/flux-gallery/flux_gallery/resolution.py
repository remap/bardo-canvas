from __future__ import annotations


def _round_to_16(value: int) -> int:
    return ((value + 8) // 16) * 16


def compute_target_resolution(screen_width: int, screen_height: int) -> tuple[int, int]:
    return _round_to_16(screen_width), _round_to_16(screen_height)
