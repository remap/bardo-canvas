from __future__ import annotations

"""
macOS-only capture backend using ScreenCaptureKit via PyObjC, matching a
window by title substring. Opt-in (config/broadcaster.yaml: capture_backend: sck) —
requires Screen Recording permission and a headed display. There is no way to
unit test this without a real macOS window and granted permission; verify
manually per the steps below.
"""

import numpy as np


class ScreenCaptureKitUnavailableError(RuntimeError):
    pass


class SckCapture:
    def __init__(self, window_title_hint: str) -> None:
        try:
            import Quartz
            import ScreenCaptureKit
        except ImportError as exc:
            raise ScreenCaptureKitUnavailableError(
                "ScreenCaptureKit backend requires macOS + pyobjc-framework-ScreenCaptureKit"
            ) from exc

        self._quartz = Quartz
        self._sck = ScreenCaptureKit
        self._window_title_hint = window_title_hint

    def latest_frame(self) -> np.ndarray:
        raise NotImplementedError(
            "Wire up SCStream/SCStreamOutput per karaoke-test's sck_capture.py, matching "
            f"a window whose title contains {self._window_title_hint!r}, and convert the "
            "delivered CVPixelBuffer (BGRA) to an RGBA numpy array here."
        )
