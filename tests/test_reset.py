"""POST /reset: the test helper that wipes all data, and the guards around it."""
import os

import pytest

from conftest import count_rows
from test_q1 import PNG, app_module, assert_error, upload

CONFIRM = {"X-Reset-Confirm": "yes"}
MODELS = (app_module.UploadedImage, app_module.ChatHistory, app_module.StreamSession,
          app_module.RateLimitCounter)


@pytest.fixture
def client(tmp_path, monkeypatch):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    monkeypatch.setitem(app_module.app.config, "ALLOW_RESET", True)
    monkeypatch.setattr(app_module, "MOCK_CHAT_DELAY", 0)
    monkeypatch.setattr(app_module, "STREAM_CHUNK_DELAY", 0)
    with app_module.app.test_client() as c:
        yield c


def fill(client, tmp_path):
    """Two images, a chat, a stream and a leftover staging file."""
    first = upload(client, PNG).get_json()["image_id"]
    upload(client, PNG)
    client.post(f"/chat/{first}", json={"question": "hi"})
    client.post(f"/chat-stream/{first}", json={"question": "again"}).get_data()
    (tmp_path / "part-leftover.png").write_bytes(PNG)
    return first


def test_reset_removes_all_data_and_files(client, tmp_path):
    first = fill(client, tmp_path)
    assert all(count_rows(model) > 0 for model in MODELS) and len(os.listdir(tmp_path)) == 3

    resp = client.post("/reset", headers=CONFIRM)

    assert resp.status_code == 200
    assert resp.get_json() == {"reset": True, "removed": {"images": 2, "turns": 2}}
    assert [count_rows(model) for model in MODELS] == [0, 0, 0, 0]
    assert os.listdir(tmp_path) == []
    assert_error(client.post(f"/chat/{first}", json={"question": "still there?"}),
                 404, "image_not_found")                 # the cache was cleared too


def test_reset_lifts_rate_limit_blocks(client, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (1, 60), "chat": (100, 60)})
    assert upload(client, PNG).status_code == 201
    assert upload(client, PNG).status_code == 429
    client.post("/reset", headers=CONFIRM)
    assert upload(client, PNG).status_code == 201


def test_reset_clears_request_counts(client):
    upload(client, PNG)
    client.post("/reset", headers=CONFIRM)
    text = client.get("/metrics").get_data(as_text=True)
    assert 'route="/upload"' not in text


def test_service_works_normally_after_reset(client, tmp_path):
    fill(client, tmp_path)
    client.post("/reset", headers=CONFIRM)
    image_id = upload(client, PNG).get_json()["image_id"]
    answer = client.post(f"/chat/{image_id}", json={"question": "fresh"}).get_json()
    assert "previous question" not in answer["choices"][0]["message"]["content"]


# --- guards ----------------------------------------------------------------

def test_reset_is_off_unless_switched_on(client, tmp_path, monkeypatch):
    fill(client, tmp_path)
    monkeypatch.setitem(app_module.app.config, "ALLOW_RESET", False)
    assert_error(client.post("/reset", headers=CONFIRM), 403, "reset_disabled")
    assert count_rows(app_module.UploadedImage) == 2


def test_reset_is_refused_from_another_machine(client, tmp_path):
    fill(client, tmp_path)
    resp = client.post("/reset", headers=CONFIRM, environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert_error(resp, 403, "forbidden")
    assert count_rows(app_module.UploadedImage) == 2


def test_reset_needs_the_confirmation_header(client, tmp_path):
    """A form on another website can POST here but cannot add a custom header."""
    fill(client, tmp_path)
    assert_error(client.post("/reset"), 400, "confirmation_required")
    assert_error(client.post("/reset", headers={"X-Reset-Confirm": "no"}), 400, "confirmation_required")
    assert count_rows(app_module.UploadedImage) == 2


def test_reset_is_post_only(client):
    assert_error(client.get("/reset"), 405)


def test_reset_is_off_by_default_when_imported():
    """Only `python app.py` switches it on; importing the app (as serve.py does) leaves it off."""
    assert app_module.app.config["ALLOW_RESET"] is False
