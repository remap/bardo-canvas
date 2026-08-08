from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StoredImage:
    content_type: str
    data: bytes
    version: int


class ScreenImageStore:
    def __init__(self) -> None:
        self._images: dict[str, StoredImage] = {}

    def put(self, screen_id: str, content_type: str, data: bytes) -> int:
        current = self._images.get(screen_id)
        version = (current.version + 1) if current else 1
        self._images[screen_id] = StoredImage(content_type=content_type, data=data, version=version)
        return version

    def get(self, screen_id: str) -> StoredImage | None:
        return self._images.get(screen_id)
