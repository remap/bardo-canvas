from __future__ import annotations

import logging
import os
import random
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from layout_server.config import LayoutConfig, load_layout_config

from .config import PromptsConfig, load_prompts_config
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
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".png"


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
        except Exception as exc:  # noqa: BLE001 - retry must survive any push failure mode
            last_exc = exc
            logger.warning("Push to screen %s failed (attempt %d): %s", screen_id, attempt + 1, exc)
            if attempt < retries:
                time.sleep(backoff_seconds)
    assert last_exc is not None
    raise last_exc


def _validate_screen_ids(prompts_config: PromptsConfig, layout_config: LayoutConfig) -> None:
    """Fail at boot on a prompts.yaml screen id the layout doesn't have.

    Without this, a typo'd id only surfaces as a KeyError on screen_dims[screen_id]
    whenever random.choice happens to pick it -- potentially hours in, and outside
    every per-stage try/except, so it kills the worker.
    """
    known_ids = {screen.id for screen in layout_config.screens}
    for screen in prompts_config.screens:
        if screen.id not in known_ids:
            raise ValueError(
                f"prompts.yaml references unknown screen id {screen.id!r}; "
                f"known screen ids from screens.yaml: {sorted(known_ids)}"
            )


def run_forever() -> None:
    screens_yaml = Path(os.environ.get("SCREENS_YAML", str(REPO_ROOT / "config" / "screens.yaml")))
    prompts_yaml = Path(os.environ.get("PROMPTS_YAML", str(APP_ROOT / "config" / "prompts.yaml")))
    layout_driver_url = os.environ.get("LAYOUT_DRIVER_URL", "https://localhost:8443")
    gemini_api_key = os.environ["GEMINI_API_KEY"]

    layout_config = load_layout_config(screens_yaml)
    prompts_config = load_prompts_config(prompts_yaml)
    _validate_screen_ids(prompts_config, layout_config)

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
    screen_dims = {
        screen.id: (screen.rect.width, screen.rect.height) for screen in layout_config.screens
    }

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

        try:
            save_and_prune(
                output_dir / screen_id, _timestamp_filename(), image_bytes, keep=HISTORY_KEEP
            )
        except Exception:
            logger.exception("Failed to save history for screen %s; continuing", screen_id)

        try:
            wall_bytes = take_screenshot(http_client)
            save_and_prune(
                output_dir / "wall", _timestamp_filename(), wall_bytes, keep=HISTORY_KEEP
            )
        except Exception:
            logger.exception("Screenshot capture failed; continuing")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run_forever()


if __name__ == "__main__":
    main()
