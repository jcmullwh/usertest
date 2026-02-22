from __future__ import annotations

from runner_core.runner import _build_binary_missing_hints


def test_binary_missing_hints_for_docker_include_rebuild_and_logs() -> None:
    hints = _build_binary_missing_hints(
        agent="claude",
        required_binary="claude",
        exec_backend="docker",
        agent_cfg={"sandbox_cli_install": {"npm_global": ["@anthropic-ai/claude-code"]}},
        command_prefix=["docker", "exec", "-i", "-w", "/workspace", "sandbox-abc"],
    )

    assert isinstance(hints, dict)
    assert "--exec-rebuild-image" in str(hints.get("install", ""))
    assert "docker_build.log" in str(hints.get("debug", ""))
    assert "@anthropic-ai/claude-code" in str(hints.get("install", ""))
    assert "sandbox-abc" in str(hints.get("container", ""))
