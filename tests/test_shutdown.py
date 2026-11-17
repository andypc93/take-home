"""POST /shutdown: the test helper that stops the server, and the guards around it."""
import os
import subprocess
import sys
import time

import pytest

from test_multiprocess import PROJECT_DIR, call, free_port
from test_q1 import app_module, assert_error

CONFIRM = {"X-Shutdown-Confirm": "yes"}


@pytest.fixture
def client(monkeypatch):
    """Shutdown switched on, with the real process exit replaced by a recorder."""
    stops = []
    monkeypatch.setitem(app_module.app.config, "ALLOW_SHUTDOWN", True)
    monkeypatch.setattr(app_module, "stop_process", lambda: stops.append(True))
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        c.stops = stops
        yield c


def test_shutdown_stops_the_process_once(client):
    resp = client.post("/shutdown", headers=CONFIRM)
    assert resp.status_code == 200 and resp.get_json() == {"stopping": True}
    assert client.stops == [True]


def test_shutdown_is_off_unless_switched_on(client, monkeypatch):
    monkeypatch.setitem(app_module.app.config, "ALLOW_SHUTDOWN", False)
    assert_error(client.post("/shutdown", headers=CONFIRM), 403, "shutdown_disabled")
    assert client.stops == []


def test_shutdown_is_refused_from_another_machine(client):
    resp = client.post("/shutdown", headers=CONFIRM, environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert_error(resp, 403, "forbidden")
    assert client.stops == []


def test_shutdown_needs_the_confirmation_header(client):
    assert_error(client.post("/shutdown"), 400, "confirmation_required")
    assert_error(client.post("/shutdown", headers={"X-Reset-Confirm": "yes"}), 400, "confirmation_required")
    assert client.stops == []


def test_shutdown_is_post_only_and_off_by_default(client):
    assert_error(client.get("/shutdown"), 405)
    assert client.stops == []


def test_shutdown_is_off_when_the_app_is_only_imported():
    assert app_module.app.config["ALLOW_SHUTDOWN"] is False


def test_real_dev_server_exits_cleanly(tmp_path):
    """Starts `python app.py` for real, asks it to stop, and waits for the process to end."""
    port = free_port()
    env = dict(os.environ, PORT=str(port), AUTO_MIGRATE="0", UPLOAD_FOLDER=str(tmp_path))
    env.pop("ALLOW_SHUTDOWN", None)                 # rely on the dev-server default
    with open(tmp_path / "server.log", "w") as log:
        server = subprocess.Popen([sys.executable, "app.py"], cwd=PROJECT_DIR, env=env,
                                  stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 30
            while True:
                try:
                    if call(port, "GET", "/health")[0] == 200:
                        break
                except OSError:
                    pass
                assert server.poll() is None and time.time() < deadline, \
                    (tmp_path / "server.log").read_text()
                time.sleep(0.2)

            assert call(port, "POST", "/shutdown")[0] == 400            # no header: still running
            assert call(port, "GET", "/health")[0] == 200
            status, _ = call(port, "POST", "/shutdown", headers=CONFIRM)
            assert status == 200                                        # the answer arrives before it stops
            assert server.wait(timeout=10) == 0
            with pytest.raises(OSError):
                call(port, "GET", "/health")                            # nothing listens any more
        finally:
            if server.poll() is None:
                server.kill()
