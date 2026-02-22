from __future__ import annotations

import importlib.resources
import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml
from sandbox_runner import DockerSandbox, MountSpec, SandboxInstance, SandboxSpec

if TYPE_CHECKING:
    from runner_core.runner import RunRequest


@dataclass(frozen=True)
class ExecutionBackendContext:
    sandbox_instance: SandboxInstance | None
    command_prefix: list[str]
    workspace_mount: str | None
    run_dir_mount: str | None

    def close(self) -> None:
        if self.sandbox_instance is None:
            return
        self.sandbox_instance.close()


_SANDBOX_CLI_PYTHON_VERSION_CANDIDATES: tuple[str, ...] = (
    "3.8",
    "3.9",
    "3.10",
    "3.11",
    "3.12",
    "3.13",
)
_DEFAULT_DOCKER_CONTEXT_REL = Path(
    "packages/sandbox_runner/src/sandbox_runner/builtins/docker/contexts/sandbox_cli"
)


def _copy_builtin_sandbox_cli_context_from_resources(*, run_dir: Path) -> Path | None:
    """
    Copy the built-in sandbox_cli Docker context shipped with the `sandbox_runner` package into
    `run_dir/sandbox/` and return the copied directory.

    Rationale: Docker build contexts must be real filesystem directories, but Python package
    resources are not guaranteed to be directly addressable as a directory path in all
    distribution modes. Copying to the run directory guarantees an on-disk context.
    """

    try:
        ctx = importlib.resources.files("sandbox_runner")
    except Exception:
        return None

    ctx = ctx / "builtins" / "docker" / "contexts" / "sandbox_cli"
    if not ctx.is_dir():
        return None

    sandbox_dir = run_dir / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    dest = sandbox_dir / "builtin_context"
    if dest.exists():
        shutil.rmtree(dest)

    with importlib.resources.as_file(ctx) as src_dir:
        shutil.copytree(src_dir, dest)

    return dest


