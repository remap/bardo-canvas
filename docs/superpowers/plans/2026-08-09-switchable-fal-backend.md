# Switchable FAL Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `flux_gallery.worker` generate images through either the existing
local `diffusers` pipeline or a hosted fal.ai endpoint, selected by config or
an env var, with zero behavior change for anyone who doesn't touch either.

**Architecture:** Extract the current `FluxGenerator` class into a
`GenerationBackend` Protocol implementation (`LocalBackend`), add a second
implementation (`FalBackend`) that calls fal.ai's hosted API with bounded
retries, and a small dict-based registry that picks one by name. The worker's
`while True:` loop is unaffected beyond which backend object it calls
`.generate()` on.

**Tech Stack:** Python 3.13, `pydantic` (config), `fal-client>=0.5.0` (new
dependency, hosted-backend HTTP calls), `httpx` (already a dependency, used to
download the generated image), `pytest` + `unittest.mock` (testing).

## Global Constraints

- Scope is `apps/flux-gallery/` only, plus one line in the root
  `pyproject.toml` (the repo has no per-app dependency manifest — every
  flux-gallery dependency, including this new one, is declared in the root
  file's `flux-gallery` extra).
- No img2img/ControlNet/style-transfer/mesh generation, no concurrent or
  batched generation — the worker stays a single-threaded, sequential loop.
  `GenerationBackend` has exactly one method: `generate(prompt, width,
  height) -> bytes`.
- Default behavior is unchanged: `backend: "local"` is `BaseGenerationConfig`'s
  default, so no existing deployment's behavior changes until an operator
  opts in.
- `LocalBackend` is a pure move from the current `FluxGenerator` — same
  constructor signature (`model: str, num_inference_steps: int`), same
  `generate()` body byte-for-byte (device resolution, MPS `empty_cache()`
  call, docstring), only the class name changes.
- Full test suite (`uv run --extra flux-gallery pytest -q` from the repo
  root) must pass after every task.

---

### Task 1: Add `backend`/`fal_endpoint` config fields

**Files:**
- Modify: `apps/flux-gallery/flux_gallery/config.py`
- Modify: `apps/flux-gallery/config/prompts.yaml`
- Modify: `apps/flux-gallery/tests/test_config.py`

**Interfaces:**
- Produces: `BaseGenerationConfig.backend: Literal["local", "fal"] = "local"`
  and `BaseGenerationConfig.fal_endpoint: str = "fal-ai/flux/schnell"` — later
  tasks (`backend_registry.py`, `worker.py`) read both fields off a
  `BaseGenerationConfig` instance.

- [ ] **Step 1: Write the failing test**

Add to `apps/flux-gallery/tests/test_config.py`, after
`test_base_config_defaults_match_prompts_yaml`:

```python
def test_base_config_backend_defaults_match_prompts_yaml():
    config = load_prompts_config(PROMPTS_YAML)
    assert config.base.backend == "local"
    assert config.base.fal_endpoint == "fal-ai/flux/schnell"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/flux-gallery && uv run --extra flux-gallery pytest tests/test_config.py -v` (from the repo root)

Expected: FAIL — `AttributeError: 'BaseGenerationConfig' object has no attribute 'backend'`

- [ ] **Step 3: Add the fields to `BaseGenerationConfig`**

In `apps/flux-gallery/flux_gallery/config.py`, add the `Literal` import and
the two new fields:

```python
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
```

(Only the `from typing import Literal` import and the two new field lines are
new; every other line is unchanged.)

- [ ] **Step 4: Document both fields in `prompts.yaml`**

In `apps/flux-gallery/config/prompts.yaml`, replace the `base:` block:

```yaml
base:
  model: "black-forest-labs/FLUX.1-schnell"   # local backend only
  gemini_model: "gemini-flash-latest"
  backend: "local"                             # "local" | "fal"
  fal_endpoint: "fal-ai/flux/schnell"           # fal backend only
  num_inference_steps: 4
  queue_size: 5
  refill_when_below: 2
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run --extra flux-gallery pytest apps/flux-gallery/tests/test_config.py -v` (from the repo root)

Expected: PASS, including the pre-existing `test_base_config_defaults_match_prompts_yaml`

- [ ] **Step 6: Run the full test suite**

Run: `uv run --extra flux-gallery pytest -q` (from the repo root)

Expected: all pass — this task only adds fields with defaults, nothing else
reads them yet.

- [ ] **Step 7: Commit**

```bash
git add apps/flux-gallery/flux_gallery/config.py apps/flux-gallery/config/prompts.yaml apps/flux-gallery/tests/test_config.py
git commit -m "flux-gallery: add backend/fal_endpoint config fields"
```

---

### Task 2: Move `FluxGenerator` into `backends/local.py` as `LocalBackend`

**Files:**
- Create: `apps/flux-gallery/flux_gallery/backends/__init__.py`
- Create: `apps/flux-gallery/flux_gallery/backends/base.py`
- Create: `apps/flux-gallery/flux_gallery/backends/local.py`
- Delete: `apps/flux-gallery/flux_gallery/flux_generator.py`
- Create: `apps/flux-gallery/tests/backends/__init__.py`
- Create: `apps/flux-gallery/tests/backends/test_local.py`
- Delete: `apps/flux-gallery/tests/test_flux_generator.py`
- Modify: `apps/flux-gallery/flux_gallery/worker.py`

**Interfaces:**
- Produces: `flux_gallery.backends.base.GenerationBackend` (a `Protocol` with
  one method, `generate(self, prompt: str, width: int, height: int) ->
  bytes`) and `flux_gallery.backends.local.LocalBackend` (constructor
  `__init__(self, model: str, num_inference_steps: int)`, method
  `generate(self, prompt: str, width: int, height: int) -> bytes`) — later
  tasks (`backend_registry.py`) import both.
- Consumes: nothing new — this task only relocates existing code.

This task is a pure move: `LocalBackend`'s body is `FluxGenerator`'s body,
unchanged, with the class renamed. `worker.py` is updated to import from the
new location so the app keeps working exactly as before — it does not yet
gain backend selection (that's Task 5).

- [ ] **Step 1: Create the `backends` package and the `GenerationBackend` Protocol**

Create `apps/flux-gallery/flux_gallery/backends/__init__.py` (empty file).

Create `apps/flux-gallery/flux_gallery/backends/base.py`:

```python
from __future__ import annotations

from typing import Protocol


class GenerationBackend(Protocol):
    def generate(self, prompt: str, width: int, height: int) -> bytes: ...
```

- [ ] **Step 2: Move `flux_generator.py`'s content into `backends/local.py`**

Create `apps/flux-gallery/flux_gallery/backends/local.py` with exactly the
current contents of `apps/flux-gallery/flux_gallery/flux_generator.py`, with
only the class renamed from `FluxGenerator` to `LocalBackend`:

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


class LocalBackend:
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
```

Delete `apps/flux-gallery/flux_gallery/flux_generator.py`:

```bash
git rm apps/flux-gallery/flux_gallery/flux_generator.py
```

- [ ] **Step 3: Move the test file, renaming `FluxGenerator` references to `LocalBackend`**

Create `apps/flux-gallery/tests/backends/__init__.py` (empty file, matching
the existing `apps/flux-gallery/tests/__init__.py` package layout).

Create `apps/flux-gallery/tests/backends/test_local.py` with the current
contents of `apps/flux-gallery/tests/test_flux_generator.py`, renaming
`FluxGenerator` to `LocalBackend` and updating the module path:

```python
from unittest.mock import MagicMock

import pytest

# LocalBackend imports torch and diffusers at module scope, so without the
# flux-gallery extra installed this file would fail at collection with a raw
# ModuleNotFoundError rather than a readable skip.
pytest.importorskip("torch")
pytest.importorskip("diffusers")

import torch

from flux_gallery.backends.local import LocalBackend


class _FakeImage:
    def save(self, buffer, format):  # noqa: A002 - matches PIL.Image.save's signature
        buffer.write(b"fake-png-bytes")


def _make_backend(monkeypatch, *, mps_available: bool) -> LocalBackend:
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: mps_available)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    fake_pipeline = MagicMock()
    fake_pipeline.return_value = MagicMock(images=[_FakeImage()])
    monkeypatch.setattr(
        "flux_gallery.backends.local.FluxPipeline.from_pretrained",
        lambda *args, **kwargs: fake_pipeline,
    )

    return LocalBackend(model="fake-model", num_inference_steps=1)


def test_generate_clears_mps_cache_after_each_call_on_mps(monkeypatch):
    backend = _make_backend(monkeypatch, mps_available=True)
    empty_cache = MagicMock()
    monkeypatch.setattr(torch.mps, "empty_cache", empty_cache)

    backend.generate("a prompt", 1600, 400)

    empty_cache.assert_called_once()


def test_generate_does_not_touch_mps_cache_on_cpu(monkeypatch):
    backend = _make_backend(monkeypatch, mps_available=False)
    empty_cache = MagicMock()
    monkeypatch.setattr(torch.mps, "empty_cache", empty_cache)

    backend.generate("a prompt", 1600, 400)

    empty_cache.assert_not_called()
```

Delete the old test file:

```bash
git rm apps/flux-gallery/tests/test_flux_generator.py
```

- [ ] **Step 4: Update `worker.py`'s import and instantiation**

`worker.py`'s local imports are alphabetically sorted; `.backends.local`
sorts before `.config`, not in the old `.flux_generator` line's position, so
this moves the import rather than replacing it in place. In
`apps/flux-gallery/flux_gallery/worker.py`, change:

```python
from .config import PromptsConfig, load_prompts_config
from .disk_history import save_and_prune
from .flux_generator import FluxGenerator
from .gemini_expander import GeminiExpander
from .layout_driver_client import push_image, take_screenshot
from .prompt_queue import PromptQueue
from .resolution import compute_target_resolution
```

to:

```python
from .backends.local import LocalBackend
from .config import PromptsConfig, load_prompts_config
from .disk_history import save_and_prune
from .gemini_expander import GeminiExpander
from .layout_driver_client import push_image, take_screenshot
from .prompt_queue import PromptQueue
from .resolution import compute_target_resolution
```

and change:

```python
    generator = FluxGenerator(
        model=prompts_config.base.model,
        num_inference_steps=prompts_config.base.num_inference_steps,
    )
```

to:

```python
    generator = LocalBackend(
        model=prompts_config.base.model,
        num_inference_steps=prompts_config.base.num_inference_steps,
    )
```

This is an intermediate state — `worker.py` always uses `LocalBackend`
directly until Task 5 wires in backend selection. Behavior is identical to
before this task.

- [ ] **Step 5: Run the moved tests**

Run: `uv run --extra flux-gallery pytest apps/flux-gallery/tests/backends/test_local.py -v` (from the repo root)

Expected: PASS (2 tests, same as the old `test_flux_generator.py`)

- [ ] **Step 6: Run the full test suite**

Run: `uv run --extra flux-gallery pytest -q` (from the repo root)

Expected: all pass, same total test count as before this task (2 tests moved,
none added or removed net).

- [ ] **Step 7: Commit**

`git rm` (Steps 2–3) already staged both deletions, so this only needs the
new/modified files added:

```bash
git add apps/flux-gallery/flux_gallery/backends/__init__.py \
        apps/flux-gallery/flux_gallery/backends/base.py \
        apps/flux-gallery/flux_gallery/backends/local.py \
        apps/flux-gallery/flux_gallery/worker.py \
        apps/flux-gallery/tests/backends/__init__.py \
        apps/flux-gallery/tests/backends/test_local.py
git commit -m "flux-gallery: move FluxGenerator to backends/local.py as LocalBackend"
```

---

### Task 3: Add `FalBackend`

**Files:**
- Create: `apps/flux-gallery/flux_gallery/backends/fal.py`
- Create: `apps/flux-gallery/tests/backends/test_fal.py`
- Modify: `pyproject.toml` (repo root)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `flux_gallery.backends.fal.FalBackend` (constructor
  `__init__(self, endpoint: str, num_inference_steps: int)`, method
  `generate(self, prompt: str, width: int, height: int) -> bytes`) — Task 4's
  `backend_registry.py` imports this.

Confirmed by direct inspection of the installed `fal-client` 1.0.0 package
(the version this floor currently resolves to): `fal_client.submit(application:
str, arguments: AnyJSON, ...)` returns a handle whose `.get()` blocks and
returns the result dict; HTTP errors raise `fal_client.FalClientHTTPError`, a
dataclass with a `.status_code: int` field. Both `submit()` and `.get()` sit
inside one `try` block below, so retry logic is correct regardless of which
call actually raises.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml` (repo root), add `fal-client>=0.5.0` to the
`flux-gallery` extra:

```toml
[project.optional-dependencies]
flux-gallery = [
    "google-genai>=1.0",
    "diffusers>=0.31",
    "transformers>=4.46",
    "accelerate>=1.1",
    "sentencepiece>=0.2",
    "torch>=2.5",
    "fal-client>=0.5.0",
]
```

Run: `uv sync --extra flux-gallery` (from the repo root)

Expected: resolves and installs `fal-client` alongside the existing
flux-gallery dependencies.

- [ ] **Step 2: Write the failing tests**

Create `apps/flux-gallery/tests/backends/test_fal.py`:

```python
from unittest.mock import MagicMock, patch

import pytest

# FalBackend imports fal_client at module scope, so without the flux-gallery
# extra installed this file would fail at collection with a raw
# ModuleNotFoundError rather than a readable skip.
pytest.importorskip("fal_client")

import fal_client

from flux_gallery.backends.fal import FalBackend


def _http_error(status_code: int, message: str) -> fal_client.FalClientHTTPError:
    return fal_client.FalClientHTTPError(
        message=message, status_code=status_code, response_headers={}, response=MagicMock()
    )


def _handle_returning(result: dict) -> MagicMock:
    handle = MagicMock()
    handle.get.return_value = result
    return handle


def test_generate_returns_downloaded_image_bytes_on_success():
    backend = FalBackend(endpoint="fal-ai/flux/schnell", num_inference_steps=4)
    handle = _handle_returning({"images": [{"url": "https://fal.example/image.png"}]})

    with (
        patch("flux_gallery.backends.fal.fal_client.submit", return_value=handle) as submit,
        patch("flux_gallery.backends.fal.httpx.get") as get,
    ):
        get.return_value = MagicMock(content=b"image-bytes")
        result = backend.generate("a prompt", 1600, 400)

    assert result == b"image-bytes"
    submit.assert_called_once_with(
        "fal-ai/flux/schnell",
        arguments={
            "prompt": "a prompt",
            "image_size": {"width": 1600, "height": 400},
            "num_inference_steps": 4,
            "num_images": 1,
        },
    )
    get.assert_called_once_with("https://fal.example/image.png", timeout=30.0)


def test_generate_retries_once_after_a_single_rate_limit_error(monkeypatch):
    backend = FalBackend(endpoint="fal-ai/flux/schnell", num_inference_steps=4)
    handle = _handle_returning({"images": [{"url": "https://fal.example/image.png"}]})
    monkeypatch.setattr("flux_gallery.backends.fal.time.sleep", lambda seconds: None)

    calls = []

    def flaky_submit(endpoint, arguments):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, "Too Many Requests")
        return handle

    with (
        patch("flux_gallery.backends.fal.fal_client.submit", side_effect=flaky_submit),
        patch("flux_gallery.backends.fal.httpx.get") as get,
    ):
        get.return_value = MagicMock(content=b"image-bytes")
        result = backend.generate("a prompt", 1600, 400)

    assert result == b"image-bytes"
    assert len(calls) == 2


def test_generate_retries_after_a_billing_lock_error(monkeypatch):
    backend = FalBackend(endpoint="fal-ai/flux/schnell", num_inference_steps=4)
    handle = _handle_returning({"images": [{"url": "https://fal.example/image.png"}]})
    monkeypatch.setattr("flux_gallery.backends.fal.time.sleep", lambda seconds: None)

    calls = []

    def flaky_submit(endpoint, arguments):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(400, "Your account balance is exhausted")
        return handle

    with (
        patch("flux_gallery.backends.fal.fal_client.submit", side_effect=flaky_submit),
        patch("flux_gallery.backends.fal.httpx.get") as get,
    ):
        get.return_value = MagicMock(content=b"image-bytes")
        result = backend.generate("a prompt", 1600, 400)

    assert result == b"image-bytes"
    assert len(calls) == 2


def test_generate_raises_after_exhausting_rate_limit_retries(monkeypatch):
    backend = FalBackend(endpoint="fal-ai/flux/schnell", num_inference_steps=4)
    monkeypatch.setattr("flux_gallery.backends.fal.time.sleep", lambda seconds: None)

    def always_rate_limited(endpoint, arguments):
        raise _http_error(429, "Too Many Requests")

    with (
        patch("flux_gallery.backends.fal.fal_client.submit", side_effect=always_rate_limited),
        pytest.raises(fal_client.FalClientHTTPError),
    ):
        backend.generate("a prompt", 1600, 400)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run --extra flux-gallery pytest apps/flux-gallery/tests/backends/test_fal.py -v` (from the repo root)

Expected: FAIL — `ModuleNotFoundError: No module named 'flux_gallery.backends.fal'`

- [ ] **Step 4: Implement `FalBackend`**

Create `apps/flux-gallery/flux_gallery/backends/fal.py`:

```python
from __future__ import annotations

import time

import fal_client
import httpx

# Rate limit (HTTP 429): a handful of quick retries clears most transient
# throttling without burning the whole cycle's Gemini-expanded prompt.
FAL_MAX_RETRIES = 5
FAL_RATE_LIMIT_BACKOFF_BASE = 2.0

# Billing lock (out-of-credit account): fal.ai's wording varies but always
# includes one of these words. Backoff starts much higher and caps lower,
# since a billing lock doesn't clear itself in seconds the way a rate limit
# does -- it clears when the operator tops up the account.
FAL_BILLING_MAX_RETRIES = 6
FAL_BILLING_BACKOFF_BASE = 30.0
FAL_BILLING_BACKOFF_MAX = 300.0

_BILLING_LOCK_MARKERS = ("locked", "exhausted", "balance", "suspended")


def _is_billing_lock(exc: fal_client.FalClientHTTPError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _BILLING_LOCK_MARKERS)


class FalBackend:
    def __init__(self, endpoint: str, num_inference_steps: int) -> None:
        self._endpoint = endpoint
        self._num_inference_steps = num_inference_steps

    def generate(self, prompt: str, width: int, height: int) -> bytes:
        result = self._submit_with_retry(prompt, width, height)
        image_url = result["images"][0]["url"]
        return self._download(image_url)

    def _submit_with_retry(self, prompt: str, width: int, height: int) -> dict:
        # width/height arrive already rounded to a multiple of 16 by
        # resolution.compute_target_resolution, so no rounding happens here.
        arguments = {
            "prompt": prompt,
            "image_size": {"width": width, "height": height},
            "num_inference_steps": self._num_inference_steps,
            "num_images": 1,
        }
        rate_limit_attempts = 0
        billing_attempts = 0
        while True:
            try:
                handle = fal_client.submit(self._endpoint, arguments=arguments)
                return handle.get()
            except fal_client.FalClientHTTPError as exc:
                if exc.status_code == 429 and rate_limit_attempts < FAL_MAX_RETRIES:
                    rate_limit_attempts += 1
                    time.sleep(FAL_RATE_LIMIT_BACKOFF_BASE * (2 ** (rate_limit_attempts - 1)))
                    continue
                if _is_billing_lock(exc) and billing_attempts < FAL_BILLING_MAX_RETRIES:
                    billing_attempts += 1
                    backoff = min(
                        FAL_BILLING_BACKOFF_BASE * (2 ** (billing_attempts - 1)),
                        FAL_BILLING_BACKOFF_MAX,
                    )
                    time.sleep(backoff)
                    continue
                raise

    def _download(self, image_url: str) -> bytes:
        response = httpx.get(image_url, timeout=30.0)
        response.raise_for_status()
        return response.content
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --extra flux-gallery pytest apps/flux-gallery/tests/backends/test_fal.py -v` (from the repo root)

Expected: PASS (4 tests)

- [ ] **Step 6: Run the full test suite**

Run: `uv run --extra flux-gallery pytest -q` (from the repo root)

Expected: all pass, 4 new tests.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock apps/flux-gallery/flux_gallery/backends/fal.py apps/flux-gallery/tests/backends/test_fal.py
git commit -m "flux-gallery: add FalBackend with rate-limit and billing-lock retries"
```

---

### Task 4: Add `backend_registry.py`

**Files:**
- Create: `apps/flux-gallery/flux_gallery/backend_registry.py`
- Create: `apps/flux-gallery/tests/test_backend_registry.py`

**Interfaces:**
- Consumes: `flux_gallery.backends.local.LocalBackend` (Task 2),
  `flux_gallery.backends.fal.FalBackend` (Task 3),
  `flux_gallery.config.BaseGenerationConfig` (Task 1, fields `model`,
  `num_inference_steps`, `fal_endpoint`).
- Produces: `create_backend(name: str, base_config: BaseGenerationConfig) ->
  GenerationBackend` — Task 5's `worker.py` calls this instead of
  instantiating `LocalBackend` directly.

- [ ] **Step 1: Write the failing tests**

Create `apps/flux-gallery/tests/test_backend_registry.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra flux-gallery pytest apps/flux-gallery/tests/test_backend_registry.py -v` (from the repo root)

Expected: FAIL — `ModuleNotFoundError: No module named 'flux_gallery.backend_registry'`

- [ ] **Step 3: Implement `backend_registry.py`**

Create `apps/flux-gallery/flux_gallery/backend_registry.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra flux-gallery pytest apps/flux-gallery/tests/test_backend_registry.py -v` (from the repo root)

Expected: PASS (3 tests)

- [ ] **Step 5: Run the full test suite**

Run: `uv run --extra flux-gallery pytest -q` (from the repo root)

Expected: all pass, 3 new tests.

- [ ] **Step 6: Commit**

```bash
git add apps/flux-gallery/flux_gallery/backend_registry.py apps/flux-gallery/tests/test_backend_registry.py
git commit -m "flux-gallery: add backend_registry.create_backend"
```

---

### Task 5: Wire backend selection into `worker.py`

**Files:**
- Modify: `apps/flux-gallery/flux_gallery/worker.py`
- Modify: `apps/flux-gallery/tests/test_worker.py`

**Interfaces:**
- Consumes: `create_backend` (Task 4), `BaseGenerationConfig` (Task 1).
- Produces: `_resolve_backend_name(env: dict[str, str], base_config:
  BaseGenerationConfig) -> str` and `_validate_backend_selection(backend_name:
  str, env: dict[str, str]) -> None` — this task is the only consumer, but
  both are module-level functions in `worker.py`, directly testable the same
  way `_validate_screen_ids` already is.

- [ ] **Step 1: Write the failing tests**

In `apps/flux-gallery/tests/test_worker.py`, change the import lines:

```python
from flux_gallery.config import PromptsConfig, ScreenPromptConfig
from flux_gallery.worker import _validate_screen_ids, push_with_retry
```

to:

```python
from flux_gallery.config import BaseGenerationConfig, PromptsConfig, ScreenPromptConfig
from flux_gallery.worker import (
    _resolve_backend_name,
    _validate_backend_selection,
    _validate_screen_ids,
    push_with_retry,
)
```

Add these tests at the end of the file:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra flux-gallery pytest apps/flux-gallery/tests/test_worker.py -v` (from the repo root)

Expected: FAIL — `ImportError: cannot import name '_resolve_backend_name' from 'flux_gallery.worker'`

- [ ] **Step 3: Add the two functions and wire them into `run_forever()`**

In `apps/flux-gallery/flux_gallery/worker.py`, change the import block from
(the state Task 2 left it in):

```python
from layout_server.config import LayoutConfig, load_layout_config

from .backends.local import LocalBackend
from .config import PromptsConfig, load_prompts_config
from .disk_history import save_and_prune
from .gemini_expander import GeminiExpander
from .layout_driver_client import push_image, take_screenshot
from .prompt_queue import PromptQueue
from .resolution import compute_target_resolution
```

to:

```python
from layout_server.config import LayoutConfig, load_layout_config

from .backend_registry import create_backend
from .config import BaseGenerationConfig, PromptsConfig, load_prompts_config
from .disk_history import save_and_prune
from .gemini_expander import GeminiExpander
from .layout_driver_client import push_image, take_screenshot
from .prompt_queue import PromptQueue
from .resolution import compute_target_resolution
```

(`LocalBackend` is no longer imported directly by `worker.py` — the registry
owns that now. `.backend_registry` sorts before `.backends.local` did, so
this is again a same-position replacement, not a move.)

Add these two functions after `_validate_screen_ids` and before
`run_forever`:

```python
def _resolve_backend_name(env: dict[str, str], base_config: BaseGenerationConfig) -> str:
    return env.get("FLUX_BACKEND") or base_config.backend


def _validate_backend_selection(backend_name: str, env: dict[str, str]) -> None:
    """Fail at boot on a fal backend selection with no FAL_KEY set.

    Without this, a missing key only surfaces as a FalBackend.generate()
    exception buried inside the worker's per-cycle catch-all on the first
    iteration.
    """
    if backend_name == "fal" and "FAL_KEY" not in env:
        raise ValueError("backend 'fal' is selected but FAL_KEY is not set in the environment")
```

In `run_forever()`, change:

```python
    layout_config = load_layout_config(screens_yaml)
    prompts_config = load_prompts_config(prompts_yaml)
    _validate_screen_ids(prompts_config, layout_config)

    expander = GeminiExpander(api_key=gemini_api_key, model=prompts_config.base.gemini_model)
    generator = LocalBackend(
        model=prompts_config.base.model,
        num_inference_steps=prompts_config.base.num_inference_steps,
    )
```

to:

```python
    layout_config = load_layout_config(screens_yaml)
    prompts_config = load_prompts_config(prompts_yaml)
    _validate_screen_ids(prompts_config, layout_config)

    env = dict(os.environ)
    backend_name = _resolve_backend_name(env, prompts_config.base)
    _validate_backend_selection(backend_name, env)

    expander = GeminiExpander(api_key=gemini_api_key, model=prompts_config.base.gemini_model)
    generator = create_backend(backend_name, prompts_config.base)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra flux-gallery pytest apps/flux-gallery/tests/test_worker.py -v` (from the repo root)

Expected: PASS, including the 5 new tests and all pre-existing ones in this file.

- [ ] **Step 5: Run the full test suite**

Run: `uv run --extra flux-gallery pytest -q` (from the repo root)

Expected: all pass, 5 new tests.

- [ ] **Step 6: Live sanity check (no live fal.ai call, backend defaults to local)**

Run: `uv run --extra flux-gallery python -c "from flux_gallery.worker import _resolve_backend_name, _validate_backend_selection; from flux_gallery.config import BaseGenerationConfig; print(_resolve_backend_name({}, BaseGenerationConfig())); _validate_backend_selection('fal', {})"` (from `apps/flux-gallery/`)

Expected: prints `local`, then raises `ValueError: backend 'fal' is selected but FAL_KEY is not set in the environment`

- [ ] **Step 7: Commit**

```bash
git add apps/flux-gallery/flux_gallery/worker.py apps/flux-gallery/tests/test_worker.py
git commit -m "flux-gallery: wire FLUX_BACKEND/backend config into worker.run_forever"
```

---

## Self-Review

**Spec coverage:**
- Architecture / file layout (`backends/base.py`, `backends/local.py`,
  `backends/fal.py`, `backend_registry.py`, `config.py` fields, `worker.py`
  changes) — Tasks 1, 2, 3, 4, 5.
- `flux_generator.py` deleted, contents moved unchanged — Task 2.
- `GenerationBackend` Protocol, one method, no extra fields — Task 2, Step 1.
- `FalBackend`'s retry conditions and constants (rate limit, billing lock) —
  Task 3, Step 4, verified against the actual installed `fal-client` API
  rather than assumed.
- `backend_registry.py`'s dict-based registry — Task 4.
- `BaseGenerationConfig` fields + `prompts.yaml` documentation — Task 1.
- `FLUX_BACKEND` env override + fail-fast `FAL_KEY` check — Task 5.
- `fal-client>=0.5.0` dependency in root `pyproject.toml` — Task 3, Step 1.
- Testing section: `tests/backends/test_local.py` (Task 2), `tests/backends/test_fal.py`
  (Task 3), `tests/test_backend_registry.py` (Task 4), `tests/test_config.py`
  extension (Task 1), `tests/test_worker.py` extension (Task 5) — all covered.
- Rollout (no migration, default unaffected) — true by construction: every
  new field has a default matching current behavior, and `run_forever()`
  only changes which backend object it constructs.

No gaps found.

**Placeholder scan:** No TBD/TODO; every code block is complete, runnable
code with concrete values, not descriptions of code.

**Type consistency:** `GenerationBackend.generate(self, prompt: str, width:
int, height: int) -> bytes` (Task 2) matches `LocalBackend.generate` (Task 2)
and `FalBackend.generate` (Task 3) exactly. `create_backend(name: str,
base_config: BaseGenerationConfig) -> GenerationBackend` (Task 4) is called
in Task 5 with the exact same argument order and types
(`create_backend(backend_name, prompts_config.base)`, where `backend_name` is
the `str` `_resolve_backend_name` returns and `prompts_config.base` is a
`BaseGenerationConfig`).

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-09-switchable-fal-backend.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
