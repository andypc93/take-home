"""deploy/nginx.conf under a real nginx in front of two real app instances.

Runs only where an nginx binary is installed (Linux/macOS). The shipped config
is used as is, except that the listen port and the two upstream ports are
swapped for free ports.
"""
import json
import os
import shutil
import subprocess
import time

import pytest

from test_multiprocess import (PROJECT_DIR, TOKEN, call, event_arrival_times, free_port,
                               post_json, running_workers, upload, upload_raw)
from test_q1 import PNG
from test_q2 import parse_sse

pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("nginx"),
                                reason="nginx binary not available")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    shared = tmp_path_factory.mktemp("nginx")
    with running_workers(shared, extra_env={"TRUSTED_PROXIES": "1"}) as running:
        listen = free_port()
        with open(os.path.join(PROJECT_DIR, "deploy", "nginx.conf")) as handle:
            site = handle.read()
        swaps = {"listen 80;": f"listen 127.0.0.1:{listen};",
                 "127.0.0.1:8001": f"127.0.0.1:{running['ports'][0]}",
                 "127.0.0.1:8002": f"127.0.0.1:{running['ports'][1]}"}
        for old, new in swaps.items():
            assert old in site, f"deploy/nginx.conf no longer contains '{old}'"
            site = site.replace(old, new)
        (shared / "site.conf").write_text(site)
        temp_paths = "".join(f"{kind}_temp_path {shared}/{kind};\n" for kind in
                             ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
        main = shared / "nginx.conf"
        main.write_text(f"pid {shared}/nginx.pid;\ndaemon off;\nworker_processes 1;\nevents {{}}\n"
                        f"http {{\naccess_log {shared}/access.log;\n{temp_paths}"
                        f"include {shared}/site.conf;\n}}\n")
        command = ["nginx", "-c", str(main), "-e", str(shared / "error.log"), "-p", str(shared)]
        check = subprocess.run(command + ["-t"], capture_output=True, text=True)
        assert check.returncode == 0, check.stderr
        nginx = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 15
            while True:
                try:
                    if call(listen, "GET", "/health")[0] == 200:
                        break
                except OSError:
                    pass
                assert time.time() < deadline and nginx.poll() is None, \
                    (shared / "error.log").read_text()
                time.sleep(0.1)
            yield {"port": listen, "workers": running, "syntax_check": check.stderr}
        finally:
            nginx.terminate()
            nginx.wait(timeout=10)


def worker_chat_counts(stack):
    counts = []
    for port in stack["workers"]["ports"]:
        text = call(port, "GET", "/metrics", headers={"Authorization": f"Bearer {TOKEN}"})[1].decode()
        line = [l for l in text.splitlines()
                if 'route="/chat/<image_id>",status="200"' in l and 'method="POST"' in l]
        counts.append(int(line[0].rsplit(" ", 1)[1]) if line else 0)
    return counts


def test_config_passes_nginx_syntax_check(stack):
    assert "syntax is ok" in stack["syntax_check"] and "test is successful" in stack["syntax_check"]


def test_requests_flow_through_proxy_and_are_balanced(stack):
    port = stack["port"]
    status, image = upload(port)
    assert status == 201
    for i in range(10):
        assert post_json(port, f"/chat/{image['image_id']}", {"question": f"q{i}"})[0] == 200
    counts = worker_chat_counts(stack)
    assert sum(counts) == 10 and min(counts) >= 3        # both instances took a share
    history = json.loads(call(port, "GET", f"/chat/{image['image_id']}/history")[1])
    assert history["count"] == 20


def test_sse_is_not_buffered_by_proxy(stack):
    image_id = upload(stack["port"])[1]["image_id"]
    arrivals = event_arrival_times(stack["port"], image_id)
    assert len(arrivals) >= 10 and arrivals[-1] - arrivals[0] > 0.3


def test_stream_resume_header_passes_through_proxy(stack):
    port = stack["port"]
    image_id = upload(port)[1]["image_id"]
    events = parse_sse(post_json(port, f"/chat-stream/{image_id}", {"question": "x"})[1].decode())
    status, data = call(port, "POST", f"/chat-stream/{image_id}",
                        headers={"Last-Event-ID": events[1][0]})
    assert status == 200 and parse_sse(data.decode()) == events[2:]


def test_client_cannot_forge_its_address_through_proxy(stack):
    port = stack["port"]
    statuses = [upload(port, headers={"X-Forwarded-For": f"10.9.8.{i}"})[0] for i in range(6)]
    assert statuses == [201, 201, 201, 201, 429, 429]    # one real client, one budget of 4


def test_metrics_blocked_and_health_allowed_at_proxy(stack):
    port = stack["port"]
    assert call(port, "GET", "/metrics", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 403
    assert call(port, "GET", "/health")[0] == 200


def test_oversized_upload_rejected_by_proxy(stack):
    status, body = upload_raw(stack["port"], data=PNG + b"\x00" * (17 * 1024 * 1024))
    assert status == 413 and b"nginx" in body            # nginx answered, not the app


def test_proxy_fails_over_when_an_instance_dies(stack):
    port = stack["port"]
    image_id = upload(port)[1]["image_id"]
    victim = stack["workers"]["processes"][0]
    victim.terminate()
    victim.wait(timeout=10)
    statuses = [post_json(port, f"/chat/{image_id}", {"question": f"q{i}"})[0] for i in range(6)]
    assert statuses == [200] * 6
