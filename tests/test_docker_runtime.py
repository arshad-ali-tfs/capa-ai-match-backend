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


@pytest.mark.parametrize("fail_build,image_exists", [(False, False), (True, False), (False, True)])
def test_launcher_preserves_mount_and_port_arguments_and_never_runs_a_failed_build(tmp_path, fail_build, image_exists):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell launcher is Windows-specific")
    configuration = tmp_path / "fictional docker.env"
    configuration.write_text("CAPA_ENV=local\n", encoding="utf-8")
    calls = tmp_path / "calls.jsonl"
    script = tmp_path / "launcher-test.ps1"
    script.write_text(r"""
function global:docker {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CommandArguments)
    @{ args = @($CommandArguments) } | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:DOCKER_TEST_CALLS
    $global:LASTEXITCODE = 0
    if ($CommandArguments[0] -eq 'info') { 'linux'; return }
    if ($CommandArguments[1] -eq 'inspect') { Write-Error 'Missing lookup must not use a failing inspection'; return }
    if ($CommandArguments[0] -eq 'image' -and $env:DOCKER_TEST_IMAGE_EXISTS -eq '1') {
        'sha256:fictional-image'
        return
    }
    if ($CommandArguments[0] -eq 'build') {
        Write-Error 'FICTIONAL build progress on stderr'
        if ($env:DOCKER_TEST_FAIL_BUILD -eq '1') { $global:LASTEXITCODE = 11 }
    }
}
& $env:DOCKER_TEST_LAUNCHER -EnvFile $env:DOCKER_TEST_ENV -DataDirectory $env:DOCKER_TEST_DATA -Port @HOST_PORT@
""".replace("@HOST_PORT@", str(PORT + 10000)), encoding="utf-8")
    env = dict(os.environ, DOCKER_TEST_CALLS=str(calls), DOCKER_TEST_FAIL_BUILD="1" if fail_build else "0",
               DOCKER_TEST_IMAGE_EXISTS="1" if image_exists else "0",
               DOCKER_TEST_LAUNCHER=str(ROOT / "Start-Docker.ps1"), DOCKER_TEST_ENV=str(configuration),
               DOCKER_TEST_DATA=str(tmp_path / "persistent data"))
    result = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            env=env, capture_output=True, timeout=30)
    recorded = [json.loads(line)["args"] for line in calls.read_text(encoding="utf-8-sig").splitlines()]
    if fail_build:
        assert result.returncode != 0
        assert not any(command[0] == "run" for command in recorded)
    else:
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        command = next(command for command in recorded if command[0] == "run")
        assert command[command.index("--env-file") + 1] == str(configuration)
        assert command[command.index("--publish") + 1] == "127.0.0.1:" + str(PORT + 10000) + ":" + str(PORT)
        assert command[command.index("--mount") + 1] == "type=bind,source=" + str(tmp_path / "persistent data") + ",target=/data"
        assert command[-1] == "capa-ai-" + SERVICE + ":local"
        assert command[command.index("--network") + 1] == "capa-ai"
        assert any(command[0] == "build" for command in recorded) != image_exists
