"""Small load test for a running server. Standard library only.

    python loadtest.py --url http://127.0.0.1:5000 --scenario chat --requests 2000 --concurrency 32

Scenarios: chat, upload, stream. Each prints requests per second, latency
percentiles and the responses by status and error code. For streams the
latency is the whole stream; time to the first event is reported as well.

The default rate limits (10 uploads, 60 chats per minute per client) are far
below what a load test sends, so either raise them on the server
(UPLOAD_RATE_LIMIT, CHAT_RATE_LIMIT) or expect 429 responses, which is how to
show the limits working. Options:

    --key KEY            API key, if the server has API_KEYS set
    --images N           images uploaded first and shared by chat/stream requests
    --drop PERCENT       streams: hang up after 3 events on this share of requests
    --metrics-url URL    after the run, wait for active_streams to return to 0 there
                         (repeatable; give each instance's own /metrics address)
    --metrics-token T    bearer token for --metrics-url
    --json FILE          also write the results as JSON
"""
import argparse
import http.client
import json
import struct
import threading
import time
import uuid
from urllib.parse import urlsplit

# Smallest well-formed PNG header the server accepts (4x3 pixels).
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 4, 3)
       + b"\x08\x02\x00\x00\x00" + b"\x00" * 40)


class Client:
    """One keep-alive connection; reconnects after an error or a dropped stream."""

    def __init__(self, url, key=None, timeout=60):
        parts = urlsplit(url)
        self.host, self.port = parts.hostname, parts.port or 80
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.conn = None

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def open(self, method, path, body=None, headers=None):
        if self.conn is None:
            self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        self.conn.request(method, path, body=body, headers={**self.headers, **(headers or {})})
        return self.conn.getresponse()

    def upload(self):
        boundary = uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="a.png"\r\n'
                f'Content-Type: image/png\r\n\r\n').encode() + PNG + f"\r\n--{boundary}--\r\n".encode()
        resp = self.open("POST", "/upload", body,
                         {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return resp, resp.read()

    def chat(self, image_id):
        resp = self.open("POST", f"/chat/{image_id}", json.dumps({"question": "What is in this image?"}),
                         {"Content-Type": "application/json"})
        return resp, resp.read()


def outcome(resp, data):
    """'200', or for errors the status plus the API's error code, e.g. '429 rate_limit_exceeded'."""
    if resp.status < 400:
        return str(resp.status)
    try:
        return f"{resp.status} {json.loads(data)['error']['code']}"
    except (ValueError, KeyError, TypeError):
        return str(resp.status)


def run_upload(client, state):
    resp, data = client.upload()
    return outcome(resp, data), None, resp


def run_chat(client, state):
    resp, data = client.chat(state.next_image())
    return outcome(resp, data), None, resp


def run_stream(client, state):
    """Reads a stream to [DONE]; returns the time to the first event as the extra figure."""
    started = time.perf_counter()
    resp = client.open("POST", f"/chat-stream/{state.next_image()}",
                       json.dumps({"question": "Describe it"}), {"Content-Type": "application/json"})
    if resp.status != 200:
        return outcome(resp, resp.read()), None, resp
    drop = state.should_drop()
    first, events, done = None, 0, False
    while True:
        line = resp.readline()
        if not line:
            break
        if line.startswith(b"data:"):
            events += 1
            if first is None:
                first = time.perf_counter() - started
            if line.strip() == b"data: [DONE]":
                done = True
            if drop and events >= 3:
                client.close()                      # hang up mid-stream
                return "200 dropped by client", first, resp
    if not done:
        client.close()
        return "200 incomplete", first, resp
    resp.read()                                     # the terminating chunk, so the connection is reusable
    return "200", first, resp


SCENARIOS = {"chat": run_chat, "upload": run_upload, "stream": run_stream}


class State:
    def __init__(self, args, images):
        self.images = images
        self.lock = threading.Lock()
        self.remaining = args.requests
        self.sent = 0
        self.drop = args.drop

    def take(self):
        with self.lock:
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            self.sent += 1
            return True

    def next_image(self):
        with self.lock:
            return self.images[self.sent % len(self.images)]

    def should_drop(self):
        with self.lock:
            return self.sent % 100 < self.drop


def percentile(sorted_values, percent):
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, round(percent / 100 * len(sorted_values) + 0.5) - 1))
    return sorted_values[index]


