"""Database layer: models, engine and session management, migrations, cache.

The database is chosen by the DATABASE_URL environment variable. Any
SQLAlchemy URL works; use PostgreSQL in production, e.g.
    DATABASE_URL=postgresql+psycopg://user:password@host:5432/visual_assistant
Without it, a local SQLite file is used so the project runs with no setup.
"""
import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import (JSON, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def default_database_url():
    path = os.path.join(BASE_DIR, "visual_assistant.db").replace("\\", "/")
    return os.environ.get("DATABASE_URL") or f"sqlite:///{path}"


class Base(DeclarativeBase):
    pass


class UploadedImage(Base):
    __tablename__ = "uploaded_images"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(512))
    uploaded_at: Mapped[float] = mapped_column(Float, index=True)  # unix time
    analysis: Mapped[dict] = mapped_column(JSON)
    # SHA-256 of the API key that uploaded the image; NULL when keys are not in use.
    owner: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class ChatHistory(Base):
    """One conversation turn. Question and answer share a row, so a turn is
    written atomically and can never be half present."""
    __tablename__ = "chat_history"
    __table_args__ = (
        UniqueConstraint("completion_id", name="uq_chat_history_completion_id"),
        Index("ix_chat_history_image_id_id", "image_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    image_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("uploaded_images.id", ondelete="CASCADE"))
    completion_id: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)  # unix time


class StreamSession(Base):
    """What is needed to regenerate a stream when a client reconnects."""
    __tablename__ = "stream_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    image_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("uploaded_images.id", ondelete="CASCADE"))
    prompt: Mapped[str] = mapped_column(Text)
    history: Mapped[list] = mapped_column(JSON)
    created: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[float] = mapped_column(Float, index=True)  # unix time


class RateLimitCounter(Base):
    """Requests counted per key and time window; shared by every worker."""
    __tablename__ = "rate_limit_counters"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_start: Mapped[int] = mapped_column(Integer, primary_key=True)  # unix time
    count: Mapped[int] = mapped_column(Integer)


def run_migrations(url):
    """Upgrades the database at `url` to the latest schema (idempotent)."""
    from alembic import command
    from alembic.config import Config

    config = Config(os.path.join(BASE_DIR, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(BASE_DIR, "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")


def _configure_sqlite(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")    # readers do not block the writer
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Database:
    """Owns the engine and hands out short-lived sessions."""

    def __init__(self):
        self.url = None
        self.engine = None
        self._sessionmaker = None

    def init(self, url, migrate=True):
        """Creates the engine for `url` (replacing any previous one) and migrates."""
        if self.engine is not None:
            self.engine.dispose()
        # pool_pre_ping replaces connections the server dropped while idle;
        # pool_recycle retires them before typical server-side idle timeouts.
        options = {"pool_pre_ping": True, "pool_recycle": 1800}
        if url.startswith("sqlite"):
            options["connect_args"] = {"timeout": 30}   # wait for the write lock
        else:
            options.update(pool_size=10, max_overflow=20, pool_timeout=10)
        self.url = url
        self.engine = create_engine(url, **options)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _configure_sqlite)
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False)
        if migrate:
            run_migrations(url)

    @contextmanager
    def session(self):
        """One transaction: commits on success, rolls back on error, always closes.

        Sessions are scoped to a unit of work rather than to a request because
        streaming generators run after the request context is torn down.
        """
        session = self._sessionmaker()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()


db = Database()


class TTLCache:
    """Small thread-safe read-through cache with expiry and a size bound.

    Per process, so only use it where another worker's write cannot make an
    entry wrong: for immutable rows (get_or_load), or with a freshness check
    against the database on every read (get/set, as the history cache does).
    """

    def __init__(self, ttl, max_size=1000, clock=time.monotonic):
        self.ttl = ttl
        self.max_size = max_size
        self._clock = clock
        self._data = OrderedDict()   # key -> (expires at, value), least recently used first
        self._lock = threading.Lock()
        self._version = 0            # bumped by every invalidation

    def get_or_load(self, key, loader):
        """Returns the cached value, or calls loader() and caches a non-None result.

        If anything was invalidated while the loader ran, its result may
        predate that write, so it is returned but not cached.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is not None and entry[0] > self._clock():
                self._data.move_to_end(key)
                return entry[1]
            version = self._version
        value = loader()
        if value is not None:
            with self._lock:
                if version == self._version:
                    self._data[key] = (self._clock() + self.ttl, value)
                    self._data.move_to_end(key)
                    while len(self._data) > self.max_size:
                        self._data.popitem(last=False)
        return value

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None or entry[0] <= self._clock():
                return None
            self._data.move_to_end(key)
            return entry[1]

    def set(self, key, value):
        with self._lock:
            self._data[key] = (self._clock() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    def delete(self, key):
        with self._lock:
            self._data.pop(key, None)
            self._version += 1

    def clear(self):
        with self._lock:
            self._data.clear()
            self._version += 1

    def __contains__(self, key):
        with self._lock:
            entry = self._data.get(key)
            return entry is not None and entry[0] > self._clock()

    def __len__(self):
        with self._lock:
            return len(self._data)
