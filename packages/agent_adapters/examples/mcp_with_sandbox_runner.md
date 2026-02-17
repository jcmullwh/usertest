# MCP with `sandbox_runner` + `agent_adapters` (Codex example)

This example shows how to run Codex inside a Docker sandbox (via `sandbox_runner`) while providing
per-run MCP server configuration (via `agent_adapters.mcp`), without editing any global dotfiles.

## What this wiring does

- Starts a long-lived Docker container (“sandbox”) with a mounted workspace and artifacts directory.
- Renders a Codex MCP `config.toml` into a per-run “Codex home” directory under the artifacts mount.
- Runs Codex *inside the container* using a `docker exec ...` prefix, injecting required env vars
  (like `CODEX_HOME` and any MCP tokens) into the `docker exec` invocation.

## Example (Python)

```python
from pathlib import Path

from agent_adapters.codex_cli import run_codex_exec
from agent_adapters.mcp.spec import McpConfig, McpServer
from agent_adapters.mcp.codex import write_codex_mcp_config
from sandbox_runner import DockerSandbox
from sandbox_runner.spec import SandboxSpec


def main() -> None:
    repo_root = Path(".").resolve()

    # These are HOST paths. They will be mounted into the container by sandbox_runner.
    workspace_dir = repo_root  # or a cloned target repo
    artifacts_dir = repo_root / "runs" / "example_mcp" / "artifacts"

    # sandbox_runner mounts workspace -> /workspace and artifacts -> /artifacts by default.
    spec = SandboxSpec(
        backend="docker",
        image_context_path=repo_root / "packages" / "sandbox_runner" / "builtins" / "docker" / "contexts" / "sandbox_cli",
        network_mode="open",
    )
    sandbox = DockerSandbox(
        workspace_dir=workspace_dir,
        artifacts_dir=artifacts_dir,
        spec=spec,
        container_name="sandbox-example-mcp",
    ).start()

    try:
        # Build an agent-agnostic MCP config.
        mcp = McpConfig(
            servers={
                "echo": McpServer(
                    transport="stdio",
                    command="python",
                    args=["-m", "my_mcp_server"],
                    env_vars=["OPENAI_API_KEY"],  # allow passing this env var through
                ),
                "remote": McpServer(
                    transport="http",
                    url="https://example.invalid/mcp",
                    bearer_token_env_var="MCP_BEARER_TOKEN",
                ),
            }
        )

        # Write Codex MCP config to a HOST path under the artifacts directory.
        # Inside the container, the artifacts directory is mounted at /artifacts.
        host_codex_home = artifacts_dir / "codex_home"
        write_codex_mcp_config(codex_home_dir=host_codex_home, mcp=mcp)

        # Point Codex at the container path for that same directory.
        container_codex_home = f"{sandbox.artifacts_mount}/codex_home"

        run_codex_exec(
            workspace_dir=sandbox.workspace_mount,
            prompt="Return ONLY a JSON object with a single key 'ok' set to true.",
            raw_events_path=artifacts_dir / "raw_events.jsonl",
            last_message_path=artifacts_dir / "agent_last_message.txt",
            stderr_path=artifacts_dir / "agent_stderr.txt",
            sandbox="read-only",
            ask_for_approval="never",
            command_prefix=sandbox.command_prefix,
            env_overrides={
                # This is injected into `docker exec -e ...` so it reaches the process in-container.
                "CODEX_HOME": container_codex_home,
                # Any other env vars your MCP servers need (tokens, endpoints, etc).
                "MCP_BEARER_TOKEN": "REDACTED",
            },
        )
    finally:
        sandbox.close()


if __name__ == "__main__":
    main()
```

Notes:
- In real usage, you’d pass `OPENAI_API_KEY` via the runner’s env allowlist (e.g., `--exec-env OPENAI_API_KEY`).
  For Codex itself, prefer `--exec-use-host-agent-login` to reuse `~/.codex` in Docker without API keys.
  This example only shows the *mechanism* for passing per-run env into a sandboxed `docker exec` run.
- For Claude/Gemini, the same `docker exec -e ...` injection mechanism is used by their adapters,
  so the “pass env via `command_prefix`” pattern generalizes.
