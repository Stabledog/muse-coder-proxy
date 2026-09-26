"""Integration tests against the real `coder` CLI and the real test-junkyard
workspace. No mocking: this module exists to validate the exact assumptions
DESIGN.md makes about coder's behavior on Windows."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import coder_exec

CODER_URL = os.environ["CODER_URL"]
CODER_TOKEN = os.environ["CODER_SESSION_TOKEN"]
WORKSPACE = "test-junkyard"


def test_list_workspaces_includes_test_junkyard():
    workspaces = coder_exec.list_workspaces(CODER_URL, CODER_TOKEN)
    names = {w["name"] for w in workspaces}
    assert WORKSPACE in names
    match = next(w for w in workspaces if w["name"] == WORKSPACE)
    assert match["status"] == "running"
    assert match["template"]


def test_exec_simple_command():
    result = coder_exec.exec_command(CODER_URL, CODER_TOKEN, WORKSPACE, "hostname", 30, 65536)
    assert result.exit_code == 0
    assert "test-junkyard" in result.stdout
    assert not result.timed_out
    assert not result.truncated


def test_exec_nonzero_exit():
    result = coder_exec.exec_command(CODER_URL, CODER_TOKEN, WORKSPACE, "exit 7", 30, 65536)
    assert result.exit_code == 7
    assert result.cli_error is None
    assert not result.timed_out


def test_exec_merges_remote_stdout_and_stderr():
    # coder ssh allocates a PTY, so the remote command's stdout and stderr
    # arrive merged on our local stdout; local stderr is reserved for
    # coder-CLI-level diagnostics, not the remote command's stderr.
    result = coder_exec.exec_command(
        CODER_URL, CODER_TOKEN, WORKSPACE, "echo out-marker && echo err-marker 1>&2", 30, 65536
    )
    assert "out-marker" in result.stdout
    assert "err-marker" in result.stdout
    assert result.cli_error is None


def test_exec_timeout_kills_process_and_returns_partial_output():
    start = time.monotonic()
    result = coder_exec.exec_command(
        CODER_URL, CODER_TOKEN, WORKSPACE, "echo before-sleep && sleep 30 && echo after-sleep", 2, 65536
    )
    elapsed = time.monotonic() - start
    assert result.timed_out
    assert "before-sleep" in result.stdout
    assert "after-sleep" not in result.stdout
    # generous upper bound: should be killed close to the 2s timeout, not run to completion
    assert elapsed < 15


def test_exec_output_truncation():
    # 65536 * 2 bytes of 'x' comfortably exceeds a 1024-byte cap
    result = coder_exec.exec_command(
        CODER_URL, CODER_TOKEN, WORKSPACE, "yes x | head -c 131072", 30, 1024
    )
    assert result.truncated
    assert len(result.stdout.encode("utf-8")) <= 1024


def test_exec_unknown_workspace_fails_cleanly():
    result = coder_exec.exec_command(CODER_URL, CODER_TOKEN, "this-workspace-does-not-exist", "hostname", 30, 65536)
    assert result.exit_code is None
    assert result.cli_error is not None
    assert not result.timed_out
