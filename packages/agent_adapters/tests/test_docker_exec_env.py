from __future__ import annotations

from agent_adapters.docker_exec_env import inject_docker_exec_env, looks_like_docker_exec_prefix


def test_looks_like_docker_exec_prefix_accepts_sandbox_runner_shape() -> None:
    assert looks_like_docker_exec_prefix(["docker", "exec", "-i", "-w", "/workspace", "c1"]) is True


def test_inject_docker_exec_env_inserts_before_container_sorted() -> None:
    prefix = ["docker", "exec", "-i", "-w", "/workspace", "c1"]
    env_overrides = {"B": "2", "A": "1"}
    assert inject_docker_exec_env(prefix, env_overrides) == [
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
    ]


def test_inject_docker_exec_env_noop_for_non_injectable_prefix() -> None:
    prefix = ["docker", "exec", "-i"]
    assert inject_docker_exec_env(prefix, {"A": "1"}) == prefix


def test_inject_docker_exec_env_noop_for_empty_env() -> None:
    prefix = ["docker", "exec", "-i", "-w", "/workspace", "c1"]
    assert inject_docker_exec_env(prefix, {}) == prefix
