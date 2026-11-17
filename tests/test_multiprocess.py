"""End-to-end test with two real server processes.

Starts two instances of `serve.py` (waitress) as separate OS processes on
different ports, sharing one database and one upload folder, and talks to them
over real HTTP. Proves that state is shared between workers and that Server-
Sent Events are delivered incrementally by the production server.

With TEST_REDIS_URL set, the workers rate-limit through that Redis server.
"""
import contextlib
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import uuid

import pytest

from conftest import count_rows
from test_q1 import PNG, app_module
from test_q2 import parse_sse

pytest.importorskip("waitress")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "integration-test-token"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def post_json(port, path, payload, headers=None):
    return call(port, "POST", path, json.dumps(payload),
                {"Content-Type": "application/json", **(headers or {})})


def upload_raw(port, data=PNG, headers=None):
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="a.png"\r\n'
            f'Content-Type: image/png\r\n\r\n').encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return call(port, "POST", "/upload", body,
                {"Content-Type": f"multipart/form-data; boundary={boundary}", **(headers or {})})


def upload(port, headers=None):
    status, data = upload_raw(port, headers=headers)
    return status, json.loads(data)


def event_arrival_times(port, image_id):
    """Streams a chat and returns when each data event reached the client."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request("POST", f"/chat-stream/{image_id}", json.dumps({"question": "Slowly"}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
        arrivals = []
        while True:
            line = resp.readline()
            if not line:
                return arrivals
            if line.startswith(b"data:"):
                arrivals.append(time.monotonic())
    finally:
        conn.close()


@contextlib.contextmanager
def running_workers(shared, count=2, extra_env=None):
    """Runs `count` serve.py processes sharing the test database and one upload folder."""
    uploads = shared / "uploads"
    uploads.mkdir(exist_ok=True)
    env = dict(os.environ, UPLOAD_FOLDER=str(uploads), AUTO_MIGRATE="0", METRICS_TOKEN=TOKEN,
               UPLOAD_RATE_LIMIT="4/60", STREAM_CHUNK_DELAY="0.05", HOST="127.0.0.1")
    if os.environ.get("TEST_REDIS_URL"):
        env["REDIS_URL"] = os.environ["TEST_REDIS_URL"]
    env.update(extra_env or {})
    ports, processes, logs = [], [], []
    try:
        for index in range(count):
            port = free_port()
            log = open(shared / f"worker{index}.log", "w")
            logs.append(log)
            processes.append(subprocess.Popen(
                [sys.executable, "serve.py"], cwd=PROJECT_DIR,
                env=dict(env, PORT=str(port)), stdout=log, stderr=subprocess.STDOUT))
            ports.append(port)
        deadline = time.time() + 60
        for port, process in zip(ports, processes):
            while True:
                try:
                    if call(port, "GET", "/health")[0] == 200:
                        break
                except OSError:
                    pass
                if process.poll() is not None or time.time() > deadline:
                    for log in logs:
                        log.flush()
                    output = "\n".join(
                        (shared / f"worker{i}.log").read_text() for i in range(len(logs)))
                    pytest.fail(f"worker on port {port} did not start:\n{output}")
                time.sleep(0.2)
        yield {"ports": ports, "uploads": uploads, "processes": processes}
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait(timeout=10)
        for log in logs:
            log.close()


@pytest.fixture(scope="module")
def workers(tmp_path_factory):
    with running_workers(tmp_path_factory.mktemp("shared")) as running:
        yield {"a": running["ports"][0], "b": running["ports"][1], "uploads": running["uploads"],
               "pids": [process.pid for process in running["processes"]]}


def test_workers_are_separate_processes(workers):
    assert len({os.getpid(), *workers["pids"]}) == 3


def test_conversation_spans_workers(workers):
    a, b = workers["a"], workers["b"]
    status, image = upload(a)
    assert status == 201
    image_id = image["image_id"]
    assert os.path.isfile(workers["uploads"] / f"{image_id}.png")

    status, _ = post_json(b, f"/chat/{image_id}", {"question": "Asked on worker B"})
    assert status == 200                                  # B found A's upload
    status, data = post_json(a, f"/chat/{image_id}", {"question": "Asked on worker A"})
    answer = json.loads(data)["choices"][0]["message"]["content"]
    assert "Asked on worker B" in answer and "turn 2" in answer   # A saw B's turn at once

    for port in (a, b):
        messages = json.loads(call(port, "GET", f"/chat/{image_id}/history")[1])["messages"]
        assert [m["content"] for m in messages if m["role"] == "user"] == [
            "Asked on worker B", "Asked on worker A"]

    assert call(b, "DELETE", f"/chat/{image_id}/history")[0] == 204
    assert json.loads(call(a, "GET", f"/chat/{image_id}/history")[1])["count"] == 0


def test_stream_started_on_one_worker_resumes_on_the_other(workers):
    a, b = workers["a"], workers["b"]
    image_id = upload(a)[1]["image_id"]
    status, data = post_json(a, f"/chat-stream/{image_id}", {"question": "Stream me"})
    assert status == 200
    events = parse_sse(data.decode())
    assert events[-1][1] == "[DONE]"

    status, data = call(b, "POST", f"/chat-stream/{image_id}",
                        headers={"Last-Event-ID": events[2][0]})
    assert status == 200
    assert parse_sse(data.decode()) == events[3:]


def test_rate_limit_is_shared_between_workers(workers):
    a, b = workers["a"], workers["b"]
    assert [upload(port)[0] for port in (a, b, a, b)] == [201, 201, 201, 201]
    assert upload(a)[0] == 429                            # limit is 4 across both, not 4 each
    assert upload(b)[0] == 429
    if os.environ.get("TEST_REDIS_URL"):
        # Counted in Redis: the database saw no rate-limit writes at all.
        assert count_rows(app_module.RateLimitCounter) == 0


def test_events_arrive_incrementally_from_production_server(workers):
    image_id = upload(workers["a"])[1]["image_id"]
    arrivals = event_arrival_times(workers["a"], image_id)
    # ~14 events at 0.05 s apart. If the server buffered the whole body, they
    # would all arrive together.
    assert len(arrivals) >= 10
    assert arrivals[-1] - arrivals[0] > 0.3


def test_client_disconnect_frees_the_stream_on_a_real_server(workers):
    """A client that vanishes mid-stream must not leave the stream counted as open."""
    a = workers["a"]
    image_id = upload(a)[1]["image_id"]

    def open_streams():
        text = call(a, "GET", "/metrics", headers={"Authorization": f"Bearer {TOKEN}"})[1].decode()
        return int([l for l in text.splitlines() if l.startswith("active_streams")][0].split()[1])

    for _ in range(3):
        conn = http.client.HTTPConnection("127.0.0.1", a, timeout=20)
        conn.request("POST", f"/chat-stream/{image_id}", json.dumps({"question": "Bye early"}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        for _ in range(4):
            resp.readline()                        # read a little, then hang up
        resp.close()                               # closes the socket mid-stream
        conn.close()

    deadline = time.time() + 10
    while open_streams() and time.time() < deadline:
        time.sleep(0.2)
    assert open_streams() == 0


def test_instance_still_answers_with_many_open_connections(workers):
    """An instance must take more connections than it has streams and threads.

    150 clients hold a connection open (as idle keep-alive connections and open
    streams do). A new request must still be answered, not left waiting for one
    of them to go away.
    """
    a = workers["a"]
    idle = []
    try:
        for _ in range(150):
            idle.append(socket.create_connection(("127.0.0.1", a), timeout=5))
        time.sleep(0.5)                                  # let the server accept them all
        conn = http.client.HTTPConnection("127.0.0.1", a, timeout=5)
        try:
            conn.request("GET", "/health")
            assert conn.getresponse().status == 200
        finally:
            conn.close()
    finally:
        for sock in idle:
            sock.close()


def test_slow_client_pauses_generation_on_the_production_server(tmp_path):
    """Backpressure: a client that stops reading must stop the generator.

    The answer is far larger than the server may buffer, and the client reads
    nothing. The turn is stored when the generator reaches the finish chunk, so
    "not stored yet" proves the generator is paused, not run to the end into a
    buffer. Once the client reads, the stream completes and the turn is stored.
    """
    def turns(port, image_id):
        return json.loads(call(port, "GET", f"/chat/{image_id}/history")[1])["count"]

    fast = {"STREAM_CHUNK_DELAY": "0", "MOCK_CHAT_DELAY": "0", "MOCK_VISION_DELAY": "0"}
    with running_workers(tmp_path, count=1, extra_env=fast) as running:
        port = running["ports"][0]
        image_id = upload(port)[1]["image_id"]
        # 2000 one-letter words: one event per word. The answer quotes this
        # question and the previous one, so the stream is over 1 MB.
        question = "a " * 2000
        assert post_json(port, f"/chat/{image_id}", {"question": question})[0] == 200
        assert turns(port, image_id) == 2

        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)   # tiny window, set before connecting
        sock.settimeout(30)
        sock.connect(("127.0.0.1", port))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.sock = sock
        try:
            conn.request("POST", f"/chat-stream/{image_id}", json.dumps({"question": question}),
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()                    # reads the headers only
            assert resp.status == 200

            time.sleep(2)                                # unthrottled, generation takes far less
            assert turns(port, image_id) == 2            # still paused: finish chunk not reached

            body = resp.read()                           # now read everything
            assert len(body) > 1_000_000
            assert body.endswith(b"data: [DONE]\n\n")
        finally:
            conn.close()

        deadline = time.time() + 10
        while turns(port, image_id) != 4 and time.time() < deadline:
            time.sleep(0.1)
        assert turns(port, image_id) == 4


def test_reset_and_shutdown_are_disabled_on_the_production_server(workers):
    status, data = call(workers["a"], "POST", "/reset", headers={"X-Reset-Confirm": "yes"})
    assert status == 403 and json.loads(data)["error"]["code"] == "reset_disabled"
    status, data = call(workers["a"], "POST", "/shutdown", headers={"X-Shutdown-Confirm": "yes"})
    assert status == 403 and json.loads(data)["error"]["code"] == "shutdown_disabled"
    assert call(workers["a"], "GET", "/health")[0] == 200               # still running


def test_metrics_need_the_token_on_a_real_server(workers):
    a = workers["a"]
    assert call(a, "GET", "/metrics")[0] == 401
    status, data = call(a, "GET", "/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200 and b"http_requests_total" in data
