# Flux Gallery App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `apps/flux-gallery/` — a Python worker that continuously generates AI images (local Flux.1-schnell on MPS, prompts expanded per-screen by Gemini) and pushes them to the already-built, already-reviewed layout-driver framework, validating its push-image and screenshot APIs end to end.

**Architecture:** A single long-running worker process (`flux_gallery.worker.run_forever`) picks a random screen each cycle, pops a ready-made prompt from that screen's `PromptQueue` (backed by a `GeminiExpander` that batch-refills via the `google-genai` SDK), generates an image with a `FluxGenerator` (`diffusers.FluxPipeline` on MPS), pushes it to the framework's `POST /screens/{id}/image`, then calls the framework's `POST /api/screenshot` and saves both artifacts to disk with simple retention pruning. Every piece other than the two real-library calls (Gemini, Flux) is a small, independently unit-tested pure/HTTP-mocked module.

**Tech Stack:** Python 3.13 via `uv`, `google-genai` SDK, `diffusers`/`torch`/`transformers` for local Flux inference on MPS, `httpx` for calling the framework's HTTP API (already a project dependency), `pytest`.

## Global Constraints

- This app lives entirely under `apps/flux-gallery/` — no changes to `layout_server/`, `ndi_broadcaster/`, or `static/`.
- The framework's real, already-implemented HTTP contract (verified directly against `layout_server/screens_api.py` and `layout_server/screenshot.py` — do not trust spec prose over this):
  - `POST /screens/{screen_id}/image` — body is raw image bytes, header `content-type: image/png` or `image/jpeg` (exact match, no parameters), optional `?transition_ms=` query param. Returns `{"version": <int>}` on success (200). Returns 404 for an unknown screen id, 400 for a bad/missing content-type or undecodable bytes.
  - `POST /api/screenshot` — no body. Returns raw PNG bytes (`Content-Type: image/png`) on success (200). Returns 504 if no browser client is currently connected to the framework's `/ws`, or if one is connected but doesn't respond within ~2 seconds.
