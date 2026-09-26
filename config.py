"""Configuration loading: config.toml for structure, env vars for secrets."""
from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    listen_addr: str
    port: int
    coder_url: str
    coder_token: str
    bearer_token: str
    workspace_allowlist: frozenset[str]
    trusted_cidr: ipaddress.IPv4Network | ipaddress.IPv6Network
    audit_log_path: Path
    default_timeout_secs: int
    max_timeout_secs: int
    max_output_bytes: int


def load(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}\n"
            f"copy config.example.toml to {path.name} and edit it"
        )
    with path.open("rb") as f:
        raw = tomllib.load(f)

    bearer_token = os.environ.get("MUSE_PROXY_BEARER_TOKEN")
    if not bearer_token:
        raise ConfigError("MUSE_PROXY_BEARER_TOKEN environment variable is not set")

    coder_token = os.environ.get("MUSE_CODER_TOKEN")
    if not coder_token:
        raise ConfigError("MUSE_CODER_TOKEN environment variable is not set")

    try:
        allowlist = frozenset(raw["workspace_allowlist"])
    except KeyError as e:
        raise ConfigError(f"missing required config key: {e}") from e
    if not allowlist:
        raise ConfigError("workspace_allowlist is empty — refusing to start with no allowed workspaces")

    try:
        coder_url = raw["coder_url"]
    except KeyError as e:
        raise ConfigError(f"missing required config key: {e}") from e

    return Config(
        listen_addr=raw.get("listen_addr", "127.0.0.1"),
        port=int(raw.get("port", 8090)),
        coder_url=coder_url,
        coder_token=coder_token,
        bearer_token=bearer_token,
        workspace_allowlist=allowlist,
        trusted_cidr=ipaddress.ip_network(raw.get("trusted_cidr", "100.64.0.0/10")),
        audit_log_path=Path(raw.get("audit_log_path", "audit.log")),
        default_timeout_secs=int(raw.get("default_timeout_secs", 300)),
        max_timeout_secs=int(raw.get("max_timeout_secs", 600)),
        max_output_bytes=int(raw.get("max_output_bytes", 65536)),
    )
