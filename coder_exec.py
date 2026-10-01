"""Subprocess wrapper around the `coder` CLI: list + one-shot exec.

Windows has no POSIX process groups, so timeout handling uses
CREATE_NEW_PROCESS_GROUP + `taskkill /T /F` to kill the whole process tree
rather than relying on Popen.terminate(), which only signals the direct
child.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

_CREATE_NEW_PROCESS_GROUP = 0x00000200

# `coder ssh` always allocates a PTY for the remote command, so remote
# stdout+stderr arrive merged on our local stdout, and a nonzero remote exit
# code is reported only as CLI-diagnostic text on local stderr, not as our
# local exit code (which is always 1 on any remote failure). Recover it here.
_EXIT_STATUS_RE = re.compile(r"Process exited with status (\d+)")


class CoderError(Exception):
    """Raised when the `coder` CLI itself fails (not a normal command exit)."""


@dataclass
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    duration_secs: float
    timed_out: bool
    cli_error: str | None = None
    """Set when `coder ssh` failed in a way that isn't a normal nonzero exit
    of the remote command (workspace unreachable, connection dropped, ...).
    When set, exit_code is None and the caller should not treat this as a
    completed exec."""


def _find_coder() -> str:
    exe = shutil.which("coder")
    if not exe:
        raise CoderError("`coder` CLI not found on PATH")
    return exe


def _env(coder_url: str, coder_token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["CODER_URL"] = coder_url
    env["CODER_SESSION_TOKEN"] = coder_token
    return env


def list_workspaces(coder_url: str, coder_token: str) -> list[dict]:
    """Return [{"name", "status", "template"}] for every workspace visible
    to this token, regardless of allowlist — callers filter."""
    coder = _find_coder()
    proc = subprocess.run(
        [coder, "list", "--output", "json"],
        env=_env(coder_url, coder_token),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if proc.returncode != 0:
        raise CoderError(proc.stderr.strip() or "coder list failed")
    data = json.loads(proc.stdout)
    result = []
    for w in data:
        latest_build = w.get("latest_build", {})
        result.append({
            "name": latest_build.get("workspace_name"),
            "status": latest_build.get("status"),
            "template": w.get("template_name"),
        })
    return result


def _drain(pipe, cap: int, buf: bytearray, truncated: list[bool], lock: threading.Lock) -> None:
    """Read a pipe to EOF, keeping at most `cap` bytes. Must keep reading even
    past the cap so the child's write() calls don't block on a full pipe."""
    while True:
        chunk = pipe.read(4096)
        if not chunk:
            break
        with lock:
            remaining = cap - len(buf)
            if remaining <= 0:
                truncated[0] = True
                continue
            if len(chunk) > remaining:
                buf.extend(chunk[:remaining])
                truncated[0] = True
            else:
                buf.extend(chunk)
    pipe.close()


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def exec_command(
    coder_url: str,
    coder_token: str,
    workspace: str,
    command: str,
    timeout_secs: int,
    max_output_bytes: int,
) -> ExecResult:
    coder = _find_coder()
    args = [coder, "ssh", workspace, "--", command]
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NEW_PROCESS_GROUP

    start = time.monotonic()
    # NB: stdin must be DEVNULL, not inherited. `coder ssh` keeps the session
    # open reading stdin after the remote command finishes; when the proxy
    # runs headless (Startup folder, no console) the inherited stdin never
    # yields, so the child never exits and exec hangs until the timeout.
    proc = subprocess.Popen(
        args,
        env=_env(coder_url, coder_token),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )

    out_buf = bytearray()
    err_buf = bytearray()
    truncated = [False]
    lock = threading.Lock()
    out_thread = threading.Thread(target=_drain, args=(proc.stdout, max_output_bytes, out_buf, truncated, lock))
    err_thread = threading.Thread(target=_drain, args=(proc.stderr, max_output_bytes, err_buf, truncated, lock))
    out_thread.start()
    err_thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        proc.wait()

    out_thread.join()
    err_thread.join()
    duration = time.monotonic() - start

    stdout = out_buf.decode("utf-8", errors="replace")
    stderr = err_buf.decode("utf-8", errors="replace")

    exit_code: int | None
    cli_error: str | None
    if timed_out:
        exit_code, cli_error = None, None
    elif proc.returncode == 0:
        exit_code, cli_error = 0, None
    else:
        match = _EXIT_STATUS_RE.search(stderr)
        if match:
            exit_code, cli_error = int(match.group(1)), None
        else:
            exit_code = None
            cli_error = stderr.strip() or f"coder ssh exited with code {proc.returncode}"

    return ExecResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        truncated=truncated[0],
        duration_secs=duration,
        timed_out=timed_out,
        cli_error=cli_error,
    )
