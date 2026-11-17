"""Visual Assistant API: upload an image, then chat about it.

Sections, in order: configuration, errors, metrics, rate limiting, upload
validation, mock AI service, persistence, request helpers, endpoints, streaming.
The mock AI functions return objects in OpenAI Chat Completions format.
"""
import hashlib
import hmac
import json
import logging
import math
import os
import re
import struct
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from functools import wraps
from itertools import islice

from flask import Flask, g, request, jsonify, Response, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from sqlalchemy import case, delete, func, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

# Models, engine/session management, migrations and the cache live in database.py.
from database import (ChatHistory, RateLimitCounter, StreamSession, TTLCache, UploadedImage, db,
                      default_database_url)
from storage import build_storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("visual_assistant")

app = Flask(__name__)

# --- Configuration ---

# Number of reverse proxies in front of the app. Only then is X-Forwarded-For
# trusted (that many hops, no more), so rate limiting sees the real client IP.
# Left at 0 the header is ignored, because any client can forge it.
TRUSTED_PROXIES = int(os.environ.get("TRUSTED_PROXIES", "0"))
if TRUSTED_PROXIES:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXIES, x_proto=TRUSTED_PROXIES)

# Bearer token required by /metrics. Unset: /metrics answers loopback clients only.
METRICS_TOKEN = os.environ.get("METRICS_TOKEN")

# Optional API keys, comma separated. Unset (the default): the API is open.
# Set: upload, chat, stream and history need "Authorization: Bearer <key>",
# and each image can only be used with the key that uploaded it.
API_KEYS = [key.strip() for key in os.environ.get("API_KEYS", "").split(",") if key.strip()]

# Two helpers for testing: POST /reset wipes all data, POST /shutdown stops this
# process. Each is off unless its variable is "1"; the dev server (python app.py)
# turns both on by default, serve.py never does.
app.config['ALLOW_RESET'] = os.environ.get("ALLOW_RESET") == "1"
app.config['ALLOW_SHUTDOWN'] = os.environ.get("ALLOW_SHUTDOWN") == "1"

# Database sessions are opened per operation (db.session()), not per request:
# streaming generators run after the request ends, so a request session would leak.

# With several hosts, point UPLOAD_FOLDER at a folder they all mount. The
# database stores only the file name, so each host finds it under its own mount.
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
MAX_QUESTION_LENGTH = 4000
MAX_JSON_BYTES = 64 * 1024      # chat request bodies; far above any valid question
MAX_IMAGE_PIXELS = 50_000_000   # width x height; rejects decompression bombs
STALE_UPLOAD_AGE = 3600         # seconds before a leftover staging file is deleted
UPLOAD_FIELD = 'image'
VISION_MODEL = 'gpt-4o'
CHAT_MODEL = 'gpt-4o'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# Persistent storage (Q4). Read-through caches sit in front of the database;
# each write invalidates its key after the transaction commits.
IMAGE_TTL = 30 * 24 * 3600      # seconds before an image and all its data are deleted
DB_RETRIES = 2                  # extra attempts after a transient database error
image_cache = TTLCache(ttl=300, max_size=1000)     # image rows never change
history_cache = TTLCache(ttl=300, max_size=1000)   # entries are validated on every read
_cleanup_state = {"last_run": 0.0}
_cleanup_lock = threading.Lock()

# Streaming (Q2)
STREAM_CHUNK_DELAY = float(os.environ.get("STREAM_CHUNK_DELAY", "0.03"))  # s between mock chunks
SSE_RETRY_MS = 3000             # reconnect delay hint sent to clients
STREAM_SESSION_TTL = 300        # seconds a stream stays resumable (state is in the database)
# Each stream holds one thread of this process, so the cap is per process.
MAX_CONCURRENT_STREAMS = int(os.environ.get("MAX_CONCURRENT_STREAMS", "100"))
stream_slots = threading.BoundedSemaphore(MAX_CONCURRENT_STREAMS)

# Conversation history (Q3)
# Simulated latency of the mock AI calls, in seconds (chat answer, image analysis).
MOCK_CHAT_DELAY = float(os.environ.get("MOCK_CHAT_DELAY", "0.2"))
MOCK_VISION_DELAY = float(os.environ.get("MOCK_VISION_DELAY", "0.1"))
MAX_HISTORY_MESSAGES = 50       # stored per image; oldest messages drop first
MAX_CONTEXT_MESSAGES = 20       # most recent messages handed to the model
HISTORY_TTL = 24 * 3600         # seconds of inactivity before a history is deleted
HISTORY_PURGE_INTERVAL = 60     # seconds between cleanup sweeps of old data

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# Final home of image files: local folder or S3 (see storage.py). UPLOAD_FOLDER
# is always used as the local staging area while an upload is validated.
storage = build_storage(lambda: app.config['UPLOAD_FOLDER'])

# Schema is managed by Alembic migrations. With several workers, run
# `alembic upgrade head` once at deploy time and set AUTO_MIGRATE=0.
db.init(default_database_url(), migrate=os.environ.get("AUTO_MIGRATE", "1") == "1")

# Magic-byte signatures -> (canonical type, MIME type). The extension alone is
# attacker-controlled, so the content has to prove it is an image too.
IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpeg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)
EXTENSION_TYPES = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif"}
STORED_EXTENSIONS = {"png": "png", "jpeg": "jpg", "gif": "gif"}  # canonical type -> file extension


# --- Errors: every failure is returned as JSON in OpenAI's error format ---