- Real screen pixel dimensions (from `config/screens.yaml`, loaded via `layout_server.config.load_layout_config` — reuse this directly, don't re-parse the YAML): F=1800×1400, B=1200×600, C=1200×600, D=1600×400, A=1600×400, E=1600×400.
- Flux dimension rounding: round each screen's width/height to the nearest multiple of 16, rounding exactly-halfway cases **up** (e.g. 1800 → 1808, 1400 → 1408, 600 → 608). Screens whose dimensions are already multiples of 16 (1600, 400, 1200) are unchanged.
- Gemini: use the official `google-genai` SDK (`from google import genai`), never a hand-rolled REST call. Verified real API: `genai.Client(api_key=...)`, `client.models.generate_content(model=..., contents=...)` returns an object with a `.text` attribute.
- Flux: use `diffusers.FluxPipeline`. Verified real API: `FluxPipeline.from_pretrained(model_name, torch_dtype=torch.bfloat16)`, `.to(device)`, then calling the pipeline instance as `pipeline(prompt=..., width=..., height=..., num_inference_steps=...)` returns an object with `.images` (a list of PIL images) when `return_dict=True` (the default).
- `HF_TOKEN` needs no custom code — `diffusers`/`huggingface_hub`'s `from_pretrained` already reads the `HF_TOKEN` env var automatically, falling back to the cached `~/.cache/huggingface/token`, matching the spec's fallback chain with zero extra code.
- `ruff format` and `ruff check` must pass on all Python files touched; line length 100 (repo's existing `[tool.ruff]` config, unchanged).
- `uv run pytest -v` (run from the repo root) must include and pass every test this plan adds, alongside the existing framework suite.

---

## Task 1: Project scaffolding + resolution rounding

**Files:**
- Modify: `pyproject.toml`
- Modify: `.gitignore`
- Create: `apps/flux-gallery/flux_gallery/__init__.py`
- Create: `apps/flux-gallery/flux_gallery/resolution.py`
- Create: `apps/flux-gallery/config/prompts.yaml`
- Create: `apps/flux-gallery/tests/__init__.py`
- Test: `apps/flux-gallery/tests/test_resolution.py`

**Interfaces:**
- Produces: `flux_gallery.resolution.compute_target_resolution(screen_width: int, screen_height: int) -> tuple[int, int]`.

- [ ] **Step 1: Add the `flux-gallery` optional dependency group and pytest path config**

Modify `pyproject.toml` — add a new `[project.optional-dependencies]` table (after the existing `dependencies = [...]` list) and a `[tool.pytest.ini_options]` table (anywhere after `[project]`, e.g. right before `[tool.ruff]`):

```toml
[project.optional-dependencies]
flux-gallery = [
    "google-genai>=1.0",
    "diffusers>=0.31",
    "transformers>=4.46",
    "accelerate>=1.1",
    "sentencepiece>=0.2",
    "torch>=2.5",
]

[tool.pytest.ini_options]
pythonpath = ["apps/flux-gallery"]
```

This keeps the framework's own base install free of ~heavy ML dependencies (torch, diffusers) — they're only pulled in via `uv sync --extra flux-gallery` — while `pythonpath` makes the `flux_gallery` package (created below) importable by both the app's own code and by `pytest` without needing package-relative import hacks.

Run: `uv sync --extra flux-gallery` (this will take a while — it downloads `torch`, `diffusers`, `transformers`, etc.)
Expected: completes without error; `uv run python -c "import torch; print(torch.backends.mps.is_available())"` prints `True` on Apple Silicon.

- [ ] **Step 2: Gitignore the app's generated output**

Modify `.gitignore` — append:

```
apps/flux-gallery/output/
```

- [ ] **Step 3: Write the failing test**

Create `apps/flux-gallery/tests/__init__.py` (empty file).

Create `apps/flux-gallery/tests/test_resolution.py`:

```python
from flux_gallery.resolution import compute_target_resolution


def test_screen_f_1800x1400_rounds_up_to_nearest_16():
    assert compute_target_resolution(1800, 1400) == (1808, 1408)


def test_screens_b_and_c_1200x600_round_height_up_to_nearest_16():
    assert compute_target_resolution(1200, 600) == (1200, 608)


def test_screens_d_a_e_1600x400_already_multiples_of_16_unchanged():
    assert compute_target_resolution(1600, 400) == (1600, 400)
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest apps/flux-gallery/tests/test_resolution.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'flux_gallery'`.

- [ ] **Step 5: Implement `flux_gallery/resolution.py`**

Create `apps/flux-gallery/flux_gallery/__init__.py` (empty file).

Create `apps/flux-gallery/flux_gallery/resolution.py`:

```python
from __future__ import annotations


def _round_to_16(value: int) -> int:
    return ((value + 8) // 16) * 16


def compute_target_resolution(screen_width: int, screen_height: int) -> tuple[int, int]:
    return _round_to_16(screen_width), _round_to_16(screen_height)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest apps/flux-gallery/tests/test_resolution.py -v`
Expected: 3 passed.

- [ ] **Step 7: Create the real `prompts.yaml`**

Create `apps/flux-gallery/config/prompts.yaml`:

```yaml
base:
  model: "black-forest-labs/FLUX.1-schnell"
  gemini_model: "gemini-2.5-flash"
  num_inference_steps: 4
  queue_size: 5
  refill_when_below: 2

screens:
  - id: F
    meta_prompt: "A sweeping abstract landscape in bold saturated colors, painterly and dreamlike."
  - id: B
    meta_prompt: "Macro photography of natural textures: water, stone, bark, ice."
  - id: C
    meta_prompt: "Geometric op-art patterns in high contrast black, white, and one accent color."
  - id: D
    meta_prompt: "Retro-futuristic cityscapes at dusk, neon reflections, wide panoramic framing."
  - id: A
    meta_prompt: "Botanical illustrations of imaginary plants, detailed linework, muted palette."
  - id: E
    meta_prompt: "Slow-motion smoke and fluid dynamics rendered as fine art photography."
```

These are placeholder themes — per the spec, each screen's theme lives entirely in this config and is meant to be hand-edited later; the six above are independent and non-overlapping, which is all this task requires.

- [ ] **Step 8: Lint and commit**

Run: `uv run ruff format apps/flux-gallery/flux_gallery/ apps/flux-gallery/tests/ && uv run ruff check apps/flux-gallery/`

```bash
git add pyproject.toml uv.lock .gitignore apps/flux-gallery/flux_gallery/ apps/flux-gallery/config/prompts.yaml apps/flux-gallery/tests/
git commit -m "feat: scaffold flux-gallery app with resolution-rounding math"
```

---

## Task 2: Prompts config model + loader

**Files:**
- Create: `apps/flux-gallery/flux_gallery/config.py`
- Test: `apps/flux-gallery/tests/test_config.py`

**Interfaces:**
- Consumes: `apps/flux-gallery/config/prompts.yaml` (Task 1).
- Produces: `flux_gallery.config.BaseGenerationConfig`, `ScreenPromptConfig`, `PromptsConfig` (Pydantic models); `PromptsConfig.screen_by_id(screen_id: str) -> ScreenPromptConfig | None`; `load_prompts_config(path: Path) -> PromptsConfig`.

- [ ] **Step 1: Write the failing test**

Create `apps/flux-gallery/tests/test_config.py`:

```python
from pathlib import Path

from flux_gallery.config import load_prompts_config

PROMPTS_YAML = Path(__file__).resolve().parent.parent / "config" / "prompts.yaml"


def test_load_prompts_config_has_six_screens_with_independent_meta_prompts():
    config = load_prompts_config(PROMPTS_YAML)
    assert len(config.screens) == 6
    ids = {screen.id for screen in config.screens}
    assert ids == {"F", "B", "C", "D", "A", "E"}
    meta_prompts = [screen.meta_prompt for screen in config.screens]
    assert len(set(meta_prompts)) == 6


def test_base_config_defaults_match_prompts_yaml():
    config = load_prompts_config(PROMPTS_YAML)
    assert config.base.model == "black-forest-labs/FLUX.1-schnell"
    assert config.base.gemini_model == "gemini-2.5-flash"
    assert config.base.num_inference_steps == 4
    assert config.base.queue_size == 5
    assert config.base.refill_when_below == 2


def test_screen_by_id_returns_none_for_unknown_id():
    config = load_prompts_config(PROMPTS_YAML)
    assert config.screen_by_id("Z") is None
    assert config.screen_by_id("F") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/flux-gallery/tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'flux_gallery.config'`.

- [ ] **Step 3: Implement `flux_gallery/config.py`**

```python
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class BaseGenerationConfig(BaseModel):
    model: str = "black-forest-labs/FLUX.1-schnell"
    gemini_model: str = "gemini-2.5-flash"
    num_inference_steps: int = 4
    queue_size: int = 5
    refill_when_below: int = 2


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest apps/flux-gallery/tests/test_config.py -v`
Expected: 3 passed.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format apps/flux-gallery/flux_gallery/config.py apps/flux-gallery/tests/test_config.py && uv run ruff check apps/flux-gallery/`

```bash
git add apps/flux-gallery/flux_gallery/config.py apps/flux-gallery/tests/test_config.py
git commit -m "feat: add flux-gallery prompts config model and loader"
```

---

## Task 3: `PromptQueue` with Gemini-refill logic

**Files:**
- Create: `apps/flux-gallery/flux_gallery/prompt_queue.py`
- Test: `apps/flux-gallery/tests/test_prompt_queue.py`

**Interfaces:**
- Consumes: nothing from Tasks 1–2 directly (structurally independent; wired together in Task 8).
- Produces: `flux_gallery.prompt_queue.PromptExpander` (a `Protocol` with `expand(meta_prompt: str, count: int) -> list[str]`); `PromptQueue(meta_prompt: str, queue_size: int, refill_when_below: int, expander: PromptExpander)` with `.pop() -> str`.

- [ ] **Step 1: Write the failing test**

Create `apps/flux-gallery/tests/test_prompt_queue.py`:

```python
from flux_gallery.prompt_queue import PromptQueue


class _FakeExpander:
    def __init__(self, prompts=None, raises=False):
        self._prompts = prompts or []
        self._raises = raises
        self.calls = 0

    def expand(self, meta_prompt, count):
        self.calls += 1
        if self._raises:
            raise RuntimeError("simulated Gemini failure")
        return list(self._prompts)


def test_refill_triggers_on_first_pop_from_an_empty_queue():
    expander = _FakeExpander(prompts=["a", "b", "c"])
    queue = PromptQueue("meta", queue_size=3, refill_when_below=2, expander=expander)

    assert queue.pop() == "a"
    assert expander.calls == 1


def test_refill_does_not_trigger_while_queue_is_above_threshold():
    expander = _FakeExpander(prompts=["a", "b", "c", "d", "e"])
    queue = PromptQueue("meta", queue_size=5, refill_when_below=2, expander=expander)

    queue.pop()  # 0 <= 2 -> refill to 5, pop "a" -> 4 left
    assert expander.calls == 1
    queue.pop()  # 4 left before this pop, 4 > 2 -> no refill
    assert expander.calls == 1


def test_refill_triggers_again_once_queue_drops_to_threshold():
    expander = _FakeExpander(prompts=["a", "b"])
    queue = PromptQueue("meta", queue_size=2, refill_when_below=1, expander=expander)

    queue.pop()  # 0 <= 1 -> refill to ["a","b"], pop "a" -> ["b"] left
    assert expander.calls == 1
    queue.pop()  # 1 left <= 1 -> refill again before popping
    assert expander.calls == 2


def test_fallback_to_meta_prompt_when_expander_raises():
    expander = _FakeExpander(raises=True)
    queue = PromptQueue("the meta prompt text", queue_size=3, refill_when_below=2, expander=expander)

    assert queue.pop() == "the meta prompt text"
    assert expander.calls == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/flux-gallery/tests/test_prompt_queue.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'flux_gallery.prompt_queue'`.

- [ ] **Step 3: Implement `flux_gallery/prompt_queue.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest apps/flux-gallery/tests/test_prompt_queue.py -v`
Expected: 4 passed.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format apps/flux-gallery/flux_gallery/prompt_queue.py apps/flux-gallery/tests/test_prompt_queue.py && uv run ruff check apps/flux-gallery/`

```bash
git add apps/flux-gallery/flux_gallery/prompt_queue.py apps/flux-gallery/tests/test_prompt_queue.py
git commit -m "feat: add PromptQueue with Gemini-refill-and-fallback logic"
```

---

## Task 4: `GeminiExpander`

**Files:**
- Create: `apps/flux-gallery/flux_gallery/gemini_expander.py`
- Test: `apps/flux-gallery/tests/test_gemini_expander.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (satisfies the `PromptExpander` protocol from Task 3 structurally, not by inheritance).
- Produces: `flux_gallery.gemini_expander.parse_expanded_prompts(text: str, count: int) -> list[str]`; `GeminiExpander(api_key: str, model: str)` with `.expand(meta_prompt: str, count: int) -> list[str]`.

- [ ] **Step 1: Write the failing test for the pure parsing function**

Create `apps/flux-gallery/tests/test_gemini_expander.py`:

```python
from flux_gallery.gemini_expander import parse_expanded_prompts


def test_parse_expanded_prompts_splits_lines_and_trims():
    text = "\n  first prompt  \nsecond prompt\n\nthird prompt\n"
    assert parse_expanded_prompts(text, count=3) == ["first prompt", "second prompt", "third prompt"]


def test_parse_expanded_prompts_truncates_to_requested_count():
    text = "one\ntwo\nthree\nfour"
    assert parse_expanded_prompts(text, count=2) == ["one", "two"]


def test_parse_expanded_prompts_skips_blank_lines():
    text = "one\n\n\ntwo\n"
    assert parse_expanded_prompts(text, count=2) == ["one", "two"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/flux-gallery/tests/test_gemini_expander.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'flux_gallery.gemini_expander'`.

- [ ] **Step 3: Implement `flux_gallery/gemini_expander.py`**

This uses the real, verified `google-genai` SDK shape: `genai.Client(api_key=...)`, `client.models.generate_content(model=..., contents=...) -> response` where `response.text` is the generated text.

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest apps/flux-gallery/tests/test_gemini_expander.py -v`
Expected: 3 passed. (These only exercise `parse_expanded_prompts` — no network call, no API key needed.)

- [ ] **Step 5: Manually verify `GeminiExpander` against the real API, if a key is available**

`GeminiExpander.__init__`/`.expand()` need a real network call, which cannot be unit tested. If a real `GEMINI_API_KEY` is available in your environment, verify it for real:

```bash
GEMINI_API_KEY=<your-real-key> uv run python -c "
from flux_gallery.gemini_expander import GeminiExpander
expander = GeminiExpander(api_key='$GEMINI_API_KEY', model='gemini-2.5-flash')
prompts = expander.expand('A sweeping abstract landscape in bold saturated colors', count=3)
print(len(prompts), prompts)
"
```

Expected: prints `3 [...]` with three distinct, non-empty prompt strings. If no real key is available, skip this step — Task 8's end-to-end verification will exercise the fallback path (an intentionally invalid key), which is itself a required, spec-mandated behavior worth verifying for real.

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff format apps/flux-gallery/flux_gallery/gemini_expander.py apps/flux-gallery/tests/test_gemini_expander.py && uv run ruff check apps/flux-gallery/`

```bash
git add apps/flux-gallery/flux_gallery/gemini_expander.py apps/flux-gallery/tests/test_gemini_expander.py
git commit -m "feat: add GeminiExpander using the real google-genai SDK"
```

---

## Task 5: `FluxGenerator`

**Files:**
- Create: `apps/flux-gallery/flux_gallery/flux_generator.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `flux_gallery.flux_generator.FluxGenerator(model: str, num_inference_steps: int)` with `.generate(prompt: str, width: int, height: int) -> bytes` (PNG-encoded).

No automated test is possible here — it needs a real multi-gigabyte model download and either a GPU or Apple Silicon MPS device to run in reasonable time. This machine has both (Apple Silicon with MPS, and internet access), so **actually run it for real** in Step 3 rather than treating this as untestable.

- [ ] **Step 1: Implement `flux_gallery/flux_generator.py`**

This uses the real, verified `diffusers.FluxPipeline` shape: `FluxPipeline.from_pretrained(model, torch_dtype=torch.bfloat16)`, `.to(device)`, and calling the pipeline as `pipeline(prompt=..., width=..., height=..., num_inference_steps=...)` returns an object whose `.images[0]` is a PIL Image (confirmed by reading the installed library's own source, not assumed).

```python
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
        device = _resolve_device()
        self._pipeline = FluxPipeline.from_pretrained(model, torch_dtype=torch.bfloat16)
        self._pipeline.to(device)

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
        return buffer.getvalue()
```

- [ ] **Step 2: Verify the real API shape before running it**

Before running real inference, double-check the installed `diffusers` version's `FluxPipeline.__call__` signature still matches what's assumed above (dimension keyword names, return shape) — library versions can drift:

```bash
uv run python -c "
from diffusers import FluxPipeline
import inspect
src = inspect.getsource(FluxPipeline.__call__)
print(src[:1200])
"
```

Expected: shows a signature with `prompt`, `height`, `width`, `num_inference_steps` keyword parameters, matching Step 1's call. If the installed version differs, adjust `flux_generator.py` accordingly and note the discrepancy in your report.

- [ ] **Step 3: Run a real generation end to end**

This downloads the FLUX.1-schnell weights on first run (tens of gigabytes — expect this to take several minutes depending on network speed) and then runs real inference on MPS (much faster, seconds once loaded). Budget real wall-clock time for this step; it is not optional.

```bash
uv run python -c "
from flux_gallery.flux_generator import FluxGenerator
generator = FluxGenerator(model='black-forest-labs/FLUX.1-schnell', num_inference_steps=4)
png_bytes = generator.generate('a simple test pattern of red and blue stripes', width=512, height=512)
with open('/tmp/flux-generator-check.png', 'wb') as f:
    f.write(png_bytes)
print('wrote', len(png_bytes), 'bytes')
"
file /tmp/flux-generator-check.png
```

Expected: prints `wrote <N> bytes` with N in the hundreds of KB, and `file` reports a valid PNG image, 512x512. Open the file (e.g. with the Read tool, since it's an image) to visually confirm it's a real generated image, not noise or a blank frame.

If this step cannot complete in your environment (no MPS/GPU, no disk space, no time budget), report DONE_WITH_CONCERNS and state exactly what you observed (e.g., how far the download got, any error) rather than skipping it silently — this is the single highest-risk untested piece of the whole app.

Delete the throwaway output file afterward: `rm -f /tmp/flux-generator-check.png`.

- [ ] **Step 4: Lint and commit**

Run: `uv run ruff format apps/flux-gallery/flux_gallery/flux_generator.py && uv run ruff check apps/flux-gallery/`

```bash
git add apps/flux-gallery/flux_gallery/flux_generator.py
git commit -m "feat: add FluxGenerator wrapping diffusers FluxPipeline on MPS"
```

---

## Task 6: Layout-driver HTTP client (push + screenshot)

**Files:**
- Create: `apps/flux-gallery/flux_gallery/layout_driver_client.py`
- Test: `apps/flux-gallery/tests/test_layout_driver_client.py`

**Interfaces:**
- Consumes: the real framework's `layout_server.app.create_app` (already implemented, see Global Constraints for the exact HTTP contract this task tests against).
- Produces: `flux_gallery.layout_driver_client.push_image(client: httpx.Client, screen_id: str, image_bytes: bytes, content_type: str = "image/png") -> int`; `take_screenshot(client: httpx.Client) -> bytes`.

- [ ] **Step 1: Write the failing test**

This test runs the REAL `layout_server` FastAPI app in-process via Starlette's `TestClient` (which is an `httpx.Client` subclass, so it can be passed directly to the functions under test) — no real network, no real server process, but exercising the actual framework code these functions will call in production.

Create `apps/flux-gallery/tests/test_layout_driver_client.py`:

```python
import io
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from flux_gallery.layout_driver_client import push_image, take_screenshot
from layout_server.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]


def _png_bytes(color=(10, 20, 30)) -> bytes:
    image = Image.new("RGB", (4, 4), color=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def client(tmp_path):
    app_static_dir = tmp_path / "app-static"
    app_static_dir.mkdir()
    (app_static_dir / "index.html").write_text("<!doctype html><html><body></body></html>")

    app = create_app(
        screens_yaml=REPO_ROOT / "config" / "screens.yaml",
        audio_yaml=REPO_ROOT / "config" / "audio.yaml",
        runtime_dir=tmp_path / "runtime",
        app_static_dir=app_static_dir,
        framework_static_dir=REPO_ROOT / "static",
    )
    with TestClient(app) as test_client:
        yield test_client


def test_push_image_returns_incrementing_version(client):
    assert push_image(client, "F", _png_bytes()) == 1
    assert push_image(client, "F", _png_bytes(color=(50, 60, 70))) == 2


def test_push_image_unknown_screen_raises(client):
    with pytest.raises(httpx.HTTPStatusError):
        push_image(client, "Z", _png_bytes())


def test_take_screenshot_with_no_connected_browser_raises(client):
    with pytest.raises(httpx.HTTPStatusError):
        take_screenshot(client)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/flux-gallery/tests/test_layout_driver_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'flux_gallery.layout_driver_client'`.

- [ ] **Step 3: Implement `flux_gallery/layout_driver_client.py`**

```python
from __future__ import annotations

import httpx


def push_image(
    client: httpx.Client, screen_id: str, image_bytes: bytes, content_type: str = "image/png"
) -> int:
    response = client.post(
        f"/screens/{screen_id}/image",
        content=image_bytes,
        headers={"content-type": content_type},
    )
    response.raise_for_status()
    return response.json()["version"]


def take_screenshot(client: httpx.Client) -> bytes:
    response = client.post("/api/screenshot")
    response.raise_for_status()
    return response.content
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest apps/flux-gallery/tests/test_layout_driver_client.py -v`
Expected: 3 passed.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format apps/flux-gallery/flux_gallery/layout_driver_client.py apps/flux-gallery/tests/test_layout_driver_client.py && uv run ruff check apps/flux-gallery/`

```bash
git add apps/flux-gallery/flux_gallery/layout_driver_client.py apps/flux-gallery/tests/test_layout_driver_client.py
git commit -m "feat: add layout-driver push/screenshot HTTP client"
```

---

## Task 7: Disk history with retention pruning

**Files:**
- Create: `apps/flux-gallery/flux_gallery/disk_history.py`
- Test: `apps/flux-gallery/tests/test_disk_history.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `flux_gallery.disk_history.save_and_prune(directory: Path, filename: str, data: bytes, keep: int) -> Path`.

- [ ] **Step 1: Write the failing test**

Create `apps/flux-gallery/tests/test_disk_history.py`:

```python
from flux_gallery.disk_history import save_and_prune


def test_save_and_prune_writes_file_with_correct_content(tmp_path):
    path = save_and_prune(tmp_path / "screen-f", "a.png", b"data", keep=200)
    assert path.read_bytes() == b"data"
    assert path.name == "a.png"


def test_save_and_prune_keeps_only_the_most_recent_n(tmp_path):
    directory = tmp_path / "screen-f"
    save_and_prune(directory, "a.png", b"1", keep=2)
    save_and_prune(directory, "b.png", b"2", keep=2)
    save_and_prune(directory, "c.png", b"3", keep=2)

    remaining = sorted(p.name for p in directory.iterdir())
    assert remaining == ["b.png", "c.png"]


def test_save_and_prune_does_not_prune_when_under_the_limit(tmp_path):
    directory = tmp_path / "screen-f"
    save_and_prune(directory, "a.png", b"1", keep=200)
    save_and_prune(directory, "b.png", b"2", keep=200)

    remaining = sorted(p.name for p in directory.iterdir())
    assert remaining == ["a.png", "b.png"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/flux-gallery/tests/test_disk_history.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'flux_gallery.disk_history'`.

- [ ] **Step 3: Implement `flux_gallery/disk_history.py`**

```python
from __future__ import annotations

from pathlib import Path


def save_and_prune(directory: Path, filename: str, data: bytes, keep: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    file_path = directory / filename
    file_path.write_bytes(data)

    existing = sorted(directory.iterdir())
    if len(existing) > keep:
        for stale in existing[: len(existing) - keep]:
            stale.unlink()

    return file_path
```

Filenames are expected to sort chronologically (the caller generates timestamp-based names — see Task 8), so a plain lexicographic sort of directory contents is sufficient to identify the oldest files.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest apps/flux-gallery/tests/test_disk_history.py -v`
Expected: 3 passed.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff format apps/flux-gallery/flux_gallery/disk_history.py apps/flux-gallery/tests/test_disk_history.py && uv run ruff check apps/flux-gallery/`

```bash
git add apps/flux-gallery/flux_gallery/disk_history.py apps/flux-gallery/tests/test_disk_history.py
git commit -m "feat: add disk history with most-recent-N retention pruning"
```

---

## Task 8: Worker main loop, retry logic, and `run.sh`

**Files:**
- Create: `apps/flux-gallery/flux_gallery/worker.py`
- Create: `apps/flux-gallery/run.sh`
- Test: `apps/flux-gallery/tests/test_worker.py`

**Interfaces:**
- Consumes: `layout_server.config.load_layout_config` (framework); `flux_gallery.config.load_prompts_config` (Task 2); `flux_gallery.prompt_queue.PromptQueue` (Task 3); `flux_gallery.gemini_expander.GeminiExpander` (Task 4); `flux_gallery.flux_generator.FluxGenerator` (Task 5); `flux_gallery.layout_driver_client.push_image`/`take_screenshot` (Task 6); `flux_gallery.disk_history.save_and_prune` (Task 7).
- Produces: `flux_gallery.worker.push_with_retry(client: httpx.Client, screen_id: str, image_bytes: bytes, retries: int = 1, backoff_seconds: float = 1.0) -> int`; `flux_gallery.worker.run_forever() -> None` (never returns under normal operation); `flux_gallery.worker.main() -> None` (entrypoint, configures logging then calls `run_forever`).

- [ ] **Step 1: Write the failing test for `push_with_retry`**

Create `apps/flux-gallery/tests/test_worker.py`:

```python
from unittest.mock import patch

import pytest

from flux_gallery.worker import push_with_retry


def test_push_with_retry_succeeds_on_first_attempt():
    calls = []

    def fake_push_image(client, screen_id, image_bytes):
        calls.append(screen_id)
        return 1

    with patch("flux_gallery.worker.push_image", fake_push_image):
        version = push_with_retry(client=None, screen_id="F", image_bytes=b"data", backoff_seconds=0)

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
        version = push_with_retry(client=None, screen_id="F", image_bytes=b"data", backoff_seconds=0)

    assert version == 2
    assert len(attempts) == 2


def test_push_with_retry_raises_after_exhausting_retries():
    def always_fails(client, screen_id, image_bytes):
        raise RuntimeError("simulated persistent failure")

    with patch("flux_gallery.worker.push_image", always_fails):
        with pytest.raises(RuntimeError, match="simulated persistent failure"):
            push_with_retry(
                client=None, screen_id="F", image_bytes=b"data", retries=1, backoff_seconds=0
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/flux-gallery/tests/test_worker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'flux_gallery.worker'`.

- [ ] **Step 3: Implement `flux_gallery/worker.py`**

```python
from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from layout_server.config import load_layout_config

from .config import load_prompts_config
from .disk_history import save_and_prune
from .flux_generator import FluxGenerator
from .gemini_expander import GeminiExpander
from .layout_driver_client import push_image, take_screenshot
from .prompt_queue import PromptQueue
from .resolution import compute_target_resolution

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = Path(__file__).resolve().parent.parent
HISTORY_KEEP = 200


def _timestamp_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".png"


def push_with_retry(
    client: httpx.Client,
    screen_id: str,
    image_bytes: bytes,
    retries: int = 1,
    backoff_seconds: float = 1.0,
) -> int:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return push_image(client, screen_id, image_bytes)
        except Exception as exc:
            last_exc = exc
            logger.warning("Push to screen %s failed (attempt %d): %s", screen_id, attempt + 1, exc)
            if attempt < retries:
                time.sleep(backoff_seconds)
    assert last_exc is not None
    raise last_exc


def run_forever() -> None:
    screens_yaml = Path(os.environ.get("SCREENS_YAML", str(REPO_ROOT / "config" / "screens.yaml")))
    prompts_yaml = Path(os.environ.get("PROMPTS_YAML", str(APP_ROOT / "config" / "prompts.yaml")))
    layout_driver_url = os.environ.get("LAYOUT_DRIVER_URL", "https://localhost:8443")
    gemini_api_key = os.environ["GEMINI_API_KEY"]

    layout_config = load_layout_config(screens_yaml)
    prompts_config = load_prompts_config(prompts_yaml)

    expander = GeminiExpander(api_key=gemini_api_key, model=prompts_config.base.gemini_model)
    generator = FluxGenerator(
        model=prompts_config.base.model,
        num_inference_steps=prompts_config.base.num_inference_steps,
    )
    http_client = httpx.Client(base_url=layout_driver_url, verify=False, timeout=30.0)

    queues = {
        screen.id: PromptQueue(
            screen.meta_prompt,
            prompts_config.base.queue_size,
            prompts_config.base.refill_when_below,
            expander,
        )
        for screen in prompts_config.screens
    }
    screen_dims = {screen.id: (screen.rect.width, screen.rect.height) for screen in layout_config.screens}

    output_dir = APP_ROOT / "output"

    while True:
        screen_id = random.choice(list(queues.keys()))
        prompt = queues[screen_id].pop()
        width, height = compute_target_resolution(*screen_dims[screen_id])

        try:
            image_bytes = generator.generate(prompt, width, height)
        except Exception:
            logger.exception("Flux generation failed for screen %s; skipping cycle", screen_id)
            continue

        try:
            push_with_retry(http_client, screen_id, image_bytes)
        except Exception:
            logger.exception("Failed to push image for screen %s; skipping cycle", screen_id)
            continue

        save_and_prune(output_dir / screen_id, _timestamp_filename(), image_bytes, keep=HISTORY_KEEP)

        try:
            wall_bytes = take_screenshot(http_client)
            save_and_prune(output_dir / "wall", _timestamp_filename(), wall_bytes, keep=HISTORY_KEEP)
        except Exception:
            logger.exception("Screenshot capture failed; continuing")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest apps/flux-gallery/tests/test_worker.py -v`
Expected: 3 passed.

Run the full app test suite: `uv run pytest apps/flux-gallery/tests/ -v`
Expected: all tests from Tasks 1–8 passing (resolution, config, prompt_queue, gemini_expander, layout_driver_client, disk_history, worker).

- [ ] **Step 5: Create `run.sh`**

Create `apps/flux-gallery/run.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

: "${GEMINI_API_KEY:?GEMINI_API_KEY must be set}"
export LAYOUT_DRIVER_URL="${LAYOUT_DRIVER_URL:-https://localhost:8443}"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

cd "$REPO_ROOT"
uv run --extra flux-gallery python -m flux_gallery.worker
```

`PYTHONPATH` is set explicitly (rather than relying on `uv run`'s own working-directory behavior, which this plan doesn't assume one way or the other) so `flux_gallery` resolves as an importable package regardless of where `uv run` ends up executing the command from.

Make it executable: `chmod +x apps/flux-gallery/run.sh`

- [ ] **Step 6: Verify `run.sh` actually finds the package**

```bash
GEMINI_API_KEY=dummy apps/flux-gallery/run.sh &
RUN_PID=$!
sleep 3
```

Expected: the process should get far enough to either start attempting Gemini calls / config loading (visible in stdout/stderr) rather than immediately failing with `ModuleNotFoundError: No module named 'flux_gallery'`. If it does fail with that specific error, the `PYTHONPATH`/`uv run` interaction in Step 5 needs adjusting — investigate with `uv run --extra flux-gallery python -c "import flux_gallery; print(flux_gallery.__file__)"` run from `REPO_ROOT` with `PYTHONPATH` set as above, and fix `run.sh` until that import succeeds, updating this step's code accordingly and noting what you changed and why in your report.

Stop it: `kill $RUN_PID 2>/dev/null || true`

- [ ] **Step 7: Full end-to-end verification against the real framework**

This exercises the entire loop for real: config loading, prompt queue/Gemini (or its fallback), real Flux generation, a real push to a running framework, and a real screenshot call.

Start the real framework server (serving the default, already-fixed `apps/test-pattern` app, which has `enableScreenshotResponder` wired in from the whole-branch review fixes, so `/api/screenshot` will succeed once a browser is connected):

```bash
uv run python -m layout_server.main &
SERVER_PID=$!
sleep 2
```

Load the test-pattern page headlessly with Playwright and keep it open in the background (this is what makes `/api/screenshot` succeed — it needs a connected browser client). Write a small throwaway script for this (don't commit it), e.g. using the pattern from `.superpowers/sdd/task-11-report.md` or similar prior verification scripts in this repo's history if you want a reference, or write your own using `playwright.sync_api`/`async_api` to launch headless chromium, navigate to `https://localhost:8443/` with `ignore_https_errors=True`, and then sleep/block to keep the browser alive while you run the next step from another process.

With that page held open, in another terminal/process run the worker for a limited time. If you have a real `GEMINI_API_KEY`, use it (exercises the primary Gemini path); if not, use an intentionally invalid key (exercises the required fallback-to-`meta_prompt` behavior from Task 3/§4 of the spec — either is a valid, real verification):

```bash
GEMINI_API_KEY=<real-key-or-"invalid-key-for-testing"> apps/flux-gallery/run.sh > /tmp/flux-gallery-run.log 2>&1 &
WORKER_PID=$!
```

Let it run long enough for at least one full cycle to complete — this includes the first-run Flux model download if not already cached from Task 5's verification (reuses the same cache, so should be fast if Task 5 already ran), plus one real inference (seconds) — poll every 10s for up to a few minutes:

```bash
for i in $(seq 1 30); do
  if find apps/flux-gallery/output -name '*.png' 2>/dev/null | grep -q .; then
    echo "output found after ${i}0s"
    break
  fi
  sleep 10
done
```

Then check:

```bash
find apps/flux-gallery/output -name '*.png'
tail -50 /tmp/flux-gallery-run.log
```

Expected: at least one `.png` file under `apps/flux-gallery/output/<some-screen-id>/` (the raw generated image) and at least one under `apps/flux-gallery/output/wall/` (the screenshot). The log should show either successful Gemini expansion or (if using the invalid key) a logged warning about Gemini failing followed by the worker continuing with the `meta_prompt` fallback — either is correct, but confirm which one you saw and report it. No unhandled traceback should have killed the worker process (`ps -p $WORKER_PID` should still show it running, or at minimum the log should show it working through complete cycles, not crashing on the first one).

Clean up:

```bash
kill "$WORKER_PID" 2>/dev/null || true
kill "$SERVER_PID" 2>/dev/null || true
```

Kill your throwaway Playwright script's process too, and delete the throwaway script file. Delete `apps/flux-gallery/output/` (it's gitignored, but clean up the verification artifacts): `rm -rf apps/flux-gallery/output`. Confirm no server/Chromium/worker processes are left running: `ps aux | grep -E "layout_server.main|flux_gallery.worker|chromium" | grep -v grep` should show nothing from this session.

- [ ] **Step 8: Lint and commit**

Run: `uv run ruff format apps/flux-gallery/flux_gallery/worker.py apps/flux-gallery/tests/test_worker.py && uv run ruff check apps/flux-gallery/`

```bash
git add apps/flux-gallery/flux_gallery/worker.py apps/flux-gallery/tests/test_worker.py apps/flux-gallery/run.sh
git commit -m "feat: add flux-gallery worker main loop and run.sh"
```

---

## Self-Review Notes

- **Spec coverage:** §2 (architecture: three collaborators + main loop) → Tasks 3, 4, 5, 8; §3 (config) → Tasks 1–2; §4 (prompt pipeline + fallback) → Task 3; §5 (generation loop: random selection, resolution rounding, push, screenshot) → Tasks 1, 8; §6 (disk history + retention) → Task 7; §7 (credentials/run.sh) → Task 8; §8 (error handling: Gemini fallback, Flux error skip, push retry-then-skip, screenshot skip) → Tasks 3, 8; §9 (testing: resolution + PromptQueue unit tests, manual full-loop verification) → Tasks 1, 3, 8.
- **Interface consistency check:** `PromptQueue`'s constructor parameter order (`meta_prompt, queue_size, refill_when_below, expander`) is used identically in Task 3's tests and Task 8's `worker.py`. `push_image`/`take_screenshot`'s signatures (Task 6) are used identically in Task 8's `worker.py` and in Task 8's test's `patch("flux_gallery.worker.push_image", ...)`. `compute_target_resolution`'s parameter order (`screen_width, screen_height`) matches its one call site in `worker.py` (`compute_target_resolution(*screen_dims[screen_id])`, where `screen_dims` stores `(width, height)` tuples).
- **No placeholder scan:** no TBD/TODO markers; the one genuinely untestable-by-automation piece (Task 5's real Flux inference, Task 4's real Gemini call, Task 8's full loop) is handled with concrete manual verification steps and explicit instructions to report honestly if the environment can't support it, not silently skipped.
