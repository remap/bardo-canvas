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
