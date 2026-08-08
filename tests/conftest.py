from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from layout_server.app import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app_static_dir(tmp_path: Path) -> Path:
    static_dir = tmp_path / "app-static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><html><body>test app</body></html>")
    return static_dir


@pytest.fixture
def client(tmp_path: Path, app_static_dir: Path) -> TestClient:
    app = create_app(
        screens_yaml=REPO_ROOT / "config" / "screens.yaml",
        audio_yaml=REPO_ROOT / "config" / "audio.yaml",
        runtime_dir=tmp_path / "runtime",
        app_static_dir=app_static_dir,
        framework_static_dir=REPO_ROOT / "static",
    )
    with TestClient(app) as test_client:
        yield test_client
