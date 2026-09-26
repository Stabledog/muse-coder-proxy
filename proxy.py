"""Coder proxy: a narrow, auditable HTTP API for Bert to run commands in
allowlisted Coder workspaces on Stablebeast. See DESIGN.md."""
from __future__ import annotations

import hmac
import ipaddress
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import audit
import coder_exec
import config as config_mod

VERSION = "0.1.0"


class Handler(BaseHTTPRequestHandler):
    server_version = f"muse-coder-proxy/{VERSION}"

    # Set once by make_server(); shared across all request instances.
    cfg: config_mod.Config
    audit_log: audit.AuditLog

    def log_message(self, fmt, *args):
        pass  # the audit log is the record of truth, not stderr access logs

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _client_ip_allowed(self) -> bool:
        try:
            addr = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return addr in self.cfg.trusted_cidr

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        token = header[len("Bearer "):]
        return hmac.compare_digest(token, self.cfg.bearer_token)

    def _guard(self, entry: audit.AuditEntry) -> bool:
        """Auth/IP checks common to every route. Sends the response and
        returns False if the request must not proceed."""
        if not self._client_ip_allowed():
            entry.status_code = 403
            entry.error = "source ip outside trusted range"
            self.audit_log.record(entry)
            self._send_json(403, {"error": "forbidden"})
            return False
        if not self._authorized():
            entry.status_code = 401
            entry.error = "missing or invalid bearer token"
            self.audit_log.record(entry)
            self._send_json(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        entry = audit.AuditEntry(remote_addr=self.client_address[0], method="GET", path=self.path)
        if not self._guard(entry):
            return
        if self.path == "/health":
            entry.status_code = 200
            self.audit_log.record(entry)
            self._send_json(200, {"status": "ok", "version": VERSION})
        elif self.path == "/workspaces":
            self._handle_workspaces(entry)
        else:
            entry.status_code = 404
            self.audit_log.record(entry)
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        entry = audit.AuditEntry(remote_addr=self.client_address[0], method="POST", path=self.path)
        if not self._guard(entry):
            return
        if self.path == "/exec":
            self._handle_exec(entry)
        else:
            entry.status_code = 404
            self.audit_log.record(entry)
            self._send_json(404, {"error": "not found"})

    def _handle_workspaces(self, entry: audit.AuditEntry) -> None:
        try:
            workspaces = coder_exec.list_workspaces(self.cfg.coder_url, self.cfg.coder_token)
        except coder_exec.CoderError as e:
            entry.status_code = 502
            entry.error = str(e)
            self.audit_log.record(entry)
            self._send_json(502, {"error": "coder CLI failed", "detail": str(e)})
            return
        visible = [w for w in workspaces if w["name"] in self.cfg.workspace_allowlist]
        entry.status_code = 200
        self.audit_log.record(entry)
        self._send_json(200, {"workspaces": visible})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw)

    def _handle_exec(self, entry: audit.AuditEntry) -> None:
        try:
            body = self._read_json_body()
        except (json.JSONDecodeError, ValueError):
            entry.status_code = 400
            entry.error = "invalid JSON body"
            self.audit_log.record(entry)
            self._send_json(400, {"error": "invalid JSON body"})
            return

        workspace = body.get("workspace")
        command = body.get("command")
        timeout_secs = body.get("timeout_secs", self.cfg.default_timeout_secs)
        entry.workspace = workspace
        entry.command = command

        if not workspace or not isinstance(workspace, str) or not command or not isinstance(command, str):
            entry.status_code = 400
            entry.error = "workspace and command are required strings"
            self.audit_log.record(entry)
            self._send_json(400, {"error": "workspace and command are required strings"})
            return

        if not isinstance(timeout_secs, int) or isinstance(timeout_secs, bool) or timeout_secs <= 0:
            entry.status_code = 400
            entry.error = "timeout_secs must be a positive integer"
            self.audit_log.record(entry)
            self._send_json(400, {"error": "timeout_secs must be a positive integer"})
            return
        timeout_secs = min(timeout_secs, self.cfg.max_timeout_secs)

        if workspace not in self.cfg.workspace_allowlist:
            entry.status_code = 403
            entry.error = "workspace not on allowlist"
            self.audit_log.record(entry)
            self._send_json(403, {"error": "workspace not allowed"})
            return

        try:
            workspaces = coder_exec.list_workspaces(self.cfg.coder_url, self.cfg.coder_token)
        except coder_exec.CoderError as e:
            entry.status_code = 502
            entry.error = str(e)
            self.audit_log.record(entry)
            self._send_json(502, {"error": "coder CLI failed", "detail": str(e)})
            return

        match = next((w for w in workspaces if w["name"] == workspace), None)
        if match is None:
            entry.status_code = 404
            entry.error = "workspace not found"
            self.audit_log.record(entry)
            self._send_json(404, {"error": "workspace not found"})
            return
        if match["status"] != "running":
            entry.status_code = 409
            entry.error = f"workspace status is {match['status']}"
            self.audit_log.record(entry)
            self._send_json(409, {"error": "workspace not running", "status": match["status"]})
            return

        result = coder_exec.exec_command(
            self.cfg.coder_url,
            self.cfg.coder_token,
            workspace,
            command,
            timeout_secs,
            self.cfg.max_output_bytes,
        )

        entry.exit_code = result.exit_code
        entry.duration_secs = result.duration_secs
        entry.truncated = result.truncated
        entry.stdout = result.stdout
        entry.stderr = result.stderr

        if result.cli_error is not None:
            entry.status_code = 502
            entry.error = result.cli_error
            self.audit_log.record(entry)
            self._send_json(502, {
                "error": "coder ssh failed",
                "detail": result.cli_error,
                "duration_secs": round(result.duration_secs, 3),
            })
            return

        if result.timed_out:
            entry.status_code = 408
            self.audit_log.record(entry)
            self._send_json(408, {
                "error": "timeout",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "truncated": result.truncated,
                "duration_secs": round(result.duration_secs, 3),
            })
            return

        entry.status_code = 200
        self.audit_log.record(entry)
        self._send_json(200, {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.truncated,
            "duration_secs": round(result.duration_secs, 3),
        })


def make_server(cfg: config_mod.Config) -> ThreadingHTTPServer:
    Handler.cfg = cfg
    Handler.audit_log = audit.AuditLog(cfg.audit_log_path)
    return ThreadingHTTPServer((cfg.listen_addr, cfg.port), Handler)


def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "config.toml"
    cfg = config_mod.load(config_path)
    server = make_server(cfg)
    print(f"muse-coder-proxy {VERSION} listening on {cfg.listen_addr}:{cfg.port}")
    print(f"workspace allowlist: {sorted(cfg.workspace_allowlist)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
