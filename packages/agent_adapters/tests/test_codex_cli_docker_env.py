from __future__ import annotations

from agent_adapters.codex_cli import _prepare_codex_argv_and_env


def test_prepare_codex_argv_and_env_injects_for_docker_exec_prefix() -> None:
    argv = ["codex", "exec", "--json", "-"]
    prefix = ["docker", "exec", "-i", "-w", "/workspace", "c1"]
    full_argv, env = _prepare_codex_argv_and_env(
        argv=argv,
        prefix=prefix,
        env_overrides={"B": "2", "A": "1"},
    )
    assert env is None
    assert full_argv == [
        "docker",
        "exec",
        "-i",
        "-w",
        "/workspace",
        "-e",
        "A=1",
        "-e",
        "B=2",
        "c1",
        "codex",
        "exec",
        "--json",
        "-",
    ]


def test_prepare_codex_argv_and_env_merges_env_without_prefix() -> None:
    argv = ["codex", "exec", "--json", "-"]
    full_argv, env = _prepare_codex_argv_and_env(
        argv=argv,
        prefix=[],
        env_overrides={"FOO": "bar"},
    )
    assert full_argv == argv
    assert env is not None
    assert env.get("FOO") == "bar"
