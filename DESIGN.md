# Coder Proxy — Design

## Goal

Give Bert (a Muse agent running on a hosted VM) a narrow, auditable HTTP API for
running commands in Coder workspaces on Stablebeast, over the Tailscale
tailnet. This service is the trust boundary between Bert and the homelab:
everything Bert may do goes through here, and every call is logged.

## Non-goals (v1)

- No interactive PTY sessions. One-shot exec only.
- No proxying of arbitrary Coder API calls. The API surface is `list` + `exec`,
  nothing wider.
- No multi-user auth. There is exactly one client: Bert.

## Architecture

```
Bert (Muse VM)
  --HTTPS--> egress/tunnel proxy :3130 --WireGuard tailnet--> 100.99.196.68:PORT
                                                              (proxy on Stablebeast)
                                                                    |
                                                              coder CLI (subprocess)
                                                                    |
                                                              Coder at localhost:7080
                                                                    |
                                                              workspace agents (docker)
```

- The proxy runs on Stablebeast (Windows 11) and binds to the Tailscale
  interface (`100.99.196.68`). Plain HTTP is fine — tailnet traffic is already
  WireGuard-encrypted end to end.
- The proxy holds a Coder session token server-side (env var or config file,
  never committed to the repo) and shells out to the `coder` CLI, which must
  be installed on Stablebeast.
- Bert reaches the proxy through the tailnet only. There is no public ingress.

## API

All requests carry `Authorization: Bearer <token>`. The token is a long random
string in the proxy's config, shared with Bert out-of-band.

### `GET /health`
`200 {"status":"ok","version":"..."}`. Auth required (no unauthenticated
endpoints except none).

### `GET /workspaces`
`200 {"workspaces":[{"name":"...","status":"...","template":"..."}]}`.
Returns only workspaces on the allowlist — Bert cannot enumerate anything
else.

### `POST /exec`
Request:
```json
{ "workspace": "test-junkyard", "command": "hostname && pwd", "timeout_secs": 300 }
```
- `timeout_secs` optional, default 300, max 600 (configurable).
- The command is executed as `coder ssh <workspace> -- <command>`, with
  output piped and exit code captured (see the PTY caveat below). On timeout
  the process tree is killed and partial output is returned.

Response:
```json
{
  "exit_code": 0,
  "stdout": "...", "stderr": "...",
  "truncated": false,
  "duration_secs": 1.23
}
```
- `409`/`404` for unknown workspace, `403` for workspace not on the
  allowlist, `401` for bad/missing bearer token, `408` on timeout (with
  partial output included).

**Build-time finding:** `coder ssh` always allocates a PTY for the remote
command (no flag to disable it in v2.33.2), which has two consequences:
- The remote command's stdout and stderr arrive merged on our local stdout.
  `stdout` in the response is that merged stream; `stderr` is reserved for
  `coder`-CLI-level diagnostics (connection failure, workspace unreachable),
  not the remote command's stderr.
- A nonzero remote exit code isn't `coder ssh`'s own exit code (that's always
  1 on any remote failure) — it's recovered by regexing `coder`'s own error
  text (`Process exited with status N`) off local stderr. If that pattern
  isn't found on a nonzero exit, it's a CLI-level failure, not a completed
  exec, and the proxy returns `502` instead of a fake exit code.

Going through `coder ssh --stdio` with a raw SSH client would give true
stream separation and a trustworthy exit code, but adds a third-party SSH
dependency and real complexity for a v1 tool whose non-goals already rule
out PTY/interactive sessions. Deferred unless demonstrated need.

## Trust boundary and scoping

- **Bearer token** for client auth. Tailnet membership alone is not
  authentication.
- **Bind to the tailnet IP only.** Belt-and-braces: refuse requests whose
  source IP is outside `100.64.0.0/10`.
- **Workspace allowlist** in config. Exec refuses anything not listed. There
  is deliberately no command filtering — the boundary is *which workspaces*,
  not *which commands*. Bert is trusted with a shell in the allowlisted
  workspaces; the blast radius is bounded by the allowlist, not by second-
  guessing commands.
- **Audit log**: append-only, one JSON object per line per call —
  timestamp, remote_addr, method, path, status_code, and (for `/exec`)
  workspace, command, exit_code, duration_secs, truncated, and stdout/stderr
  truncated to 4KB each. Truncated text, not a hash — a hash isn't reviewable
  by a human later, and 4KB per call keeps the log both readable and bounded.
  This log is the audit trail and must not be optional.
