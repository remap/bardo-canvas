from unittest.mock import patch

import pytest
from flux_gallery.worker import push_with_retry


def test_push_with_retry_succeeds_on_first_attempt():
    calls = []

    def fake_push_image(client, screen_id, image_bytes):
        calls.append(screen_id)
        return 1

    with patch("flux_gallery.worker.push_image", fake_push_image):
        version = push_with_retry(
            client=None, screen_id="F", image_bytes=b"data", backoff_seconds=0
        )

    assert version == 1
    assert calls == ["F"]


def test_push_with_retry_retries_once_then_succeeds():
    attempts = []

    def flaky_push_image(client, screen_id, image_bytes):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("simulated network failure")
        return 2

    with patch("flux_gallery.worker.push_image", flaky_push_image):
        version = push_with_retry(
            client=None, screen_id="F", image_bytes=b"data", backoff_seconds=0
        )

    assert version == 2
    assert len(attempts) == 2


def test_push_with_retry_raises_after_exhausting_retries():
    def always_fails(client, screen_id, image_bytes):
        raise RuntimeError("simulated persistent failure")

    with (
        patch("flux_gallery.worker.push_image", always_fails),
        pytest.raises(RuntimeError, match="simulated persistent failure"),
    ):
        push_with_retry(
            client=None, screen_id="F", image_bytes=b"data", retries=1, backoff_seconds=0
        )
