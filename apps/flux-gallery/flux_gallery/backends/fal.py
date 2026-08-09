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