def prepare_execution_backend(
    *,
    repo_root: Path,
    run_dir: Path,
    workspace_dir: Path,
    request: RunRequest,
    workspace_id: str,
    agent_cfg: dict[str, Any] | None = None,
) -> ExecutionBackendContext:
    backend = str(getattr(request, "exec_backend", "local") or "local").strip().lower()
    if backend == "local":
        return ExecutionBackendContext(
            sandbox_instance=None,
            command_prefix=[],
            workspace_mount=None,
            run_dir_mount=None,
        )

    if backend != "docker":
        raise ValueError(f"Unsupported exec_backend={backend!r}")

    context_dir: Path | None = getattr(request, "exec_docker_context", None)
    if context_dir is None:
        default_context = (repo_root / _DEFAULT_DOCKER_CONTEXT_REL).resolve()
        if default_context.exists() and default_context.is_dir():
            context_dir = default_context
        else:
            copied = _copy_builtin_sandbox_cli_context_from_resources(run_dir=run_dir)
            if copied is None:
                raise ValueError(
                    "exec_backend='docker' requires exec_docker_context "
                    "(CLI: --exec-docker-context PATH).\n"
                    f"default_context_checked={default_context}\n"
                    "default_context_resource="
                    "sandbox_runner:builtins/docker/contexts/sandbox_cli (missing)"
                )
            context_dir = copied
    context_dir = context_dir.resolve()
    if not context_dir.exists() or not context_dir.is_dir():
        raise FileNotFoundError(f"Missing Docker image context directory: {context_dir}")

    docker_python_raw = getattr(request, "exec_docker_python", "auto")
    docker_python = str(docker_python_raw or "auto").strip().lower()
    if not docker_python:
        docker_python = "auto"

    # Optionally create a per-run sandbox_cli build context:
    # - inject agent-specific overlays (APT/pip/npm) from configs/agents.yaml
    # - and/or select a Python base image (auto from target requires-python, or explicit)
    context_dir = _maybe_prepare_sandbox_cli_context(
        repo_root=repo_root,
        run_dir=run_dir,
        base_context_dir=context_dir,
        agent_cfg=agent_cfg,
        target_repo_root=workspace_dir,
        docker_python=docker_python,
        use_target_sandbox_cli_install=bool(
            getattr(request, "exec_use_target_sandbox_cli_install", False)
        ),
    )

    dockerfile: Path | None = getattr(request, "exec_dockerfile", None)
    if dockerfile is not None and not dockerfile.is_absolute():
        dockerfile = Path(dockerfile)

    network = str(getattr(request, "exec_network", "open") or "open").strip().lower()
    if network not in {"open", "none"}:
        raise ValueError(f"Unsupported exec_network={network!r}")
    network_mode = cast(Literal["open", "none"], network)

    cache_mode = str(getattr(request, "exec_cache", "cold") or "cold").strip().lower()
    if cache_mode not in {"cold", "warm"}:
        raise ValueError(f"Unsupported exec_cache={cache_mode!r}")
    cache_mode_typed = cast(Literal["cold", "warm"], cache_mode)

    cache_dir: Path | None = getattr(request, "exec_cache_dir", None)
    if cache_mode == "warm" and cache_dir is None:
        cache_dir = repo_root / "runs" / "_cache" / "usertest"

    env_allowlist_raw = getattr(request, "exec_env", ())
    env_allowlist = [str(x) for x in env_allowlist_raw if isinstance(x, str) and x.strip()]

    keep_container = bool(getattr(request, "exec_keep_container", False))
    rebuild_image = bool(getattr(request, "exec_rebuild_image", False))

    sandbox_dir = run_dir / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)

    run_dir_mount = "/run_dir"
    extra_mounts = [
        MountSpec(host_path=run_dir.resolve(), container_path=run_dir_mount, read_only=False)
    ]
    if bool(getattr(request, "exec_use_host_agent_login", False)):
        extra_mounts.append(_resolve_host_agent_login_mount(agent=request.agent))
        if (request.agent or "").strip().lower() == "claude":
            host_claude_json = Path.home() / ".claude.json"
            if host_claude_json.exists() and host_claude_json.is_file():
                try:
                    host_claude_json = host_claude_json.resolve()
                except OSError:
                    pass
                extra_mounts.append(
                    MountSpec(
                        host_path=host_claude_json,
                        container_path="/root/.claude.json",
                        read_only=False,
                    )
                )

    spec = SandboxSpec(
        backend="docker",
        image_context_path=context_dir,
        dockerfile=dockerfile,
        network_mode=network_mode,
        cache_mode=cache_mode_typed,
        cache_dir=cache_dir.resolve() if cache_dir is not None else None,
        env_allowlist=env_allowlist,
        extra_mounts=extra_mounts,
        keep_container=keep_container,
        rebuild_image=rebuild_image,
        docker_timeout_seconds=getattr(request, "exec_docker_timeout_seconds", None),
    )

    container_name = f"sandbox-{workspace_id}"
    instance = DockerSandbox(
        workspace_dir=workspace_dir,
        artifacts_dir=sandbox_dir,
        spec=spec,
        container_name=container_name,
    ).start()

    return ExecutionBackendContext(
        sandbox_instance=instance,
        command_prefix=instance.command_prefix,
        workspace_mount=instance.workspace_mount,
        run_dir_mount=run_dir_mount,
    )


