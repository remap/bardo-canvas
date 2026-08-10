from __future__ import annotations

"""
macOS-only capture backend using ScreenCaptureKit via PyObjC, matching a
window by title substring. Selected via config/broadcaster.yaml:
capture_backend: sck. Requires Screen Recording permission and a headed
display (real or virtual -- see ndi_broadcaster/virtual_display.py and
ndi_broadcaster/physical_display.py for how the display is resolved before
this class is ever constructed).

Ported from a validated proof-of-concept spike (framework spec Sec 3.4a):
SCStream delivered continuously changing frames at a sustained ~29.3fps
average with zero repeated frames over a 5-minute run, independent of
Playwright's own driver process -- the actual bottleneck this backend
removes from the capture path.
"""

import logging
import threading
import time
from collections.abc import Callable

import numpy as np

logger = logging.getLogger(__name__)

# 'BGRA' as a FourCC integer -- matches DeskPad's CGDisplayStream usage and
# the validated spike; ScreenCaptureKit delivers pixel buffers in this format.
_BGRA_PIXEL_FORMAT = 1111970369


class ScreenCaptureKitUnavailableError(RuntimeError):
    pass


try:
    import objc
    from Foundation import NSObject

    import CoreMedia as CM
    import Quartz
    import ScreenCaptureKit as SCK
except ImportError as exc:  # pragma: no cover - exercised only off-macOS
    raise ScreenCaptureKitUnavailableError(
        "capture_backend: sck requires macOS + pyobjc-framework-ScreenCaptureKit"
    ) from exc


def bgra_buffer_to_rgba_bytes(raw: bytes, width: int, height: int, bytes_per_row: int) -> bytes:
    """Convert a raw BGRA CVPixelBuffer read (with possible row padding) into
    tightly packed RGBA bytes of exactly (height, width, 4).

    Pure function, no PyObjC types -- testable with synthetic byte buffers.
    ScreenCaptureKit pixel buffers are frequently padded to a stride wider
    than width * 4 bytes; slicing by bytes_per_row before reshaping strips
    that padding rather than corrupting the image with it.
    """
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(height, bytes_per_row // 4, 4)
    arr = arr[:, :width, [2, 1, 0, 3]]  # BGRA -> RGBA
    return np.ascontiguousarray(arr).tobytes()


def _get_shareable_content(timeout_s: float = 10.0):
    result: dict = {}
    done = threading.Event()

    def handler(content, error):
        result["content"] = content
        result["error"] = error
        done.set()

    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(handler)
    if not done.wait(timeout_s):
        raise TimeoutError("SCShareableContent did not respond")
    if result["error"] is not None:
        raise RuntimeError(f"SCShareableContent error: {result['error']}")
    return result["content"]


def _find_target_window(content, title_hint: str):
    matches = [w for w in content.windows() if title_hint in (w.title() or "")]
    if not matches:
        found_titles = sorted({w.title() for w in content.windows() if w.title()})
        raise ValueError(
            f"no window found with title containing {title_hint!r}; "
            f"window titles found: {found_titles}"
        )
    return matches[0]


class _StreamOutput(NSObject):
    def initWithOnFrame_(self, on_frame: Callable[[bytes], None]):
        self = objc.super(_StreamOutput, self).init()
        if self is None:
            return None
        self._on_frame = on_frame
        self._frame_count = 0
        self._lock = threading.Lock()
        self._last_log = time.monotonic()
        return self

    def stream_didOutputSampleBuffer_ofType_(self, stream, sampleBuffer, outputType):
        if not CM.CMSampleBufferIsValid(sampleBuffer):
            return
        pixel_buffer = CM.CMSampleBufferGetImageBuffer(sampleBuffer)
        if pixel_buffer is None:
            return

        Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)
        try:
            width = Quartz.CVPixelBufferGetWidth(pixel_buffer)
            height = Quartz.CVPixelBufferGetHeight(pixel_buffer)
            bytes_per_row = Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer)
            base_address = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
            raw = bytes(base_address.as_buffer(bytes_per_row * height))
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)

        try:
            self._on_frame(bgra_buffer_to_rgba_bytes(raw, width, height, bytes_per_row))
        except Exception:
            logger.exception("SCK frame callback raised; dropping this frame")

        with self._lock:
            self._frame_count += 1
            now = time.monotonic()
            if now - self._last_log >= 5.0:
                logger.info(
                    "SCK capture: %.1f fps in the last %.1fs",
                    self._frame_count / (now - self._last_log),
                    now - self._last_log,
                )
                self._frame_count = 0
                self._last_log = now


class _StreamDelegate(NSObject):
    def stream_didStopWithError_(self, stream, error):
        logger.warning("SCK stream stopped with error: %s", error)


class SckCapture:
    def __init__(
        self,
        window_title_hint: str,
        width: int,
        height: int,
        fps: int,
        on_frame: Callable[[bytes], None],
    ) -> None:
        self._window_title_hint = window_title_hint
        self._width = width
        self._height = height
        self._fps = fps
        self._on_frame = on_frame
        self._stream = None
        self._output = None

    def start(self) -> None:
        content = _get_shareable_content()
        window = _find_target_window(content, self._window_title_hint)

        content_filter = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)

        config = SCK.SCStreamConfiguration.alloc().init()
        config.setWidth_(self._width)
        config.setHeight_(self._height)
        config.setMinimumFrameInterval_(CM.CMTimeMake(1, self._fps))
        config.setQueueDepth_(8)
        config.setShowsCursor_(False)
        config.setPixelFormat_(_BGRA_PIXEL_FORMAT)

        self._output = _StreamOutput.alloc().initWithOnFrame_(self._on_frame)
        delegate = _StreamDelegate.alloc().init()
        self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, config, delegate
        )
        added_ok = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._output, SCK.SCStreamOutputTypeScreen, None, objc.NULL
        )
        if not added_ok:
            raise RuntimeError("SCStream.addStreamOutput_type_sampleHandlerQueue_error_ failed")

        start_done = threading.Event()
        start_result: dict = {}

        def start_handler(error):
            start_result["error"] = error
            start_done.set()

        self._stream.startCaptureWithCompletionHandler_(start_handler)
        if not start_done.wait(10.0):
            raise TimeoutError("SCStream startCapture did not complete in time")
        if start_result["error"] is not None:
            raise RuntimeError(f"SCStream startCapture error: {start_result['error']}")

    def stop(self) -> None:
        if self._stream is None:
            return
        stop_done = threading.Event()
        self._stream.stopCaptureWithCompletionHandler_(lambda error: stop_done.set())
        stop_done.wait(10.0)
        self._stream = None
        self._output = None
