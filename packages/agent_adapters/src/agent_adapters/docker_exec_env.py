from __future__ import annotations

from pathlib import Path


def looks_like_docker_exec_prefix(prefix: list[str]) -> bool:
    """
    Detect whether `prefix` looks like a `docker exec ... <container>` command prefix.

    The sandbox runner uses a `docker exec` prefix to run commands inside a long-lived
    container. When that's the case, environment variables must be passed via
    `docker exec -e KEY=VALUE` (setting `env=...` on the host process does not reliably
    reach the process inside the container).
    """

    if len(prefix) < 3:
        return False

    docker_bin = Path(prefix[0]).name.lower()
    if docker_bin.endswith((".exe", ".cmd", ".bat")):
        docker_bin = Path(docker_bin).stem
    if docker_bin != "docker" or prefix[1] != "exec":
        return False
    # A sandbox "command_prefix" should end at the container name token (before the command).
    return not prefix[-1].startswith("-")


def inject_docker_exec_env(prefix: list[str], env_overrides: dict[str, str]) -> list[str]:
    """
    Return a copy of `prefix` with `docker exec -e KEY=VALUE` flags injected.

    Requirements:
    - Preserves the original prefix option ordering.
    - Inserts env flags immediately before the container name token.
    - Deterministic output: inject keys in sorted order.
    """

    if not prefix:
        return prefix
    if not env_overrides:
        return prefix
    if not looks_like_docker_exec_prefix(prefix):
        return prefix
    if prefix[-1].startswith("-"):
        # We don't have a container token to inject before.
        return prefix

    container_name = prefix[-1]
    out = prefix[:-1]

    keys = [k for k in env_overrides.keys() if isinstance(k, str) and k.strip()]
    for key in sorted(keys):
        value = env_overrides.get(key)
        if not isinstance(value, str):
            continue
        out.extend(["-e", f"{key}={value}"])

    out.append(container_name)
    return out
