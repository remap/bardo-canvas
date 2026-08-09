from __future__ import annotations

from google import genai


def parse_expanded_prompts(text: str, count: int) -> list[str]:
    lines = [line.strip() for line in text.strip().splitlines()]
    prompts = [line for line in lines if line]
    return prompts[:count]


class GeminiExpander:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def expand(self, meta_prompt: str, count: int) -> list[str]:
        instruction = (
            f"Generate exactly {count} distinct, vivid image-generation prompts based on this "
            f"theme: {meta_prompt!r}\n\n"
            f"Reply with exactly {count} lines, one prompt per line, no numbering, no extra commentary."
        )
        response = self._client.models.generate_content(model=self._model, contents=instruction)
        return parse_expanded_prompts(response.text, count)
