from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sandbox_runner.image_hash import compute_image_hash
from sandbox_runner.spec import MountSpec, ResourceSpec, SandboxInstance, SandboxSpec

_DEFAULT_DOCKER_IMAGE_REPO = "sandbox-runner"
_DOCKER_TIMEOUT_ENV = "SANDBOX_RUNNER_DOCKER_TIMEOUT_SECONDS"
_SAFE_ENV_KEYS_FOR_META: frozenset[str] = frozenset(
    {
        "TMPDIR",
        "TMP",
        "TEMP",
        "PIP_CACHE_DIR",
        "PIP_BUILD_DIR",
        "PIP_NO_CACHE_DIR",
        "PIP_DISABLE_PIP_VERSION_CHECK",
        "PIP_NO_INPUT",
        "PYTEST_ADDOPTS",
    }
)


def _sanitize_container_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-.")
    if not cleaned or not re.match(r"^[a-zA-Z0-9]", cleaned):
        cleaned = f"sandbox-{cleaned}".strip("-.")
    return cleaned[:128] if len(cleaned) > 128 else cleaned


def _get_docker_timeout_seconds() -> float | None:
    raw = os.environ.get(_DOCKER_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return None

    try:
        timeout = float(raw)
    except ValueError:
        return None

    if timeout <= 0:
        return None
    return timeout


def _docker_run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "Docker CLI not found. Ensure `docker` is installed and available on PATH."
        ) from e
    except subprocess.TimeoutExpired as e:
        timeout_note = f"{timeout_seconds:.1f}s" if timeout_seconds is not None else "unknown"
        raise RuntimeError(
            "Docker command timed out.\n"
            f"timeout={timeout_note}\n"
            f"argv={' '.join(argv)}\n"
            "Tip: adjust it via timeout_seconds / SandboxSpec.docker_timeout_seconds or "
            "SANDBOX_RUNNER_DOCKER_TIMEOUT_SECONDS (0 disables it)."
        ) from e


def _ensure_docker_available(*, timeout_seconds: float | None = None) -> None:
    proc = _docker_run(["docker", "version"], check=False, timeout_seconds=timeout_seconds)
    if proc.returncode == 0:
        return
    msg = proc.stderr.strip() or proc.stdout.strip()
    raise RuntimeError(
        "Docker is unavailable. Ensure the Docker daemon is running and reachable.\n"
        f"{msg}\n"
    )


def _docker_image_exists(tag: str, *, timeout_seconds: float | None = None) -> bool:
    proc = _docker_run(
        ["docker", "image", "inspect", tag], check=False, timeout_seconds=timeout_seconds
    )
    return proc.returncode == 0


def _resource_args(resources: ResourceSpec | None) -> list[str]:
    if resources is None:
        return []
    out: list[str] = []
    if resources.cpus is not None:
        out.extend(["--cpus", str(resources.cpus)])
    if resources.memory is not None:
        out.extend(["--memory", str(resources.memory)])
    if resources.pids_limit is not None:
        out.extend(["--pids-limit", str(resources.pids_limit)])
    return out


def _mount_args(mounts: list[MountSpec]) -> list[str]:
    out: list[str] = []
    for m in mounts:
        spec = f"type=bind,source={m.host_path},target={m.container_path}"
        if m.read_only:
            spec += ",readonly"
        out.extend(["--mount", spec])
    return out


def _env_args_with_overrides(
    env_allowlist: list[str],
    env_overrides: Mapping[str, str] | None,
) -> list[str]:
    overrides: dict[str, str] = {}
    if env_overrides:
        for key, value in env_overrides.items():
            if not isinstance(key, str) or not key.strip():
                continue
            if not isinstance(value, str):
                continue
            overrides[key] = value

    out: list[str] = []
    for key in env_allowlist:
        if not isinstance(key, str) or not key.strip():
            continue
        if key in overrides:
            continue
        value = os.environ.get(key)
        if value is None:
            continue
        out.extend(["-e", f"{key}={value}"])

    for key in sorted(overrides):
        out.extend(["-e", f"{key}={overrides[key]}"])
    return out


def _env_overrides_meta(env_overrides: Mapping[str, str] | None) -> dict[str, Any]:
    if not env_overrides:
        return {"env_overrides_keys": [], "env_overrides_safe": {}}
    keys: list[str] = []
    safe: dict[str, str] = {}
    for key, value in env_overrides.items():
        if not isinstance(key, str) or not key.strip():
            continue
        keys.append(key)
        if key in _SAFE_ENV_KEYS_FOR_META and isinstance(value, str):
            safe[key] = value
    keys_sorted = sorted({k for k in keys if k.strip()})
    safe_sorted = {k: safe[k] for k in sorted(safe)}
    return {"env_overrides_keys": keys_sorted, "env_overrides_safe": safe_sorted}


