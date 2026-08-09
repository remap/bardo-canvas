# Switchable FAL backend for flux-gallery

Date: 2026-08-09
Status: Approved, ready for implementation planning
Scope: `apps/flux-gallery/` only, plus one line in the root `pyproject.toml` (see
[Dependency](#dependency))

## Problem

`flux_gallery.worker` generates every image through one concrete class,
`FluxGenerator` (`flux_gallery/flux_generator.py`), which loads
`black-forest-labs/FLUX.1-schnell` via `diffusers.FluxPipeline` and runs it on
whatever local accelerator is available (MPS, CUDA, or CPU). There is no way to
generate images anywhere else — e.g. via [fal.ai](https://fal.ai)'s hosted FLUX
endpoints — without editing `worker.py` directly.

[remap/gentree](https://github.com/remap/gentree) (branch `v2_10`) solves the
same problem — one call site needing to run either a local diffusers pipeline
or fal.ai — with a small backend abstraction: `backends/base.py` defines the
interface, `backends/local.py` and `backends/fal.py` implement it,
`backend_registry.py` maps a name to a class, and a config field (overridable
by a CLI flag) picks which one runs. This design ports that shape to
flux-gallery, scaled down to match flux-gallery's actual concurrency model —
gentree's DAG dispatcher needs async submit()/collect() handles and a
ready-queue to run many nodes in parallel; flux-gallery's worker is a plain
`while True:` loop generating one image at a time, so its backends only need a
synchronous `generate()` call.

## Non-goals

- No img2img, ControlNet, style-transfer, or mesh generation — flux-gallery
  only ever does text2img. gentree's abstraction covers many modes; this one
  covers exactly the one flux-gallery uses.
- No concurrent/batched generation. The worker loop stays single-threaded and
  sequential; adding a third backend or concurrent dispatch later is out of
  scope here.
- No changes to `layout_server`, other apps, or anything outside
  `apps/flux-gallery/` except the one dependency line noted below.

## Architecture

```
flux_gallery/
  backends/
    __init__.py
    base.py               # GenerationBackend Protocol
    local.py               # LocalBackend (moved from flux_generator.py)
    fal.py                  # FalBackend
  backend_registry.py       # create_backend(name, base_config) -> GenerationBackend
  config.py                 # + backend, fal_endpoint fields on BaseGenerationConfig
  worker.py                 # resolves backend name, fails fast if FAL_KEY missing
```

`flux_generator.py` is deleted; its contents move into `backends/local.py`
unchanged — same device resolution (`_resolve_device`), same MPS
`empty_cache()` call after each generation, same constructor signature. This
is a move, not a rewrite: no behavior changes for the existing local path.

### `backends/base.py`

```python
from __future__ import annotations

from typing import Protocol


class GenerationBackend(Protocol):
    def generate(self, prompt: str, width: int, height: int) -> bytes: ...
```

One method, matching the return type `FluxGenerator.generate()` already has
today (PNG bytes). No `SubmitHandle`, no `GenerationResult` dataclass, no
`cost_usd`/`elapsed_s`/`metadata` fields — gentree needs those for its
concurrent dispatcher's bookkeeping (inflight counters, cost estimation
across a batch); flux-gallery's sequential loop has no batch to account for,
and adding that structure now would be speculative.

### `backends/local.py`

`LocalBackend` — `FluxGenerator` renamed. Same constructor
(`model: str, num_inference_steps: int`), same `generate()` body, same
docstring/comment about MPS's per-shape caching allocator. Verified via git
diff to be a pure move (no line changes beyond the class name and the
module docstring line) during implementation.

### `backends/fal.py`

`FalBackend` wraps the `fal_client` SDK for one fal.ai endpoint (configured
per [Config & selection](#config--selection)).

```python
class FalBackend:
    def __init__(self, endpoint: str, num_inference_steps: int) -> None:
        self._endpoint = endpoint
        self._num_inference_steps = num_inference_steps

    def generate(self, prompt: str, width: int, height: int) -> bytes:
        result = self._submit_with_retry(prompt, width, height)
        image_url = result["images"][0]["url"]
        return self._download(image_url)
```

`_submit_with_retry` calls `fal_client.submit(self._endpoint, arguments={...})`
then blocks on the handle's result (`.get()`), retrying on two conditions
ported from gentree's `fal_backend.py`:

- **Rate limit (HTTP 429):** up to 5 retries, exponential backoff starting at
  2s and doubling each attempt (`FAL_MAX_RETRIES = 5`,
  `FAL_RATE_LIMIT_BACKOFF_BASE = 2.0`).
- **Billing lock** (error message containing "locked", "exhausted",
  "balance", or "suspended" — fal.ai's wording for an out-of-credit account):
  up to 6 retries, backoff starting at 30s and doubling up to a 300s cap
  (`FAL_BILLING_MAX_RETRIES = 6`, `FAL_BILLING_BACKOFF_BASE = 30.0`,
  `FAL_BILLING_BACKOFF_MAX = 300.0`).

Both retry loops are bounded — after exhausting retries, `generate()` raises,
and the exception propagates up to `worker.py`'s existing per-cycle
`try/except` around `generator.generate(...)`, which logs, sleeps 1s, and
moves to the next cycle. No new catch-all is introduced in the worker; the
retry logic here only exists to avoid burning a whole cycle (and its
Gemini-expanded prompt) on a transient 429 that a few seconds of backoff would
have cleared.

`arguments` submitted to the endpoint:
```python
{
    "prompt": prompt,
    "image_size": {"width": width, "height": height},
    "num_inference_steps": self._num_inference_steps,
    "num_images": 1,
}
```
`width`/`height` arrive already rounded to a multiple of 16 by
`resolution.compute_target_resolution`, so no additional rounding is needed
before submission.

`_download(image_url)` fetches the image bytes via a plain `httpx.get(...)`
call and returns `.content` — fal.ai returns a URL to the generated image
rather than inline bytes, unlike the local pipeline which returns a PIL image
directly.

### `backend_registry.py`

```python
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

A dict-based registry (rather than an `if`/`elif` chain) mirrors gentree's
`backend_registry.py` `register()`/`get_backend()` shape closely enough to
keep the two codebases legible side by side, without importing gentree's
`**kwargs`-passthrough registration API — flux-gallery only ever has two
backends with different constructor arguments, so each branch builds its own
backend explicitly rather than forcing both constructors into one generic
kwargs shape.

## Config & selection

`BaseGenerationConfig` (`flux_gallery/config.py`) gets two new fields:

```python
class BaseGenerationConfig(BaseModel):
    model: str = "black-forest-labs/FLUX.1-schnell"
    gemini_model: str = "gemini-flash-latest"
    num_inference_steps: int = 4
    queue_size: int = 5
    refill_when_below: int = 2
    backend: Literal["local", "fal"] = "local"
    fal_endpoint: str = "fal-ai/flux/schnell"
```

`prompts.yaml`'s `base` block documents both:
```yaml
base:
  model: "black-forest-labs/FLUX.1-schnell"   # local backend only
  backend: "local"                             # "local" | "fal"
  fal_endpoint: "fal-ai/flux/schnell"           # fal backend only
  num_inference_steps: 4
  queue_size: 5
  refill_when_below: 2
```

Default is `"local"` — unchanged behavior for anyone not touching this config.

`model` and `fal_endpoint` are deliberately independent fields rather than
one field the FAL backend re-derives by string-matching (e.g. sniffing
`"schnell"` out of the HF repo id): `model` is an HF repo id meaningful only
to the local diffusers pipeline, `fal_endpoint` is a fal.ai endpoint path
meaningful only to the FAL backend, and the two naming schemes aren't
guaranteed to line up beyond this one case.

`worker.run_forever()` resolves which backend to construct as:
```python
backend_name = os.environ.get("FLUX_BACKEND") or prompts_config.base.backend
```
`FLUX_BACKEND` (values `"local"` or `"fal"`) overrides the config file when
set — the same override relationship gentree uses between its `--remote` CLI
flag and `meta.remote` in its YAML config, adapted to flux-gallery's
env-var-driven `run.sh` (which has no CLI flags today).

If `backend_name == "fal"` and `FAL_KEY` is not set in the environment,
`run_forever()` raises `ValueError` before constructing anything or entering
the loop — the same fail-fast-at-boot approach `_validate_screen_ids` already
uses for a bad `prompts.yaml` screen id, so a missing key surfaces
immediately and legibly instead of as a `FalBackend.generate()` exception
buried inside the worker's per-cycle catch-all on the first iteration.
`FAL_KEY` itself is never read or plumbed by flux-gallery code beyond this
presence check — `fal_client` reads it from the environment automatically,
same as gentree's `fal_backend.py` relies on.

## Dependency

`fal-client>=0.5.0` (the version gentree pins) is added to the `flux-gallery`
extra in the root `pyproject.toml`:
```toml
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
This is the one required change outside `apps/flux-gallery/` — the repo has
no per-app manifest, so every existing flux-gallery dependency (torch,
diffusers, etc.) is already declared here rather than in the app directory.

Root `README.md`'s flux-gallery section (documenting `GEMINI_API_KEY`,
`HF_TOKEN`, etc.) is a candidate follow-up for documenting `FLUX_BACKEND` and
`FAL_KEY`, but isn't required for this design to be complete and is left to
implementation-time judgment rather than specified here.

## Testing

- `tests/backends/test_local.py` — moved verbatim from the current
  `tests/test_flux_generator.py`, `FluxGenerator` renamed to `LocalBackend`,
  same two tests (MPS cache cleared on MPS, untouched on CPU).
- `tests/backends/test_fal.py` — new. `pytest.importorskip("fal_client")`
  guard, matching the existing `pytest.importorskip("torch")` /
  `pytest.importorskip("diffusers")` pattern used for the flux-gallery extra.
  Covers, all via mocking `fal_client.submit`:
  - a successful call returns the downloaded image bytes;
  - a single 429 followed by success retries once and returns the result;
  - a billing-lock error followed by success retries and returns the result;
  - exhausting retries (429s all the way down) raises.
- `tests/test_backend_registry.py` — new. `create_backend("local", ...)`
  returns a `LocalBackend`; `create_backend("fal", ...)` returns a
  `FalBackend`; an unknown name raises `ValueError` naming the available
  backends (mirroring `test_validate_screen_ids_rejects_an_id_missing_...`'s
  existing "message names what's actually available" convention).
- `tests/test_config.py` — extended: `base.backend` defaults to `"local"`,
  `base.fal_endpoint` defaults to `"fal-ai/flux/schnell"`.
- `tests/test_worker.py` — extended:
  - `FLUX_BACKEND=fal` env var overrides `base.backend: local` in the loaded
    config;
  - no `FLUX_BACKEND` set falls through to `base.backend`;
  - resolved backend `"fal"` with no `FAL_KEY` in the environment raises
    before `run_forever()` reaches its main loop.

## Rollout

No migration needed — `backend: "local"` is the default, so existing
deployments are unaffected until an operator opts in by setting
`FLUX_BACKEND=fal` (with `FAL_KEY` exported) or editing `prompts.yaml`.
