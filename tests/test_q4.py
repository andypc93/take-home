"""Q4 tests: database persistence, migrations, cache, failures, cleanup."""
import os

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError

import database
from conftest import count_rows
from test_q1 import PNG, app_module, assert_error, upload
from test_q2 import parse_sse, stream
from test_q3 import chat, history


@pytest.fixture
def client(tmp_path, monkeypatch):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    monkeypatch.setattr(app_module, "STREAM_CHUNK_DELAY", 0)
    monkeypatch.setattr(app_module, "MOCK_CHAT_DELAY", 0)
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def image_id(client):
    return upload(client, PNG).get_json()["image_id"]


def broken_session(*args, **kwargs):
    raise OperationalError("SELECT 1", {}, Exception("connection refused"))


class FakeClock:
    now = 0.0

    def __call__(self):
        return self.now


# --- migrations ------------------------------------------------------------

def test_migrations_build_schema_and_are_idempotent(tmp_path):
    url = "sqlite:///" + str(tmp_path / "fresh.db").replace("\\", "/")
    database.run_migrations(url)
    database.run_migrations(url)
    engine = create_engine(url)
    inspector = inspect(engine)
    assert {"uploaded_images", "chat_history", "stream_sessions", "alembic_version"} <= set(
        inspector.get_table_names())
    assert "ix_chat_history_image_id_id" in {i["name"] for i in inspector.get_indexes("chat_history")}
    assert inspector.get_unique_constraints("chat_history")[0]["column_names"] == ["completion_id"]
    engine.dispose()


def test_models_match_migrations():
    """Every model column exists in the migrated database, and nothing extra."""
    inspector = inspect(app_module.db.engine)
    for table in database.Base.metadata.sorted_tables:
        migrated = {c["name"] for c in inspector.get_columns(table.name)}
        assert migrated == {c.name for c in table.columns}, table.name


# --- persistence -----------------------------------------------------------

def test_data_survives_restart(client, image_id):
    first = chat(client, image_id, "Remember me?")
    stream(client, image_id, "And me?").get_data()

    app_module.db.init(app_module.db.url)          # new engine, as after a restart
    app_module.image_cache.clear()
    app_module.history_cache.clear()

    messages = history(client, image_id)
    assert [m["content"] for m in messages if m["role"] == "user"] == ["Remember me?", "And me?"]
    assert messages[1]["content"] == first.get_json()["choices"][0]["message"]["content"]
    third = chat(client, image_id, "Still there?")
    assert "And me?" in third.get_json()["choices"][0]["message"]["content"]


def test_upload_persists_metadata_and_analysis(client, image_id):
    app_module.image_cache.clear()
    record = app_module.load_image(image_id)
    assert record["content_type"] == "image/png" and record["size"] == len(PNG)
    assert record["analysis"]["object"] == "chat.completion"
    assert record["path"] == f"{image_id}.png"          # file name only, no host directory
    assert os.path.isfile(app_module.image_file_path(record["path"]))


def test_image_resolves_under_another_hosts_mount(client, image_id, tmp_path, monkeypatch):
    """A second host mounts the shared storage elsewhere and still finds the file."""
    other_mount = tmp_path / "other_host_mount"
    os.rename(tmp_path / f"{image_id}.png", tmp_path / "moved.png")
    other_mount.mkdir()
    os.rename(tmp_path / "moved.png", other_mount / f"{image_id}.png")
    app_module.app.config["UPLOAD_FOLDER"] = str(other_mount)
    app_module.image_cache.clear()
    path = app_module.image_file_path(app_module.load_image(image_id)["path"])
    assert os.path.isfile(path) and path.startswith(str(other_mount))


def test_turn_is_stored_once(image_id):
    assert app_module.append_turn(image_id, "chatcmpl-x", "q", "a") is True
    assert app_module.append_turn(image_id, "chatcmpl-x", "q", "a") is False
    assert count_rows(app_module.ChatHistory) == 1


def test_stream_resume_survives_restart(client, image_id):
    full = parse_sse(stream(client, image_id).get_data(as_text=True))
    app_module.db.init(app_module.db.url)
    resumed = stream(client, image_id, headers={"Last-Event-ID": full[1][0]})
    assert parse_sse(resumed.get_data(as_text=True)) == full[2:]


# --- cache -----------------------------------------------------------------

def test_image_cache_serves_reads_without_database(client, image_id, monkeypatch):
    app_module.load_image(image_id)                     # first read fills the cache
    monkeypatch.setattr(app_module.db, "session", broken_session)
    assert app_module.load_image(image_id)["image_id"] == image_id


def test_history_cache_is_used_when_unchanged(client, image_id):
    chat(client, image_id, "Warm the cache")
    history(client, image_id)
    tag, _ = app_module.history_cache.get(image_id)
    sentinel = [{"role": "user", "content": "from cache"}]
    app_module.history_cache.set(image_id, (tag, sentinel))
    assert app_module.get_history(image_id) == sentinel


