"""Q2 tests: SSE streaming endpoint and streaming mock."""
import json
import threading
import uuid

import pytest

from test_q1 import PNG, app_module, assert_error, upload


@pytest.fixture
def client(tmp_path, monkeypatch):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    monkeypatch.setattr(app_module, "STREAM_CHUNK_DELAY", 0)
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def image_id(client):
    return upload(client, PNG).get_json()["image_id"]


def parse_sse(text):
    """Returns [(event id, data string)] for every event that has data."""
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        if "data" in fields:
            events.append((fields.get("id"), fields["data"]))
    return events


def stream(client, image_id, question="What is this?", headers=None):
    return client.post(f"/chat-stream/{image_id}", json={"question": question}, headers=headers)


def text_of(chunks):
    return "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])


# --- streaming mock --------------------------------------------------------

def test_mock_stream_sequence(monkeypatch):
    monkeypatch.setattr(app_module, "STREAM_CHUNK_DELAY", 0)
    items = list(app_module.mock_openai_chat("What is this?", "img", stream=True))
    assert items[-1] == "[DONE]"
    chunks = items[:-1]

    assert len({c["id"] for c in chunks}) == 1 and chunks[0]["id"].startswith("chatcmpl-")
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert all(isinstance(c["created"], int) and c["model"] for c in chunks)

    first, finish, usage = chunks[0], chunks[-2], chunks[-1]
    assert first["choices"][0]["delta"]["role"] == "assistant"
    assert first["choices"][0]["finish_reason"] is None
    assert finish["choices"][0] == {"index": 0, "delta": {}, "logprobs": None, "finish_reason": "stop"}
    assert usage["choices"] == []
    assert usage["usage"]["total_tokens"] == (
        usage["usage"]["prompt_tokens"] + usage["usage"]["completion_tokens"])
    assert all(c["usage"] is None for c in chunks[:-1])


def test_mock_stream_matches_non_stream_content(monkeypatch):
    monkeypatch.setattr(app_module, "STREAM_CHUNK_DELAY", 0)
    full = app_module.mock_openai_chat("What is this?", "img")["choices"][0]["message"]["content"]
    items = list(app_module.mock_openai_chat("What is this?", "img", stream=True))
    assert text_of(items[:-1]) == full


def test_mock_stream_rejects_bad_prompt():
    with pytest.raises(ValueError):
        app_module.mock_openai_chat("", "img", stream=True)


# --- endpoint --------------------------------------------------------------

def test_stream_success(client, image_id):
    resp = stream(client, image_id)
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert resp.headers["Cache-Control"] == "no-cache"
    body = resp.get_data(as_text=True)
    assert body.startswith("retry: ")

    events = parse_sse(body)
    assert events[-1][1] == "[DONE]"
    chunks = [json.loads(data) for _, data in events[:-1]]
    completion_id = chunks[0]["id"]
    assert [eid for eid, _ in events] == [f"{completion_id}:{i}" for i in range(len(events))]
    assert "What is this?" in text_of(chunks)
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"


def test_stream_resume_from_last_event_id(client, image_id):
    full = parse_sse(stream(client, image_id).get_data(as_text=True))
    resumed = parse_sse(
        stream(client, image_id, headers={"Last-Event-ID": full[2][0]}).get_data(as_text=True))
    assert resumed == full[3:]


def test_stream_resume_needs_no_body(client, image_id):
    full = parse_sse(stream(client, image_id).get_data(as_text=True))
    resp = client.post(f"/chat-stream/{image_id}", headers={"Last-Event-ID": full[0][0]})
    assert parse_sse(resp.get_data(as_text=True)) == full[1:]


def test_stream_resume_after_done_sends_nothing(client, image_id):
    full = parse_sse(stream(client, image_id).get_data(as_text=True))
    resp = stream(client, image_id, headers={"Last-Event-ID": full[-1][0]})
    assert resp.status_code == 200
    assert parse_sse(resp.get_data(as_text=True)) == []


@pytest.mark.parametrize("value", ["garbage", "chatcmpl-abc:x", ":3"])
def test_stream_bad_last_event_id(client, image_id, value):
    assert_error(stream(client, image_id, headers={"Last-Event-ID": value}),
                 400, "invalid_last_event_id")


