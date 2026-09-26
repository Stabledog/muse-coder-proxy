"""HTTP-level tests: auth, IP allowlist, workspace allowlist, exec, audit log.

Runs a real server on 127.0.0.1 (an ephemeral port) against the real `coder`
CLI and test-junkyard workspace — no mocking of coder_exec, since the whole
point of this proxy is narrow, faithfully-audited pass-through.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as config_mod
import proxy

BEARER_TOKEN = "test-bearer-token-please-ignore"
WORKSPACE = "test-junkyard"


def _write_config(tmp_path: Path, *, trusted_cidr: str, allowlist=(WORKSPACE,)) -> Path:
    path = tmp_path / "config.toml"
    lines = [
        'listen_addr = "127.0.0.1"',
        "port = 0",
        f'coder_url = "{os.environ["CODER_URL"]}"',
        f"workspace_allowlist = {list(allowlist)!r}".replace("'", '"'),
        f'trusted_cidr = "{trusted_cidr}"',
        f'audit_log_path = "{(tmp_path / "audit.log").as_posix()}"',
        "default_timeout_secs = 300",
        "max_timeout_secs = 600",
        "max_output_bytes = 65536",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _start_server(tmp_path: Path, *, trusted_cidr: str = "127.0.0.0/8", allowlist=(WORKSPACE,)):
    os.environ["MUSE_PROXY_BEARER_TOKEN"] = BEARER_TOKEN
    os.environ["MUSE_CODER_TOKEN"] = os.environ["CODER_SESSION_TOKEN"]
    cfg = config_mod.load(_write_config(tmp_path, trusted_cidr=trusted_cidr, allowlist=allowlist))
    server = proxy.make_server(cfg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    return server, base_url, cfg.audit_log_path


@pytest.fixture()
def server(tmp_path):
    srv, base_url, audit_log_path = _start_server(tmp_path)
    yield base_url, audit_log_path
    srv.shutdown()


def _request(base_url, method, path, token=BEARER_TOKEN, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base_url + path, data=data, method=method)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_health_requires_token(server):
    base_url, _ = server
    status, body = _request(base_url, "GET", "/health", token=None)
    assert status == 401


def test_health_rejects_wrong_token(server):
    base_url, _ = server
    status, body = _request(base_url, "GET", "/health", token="not-the-token")
    assert status == 401


def test_health_ok(server):
    base_url, _ = server
    status, body = _request(base_url, "GET", "/health")
    assert status == 200
    assert body["status"] == "ok"


def test_ip_outside_trusted_range_rejected(tmp_path):
    srv, base_url, _ = _start_server(tmp_path, trusted_cidr="10.0.0.0/8")
    try:
        status, body = _request(base_url, "GET", "/health")
        assert status == 403
    finally:
        srv.shutdown()


def test_workspaces_filtered_to_allowlist(server):
    base_url, _ = server
    status, body = _request(base_url, "GET", "/workspaces")
    assert status == 200
    names = {w["name"] for w in body["workspaces"]}
    assert names == {WORKSPACE}


def test_exec_allowlisted_workspace(server):
    base_url, _ = server
    status, body = _request(base_url, "POST", "/exec", body={"workspace": WORKSPACE, "command": "hostname"})
    assert status == 200
    assert body["exit_code"] == 0
    assert WORKSPACE in body["stdout"]


def test_exec_non_allowlisted_workspace_forbidden(server):
    base_url, _ = server
    status, body = _request(base_url, "POST", "/exec", body={"workspace": "debweb-maint", "command": "hostname"})
    assert status == 403


def test_exec_missing_fields(server):
    base_url, _ = server
    status, body = _request(base_url, "POST", "/exec", body={"workspace": WORKSPACE})
    assert status == 400


def test_exec_timeout(server):
    base_url, _ = server
    start = time.monotonic()
    status, body = _request(
        base_url, "POST", "/exec",
        body={"workspace": WORKSPACE, "command": "sleep 30", "timeout_secs": 2},
    )
    elapsed = time.monotonic() - start
    assert status == 408
    assert elapsed < 15


def test_audit_log_records_calls(server):
    base_url, audit_log_path = server
    _request(base_url, "GET", "/health")
    _request(base_url, "POST", "/exec", body={"workspace": WORKSPACE, "command": "echo hi"})
    lines = audit_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2
    records = [json.loads(l) for l in lines]
    assert any(r["path"] == "/health" and r["status_code"] == 200 for r in records)
    exec_records = [r for r in records if r["path"] == "/exec"]
    assert exec_records
    assert exec_records[-1]["workspace"] == WORKSPACE
    assert exec_records[-1]["exit_code"] == 0
