from unittest.mock import MagicMock

import pytest

# LocalBackend imports torch and diffusers at module scope, so without the
# flux-gallery extra installed this file would fail at collection with a raw
# ModuleNotFoundError rather than a readable skip.
pytest.importorskip("torch")
pytest.importorskip("diffusers")

import torch

from flux_gallery.backends.local import LocalBackend


class _FakeImage:
    def save(self, buffer, format):  # noqa: A002 - matches PIL.Image.save's signature
        buffer.write(b"fake-png-bytes")


def _make_backend(monkeypatch, *, mps_available: bool) -> LocalBackend:
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: mps_available)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    fake_pipeline = MagicMock()
    fake_pipeline.return_value = MagicMock(images=[_FakeImage()])
    monkeypatch.setattr(
        "flux_gallery.backends.local.FluxPipeline.from_pretrained",
        lambda *args, **kwargs: fake_pipeline,
    )

    return LocalBackend(model="fake-model", num_inference_steps=1)


def test_generate_clears_mps_cache_after_each_call_on_mps(monkeypatch):
    backend = _make_backend(monkeypatch, mps_available=True)
    empty_cache = MagicMock()
    monkeypatch.setattr(torch.mps, "empty_cache", empty_cache)

    backend.generate("a prompt", 1600, 400)

    empty_cache.assert_called_once()


def test_generate_does_not_touch_mps_cache_on_cpu(monkeypatch):
    backend = _make_backend(monkeypatch, mps_available=False)
    empty_cache = MagicMock()
    monkeypatch.setattr(torch.mps, "empty_cache", empty_cache)

    backend.generate("a prompt", 1600, 400)

    empty_cache.assert_not_called()
