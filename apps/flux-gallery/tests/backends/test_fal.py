from unittest.mock import MagicMock, patch

import pytest

# FalBackend imports fal_client at module scope, so without the flux-gallery
# extra installed this file would fail at collection with a raw
# ModuleNotFoundError rather than a readable skip.
pytest.importorskip("fal_client")

import fal_client

from flux_gallery.backends.fal import FalBackend


def _http_error(status_code: int, message: str) -> fal_client.FalClientHTTPError:
    return fal_client.FalClientHTTPError(
        message=message, status_code=status_code, response_headers={}, response=MagicMock()
    )


def _handle_returning(result: dict) -> MagicMock:
    handle = MagicMock()
    handle.get.return_value = result
    return handle


def test_generate_returns_downloaded_image_bytes_on_success():
    backend = FalBackend(endpoint="fal-ai/flux/schnell", num_inference_steps=4)
    handle = _handle_returning({"images": [{"url": "https://fal.example/image.png"}]})

    with (
        patch("flux_gallery.backends.fal.fal_client.submit", return_value=handle) as submit,
        patch("flux_gallery.backends.fal.httpx.get") as get,
    ):
        get.return_value = MagicMock(content=b"image-bytes")
        result = backend.generate("a prompt", 1600, 400)

    assert result == b"image-bytes"
    submit.assert_called_once_with(
        "fal-ai/flux/schnell",
        arguments={
            "prompt": "a prompt",
            "image_size": {"width": 1600, "height": 400},
            "num_inference_steps": 4,
            "num_images": 1,
        },
    )
    get.assert_called_once_with("https://fal.example/image.png", timeout=30.0)


def test_generate_retries_once_after_a_single_rate_limit_error(monkeypatch):
    backend = FalBackend(endpoint="fal-ai/flux/schnell", num_inference_steps=4)
    handle = _handle_returning({"images": [{"url": "https://fal.example/image.png"}]})
    monkeypatch.setattr("flux_gallery.backends.fal.time.sleep", lambda seconds: None)

    calls = []

    def flaky_submit(endpoint, arguments):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, "Too Many Requests")
        return handle

    with (
        patch("flux_gallery.backends.fal.fal_client.submit", side_effect=flaky_submit),
        patch("flux_gallery.backends.fal.httpx.get") as get,
    ):
        get.return_value = MagicMock(content=b"image-bytes")
        result = backend.generate("a prompt", 1600, 400)

    assert result == b"image-bytes"
    assert len(calls) == 2


def test_generate_retries_after_a_billing_lock_error(monkeypatch):
    backend = FalBackend(endpoint="fal-ai/flux/schnell", num_inference_steps=4)
    handle = _handle_returning({"images": [{"url": "https://fal.example/image.png"}]})
    monkeypatch.setattr("flux_gallery.backends.fal.time.sleep", lambda seconds: None)

    calls = []

    def flaky_submit(endpoint, arguments):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(400, "Your account balance is exhausted")
        return handle

    with (
        patch("flux_gallery.backends.fal.fal_client.submit", side_effect=flaky_submit),
        patch("flux_gallery.backends.fal.httpx.get") as get,
    ):
        get.return_value = MagicMock(content=b"image-bytes")
        result = backend.generate("a prompt", 1600, 400)

    assert result == b"image-bytes"
    assert len(calls) == 2


def test_generate_raises_after_exhausting_rate_limit_retries(monkeypatch):
    backend = FalBackend(endpoint="fal-ai/flux/schnell", num_inference_steps=4)
    monkeypatch.setattr("flux_gallery.backends.fal.time.sleep", lambda seconds: None)

    def always_rate_limited(endpoint, arguments):
        raise _http_error(429, "Too Many Requests")

    with (
        patch("flux_gallery.backends.fal.fal_client.submit", side_effect=always_rate_limited),
        pytest.raises(fal_client.FalClientHTTPError),
    ):
        backend.generate("a prompt", 1600, 400)
