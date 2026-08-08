from __future__ import annotations

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
        yield

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

    for framework_js_filename in ("layout-driver.js", "geometry.js", "device-match.js"):
        app.get(f"/{framework_js_filename}")(_framework_js_route(framework_js_filename))

    app.mount("/", StaticFiles(directory=app_static_dir, html=True), name="app")

    return app