def test_stream_resume_unknown_stream(client, image_id):
    assert_error(stream(client, image_id, headers={"Last-Event-ID": "chatcmpl-nope:1"}),
                 404, "stream_not_found")


def test_stream_resume_wrong_image(client, image_id):
    other = upload(client, PNG).get_json()["image_id"]
    full = parse_sse(stream(client, image_id).get_data(as_text=True))
    assert_error(stream(client, other, headers={"Last-Event-ID": full[0][0]}),
                 404, "stream_not_found")


def test_stream_resume_expired(client, image_id, monkeypatch):
    monkeypatch.setattr(app_module, "STREAM_SESSION_TTL", -1)
    full = parse_sse(stream(client, image_id).get_data(as_text=True))
    assert_error(stream(client, image_id, headers={"Last-Event-ID": full[0][0]}),
                 404, "stream_not_found")


def test_expired_sessions_are_purged(client, image_id, monkeypatch):
    monkeypatch.setattr(app_module, "STREAM_SESSION_TTL", -1)
    stream(client, image_id).get_data()
    stream(client, image_id).get_data()
    from conftest import count_rows
    assert count_rows(app_module.StreamSession) == 1


def test_stream_validation_errors_are_json(client, image_id):
    assert_error(stream(client, str(uuid.uuid4())), 404, "image_not_found")
    assert_error(stream(client, "not-a-uuid"), 400, "invalid_image_id")
    assert_error(stream(client, image_id, question=""), 400, "invalid_question")
    assert_error(client.post(f"/chat-stream/{image_id}", data="x", content_type="text/plain"),
                 415, "unsupported_media_type")


def test_stream_limit_returns_429(client, image_id, monkeypatch):
    monkeypatch.setattr(app_module, "stream_slots", threading.BoundedSemaphore(1))
    held = stream(client, image_id)          # not consumed, so it keeps its slot
    refused = stream(client, image_id)
    assert_error(refused, 429, "too_many_streams")
    assert refused.headers["Retry-After"] == "1"
    held.close()


def test_slot_released_after_completion_and_disconnect(client, image_id, monkeypatch):
    monkeypatch.setattr(app_module, "stream_slots", threading.BoundedSemaphore(1))
    done = stream(client, image_id)
    done.get_data()
    done.close()

    dropped = stream(client, image_id)       # client reads one event, then drops
    next(iter(dropped.response))
    dropped.close()

    never_read = stream(client, image_id)    # client drops before reading anything
    never_read.close()

    assert stream(client, image_id).status_code == 200


def test_slot_released_when_server_never_calls_close(client, image_id, monkeypatch):
    """Regression: on a client disconnect the Flask dev server can skip close()
    and just drop the response iterator. The slot must still come back."""
    import gc
    from werkzeug.test import EnvironBuilder

    monkeypatch.setattr(app_module, "stream_slots", threading.BoundedSemaphore(1))
    environ = EnvironBuilder(path=f"/chat-stream/{image_id}", method="POST",
                             json={"question": "hi"}).get_environ()
    app_iter = app_module.app.wsgi_app(environ, lambda status, headers, exc_info=None: None)
    events = iter(app_iter)
    next(events)                                   # stream has started
    assert "active_streams 1" in app_module.metrics.render()

    del events, app_iter                           # dropped without close()
    gc.collect()

    assert "active_streams 0" in app_module.metrics.render()
    assert stream(client, image_id).status_code == 200      # the only slot is free again


def test_slot_released_exactly_once(client, image_id, monkeypatch):
    """close() and generator end both fire; the semaphore must not be over-released."""
    monkeypatch.setattr(app_module, "stream_slots", threading.BoundedSemaphore(1))
    for _ in range(3):
        resp = stream(client, image_id)
        resp.get_data()
        resp.close()                               # BoundedSemaphore would raise on a double release
    assert "active_streams 0" in app_module.metrics.render()


def test_mid_stream_failure_reported_in_band(client, image_id, monkeypatch):
    def broken(*args, **kwargs):
        yield {"id": "x", "choices": []}
        raise RuntimeError("boom")

    monkeypatch.setattr(app_module, "generate_mock_assistant_events", broken)
    events = parse_sse(stream(client, image_id).get_data(as_text=True))
    assert json.loads(events[-1][1])["error"]["code"] == "stream_error"