def worker(args, state, results, missing_retry_after):
    client = Client(args.url, args.key)
    scenario = SCENARIOS[args.scenario]
    while state.take():
        started = time.perf_counter()
        try:
            result, extra, resp = scenario(client, state)
            if resp.status == 429 and not resp.getheader("Retry-After"):
                missing_retry_after.append(result)
        except (OSError, http.client.HTTPException) as err:
            client.close()
            result, extra = f"connection error: {type(err).__name__}", None
        results.append((result, time.perf_counter() - started, extra))
    client.close()


def open_streams(url, token):
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=10)
    try:
        conn.request("GET", parts.path or "/metrics",
                     headers={"Authorization": f"Bearer {token}"} if token else {})
        text = conn.getresponse().read().decode()
    finally:
        conn.close()
    return int([line for line in text.splitlines() if line.startswith("active_streams")][0].split()[1])


def main():
    parser = argparse.ArgumentParser(description="Load test for the Visual Assistant API.")
    parser.add_argument("--url", default="http://127.0.0.1:5000")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="chat")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--images", type=int, default=8)
    parser.add_argument("--drop", type=int, default=0)
    parser.add_argument("--key")
    parser.add_argument("--metrics-url", action="append", default=[])
    parser.add_argument("--metrics-token")
    parser.add_argument("--json")
    args = parser.parse_args()

    images = []
    if args.scenario != "upload":
        setup = Client(args.url, args.key)
        for _ in range(args.images):
            resp, data = setup.upload()
            if resp.status != 201:
                raise SystemExit(f"Setup upload failed: HTTP {outcome(resp, data)}. "
                                 "Raise UPLOAD_RATE_LIMIT on the server or lower --images.")
            images.append(json.loads(data)["image_id"])
        setup.close()

    state, results, missing_retry_after = State(args, images), [], []
    threads = [threading.Thread(target=worker, args=(args, state, results, missing_retry_after))
               for _ in range(args.concurrency)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    by_outcome = {}
    for result, _, _ in results:
        by_outcome[result] = by_outcome.get(result, 0) + 1
    good = sorted(seconds for result, seconds, _ in results if result == "200" or result == "201")
    first_events = sorted(extra for result, _, extra in results if result == "200" and extra is not None)
    failed = sum(count for result, count in by_outcome.items()
                 if result[:1] == "5" or result.startswith("connection") or result == "200 incomplete")
    summary = {
        "scenario": args.scenario, "url": args.url, "requests": len(results),
        "concurrency": args.concurrency, "seconds": round(elapsed, 2),
        "requests_per_second": round(len(results) / elapsed, 1),
        "successful": len(good),
        "successful_per_second": round(len(good) / elapsed, 1),
        "latency_ms": {name: (round(percentile(good, p) * 1000, 1) if good else None)
                       for name, p in (("p50", 50), ("p95", 95), ("p99", 99), ("max", 100))},
        "outcomes": dict(sorted(by_outcome.items())),
        "server_errors_or_broken": failed,
        "error_rate_percent": round(100 * failed / max(1, len(results)), 2),
        "responses_429_without_retry_after": len(missing_retry_after),
    }
    if first_events:
        summary["first_event_ms"] = {name: round(percentile(first_events, p) * 1000, 1)
                                     for name, p in (("p50", 50), ("p95", 95), ("p99", 99))}
    if args.metrics_url:
        deadline = time.time() + 15
        while True:
            counts = [open_streams(url, args.metrics_token) for url in args.metrics_url]
            if not any(counts) or time.time() > deadline:
                break
            time.sleep(0.5)
        summary["open_streams_after_run"] = counts

    print(f"{args.scenario}: {summary['requests']} requests, {args.concurrency} at a time, "
          f"{summary['seconds']} s")
    print(f"  throughput   {summary['requests_per_second']} requests/s "
          f"({summary['successful_per_second']} successful/s)")
    latency = summary["latency_ms"]
    print(f"  latency ms   p50 {latency['p50']}  p95 {latency['p95']}  p99 {latency['p99']}  "
          f"max {latency['max']}   (successful requests)")
    if first_events:
        first = summary["first_event_ms"]
        print(f"  first event  p50 {first['p50']}  p95 {first['p95']}  p99 {first['p99']} ms")
    for result, count in summary["outcomes"].items():
        print(f"  {count:>7}  {result}")
    print(f"  server errors or broken responses: {failed} ({summary['error_rate_percent']} %)")
    print(f"  429 responses without Retry-After: {len(missing_retry_after)}")
    if args.metrics_url:
        print(f"  open streams after the run, per instance: {summary['open_streams_after_run']}")
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(summary, handle, indent=2)


if __name__ == "__main__":
    main()