def _resolve_host_agent_login_mount(*, agent: str) -> MountSpec:
    """
    Build a bind mount for an agent's host login state into the Docker sandbox.

    Notes
    -----
    This is an opt-in mechanism intended to avoid passing API keys via environment variables
    for Docker runs. It reuses the login/config directories created by each agent CLI when
    running locally.
    """

    agent_norm = (agent or "").strip().lower()
    host_home = Path.home()

    if agent_norm == "codex":
        host_dir = host_home / ".codex"
        container_dir = "/root/.codex"
    elif agent_norm == "claude":
        host_dir = host_home / ".claude"
        container_dir = "/root/.claude"
    elif agent_norm == "gemini":
        host_dir = host_home / ".gemini"
        container_dir = "/root/.gemini"
    else:
        raise ValueError(
            "exec_use_host_agent_login is only supported for agents with known login dirs "
            f"(codex/claude/gemini); got agent={agent!r}."
        )

    if not host_dir.exists() or not host_dir.is_dir():
        raise FileNotFoundError(
            "Host agent login directory not found.\n"
            f"agent={agent_norm}\n"
            f"expected={host_dir}\n"
            "Fix: run the agent CLI locally once to log in (so it creates its state dir), "
            "or use --exec-use-api-key-auth and pass an API key via --exec-env."
        )

    return MountSpec(host_path=host_dir.resolve(), container_path=container_dir, read_only=False)


def _maybe_prepare_sandbox_cli_context(
    *,
    repo_root: Path,
    run_dir: Path,
    base_context_dir: Path,
    agent_cfg: dict[str, Any] | None,
    target_repo_root: Path,
    docker_python: str,
    use_target_sandbox_cli_install: bool = False,
) -> Path:
    """
    Prepare a per-run Docker image context for the `sandbox_cli`-shaped context.

    This is a best-effort mechanism to keep the checked-in docker context generic while still
    allowing per-run customization. When needed, it copies the context under `run_dir/sandbox/`
    so the checked-in context is never mutated.

    Customizations:
    - Agent overlays (APT/pip/npm) from `configs/agents.yaml -> sandbox_cli_install`
    - Optional Python base image selection (auto from target `requires-python`, or explicit)
    """

    # Only apply this mechanism to contexts that are structured like sandbox_cli.
    is_sandbox_cli = (base_context_dir / "scripts" / "install_manifests.sh").exists()
    if not is_sandbox_cli:
        if use_target_sandbox_cli_install:
            raise ValueError(
                "Target sandbox install manifests require a sandbox_cli-shaped Docker context "
                "(missing scripts/install_manifests.sh)."
            )
        return base_context_dir
    dockerfile_path = base_context_dir / "Dockerfile"
    if not dockerfile_path.exists():
        if use_target_sandbox_cli_install:
            raise ValueError(
                "Target sandbox install manifests require a sandbox_cli-shaped Docker context "
                "(missing Dockerfile)."
            )
        return base_context_dir

    agent_apt_items: list[str] = []
    agent_pip_items: list[str] = []
    agent_npm_items: list[str] = []
    if agent_cfg is not None and isinstance(agent_cfg, dict):
        install_cfg = agent_cfg.get("sandbox_cli_install")
        if isinstance(install_cfg, dict):
            agent_apt_items = _coerce_str_list(install_cfg.get("apt"))
            agent_pip_items = _coerce_str_list(install_cfg.get("pip"))
            agent_npm_items = _coerce_str_list(install_cfg.get("npm_global"))

    apt_items = list(agent_apt_items)
    pip_items = list(agent_pip_items)
    npm_items = list(agent_npm_items)

    target_manifest_path = target_repo_root / ".usertest" / "sandbox_cli_install.yaml"
    target_install: dict[str, list[str]] | None = None
    if use_target_sandbox_cli_install and target_manifest_path.exists():
        target_install = _load_target_sandbox_cli_install(target_manifest_path)
        apt_items = _merge_unique(apt_items, target_install.get("apt", []))
        pip_items = _merge_unique(pip_items, target_install.get("pip", []))
        npm_items = _merge_unique(npm_items, target_install.get("npm_global", []))

    dockerfile_base_image = _read_dockerfile_base_image(dockerfile_path)

    requires_python: str | None = None
    if docker_python == "auto":
        requires_python = _read_target_requires_python(target_repo_root)

    sandbox_dir = run_dir / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    selected_base_image: str | None = None
    selection_reason: str | None = None
    selection_payload: dict[str, Any] = {
        "mode": docker_python,
        "target_requires_python": requires_python,
        "dockerfile_base_image": dockerfile_base_image,
        "selected_base_image": None,
        "selection_reason": None,
        "candidates": list(_SANDBOX_CLI_PYTHON_VERSION_CANDIDATES),
        "error": None,
    }

    try:
        selected_base_image, selection_reason = _resolve_sandbox_cli_base_image(
            docker_python=docker_python,
            dockerfile_base_image=dockerfile_base_image,
            requires_python=requires_python,
        )
        selection_payload["selected_base_image"] = selected_base_image
        selection_payload["selection_reason"] = selection_reason
        _write_json(sandbox_dir / "python_selection.json", selection_payload)
    except Exception as e:  # noqa: BLE001
        selection_payload["error"] = str(e)
        _write_json(sandbox_dir / "python_selection.json", selection_payload)
        raise

    install_payload: dict[str, Any] = {
        "use_target_sandbox_cli_install": bool(use_target_sandbox_cli_install),
        "target_manifest_path": str(target_manifest_path),
        "target_manifest_present": target_manifest_path.exists(),
        "target_manifest": target_install,
        "agent_install": {
            "apt": agent_apt_items,
            "pip": agent_pip_items,
            "npm_global": agent_npm_items,
        },
        "merged_install": {"apt": apt_items, "pip": pip_items, "npm_global": npm_items},
        "error": None,
    }
    _write_json(sandbox_dir / "sandbox_cli_install.json", install_payload)

    needs_overlays = bool(apt_items or pip_items or npm_items)
    needs_base_override = (
        selected_base_image is not None
        and dockerfile_base_image is not None
        and selected_base_image != dockerfile_base_image
    )
    if not needs_overlays and not needs_base_override:
        return base_context_dir

    # Create a per-run build context so we never mutate the checked-in docker context.
    context_dir = sandbox_dir / "image_context"
    if context_dir.exists():
        shutil.rmtree(context_dir)
    shutil.copytree(base_context_dir, context_dir)

    if needs_overlays:
        overlays_dir = context_dir / "overlays" / "manifests"
        overlays_dir.mkdir(parents=True, exist_ok=True)

        (overlays_dir / "apt.txt").write_text(
            _render_simple_manifest(
                header="# Overlay APT packages for selected agent CLI.",
                items=apt_items,
            ),
            encoding="utf-8",
            newline="\n",
        )
        (overlays_dir / "pip.txt").write_text(
            _render_simple_manifest(
                header="# Overlay pip requirements for selected agent CLI.",
                items=pip_items,
            ),
            encoding="utf-8",
            newline="\n",
        )
        (overlays_dir / "npm-global.txt").write_text(
            _render_simple_manifest(
                header="# Overlay global npm packages for selected agent CLI.",
                items=npm_items,
            ),
            encoding="utf-8",
            newline="\n",
        )

        # Ensure the copied Dockerfile can see overlays/ (it should, but keep it explicit).
        if not (context_dir / "overlays").exists():
            (context_dir / "overlays").mkdir(parents=True, exist_ok=True)

    if needs_base_override:
        _rewrite_dockerfile_base_image(context_dir / "Dockerfile", selected_base_image)

    return context_dir