- **Output caps**: truncate stdout/stderr, independently, at `max_output_bytes`
  each (default 64KB) — not the combined-stream head+tail style originally
  suggested. Combining two concurrently-read pipes into one true head+tail
  view adds real complexity for a v1 tool; a plain per-stream cutoff is both
  simpler and defends against the same runaway-output case. Revisit only on
  demonstrated need.

## Configuration

TOML file for structure, environment variables for secrets only
(`config.toml`, gitignored; template in `config.example.toml`):
- `listen_addr` (default `100.99.196.68`), `port` (default `8090`)
- `coder_url` (default `http://127.0.0.1:7080`)
- `workspace_allowlist` (list)
- `trusted_cidr` (default `100.64.0.0/10`)
- `audit_log_path`, `default_timeout_secs`, `max_timeout_secs`, `max_output_bytes`
- Secrets, env vars only, never written to a file: `MUSE_PROXY_BEARER_TOKEN`,
  `MUSE_CODER_TOKEN` (named distinctly from an interactive user's own
  `CODER_SESSION_TOKEN` so the two are never accidentally conflated).

## Operational notes (Windows)

- Built in pure Python 3.13 stdlib — `http.server.ThreadingHTTPServer`, no
  micro-framework. Three routes and one trusted client don't justify a web
  framework dependency on a hand-started Windows console script.
  Built and tested directly on Stablebeast, so Windows-native behavior
  (subprocess, paths, quoting) was verified there, not assumed. In
  particular, Windows has no POSIX process groups: exec launches with
  `CREATE_NEW_PROCESS_GROUP` and a timeout kill uses `taskkill /PID <pid> /T
  /F` (tree-kill) rather than `Popen.terminate()`.
- v1 starts automatically at Windows login via a stub in the Startup folder
  (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\muse-coder-proxy.cmd`,
  copied from `scripts/muse-coder-proxy.cmd`) calling `scripts/start-proxy.ps1`,
  which is idempotent (won't start a second copy if one's already running) and
  logs to `logs/`. Not a Windows Service - no auto-restart on crash, no
  `services.msc` visibility. Service-ifying (NSSM / scheduled task) is a
  later step if that's ever needed.
- `config.toml` is read once at startup. After editing it (e.g. to change the
  allowlist), run `scripts\restart-proxy.ps1`: it stops the running proxy,
  re-runs the launcher, and prints the new startup log (including the
  allowlist in effect). It aborts without stopping anything if the `MUSE_*`
  secrets are missing from the calling shell. An in-flight `/exec` is dropped.
- The `coder` CLI must be on PATH for the proxy process; it's invoked with
  `CODER_URL`/`CODER_SESSION_TOKEN` set from the proxy's own config/env
  (`coder_url` / `MUSE_CODER_TOKEN`), not inherited from the launching shell.

## Coder token for the proxy

The proxy needs a Coder token with exec access to the allowlisted workspaces.

**Build-time finding:** Coder v2.33.2 supports resource-scoped tokens
(`coder tokens create --allow workspace:<uuid>`), which looked like an easy
least-privilege win. Tested and rejected: a token scoped this way was denied
`coder ssh` even against its own allowed workspace, and `coder list` under it
hit a server-side SQL error (`column reference "id" is ambiguous`) — a bug in
this version's scoping, not something to build a security boundary on. v1
uses a dedicated full-access token instead (env var `MUSE_CODER_TOKEN`,
distinct from any interactive user's own `CODER_SESSION_TOKEN`), with the
allowlist as the only real boundary. Revisit `--allow` scoping if a future
Coder version fixes it.

## Acceptance — the live test

From Bert's VM, over the tailnet:
1. `GET /health` → 200.
2. `GET /workspaces` → allowlisted workspaces visible.
3. `POST /exec {"workspace":"test-junkyard","command":"hostname && pwd"}`
   → exit 0, output matching the workspace.
4. The audit log contains all three calls.
5. Negative checks: exec on a non-allowlisted workspace → refused; wrong
   bearer token → 401.

**Verified 2026-09-26**, run locally on Stablebeast against the real tailnet
IP (100.99.196.68:8090) and the real `test-junkyard` workspace — not yet run
from Bert's VM itself, since that requires Bert's operator to have the
proxy's bearer token. All five checks passed, including the audit log
capturing every call (auth failures, the successful exec, and the allowlist
rejection) with accurate timestamps and source addresses.

## Open questions (not blockers)

- Whether persistent shell sessions are ever needed (one-shot covers the
  known use cases; add only on demonstrated need).
- Whether a real Windows Service (NSSM or similar) is ever needed for
  auto-restart-on-crash - the Startup-folder launcher covers "runs at
  login" but not "comes back if it dies mid-session."
