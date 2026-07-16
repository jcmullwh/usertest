from __future__ import annotations

import importlib.resources
import json
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml
from sandbox_runner import DockerSandbox, MountSpec, SandboxInstance, SandboxSpec
from sandbox_runner.image_hash import compute_image_hash

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
_DEFAULT_MAINTENANCE_DOCKER_CONFIG_REL = Path("configs/maintenance_docker.yaml")
_INSTALL_CACHE_FINGERPRINT_SCRIPT_REL = Path("tools/scaffold/install_cache_fingerprint.py")
_MAINTENANCE_CONTEXT_PREPARE_SCRIPT_REL = Path("tools/maintenance_image/prepare_context.py")
_DEFAULT_MAINTENANCE_CACHE_ROOT_SUBDIR = "usertest_maint_venvs"


def _safe_cache_project_id(project_id: str) -> str:
    # Keep this local so runner_core does not depend on the repo-only tools/ tree at import time.
    cleaned = "".join(
        ch if ch.isalnum() or ch in {"_", ".", "-"} else "-" for ch in (project_id or "")
    ).strip("-.")
    return cleaned or "project"


def _prepare_per_worker_venv_cache_copy(
    *,
    run_dir: Path,
    project_id: str,
    fingerprint: str,
    source_venv_dir: Path,
) -> Path:
    """Return a writable per-run copy of a shared maintenance venv cache hit.

    Maintenance cache entries live under the warm Docker cache directory and may be reused by
    multiple ticket workers. Mounting those entries directly as a writable project ``.venv`` lets
    concurrent containers mutate the same host directory. Instead, each container gets a copy
    scoped to its run artifacts directory, while scaffold's install-cache code remains pointed at
    the shared cache root for locked save/update operations.
    """

    safe_project_id = _safe_cache_project_id(project_id)
    copy_root = run_dir / "sandbox" / "maintenance_venv_copies"
    copy_venv_dir = copy_root / safe_project_id / fingerprint / "venv"
    if copy_venv_dir.exists():
        shutil.rmtree(copy_venv_dir, ignore_errors=True)
    copy_venv_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_venv_dir, copy_venv_dir, symlinks=True)
    return copy_venv_dir.resolve()


@dataclass(frozen=True)
class MaintenanceDockerConfig:
    local_image_repo: str
    published_image_repo: str
    pull_policy: Literal["if_missing", "always", "never"]
    seed_root: str
    cache_root_subdir: str
    publish_branches: tuple[str, ...]
    cleanup_enabled: bool = True
    keep_local_count: int = 2
    keep_local_days: int = 7
    keep_branch_alias_tags: bool = True
    protect_tags: tuple[str, ...] = ()
    cleanup_on_prepare: bool = True
    cleanup_dry_run_default: bool = False


@dataclass(frozen=True)
class MaintenanceProfilePreparation:
    image_ref: str
    env_hash: str
    image_source: str
    image_resolution_seconds: float
    fingerprint_seconds: float
    cache_mount_hits: int
    cache_mounts: list[MountSpec]
    env_overrides: dict[str, str]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MaintenanceImageResolution:
    image_ref: str
    env_hash: str
    image_source: Literal["local", "pulled", "built"]
    image_resolution_seconds: float
    context_metadata: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MaintenanceImagePreparation:
    """Prepared, fingerprinted maintenance image inputs reusable by preflight."""

    context_dir: Path
    context_metadata: dict[str, Any]
    env_hash: str
    local_ref: str
    published_ref: str


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_sandbox_env_overrides(
    *,
    cache_mode: Literal["cold", "warm"],
    maintenance_venv_cache_enabled: bool,
    maintenance_venv_cache_root: str = f"/cache/{_DEFAULT_MAINTENANCE_CACHE_ROOT_SUBDIR}",
    maintenance_venv_seed_root: str | None = None,
) -> dict[str, str]:
    """
    Ensure common tooling (pip/pytest/build backends) uses a writable temp root inside the sandbox.

    This is especially important for editable installs (`pip install -e ...`) where pip's temp
    build-tracker directories can fail if TMP/TMPDIR resolve to a read-only path in a sandboxed
    filesystem.
    """

    pip_cache_dir = "/cache/pip" if cache_mode == "warm" else "/tmp/usertest-pip-cache"
    env = {
        "TMPDIR": "/tmp",
        "TMP": "/tmp",
        "TEMP": "/tmp",
        "PIP_CACHE_DIR": pip_cache_dir,
    }
    if maintenance_venv_cache_enabled:
        env["USERTEST_MAINT_VENV_CACHE_ENABLED"] = "1"
        env["USERTEST_MAINT_VENV_CACHE_ROOT"] = maintenance_venv_cache_root
    else:
        env["USERTEST_MAINT_VENV_CACHE_ENABLED"] = "0"
    if maintenance_venv_seed_root is not None and maintenance_venv_seed_root.strip():
        env["USERTEST_MAINT_VENV_SEED_ROOT"] = maintenance_venv_seed_root.strip()
    return env


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


def _normalize_exec_docker_profile(value: object) -> Literal["standard", "maintenance"]:
    raw = str(value or "standard").strip().lower()
    if raw not in {"standard", "maintenance"}:
        raise ValueError(f"Unsupported exec_docker_profile={value!r}")
    return cast(Literal["standard", "maintenance"], raw)


def _load_maintenance_docker_config(*, repo_root: Path) -> MaintenanceDockerConfig:
    path = (repo_root / _DEFAULT_MAINTENANCE_DOCKER_CONFIG_REL).resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise FileNotFoundError(f"Missing maintenance Docker config: {path}") from e
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse maintenance Docker config {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"Expected YAML mapping in maintenance Docker config: {path}")
    if raw.get("version") != 1:
        raise ValueError(
            f"Unsupported maintenance Docker config version in {path}: {raw.get('version')!r}"
        )
    cfg = raw.get("maintenance_docker")
    if not isinstance(cfg, dict):
        raise ValueError(f"Missing maintenance_docker mapping in {path}")

    def _require_nonempty_str(key: str) -> str:
        value = cfg.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"maintenance_docker.{key} must be a non-empty string in {path}")
        return value.strip()

    def _get_bool(key: str, default: bool) -> bool:
        value = cfg.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"maintenance_docker.{key} must be a boolean in {path}")
        return value

    def _get_nonnegative_int(key: str, default: int) -> int:
        value = cfg.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"maintenance_docker.{key} must be a non-negative integer in {path}")
        return value

    pull_policy = _require_nonempty_str("pull_policy").lower()
    if pull_policy not in {"if_missing", "always", "never"}:
        raise ValueError(
            f"maintenance_docker.pull_policy must be one of if_missing|always|never in {path}"
        )
    publish_branches_raw = cfg.get("publish_branches")
    if not isinstance(publish_branches_raw, list) or not all(
        isinstance(item, str) and item.strip() for item in publish_branches_raw
    ):
        raise ValueError(
            f"maintenance_docker.publish_branches must be a list of non-empty strings in {path}"
        )
    protect_tags_raw = cfg.get("protect_tags", [])
    if not isinstance(protect_tags_raw, list) or not all(
        isinstance(item, str) and item.strip() for item in protect_tags_raw
    ):
        raise ValueError(
            f"maintenance_docker.protect_tags must be a list of non-empty strings in {path}"
        )

    return MaintenanceDockerConfig(
        local_image_repo=_require_nonempty_str("local_image_repo"),
        published_image_repo=_require_nonempty_str("published_image_repo"),
        pull_policy=cast(Literal["if_missing", "always", "never"], pull_policy),
        seed_root=_require_nonempty_str("seed_root"),
        cache_root_subdir=_require_nonempty_str("cache_root_subdir"),
        publish_branches=tuple(item.strip() for item in publish_branches_raw),
        cleanup_enabled=_get_bool("cleanup_enabled", True),
        keep_local_count=_get_nonnegative_int("keep_local_count", 2),
        keep_local_days=_get_nonnegative_int("keep_local_days", 7),
        keep_branch_alias_tags=_get_bool("keep_branch_alias_tags", True),
        protect_tags=tuple(item.strip() for item in protect_tags_raw),
        cleanup_on_prepare=_get_bool("cleanup_on_prepare", True),
        cleanup_dry_run_default=_get_bool("cleanup_dry_run_default", False),
    )


