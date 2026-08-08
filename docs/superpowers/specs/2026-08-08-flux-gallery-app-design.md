# Flux/Gemini Gallery App — Design Spec

Status: approved for implementation planning
Scope: `apps/flux-gallery/` — a validation app built on the layout-driver framework (see `2026-08-08-layout-driver-framework-design.md`). Self-contained under its own app directory; touches nothing in `layout_server/`, `ndi_broadcaster/`, or `static/`.

## 1. Purpose

Generate a continuously-rotating gallery of AI-generated images across all 6 screens, using local Flux.1-schnell (via `diffusers` on MPS) for image generation and Gemini for per-screen prompt expansion. Exercises the framework's push-image path (`POST /screens/{id}/image`) and its screenshot capability (`POST /api/screenshot`, see framework spec addendum).

## 2. Architecture

A single Python worker process, `apps/flux-gallery/worker.py`. One shared MPS device means generation is inherently serial — no concurrency to manage, just a loop. Three collaborators:

- **`PromptQueue`** (one per screen) — holds pre-expanded, ready-to-use Flux prompt strings.
- **`GeminiExpander`** — turns a screen's `meta_prompt` instruction into a batch of `queue_size` concrete Flux prompts via the `google-genai` SDK.
- **`FluxGenerator`** — wraps `diffusers.FluxPipeline` (schnell weights) on the MPS device.
- **`worker.py` main loop** — ties them together and talks to the framework's HTTP API.

## 3. Config

`apps/flux-gallery/config/prompts.yaml`:

```yaml
base:
  model: "black-forest-labs/FLUX.1-schnell"
  num_inference_steps: 4     # schnell default; no guidance_scale — schnell is guidance-free
  queue_size: 5               # pre-expanded prompts kept ready per screen
  refill_when_below: 2        # trigger a Gemini batch refill once a screen's queue drops below this

screens:
  - id: F
    meta_prompt: "..."        # independent natural-language instruction, one per screen
  - id: B
    meta_prompt: "..."
  - id: C
    meta_prompt: "..."
  - id: D
    meta_prompt: "..."
  - id: A
    meta_prompt: "..."
  - id: E
    meta_prompt: "..."
```

Each screen's `meta_prompt` is fully independent — theme/subject/style control lives entirely in this config, not in code. The framework and app impose no shared theme.

## 4. Prompt pipeline

Each screen owns a `PromptQueue`. When a screen's queue length drops below `refill_when_below`, a single batch Gemini call expands that screen's `meta_prompt` into `queue_size` fresh Flux prompts and refills the queue. This decouples Gemini's network latency from the generation loop — Flux never blocks waiting on a Gemini response mid-cycle, only during the (infrequent) refill.

**Fallback:** if the Gemini call fails (network error, rate limit, auth issue), the worker logs a warning and uses the raw `meta_prompt` text directly as the Flux prompt for that cycle, rather than blocking or crashing.

## 5. Generation loop

Each cycle:

1. Pick a random screen (not round-robin — matches the "as fast as possible, random" scheduling decision; avoids the predictability of strict ordering, no minimum dwell time).
2. Pop a prompt from that screen's queue (triggering a refill check as in §4).
3. Compute a target resolution near that screen's native aspect ratio, rounded to the nearest multiple of 16 (Flux's dimension constraint). E.g. screen A (1600×400, already ÷16) generates at exactly 1600×400; screen F (1800×1400, not ÷16) generates at the nearest valid shape (e.g. 1808×1408) and lets the framework's cover-fit crop on the client absorb the few-pixel remainder — no cropping math lives in this app.
4. Run Flux schnell inference (`diffusers.FluxPipeline`, `torch.bfloat16`, MPS device via `torch.backends.mps.is_available()`).
5. `POST {LAYOUT_DRIVER_URL}/screens/{id}/image` with the resulting PNG bytes.
6. Call the framework's `POST {LAYOUT_DRIVER_URL}/api/screenshot` (see framework spec addendum — this is a framework capability the app merely calls, not something it implements) and save the returned full-wall PNG.
7. Persist both artifacts to disk (§6).
8. Loop.

## 6. Output / disk history

- Raw generated image: `apps/flux-gallery/output/<screen_id>/<timestamp>.png`
- Full-wall screenshot (from the framework's screenshot API, taken right after the push in step 6): `apps/flux-gallery/output/wall/<timestamp>.png`
- Simple retention: prune each directory to the most recent 200 files after each write. No other retention policy (no size-based quotas, no archival) — YAGNI until this actually becomes a problem.

## 7. Credentials and `run.sh`

`apps/flux-gallery/run.sh` (separate from the framework's top-level `run.sh` — this app assumes a layout-driver instance is already running and reachable):

- `GEMINI_API_KEY` — required, official `google-genai` SDK.
- `HF_TOKEN` — optional. FLUX.1-schnell is openly licensed and doesn't require auth to download, but the token is honored via the same fallback chain gentree uses (explicit config → `HF_TOKEN` env var → cached `~/.cache/huggingface/token`) so switching to a gated model or private mirror later needs no code change.
- `LAYOUT_DRIVER_URL` — defaults to `https://localhost:8443`; the worker disables TLS verification against this URL specifically (self-signed cert, trusted local endpoint), never against any other host.

## 8. Error handling

- Gemini failure → fallback to raw `meta_prompt` as the literal Flux prompt (§4).
- Flux inference error (e.g. MPS OOM) → log, skip this cycle, continue the loop on the next random screen rather than crashing the whole worker.
- Push (`POST /screens/{id}/image`) failure (framework unreachable) → log, retry once after a short backoff, then skip the cycle if still failing — the worker keeps running and tries again next cycle rather than exiting.
- Screenshot call failure → log and skip saving that cycle's wall screenshot; does not block or retry, since it's a nice-to-have artifact, not the primary output.

## 9. Testing

- `pytest` unit test for the per-screen resolution-rounding function, using all 6 real screen aspect ratios (from the framework's `screens.yaml` fixture) as cases.
- `pytest` unit test for `PromptQueue` refill-trigger logic against a fake Gemini client: asserts refill fires exactly when queue length crosses `refill_when_below`, and that the fallback path engages when the fake client raises.
- The full generate→push→screenshot loop is verified manually against a running framework instance (`apps/test-pattern/` or a live layout_server) — no practical way to unit test actual MPS inference or the visual result.

## 10. Non-goals

- No image editing/inpainting/LoRA — single text-to-image schnell generation only.
- No cross-screen prompt coordination (each screen's queue and theme are fully independent, per §3).
- No UI for editing `prompts.yaml` — it's a config file, edited by hand.
