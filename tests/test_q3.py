"""Q3 tests: conversation history for streaming and non-streaming chat."""
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_q1 import PNG, app_module, assert_error, upload
from test_q2 import parse_sse, stream, text_of

HISTORY = [{"role": "user", "content": "What colour is it?"},
           {"role": "assistant", "content": "It is red."}]


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


def chat(client, image_id, question):
    return client.post(f"/chat/{image_id}", json={"question": question})


def answer(resp):
    return resp.get_json()["choices"][0]["message"]["content"]


def history(client, image_id):
    return client.get(f"/chat/{image_id}/history").get_json()["messages"]


def stream_text(client, image_id, question):
    events = parse_sse(stream(client, image_id, question).get_data(as_text=True))
    import json
    return text_of([json.loads(data) for _, data in events[:-1]])


# --- mocks -----------------------------------------------------------------

def test_mock_chat_uses_history(monkeypatch):
    monkeypatch.setattr(app_module, "MOCK_CHAT_DELAY", 0)
    fresh = app_module.mock_openai_chat("And its size?", "img")
    aware = app_module.mock_openai_chat("And its size?", "img", history=HISTORY)
    text = aware["choices"][0]["message"]["content"]
    assert "What colour is it?" in text and "turn 2" in text
    assert "previous question" not in fresh["choices"][0]["message"]["content"]
    assert aware["usage"]["prompt_tokens"] > fresh["usage"]["prompt_tokens"]


def test_mock_stream_uses_history(monkeypatch):
    monkeypatch.setattr(app_module, "STREAM_CHUNK_DELAY", 0)
    monkeypatch.setattr(app_module, "MOCK_CHAT_DELAY", 0)
    full = app_module.mock_openai_chat("And its size?", "img", history=HISTORY)
    items = list(app_module.mock_openai_chat("And its size?", "img", stream=True, history=HISTORY))
    assert text_of(items[:-1]) == full["choices"][0]["message"]["content"]
    assert items[-2]["usage"] == full["usage"]


def test_mock_context_window_is_bounded(monkeypatch):
    monkeypatch.setattr(app_module, "MOCK_CHAT_DELAY", 0)
    long_history = HISTORY * 100
    capped = long_history[-app_module.MAX_CONTEXT_MESSAGES:]
    assert (app_module.mock_openai_chat("q", "img", history=long_history)["usage"]
            == app_module.mock_openai_chat("q", "img", history=capped)["usage"])


@pytest.mark.parametrize("bad", ["text", [1], [{"role": "user"}], [{"role": "x", "content": "y"}],
                                 [{"role": "user", "content": 5}]])
def test_mock_rejects_malformed_history(bad):
    with pytest.raises(ValueError):
        app_module.mock_openai_chat("q", "img", history=bad)


# --- non-streaming ---------------------------------------------------------

def test_history_starts_empty(client, image_id):
    body = client.get(f"/chat/{image_id}/history").get_json()
    assert body == {"image_id": image_id, "count": 0, "messages": []}


def test_chat_records_turns_in_order(client, image_id):
    first = chat(client, image_id, "What colour is it?")
    second = chat(client, image_id, "And its size?")
    assert "What colour is it?" in answer(second)

    messages = history(client, image_id)
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in messages] == [
        "What colour is it?", answer(first), "And its size?", answer(second)]
    assert messages[0]["completion_id"] == messages[1]["completion_id"] == first.get_json()["id"]
    assert all(m["created_at"] for m in messages)


def test_failed_chat_records_nothing(client, image_id):
    chat(client, image_id, "")
    assert history(client, image_id) == []


def test_history_is_per_image(client, image_id):
    other = upload(client, PNG).get_json()["image_id"]
    chat(client, image_id, "About the first")
    assert history(client, other) == []
    assert "previous question" not in answer(chat(client, other, "About the second"))


def test_history_unknown_image(client):
    assert_error(client.get(f"/chat/{uuid.uuid4()}/history"), 404, "image_not_found")
    assert_error(client.delete(f"/chat/{uuid.uuid4()}/history"), 404, "image_not_found")
    assert_error(client.get("/chat/nope/history"), 400, "invalid_image_id")