class APIError(Exception):
    """An error returned to the client in the OpenAI error envelope."""

    def __init__(self, status, code, message, error_type="invalid_request_error", headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.error_type = error_type
        self.headers = headers or {}


def error_response(status, code, message, error_type="invalid_request_error"):
    """Builds the JSON error body and status."""
    body = {"error": {"message": message, "type": error_type, "param": None, "code": code}}
    return jsonify(body), status


@app.errorhandler(APIError)
def handle_api_error(err):
    response, status = error_response(err.status, err.code, err.message, err.error_type)
    response.headers.update(err.headers)
    return response, status


@app.errorhandler(HTTPException)
def handle_http_exception(err):
    """Flask's own errors (404, 405, 413, ...) in the same JSON format."""
    codes = {404: "not_found", 405: "method_not_allowed", 413: "file_too_large"}
    message = err.description
    if err.code == 413:
        message = f"Upload exceeds the {app.config['MAX_CONTENT_LENGTH']} byte limit."
    error_type = "invalid_request_error" if err.code < 500 else "server_error"
    return error_response(err.code, codes.get(err.code, "http_error"), message, error_type)


@app.errorhandler(SQLAlchemyError)
def handle_database_error(err):
    logger.exception("Database error on %s %s", request.method, request.path)
    response, status = error_response(
        503, "database_unavailable",
        "The database is temporarily unavailable. Retry shortly.", "server_error")
    response.headers["Retry-After"] = "1"
    return response, status


@app.errorhandler(Exception)
def handle_unexpected_error(err):
    logger.exception("Unhandled error on %s %s", request.method, request.path)
    return error_response(500, "internal_error", "Internal server error.", "server_error")


# --- Metrics ---

class Metrics:
    """Request counters in Prometheus text format, served at /metrics.

    Per process: Prometheus scrapes each instance and aggregates. Latency is
    time until the response starts, so for streams it excludes the stream body.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._active_streams = 0
        self.reset_counts()

    def reset_counts(self):
        """Clears the request counters but keeps the gauge of streams that are still open."""
        with self._lock:
            self._requests = {}    # (method, route, status) -> count
            self._durations = {}   # route -> [seconds total, count]

    def observe(self, method, route, status, seconds):
        with self._lock:
            key = (method, route, status)
            self._requests[key] = self._requests.get(key, 0) + 1
            total = self._durations.setdefault(route, [0.0, 0])
            total[0] += seconds
            total[1] += 1

    def stream_opened(self):
        with self._lock:
            self._active_streams += 1

    def stream_closed(self):
        with self._lock:
            self._active_streams -= 1

    def render(self):
        with self._lock:
            lines = ["# HELP http_requests_total Requests handled, by method, route and status.",
                     "# TYPE http_requests_total counter"]
            lines += [f'http_requests_total{{method="{m}",route="{r}",status="{s}"}} {n}'
                      for (m, r, s), n in sorted(self._requests.items())]
            lines += ["# HELP http_request_duration_seconds Time until the response starts.",
                      "# TYPE http_request_duration_seconds summary"]
            for route, (seconds, count) in sorted(self._durations.items()):
                lines.append(f'http_request_duration_seconds_sum{{route="{route}"}} {seconds:.6f}')
                lines.append(f'http_request_duration_seconds_count{{route="{route}"}} {count}')
            lines += ["# HELP active_streams Streaming responses currently open.",
                      "# TYPE active_streams gauge", f"active_streams {self._active_streams}"]
        return "\n".join(lines) + "\n"


metrics = Metrics()


@app.before_request
def start_timer():
    g.started = time.perf_counter()


@app.after_request
def record_request(response):
    # Label by route template ("/chat/<image_id>"), not raw path, so labels stay few.
    route = request.url_rule.rule if request.url_rule else "unmatched"
    started = g.get("started")
    metrics.observe(request.method, route, response.status_code,
                    time.perf_counter() - started if started else 0.0)
    # Browsers must not guess content types or embed these responses in a frame.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


def image_file_path(stored_name):
    """Resolves a stored file name inside this host's upload folder."""
    return os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(stored_name))


# --- Rate limiting ---

class SlidingWindowRateLimiter:
    """Thread-safe, in-memory sliding-window limiter (per process).

    Used when RATE_LIMIT_STORAGE=memory, and as the fallback when the shared
    store (Redis or database) is unreachable.
    """

    PURGE_INTERVAL = 60  # seconds between sweeps of idle keys

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._hits = {}  # key -> deque of hit timestamps, oldest first
        self._lock = threading.Lock()
        self._last_purge = clock()
        self._horizon = 0  # longest window seen; idle keys older than this are dropped

    def check(self, key, limit, window):
        """Records a hit and returns 0 if allowed, else seconds until a retry can succeed."""
        now = self._clock()
        with self._lock:
            self._horizon = max(self._horizon, window)
            if now - self._last_purge >= self.PURGE_INTERVAL:
                self._purge(now)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, math.ceil(hits[0] + window - now))
            hits.append(now)
            return 0

    def _purge(self, now):
        cutoff = now - self._horizon
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]
        self._last_purge = now

    def reset(self):
        with self._lock:
            self._hits.clear()


def _rate_limit_setting(name, default):
    """Parses 'requests/seconds', e.g. UPLOAD_RATE_LIMIT=10/60."""
    requests_, _, seconds = os.environ.get(name, default).partition("/")
    return int(requests_), int(seconds)


# scope -> (max requests, window in seconds), counted per API key, or per client IP without one
RATE_LIMITS = {"upload": _rate_limit_setting("UPLOAD_RATE_LIMIT", "10/60"),
               "chat": _rate_limit_setting("CHAT_RATE_LIMIT", "60/60")}
rate_limiter = SlidingWindowRateLimiter()
RATE_LIMIT_RETENTION = 3600     # seconds before old shared counters are deleted
# Where counters live. "redis": shared, in memory, cheapest per request (needs
# REDIS_URL). "database": shared, one small write per request. "memory": per
# process. Default: redis when REDIS_URL is set, otherwise database.
REDIS_URL = os.environ.get("REDIS_URL")
RATE_LIMIT_STORAGE = os.environ.get("RATE_LIMIT_STORAGE", "redis" if REDIS_URL else "database")


def sliding_window_verdict(count, previous, limit, window, now, current):
    """0 if a request is allowed, else seconds until a retry can succeed.

    `count` is this window's hits including the current one, `previous` the
    window before; the previous window is weighted by how much of it still
    overlaps the sliding window.
    """
    elapsed = (now - current) / window
    if count > limit:
        return max(1, math.ceil(current + window - now))
    if previous * (1 - elapsed) + count > limit:
        # Wait until enough of the previous window has slid out of view.
        needed = 1 - (limit - count) / previous
        return max(1, math.ceil((needed - elapsed) * window))
    return 0


