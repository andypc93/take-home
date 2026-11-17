"""Optional API-key authentication: off by default, per-key image ownership when on."""
import hashlib
import io
import json
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

import database
from test_q1 import PNG, app_module, assert_error
from test_q2 import parse_sse

KEY_A, KEY_B = "key-alpha-123", "key-beta-456"
A = {"Authorization": f"Bearer {KEY_A}"}
B = {"Authorization": f"Bearer {KEY_B}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "STREAM_CHUNK_DELAY", 0)
    monkeypatch.setattr(app_module, "MOCK_CHAT_DELAY", 0)
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(app_module, "API_KEYS", [KEY_A, KEY_B])


def upload(client, headers=None):
    return client.post("/upload", data={"image": (io.BytesIO(PNG), "cat.png")},
                       content_type="multipart/form-data", headers=headers)


def protected_calls(client, image_id, headers=None):
    """One request to each protected route."""
    return {
        "upload": upload(client, headers),
        "chat": client.post(f"/chat/{image_id}", json={"question": "hi"}, headers=headers),
        "stream": client.post(f"/chat-stream/{image_id}", json={"question": "hi"}, headers=headers),
        "history": client.get(f"/chat/{image_id}/history", headers=headers),
        "clear": client.delete(f"/chat/{image_id}/history", headers=headers),
    }


# --- off (the default) -------------------------------------------------------

def test_keys_are_off_by_default_and_nothing_changes(client):
    assert app_module.API_KEYS == []
    image = upload(client)
    assert image.status_code == 201
    statuses = {name: resp.status_code
                for name, resp in protected_calls(client, image.get_json()["image_id"]).items()}
    assert statuses == {"upload": 201, "chat": 200, "stream": 200, "history": 200, "clear": 204}


def test_a_key_sent_while_keys_are_off_is_ignored(client):
    image = upload(client, A)
    assert image.status_code == 201
    assert client.get(f"/chat/{image.get_json()['image_id']}/history").status_code == 200


# --- on ----------------------------------------------------------------------

def test_valid_key_is_accepted_on_every_route(client, keys):
    image_id = upload(client, A).get_json()["image_id"]
    statuses = {name: resp.status_code for name, resp in protected_calls(client, image_id, A).items()}
    assert statuses == {"upload": 201, "chat": 200, "stream": 200, "history": 200, "clear": 204}


@pytest.mark.parametrize("headers", [
    None,                                            # missing
    {"Authorization": "Bearer wrong-key"},           # unknown key
    {"Authorization": f"Bearer {KEY_A}x"},           # close, but not it
    {"Authorization": f"Basic {KEY_A}"},             # wrong scheme
    {"Authorization": KEY_A},                        # no scheme
    {"Authorization": "Bearer "},                    # empty
    {"Authorization": "Bearer éè"},        # not ASCII
])
def test_missing_or_wrong_key_is_401_on_every_route(client, keys, headers):
    image_id = upload(client, A).get_json()["image_id"]
    for name, resp in protected_calls(client, image_id, headers).items():
        assert_error(resp, 401, "invalid_api_key")
        assert resp.headers["WWW-Authenticate"] == "Bearer", name
    assert client.get(f"/chat/{image_id}/history", headers=A).get_json()["count"] == 0


def test_key_is_checked_before_the_request_is_looked_at(client, keys):
    """No key: 401 even for an unknown image or a bad body, so nothing can be probed."""
    assert_error(client.post("/chat/not-a-uuid", json={"question": "hi"}), 401, "invalid_api_key")
    assert_error(client.post("/upload"), 401, "invalid_api_key")


def test_scheme_is_case_insensitive_and_second_key_works(client, keys):
    assert upload(client, {"Authorization": f"bearer {KEY_A}"}).status_code == 201
    assert upload(client, B).status_code == 201


def test_open_routes_stay_open(client, keys):
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/metrics").status_code == 200          # loopback rule, unchanged


# --- ownership ---------------------------------------------------------------

def test_another_key_cannot_see_or_use_an_image(client, keys):
    image_id = upload(client, A).get_json()["image_id"]
    assert client.post(f"/chat/{image_id}", json={"question": "mine"}, headers=A).status_code == 200

    calls = protected_calls(client, image_id, B)
    assert calls.pop("upload").status_code == 201             # B may upload its own
    for name, resp in calls.items():
        assert_error(resp, 404, "image_not_found")            # 404, not 403: ids cannot be probed

    history = client.get(f"/chat/{image_id}/history", headers=A).get_json()
    assert [m["content"] for m in history["messages"] if m["role"] == "user"] == ["mine"]


def test_another_key_cannot_resume_a_stream(client, keys):
    image_id = upload(client, A).get_json()["image_id"]
    events = parse_sse(client.post(f"/chat-stream/{image_id}", json={"question": "secret"},
                                   headers=A).get_data(as_text=True))
    resume = {"Last-Event-ID": events[1][0]}

    assert_error(client.post(f"/chat-stream/{image_id}", headers={**B, **resume}), 404, "image_not_found")
    assert_error(client.post(f"/chat-stream/{image_id}", headers=resume), 401, "invalid_api_key")
    # B cannot replay A's stream through an image of its own either.
    own = upload(client, B).get_json()["image_id"]
    assert_error(client.post(f"/chat-stream/{own}", headers={**B, **resume}), 404, "stream_not_found")

    resumed = client.post(f"/chat-stream/{image_id}", headers={**A, **resume})
    assert resumed.status_code == 200
    assert parse_sse(resumed.get_data(as_text=True)) == events[2:]


def test_ownership_is_checked_even_when_the_image_is_cached(client, keys):
    image_id = upload(client, A).get_json()["image_id"]
    assert client.get(f"/chat/{image_id}/history", headers=A).status_code == 200
    assert image_id in app_module.image_cache
    assert_error(client.get(f"/chat/{image_id}/history", headers=B), 404, "image_not_found")


def test_images_uploaded_without_keys_are_hidden_once_keys_are_on(client, monkeypatch):
    image_id = upload(client).get_json()["image_id"]
    monkeypatch.setattr(app_module, "API_KEYS", [KEY_A])
    assert_error(client.get(f"/chat/{image_id}/history", headers=A), 404, "image_not_found")
    monkeypatch.setattr(app_module, "API_KEYS", [])
    assert client.get(f"/chat/{image_id}/history").status_code == 200


# --- what is stored and returned ---------------------------------------------

def test_owner_is_stored_as_a_hash_and_never_returned(client, keys):
    resp = upload(client, A)
    body = resp.get_json()
    image_id = body["image_id"]
    with app_module.db.session() as session:
        owner = session.get(app_module.UploadedImage, image_id).owner
    assert owner == hashlib.sha256(KEY_A.encode()).hexdigest()

    replies = [resp.get_data(as_text=True)]
    replies.append(client.post(f"/chat/{image_id}", json={"question": "hi"}, headers=A).get_data(as_text=True))
    replies.append(client.post(f"/chat-stream/{image_id}", json={"question": "hi"},
                               headers=A).get_data(as_text=True))
    replies.append(client.get(f"/chat/{image_id}/history", headers=A).get_data(as_text=True))
    replies.append(client.get(f"/chat/{image_id}/history", headers=B).get_data(as_text=True))
    replies.append(upload(client).get_data(as_text=True))
    for reply in replies:
        assert owner not in reply and owner[:16] not in reply
        assert KEY_A not in reply and KEY_B not in reply
    assert "owner" not in body and "path" not in body
    assert "owner" not in json.dumps(body)


def test_key_is_not_stored_anywhere_in_the_database(client, keys):
    upload(client, A)
    with app_module.db.session() as session:
        for table in database.Base.metadata.sorted_tables:
            for row in session.execute(table.select()).all():
                assert KEY_A not in repr(row)


def test_owner_is_null_without_keys(client):
    image_id = upload(client).get_json()["image_id"]
    with app_module.db.session() as session:
        assert session.get(app_module.UploadedImage, image_id).owner is None


# --- rate limiting -----------------------------------------------------------

def test_rate_limit_is_per_key(client, keys, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (2, 60), "chat": (100, 60)})
    assert [upload(client, A).status_code for _ in range(3)] == [201, 201, 429]
    assert upload(client, B).status_code == 201               # same address, its own allowance


