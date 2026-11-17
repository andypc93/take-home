import atexit
import os
import shutil
import sys
import tempfile
import time

import pytest

# Point the app at a throwaway database before it is imported. Set
# TEST_DATABASE_URL to run the suite against another database (e.g. PostgreSQL);
# its tables are emptied before every test, so never point it at real data.
_db_dir = tempfile.mkdtemp(prefix="visual_assistant_test_")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or (
    "sqlite:///" + os.path.join(_db_dir, "test.db").replace("\\", "/"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from sqlalchemy import delete, func, select  # noqa: E402


@atexit.register
def _remove_test_database():
    app_module.db.engine.dispose()
    shutil.rmtree(_db_dir, ignore_errors=True)


def count_rows(model):
    with app_module.db.session() as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Empty database and caches, relaxed rate limits, no background cleanup."""
    monkeypatch.setattr(app_module, "RATE_LIMITS", {"upload": (10**6, 60), "chat": (10**6, 60)})
    app_module.rate_limiter.reset()
    app_module.metrics.reset()
    # Endpoint tests must not depend on the wall clock: freeze the shared
    # limiter mid-window, so a minute boundary or a clock step cannot change
    # a verdict. The limiter's time behaviour is tested with its own fake clocks.
    frozen = int(time.time() // 60) * 60 + 30.0
    monkeypatch.setattr(app_module, "shared_rate_limiter",
                        app_module.DatabaseRateLimiter(clock=lambda: frozen))
    with app_module.db.session() as session:
        for model in (app_module.ChatHistory, app_module.StreamSession, app_module.UploadedImage,
                      app_module.RateLimitCounter):
            session.execute(delete(model))
    app_module.image_cache.clear()
    app_module.history_cache.clear()
    app_module._cleanup_state["last_run"] = time.time()
    if os.environ.get("TEST_REDIS_URL"):
        import redis
        client = redis.Redis.from_url(os.environ["TEST_REDIS_URL"])
        stale = client.keys("ratelimit:*")
        if stale:
            client.delete(*stale)
