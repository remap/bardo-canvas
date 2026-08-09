import io
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from flux_gallery.layout_driver_client import push_image, take_screenshot
from PIL import Image

from layout_server.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]


def _png_bytes(color=(10, 20, 30)) -> bytes:
    image = Image.new("RGB", (4, 4), color=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def client(tmp_path):
    app_static_dir = tmp_path / "app-static"
    app_static_dir.mkdir()
    (app_static_dir / "index.html").write_text("<!doctype html><html><body></body></html>")

    app = create_app(
        screens_yaml=REPO_ROOT / "config" / "screens.yaml",
        audio_yaml=REPO_ROOT / "config" / "audio.yaml",
        runtime_dir=tmp_path / "runtime",
        app_static_dir=app_static_dir,
        framework_static_dir=REPO_ROOT / "static",
    )
    with TestClient(app) as test_client:
        yield test_client


def test_push_image_returns_incrementing_version(client):
    assert push_image(client, "F", _png_bytes()) == 1
    assert push_image(client, "F", _png_bytes(color=(50, 60, 70))) == 2


def test_push_image_unknown_screen_raises(client):
    with pytest.raises(httpx.HTTPStatusError):
        push_image(client, "Z", _png_bytes())


def test_take_screenshot_with_no_connected_browser_raises(client):
    with pytest.raises(httpx.HTTPStatusError):
        take_screenshot(client)
