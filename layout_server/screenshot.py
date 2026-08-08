from __future__ import annotations

import asyncio
import uuid

from fastapi import FastAPI, HTTPException, Request, Response

from .ws_manager import ConnectionManager

SCREENSHOT_TIMEOUT_SECONDS = 2.0


class ScreenshotBroker:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[bytes]] = {}

    def new_request(self) -> tuple[str, asyncio.Future[bytes]]:
        request_id = uuid.uuid4().hex
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return request_id, future

    def resolve(self, request_id: str, data: bytes) -> bool:
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(data)
        return True

    def discard(self, request_id: str) -> None:
        self._pending.pop(request_id, None)


def register_screenshot_routes(
    app: FastAPI, connections: ConnectionManager, broker: ScreenshotBroker
) -> None:
    @app.post("/api/screenshot")
    async def take_screenshot() -> Response:
        if connections.connection_count == 0:
            raise HTTPException(status_code=504, detail="No browser client connected")

        request_id, future = broker.new_request()
        await connections.broadcast({"type": "screenshot_request", "request_id": request_id})

        try:
            data = await asyncio.wait_for(future, timeout=SCREENSHOT_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            broker.discard(request_id)
            raise HTTPException(status_code=504, detail="Screenshot request timed out") from exc

        return Response(content=data, media_type="image/png")

    @app.post("/api/screenshot-result/{request_id}")
    async def screenshot_result(request_id: str, request: Request) -> dict[str, bool]:
        data = await request.body()
        resolved = broker.resolve(request_id, data)
        if not resolved:
            raise HTTPException(status_code=404, detail="Unknown or already-resolved request_id")
        return {"ok": True}
