from __future__ import annotations

import asyncio
import contextlib

from layout_server.file_watcher import watch_for_changes


async def _run_briefly(paths, on_change, poll_interval_seconds=0.05, run_for_seconds=0.3):
    task = asyncio.create_task(
        watch_for_changes(paths, on_change, poll_interval_seconds=poll_interval_seconds)
    )
    await asyncio.sleep(run_for_seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_watch_for_changes_fires_when_a_file_is_modified(tmp_path):
    watched = tmp_path / "static"
    watched.mkdir()
    target = watched / "index.html"
    target.write_text("<h1>before</h1>")

    calls = []

    async def on_change():
        calls.append(1)

    async def scenario():
        task = asyncio.create_task(watch_for_changes([watched], on_change, poll_interval_seconds=0.05))
        await asyncio.sleep(0.15)
        target.write_text("<h1>after</h1>")
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert len(calls) == 1


def test_watch_for_changes_fires_when_a_file_is_added(tmp_path):
    watched = tmp_path / "static"
    watched.mkdir()
    (watched / "F.html").write_text("<h1>F</h1>")

    calls = []

    async def on_change():
        calls.append(1)

    async def scenario():
        task = asyncio.create_task(watch_for_changes([watched], on_change, poll_interval_seconds=0.05))
        await asyncio.sleep(0.15)
        (watched / "B.html").write_text("<h1>B</h1>")
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert len(calls) == 1


def test_watch_for_changes_does_not_fire_when_nothing_changes(tmp_path):
    watched = tmp_path / "static"
    watched.mkdir()
    (watched / "index.html").write_text("<h1>unchanged</h1>")

    calls = []

    async def on_change():
        calls.append(1)

    asyncio.run(_run_briefly([watched], on_change))

    assert calls == []


def test_watch_for_changes_tolerates_a_missing_directory(tmp_path):
    # A configured app_static_dir that doesn't exist yet (or a directory
    # deleted mid-run) must not crash the watcher loop.
    missing = tmp_path / "does-not-exist"

    async def on_change():
        pass

    asyncio.run(_run_briefly([missing], on_change))
