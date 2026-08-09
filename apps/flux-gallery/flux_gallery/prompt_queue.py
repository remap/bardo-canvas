from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class PromptExpander(Protocol):
    def expand(self, meta_prompt: str, count: int) -> list[str]: ...


class PromptQueue:
    def __init__(
        self, meta_prompt: str, queue_size: int, refill_when_below: int, expander: PromptExpander
    ) -> None:
        self._meta_prompt = meta_prompt
        self._queue_size = queue_size
        self._refill_when_below = refill_when_below
        self._expander = expander
        self._prompts: list[str] = []

    def pop(self) -> str:
        if len(self._prompts) <= self._refill_when_below:
            self._try_refill()
        if self._prompts:
            return self._prompts.pop(0)
        return self._meta_prompt

    def _try_refill(self) -> None:
        try:
            fresh = self._expander.expand(self._meta_prompt, self._queue_size)
            self._prompts.extend(fresh)
        except Exception:
            logger.warning(
                "Gemini prompt expansion failed; falling back to meta_prompt", exc_info=True
            )