def test_failed_keys_count_against_the_address_not_against_a_key(client, keys, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (2, 60), "chat": (100, 60)})
    wrong = {"Authorization": "Bearer guess"}
    assert [upload(client, wrong).status_code for _ in range(2)] == [401, 401]
    blocked = upload(client, wrong)
    assert_error(blocked, 429, "rate_limit_exceeded")         # guessing is rate limited
    assert int(blocked.headers["Retry-After"]) >= 1
    assert upload(client).status_code == 429                  # missing key: same address budget
    assert upload(client, A).status_code == 201               # a valid key is not locked out


def test_rate_limit_is_per_address_without_keys(client, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (2, 60), "chat": (100, 60)})
    assert [upload(client, headers).status_code for headers in (None, A, B)] == [201, 201, 429]


# --- migration ---------------------------------------------------------------

def test_migration_adds_owner_and_keeps_existing_rows(tmp_path):
    url = "sqlite:///" + str(tmp_path / "old.db").replace("\\", "/")
    config = Config(os.path.join(database.BASE_DIR, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(database.BASE_DIR, "migrations"))
    config.set_main_option("sqlalchemy.url", url)

    command.upgrade(config, "0002_rate_limit_counters")
    engine = create_engine(url)
    assert "owner" not in {c["name"] for c in inspect(engine).get_columns("uploaded_images")}
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO uploaded_images (id, filename, content_type, size, path, uploaded_at, analysis)"
            " VALUES ('old-image', 'a.png', 'image/png', 1, 'a.png', 1.0, '{}')"))

    command.upgrade(config, "head")
    columns = {c["name"]: c for c in inspect(engine).get_columns("uploaded_images")}
    assert columns["owner"]["nullable"] is True
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id, owner FROM uploaded_images")).all() == [
            ("old-image", None)]

    command.downgrade(config, "0002_rate_limit_counters")
    assert "owner" not in {c["name"] for c in inspect(engine).get_columns("uploaded_images")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id FROM uploaded_images")).all() == [("old-image",)]
    engine.dispose()
