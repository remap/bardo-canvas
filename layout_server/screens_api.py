from __future__ import annotations

import io

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from PIL import Image

from .config import LayoutConfig
from .screen_store import ScreenImageStore
from .ws_manager import ConnectionManager

_VALID_CONTENT_TYPES = {"image/png", "image/jpeg"}


def register_screen_routes(
    app: FastAPI,
    layout_config: LayoutConfig,
    store: ScreenImageStore,
    connections: ConnectionManager,
) -> None:
    @app.post("/screens/{screen_id}/image")
    async def push_image(
        screen_id: str, request: Request, transition_ms: int = Query(default=500)
    ) -> dict[str, int]:
        if layout_config.screen_by_id(screen_id) is None:
            raise HTTPException(status_code=404, detail=f"Unknown screen {screen_id!r}")

        content_type = request.headers.get("content-type", "")
        if content_type not in _VALID_CONTENT_TYPES:
            raise HTTPException(
                status_code=400, detail=f"Unsupported content-type {content_type!r}"
            )

        body = await request.body()
        try:
            Image.open(io.BytesIO(body)).verify()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Image bytes did not decode") from exc

        version = store.put(screen_id, content_type, body)
        await connections.broadcast(
            {
                "type": "frame",
                "screen": screen_id,
                "version": version,
                "transition_ms": transition_ms,
            }
        )
        return {"version": version}

    @app.get("/screens/{screen_id}/image")
    async def get_image(screen_id: str, v: int | None = Query(default=None)) -> Response:
        stored = store.get(screen_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"No image pushed for {screen_id!r} yet")
        return Response(content=stored.data, media_type=stored.content_type)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await connections.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            connections.disconnect(websocket)
