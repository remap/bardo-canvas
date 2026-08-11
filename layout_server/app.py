from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .audio import (
    AudioConfig,
    AudioDeviceList,
    discover_audio_devices,
    load_audio_config,
    write_audio_devices_file,
)
from .config import LayoutConfig, load_layout_config
from .file_watcher import watch_for_changes
from .screen_store import ScreenImageStore
from .screens_api import register_screen_routes
from .screenshot import ScreenshotBroker, register_screenshot_routes
from .ws_manager import ConnectionManager


@dataclass
class AppState:
    layout_config: LayoutConfig
    audio_config: AudioConfig
    runtime_dir: Path
    audio_devices: AudioDeviceList | None = None


def create_app(
    *,
    screens_yaml: Path,
    audio_yaml: Path,
    runtime_dir: Path,
    app_static_dir: Path,
    framework_static_dir: Path,
) -> FastAPI:
    state = AppState(
        layout_config=load_layout_config(screens_yaml),
        audio_config=load_audio_config(audio_yaml),
        runtime_dir=runtime_dir,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.audio_devices = discover_audio_devices()
        state.runtime_dir.mkdir(parents=True, exist_ok=True)
        write_audio_devices_file(state.audio_devices, state.runtime_dir / "audio_devices.json")

        # Auto-reload: any app's static files changing (its own directory or
        # the shared framework one) broadcasts a "reload" message every
        # connected client is free to act on -- see enableAutoReload() in
        # layout-driver.js. Runs unconditionally: with no client listening
        # for it, the message is just ignored (same as any other message
        # type an app doesn't register a handler for), so there's no
        # config flag needed to turn this on.
        watch_task = asyncio.create_task(
            watch_for_changes(
                [app_static_dir, framework_static_dir],
                on_change=lambda: connections.broadcast({"type": "reload"}),
            )
        )
        try:
            yield
        finally:
            watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch_task

    app = FastAPI(lifespan=lifespan)
    app.state.layout = state

    store = ScreenImageStore()
    connections = ConnectionManager()
    register_screen_routes(app, state.layout_config, store, connections)

    screenshot_broker = ScreenshotBroker()
    register_screenshot_routes(app, connections, screenshot_broker)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/screens")
    async def get_screens() -> LayoutConfig:
        return state.layout_config

    @app.get("/api/audio-config")
    async def get_audio_config() -> AudioConfig:
        return state.audio_config

    @app.get("/api/audio-devices")
    async def get_audio_devices() -> AudioDeviceList:
        return state.audio_devices

    def _framework_js_route(filename: str):
        async def handler() -> FileResponse:
            return FileResponse(framework_static_dir / filename, media_type="text/javascript")

        return handler

    for framework_js_filename in (
        "layout-driver.js",
        "geometry.js",
        "device-match.js",
        "backoff.js",
        "screenshot-worker.js",
    ):
        app.get(f"/{framework_js_filename}")(_framework_js_route(framework_js_filename))

    app.mount("/", StaticFiles(directory=app_static_dir, html=True), name="app")

    return app
