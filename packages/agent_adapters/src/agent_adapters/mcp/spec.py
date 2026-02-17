from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_non_empty_str(value: object, *, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string; got {value!r}.")


def _validate_optional_timeout(value: object, *, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, int):
        raise ValueError(f"{label} must be an int if set; got {value!r}.")
    if value < 0:
        raise ValueError(f"{label} must be >= 0; got {value!r}.")


@dataclass(frozen=True)
class McpServer:
    transport: Literal["stdio", "http"]

    # stdio transport
    command: str | None = None
    args: list[str] = field(default_factory=list)
    cwd: str | None = None

    # http transport
    url: str | None = None
    bearer_token_env_var: str | None = None
    http_headers: dict[str, str] = field(default_factory=dict)
    env_http_headers: dict[str, str] = field(default_factory=dict)

    # shared
    enabled: bool | None = None
    startup_timeout_sec: int | None = None
    tool_timeout_sec: int | None = None
    enabled_tools: list[str] = field(default_factory=list)
    disabled_tools: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    env_vars: list[str] = field(default_factory=list)

    def validate(self) -> None:
        transport = self.transport
        if transport not in {"stdio", "http"}:
            raise ValueError(f"transport must be 'stdio' or 'http'; got {transport!r}.")

        if transport == "stdio":
            _validate_non_empty_str(self.command, label="command")
            if self.url is not None:
                raise ValueError("stdio MCP servers must not set url.")
        else:
            _validate_non_empty_str(self.url, label="url")
            if self.command is not None:
                raise ValueError("http MCP servers must not set command.")

        if self.cwd is not None:
            _validate_non_empty_str(self.cwd, label="cwd")
        if self.bearer_token_env_var is not None:
            _validate_non_empty_str(self.bearer_token_env_var, label="bearer_token_env_var")

        _validate_optional_timeout(self.startup_timeout_sec, label="startup_timeout_sec")
        _validate_optional_timeout(self.tool_timeout_sec, label="tool_timeout_sec")

        for label, values in (
            ("args", self.args),
            ("enabled_tools", self.enabled_tools),
            ("disabled_tools", self.disabled_tools),
            ("env_vars", self.env_vars),
        ):
            for idx, item in enumerate(values):
                _validate_non_empty_str(item, label=f"{label}[{idx}]")

        for label, mapping in (
            ("http_headers", self.http_headers),
            ("env_http_headers", self.env_http_headers),
            ("env", self.env),
        ):
            for key, value in mapping.items():
                _validate_non_empty_str(key, label=f"{label} key")
                _validate_non_empty_str(value, label=f"{label}[{key!r}]")


@dataclass(frozen=True)
class McpConfig:
    servers: dict[str, McpServer] = field(default_factory=dict)

    def validate(self) -> None:
        for name, server in self.servers.items():
            _validate_non_empty_str(name, label="server name")
            if not _SERVER_NAME_RE.match(name):
                raise ValueError(
                    "Server names must contain only letters, numbers, '_' and '-'.\n"
                    f"name={name!r}"
                )
            server.validate()
