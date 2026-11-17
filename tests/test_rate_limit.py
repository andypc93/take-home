"""Rate limiting tests: limiter unit behaviour and endpoint enforcement."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_q1 import PNG, app_module, assert_error, upload


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limiter(clock):
    return app_module.SlidingWindowRateLimiter(clock=clock)


@pytest.fixture
def client(tmp_path):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    with app_module.app.test_client() as c:
        yield c


# --- limiter ---------------------------------------------------------------

def test_allows_up_to_limit_then_blocks(limiter):
    assert [limiter.check("a", 2, 60) for _ in range(2)] == [0, 0]
    assert limiter.check("a", 2, 60) == 60


def test_keys_are_independent(limiter):
    assert limiter.check("a", 1, 60) == 0
    assert limiter.check("b", 1, 60) == 0
    assert limiter.check("a", 1, 60) > 0


def test_window_slides(limiter, clock):
    limiter.check("a", 2, 60)
    clock.now += 30
    limiter.check("a", 2, 60)
    assert limiter.check("a", 2, 60) == 30      # first hit expires in 30s
    clock.now += 30
    assert limiter.check("a", 2, 60) == 0       # first hit gone, one slot free
    assert limiter.check("a", 2, 60) == 30


def test_blocked_requests_do_not_extend_the_block(limiter, clock):
    limiter.check("a", 1, 60)
    for _ in range(5):
        clock.now += 10
        assert limiter.check("a", 1, 60) > 0
    clock.now += 10
    assert limiter.check("a", 1, 60) == 0


def test_idle_keys_are_purged(limiter, clock):
    for i in range(100):
        limiter.check(f"k{i}", 5, 60)
    clock.now += 61
    limiter.check("fresh", 5, 60)
    assert list(limiter._hits) == ["fresh"]


def test_limiter_is_thread_safe():
    limiter = app_module.SlidingWindowRateLimiter()
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: limiter.check("a", 10, 60), range(200)))
    assert results.count(0) == 10


# --- shared (database) limiter ---------------------------------------------

@pytest.fixture
def shared(clock):
    return app_module.DatabaseRateLimiter(clock=clock)


def test_shared_allows_up_to_limit_then_blocks(shared):
    assert [shared.check("a", 3, 60) for _ in range(3)] == [0, 0, 0]
    assert 1 <= shared.check("a", 3, 60) <= 60
    assert shared.check("b", 3, 60) == 0                 # other keys unaffected


def test_shared_limit_spans_workers(shared, clock):
    other_worker = app_module.DatabaseRateLimiter(clock=clock)
    assert shared.check("a", 2, 60) == 0
    assert other_worker.check("a", 2, 60) == 0
    assert shared.check("a", 2, 60) > 0
    assert other_worker.check("a", 2, 60) > 0


def test_shared_window_slides(shared, clock):
    clock.now = 1020.0                                   # window [1020, 1080)
    for _ in range(4):
        shared.check("a", 4, 60)
    clock.now = 1080.0 + 15                              # 25% into the next window
    assert shared.check("a", 4, 60) == 0                 # 4 * 0.75 + 1 = 4
    assert shared.check("a", 4, 60) > 0                  # 4 * 0.75 + 2 = 5
    clock.now = 1140.0 + 60                              # two windows later: clean slate
    assert shared.check("a", 4, 60) == 0


def test_shared_blocked_requests_do_not_extend_the_block(shared, clock):
    clock.now = 1020.0
    for _ in range(50):
        shared.check("a", 2, 60)
    assert app_module.db.engine is not None
    from conftest import count_rows
    with app_module.db.session() as session:
        stored = session.execute(app_module.select(app_module.RateLimitCounter.count)).scalar_one()
    assert stored == 3 and count_rows(app_module.RateLimitCounter) == 1
    clock.now = 1140.0                                   # previous window fully out of view
    assert shared.check("a", 2, 60) == 0


def test_shared_limiter_is_race_free():
    limiter = app_module.DatabaseRateLimiter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: limiter.check("race", 10, 3600), range(60)))
    assert results.count(0) == 10


def test_falls_back_to_memory_limiter_when_database_is_down(monkeypatch):
    def broken(*args, **kwargs):
        from sqlalchemy.exc import OperationalError
        raise OperationalError("x", {}, Exception("down"))

    monkeypatch.setattr(app_module.db, "session", broken)
    assert app_module.check_rate_limit("k", 1, 60) == 0
    assert app_module.check_rate_limit("k", 1, 60) > 0


def test_fallback_is_logged_with_traceback(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(app_module.shared_rate_limiter, "check", broken)
    with caplog.at_level("ERROR", logger="visual_assistant"):
        assert app_module.check_rate_limit("k", 5, 60) == 0
    record = [r for r in caplog.records if "Shared rate limiter failed" in r.getMessage()][0]
    assert record.exc_info and record.exc_info[0] is RuntimeError
    assert "k" in record.getMessage()


def test_fallback_does_not_start_from_zero(monkeypatch):
    """Requests counted while the shared store worked still count after it fails."""
    assert app_module.check_rate_limit("k", 2, 60) == 0
    assert app_module.check_rate_limit("k", 2, 60) == 0

    def broken(*args, **kwargs):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(app_module.shared_rate_limiter, "check", broken)
    assert app_module.check_rate_limit("k", 2, 60) > 0   # no fresh allowance of 2


# --- wall-clock steps (the shared limiter needs wall time to agree across hosts) ---

def test_natural_window_change_keeps_the_block(shared, clock):
    clock.now = 1075.0                                   # window [1020, 1080)
    assert [shared.check("a", 2, 60) for _ in range(2)] == [0, 0]
    clock.now = 1081.0                                   # one second into the next window
    assert shared.check("a", 2, 60) > 0                  # 2 * 59/60 + 1 > 2


def test_forward_clock_step_past_half_a_window_lets_a_request_through(shared, clock):
    """Documents a known property: if the wall clock jumps forward by more than
    half a window between requests, the previous window's weight drops below
    one request and the next request is allowed."""
    clock.now = 1075.0
    assert [shared.check("a", 2, 60) for _ in range(2)] == [0, 0]
    clock.now = 1080.0 + 31                              # stepped 36 s ahead
    assert shared.check("a", 2, 60) == 0                 # 2 * 29/60 + 1 <= 2


def test_backward_clock_step_into_the_previous_window_lets_a_request_through(shared, clock):
    clock.now = 1025.0                                   # window [1020, 1080)
    assert [shared.check("a", 2, 60) for _ in range(2)] == [0, 0]
    clock.now = 1015.0                                   # stepped back 10 s: window [960, 1020)
    assert shared.check("a", 2, 60) == 0                 # that window has no hits yet


def test_clock_step_between_uploads_allows_a_third_upload_at_limit_two(client, monkeypatch, clock):
    """The same property seen through the endpoint: with a limit of 2, a third
    upload succeeds if the wall clock was stepped in between. Without the step
    it is refused (test_upload_rate_limit)."""
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (2, 60), "chat": (100, 60)})
    monkeypatch.setattr(app_module, "shared_rate_limiter", app_module.DatabaseRateLimiter(clock=clock))
    clock.now = 1075.0
    assert upload(client, PNG).status_code == 201
    assert upload(client, PNG).status_code == 201
    clock.now = 1080.0 + 31
    assert upload(client, PNG).status_code == 201


def test_fallback_at_the_endpoint_still_refuses_the_third_upload(client, monkeypatch):
    """If the shared store fails between uploads, the limit still holds."""
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (2, 60), "chat": (100, 60)})
    assert upload(client, PNG).status_code == 201
    assert upload(client, PNG).status_code == 201

    def broken(*args, **kwargs):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(app_module.shared_rate_limiter, "check", broken)
    assert_error(upload(client, PNG), 429, "rate_limit_exceeded")


def test_memory_storage_setting_skips_database(monkeypatch):
    from conftest import count_rows
    monkeypatch.setattr(app_module, "RATE_LIMIT_STORAGE", "memory")
    assert app_module.check_rate_limit("k", 1, 60) == 0
    assert count_rows(app_module.RateLimitCounter) == 0


def test_cleanup_removes_old_counters(shared, clock):
    from conftest import count_rows
    shared.check("old", 5, 60)                           # clock is at t=1000, long ago
    app_module.DatabaseRateLimiter().check("new", 5, 60)
    app_module.run_cleanup()
    assert count_rows(app_module.RateLimitCounter) == 1


# --- endpoints -------------------------------------------------------------

def test_upload_rate_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (2, 60), "chat": (100, 60)})
    assert upload(client, PNG).status_code == 201
    assert upload(client, PNG).status_code == 201
    resp = upload(client, PNG)
    assert_error(resp, 429, "rate_limit_exceeded")
    assert resp.get_json()["error"]["type"] == "rate_limit_error"
    assert 1 <= int(resp.headers["Retry-After"]) <= 60


def test_chat_and_stream_share_a_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (100, 60), "chat": (2, 60)})
    monkeypatch.setattr(app_module, "STREAM_CHUNK_DELAY", 0)
    image_id = upload(client, PNG).get_json()["image_id"]
    assert client.post(f"/chat/{image_id}", json={"question": "hi"}).status_code == 200
    assert client.post(f"/chat-stream/{image_id}", json={"question": "hi"}).status_code == 200
    assert_error(client.post(f"/chat/{image_id}", json={"question": "hi"}), 429, "rate_limit_exceeded")
    assert_error(client.post(f"/chat-stream/{image_id}", json={"question": "hi"}),
                 429, "rate_limit_exceeded")


def test_scopes_are_independent(client, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (1, 60), "chat": (1, 60)})
    image_id = upload(client, PNG).get_json()["image_id"]
    assert upload(client, PNG).status_code == 429
    assert client.post(f"/chat/{image_id}", json={"question": "hi"}).status_code == 200


def test_invalid_requests_count_toward_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (2, 60), "chat": (100, 60)})
    upload(client, b"junk", "a.png")
    upload(client, b"junk", "a.png")
    assert upload(client, PNG).status_code == 429
