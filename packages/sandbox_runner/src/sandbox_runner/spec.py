from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self


@dataclass(frozen=True)
class MountSpec:
    host_path: Path
    container_path: str
    read_only: bool = False


@dataclass(frozen=True)
class ResourceSpec:
    cpus: float | None = None
    memory: str | None = None
    pids_limit: int | None = None


@dataclass(frozen=True)
class SandboxSpec:
    backend: Literal["local", "docker"] = "local"

    image_context_path: Path | None = None
    dockerfile: Path | None = None

    network_mode: Literal["open", "none"] = "open"

    cache_mode: Literal["cold", "warm"] = "cold"
    cache_dir: Path | None = None

    env_allowlist: list[str] = field(default_factory=list)
    extra_mounts: list[MountSpec] = field(default_factory=list)
    resources: ResourceSpec | None = None

    keep_container: bool = False
    rebuild_image: bool = False
    image_repo: str | None = None

    # Optional per-run timeout (in seconds) for Docker CLI operations issued by sandbox_runner
    # (e.g., docker version/image inspect/run/rm). When None, no timeout is applied.
    docker_timeout_seconds: float | None = None


class SandboxInstance:
    workspace_mount: str
    artifacts_mount: str
    command_prefix: list[str]

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
