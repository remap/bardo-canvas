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
        except Exception:
            logger.warning(
                "Gemini prompt expansion failed; falling back to meta_prompt", exc_info=True
            )
            return
        if not fresh:
            # A successful call that yielded nothing (e.g. unparseable text) is
            # otherwise indistinguishable from "queue didn't need a refill yet".
            logger.warning(
                "Gemini prompt expansion returned no usable prompts; falling back to meta_prompt"
            )
            return
        self._prompts.extend(fresh)
        # extend() without a cap could grow the queue past queue_size across
        # repeated refills that happen while prompts remain.
        del self._prompts[self._queue_size :]
