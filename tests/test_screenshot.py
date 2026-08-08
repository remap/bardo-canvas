import threading


def test_screenshot_with_no_connected_client_returns_504(client):
    response = client.post("/api/screenshot")
    assert response.status_code == 504


def test_screenshot_round_trip(client):
    result: dict = {}

    def do_post() -> None:
        response = client.post("/api/screenshot")
        result["status_code"] = response.status_code
        result["content"] = response.content

    with client.websocket_connect("/ws") as websocket:
        thread = threading.Thread(target=do_post)
        thread.start()

        message = websocket.receive_json()
        assert message["type"] == "screenshot_request"
        request_id = message["request_id"]

        client.post(f"/api/screenshot-result/{request_id}", content=b"fake-png-bytes")
        thread.join(timeout=5)

    assert result["status_code"] == 200
    assert result["content"] == b"fake-png-bytes"


def test_screenshot_result_for_unknown_request_id_returns_404(client):
    response = client.post("/api/screenshot-result/does-not-exist", content=b"data")
    assert response.status_code == 404
