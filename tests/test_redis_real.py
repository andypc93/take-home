"""Redis rate limiter against a real Redis server.

Runs only when TEST_REDIS_URL points at a Redis server, e.g.
    TEST_REDIS_URL=redis://127.0.0.1:6379/15
Keys named ratelimit:* in that database are deleted before every test.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import count_rows
from test_q1 import PNG, app_module, upload

REDIS_URL = os.environ.get("TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL not set")


@pytest.fixture
def connection():
    import redis
    client = redis.Redis.from_url(REDIS_URL, socket_timeout=2)
    assert client.ping()
    return client


@pytest.fixture
def limiter(connection):
    return app_module.RedisRateLimiter(connection)


def test_server_is_a_real_redis(connection):
    info = connection.info("server")
    assert info["redis_version"] and info["process_id"] != os.getpid()


def test_allows_up_to_limit_then_blocks(limiter):
    assert [limiter.check("real", 3, 60) for _ in range(3)] == [0, 0, 0]
    assert 1 <= limiter.check("real", 3, 60) <= 60
    assert limiter.check("other", 3, 60) == 0


def test_limit_is_shared_between_connections(limiter):
    import redis
    second = app_module.RedisRateLimiter(redis.Redis.from_url(REDIS_URL))
    assert limiter.check("shared", 2, 60) == 0
    assert second.check("shared", 2, 60) == 0
    assert limiter.check("shared", 2, 60) > 0 and second.check("shared", 2, 60) > 0


def test_exactly_limit_requests_pass_under_concurrency(limiter):
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: limiter.check("race", 10, 3600), range(200)))
    assert results.count(0) == 10


def test_counter_is_capped_and_expires(limiter, connection):
    for _ in range(30):
        limiter.check("cap", 4, 60)
    key = [k for k in connection.keys("ratelimit:cap:*")][0]
    assert int(connection.get(key)) == 5                 # limit + 1, not 30
    assert 0 < connection.ttl(key) <= 120


def test_window_really_expires_on_the_server(limiter, connection):
    assert limiter.check("ttl", 1, 1) == 0
    assert limiter.check("ttl", 1, 1) > 0
    time.sleep(2.1)                                      # both 1 s windows have passed
    assert limiter.check("ttl", 1, 1) == 0


def test_endpoints_count_in_redis_not_the_database(tmp_path, limiter, connection, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMIT_STORAGE", "redis")
    monkeypatch.setattr(app_module, "redis_rate_limiter", limiter)
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (2, 60), "chat": (100, 60)})
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    with app_module.app.test_client() as client:
        assert [upload(client, PNG).status_code for _ in range(3)] == [201, 201, 429]
    assert connection.keys("ratelimit:upload:*")
    assert count_rows(app_module.RateLimitCounter) == 0


def test_unreachable_redis_falls_back_to_memory_limiter(monkeypatch):
    import redis
    dead = redis.Redis.from_url("redis://127.0.0.1:1", socket_connect_timeout=0.3)
    monkeypatch.setattr(app_module, "RATE_LIMIT_STORAGE", "redis")
    monkeypatch.setattr(app_module, "redis_rate_limiter", app_module.RedisRateLimiter(dead))
    assert app_module.check_rate_limit("fallback", 1, 60) == 0    # real connection error inside
    assert app_module.check_rate_limit("fallback", 1, 60) > 0
