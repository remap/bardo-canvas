from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path


def _snapshot(paths: list[Path]) -> dict[Path, float]:
    snapshot: dict[Path, float] = {}
    for root in paths:
        if not root.exists():
            continue
        for file in root.rglob("*"):
            if file.is_file():
                snapshot[file] = file.stat().st_mtime
    return snapshot


async def watch_for_changes(
    paths: list[Path],
    on_change: Callable[[], Awaitable[None]],
    poll_interval_seconds: float = 0.5,
) -> None:
    """Poll the given directories (recursively) for any added, removed, or
    modified file, calling on_change() whenever something changes. Runs
    until the calling task is cancelled -- meant to be launched as a
    background task from an app's lifespan.

    Polling by mtime, not a native filesystem-events library (watchdog,
    watchfiles): this repo's dependency philosophy favors small and
    readable over feature-complete, and a fixed-interval scan is simple
    enough to read start to finish, with no platform-specific event API to
    reason about, at a cost (a poll_interval_seconds-worst-case reload
    delay) that doesn't matter for a "someone edited an HTML file, expects
    to see it moments later" workflow.
    """
    previous = _snapshot(paths)
    while True:
        await asyncio.sleep(poll_interval_seconds)
        current = _snapshot(paths)
        if current != previous:
            previous = current
            await on_change()
