from __future__ import annotations

import io

import torch
from diffusers import FluxPipeline


def _resolve_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class FluxGenerator:
    def __init__(self, model: str, num_inference_steps: int) -> None:
        self._num_inference_steps = num_inference_steps
        self._device = _resolve_device()
        self._pipeline = FluxPipeline.from_pretrained(model, torch_dtype=torch.bfloat16)
        self._pipeline.to(self._device)

    def generate(self, prompt: str, width: int, height: int) -> bytes:
        result = self._pipeline(
            prompt=prompt,
            width=width,
            height=height,
            num_inference_steps=self._num_inference_steps,
        )
        image = result.images[0]
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        if self._device == "mps":
            # Each screen has a different rect size, so this pipeline is called with a
            # different resolution every few cycles. MPS's caching allocator keeps a
            # separate pool per distinct tensor shape and never trims it on its own --
            # without this, RSS grows unbounded (observed 50GB+ after ~40 minutes).
            torch.mps.empty_cache()
        return buffer.getvalue()