class DatabaseRateLimiter:
    """Sliding-window counter stored in the database, so every worker and host
    enforces one shared limit.

    Each request is one atomic upsert (INSERT ... ON CONFLICT DO UPDATE
    RETURNING), so concurrent workers cannot both read a stale count. The
    rate is estimated from the current window plus the previous window
    weighted by how much of it still overlaps the sliding window.
    """

    _INSERT = {"postgresql": postgresql.insert, "sqlite": sqlite.insert}

    def __init__(self, clock=time.time):
        self._clock = clock

    def supported(self):
        return db.engine.dialect.name in self._INSERT

    def check(self, key, limit, window):
        """Records a hit and returns 0 if allowed, else seconds until a retry can succeed."""
        now = self._clock()
        current = int(now // window) * window  # start of the current window
        insert = self._INSERT[db.engine.dialect.name]
        # Stop counting once over the limit, so hammering a blocked endpoint
        # does not push the block into the next window.
        bump = case((RateLimitCounter.count <= limit, RateLimitCounter.count + 1),
                    else_=RateLimitCounter.count)
        upsert = (insert(RateLimitCounter)
                  .values(key=key, window_start=current, count=1)
                  .on_conflict_do_update(index_elements=["key", "window_start"],
                                         set_={"count": bump})
                  .returning(RateLimitCounter.count))
        with db.session() as session:
            count = session.execute(upsert).scalar_one()
            previous = session.execute(
                select(RateLimitCounter.count).where(
                    RateLimitCounter.key == key,
                    RateLimitCounter.window_start == current - window)).scalar() or 0
        return sliding_window_verdict(count, previous, limit, window, now, current)


class RedisRateLimiter:
    """Same sliding-window counter as DatabaseRateLimiter, kept in Redis:
    no database write per request. INCR is atomic, so workers cannot race."""

    def __init__(self, client, clock=time.time):
        self._client = client
        self._clock = clock

    def check(self, key, limit, window):
        """Records a hit and returns 0 if allowed, else seconds until a retry can succeed."""
        now = self._clock()
        current = int(now // window) * window  # start of the current window
        current_key = f"ratelimit:{key}:{current}"
        pipeline = self._client.pipeline()
        pipeline.incr(current_key)
        pipeline.expire(current_key, int(window * 2))   # outlive its use as "previous"
        pipeline.get(f"ratelimit:{key}:{current - window}")
        count, _, previous = pipeline.execute()
        if count > limit + 1:
            # Stop counting once over the limit (see DatabaseRateLimiter).
            self._client.decr(current_key)
            count = limit + 1
        return sliding_window_verdict(count, int(previous or 0), limit, window, now, current)


shared_rate_limiter = DatabaseRateLimiter()
redis_rate_limiter = None
if REDIS_URL:
    import redis  # optional dependency, only needed when REDIS_URL is set
    redis_rate_limiter = RedisRateLimiter(
        redis.Redis.from_url(REDIS_URL, socket_timeout=0.5, socket_connect_timeout=0.5))


def check_rate_limit(key, limit, window):
    """Uses the configured shared limiter; if its store is unreachable, falls
    back to the per-process limiter, so an outage never disables rate limiting.

    While the shared store works its verdict decides, and every request it
    allows is also recorded in the per-process limiter. A fallback therefore
    starts from what this process has already seen, not from zero, and cannot
    hand a client a fresh allowance.
    """
    shared = None
    if RATE_LIMIT_STORAGE == "redis" and redis_rate_limiter is not None:
        shared = redis_rate_limiter
    elif RATE_LIMIT_STORAGE in ("redis", "database") and shared_rate_limiter.supported():
        shared = shared_rate_limiter
    if shared is None:
        return rate_limiter.check(key, limit, window)
    try:
        retry_after = shared.check(key, limit, window)
    except Exception:  # redis.RedisError or SQLAlchemyError
        # Logged with the traceback: a fallback that goes unnoticed is hard to diagnose later.
        logger.exception("Shared rate limiter failed for %s; using per-process limiter", key)
        return rate_limiter.check(key, limit, window)
    if not retry_after:
        rate_limiter.check(key, limit, window)   # record only; the shared verdict stands
    return retry_after


def key_owner(key):
    """What is stored and logged in place of an API key: its SHA-256, never the key."""
    return hashlib.sha256(key.encode()).hexdigest()


def authenticate():
    """Returns the owner id of the request's API key, or None when keys are not in use.

    Raises 401 for a missing or unknown key. Every configured key is compared
    in constant time, so the response time does not reveal how close a guess was.
    """
    if not API_KEYS:
        return None
    scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
    supplied = supplied.strip().encode()
    matched = None
    for key in API_KEYS:
        if hmac.compare_digest(supplied, key.encode()) and scheme.lower() == "bearer":
            matched = key
    if matched is None:
        raise APIError(401, "invalid_api_key",
                       "A valid API key is required: send 'Authorization: Bearer <key>'.",
                       headers={"WWW-Authenticate": "Bearer"})
    return key_owner(matched)


def rate_limited(scope):
    """Authenticates the request (when API keys are in use) and rejects it with
    429 once the client exceeds the scope's limit.

    The limit is counted per API key; without a valid key it is counted per
    client address, so guessing keys is rate limited too.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            limit, window = RATE_LIMITS[scope]
            try:
                g.owner, rejected = authenticate(), None
            except APIError as err:
                g.owner, rejected = None, err
            # remote_addr is the real client only if TRUSTED_PROXIES is set correctly.
            subject = f"key:{g.owner[:16]}" if g.owner else request.remote_addr
            retry_after = check_rate_limit(f"{scope}:{subject}", limit, window)
            if retry_after:
                logger.warning("Rate limit hit: %s by %s", scope, subject)
                raise APIError(429, "rate_limit_exceeded",
                               f"Rate limit of {limit} requests per {window}s exceeded.",
                               "rate_limit_error", {"Retry-After": str(retry_after)})
            if rejected:
                logger.warning("Rejected API key on %s from %s", request.path, request.remote_addr)
                raise rejected
            return view(*args, **kwargs)
        return wrapper
    return decorator


# --- Upload validation ---

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def sniff_image_type(header):
    """Returns (canonical type, MIME type) for known image magic bytes, else None."""
    for signature, image_type, mime in IMAGE_SIGNATURES:
        if header.startswith(signature):
            return image_type, mime
    return None


def describe_unsupported(header):
    """Names a few common formats we do not accept, to make the error message useful."""
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "WebP"
    if header[4:8] == b"ftyp":
        return "HEIC/AVIF"
    if header[:2] == b"BM":
        return "BMP"
    if header[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    if header[:4] == b"%PDF":
        return "PDF"
    return None


# JPEG markers that start a frame header (which holds the image size).
JPEG_FRAME_MARKERS = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def read_image_size(path, image_type):
    """Returns (width, height) from the image's own header, or None if it is damaged.

    This goes one step past the magic bytes: the header has to be well formed
    enough to state a size. No image library is needed for these three formats.
    """
    try:
        with open(path, "rb") as handle:
            if image_type == "png":            # 8-byte signature, then the IHDR chunk
                head = handle.read(24)
                if len(head) < 24 or head[12:16] != b"IHDR":
                    return None
                return struct.unpack(">II", head[16:24])
            if image_type == "gif":            # size follows the 6-byte signature
                head = handle.read(10)
                return struct.unpack("<HH", head[6:10]) if len(head) == 10 else None
            # JPEG: walk the segments until a frame header.
            handle.seek(2)
            while True:
                marker = handle.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                code = marker[1]
                while code == 0xFF:            # padding between segments
                    code = handle.read(1)[0]
                if code in (0xD9, 0xDA):       # image data or end reached without a frame header
                    return None
                length = struct.unpack(">H", handle.read(2))[0]
                if length < 2:
                    return None
                if code in JPEG_FRAME_MARKERS:
                    height, width = struct.unpack(">HH", handle.read(5)[1:5])
                    return width, height
                handle.seek(length - 2, os.SEEK_CUR)
    except (OSError, struct.error, IndexError):
        return None


# --- Mock AI service: responses shaped like OpenAI's Chat Completions API ---

def estimate_tokens(text):
    """Rough token count (~4 characters per token), never less than 1."""
    return max(1, len(text) // 4)


def build_chat_completion(model, content, prompt_tokens):
    """Builds a response shaped like OpenAI's Chat Completions object."""
    completion_tokens = estimate_tokens(content)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": "fp_mock",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": None},
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def mock_openai_vision_analysis(image_path):
    """Simulates initial analysis of the uploaded image.

    Vision requests go through Chat Completions, so the result is a
    chat.completion object. Raises FileNotFoundError if the image is missing.
    """
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")
    if MOCK_VISION_DELAY:
        time.sleep(MOCK_VISION_DELAY)
    size = os.path.getsize(image_path)
    extension = os.path.splitext(image_path)[1].lstrip('.').lower() or "unknown"
    dimensions = read_image_size(image_path, EXTENSION_TYPES.get(extension))
    measured = f"{dimensions[0]}x{dimensions[1]} pixel " if dimensions else ""
    content = (
        f"Mock analysis: this is a {measured}{extension.upper()} image of {size} bytes. "
        "It appears to contain a subject in the foreground against a plain background."
    )
    # 85 tokens is the base cost OpenAI charges for a low-detail image input.
    prompt_tokens = 85 + estimate_tokens("Describe this image.")
    return build_chat_completion(VISION_MODEL, content, prompt_tokens)


def validate_history(history):
    """Returns the context window of a history list; raises ValueError if malformed."""
    if history is None:
        return []
    if not isinstance(history, (list, tuple)):
        raise ValueError("history must be a list of messages")
    for message in history:
        if (not isinstance(message, dict)
                or message.get("role") not in ("system", "user", "assistant")
                or not isinstance(message.get("content"), str)):
            raise ValueError("each history message needs a valid 'role' and string 'content'")
    return list(history[-MAX_CONTEXT_MESSAGES:])


def mock_chat_content(prompt, image_id, history=None):
    """The mock answer; later turns refer back to the previous question."""
    content = f'Mock answer about image {image_id}: you asked "{prompt.strip()}".'
    previous = [m["content"] for m in validate_history(history) if m["role"] == "user"]
    if previous:
        content += (f' This is turn {len(previous) + 1} of our conversation;'
                    f' your previous question was "{previous[-1].strip()}".')
    return content


def mock_prompt_tokens(prompt, history=None):
    """Prompt tokens: image base cost + question + every history message in context."""
    return (85 + estimate_tokens(prompt)
            + sum(estimate_tokens(m["content"]) for m in validate_history(history)))


def generate_mock_assistant_events(prompt, image_id, completion_id=None, created=None, start=0,
                                   history=None):
    """Yields the items of an OpenAI Chat Completions stream, in order:

    1. a chunk whose delta carries the assistant role        (stream created)
    2. one chunk per content piece                           (deltas)
    3. a chunk with an empty delta and finish_reason "stop"  (completed)
    4. a chunk with empty choices carrying token usage
    5. the literal string "[DONE]"

    Output is deterministic for a given (prompt, image_id, completion_id,
    created, history), so a dropped stream can be regenerated without
    buffering anything. On a resume, items before index `start` are yielded
    without delay so the caller can fast-forward past them.
    """
    completion_id = completion_id or f"chatcmpl-{uuid.uuid4().hex}"
    created = created or int(time.time())
    content = mock_chat_content(prompt, image_id, history)
    base = {"id": completion_id, "object": "chat.completion.chunk", "created": created,
            "model": CHAT_MODEL, "system_fingerprint": "fp_mock"}

    def chunk(delta, finish_reason=None):
        choice = {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}
        return {**base, "choices": [choice], "usage": None}

    prompt_tokens = mock_prompt_tokens(prompt, history)
    completion_tokens = estimate_tokens(content)
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
             "total_tokens": prompt_tokens + completion_tokens}

    items = [chunk({"role": "assistant", "content": "", "refusal": None})]
    items += [chunk({"content": piece}) for piece in re.findall(r"\S+\s*", content)]
    items += [chunk({}, "stop"), {**base, "choices": [], "usage": usage}, "[DONE]"]

    for index, item in enumerate(items):
        if index >= start and STREAM_CHUNK_DELAY:
            time.sleep(STREAM_CHUNK_DELAY)
        yield item


