"""Append-only JSONL audit log. Every call through the proxy lands here."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Cap per-stream output kept in the audit log so a runaway command can't
# blow up the log file; the HTTP response has its own (larger) cap.
_MAX_LOGGED_OUTPUT = 4096


@dataclass
class AuditEntry:
    remote_addr: str
    method: str
    path: str
    status_code: int = 0
    workspace: str | None = None
    command: str | None = None
    exit_code: int | None = None
    duration_secs: float | None = None
    truncated: bool | None = None
    stdout: str | None = None
    stderr: str | None = None
    error: str | None = None


class AuditLog:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: AuditEntry) -> None:
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "remote_addr": entry.remote_addr,
            "method": entry.method,
            "path": entry.path,
            "status_code": entry.status_code,
        }
        for field_name in ("workspace", "command", "exit_code", "duration_secs", "truncated", "error"):
            value = getattr(entry, field_name)
            if value is not None:
                record[field_name] = round(value, 3) if field_name == "duration_secs" else value
        if entry.stdout is not None:
            record["stdout"] = entry.stdout[:_MAX_LOGGED_OUTPUT]
        if entry.stderr is not None:
            record["stderr"] = entry.stderr[:_MAX_LOGGED_OUTPUT]

        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