_DOCKERFILE_FROM_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)(?P<rest>.*)$", re.IGNORECASE)


def _read_dockerfile_base_image(dockerfile: Path) -> str | None:
    """
    Return the image reference from the first `FROM ...` line in a Dockerfile.
    """

    try:
        lines = dockerfile.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DOCKERFILE_FROM_RE.match(raw)
        if match:
            image = match.group("image").strip()
            return image if image else None
    return None


def _rewrite_dockerfile_base_image(dockerfile: Path, new_base_image: str) -> None:
    """
    Rewrite the first `FROM ...` line in `dockerfile` to use `new_base_image`.
    """

    text = dockerfile.read_text(encoding="utf-8")
    lines = text.splitlines()
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DOCKERFILE_FROM_RE.match(raw)
        if not match:
            continue
        prefix = raw[: match.start("image")]
        rest = match.group("rest")
        lines[idx] = f"{prefix}{new_base_image}{rest}"
        dockerfile.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")
        return
    raise ValueError(f"Could not find a FROM line in Dockerfile: {dockerfile}")


def _write_json(path: Path, payload: Any) -> None:
    """
    Write a JSON artifact with stable formatting.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_target_requires_python(target_repo_root: Path) -> str | None:
    """
    Read `project.requires-python` from the target's `pyproject.toml` (PEP 621), if present.
    """

    pyproject_path = target_repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return None

    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ValueError(f"Failed to read {pyproject_path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Failed to parse TOML in {pyproject_path}: {e}") from e

    project = data.get("project")
    if not isinstance(project, dict):
        return None
    value = project.get("requires-python")
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _resolve_sandbox_cli_base_image(
    *,
    docker_python: str,
    dockerfile_base_image: str | None,
    requires_python: str | None,
) -> tuple[str | None, str]:
    """
    Resolve which base image should be used for sandbox_cli.

    Parameters
    ----------
    docker_python:
        The user-supplied mode/value for Docker sandbox Python selection.
    dockerfile_base_image:
        Base image currently declared in the sandbox_cli Dockerfile.
    requires_python:
        The target repo's `requires-python` value (only populated in auto mode).

    Returns
    -------
    tuple[str | None, str]
        (selected_base_image, reason)
    """

    if dockerfile_base_image is None:
        return None, "could not read Dockerfile base image"

    if docker_python == "context":
        return dockerfile_base_image, "mode=context (no override)"

    if docker_python != "auto":
        resolved = _resolve_python_base_image_override(docker_python)
        return resolved, "mode=explicit"

    if requires_python is None:
        return dockerfile_base_image, "mode=auto (target requires-python not found)"

    dockerfile_python_version = _python_version_from_image(dockerfile_base_image)
    if _python_version_satisfies(requires_python, dockerfile_python_version):
        return dockerfile_base_image, "mode=auto (Dockerfile base satisfies requires-python)"

    selected_version = _select_python_version_for_requires(requires_python)
    if selected_version is None:
        candidates = ", ".join(_SANDBOX_CLI_PYTHON_VERSION_CANDIDATES)
        raise ValueError(
            "Docker sandbox python auto-selection failed.\n"
            f"requires_python={requires_python!r}\n"
            f"supported_versions=[{candidates}]\n"
            "Tip: pass --exec-docker-python <VERSION> (e.g., 3.12) or --exec-docker-python context."
        )

    return (
        _resolve_python_base_image_override(selected_version),
        "mode=auto (override to satisfy target requires-python)",
    )


def _resolve_python_base_image_override(value: str) -> str:
    """
    Convert a user-supplied python selector to a Docker image reference.

    The input may be:
    - a full image reference (contains ':' or '/'), returned as-is
    - a bare version like '3.12' / '3.12.8' -> 'python:<version>-slim'
    - a python tag suffix like '3.12-slim-bookworm' -> 'python:<value>'
    """

    raw = value.strip()
    if not raw:
        raise ValueError("exec_docker_python must be non-empty")
    if ":" in raw or "/" in raw:
        return raw
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", raw):
        return f"python:{raw}-slim"
    return f"python:{raw}"


def _python_version_from_image(image: str) -> str:
    """
    Extract a Python version string (e.g. '3.12' or '3.12.8') from a docker image tag.
    """

    tag = image.rsplit(":", maxsplit=1)[-1]
    version = tag.split("-", maxsplit=1)[0]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", version):
        raise ValueError(f"Unsupported python base image (cannot parse version): {image!r}")
    return version


_SPEC_RE = re.compile(r"^(>=|<=|==|!=|>|<|~=)\s*([0-9]+(?:\.[0-9]+){0,2}(?:\.\*)?)\s*$")


def _python_version_satisfies(requires_python: str, version: str) -> bool:
    """
    Check whether a Python version string satisfies a (common) requires-python constraint.

    Notes
    -----
    This is a small, dependency-free subset of PEP 440 that supports the forms typically seen
    in `project.requires-python`:
    - comma-separated specifiers (e.g. '>=3.11,<4')
    - wildcards for equality/inequality (e.g. '!=3.11.*')
    - compatible release operator ('~=3.11' / '~=3.11.2')
    """

    candidate = _parse_version(version, patch_default=9999)
    expanded = _expand_compatible_release(requires_python)
    for spec in _split_specifiers(expanded):
        if not _satisfies_specifier(candidate, spec):
            return False
    return True


def _select_python_version_for_requires(requires_python: str) -> str | None:
    """
    Select the lowest supported Python X.Y version that satisfies `requires_python`.
    """

    for candidate in _SANDBOX_CLI_PYTHON_VERSION_CANDIDATES:
        if _python_version_satisfies(requires_python, candidate):
            return candidate
    return None


def _split_specifiers(text: str) -> list[str]:
    specs = [s.strip() for s in text.split(",")]
    return [s for s in specs if s]


def _expand_compatible_release(text: str) -> str:
    """
    Expand '~=' specifiers into equivalent lower/upper bounds.
    """

    specs = _split_specifiers(text)
    expanded: list[str] = []
    for spec in specs:
        match = _SPEC_RE.match(spec)
        if not match or match.group(1) != "~=":
            expanded.append(spec)
            continue
        raw_version = match.group(2)
        version_no_wildcard = raw_version.replace(".*", "")
        parts = version_no_wildcard.split(".") if version_no_wildcard else []
        lower = version_no_wildcard
        if len(parts) <= 2:
            major = int(parts[0]) if parts else 0
            upper = f"{major + 1}.0"
        else:
            major = int(parts[0])
            minor = int(parts[1])
            upper = f"{major}.{minor + 1}.0"
        expanded.append(f">={lower}")
        expanded.append(f"<{upper}")
    return ",".join(expanded)


def _parse_version(text: str, *, patch_default: int) -> tuple[int, int, int]:
    parts = [p for p in text.split(".") if p]
    if not parts or any(not p.isdigit() for p in parts):
        raise ValueError(f"Invalid version: {text!r}")

    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 else 0
    if len(parts) > 2:
        patch = int(parts[2])
    else:
        patch = patch_default
    return major, minor, patch


def _satisfies_specifier(candidate: tuple[int, int, int], spec: str) -> bool:
    match = _SPEC_RE.match(spec)
    if not match:
        raise ValueError(f"Unsupported requires-python fragment: {spec!r}")

    op = match.group(1)
    raw_version = match.group(2)
    wildcard = raw_version.endswith(".*")
    version_text = raw_version[:-2] if wildcard else raw_version

    if wildcard:
        prefix_parts = [p for p in version_text.split(".") if p]
        prefix = tuple(int(p) for p in prefix_parts)
        candidate_prefix = candidate[: len(prefix)]
        if op == "==":
            return candidate_prefix == prefix
        if op == "!=":
            return candidate_prefix != prefix
        raise ValueError(f"Unsupported wildcard operator in requires-python: {spec!r}")

    parsed = _parse_version(version_text, patch_default=0)

    if op == "==":
        if version_text.count(".") == 0:
            return candidate[0] == parsed[0]
        if version_text.count(".") == 1:
            return candidate[:2] == parsed[:2]
        return candidate == parsed
    if op == "!=":
        if version_text.count(".") == 0:
            return candidate[0] != parsed[0]
        if version_text.count(".") == 1:
            return candidate[:2] != parsed[:2]
        return candidate != parsed
    if op == ">=":
        return candidate >= parsed
    if op == ">":
        return candidate > parsed
    if op == "<=":
        return candidate <= parsed
    if op == "<":
        return candidate < parsed
    raise ValueError(f"Unsupported operator in requires-python: {spec!r}")


def _coerce_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned:
            continue
        if "\n" in cleaned or "\r" in cleaned:
            continue
        out.append(cleaned)
    return out


def _merge_unique(existing: list[str], extra: list[str]) -> list[str]:
    """
    Merge two string lists while preserving order and removing duplicates.

    Parameters
    ----------
    existing
        Existing items in their original order.
    extra
        Additional items to append (deduplicated).

    Returns
    -------
    list[str]
        The merged list.
    """

    merged: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *extra]:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        merged.append(cleaned)
        seen.add(cleaned)
    return merged


def _load_target_sandbox_cli_install(path: Path) -> dict[str, list[str]]:
    """
    Load a target-repo sandbox_cli install manifest.

    The manifest is a YAML file at `.usertest/sandbox_cli_install.yaml` that allows a target repo
    to declare system/tooling dependencies needed for sandboxed runs.

    Expected schema (version 1)
    ---------------------------
    version: 1
    sandbox_cli_install:
      apt: [ ... ]
      pip: [ ... ]
      npm_global: [ ... ]
    """

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ValueError(
            f"Failed to read target sandbox install manifest {path}: {e}"
        ) from e
    except yaml.YAMLError as e:
        raise ValueError(
            f"Failed to parse YAML in target sandbox install manifest {path}: {e}"
        ) from e

    if not isinstance(raw, dict):
        raise ValueError(
            f"Expected YAML mapping in target sandbox install manifest {path}, "
            f"got {type(raw).__name__}."
        )

    version = raw.get("version")
    if version != 1:
        raise ValueError(
            f"Unsupported target sandbox install manifest version in {path}: {version!r} "
            "(expected 1)."
        )

    install = raw.get("sandbox_cli_install")
    if not isinstance(install, dict):
        raise ValueError(
            "Missing or invalid sandbox_cli_install mapping in target sandbox install manifest "
            f"{path}."
        )

    allowed = {"apt", "pip", "npm_global", "meta"}
    unknown = set(install) - allowed
    if unknown:
        unknown_list = ", ".join(sorted(str(k) for k in unknown))
        allowed_list = ", ".join(sorted(allowed))
        raise ValueError(
            f"Unknown keys in sandbox_cli_install for {path}: {unknown_list}. "
            f"Allowed: {allowed_list}."
        )

    return {
        "apt": _require_str_list(install.get("apt"), path=path, field="sandbox_cli_install.apt"),
        "pip": _require_str_list(install.get("pip"), path=path, field="sandbox_cli_install.pip"),
        "npm_global": _require_str_list(
            install.get("npm_global"), path=path, field="sandbox_cli_install.npm_global"
        ),
    }


def _require_str_list(value: object, *, path: Path, field: str) -> list[str]:
    """
    Validate and normalize a YAML list-of-strings field.

    Parameters
    ----------
    value
        Raw YAML value (typically from `yaml.safe_load`).
    path
        Manifest path (used for error messages).
    field
        Dotted field name within the manifest (used for error messages).

    Returns
    -------
    list[str]
        A list of stripped strings preserving order.

    Raises
    ------
    ValueError
        If `value` is not a list, contains non-strings, empty strings, or values
        containing newlines.
    """

    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Expected list for {field} in {path}.")
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"Expected string for {field}[{idx}] in {path}, "
                f"got {type(item).__name__}."
            )
        cleaned = item.strip()
        if not cleaned:
            raise ValueError(f"Expected non-empty string for {field}[{idx}] in {path}.")
        if "\n" in cleaned or "\r" in cleaned:
            raise ValueError(f"Newlines are not allowed in {field}[{idx}] in {path}.")
        out.append(cleaned)
    return out


def _render_simple_manifest(*, header: str, items: list[str]) -> str:
    """
    Render a plain-text manifest file consumed by `scripts/install_manifests.sh`.

    Parameters
    ----------
    header
        The leading comment line for the manifest.
    items
        The items to list in the manifest (one per line).

    Returns
    -------
    str
        Manifest contents.
    """

    lines = [
        header,
        "#",
        "# Generated per-run from agent + (optional) target sandbox_cli_install manifests.",
        "",
    ]
    if items:
        lines.extend(items)
        lines.append("")
    return "\n".join(lines)