def _maintenance_repo_names(*, cfg: MaintenanceDockerConfig) -> tuple[str, ...]:
    """Return the Docker repositories managed by the maintenance image workflow."""

    return (cfg.local_image_repo, cfg.published_image_repo)


def _maintenance_protected_tags(*, cfg: MaintenanceDockerConfig) -> tuple[str, ...]:
    """Return tag names that local cleanup must preserve."""

    tags: list[str] = list(cfg.protect_tags)
    if cfg.keep_branch_alias_tags:
        tags.extend(["latest", "main-latest", "dev-latest"])
    ordered: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        normalized = str(tag).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)


def _maintenance_hash_tag(tag: str) -> bool:
    """Return True when the tag matches the current immutable maintenance-image hash suffix."""

    return bool(re.fullmatch(r"[0-9a-f]{16}", tag or ""))


def _run_subprocess(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=capture_output,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"Command not found: {argv[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Command timed out after {timeout_seconds}s: {' '.join(argv)}") from e


def _docker_image_exists_local(*, ref: str, timeout_seconds: float | None) -> bool:
    proc = _run_subprocess(
        ["docker", "image", "inspect", ref],
        timeout_seconds=timeout_seconds,
    )
    return proc.returncode == 0


def _docker_pull_image(
    *, ref: str, timeout_seconds: float | None, log_path: Path
) -> subprocess.CompletedProcess[str]:
    proc = _run_subprocess(
        ["docker", "pull", ref],
        timeout_seconds=timeout_seconds,
    )
    log_path.write_text(
        json.dumps(
            {
                "argv": ["docker", "pull", ref],
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return proc


def _docker_pull_images(
    *, refs: Sequence[str], timeout_seconds: float | None, log_path: Path
) -> list[dict[str, Any]]:
    """Pull candidate image refs and persist compact per-ref results."""

    results: list[dict[str, Any]] = []
    for ref in refs:
        proc = _run_subprocess(
            ["docker", "pull", ref],
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "argv": ["docker", "pull", ref],
                "ref": ref,
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        )
    log_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return results


def _docker_tag_image(*, source_ref: str, target_ref: str, timeout_seconds: float | None) -> None:
    proc = _run_subprocess(
        ["docker", "tag", source_ref, target_ref],
        timeout_seconds=timeout_seconds,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to tag Docker image.\n"
            f"source={source_ref}\n"
            f"target={target_ref}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}\n"
        )


def _docker_image_ls_rows(*, timeout_seconds: float | None) -> list[dict[str, Any]]:
    """List local Docker images as parsed JSON rows."""

    proc = _run_subprocess(
        ["docker", "image", "ls", "--format", "{{json .}}"],
        timeout_seconds=timeout_seconds,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to list Docker images.\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}\n"
        )
    rows: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            rows.append({str(key): value for key, value in parsed.items()})
    return rows


def _docker_image_inspect_rows(
    refs_or_ids: list[str],
    *,
    timeout_seconds: float | None,
    allow_single_not_found: bool = False,
) -> list[dict[str, Any]]:
    """Inspect Docker image refs/IDs and return parsed JSON objects."""

    if not refs_or_ids:
        return []
    proc = _run_subprocess(
        ["docker", "image", "inspect", *refs_or_ids],
        timeout_seconds=timeout_seconds,
    )
    if (
        proc.returncode != 0
        and allow_single_not_found
        and len(refs_or_ids) == 1
        and _docker_image_inspect_is_not_found(proc=proc, ref_or_id=refs_or_ids[0])
    ):
        return []
    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to inspect Docker images.\n"
            f"refs_or_ids={refs_or_ids!r}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}\n"
        )
    parsed = json.loads(proc.stdout or "[]")
    if not isinstance(parsed, list):
        raise RuntimeError("docker image inspect returned invalid JSON.")
    return [cast(dict[str, Any], item) for item in parsed if isinstance(item, dict)]


def _docker_image_inspect_is_not_found(*, proc: Any, ref_or_id: str) -> bool:
    """Return whether Docker reported the single requested image as absent."""

    output = f"{proc.stdout}\n{proc.stderr}".casefold()
    return ref_or_id.casefold() in output and bool(
        re.search(r"\b(?:no such image|image not known)\b", output)
    )


def _coerce_iso8601_utc(value: object) -> str | None:
    """Normalize a Docker timestamp into an ISO-8601 UTC string."""

    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    match = re.match(r"^(.*?\.\d{6})\d+(Z|[+-]\d{2}:\d{2})$", raw)
    if match:
        raw = f"{match.group(1)}{match.group(2)}"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_created_at_for_sort(value: object) -> datetime:
    """Convert a serialized created_at value into a sortable UTC datetime."""

    normalized = _coerce_iso8601_utc(value)
    if normalized is None:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def _normalize_maintenance_inventory_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Fill derived maintenance-inventory fields expected by cleanup logic."""

    repository = str(entry.get("repository", "") or "").strip()
    tag = str(entry.get("tag", "") or "").strip()
    ref = str(entry.get("ref", "") or "").strip()
    if not ref and repository and tag:
        ref = f"{repository}:{tag}"
    return {
        **entry,
        "repository": repository,
        "tag": tag,
        "ref": ref,
        "image_id": str(entry.get("image_id", "") or "").strip(),
        "created_at": _coerce_iso8601_utc(entry.get("created_at")) or entry.get("created_at"),
        "hash_tag": bool(entry.get("hash_tag")) or _maintenance_hash_tag(tag),
    }


def list_local_maintenance_images(
    *,
    repo_root: Path,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Return a structured inventory of local maintenance images and tags."""

    cfg = _load_maintenance_docker_config(repo_root=repo_root)
    repos_scanned = list(_maintenance_repo_names(cfg=cfg))
    protected_tags = list(_maintenance_protected_tags(cfg=cfg))
    ls_rows = _docker_image_ls_rows(timeout_seconds=timeout_seconds)
    refs: list[str] = []
    base_entries: list[dict[str, Any]] = []
    for row in ls_rows:
        repository = str(row.get("repository", row.get("Repository", "")) or "").strip()
        tag = str(row.get("tag", row.get("Tag", "")) or "").strip()
        image_id = str(row.get("image_id", row.get("ID", row.get("Id", ""))) or "").strip()
        if repository not in repos_scanned or not tag or tag == "<none>":
            continue
        ref = f"{repository}:{tag}"
        refs.append(ref)
        base_entries.append(
            {
                "repository": repository,
                "tag": tag,
                "ref": ref,
                "image_id": image_id,
            }
        )

    inspect_rows = _docker_image_inspect_rows(refs, timeout_seconds=timeout_seconds)
    inspect_by_ref: dict[str, dict[str, Any]] = {}
    inspect_by_id: dict[str, dict[str, Any]] = {}
    for row in inspect_rows:
        image_id = str(row.get("Id", "") or "").strip()
        if image_id:
            inspect_by_id[image_id] = row
        repo_tags = row.get("RepoTags") or []
        if isinstance(repo_tags, list):
            for tag in repo_tags:
                if isinstance(tag, str) and tag.strip():
                    inspect_by_ref[tag.strip()] = row

    entries: list[dict[str, Any]] = []
    for entry in base_entries:
        inspect = inspect_by_ref.get(entry["ref"]) or inspect_by_id.get(entry["image_id"]) or {}
        created_at = _coerce_iso8601_utc(inspect.get("Created"))
        entries.append(
            {
                **entry,
                "created_at": created_at,
                "protected": entry["tag"] in protected_tags,
                "hash_tag": _maintenance_hash_tag(entry["tag"]),
            }
        )

    entries.sort(
        key=lambda item: _parse_created_at_for_sort(item.get("created_at")),
        reverse=True,
    )
    return {
        "schema_version": 1,
        "repos_scanned": repos_scanned,
        "protected_tags": protected_tags,
        "entries": entries,
    }


def cleanup_local_maintenance_images(
    *,
    repo_root: Path,
    dry_run: bool = False,
    timeout_seconds: float | None = None,
    artifact_path: Path | None = None,
    protected_refs: Collection[str] = (),
) -> dict[str, Any]:
    """Prune local maintenance-image identities according to the retention settings.

    ``keep_local_count`` is an identity budget, not a tag budget.  A built image is
    normally tagged in both the local and published repositories, so selecting tags
    independently can retain twice as many tags and, more importantly, allow the
    age window to retain an unbounded number of image layers.  Select identities
    first, then keep or delete all of each selected identity's managed aliases.

    Callers that are about to use an image may pass its references in
    ``protected_refs``.  This keeps an existing current image available while
    cleanup runs before a pull, tag, or build writes to Docker storage.
    """

    cfg = _load_maintenance_docker_config(repo_root=repo_root)
    inventory = list_local_maintenance_images(
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
    )
    protected_tags = set(cast(list[str], inventory.get("protected_tags", [])))
    protected_refs_set = {str(ref).strip() for ref in protected_refs if str(ref).strip()}
    entries = [
        _normalize_maintenance_inventory_entry(cast(dict[str, Any], item))
        for item in inventory.get("entries", [])
        if isinstance(item, dict)
    ]
    identities: dict[str, list[dict[str, Any]]] = {}
    identity_order: dict[str, int] = {}
    for index, entry in enumerate(entries):
        image_id = str(entry.get("image_id") or "")
        ref = str(entry.get("ref") or "")
        # Docker normally supplies an ID.  Treat an anomalous unlabelled entry as
        # its own identity so it cannot bypass the configured retention budget.
        identity = image_id or f"ref:{ref}"
        identities.setdefault(identity, []).append(entry)
        identity_order.setdefault(identity, index)

    protected_identity_keys: set[str] = set()
    ordinary_identity_keys: list[str] = []
    for identity, identity_entries in identities.items():
        protected = any(
            str(entry.get("tag") or "") in protected_tags
            or str(entry.get("ref") or "") in protected_refs_set
            # A non-hash alias (for example, ``main-latest``) keeps the image
            # addressable, so it must protect the complete identity.
            or not bool(entry.get("hash_tag"))
            for entry in identity_entries
        )
        if protected:
            protected_identity_keys.add(identity)
        elif any(bool(entry.get("hash_tag")) for entry in identity_entries):
            ordinary_identity_keys.append(identity)

    def _identity_sort_key(identity: str) -> tuple[datetime, int]:
        newest = max(
            (_parse_created_at_for_sort(entry.get("created_at")) for entry in identities[identity]),
            default=datetime.fromtimestamp(0, tz=timezone.utc),
        )
        # The negated timestamp gives newest first while retaining inventory order
        # as a deterministic tie breaker.
        return (newest, -identity_order[identity])

    ordinary_identity_keys.sort(key=_identity_sort_key, reverse=True)
    kept_identity_keys = protected_identity_keys | set(
        ordinary_identity_keys[: cfg.keep_local_count]
    )

    kept_tags: list[str] = []
    deleted_tags: list[str] = []
    projected_deleted_tags: list[str] = []
    attempted_deleted_tags: list[str] = []
    deleted_image_ids: list[str] = []
    errors: list[str] = []
    deleted_candidate_ids: set[str] = set()
    candidate_image_ids = sorted(
        {
            str(entry.get("image_id") or "")
            for entry in entries
            if (
                (str(entry.get("image_id") or "") or f"ref:{entry.get('ref') or ''}")
                not in kept_identity_keys
                and bool(entry.get("hash_tag"))
                and str(entry.get("image_id") or "")
            )
        }
    )

    for entry in entries:
        ref = str(entry.get("ref") or "")
        tag = str(entry.get("tag") or "")
        image_id = str(entry.get("image_id") or "")
        identity = image_id or f"ref:{ref}"
        is_hash = bool(entry.get("hash_tag"))
        if identity in kept_identity_keys or tag in protected_tags or not is_hash:
            if ref:
                kept_tags.append(ref)
            continue
        projected_deleted_tags.append(ref)
        if dry_run:
            continue
        attempted_deleted_tags.append(ref)
        proc = _run_subprocess(
            ["docker", "image", "rm", ref],
            timeout_seconds=timeout_seconds,
        )
        if proc.returncode != 0:
            errors.append(
                f"Failed to delete maintenance image tag {ref}: "
                f"{proc.stderr.strip() or proc.stdout.strip() or 'docker image rm failed'}"
            )
            continue
        deleted_tags.append(ref)
        if image_id:
            deleted_candidate_ids.add(image_id)

    if not dry_run:
        for image_id in sorted(deleted_candidate_ids):
            try:
                inspect_rows = _docker_image_inspect_rows(
                    [image_id],
                    timeout_seconds=timeout_seconds,
                    allow_single_not_found=True,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Failed to inspect maintenance image id {image_id}: {exc}")
                continue
            if not inspect_rows:
                continue
            repo_tags = inspect_rows[0].get("RepoTags") or []
            if repo_tags:
                continue
            proc = _run_subprocess(
                ["docker", "image", "rm", image_id],
                timeout_seconds=timeout_seconds,
            )
            if proc.returncode != 0:
                errors.append(
                    f"Failed to delete unreferenced maintenance image id {image_id}: "
                    f"{proc.stderr.strip() or proc.stdout.strip() or 'docker image rm failed'}"
                )
                continue
            deleted_image_ids.append(image_id)

    reclaimed_image_ids: list[str] = []
    retained_candidate_image_ids: list[str] = []
    externally_retained_image_ids: list[str] = []
    externally_retained_refs: dict[str, list[str]] = {}
    physical_identity_bounded: bool | None = None
    physical_state_errors: list[str] = []
    physical_candidate_status: dict[str, dict[str, Any]] = {}
    if not dry_run:
        managed_ref_prefixes = tuple(f"{repo}:" for repo in _maintenance_repo_names(cfg=cfg))
        for image_id in candidate_image_ids:
            try:
                inspect_rows = _docker_image_inspect_rows(
                    [image_id],
                    timeout_seconds=timeout_seconds,
                    allow_single_not_found=True,
                )
            except Exception as exc:  # noqa: BLE001
                message = (
                    f"Failed to determine physical maintenance image state for {image_id}: {exc}"
                )
                physical_state_errors.append(message)
                errors.append(message)
                physical_candidate_status[image_id] = {"exists": None, "error": message}
                continue
            if not inspect_rows:
                # Docker removes the image implicitly when the final managed tag is
                # removed.  This is physical reclamation evidence even without a
                # separate image-id remove command.
                reclaimed_image_ids.append(image_id)
                physical_candidate_status[image_id] = {"exists": False, "external_refs": []}
                continue
            retained_candidate_image_ids.append(image_id)
            repo_tags = inspect_rows[0].get("RepoTags") or []
            external_refs = sorted(
                tag.strip()
                for tag in repo_tags
                if isinstance(tag, str)
                and tag.strip()
                and not tag.strip().startswith(managed_ref_prefixes)
            )
            if external_refs:
                externally_retained_image_ids.append(image_id)
                externally_retained_refs[image_id] = external_refs
            physical_candidate_status[image_id] = {
                "exists": True,
                "external_refs": external_refs,
            }
        if not physical_state_errors:
            physical_identity_bounded = not retained_candidate_image_ids

    def _inventory_state(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = [
            _normalize_maintenance_inventory_entry(cast(dict[str, Any], item))
            for item in payload.get("entries", [])
            if isinstance(item, dict)
        ]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in normalized:
            ref = str(entry.get("ref") or "")
            identity = str(entry.get("image_id") or "") or f"ref:{ref}"
            grouped.setdefault(identity, []).append(entry)
        protected_ids: set[str] = set()
        ordinary_ids: set[str] = set()
        aliases: dict[str, list[str]] = {}
        for identity, grouped_entries in grouped.items():
            aliases[identity] = sorted(
                str(entry.get("ref") or "") for entry in grouped_entries if entry.get("ref")
            )
            protected = any(
                str(entry.get("tag") or "") in protected_tags
                or str(entry.get("ref") or "") in protected_refs_set
                or not bool(entry.get("hash_tag"))
                for entry in grouped_entries
            )
            if protected:
                protected_ids.add(identity)
            elif any(bool(entry.get("hash_tag")) for entry in grouped_entries):
                ordinary_ids.add(identity)
        return {
            "image_ids": sorted(
                identity for identity in grouped if not identity.startswith("ref:")
            ),
            "ordinary_image_ids": sorted(
                identity for identity in ordinary_ids if not identity.startswith("ref:")
            ),
            "protected_image_ids": sorted(
                identity for identity in protected_ids if not identity.startswith("ref:")
            ),
            "aliases": aliases,
        }

    before_state = _inventory_state(inventory)
    after_inventory: dict[str, Any] | None = None
    after_state = before_state
    if not dry_run:
        try:
            after_inventory = list_local_maintenance_images(
                repo_root=repo_root,
                timeout_seconds=timeout_seconds,
            )
            after_state = _inventory_state(after_inventory)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Failed to re-list maintenance images after cleanup: {exc}")

    summary = {
        "schema_version": 1,
        "cleanup_enabled": bool(cfg.cleanup_enabled),
        "dry_run": bool(dry_run),
        "repos_scanned": inventory.get("repos_scanned", []),
        "protected_tags": sorted(protected_tags),
        "protected_refs": sorted(protected_refs_set),
        "keep_local_count": cfg.keep_local_count,
        "kept_image_ids": sorted(
            identity for identity in kept_identity_keys if not identity.startswith("ref:")
        ),
        "kept_tags": sorted(set(kept_tags)),
        "deleted_tags": sorted(set(deleted_tags)),
        "attempted_deleted_tags": sorted(set(attempted_deleted_tags)),
        "projected_deleted_tags": sorted(set(projected_deleted_tags)),
        "deleted_image_ids": deleted_image_ids,
        "candidate_image_ids": candidate_image_ids,
        "reclaimed_image_ids": sorted(reclaimed_image_ids),
        "retained_candidate_image_ids": sorted(retained_candidate_image_ids),
        "externally_retained_image_ids": sorted(externally_retained_image_ids),
        "externally_retained_refs": externally_retained_refs,
        "physical_identity_bounded": physical_identity_bounded,
        "physical_candidate_status": physical_candidate_status,
        "physical_state_errors": physical_state_errors,
        "physical_reclamation_evidence": "image_id_existence_only",
        "byte_reclamation": None,
        "errors": errors,
        "before_inventory": inventory,
        "before_state": before_state,
        "after_inventory": after_inventory,
        "after_state": after_state if not dry_run else None,
        "remaining_ordinary_image_ids": after_state["ordinary_image_ids"] if not dry_run else [],
        "remaining_protected_image_ids": after_state["protected_image_ids"] if not dry_run else [],
        "remaining_aliases": after_state["aliases"] if not dry_run else {},
        "managed_tag_bounded": (
            len(cast(list[str], after_state["ordinary_image_ids"])) <= cfg.keep_local_count
            if not dry_run and after_inventory is not None
            else None
        ),
        # Preserve the historical field while making its claim conservative: an
        # overflow candidate that remains due to an external tag is not bounded
        # physically even when the managed-tag inventory is within budget.
        "bounded": physical_identity_bounded,
        "state_kind": "projected" if dry_run else "actual",
    }
    if artifact_path is not None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(artifact_path, summary)
    return summary


def _git_remote_url(*, repo_dir: Path, remote_name: str = "origin") -> str | None:
    proc = _run_subprocess(
        ["git", "-C", str(repo_dir), "remote", "get-url", remote_name.strip() or "origin"],
    )
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out if out else None


def _git_current_branch(*, repo_dir: Path) -> str | None:
    proc = _run_subprocess(
        ["git", "-C", str(repo_dir), "branch", "--show-current"],
    )
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out if out else None


def _maintenance_branch_cache_refs(
    *,
    cfg: MaintenanceDockerConfig,
    repo_root: Path,
) -> list[str]:
    branch = _git_current_branch(repo_dir=repo_root)
    if branch is None or branch not in set(cfg.publish_branches):
        return []
    tags = [f"{branch}-latest"]
    if branch == "main":
        tags.append("latest")
    refs: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        ref = f"{cfg.published_image_repo}:{tag}"
        if ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
    return refs


def _read_json_artifact(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return cast(dict[str, Any], raw)


def _prepare_maintenance_image_context(
    *,
    repo_root: Path,
    run_dir: Path,
    timeout_seconds: float | None,
) -> tuple[Path, dict[str, Any]]:
    script_path = (repo_root / _MAINTENANCE_CONTEXT_PREPARE_SCRIPT_REL).resolve()
    if not script_path.exists():
        raise FileNotFoundError(f"Missing maintenance context preparation script: {script_path}")
    sandbox_dir = run_dir / "sandbox"
    context_dir = sandbox_dir / "maintenance_image_context"
    metadata_path = sandbox_dir / "maintenance_image_context.json"
    proc = _run_subprocess(
        [
            sys.executable,
            str(script_path),
            "--repo-root",
            str(repo_root),
            "--output-dir",
            str(context_dir),
            "--metadata-out",
            str(metadata_path),
        ],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to prepare maintenance Docker context.\n"
            f"script={script_path}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}\n"
        )
    return context_dir, _read_json_artifact(metadata_path)


def _compute_install_cache_fingerprints(
    *,
    repo_root: Path,
    run_dir: Path,
    python_major_minor: str,
    pdm_version: str,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    script_path = (repo_root / _INSTALL_CACHE_FINGERPRINT_SCRIPT_REL).resolve()
    if not script_path.exists():
        raise FileNotFoundError(f"Missing install-cache fingerprint script: {script_path}")
    output_path = run_dir / "sandbox" / "install_cache_fingerprints.json"
    proc = _run_subprocess(
        [
            sys.executable,
            str(script_path),
            "--repo-root",
            str(repo_root),
            "--all",
            "--python-major-minor",
            python_major_minor,
            "--pdm-version",
            pdm_version,
            "--output",
            str(output_path),
        ],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to compute install-cache fingerprints.\n"
            f"script={script_path}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}\n"
        )
    return _read_json_artifact(output_path)


def _update_json_artifact(path: Path, updater: Any) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        payload = _read_json_artifact(path)
    updated = updater(payload)
    _write_json(path, updated)


def _build_maintenance_image(
    *,
    context_dir: Path,
    local_ref: str,
    published_ref: str,
    cache_from: Sequence[str] = (),
    timeout_seconds: float | None,
    log_path: Path,
) -> None:
    argv = [
        "docker",
        "build",
        "--progress=plain",
        "-t",
        local_ref,
        "-t",
        published_ref,
        "-f",
        "Dockerfile",
        *[part for ref in cache_from for part in ("--cache-from", ref)],
        ".",
    ]
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as handle:
            proc = subprocess.run(
                argv,
                cwd=str(context_dir),
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
    except FileNotFoundError as e:
        raise RuntimeError("Docker CLI not found while building maintenance image.") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            "Timed out while building maintenance image.\n"
            f"context={context_dir}\n"
            f"log={log_path}\n"
        ) from e
    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to build maintenance Docker image.\n"
            f"context={context_dir}\n"
            f"log={log_path}\n"
            f"local_ref={local_ref}\n"
            f"published_ref={published_ref}\n"
        )


def prepare_maintenance_docker_image(
    *,
    repo_root: Path,
    run_dir: Path,
    timeout_seconds: float | None,
) -> MaintenanceImagePreparation:
    """Prepare and fingerprint a maintenance image without mutating Docker storage."""

    cfg = _load_maintenance_docker_config(repo_root=repo_root)
    context_dir, context_meta = _prepare_maintenance_image_context(
        repo_root=repo_root,
        run_dir=run_dir,
        timeout_seconds=timeout_seconds,
    )
    env_hash = compute_image_hash(context_dir=context_dir, dockerfile=context_dir / "Dockerfile")
    tag_suffix = env_hash[:16]
    return MaintenanceImagePreparation(
        context_dir=context_dir,
        context_metadata=context_meta,
        env_hash=env_hash,
        local_ref=f"{cfg.local_image_repo}:{tag_suffix}",
        published_ref=f"{cfg.published_image_repo}:{tag_suffix}",
    )


def resolve_maintenance_docker_image(
    *,
    repo_root: Path,
    run_dir: Path,
    force_rebuild: bool = False,
    timeout_seconds: float | None,
    artifact_path: Path | None = None,
    preparation: MaintenanceImagePreparation | None = None,
    prewrite_cleanup: dict[str, Any] | None = None,
) -> MaintenanceImageResolution:
    """
    Resolve the maintenance Docker image once and persist the immutable image contract.

    This function owns the expensive/local-Docker-mutating part of the maintenance profile:
    context preparation, environment hashing, local lookup, pull/tag, branch-alias cache pulls,
    build, and optional cleanup.  Per-ticket runs can later consume the persisted metadata via
    ``RunRequest.exec_maintenance_image_metadata_path`` without repeating these operations.
    """

    started = time.monotonic()
    cfg = _load_maintenance_docker_config(repo_root=repo_root)
    sandbox_dir = run_dir / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    if preparation is None:
        preparation = prepare_maintenance_docker_image(
            repo_root=repo_root, run_dir=run_dir, timeout_seconds=timeout_seconds
        )
    context_dir = preparation.context_dir
    context_meta = preparation.context_metadata
    env_hash = preparation.env_hash
    local_ref = preparation.local_ref
    published_ref = preparation.published_ref
    cleanup_summary: dict[str, Any] | None = prewrite_cleanup
    if cleanup_summary is None and cfg.cleanup_enabled and cfg.cleanup_on_prepare:
        cleanup_artifact_path = sandbox_dir / "maintenance_image_cleanup.json"
        try:
            # Cleanup must reclaim space before a pull, tag, or build can fail
            # because Docker's local store is full.  Protect both aliases of the
            # image being resolved in case that identity already exists locally.
            cleanup_summary = cleanup_local_maintenance_images(
                repo_root=repo_root,
                dry_run=cfg.cleanup_dry_run_default,
                timeout_seconds=timeout_seconds,
                artifact_path=cleanup_artifact_path,
                protected_refs=(local_ref, published_ref),
            )
        except Exception as exc:  # noqa: BLE001
            cleanup_summary = {
                "schema_version": 1,
                "cleanup_enabled": True,
                "dry_run": bool(cfg.cleanup_dry_run_default),
                "repos_scanned": list(_maintenance_repo_names(cfg=cfg)),
                "protected_tags": list(_maintenance_protected_tags(cfg=cfg)),
                "protected_refs": [local_ref, published_ref],
                "keep_local_count": cfg.keep_local_count,
                "kept_image_ids": [],
                "kept_tags": [],
                "deleted_tags": [],
                "deleted_image_ids": [],
                "errors": [f"Automatic maintenance image cleanup failed: {exc}"],
            }
            _write_json(cleanup_artifact_path, cleanup_summary)
    pull_attempted = False
    alias_pull_attempts: list[dict[str, Any]] = []
    build_cache_from: list[str] = []
    build_performed = False
    image_source: Literal["local", "pulled", "built"] | None = None
    pull_log_path: Path | None = None
    alias_pull_log_path: Path | None = None
    build_log_path: Path | None = None

    if force_rebuild:
        build_log_path = sandbox_dir / "maintenance_docker_build.log"
        _build_maintenance_image(
            context_dir=context_dir,
            local_ref=local_ref,
            published_ref=published_ref,
            cache_from=build_cache_from,
            timeout_seconds=timeout_seconds,
            log_path=build_log_path,
        )
        build_performed = True
        image_source = "built"
    elif _docker_image_exists_local(ref=local_ref, timeout_seconds=timeout_seconds):
        image_source = "local"
    else:
        if cfg.pull_policy in {"if_missing", "always"}:
            pull_attempted = True
            pull_log_path = sandbox_dir / "maintenance_docker_pull.json"
            pull_proc = _docker_pull_image(
                ref=published_ref,
                timeout_seconds=timeout_seconds,
                log_path=pull_log_path,
            )
            if pull_proc.returncode == 0:
                if local_ref != published_ref:
                    _docker_tag_image(
                        source_ref=published_ref,
                        target_ref=local_ref,
                        timeout_seconds=timeout_seconds,
                    )
                image_source = "pulled"
            else:
                candidate_cache_refs = _maintenance_branch_cache_refs(
                    cfg=cfg,
                    repo_root=repo_root,
                )
                if candidate_cache_refs:
                    alias_pull_log_path = sandbox_dir / "maintenance_docker_cache_pulls.json"
                    alias_pull_attempts = _docker_pull_images(
                        refs=candidate_cache_refs,
                        timeout_seconds=timeout_seconds,
                        log_path=alias_pull_log_path,
                    )
                    build_cache_from = [
                        str(item["ref"])
                        for item in alias_pull_attempts
                        if item.get("returncode") == 0 and item.get("ref")
                    ]

        if image_source is None:
            build_log_path = sandbox_dir / "maintenance_docker_build.log"
            _build_maintenance_image(
                context_dir=context_dir,
                local_ref=local_ref,
                published_ref=published_ref,
                cache_from=build_cache_from,
                timeout_seconds=timeout_seconds,
                log_path=build_log_path,
            )
            build_performed = True
            image_source = "built"

    post_cleanup_summary: dict[str, Any] | None = None
    if cfg.cleanup_enabled and cfg.cleanup_on_prepare:
        post_cleanup_artifact_path = sandbox_dir / "maintenance_image_postresolution_cleanup.json"
        current_aliases = {local_ref, published_ref}
        alias_error: str | None = None
        try:
            inspect_rows = _docker_image_inspect_rows(
                [local_ref], timeout_seconds=timeout_seconds
            )
            if inspect_rows:
                repo_tags = inspect_rows[0].get("RepoTags") or []
                current_aliases.update(
                    tag.strip() for tag in repo_tags if isinstance(tag, str) and tag.strip()
                )
        except Exception as exc:  # noqa: BLE001
            alias_error = f"Failed to inspect current maintenance image aliases: {exc}"
        try:
            post_cleanup_summary = cleanup_local_maintenance_images(
                repo_root=repo_root,
                dry_run=cfg.cleanup_dry_run_default,
                timeout_seconds=timeout_seconds,
                artifact_path=post_cleanup_artifact_path,
                protected_refs=sorted(current_aliases),
            )
            if alias_error is not None:
                post_cleanup_summary.setdefault("errors", []).append(alias_error)
        except Exception as exc:  # noqa: BLE001
            post_cleanup_summary = {
                "schema_version": 1,
                "cleanup_enabled": True,
                "dry_run": bool(cfg.cleanup_dry_run_default),
                "protected_refs": sorted(current_aliases),
                "errors": [f"Post-resolution maintenance image cleanup failed: {exc}"],
            }
            _write_json(post_cleanup_artifact_path, post_cleanup_summary)

    artifacts = {
        "context_metadata": str(sandbox_dir / "maintenance_image_context.json"),
        "pull_log": str(pull_log_path) if pull_log_path is not None else None,
        "alias_pull_log": str(alias_pull_log_path) if alias_pull_log_path is not None else None,
        "build_log": str(build_log_path) if build_log_path is not None else None,
    }
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "kind": "maintenance_image_resolution",
        "profile": "maintenance",
        "generated_at": _utc_now_z(),
        "repo_identity": {
            "local_git_root": str(repo_root),
            "origin_url": _git_remote_url(repo_dir=repo_root),
        },
        "image": {
            "env_hash": env_hash,
            "image_ref": local_ref,
            "local_ref": local_ref,
            "published_ref": published_ref,
            "source": image_source,
            "pull_attempted": pull_attempted,
            "alias_pull_attempts": alias_pull_attempts,
            "build_cache_from": build_cache_from,
            "build_performed": build_performed,
            "context_dir": str(context_dir),
            "context_metadata": context_meta,
        },
        "artifacts": artifacts,
        "timings": {
            "image_resolution_seconds": max(0.0, time.monotonic() - started),
        },
    }
    if cleanup_summary is not None:
        # Keep the historical top-level cleanup summary tied to the pre-write
        # cleanup artifact.  Post-resolution cleanup is a distinct later phase
        # and must not overwrite the primary result/error contract.
        cleanup_metadata = dict(cleanup_summary)
        cleanup_metadata["prewrite"] = cleanup_summary
        cleanup_metadata["postresolution"] = post_cleanup_summary
        metadata["cleanup"] = cleanup_metadata

    if artifact_path is not None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(artifact_path, metadata)

    return MaintenanceImageResolution(
        image_ref=local_ref,
        env_hash=env_hash,
        image_source=image_source,
        image_resolution_seconds=cast(float, metadata["timings"]["image_resolution_seconds"]),
        context_metadata=context_meta,
        metadata=metadata,
    )


def _load_pre_resolved_maintenance_image(
    *,
    metadata_path: Path,
) -> MaintenanceImageResolution:
    metadata = _read_json_artifact(metadata_path)
    image = metadata.get("image")
    if not isinstance(image, dict):
        raise ValueError(
            "Pre-resolved maintenance image metadata missing image object: "
            f"{metadata_path}"
        )

    env_hash = image.get("env_hash")
    image_ref = image.get("image_ref") or image.get("local_ref")
    image_source = image.get("source")
    context_meta = image.get("context_metadata")
    if not isinstance(env_hash, str) or not env_hash.strip():
        raise ValueError(
            "Pre-resolved maintenance image metadata missing image.env_hash: "
            f"{metadata_path}"
        )
    if not isinstance(image_ref, str) or not image_ref.strip():
        raise ValueError(
            "Pre-resolved maintenance image metadata missing image.image_ref: "
            f"{metadata_path}"
        )
    if image_source not in {"local", "pulled", "built"}:
        raise ValueError(
            "Pre-resolved maintenance image metadata image.source must be one of "
            f"local|pulled|built: {metadata_path}"
        )
    if not isinstance(context_meta, dict):
        raise ValueError(
            "Pre-resolved maintenance image metadata missing image.context_metadata: "
            f"{metadata_path}"
        )
    timings = metadata.get("timings")
    image_resolution_seconds = 0.0
    if isinstance(timings, dict) and isinstance(
        timings.get("image_resolution_seconds"), (int, float)
    ):
        image_resolution_seconds = max(0.0, float(timings["image_resolution_seconds"]))

    return MaintenanceImageResolution(
        image_ref=image_ref.strip(),
        env_hash=env_hash.strip(),
        image_source=cast(Literal["local", "pulled", "built"], image_source),
        image_resolution_seconds=image_resolution_seconds,
        context_metadata=cast(dict[str, Any], context_meta),
        metadata=metadata,
    )


def _prepare_maintenance_profile(
    *,
    repo_root: Path,
    run_dir: Path,
    workspace_dir: Path,
    request: RunRequest,
    cache_mode: Literal["cold", "warm"],
    cache_dir: Path | None,
    maintenance_venv_reuse_enabled: bool,
    timeout_seconds: float | None,
) -> MaintenanceProfilePreparation:
    cfg = _load_maintenance_docker_config(repo_root=repo_root)
    pre_resolved_metadata_path_raw = getattr(
        request, "exec_maintenance_image_metadata_path", None
    )
    pre_resolved_metadata_path: Path | None = None
    pre_resolved = False
    if pre_resolved_metadata_path_raw is not None:
        pre_resolved_metadata_path = Path(pre_resolved_metadata_path_raw).resolve()
        image_resolution = _load_pre_resolved_maintenance_image(
            metadata_path=pre_resolved_metadata_path,
        )
        pre_resolved = True
    else:
        image_resolution = resolve_maintenance_docker_image(
            repo_root=repo_root,
            run_dir=run_dir,
            force_rebuild=bool(getattr(request, "exec_rebuild_image", False)),
            timeout_seconds=timeout_seconds,
        )

    context_meta = image_resolution.context_metadata
    env_hash = image_resolution.env_hash
    local_ref = image_resolution.image_ref
    image_source = image_resolution.image_source

    fingerprint_start = time.monotonic()
    python_major_minor = context_meta.get("python_major_minor")
    pdm_version = context_meta.get("pdm_version")
    if not isinstance(python_major_minor, str) or not python_major_minor.strip():
        raise ValueError("Maintenance context metadata missing python_major_minor")
    if not isinstance(pdm_version, str) or not pdm_version.strip():
        raise ValueError("Maintenance context metadata missing pdm_version")
    fingerprints = _compute_install_cache_fingerprints(
        repo_root=workspace_dir,
        run_dir=run_dir,
        python_major_minor=python_major_minor.strip(),
        pdm_version=pdm_version.strip(),
        timeout_seconds=timeout_seconds,
    )
    fingerprint_seconds = max(0.0, time.monotonic() - fingerprint_start)

    cache_mounts: list[MountSpec] = []
    cache_mount_hits = 0
    host_cache_dir = cache_dir.resolve() if cache_dir is not None else None
    host_cache_root = (
        host_cache_dir / cfg.cache_root_subdir
        if cache_mode == "warm" and host_cache_dir is not None and maintenance_venv_reuse_enabled
        else None
    )
    container_cache_root = f"/cache/{cfg.cache_root_subdir}"
    projects_meta: list[dict[str, Any]] = []
    raw_projects = fingerprints.get("projects")
    if not isinstance(raw_projects, list):
        raise ValueError("Invalid install-cache fingerprint artifact: missing projects list")
    for project in raw_projects:
        if not isinstance(project, dict):
            raise ValueError(f"Invalid install-cache fingerprint entry: {project!r}")
        project_id = project.get("id")
        project_path = project.get("path")
        fingerprint = project.get("fingerprint")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError(f"Invalid install-cache fingerprint project id: {project!r}")
        if not isinstance(project_path, str) or not project_path.strip():
            raise ValueError(f"Invalid install-cache fingerprint project path: {project!r}")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError(f"Invalid install-cache fingerprint value: {project!r}")
        host_venv_dir = (
            host_cache_root / _safe_cache_project_id(project_id) / fingerprint / "venv"
            if host_cache_root is not None
            else None
        )
        mounted_cache_hit = bool(host_venv_dir is not None and host_venv_dir.is_dir())
        mount_host_path: Path | None = None
        mount_read_only: bool | None = None
        project_cache_strategy = (
            "disabled" if host_cache_root is None else "per-worker-writable-copy"
        )
        if mounted_cache_hit and host_venv_dir is not None:
            cache_mount_hits += 1
            mount_host_path = _prepare_per_worker_venv_cache_copy(
                run_dir=run_dir,
                project_id=project_id,
                fingerprint=fingerprint,
                source_venv_dir=host_venv_dir.resolve(),
            )
            mount_read_only = False
            cache_mounts.append(
                MountSpec(
                    host_path=mount_host_path,
                    container_path=f"/workspace/{project_path}/.venv",
                    read_only=mount_read_only,
                )
            )
        projects_meta.append(
            {
                "id": project_id,
                "path": project_path,
                "fingerprint": fingerprint,
                "cache_strategy": project_cache_strategy,
                "host_venv_dir": (
                    str(host_venv_dir.resolve()) if host_venv_dir is not None else None
                ),
                "mounted_cache_hit": mounted_cache_hit,
                "mounted_host_path": str(mount_host_path) if mount_host_path is not None else None,
                "mount_read_only": mount_read_only,
                "seed_available": bool(maintenance_venv_reuse_enabled),
            }
        )

    verification_contract = {"commands": list(getattr(request, "verification_commands", ()) or ())}
    commands = verification_contract["commands"]
    if len(commands) >= 4:
        verification_contract.update(
            {
                "smoke": commands[0],
                "install": commands[1],
                "lint": commands[2],
                "test": commands[3],
            }
        )

    env_overrides = _default_sandbox_env_overrides(
        cache_mode=cache_mode,
        maintenance_venv_cache_enabled=bool(host_cache_root is not None),
        maintenance_venv_cache_root=container_cache_root,
        maintenance_venv_seed_root=cfg.seed_root if maintenance_venv_reuse_enabled else None,
    )

    cache_strategy = "per-worker-writable-copy" if host_cache_root is not None else "disabled"

    resolution_image_meta = image_resolution.metadata.get("image")
    if not isinstance(resolution_image_meta, dict):
        resolution_image_meta = {}
    resolution_artifacts_meta = image_resolution.metadata.get("artifacts")
    if not isinstance(resolution_artifacts_meta, dict):
        resolution_artifacts_meta = {}

    metadata: dict[str, Any] = {
        "schema_version": 1,
        "profile": "maintenance",
        "eligible": True,
        "repo_identity": {
            "local_git_root": str(repo_root),
            "origin_url": _git_remote_url(repo_dir=repo_root),
        },
        "image": {
            "env_hash": env_hash,
            "image_ref": local_ref,
            "local_ref": local_ref,
            "published_ref": resolution_image_meta.get("published_ref"),
            "source": image_source,
            "pull_attempted": bool(resolution_image_meta.get("pull_attempted", False)),
            "alias_pull_attempts": resolution_image_meta.get("alias_pull_attempts", []),
            "build_cache_from": resolution_image_meta.get("build_cache_from", []),
            "build_performed": bool(resolution_image_meta.get("build_performed", False)),
            "context_dir": resolution_image_meta.get("context_dir"),
            "context_metadata": context_meta,
            "pre_resolved": pre_resolved,
            "pre_resolved_image_ref": local_ref if pre_resolved else None,
            "pre_resolved_metadata_path": (
                str(pre_resolved_metadata_path) if pre_resolved_metadata_path is not None else None
            ),
        },
        "image_resolution": {
            "pre_resolved": pre_resolved,
            "metadata_path": (
                str(pre_resolved_metadata_path) if pre_resolved_metadata_path is not None else None
            ),
            "provenance": image_resolution.metadata,
            "artifacts": resolution_artifacts_meta,
        },
        "cache": {
            "enabled": bool(host_cache_root is not None),
            "strategy": cache_strategy,
            "strategy_reason": (
                "Shared warm-cache .venv hits are copied to a per-run writable directory before "
                "being mounted into the project workspace, so concurrent workers never receive "
                "the same host .venv cache path as a writable bind mount."
                if host_cache_root is not None
                else (
                    "Maintenance venv cache is disabled because warm cache or reuse "
                    "is not enabled."
                )
            ),
            "host_cache_dir": str(host_cache_dir) if host_cache_dir is not None else None,
            "host_cache_root": (
                str(host_cache_root.resolve()) if host_cache_root is not None else None
            ),
            "container_cache_root": container_cache_root if host_cache_root is not None else None,
            "copy_root": (
                str((run_dir / "sandbox" / "maintenance_venv_copies").resolve())
                if host_cache_root is not None
                else None
            ),
            "seed_root": cfg.seed_root if maintenance_venv_reuse_enabled else None,
            "projects": projects_meta,
        },
        "verification_contract": verification_contract,
        "timings": {
            "fingerprint_seconds": fingerprint_seconds,
            "image_resolution_seconds": (
                0.0 if pre_resolved else image_resolution.image_resolution_seconds
            ),
            "pre_resolved_image_resolution_seconds": image_resolution.image_resolution_seconds,
            "container_start_seconds": None,
            "cache_mount_hits": cache_mount_hits,
            "seed_hits": None,
            "install_projects_run": None,
        },
    }
    if "cleanup" in image_resolution.metadata:
        metadata["cleanup"] = image_resolution.metadata["cleanup"]

    return MaintenanceProfilePreparation(
        image_ref=local_ref,
        env_hash=env_hash,
        image_source=image_source,
        image_resolution_seconds=0.0 if pre_resolved else image_resolution.image_resolution_seconds,
        fingerprint_seconds=fingerprint_seconds,
        cache_mount_hits=cache_mount_hits,
        cache_mounts=cache_mounts,
        env_overrides=env_overrides,
        metadata=metadata,
    )


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

    profile = _normalize_exec_docker_profile(getattr(request, "exec_docker_profile", "standard"))

    docker_python_raw = getattr(request, "exec_docker_python", "auto")
    docker_python = str(docker_python_raw or "auto").strip().lower()
    if not docker_python:
        docker_python = "auto"

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
    maintenance_venv_reuse_enabled = bool(getattr(request, "exec_maintenance_venv_cache", False))
    maintenance_venv_cache_enabled = bool(
        cache_mode == "warm" and maintenance_venv_reuse_enabled
    )

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

    context_dir: Path | None = None
    image_ref: str | None = None
    env_overrides: dict[str, str]
    maintenance_profile: MaintenanceProfilePreparation | None = None
    docker_timeout_seconds = getattr(request, "exec_docker_timeout_seconds", None)

    if profile == "maintenance":
        if getattr(request, "exec_docker_context", None) is not None:
            raise ValueError(
                "exec_docker_profile='maintenance' does not support exec_docker_context."
            )
        if dockerfile is not None:
            raise ValueError("exec_docker_profile='maintenance' does not support exec_dockerfile.")
        if bool(getattr(request, "exec_use_target_sandbox_cli_install", False)):
            raise ValueError(
                "exec_docker_profile='maintenance' does not support "
                "exec_use_target_sandbox_cli_install."
            )
        maintenance_profile = _prepare_maintenance_profile(
            repo_root=repo_root,
            run_dir=run_dir,
            workspace_dir=workspace_dir,
            request=request,
            cache_mode=cache_mode_typed,
            cache_dir=cache_dir,
            maintenance_venv_reuse_enabled=maintenance_venv_reuse_enabled,
            timeout_seconds=docker_timeout_seconds,
        )
        image_ref = maintenance_profile.image_ref
        extra_mounts.extend(maintenance_profile.cache_mounts)
        env_overrides = dict(maintenance_profile.env_overrides)
        _write_json(sandbox_dir / "maintenance_profile.json", maintenance_profile.metadata)
    else:
        context_dir = getattr(request, "exec_docker_context", None)
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
        env_overrides = _default_sandbox_env_overrides(
            cache_mode=cache_mode_typed,
            maintenance_venv_cache_enabled=maintenance_venv_cache_enabled,
        )

    spec = SandboxSpec(
        backend="docker",
        image_ref=image_ref,
        image_context_path=context_dir,
        dockerfile=dockerfile,
        network_mode=network_mode,
        cache_mode=cache_mode_typed,
        cache_dir=cache_dir.resolve() if cache_dir is not None else None,
        env_allowlist=env_allowlist,
        env_overrides=env_overrides,
        extra_mounts=extra_mounts,
        keep_container=keep_container,
        rebuild_image=False if profile == "maintenance" else rebuild_image,
        docker_timeout_seconds=docker_timeout_seconds,
    )

    container_name = f"sandbox-{workspace_id}"
    container_start_monotonic = time.monotonic()
    instance = DockerSandbox(
        workspace_dir=workspace_dir,
        artifacts_dir=sandbox_dir,
        spec=spec,
        container_name=container_name,
    ).start()
    container_start_seconds = max(0.0, time.monotonic() - container_start_monotonic)

    _update_json_artifact(
        sandbox_dir / "sandbox.json",
        lambda payload: {
            **payload,
            "docker_profile": profile,
            "image_ref": image_ref or payload.get("image_ref"),
            "maintenance_env_hash": (
                maintenance_profile.env_hash if maintenance_profile is not None else None
            ),
            "maintenance_image_source": (
                maintenance_profile.image_source if maintenance_profile is not None else None
            ),
            "maintenance_cache_mount_count": (
                maintenance_profile.cache_mount_hits if maintenance_profile is not None else 0
            ),
            "maintenance_cache_strategy": (
                cast(dict[str, Any], maintenance_profile.metadata.get("cache", {})).get(
                    "strategy"
                )
                if maintenance_profile is not None
                and isinstance(maintenance_profile.metadata.get("cache"), dict)
                else None
            ),
        },
    )
    if maintenance_profile is not None:
        _update_json_artifact(
            sandbox_dir / "maintenance_profile.json",
            lambda payload: {
                **payload,
                "container_name": instance.container_name,
                "image_ref": instance.image_tag,
                "timings": {
                    **(
                        cast(dict[str, Any], payload.get("timings", {}))
                        if isinstance(payload.get("timings"), dict)
                        else {}
                    ),
                    "fingerprint_seconds": maintenance_profile.fingerprint_seconds,
                    "image_resolution_seconds": maintenance_profile.image_resolution_seconds,
                    "container_start_seconds": container_start_seconds,
                    "cache_mount_hits": maintenance_profile.cache_mount_hits,
                },
            },
        )

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
