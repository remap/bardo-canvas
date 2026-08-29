def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_screens_returns_computed_rects(client):
    response = client.get("/api/screens")
    assert response.status_code == 200
    body = response.json()
    screen_f = next(s for s in body["screens"] if s["id"] == "F")
    assert screen_f["rect"] == {"x": 0, "y": 0, "width": 1800, "height": 1400}


def test_get_audio_config(client):
    response = client.get("/api/audio-config")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "input_device": "Loopback Audio",
        "output_device": "Loopback Audio",
    }


def test_get_audio_devices_populated_by_lifespan(client, tmp_path, monkeypatch):
    response = client.get("/api/audio-devices")
    assert response.status_code == 200
    body = response.json()
    assert "inputs" in body
    assert "outputs" in body


def test_audio_devices_file_written_on_startup(client, tmp_path):
    devices_file = tmp_path / "runtime" / "audio_devices.json"
    assert devices_file.exists()


def test_get_layout_driver_js(client):
    response = client.get("/layout-driver.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_get_geometry_js(client):
    response = client.get("/geometry.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_get_device_match_js(client):
    response = client.get("/device-match.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_get_backoff_js(client):
    response = client.get("/backoff.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_get_screenshot_worker_js(client):
    response = client.get("/screenshot-worker.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
