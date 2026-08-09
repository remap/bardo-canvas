from __future__ import annotations

from .backends.base import GenerationBackend
from .backends.fal import FalBackend
from .backends.local import LocalBackend
from .config import BaseGenerationConfig

_BACKENDS: dict[str, type] = {
    "local": LocalBackend,
    "fal": FalBackend,
}


def create_backend(name: str, base_config: BaseGenerationConfig) -> GenerationBackend:
    if name not in _BACKENDS:
        raise ValueError(f"Unknown backend: {name!r}. Available: {sorted(_BACKENDS)}")
    if name == "local":
        return LocalBackend(
            model=base_config.model,
            num_inference_steps=base_config.num_inference_steps,
        )
    return FalBackend(
        endpoint=base_config.fal_endpoint,
        num_inference_steps=base_config.num_inference_steps,
    )
