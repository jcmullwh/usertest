from __future__ import annotations

from pathlib import Path

from agent_adapters.codex_config import toml_basic_string
from agent_adapters.mcp.spec import McpConfig


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_int(value: int) -> str:
    return str(int(value))


def _toml_str_array(values: list[str]) -> str:
    return "[" + ", ".join(toml_basic_string(v) for v in values) + "]"


def _toml_inline_table(values: dict[str, str]) -> str:
    parts: list[str] = []
    for key in sorted(values.keys()):
        parts.append(f"{toml_basic_string(key)} = {toml_basic_string(values[key])}")
    return "{ " + ", ".join(parts) + " }"


def render_codex_mcp_config_toml(mcp: McpConfig) -> str:
    """
    Return a TOML document for Codex MCP configuration.

    The output must be parseable by Python's `tomllib` and uses:
    - [mcp_servers.<name>] tables per server
    - [mcp_servers.<name>.env] nested tables for explicit env values
    """

    mcp.validate()

    lines: list[str] = []

    # Emit servers in sorted order for deterministic output.
    for name in sorted(mcp.servers.keys()):
        server = mcp.servers[name]
        lines.append(f"[mcp_servers.{name}]")

        # Key emission order is fixed for deterministic output and readability.
        if server.enabled is not None:
            lines.append(f"enabled = {_toml_bool(server.enabled)}")

        if server.transport == "stdio":
            assert server.command is not None
            lines.append(f"command = {toml_basic_string(server.command)}")
            if server.args:
                lines.append(f"args = {_toml_str_array(server.args)}")
            if server.cwd is not None:
                lines.append(f"cwd = {toml_basic_string(server.cwd)}")
        else:
            assert server.url is not None
            lines.append(f"url = {toml_basic_string(server.url)}")
            if server.bearer_token_env_var is not None:
                lines.append(
                    f"bearer_token_env_var = {toml_basic_string(server.bearer_token_env_var)}"
                )
            if server.http_headers:
                lines.append(f"http_headers = {_toml_inline_table(server.http_headers)}")
            if server.env_http_headers:
                lines.append(f"env_http_headers = {_toml_inline_table(server.env_http_headers)}")

        if server.env_vars:
            lines.append(f"env_vars = {_toml_str_array(server.env_vars)}")
        if server.enabled_tools:
            lines.append(f"enabled_tools = {_toml_str_array(server.enabled_tools)}")
        if server.disabled_tools:
            lines.append(f"disabled_tools = {_toml_str_array(server.disabled_tools)}")
        if server.startup_timeout_sec is not None:
            lines.append(f"startup_timeout_sec = {_toml_int(server.startup_timeout_sec)}")
        if server.tool_timeout_sec is not None:
            lines.append(f"tool_timeout_sec = {_toml_int(server.tool_timeout_sec)}")

        if server.env:
            lines.append("")
            lines.append(f"[mcp_servers.{name}.env]")
            for key in sorted(server.env.keys()):
                lines.append(f"{toml_basic_string(key)} = {toml_basic_string(server.env[key])}")

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_codex_mcp_config(*, codex_home_dir: Path, mcp: McpConfig) -> Path:
    """
    Write `codex_home_dir/config.toml` (creating directories as needed) and return the path.
    """

    codex_home_dir.mkdir(parents=True, exist_ok=True)
    path = codex_home_dir / "config.toml"
    path.write_text(render_codex_mcp_config_toml(mcp), encoding="utf-8", newline="\n")
    return path
