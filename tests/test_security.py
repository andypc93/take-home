"""Access control on /metrics, trusted-proxy handling, Redis-backed rate limiting."""
import pytest
from werkzeug.middleware.proxy_fix import ProxyFix

from test_q1 import PNG, app_module, assert_error, upload
from test_rate_limit import FakeClock

REMOTE = {"REMOTE_ADDR": "203.0.113.9"}


@pytest.fixture
def client(tmp_path):
    app_module.app.config.update(TESTING=True, UPLOAD_FOLDER=str(tmp_path))
    with app_module.app.test_client() as c:
        yield c


# --- /metrics --------------------------------------------------------------

def test_metrics_without_token_is_local_only(client):
    assert client.get("/metrics").status_code == 200
    assert_error(client.get("/metrics", environ_base=REMOTE), 403, "forbidden")


def test_metrics_with_token_requires_it_everywhere(client, monkeypatch):
    monkeypatch.setattr(app_module, "METRICS_TOKEN", "s3cret")
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "s3cret"}):
        resp = client.get("/metrics", headers=headers)
        assert_error(resp, 401, "unauthorized")
        assert resp.headers["WWW-Authenticate"] == "Bearer"
    good = {"Authorization": "Bearer s3cret"}
    assert client.get("/metrics", headers=good).status_code == 200
    assert client.get("/metrics", headers=good, environ_base=REMOTE).status_code == 200


def test_health_stays_open_and_reveals_nothing_sensitive(client):
    resp = client.get("/health", environ_base=REMOTE)
    assert resp.get_json() == {"status": "ok", "database": "ok"}


# --- proxies ---------------------------------------------------------------

def test_forwarded_header_is_ignored_without_trusted_proxy(client, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (1, 60), "chat": (100, 60)})
    assert upload(client, PNG).status_code == 201
    forged = client.post("/upload", headers={"X-Forwarded-For": "1.2.3.4"})
    assert forged.status_code == 429            # a forged header does not dodge the limit


def test_trusted_proxy_limits_per_real_client(client, monkeypatch):
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (1, 60), "chat": (100, 60)})
    monkeypatch.setattr(app_module.app, "wsgi_app", ProxyFix(app_module.app.wsgi_app, x_for=1))

    def post(forwarded_for):
        return client.post("/upload", headers={"X-Forwarded-For": forwarded_for}).status_code

    assert post("198.51.100.1") == 400          # counted, then rejected for the missing file
    assert post("198.51.100.2") == 400          # a different client has its own budget
    assert post("198.51.100.1") == 429
    assert post("6.6.6.6, 198.51.100.2") == 429  # only the hop added by our proxy counts


# --- Redis limiter ---------------------------------------------------------

@pytest.fixture
def redis_limiter():
    fakeredis = pytest.importorskip("fakeredis")
    clock = FakeClock()
    clock.now = 1020.0
    return app_module.RedisRateLimiter(fakeredis.FakeRedis(), clock=clock), clock


def test_redis_allows_up_to_limit_then_blocks(redis_limiter):
    limiter, _ = redis_limiter
    assert [limiter.check("a", 3, 60) for _ in range(3)] == [0, 0, 0]
    assert 1 <= limiter.check("a", 3, 60) <= 60
    assert limiter.check("b", 3, 60) == 0


def test_redis_window_slides_and_block_is_not_extended(redis_limiter):
    limiter, clock = redis_limiter
    for _ in range(50):
        limiter.check("a", 4, 60)
    assert int(limiter._client.get("ratelimit:a:1020")) == 5      # capped at limit + 1
    clock.now = 1080.0 + 30
    assert limiter.check("a", 4, 60) == 0        # 5 * 0.5 + 1 = 3.5
    clock.now = 1200.0
    assert limiter.check("a", 4, 60) == 0


def test_redis_keys_expire(redis_limiter):
    limiter, _ = redis_limiter
    limiter.check("a", 4, 60)
    assert 0 < limiter._client.ttl("ratelimit:a:1020") <= 120


def test_redis_is_used_when_configured_and_falls_back_when_down(redis_limiter, monkeypatch):
    limiter, _ = redis_limiter
    monkeypatch.setattr(app_module, "RATE_LIMIT_STORAGE", "redis")
    monkeypatch.setattr(app_module, "redis_rate_limiter", limiter)
    assert app_module.check_rate_limit("k", 1, 60) == 0
    assert app_module.check_rate_limit("k", 1, 60) > 0
    from conftest import count_rows
    assert count_rows(app_module.RateLimitCounter) == 0           # no database write

    class Down:
        def check(self, *args):
            raise ConnectionError("redis unreachable")

    monkeypatch.setattr(app_module, "redis_rate_limiter", Down())
    assert app_module.check_rate_limit("other", 1, 60) == 0       # per-process fallback
    assert app_module.check_rate_limit("other", 1, 60) > 0
