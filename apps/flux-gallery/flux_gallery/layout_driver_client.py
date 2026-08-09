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
