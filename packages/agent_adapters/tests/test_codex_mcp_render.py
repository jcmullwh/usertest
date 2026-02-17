from __future__ import annotations

import tomllib
from pathlib import Path

from agent_adapters.mcp.codex import render_codex_mcp_config_toml, write_codex_mcp_config
from agent_adapters.mcp.spec import McpConfig, McpServer


def test_render_codex_mcp_config_toml_parses_and_has_expected_shape() -> None:
    mcp = McpConfig(
        servers={
            "echo": McpServer(
                transport="stdio",
                command="python",
                args=["-m", "my_mcp_server"],
                env={"FOO": "bar"},
                env_vars=["OPENAI_API_KEY"],
            ),
            "remote": McpServer(
                transport="http",
                url="https://example.invalid/mcp",
                bearer_token_env_var="MCP_BEARER_TOKEN",
                http_headers={"X-Test": "1"},
                env_http_headers={"Authorization": "AUTH_ENV"},
            ),
        }
    )

    rendered = render_codex_mcp_config_toml(mcp)
    parsed = tomllib.loads(rendered)

    assert parsed["mcp_servers"]["echo"]["command"] == "python"
    assert parsed["mcp_servers"]["echo"]["args"] == ["-m", "my_mcp_server"]
    assert parsed["mcp_servers"]["echo"]["env_vars"] == ["OPENAI_API_KEY"]
    assert parsed["mcp_servers"]["echo"]["env"]["FOO"] == "bar"

    assert parsed["mcp_servers"]["remote"]["url"] == "https://example.invalid/mcp"
    assert parsed["mcp_servers"]["remote"]["bearer_token_env_var"] == "MCP_BEARER_TOKEN"
    assert parsed["mcp_servers"]["remote"]["http_headers"]["X-Test"] == "1"
    assert parsed["mcp_servers"]["remote"]["env_http_headers"]["Authorization"] == "AUTH_ENV"


def test_write_codex_mcp_config_writes_trailing_newline(tmp_path: Path) -> None:
    mcp = McpConfig(
        servers={
            "echo": McpServer(transport="stdio", command="python", args=["-c", "print('ok')"]),
        }
    )
    codex_home = tmp_path / "codex_home"
    path = write_codex_mcp_config(codex_home_dir=codex_home, mcp=mcp)
    assert path == codex_home / "config.toml"

    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    tomllib.loads(raw)