def test_history_cache_sees_writes_from_other_workers(client, image_id):
    chat(client, image_id, "written here")
    assert len(history(client, image_id)) == 2          # cached now
    with app_module.db.session() as session:            # another worker: no local invalidation
        session.add(app_module.ChatHistory(image_id=image_id, completion_id="chatcmpl-other",
                                           question="written elsewhere", answer="a",
                                           created_at=1.0))
    assert [m["content"] for m in history(client, image_id)][2] == "written elsewhere"
    with app_module.db.session() as session:
        session.execute(app_module.delete(app_module.ChatHistory))
    assert history(client, image_id) == []


def test_write_invalidates_history_cache(client, image_id):
    chat(client, image_id, "one")
    assert len(history(client, image_id)) == 2
    chat(client, image_id, "two")
    assert len(history(client, image_id)) == 4
    client.delete(f"/chat/{image_id}/history")
    assert history(client, image_id) == []


def test_cache_expires_and_is_bounded():
    clock = FakeClock()
    cache = database.TTLCache(ttl=10, max_size=2, clock=clock)
    assert cache.get_or_load("a", lambda: 1) == 1
    assert cache.get_or_load("a", lambda: 2) == 1          # hit
    clock.now = 11
    assert cache.get_or_load("a", lambda: 2) == 2          # expired, reloaded
    cache.get_or_load("b", lambda: 3)
    cache.get_or_load("c", lambda: 4)
    assert len(cache) == 2 and "a" not in cache            # least recently used evicted


def test_cache_does_not_store_missing_or_stale_values():
    cache = database.TTLCache(ttl=10)
    assert cache.get_or_load("missing", lambda: None) is None
    assert "missing" not in cache

    def load_while_invalidated():
        cache.delete("k")          # a write lands while the read is in flight
        return "stale"

    assert cache.get_or_load("k", load_while_invalidated) == "stale"
    assert "k" not in cache


# --- failures --------------------------------------------------------------

def test_database_down_returns_503(client, image_id, monkeypatch):
    app_module.image_cache.clear()
    monkeypatch.setattr(app_module.db, "session", broken_session)
    for resp in (chat(client, image_id, "hi"), stream(client, image_id),
                 client.get(f"/chat/{image_id}/history"), upload(client, PNG)):
        assert_error(resp, 503, "database_unavailable")
        assert resp.headers["Retry-After"] == "1"


def test_failed_insert_leaves_no_file(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module.db, "session", broken_session)
    assert upload(client, PNG).status_code == 503
    assert os.listdir(tmp_path) == []


def test_health(client, monkeypatch):
    assert client.get("/health").get_json() == {"status": "ok", "database": "ok"}
    monkeypatch.setattr(app_module.db, "session", broken_session)
    resp = client.get("/health")
    assert resp.status_code == 503 and resp.get_json()["database"] == "unavailable"


def test_transient_error_is_retried(monkeypatch):
    calls = []

    @app_module.db_retry
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            broken_session()
        return "ok"

    assert flaky() == "ok" and len(calls) == 3


def test_persistent_error_is_raised_after_retries():
    calls = []

    @app_module.db_retry
    def dead():
        calls.append(1)
        broken_session()

    with pytest.raises(OperationalError):
        dead()
    assert len(calls) == app_module.DB_RETRIES + 1


def test_history_save_failure_mid_stream_is_reported(client, image_id, monkeypatch):
    def fail(*args, **kwargs):
        broken_session()

    monkeypatch.setattr(app_module, "append_turn", fail)
    events = parse_sse(stream(client, image_id).get_data(as_text=True))
    assert '"stream_error"' in events[-1][1]
    assert count_rows(app_module.ChatHistory) == 0


# --- cleanup ---------------------------------------------------------------

def test_cleanup_removes_expired_images_with_their_data(client, image_id, monkeypatch):
    chat(client, image_id, "soon gone")
    stream(client, image_id, "me too").get_data()
    path = app_module.image_file_path(app_module.load_image(image_id)["path"])
    assert os.path.exists(path)
    monkeypatch.setattr(app_module, "IMAGE_TTL", -1)

    removed = app_module.run_cleanup()

    assert removed["images"] == 1
    assert not os.path.exists(path)
    for model in (app_module.UploadedImage, app_module.ChatHistory, app_module.StreamSession):
        assert count_rows(model) == 0
    assert_error(chat(client, image_id, "anyone?"), 404, "image_not_found")


def test_cleanup_keeps_fresh_data(client, image_id):
    chat(client, image_id, "keep me")
    assert app_module.run_cleanup() == {"histories": 0, "stream_sessions": 0, "images": 0}
    assert len(history(client, image_id)) == 2


def test_cleanup_removes_expired_stream_sessions(client, image_id, monkeypatch):
    monkeypatch.setattr(app_module, "STREAM_SESSION_TTL", -1)
    stream(client, image_id).get_data()
    assert app_module.run_cleanup()["stream_sessions"] == 1


def test_cleanup_cli_command():
    result = app_module.app.test_cli_runner().invoke(args=["cleanup"])
    assert result.exit_code == 0 and "images" in result.output
