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
- The command is executed as `coder ssh <workspace> -- <command>` — no PTY,
  stdout/stderr piped, exit code captured. On timeout the process group is
  killed and partial output is returned.

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
  timestamp, workspace, command, exit_code, duration_secs, and truncated
  stdout/stderr (or a hash if full output is too noisy; decide at build
  time). This log is the audit trail and must not be optional.
- **Output caps**: truncate stdout/stderr (suggest 64KB combined, head+tail
  with a marker) so a runaway log can't flood the caller.

## Configuration

Env vars or a small TOML file (decide at build time):
- `LISTEN_ADDR` (default `100.99.196.68`), `PORT` (default `8090`)
- `BEARER_TOKEN`
- `CODER_URL` (default `http://127.0.0.1:7080`), `CODER_SESSION_TOKEN`
- `WORKSPACE_ALLOWLIST` (comma-separated)
- `AUDIT_LOG_PATH`, `DEFAULT_TIMEOUT_SECS`, `MAX_OUTPUT_BYTES`

## Operational notes (Windows)

- Python with minimal dependencies (stdlib preferred; one micro-framework at
  most). This will be built and iterated on Stablebeast itself via Claude
  Code, so Windows-native behavior (subprocess, paths, quoting) must be
  tested there, not assumed.
- v1 runs as a console script started by hand. Service-ifying (NSSM /
  scheduled task) is a later step, not v1.
- The `coder` CLI must be on PATH for the proxy process, with
  `CODER_URL`/`CODER_SESSION_TOKEN` available to it.

## Coder token for the proxy

The proxy needs a Coder token with exec access to the allowlisted workspaces.
Simplest v1: reuse an existing token with that access. If Coder's permission
model makes a dedicated least-privilege token easy, prefer that. Decide at
build time; don't gold-plate.

## Acceptance — the live test

From Bert's VM, over the tailnet:
1. `GET /health` → 200.
2. `GET /workspaces` → allowlisted workspaces visible.
3. `POST /exec {"workspace":"test-junkyard","command":"hostname && pwd"}`
   → exit 0, output matching the workspace.
4. The audit log contains all three calls.
5. Negative checks: exec on a non-allowlisted workspace → refused; wrong
   bearer token → 401.

## Open questions (not blockers)

- Exact output-cap size and truncation style.
- Whether persistent shell sessions are ever needed (one-shot covers the
  known use cases; add only on demonstrated need).
- Windows service vs manual start for the long term.
