from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class BaseGenerationConfig(BaseModel):
    model: str = "black-forest-labs/FLUX.1-schnell"
    gemini_model: str = "gemini-flash-latest"
    num_inference_steps: int = 4
    queue_size: int = 5
    refill_when_below: int = 2
    backend: Literal["local", "fal"] = "local"
    fal_endpoint: str = "fal-ai/flux/schnell"


class ScreenPromptConfig(BaseModel):
    id: str
    meta_prompt: str


class PromptsConfig(BaseModel):
    base: BaseGenerationConfig
    screens: list[ScreenPromptConfig]

    def screen_by_id(self, screen_id: str) -> ScreenPromptConfig | None:
        return next((screen for screen in self.screens if screen.id == screen_id), None)


def load_prompts_config(path: Path) -> PromptsConfig:
    raw = yaml.safe_load(path.read_text())
    return PromptsConfig(**raw)
