"""Offline probes and launcher behavior; these tests do not claim a Docker build."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "match"
PORT = 8102


@pytest.fixture
def server():
    state = {"status": 200, "body": b'{"status":"alive"}', "calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["calls"] += 1
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield http, state
    http.shutdown()
    http.server_close()
    thread.join()


def probe(port, *args):
    env = dict(os.environ)
    env[SERVICE.upper() + "_PORT"] = str(port)
    env["HTTP_PROXY"] = env["http_proxy"] = "http://127.0.0.1:1"
    return subprocess.run(
        [sys.executable, str(ROOT / "docker/healthcheck.py"), *args],
        env=env, capture_output=True, timeout=10,
    )


@pytest.mark.parametrize("status,body,expected", [
    (200, b'{"status":"alive"}', 0),
    (503, b'{"detail":"Unavailable"}', 1),
    (200, b'["invalid response"]', 1),
    (200, b"<html>invalid response</html>", 1),
])
def test_health_probe_uses_configured_port_and_rejects_unavailable_or_invalid_responses(server, status, body, expected):
    http, state = server
    state.update(status=status, body=body)
    assert probe(http.server_port).returncode == expected
    assert state["calls"] == 1


def test_probe_checks_local_readiness_and_rejects_external_targets(server):
    http, state = server
    assert probe(http.server_port, "--path", "/api/v1/" + SERVICE + "/ready").returncode == 0
    assert probe(http.server_port, "--path", "//example.com").returncode != 0
    assert probe(http.server_port, "--timeout", "0").returncode != 0
    assert state["calls"] == 1