def mock_openai_chat(prompt, image_id, stream=False, history=None):
    """Simulates a call to a chat model.

    `history` is the prior conversation as a list of {"role", "content"}
    messages, oldest first; the answer and token usage reflect it.
    Returns a chat.completion dict, or with stream=True a generator of
    chat.completion.chunk dicts ending in "[DONE]".
    Raises ValueError when the prompt or history is malformed.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    history = validate_history(history)
    if stream:
        return generate_mock_assistant_events(prompt, image_id, history=history)
    if MOCK_CHAT_DELAY:
        time.sleep(MOCK_CHAT_DELAY)
    return build_chat_completion(CHAT_MODEL, mock_chat_content(prompt, image_id, history),
                                 prompt_tokens=mock_prompt_tokens(prompt, history))


# --- Persistence: all database reads and writes ---

def db_retry(func):
    """Retries on OperationalError (dropped connection, lock timeout) with backoff.

    Every wrapped operation is one transaction that rolled back on failure, so
    running it again is safe.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(DB_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except OperationalError:
                if attempt == DB_RETRIES:
                    raise
                logger.warning("Database error in %s, retry %d of %d",
                               func.__name__, attempt + 1, DB_RETRIES)
                time.sleep(0.05 * 2 ** attempt)
    return wrapper


def iso(timestamp):
    """Unix time -> ISO 8601 string in UTC."""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def image_record(row):
    """Database row -> plain dict ("path" and "owner" are internal, never sent to clients)."""
    return {"image_id": row.id, "filename": row.filename, "content_type": row.content_type,
            "size": row.size, "uploaded_at": iso(row.uploaded_at), "analysis": row.analysis,
            "path": row.path, "owner": row.owner}


INTERNAL_FIELDS = ("path", "owner")


@db_retry
def save_image(image_id, filename, content_type, size, path, analysis, owner=None):
    """Inserts the image row and returns its record."""
    row = UploadedImage(id=image_id, filename=filename, content_type=content_type, size=size,
                        path=path, uploaded_at=time.time(), analysis=analysis, owner=owner)
    with db.session() as session:
        session.add(row)
    return image_record(row)


@db_retry
def load_image(image_id):
    """Returns the image record, or None. Served from cache when possible."""
    def loader():
        with db.session() as session:
            row = session.get(UploadedImage, image_id)
            return image_record(row) if row is not None else None
    return image_cache.get_or_load(image_id, loader)


@db_retry
def get_history(image_id):
    """Returns a copy of an image's messages, oldest first.

    The cached copy is used only if a cheap index-only query (newest turn id
    and turn count) shows nothing changed. Any insert, trim or delete, from
    this worker or another, changes that pair, so a stale history is never
    served; the cache saves loading and decoding the message rows.
    """
    maybe_cleanup()
    with db.session() as session:
        tag = tuple(session.execute(
            select(func.max(ChatHistory.id), func.count(ChatHistory.id))
            .where(ChatHistory.image_id == image_id)).one())
        cached = history_cache.get(image_id)
        if cached is not None and cached[0] == tag:
            messages = cached[1]
        else:
            turns = session.execute(
                select(ChatHistory).where(ChatHistory.image_id == image_id)
                .order_by(ChatHistory.id)).scalars().all()
            messages = [{"role": role, "content": content, "created_at": iso(turn.created_at),
                         "completion_id": turn.completion_id}
                        for turn in turns
                        for role, content in (("user", turn.question), ("assistant", turn.answer))]
            history_cache.set(image_id, (tag, messages))
    return [dict(message) for message in messages]


@db_retry
def append_turn(image_id, completion_id, question, answer):
    """Stores one question/answer turn and trims the image's history to its cap.

    The turn is a single row in a single transaction, so readers never see a
    question without its answer. completion_id is unique, which makes this
    safe to call again when a resumed stream replays its ending.
    Returns False if the turn was already stored.
    """
    max_turns = max(1, MAX_HISTORY_MESSAGES // 2)
    try:
        with db.session() as session:
            session.add(ChatHistory(image_id=image_id, completion_id=completion_id,
                                    question=question, answer=answer, created_at=time.time()))
            session.flush()
            newest = (select(ChatHistory.id).where(ChatHistory.image_id == image_id)
                      .order_by(ChatHistory.id.desc()).limit(max_turns))
            session.execute(
                delete(ChatHistory)
                .where(ChatHistory.image_id == image_id, ChatHistory.id.not_in(newest))
                .execution_options(synchronize_session=False))
    except IntegrityError:
        return False  # duplicate completion id, or the image was deleted meanwhile
    finally:
        history_cache.delete(image_id)
    maybe_cleanup()
    return True


@db_retry
def clear_history(image_id):
    """Deletes an image's whole conversation."""
    try:
        with db.session() as session:
            session.execute(delete(ChatHistory).where(ChatHistory.image_id == image_id))
    finally:
        history_cache.delete(image_id)


@db_retry
def load_stream_session(completion_id):
    """Returns the saved state of a stream (for resuming), or None."""
    with db.session() as session:
        row = session.get(StreamSession, completion_id)
        if row is None:
            return None
        return {"id": row.id, "image_id": row.image_id, "prompt": row.prompt,
                "history": row.history, "created": row.created, "expires_at": row.expires_at}


def run_cleanup(now=None):
    """Deletes old data: idle histories, expired stream sessions, and images past
    IMAGE_TTL together with their files. Returns how many of each were removed."""
    now = time.time() if now is None else now
    with db.session() as session:
        idle = (select(ChatHistory.image_id).group_by(ChatHistory.image_id)
                .having(func.max(ChatHistory.created_at) < now - HISTORY_TTL))
        histories = session.execute(
            delete(ChatHistory).where(ChatHistory.image_id.in_(idle))
            .execution_options(synchronize_session=False)).rowcount
        streams = session.execute(
            delete(StreamSession).where(StreamSession.expires_at < now)).rowcount
        session.execute(delete(RateLimitCounter).where(
            RateLimitCounter.window_start < now - RATE_LIMIT_RETENTION))
        old_images = session.execute(
            select(UploadedImage.id, UploadedImage.path)
            .where(UploadedImage.uploaded_at < now - IMAGE_TTL)).all()
        ids = [row.id for row in old_images]
        if ids:
            # Children first: do not rely on the database enforcing ON DELETE CASCADE.
            for model, column in ((ChatHistory, ChatHistory.image_id),
                                  (StreamSession, StreamSession.image_id),
                                  (UploadedImage, UploadedImage.id)):
                session.execute(delete(model).where(column.in_(ids))
                                .execution_options(synchronize_session=False))
    history_cache.clear()
    for row in old_images:
        image_cache.delete(row.id)
        try:
            storage.delete(row.path)
        except Exception:
            logger.warning("Could not remove file of expired image %s", row.id)
    stale = remove_stale_uploads(now)
    removed = {"histories": histories, "stream_sessions": streams, "images": len(ids)}
    if any(removed.values()) or stale:
        logger.info("Cleanup removed %s and %d stale staging files", removed, stale)
    return removed


def remove_stale_uploads(now):
    """Deletes staging files left by uploads that never finished (e.g. the process was killed)."""
    folder = app.config['UPLOAD_FOLDER']
    count = 0
    try:
        names = [name for name in os.listdir(folder) if name.startswith("part-")]
    except OSError:
        return 0
    for name in names:
        path = os.path.join(folder, name)
        try:
            if os.path.getmtime(path) < now - STALE_UPLOAD_AGE:
                os.remove(path)
                count += 1
        except OSError:
            pass  # already gone, or in use by an upload that is still running
    return count


def maybe_cleanup():
    """Runs cleanup at most once per HISTORY_PURGE_INTERVAL; never fails the caller."""
    now = time.time()
    with _cleanup_lock:
        if now - _cleanup_state["last_run"] < HISTORY_PURGE_INTERVAL:
            return
        _cleanup_state["last_run"] = now
    try:
        run_cleanup(now)
    except SQLAlchemyError:
        logger.exception("Cleanup failed")


@app.cli.command("cleanup")
def cleanup_command():
    """Delete expired data now:  flask --app app cleanup  (suits a cron job)."""
    print(run_cleanup())


# --- Request helpers: validate input or raise APIError ---

def parse_image_id(image_id):
    """Returns the canonical UUID string, or raises APIError for a malformed ID."""
    try:
        parsed = str(uuid.UUID(image_id))
    except (ValueError, AttributeError, TypeError):
        parsed = None
    if parsed != image_id.lower():
        raise APIError(400, "invalid_image_id", "image_id must be a valid UUID.")
    return parsed


def get_image_or_404(image_id):
    """Returns the image record for a valid, existing id; else raises 400 or 404.

    With API keys in use, an image uploaded with another key (or before keys
    were switched on) is reported as not found, so ids cannot be probed.
    """
    image_id = parse_image_id(image_id)
    record = load_image(image_id)
    if record is not None and API_KEYS and record["owner"] != g.get("owner"):
        record = None
    if record is None:
        raise APIError(404, "image_not_found", f"No image found with id '{image_id}'.")
    return record


def parse_question():
    """Returns the validated 'question' from the JSON body, or raises APIError."""
    if (request.content_length or 0) > MAX_JSON_BYTES:
        raise APIError(413, "request_too_large",
                       f"Request body must be at most {MAX_JSON_BYTES} bytes.")
    if not request.is_json:
        raise APIError(415, "unsupported_media_type", "Content-Type must be application/json.")
    payload = request.get_json(silent=True)
    if payload is None:
        raise APIError(400, "invalid_json", "Request body is not valid JSON.")
    question = payload.get("question") if isinstance(payload, dict) else None
    if not isinstance(question, str) or not question.strip():
        raise APIError(400, "invalid_question", "'question' must be a non-empty string.")
    if len(question) > MAX_QUESTION_LENGTH:
        raise APIError(400, "question_too_long",
                       f"'question' must be at most {MAX_QUESTION_LENGTH} characters.")
    return question


# --- Endpoints ---

@app.route('/', methods=['GET'])
def test_console():
    """Browser page for trying the API by hand (static/demo.html). Not part of the API."""
    response = send_from_directory(os.path.join(app.root_path, "static"), "demo.html")
    # The page is self-contained: allow only its own inline code and same-origin calls.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src 'self' blob:")
    return response

@app.route('/health', methods=['GET'])
def health():
    """Liveness and database check for load balancers and monitoring."""
    try:
        with db.session() as session:
            session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("Health check: database unreachable")
        return jsonify({"status": "degraded", "database": "unavailable"}), 503
    return jsonify({"status": "ok", "database": "ok"}), 200


@app.route('/metrics', methods=['GET'])
def metrics_endpoint():
    """Prometheus scrape target. Needs the METRICS_TOKEN bearer token; with no
    token configured it only answers clients on this machine."""
    if METRICS_TOKEN:
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {METRICS_TOKEN}".encode()):
            raise APIError(401, "unauthorized", "A valid bearer token is required.",
                           headers={"WWW-Authenticate": "Bearer"})
    elif request.remote_addr not in ("127.0.0.1", "::1"):
        raise APIError(403, "forbidden", "Metrics are only available locally.")
    return Response(metrics.render(), mimetype="text/plain; version=0.0.4")


def require_test_helper(name, flag, header):
    """Guards shared by the test helpers (/reset, /shutdown), which are destructive.

    Three checks: the helper must be switched on, the caller must be on this
    machine, and the request must carry a confirmation header, which a form on
    another website cannot send.
    """
    if not app.config.get(flag):
        raise APIError(403, f"{name}_disabled",
                       f"{name.capitalize()} is turned off on this server. Start it with {flag}=1 to allow it.")
    if request.remote_addr not in ("127.0.0.1", "::1"):
        raise APIError(403, "forbidden", f"{name.capitalize()} is only available from this machine.")
    if request.headers.get(header) != "yes":
        raise APIError(400, "confirmation_required", f"Send the header '{header}: yes'.")


def stop_process():
    """Ends this server process half a second from now, after the response has gone out."""
    threading.Timer(0.5, os._exit, args=(0,)).start()


@app.route('/shutdown', methods=['POST'])
def shutdown_server():
    """Test helper: stops this server process, since the dev server has no stop command.

    Finished requests are already committed to the database, so nothing is lost;
    a stream that is open at that moment is cut and can be resumed after a restart.
    """
    require_test_helper("shutdown", "ALLOW_SHUTDOWN", "X-Shutdown-Confirm")
    logger.warning("Shutdown requested by %s; stopping the server", request.remote_addr)
    stop_process()
    return jsonify({"stopping": True}), 200


@app.route('/reset', methods=['POST'])
def reset_everything():
    """Test helper: deletes every image, conversation, stream session and counter."""
    require_test_helper("reset", "ALLOW_RESET", "X-Reset-Confirm")

    with db.session() as session:
        paths = [row.path for row in session.execute(select(UploadedImage.path)).all()]
        turns = session.execute(delete(ChatHistory)).rowcount
        session.execute(delete(StreamSession))
        session.execute(delete(RateLimitCounter))
        session.execute(delete(UploadedImage))
    for path in paths:
        try:
            storage.delete(path)
        except Exception:
            logger.warning("Reset could not remove stored file %s", path)
    remove_stale_uploads(float("inf"))      # every staging file, whatever its age

    image_cache.clear()
    history_cache.clear()
    rate_limiter.reset()
    metrics.reset_counts()
    if redis_rate_limiter is not None:
        try:
            keys = redis_rate_limiter._client.keys("ratelimit:*")
            if keys:
                redis_rate_limiter._client.delete(*keys)
        except Exception:
            logger.warning("Reset could not clear rate-limit counters in Redis")

    logger.warning("Reset by %s: removed %d images and %d conversation turns",
                   request.remote_addr, len(paths), turns)
    return jsonify({"reset": True, "removed": {"images": len(paths), "turns": turns}}), 200


@app.route('/upload', methods=['POST'])
@rate_limited("upload")
def upload_image():
    """Validates and stores an image; returns its id, metadata and initial analysis."""
    # 1. A file must be present, with an allowed extension.
    file = request.files.get(UPLOAD_FIELD)
    if file is None or not file.filename:
        raise APIError(400, "missing_file",
                       f"Send the image as multipart form field '{UPLOAD_FIELD}'.")

    if not allowed_file(file.filename):
        raise APIError(415, "unsupported_file_type",
                       f"Allowed file types: {', '.join(sorted(ALLOWED_EXTENSIONS))}.")
    extension = file.filename.rsplit('.', 1)[1].lower()

    # 2. The content must really be an allowed image (checked by its first bytes).
    header = file.stream.read(16)
    if not header:
        raise APIError(400, "empty_file", "The uploaded file is empty.")
    sniffed = sniff_image_type(header)
    if sniffed is None:
        kind = describe_unsupported(header)
        raise APIError(415, "invalid_image_content",
                       "The file's content is not a PNG, JPEG or GIF image"
                       + (f" (it is a {kind} file)." if kind else "."))
    if sniffed[0] != EXTENSION_TYPES[extension]:
        # A real image saved with the wrong extension is common (downloads,
        # renamed files). The content decides: store it under its true type.
        logger.info("Upload named .%s is really %s; storing as %s", extension, sniffed[1],
                    STORED_EXTENSIONS[sniffed[0]])
        extension = STORED_EXTENSIONS[sniffed[0]]
    file.stream.seek(0)

    # 3. Store under a server-generated name. The client's filename is kept only
    #    as sanitized metadata and never used to build a path.
    image_id = str(uuid.uuid4())
    upload_folder = app.config['UPLOAD_FOLDER']
    os.makedirs(upload_folder, exist_ok=True)
    stored_name = f"{image_id}.{extension}"
    temp_path = os.path.join(upload_folder, f"part-{stored_name}")
    stored = False
    try:
        file.save(temp_path)  # streamed to disk in chunks, not held in memory
        size = os.path.getsize(temp_path)

        # 4. The header must be intact and state a sane size.
        dimensions = read_image_size(temp_path, sniffed[0])
        if dimensions is None or 0 in dimensions:
            raise APIError(415, "invalid_image_content",
                           f"The file starts like a {sniffed[0].upper()} image but is damaged.")
        if dimensions[0] * dimensions[1] > MAX_IMAGE_PIXELS:
            raise APIError(413, "image_dimensions_too_large",
                           f"Image is {dimensions[0]}x{dimensions[1]} pixels; "
                           f"the limit is {MAX_IMAGE_PIXELS} pixels in total.")

        analysis = mock_openai_vision_analysis(temp_path)
        try:
            storage.save(temp_path, stored_name)  # local: atomic rename; S3: upload
        except Exception as err:
            logger.exception("Storing image %s failed", image_id)
            raise APIError(503, "storage_unavailable", "The image could not be stored. Retry shortly.",
                           "server_error", {"Retry-After": "1"}) from err
        stored = True
        record = save_image(image_id, secure_filename(file.filename) or f"upload.{extension}",
                            sniffed[1], size, stored_name, analysis, owner=g.get("owner"))
    except Exception:
        # Covers a failed database insert too: no stored file without a row.
        if os.path.exists(temp_path):
            os.remove(temp_path)
        if stored:
            storage.delete(stored_name)
        raise
    maybe_cleanup()

    logger.info("Uploaded image %s (%s, %d bytes)", image_id, record["content_type"], record["size"])
    public = {key: value for key, value in record.items() if key not in INTERNAL_FIELDS}
    return jsonify(public), 201


@app.route('/chat/<image_id>', methods=['POST'])
@rate_limited("chat")
def chat_about_image(image_id):
    """Answers a question about an image, using and then extending its history."""
    image_id = get_image_or_404(image_id)["image_id"]
    question = parse_question()
    response = mock_openai_chat(question, image_id, history=get_history(image_id))
    append_turn(image_id, response["id"], question, response["choices"][0]["message"]["content"])
    logger.info("Chat on image %s -> %s", image_id, response["id"])
    return jsonify(response), 200


@app.route('/chat/<image_id>/history', methods=['GET'])
@rate_limited("chat")
def get_chat_history(image_id):
    """Returns the conversation for an image, oldest message first."""
    image_id = get_image_or_404(image_id)["image_id"]
    messages = get_history(image_id)
    return jsonify({"image_id": image_id, "count": len(messages), "messages": messages}), 200


@app.route('/chat/<image_id>/history', methods=['DELETE'])
@rate_limited("chat")
def delete_chat_history(image_id):
    """Clears the conversation for an image."""
    clear_history(get_image_or_404(image_id)["image_id"])
    return "", 204


# --- Streaming (Server-Sent Events) ---

@db_retry
def register_stream(image_id, prompt, history):
    """Records a new stream so it can be resumed, and drops expired ones.

    The history snapshot is kept so a resumed stream regenerates the exact
    same answer even if the conversation has moved on since.
    """
    now = time.time()
    session = {"id": f"chatcmpl-{uuid.uuid4().hex}", "created": int(now), "image_id": image_id,
               "prompt": prompt, "history": history, "expires_at": now + STREAM_SESSION_TTL}
    with db.session() as db_session:
        db_session.execute(delete(StreamSession).where(StreamSession.expires_at < now))
        db_session.add(StreamSession(**session))
    return session


def resume_stream(last_event_id, image_id):
    """Maps a Last-Event-ID ("<completion id>:<seq>") to (session, next seq)."""
    completion_id, _, seq = last_event_id.rpartition(":")
    if not completion_id or not (seq.isascii() and seq.isdigit()) or len(seq) > 9:
        raise APIError(400, "invalid_last_event_id",
                       "Last-Event-ID must look like '<completion id>:<sequence>'.")
    session = load_stream_session(completion_id)
    if session is None or session["image_id"] != image_id or session["expires_at"] < time.time():
        raise APIError(404, "stream_not_found", "That stream is unknown or has expired.")
    return session, int(seq) + 1


def once(func):
    """Wraps func so that only its first call does anything (thread-safe)."""
    lock = threading.Lock()
    called = []

    def wrapper():
        with lock:
            if called:
                return
            called.append(True)
        func()
    return wrapper


def sse_events(items, completion_id, start, on_finish=None):
    """Formats stream items as Server-Sent Events.

    Each event carries an id so a client that drops can reconnect with
    Last-Event-ID. Nothing is produced ahead of time: the WSGI server pulls
    each event from this generator. The dev server writes one to the socket
    before asking for the next; serve.py caps how many bytes may wait unsent
    and stops pulling above that. Either way a slow client slows generation
    instead of filling a buffer (backpressure).

    `on_finish` runs when the generator ends for any reason: completion, error,
    close(), or being discarded by a server that skipped close() after the
    client disconnected.
    """
    seq = start
    try:
        yield f"retry: {SSE_RETRY_MS}\n\n"
        for item in items:
            data = item if isinstance(item, str) else json.dumps(item, separators=(",", ":"))
            yield f"id: {completion_id}:{seq}\ndata: {data}\n\n"
            seq += 1
    except GeneratorExit:
        logger.info("Stream %s closed by client at event %d", completion_id, seq)
        raise
    except Exception:
        # Headers are already sent, so the failure is reported in-band.
        logger.exception("Stream %s failed at event %d", completion_id, seq)
        error = {"error": {"message": "Stream failed.", "type": "server_error",
                           "param": None, "code": "stream_error"}}
        yield f"data: {json.dumps(error)}\n\n"
    finally:
        if on_finish:
            on_finish()


@app.route('/chat-stream/<image_id>', methods=['POST'])
@rate_limited("chat")
def chat_about_image_stream(image_id):
    """Same as /chat, streamed as SSE. Send Last-Event-ID to resume a dropped stream."""
    image_id = get_image_or_404(image_id)["image_id"]

    # New stream, or continue an earlier one after the event the client last saw.
    last_event_id = request.headers.get("Last-Event-ID")
    if last_event_id:
        session, start = resume_stream(last_event_id, image_id)
    else:
        session, start = register_stream(image_id, parse_question(), get_history(image_id)), 0

    # Refuse rather than queue when this process is at its stream limit.
    slots = stream_slots
    if not slots.acquire(blocking=False):
        raise APIError(429, "too_many_streams",
                       "Too many concurrent streams. Retry shortly.", "rate_limit_error",
                       {"Retry-After": "1"})
    metrics.stream_opened()

    @once
    def release():
        slots.release()
        metrics.stream_closed()

    items = generate_mock_assistant_events(session["prompt"], image_id, session["id"],
                                           session["created"], start=start,
                                           history=session["history"])
    captured = generate_chunks_and_capture_history(session["prompt"], image_id, items)
    # A resume regenerates from the top (so the full answer can still be
    # captured) and skips the events the client already has.
    response = Response(sse_events(islice(captured, start, None), session["id"], start, release),
                        mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"  # stop reverse proxies from buffering
    # The slot is released from two places, whichever comes first: here when the
    # server closes the response (covers a generator that never started), and in
    # sse_events when the generator ends (covers a server that skips close()).
    response.call_on_close(release)
    logger.info("Stream %s on image %s from event %d", session["id"], image_id, start)
    return response


def generate_chunks_and_capture_history(prompt, image_id, stream_response):
    """Passes stream items through and stores the turn once the answer is complete.

    The turn is saved when the finish chunk arrives. A stream that is dropped
    earlier stores nothing, so history never holds a partial answer; the turn
    is stored when the client resumes and the stream completes.
    """
    parts = []
    for item in stream_response:
        if isinstance(item, dict) and item.get("choices"):
            choice = item["choices"][0]
            parts.append(choice["delta"].get("content") or "")
            if choice["finish_reason"] is not None:
                append_turn(image_id, item["id"], prompt, "".join(parts))
        yield item


if __name__ == '__main__':
    # Development server only; production runs serve.py. The debugger allows
    # code execution, so it is opt-in (FLASK_DEBUG=1) and never on by default.
    # The test helpers are for local use, so they are on here unless their variable says otherwise.
    for flag in ("ALLOW_RESET", "ALLOW_SHUTDOWN"):
        if flag not in os.environ:
            app.config[flag] = True
    # PORT changes the port without editing this file (default 5000).
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True,
            port=int(os.environ.get("PORT", "5000")))