def test_delete_history(client, image_id):
    chat(client, image_id, "Remember this")
    assert client.delete(f"/chat/{image_id}/history").status_code == 204
    assert history(client, image_id) == []
    assert "previous question" not in answer(chat(client, image_id, "Fresh start"))


# --- streaming -------------------------------------------------------------

def test_stream_records_turn(client, image_id):
    text = stream_text(client, image_id, "What colour is it?")
    messages = history(client, image_id)
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "What colour is it?"), ("assistant", text)]


def test_context_crosses_stream_and_non_stream(client, image_id):
    chat(client, image_id, "First, plain")
    assert "First, plain" in stream_text(client, image_id, "Second, streamed")
    assert "Second, streamed" in answer(chat(client, image_id, "Third, plain"))
    assert len(history(client, image_id)) == 6


def test_dropped_stream_saves_nothing_until_resumed(client, image_id):
    dropped = stream(client, image_id, "Will I be saved?")
    events = iter(dropped.response)
    next(events)                                         # retry hint
    last_id = next(events).decode().split("\n")[0].split("id: ", 1)[1]
    dropped.close()
    assert history(client, image_id) == []               # no partial answer stored

    resumed = stream(client, image_id, headers={"Last-Event-ID": last_id})
    resumed.get_data()
    messages = history(client, image_id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Will I be saved?"
    assert messages[1]["content"].startswith("Mock answer")   # full text, not the tail


def test_replayed_stream_is_not_saved_twice(client, image_id):
    events = parse_sse(stream(client, image_id, "Once only").get_data(as_text=True))
    stream(client, image_id, headers={"Last-Event-ID": events[0][0]}).get_data()
    assert len(history(client, image_id)) == 2


def test_resume_is_identical_after_history_changed(client, image_id):
    full = parse_sse(stream(client, image_id, "Stream one").get_data(as_text=True))
    chat(client, image_id, "Something in between")
    resumed = parse_sse(
        stream(client, image_id, headers={"Last-Event-ID": full[1][0]}).get_data(as_text=True))
    assert resumed == full[2:]


# --- cleanup and concurrency -----------------------------------------------

def test_history_is_capped(client, image_id, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_HISTORY_MESSAGES", 4)
    for i in range(3):
        chat(client, image_id, f"question {i}")
    messages = history(client, image_id)
    assert [m["content"] for m in messages if m["role"] == "user"] == ["question 1", "question 2"]


def test_idle_history_expires(client, image_id, monkeypatch):
    chat(client, image_id, "Old news")
    monkeypatch.setattr(app_module, "HISTORY_TTL", -1)
    monkeypatch.setattr(app_module, "HISTORY_PURGE_INTERVAL", 0)
    assert history(client, image_id) == []
    from conftest import count_rows
    assert count_rows(app_module.ChatHistory) == 0


def test_concurrent_chats_keep_turns_intact(tmp_path, monkeypatch):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    monkeypatch.setattr(app_module, "MOCK_CHAT_DELAY", 0.01)
    monkeypatch.setattr(app_module, "STREAM_CHUNK_DELAY", 0)
    with app_module.app.test_client() as c:
        image_id = upload(c, PNG).get_json()["image_id"]

    def one(i):
        with app_module.app.test_client() as c:
            if i % 2:
                return stream(c, image_id, f"q{i}").get_data() and 200
            return chat(c, image_id, f"q{i}").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert set(pool.map(one, range(20))) == {200}

    messages = app_module.get_history(image_id)
    assert len(messages) == 40
    for user, assistant in zip(messages[::2], messages[1::2]):
        assert (user["role"], assistant["role"]) == ("user", "assistant")
        assert user["completion_id"] == assistant["completion_id"]
        assert user["content"] in assistant["content"]
    assert sorted(m["content"] for m in messages[::2]) == sorted(f"q{i}" for i in range(20))