@dataclass
class DockerSandboxInstance(SandboxInstance):
    workspace_mount: str
    artifacts_mount: str
    cache_mount: str | None
    command_prefix: list[str]

    container_name: str
    image_tag: str
    image_hash: str
    docker_timeout_seconds: float | None = None

    keep_container: bool = False
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.keep_container:
            return
        try:
            _docker_run(
                ["docker", "rm", "-f", self.container_name],
                check=False,
                timeout_seconds=self.docker_timeout_seconds,
            )
        except Exception:
            # Best-effort cleanup only.
            return


class DockerSandbox:
    def __init__(
        self,
        workspace_dir: Path,
        artifacts_dir: Path,
        spec: SandboxSpec,
        container_name: str | None = None,
    ) -> None:
        if spec.backend != "docker":
            raise ValueError(f"DockerSandbox requires spec.backend='docker', got {spec.backend!r}")
        self._workspace_dir = workspace_dir
        self._artifacts_dir = artifacts_dir
        self._spec = spec
        self._container_name = container_name

    def start(self) -> DockerSandboxInstance:
        # Ensure we can write progress/log artifacts even if Docker hangs.
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        progress_path = self._artifacts_dir / "docker_progress.txt"

        def _progress(message: str) -> None:
            try:
                timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
                with progress_path.open("a", encoding="utf-8", newline="\n") as f:
                    f.write(f"{timestamp}\t{message}\n")
            except Exception:
                return

        _progress("start")

        spec = self._spec

        docker_timeout_seconds = getattr(spec, "docker_timeout_seconds", None)
        if docker_timeout_seconds is None:
            docker_timeout_seconds = _get_docker_timeout_seconds()
        _progress(f"docker timeout seconds: {docker_timeout_seconds}")

        _progress("docker version")
        _ensure_docker_available(timeout_seconds=docker_timeout_seconds)
        _progress("docker available")

        context_dir = spec.image_context_path
        if context_dir is None:
            raise ValueError("Docker sandbox requires spec.image_context_path.")
        context_dir = context_dir.resolve()
        if not context_dir.exists() or not context_dir.is_dir():
            raise FileNotFoundError(f"Missing Docker image context directory: {context_dir}")

        dockerfile_path = spec.dockerfile
        if dockerfile_path is None:
            dockerfile_path = context_dir / "Dockerfile"
        elif not dockerfile_path.is_absolute():
            dockerfile_path = context_dir / dockerfile_path
        dockerfile_path = dockerfile_path.resolve()
        if not dockerfile_path.exists() or not dockerfile_path.is_file():
            raise FileNotFoundError(f"Missing Dockerfile: {dockerfile_path}")

        _progress("compute image hash")
        image_hash = compute_image_hash(context_dir=context_dir, dockerfile=dockerfile_path)
        image_repo = spec.image_repo.strip() if isinstance(spec.image_repo, str) else ""
        image_repo = image_repo if image_repo else _DEFAULT_DOCKER_IMAGE_REPO
        image_tag = f"{image_repo}:{image_hash[:12]}"
        _progress(f"image tag {image_tag}")

        # Make sure we have somewhere to write build logs, even if the build fails.
        build_log_path = self._artifacts_dir / "docker_build.log"

        if spec.rebuild_image or not _docker_image_exists(
            image_tag, timeout_seconds=docker_timeout_seconds
        ):
            dockerfile_ref = str(dockerfile_path)
            try:
                dockerfile_ref = (
                    dockerfile_path.resolve().relative_to(context_dir.resolve()).as_posix()
                )
            except ValueError:
                dockerfile_ref = str(dockerfile_path)

            # Stream build output to both the console and a log file so long builds
            # don't look "hung" when invoked from the CLI.
            _progress("docker build")
            rc = _docker_build_streaming(
                argv=[
                    "docker",
                    "build",
                    "--progress=plain",
                    "-t",
                    image_tag,
                    "-f",
                    dockerfile_ref,
                    ".",
                ],
                cwd=context_dir,
                log_path=build_log_path,
            )
            if rc != 0:
                raise RuntimeError(
                    "Docker image build failed.\n"
                    f"tag={image_tag}\n"
                    f"context={context_dir}\n"
                    f"dockerfile={dockerfile_path}\n"
                    f"build_log={build_log_path}\n"
                )
        else:
            _progress("docker build skipped (image exists)")

        container_name = self._container_name or f"sandbox-{uuid.uuid4().hex[:12]}"
        container_name = _sanitize_container_name(container_name)

        workspace_mount = "/workspace"
        artifacts_mount = "/artifacts"

        mounts: list[MountSpec] = [
            MountSpec(
                host_path=self._workspace_dir.resolve(),
                container_path=workspace_mount,
                read_only=False,
            ),
            MountSpec(
                host_path=self._artifacts_dir.resolve(),
                container_path=artifacts_mount,
                read_only=False,
            ),
        ]

        cache_mount: str | None = None
        if spec.cache_mode == "warm":
            if spec.cache_dir is None:
                raise ValueError("cache_mode='warm' requires spec.cache_dir.")
            cache_mount = "/cache"
            spec.cache_dir.mkdir(parents=True, exist_ok=True)

            # Best-effort: create a minimal cache directory layout expected by the
            # built-in sandbox_cli image.
            #
            # The sandbox_cli Dockerfile links common tool caches to:
            #   /cache/pip
            #   /cache/pdm
            #   /cache/pdm-share
            # If these targets don't exist in a fresh host cache dir, some tools can
            # mis-handle the symlink path and error.
            for rel in ("pip", "pdm", "pdm-share"):
                try:
                    (spec.cache_dir / rel).mkdir(parents=True, exist_ok=True)
                except OSError:
                    # If we can't create these directories (permissions, etc), proceed.
                    # The container may still be able to create what it needs.
                    pass
            mounts.append(
                MountSpec(
                    host_path=spec.cache_dir.resolve(),
                    container_path=cache_mount,
                    read_only=False,
                )
            )

        mounts.extend(spec.extra_mounts)

        network_args: list[str] = []
        if spec.network_mode == "none":
            network_args = ["--network", "none"]
        elif spec.network_mode == "open":
            network_args = []
        else:
            raise ValueError(f"Unsupported network_mode={spec.network_mode!r}")

        run_argv: list[str] = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            *_env_args_with_overrides(spec.env_allowlist, spec.env_overrides),
            *_resource_args(spec.resources),
            *network_args,
            *_mount_args(mounts),
            image_tag,
            "sh",
            "-lc",
            "sleep infinity",
        ]

        _progress(f"docker run {container_name}")
        proc = _docker_run(run_argv, check=False, timeout_seconds=docker_timeout_seconds)
        if proc.returncode != 0:
            raise RuntimeError(
                "Failed to start Docker sandbox container.\n"
                f"container_name={container_name}\n"
                f"image={image_tag}\n"
                f"stdout:\n{proc.stdout}\n"
                f"stderr:\n{proc.stderr}\n"
            )

        command_prefix = ["docker", "exec", "-i", "-w", workspace_mount, container_name]

        instance = DockerSandboxInstance(
            workspace_mount=workspace_mount,
            artifacts_mount=artifacts_mount,
            cache_mount=cache_mount,
            command_prefix=command_prefix,
            container_name=container_name,
            image_tag=image_tag,
            image_hash=image_hash,
            docker_timeout_seconds=docker_timeout_seconds,
            keep_container=spec.keep_container,
        )

        meta: dict[str, Any] = {
            "backend": "docker",
            "image_tag": image_tag,
            "image_hash": image_hash,
            "image_repo": image_repo,
            "context_dir": str(context_dir),
            "dockerfile": str(dockerfile_path),
            "container_name": container_name,
            "workspace_mount": workspace_mount,
            "artifacts_mount": artifacts_mount,
            "cache_mode": spec.cache_mode,
            "cache_dir": str(spec.cache_dir) if spec.cache_dir is not None else None,
            "network_mode": spec.network_mode,
            "docker_timeout_seconds": docker_timeout_seconds,
            "env_allowlist": [k for k in spec.env_allowlist if isinstance(k, str) and k.strip()],
            **_env_overrides_meta(getattr(spec, "env_overrides", None)),
            "extra_mounts": [
                {
                    "host_path": str(m.host_path),
                    "container_path": m.container_path,
                    "read_only": m.read_only,
                }
                for m in spec.extra_mounts
            ],
        }

        sandbox_meta_path = self._artifacts_dir / "sandbox.json"
        sandbox_meta_path.parent.mkdir(parents=True, exist_ok=True)
        sandbox_meta_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        _progress("ready")
        return instance


def _docker_build_streaming(*, argv: list[str], cwd: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        log.write(f"$ {' '.join(argv)}\n")
        log.write(f"cwd={cwd}\n\n")
        log.flush()

        proc = subprocess.Popen(  # noqa: S603
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        log_enabled = True
        print_enabled = True

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if log_enabled:
                    try:
                        log.write(line)
                        log.flush()
                    except OSError:
                        log_enabled = False

                if print_enabled:
                    try:
                        print(line, end="", flush=True)
                    except OSError:
                        print_enabled = False
        except Exception as exc:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            raise RuntimeError(
                "Failed while streaming Docker build output.\n"
                f"cwd={cwd}\n"
                f"log_path={log_path}\n"
                f"argv={' '.join(argv)}\n"
            ) from exc

        return proc.wait()
