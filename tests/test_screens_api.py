import io

from PIL import Image


def _png_bytes(color=(255, 0, 0)) -> bytes:
    image = Image.new("RGB", (4, 4), color=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_push_image_unknown_screen_returns_404(client):
    response = client.post(
        "/screens/Z/image", content=_png_bytes(), headers={"content-type": "image/png"}
    )
    assert response.status_code == 404


def test_push_image_bad_content_type_returns_400(client):
    response = client.post(
        "/screens/F/image", content=b"not an image", headers={"content-type": "text/plain"}
    )
    assert response.status_code == 400


def test_push_image_undecodable_bytes_returns_400(client):
    response = client.post(
        "/screens/F/image", content=b"not a real png", headers={"content-type": "image/png"}
    )
    assert response.status_code == 400


def test_push_then_get_image_round_trip(client):
    push_response = client.post(
        "/screens/F/image", content=_png_bytes(), headers={"content-type": "image/png"}
    )
    assert push_response.status_code == 200
    assert push_response.json() == {"version": 1}

    get_response = client.get("/screens/F/image")
    assert get_response.status_code == 200
    assert get_response.content == _png_bytes()
    assert get_response.headers["content-type"] == "image/png"


def test_get_image_before_any_push_returns_404(client):
    response = client.get("/screens/B/image")
    assert response.status_code == 404


def test_push_image_broadcasts_frame_over_websocket(client):
    with client.websocket_connect("/ws") as websocket:
        client.post("/screens/F/image", content=_png_bytes(), headers={"content-type": "image/png"})
        message = websocket.receive_json()
        assert message == {"type": "frame", "screen": "F", "version": 1, "transition_ms": 500}


def test_push_image_respects_transition_ms_query_param(client):
    with client.websocket_connect("/ws") as websocket:
        client.post(
            "/screens/F/image?transition_ms=200",
            content=_png_bytes(),
            headers={"content-type": "image/png"},
        )
        message = websocket.receive_json()
        assert message["transition_ms"] == 200
