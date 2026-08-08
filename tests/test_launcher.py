import http.server
import socketserver
import threading

import pytest

from ndi_broadcaster.launcher import (
    HealthCheckTimeoutError,
    _LatestFrameSlot,
    wait_for_healthy,
)


class _HealthyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def test_wait_for_healthy_succeeds():
    with socketserver.TCPServer(("127.0.0.1", 0), _HealthyHandler) as server:
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            wait_for_healthy(f"http://127.0.0.1:{port}/healthz", timeout_seconds=5.0)
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_wait_for_healthy_times_out():
    with pytest.raises(HealthCheckTimeoutError):
        wait_for_healthy(
            "http://127.0.0.1:1/healthz", timeout_seconds=1.0, poll_interval_seconds=0.2
        )


def test_latest_frame_slot_starts_empty():
    assert _LatestFrameSlot().take() is None


def test_latest_frame_slot_keeps_only_the_latest_value():
    slot = _LatestFrameSlot()
    slot.put("first")
    slot.put("second")

    assert slot.take() == "second"
    # No backlog: the superseded frame is gone, not queued behind the latest one.
    assert slot.take() is None
