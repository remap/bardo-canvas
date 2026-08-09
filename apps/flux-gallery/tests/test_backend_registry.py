import pytest

from flux_gallery.backend_registry import create_backend
from flux_gallery.backends.fal import FalBackend
from flux_gallery.config import BaseGenerationConfig


def test_create_backend_local_returns_local_backend(monkeypatch):
    # A fake stands in for LocalBackend so this test doesn't load a real
    # diffusers pipeline (network + multi-GB download).
    created = {}

    class _FakeLocalBackend:
        def __init__(self, model, num_inference_steps):
            created["model"] = model
            created["num_inference_steps"] = num_inference_steps

    monkeypatch.setattr("flux_gallery.backend_registry.LocalBackend", _FakeLocalBackend)

    backend = create_backend(
        "local", BaseGenerationConfig(model="fake-model", num_inference_steps=7)
    )

    assert isinstance(backend, _FakeLocalBackend)
    assert created == {"model": "fake-model", "num_inference_steps": 7}


def test_create_backend_fal_returns_fal_backend():
    backend = create_backend(
        "fal", BaseGenerationConfig(fal_endpoint="fal-ai/flux/dev", num_inference_steps=8)
    )

    assert isinstance(backend, FalBackend)
    assert backend._endpoint == "fal-ai/flux/dev"
    assert backend._num_inference_steps == 8


def test_create_backend_rejects_unknown_name():
    with pytest.raises(ValueError, match="Unknown backend: 'bogus'") as excinfo:
        create_backend("bogus", BaseGenerationConfig())

    # The message names what's actually available, matching this codebase's
    # existing convention (see _validate_screen_ids in worker.py).
    assert "['fal', 'local']" in str(excinfo.value)
