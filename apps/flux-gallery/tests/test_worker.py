from unittest.mock import patch

import pytest

# flux_gallery.worker -> backend_registry -> backends.local -> torch, diffusers
# (and -> backends.fal -> fal_client), so without the flux-gallery extra installed
# this file would fail at collection with a raw ModuleNotFoundError.
pytest.importorskip("torch")
pytest.importorskip("diffusers")
pytest.importorskip("fal_client")

from flux_gallery.config import BaseGenerationConfig, PromptsConfig, ScreenPromptConfig
from flux_gallery.worker import (
    _resolve_backend_name,
    _validate_backend_selection,
    _validate_screen_ids,
    push_with_retry,
)

from layout_server.config import (
    CanvasConfig,
    LayoutConfig,
    LayoutOffset,
    ScreenConfig,
    ScreenGrid,
    ScreenRect,
)


def _layout_with_ids(*ids: str) -> LayoutConfig:
    return LayoutConfig(
        canvas=CanvasConfig(width=3840, height=2160, fps=30),
        module_size=200,
        layout_offset=LayoutOffset(x=0, y=0),
        screens=[
            ScreenConfig(
                id=screen_id,
                name=f"Screen {screen_id}",
                grid=ScreenGrid(col=0, row=index, cols=2, rows=1),
                rect=ScreenRect(x=0, y=index * 200, width=400, height=200),
            )
            for index, screen_id in enumerate(ids)
        ],
    )


def _prompts_with_ids(*ids: str) -> PromptsConfig:
    return PromptsConfig(
        base={},
        screens=[ScreenPromptConfig(id=screen_id, meta_prompt="a prompt") for screen_id in ids],
    )


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


def test_validate_screen_ids_accepts_a_matching_set():
    # No exception: every prompts.yaml id exists in the layout.
    _validate_screen_ids(_prompts_with_ids("F", "B"), _layout_with_ids("F", "B", "C"))


def test_validate_screen_ids_rejects_an_id_missing_from_the_layout():
    with pytest.raises(ValueError, match="unknown screen id 'Z'") as excinfo:
        _validate_screen_ids(_prompts_with_ids("F", "Z"), _layout_with_ids("F", "B"))

    # The message names the ids the operator can actually choose from.
    assert "['B', 'F']" in str(excinfo.value)


def test_resolve_backend_name_uses_flux_backend_env_override():
    base_config = BaseGenerationConfig(backend="local")

    assert _resolve_backend_name({"FLUX_BACKEND": "fal"}, base_config) == "fal"


def test_resolve_backend_name_falls_through_to_config_when_env_unset():
    base_config = BaseGenerationConfig(backend="fal")

    assert _resolve_backend_name({}, base_config) == "fal"


def test_validate_backend_selection_accepts_local_without_fal_key():
    _validate_backend_selection("local", {})  # must not raise


def test_validate_backend_selection_accepts_fal_with_fal_key_set():
    _validate_backend_selection("fal", {"FAL_KEY": "secret"})  # must not raise


def test_validate_backend_selection_rejects_fal_without_fal_key():
    with pytest.raises(ValueError, match="FAL_KEY"):
        _validate_backend_selection("fal", {})


def test_validate_backend_selection_rejects_fal_with_empty_fal_key():
    with pytest.raises(ValueError, match="FAL_KEY"):
        _validate_backend_selection("fal", {"FAL_KEY": ""})
