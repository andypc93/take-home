"""Monitoring tests: /metrics endpoint."""
import uuid

import pytest

from test_q1 import PNG, app_module, upload
from test_q2 import stream


@pytest.fixture
def client(tmp_path, monkeypatch):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    monkeypatch.setattr(app_module, "STREAM_CHUNK_DELAY", 0)
    monkeypatch.setattr(app_module, "MOCK_CHAT_DELAY", 0)
    with app_module.app.test_client() as c:
        yield c


def scrape(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200 and resp.mimetype == "text/plain"
    return resp.get_data(as_text=True)


def test_test_console_page_is_served(client):
    resp = client.get("/")
    assert resp.status_code == 200 and resp.mimetype == "text/html"
    assert b"Visual Assistant" in resp.data
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    resp.close()


def test_requests_counted_by_route_and_status(client):
    image_id = upload(client, PNG).get_json()["image_id"]
    upload(client, b"junk", "a.png")
    client.post(f"/chat/{image_id}", json={"question": "hi"})
    client.post(f"/chat/{uuid.uuid4()}", json={"question": "hi"})
    client.get("/no/such/route")

    text = scrape(client)
    assert 'http_requests_total{method="POST",route="/upload",status="201"} 1' in text
    assert 'http_requests_total{method="POST",route="/upload",status="415"} 1' in text
    assert 'http_requests_total{method="POST",route="/chat/<image_id>",status="200"} 1' in text
    assert 'http_requests_total{method="POST",route="/chat/<image_id>",status="404"} 1' in text
    assert 'http_requests_total{method="GET",route="unmatched",status="404"} 1' in text
    assert 'http_request_duration_seconds_count{route="/upload"} 2' in text
    assert str(image_id) not in text                    # raw paths never become labels


def test_rate_limited_requests_are_visible(client, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (1, 60), "chat": (100, 60)})
    upload(client, PNG)
    upload(client, PNG)
    assert 'route="/upload",status="429"} 1' in scrape(client)


def test_active_streams_gauge(client):
    image_id = upload(client, PNG).get_json()["image_id"]
    held = stream(client, image_id)
    assert "active_streams 1" in scrape(client)
    held.close()
    assert "active_streams 0" in scrape(client)
