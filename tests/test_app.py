def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_screens_returns_computed_rects(client):
    response = client.get("/api/screens")
    assert response.status_code == 200
    body = response.json()
    screen_f = next(s for s in body["screens"] if s["id"] == "F")
    assert screen_f["rect"] == {"x": 220, "y": 80, "width": 1800, "height": 1400}


def test_get_audio_config(client):
    response = client.get("/api/audio-config")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "input_device": "BlackHole 2ch",
        "output_device": "BlackHole 2ch",
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
