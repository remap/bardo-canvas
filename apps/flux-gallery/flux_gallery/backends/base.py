from __future__ import annotations

from typing import Protocol


class GenerationBackend(Protocol):
    def generate(self, prompt: str, width: int, height: int) -> bytes: ...
