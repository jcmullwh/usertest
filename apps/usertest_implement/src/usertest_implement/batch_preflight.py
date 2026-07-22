from __future__ import annotations

import base64
import json
import os
import random
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml
from runner_core.execution_backend import (
    _load_maintenance_docker_config,
    cleanup_local_maintenance_images,
    prepare_maintenance_docker_image,
    resolve_maintenance_docker_image,
)

from usertest_implement.batch_state import utc_now_z


def _run(
    argv: list[str],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    command = list(argv)
    resolved = shutil.which(command[0])
    if resolved:
        command[0] = resolved
    return subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _write_log(path: Path, proc: subprocess.CompletedProcess[str]) -> None:
    lines = ["$ " + " ".join(str(part) for part in proc.args), f"exit_code={proc.returncode}"]
    if proc.stdout.strip():
        lines.extend(["stdout:", proc.stdout.rstrip()])
    if proc.stderr.strip():
        lines.extend(["stderr:", proc.stderr.rstrip()])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _coerce_handoff_bool(*, key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"Setting {key!r} must be a boolean value")


def _effective_handoff_flags(run_settings: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Apply the run command's commit -> push -> PR dependency chain."""

    commit = _coerce_handoff_bool(key="commit", value=run_settings.get("commit", False))
    push = commit and _coerce_handoff_bool(
        key="push", value=run_settings.get("push", False)
    )
    pr = push and _coerce_handoff_bool(key="pr", value=run_settings.get("pr", False))
    return commit, push, pr


def _batch_remote_handoff_requested(*, repo_root: Path, batch_config: dict[str, Any]) -> bool:
    defaults = batch_config.get("defaults", {})
    if not isinstance(defaults, dict):
        return True

    settings_raw = defaults.get("run_settings_path") or "configs/usertest_implement_settings.yaml"
    settings_path = Path(str(settings_raw))
    if not settings_path.is_absolute():
        settings_path = repo_root / settings_path
    if not settings_path.exists():
        return True

    settings = _load_yaml(settings_path)
    profiles = settings.get("profiles", {})
    if not isinstance(profiles, dict):
        return True

    profile_name = str(
        defaults.get("run_settings_profile") or settings.get("default_profile") or "default"
    )
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        return True

    run_settings: dict[str, Any] = {}
    for section_name in ("run_common", "run"):
        section = profile.get(section_name, {})
        if not isinstance(section, dict):
            return True
        run_settings.update(section)

    _, push, pr = _effective_handoff_flags(run_settings)
    return push or pr


def _github_cli_ready(*, repo_root: Path, preflight_dir: Path) -> bool:
    auth_proc = _run(["gh", "auth", "status"], cwd=repo_root)
    _write_log(preflight_dir / "gh_auth.log", auth_proc)
    if auth_proc.returncode == 0:
        return True

    user_proc = _run(["gh", "api", "user", "--jq", ".login"], cwd=repo_root)
    _write_log(preflight_dir / "gh_auth_user_probe.log", user_proc)
    repo_proc = _run(["gh", "repo", "view", "--json", "nameWithOwner"], cwd=repo_root)
    _write_log(preflight_dir / "gh_auth_repo_probe.log", repo_proc)
    return user_proc.returncode == 0 and repo_proc.returncode == 0


def _git_branch(repo_root: Path) -> str:
    proc = _run(["git", "branch", "--show-current"], cwd=repo_root)
    branch = proc.stdout.strip()
    if proc.returncode != 0 or not branch:
        raise RuntimeError("Unable to determine git branch.")
    return branch


def _git_head(repo_root: Path) -> str:
    proc = _run(["git", "rev-parse", "HEAD"], cwd=repo_root)
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise RuntimeError("Unable to determine git HEAD.")
    return sha


def _pick_ci_run(runs: list[dict[str, Any]], *, head_sha: str) -> dict[str, Any] | None:
    matches = [
        item
        for item in runs
        if isinstance(item, dict)
        and str(item.get("headSha") or "").strip() == head_sha
        and str(item.get("event") or "").strip() == "push"
    ]
    if not matches:
        matches = [
            item
            for item in runs
            if isinstance(item, dict) and str(item.get("headSha") or "").strip() == head_sha
        ]
    if not matches:
        return None
    matches.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
    return matches[0]


def _blocker(
    *,
    blocker_id: str,
    failure_class: str,
    summary: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "blocker_id": blocker_id,
        "class": failure_class,
        "summary": summary,
        "evidence": evidence,
        "created_utc": utc_now_z(),
    }


def _gitlab_registry_probe() -> dict[str, Any] | None:
    project_id = os.environ.get("GITLAB_PYPI_PROJECT_ID", "").strip()
    if not project_id:
        return None
    username = os.environ.get("GITLAB_PYPI_USERNAME", "").strip()
    password = os.environ.get("GITLAB_PYPI_PASSWORD", "").strip()
    if not username or not password:
        return _blocker(
            blocker_id="registry_or_auth",
            failure_class="registry_or_auth",
            summary="GitLab package index is configured but credentials are missing.",
            evidence={"project_id": project_id},
        )
    base_url = (os.environ.get("GITLAB_BASE_URL", "").strip() or "https://gitlab.com").rstrip("/")
    if "://" not in base_url:
        base_url = "https://" + base_url
    url = f"{base_url}/api/v4/projects/{project_id}/packages/pypi/simple"
    auth = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    request = Request(url, headers={"Authorization": f"Basic {auth}"})
    try:
        with urlopen(request) as response:  # noqa: S310
            content_type = str(response.headers.get("Content-Type") or "")
            body = response.read(512)
    except HTTPError as exc:
        return _blocker(
            blocker_id="registry_or_auth",
            failure_class="registry_or_auth",
            summary="GitLab package index probe returned an HTTP error.",
            evidence={"url": url, "status": exc.code},
        )
    except URLError as exc:
        return _blocker(
            blocker_id="registry_or_auth",
            failure_class="registry_or_auth",
            summary="GitLab package index probe failed to connect.",
            evidence={"url": url, "error": str(exc)},
        )

    lowered_type = content_type.lower()
    if not body.strip() or not any(marker in lowered_type for marker in ("json", "html")):
        return _blocker(
            blocker_id="registry_or_auth",
            failure_class="registry_or_auth",
            summary="GitLab package index probe returned malformed content.",
            evidence={"url": url, "content_type": content_type},
        )
    return None


def run_batch_preflight(
    *,
    repo_root: Path,
    batch_dir: Path,
    batch_config: dict[str, Any],
    worker_roster: list[dict[str, Any]],
    exec_backend: str,
    exec_docker_profile: str = "standard",
    resolve_maintenance_image: bool = False,
    docker_timeout_seconds: float | None = None,
    docker_scratch_payload_bytes: int = 3,
) -> dict[str, Any]:
    defaults = batch_config.get("defaults", {})
    blockers: list[dict[str, Any]] = []
    preflight_dir = batch_dir / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)

    if bool(defaults.get("require_clean_git", True)):
        proc = _run(["git", "status", "--porcelain"], cwd=repo_root)
        _write_log(preflight_dir / "git_status.log", proc)
        if proc.returncode != 0:
            blockers.append(
                _blocker(
                    blocker_id="baseline_repo_red",
                    failure_class="baseline_repo_regression",
                    summary="Unable to read git status for batch preflight.",
                    evidence={"path": str(preflight_dir / "git_status.log")},
                )
            )
        elif proc.stdout.strip():
            blockers.append(
                _blocker(
                    blocker_id="baseline_repo_red",
                    failure_class="baseline_repo_regression",
                    summary="Batch repo has uncommitted tracked changes.",
                    evidence={"path": str(preflight_dir / "git_status.log")},
                )
            )

    if bool(defaults.get("require_local_green", True)):
        lint_proc = _run(
            [
                "python",
                "tools/scaffold/scaffold.py",
                "run",
                "lint",
                "--all",
                "--skip-missing",
                "--keep-going",
            ],
            cwd=repo_root,
        )
        _write_log(preflight_dir / "local_lint.log", lint_proc)
        if lint_proc.returncode != 0:
            blockers.append(
                _blocker(
                    blocker_id="baseline_repo_red",
                    failure_class="baseline_repo_regression",
                    summary="Local lint --all failed during batch preflight.",
                    evidence={"path": str(preflight_dir / "local_lint.log")},
                )
            )

        test_proc = _run(
            [
                "python",
                "tools/scaffold/scaffold.py",
                "run",
                "test",
                "--all",
                "--skip-missing",
                "--keep-going",
            ],
            cwd=repo_root,
        )
        _write_log(preflight_dir / "local_test.log", test_proc)
        if test_proc.returncode != 0:
            blockers.append(
                _blocker(
                    blocker_id="baseline_repo_red",
                    failure_class="baseline_repo_regression",
                    summary="Local test --all failed during batch preflight.",
                    evidence={"path": str(preflight_dir / "local_test.log")},
                )
            )

    branch = _git_branch(repo_root)
    head_sha = _git_head(repo_root)
    base_ci_run_url: str | None = None
    require_base_ci = bool(defaults.get("require_ci_green_for_base", True))
    require_github = require_base_ci or _batch_remote_handoff_requested(
        repo_root=repo_root,
        batch_config=batch_config,
    )

    if require_github:
        if not _github_cli_ready(repo_root=repo_root, preflight_dir=preflight_dir):
            blockers.append(
                _blocker(
                    blocker_id="batch_control_plane",
                    failure_class="batch_control_plane",
                    summary="GitHub CLI cannot perform required batch push/PR operations.",
                    evidence={
                        "auth_path": str(preflight_dir / "gh_auth.log"),
                        "user_probe_path": str(preflight_dir / "gh_auth_user_probe.log"),
                        "repo_probe_path": str(preflight_dir / "gh_auth_repo_probe.log"),
                    },
                )
            )
    else:
        (preflight_dir / "gh_auth.log").write_text(
            "skipped: run settings do not request push/PR and base CI preflight is disabled.\n",
            encoding="utf-8",
        )

    if require_base_ci:
        ci_proc = _run(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                "CI",
                "--branch",
                branch,
                "--limit",
                "50",
                "--json",
                "databaseId,headSha,event,status,conclusion,createdAt,url",
            ],
            cwd=repo_root,
        )
        _write_log(preflight_dir / "base_ci.log", ci_proc)
        if ci_proc.returncode != 0:
            blockers.append(
                _blocker(
                    blocker_id="baseline_repo_red",
                    failure_class="baseline_repo_regression",
                    summary="Unable to query GitHub Actions for the batch base commit.",
                    evidence={"path": str(preflight_dir / "base_ci.log")},
                )
            )
        else:
            try:
                runs = json.loads(ci_proc.stdout or "[]")
            except json.JSONDecodeError:
                runs = []
            picked = _pick_ci_run(runs if isinstance(runs, list) else [], head_sha=head_sha)
            if picked is None or str(picked.get("conclusion") or "").strip().lower() != "success":
                blockers.append(
                    _blocker(
                        blocker_id="baseline_repo_red",
                        failure_class="baseline_repo_regression",
                        summary="Latest CI for the batch commit is not green.",
                        evidence={"head_sha": head_sha, "path": str(preflight_dir / "base_ci.log")},
                    )
                )
            else:
                base_ci_run_url = str(picked.get("url") or "") or None

    agents_config = _load_yaml(repo_root / "configs" / "agents.yaml").get("agents", {})
    if not isinstance(agents_config, dict):
        agents_config = {}
    default_binaries = {"codex": "codex", "claude": "claude", "gemini": "gemini"}
    for worker in worker_roster:
        agent = str(worker.get("agent") or "").strip().lower()
        if not agent:
            continue
        agent_cfg = agents_config.get(agent, {})
        binary = (
            str(agent_cfg.get("binary") or default_binaries.get(agent) or agent).strip()
            if isinstance(agent_cfg, dict)
            else default_binaries.get(agent, agent)
        )
        proc = _run([binary, "--version"], cwd=repo_root)
        _write_log(preflight_dir / f"agent_{agent}.log", proc)
        if proc.returncode != 0:
            blockers.append(
                _blocker(
                    blocker_id="batch_control_plane",
                    failure_class="batch_control_plane",
                    summary=f"Agent binary preflight failed for {agent}.",
                    evidence={"agent": agent, "path": str(preflight_dir / f"agent_{agent}.log")},
                )
            )

    if exec_backend == "docker":
        maintenance_image_metadata: dict[str, Any] | None = None
        maintenance_preparation = None
        batch_prewrite: dict[str, Any] | None = None
        maintenance_metadata_path: Path | None = None
        docker_version = _run(["docker", "version"], cwd=repo_root)
        _write_log(preflight_dir / "docker_version.log", docker_version)
        if docker_version.returncode != 0:
            blockers.append(
                _blocker(
                    blocker_id="infra_transient",
                    failure_class="infra_transient",
                    summary="docker version failed during preflight.",
                    evidence={"path": str(preflight_dir / "docker_version.log")},
                )
            )
        docker_buildx = _run(["docker", "buildx", "ls"], cwd=repo_root)
        _write_log(preflight_dir / "docker_buildx.log", docker_buildx)
        if docker_buildx.returncode != 0:
            blockers.append(
                _blocker(
                    blocker_id="infra_transient",
                    failure_class="infra_transient",
                    summary="docker buildx is not healthy during preflight.",
                    evidence={"path": str(preflight_dir / "docker_buildx.log")},
                )
            )
        if resolve_maintenance_image and exec_docker_profile == "maintenance":
            maintenance_metadata_path = preflight_dir / "maintenance_image.json"
            try:
                preparation_dir = batch_dir / "preflight_maintenance_image"
                maintenance_preparation = prepare_maintenance_docker_image(
                    repo_root=repo_root,
                    run_dir=preparation_dir,
                    timeout_seconds=docker_timeout_seconds,
                )
                prewrite_artifact_path = preflight_dir / "maintenance_image_batch_prewrite.json"
                cleanup_cfg = _load_maintenance_docker_config(repo_root=repo_root)
                if not (cleanup_cfg.cleanup_enabled and cleanup_cfg.cleanup_on_prepare):
                    batch_prewrite = {
                        "schema_version": 1,
                        "phase": "batch_prewrite",
                        "skipped": True,
                        "cleanup_enabled": cleanup_cfg.cleanup_enabled,
                        "cleanup_on_prepare": cleanup_cfg.cleanup_on_prepare,
                        "dry_run": cleanup_cfg.cleanup_dry_run_default,
                        "errors": [],
                    }
                else:
                    try:
                        batch_prewrite = cleanup_local_maintenance_images(
                            repo_root=repo_root,
                            dry_run=cleanup_cfg.cleanup_dry_run_default,
                            timeout_seconds=docker_timeout_seconds,
                            artifact_path=prewrite_artifact_path,
                            protected_refs=(
                                maintenance_preparation.local_ref,
                                maintenance_preparation.published_ref,
                            ),
                        )
                    except Exception as exc:  # noqa: BLE001
                        batch_prewrite = {
                            "schema_version": 1,
                            "phase": "batch_prewrite",
                            "errors": [f"Batch prewrite maintenance cleanup failed: {exc}"],
                        }
            except Exception as exc:  # noqa: BLE001
                blockers.append(
                    _blocker(
                        blocker_id="infra_transient",
                        failure_class="infra_transient",
                        summary=(
                            "Maintenance Docker image resolution failed during batch preflight."
                        ),
                        evidence={
                            "path": str(maintenance_metadata_path),
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
                )
        if docker_scratch_payload_bytes < 1:
            raise ValueError("docker_scratch_payload_bytes must be at least one byte")
        with tempfile.TemporaryDirectory(prefix="usertest_batch_docker_preflight_") as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "Dockerfile").write_text(
                "FROM scratch\nCOPY sentinel /sentinel\n",
                encoding="utf-8",
            )
            sentinel = temp_root / "sentinel"
            if docker_scratch_payload_bytes == 3:
                sentinel.write_text("ok\n", encoding="utf-8")
            else:
                rng = random.Random(0)
                with sentinel.open("wb") as handle:
                    remaining = docker_scratch_payload_bytes
                    while remaining:
                        size = min(1024 * 1024, remaining)
                        handle.write(rng.randbytes(size))
                        remaining -= size
            docker_build = _run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--progress=plain",
                    "--load",
                    "-t",
                    "usertest-batch-preflight:latest",
                    str(temp_root),
                ],
                cwd=repo_root,
            )
        _write_log(preflight_dir / "docker_build.log", docker_build)
        if docker_build.returncode != 0:
            blockers.append(
                _blocker(
                    blocker_id="infra_transient",
                    failure_class="infra_transient",
                    summary="Docker buildx scratch build failed during preflight.",
                    evidence={"path": str(preflight_dir / "docker_build.log")},
                )
            )
        if maintenance_preparation is not None and maintenance_metadata_path is not None:
            try:
                resolution = resolve_maintenance_docker_image(
                    repo_root=repo_root,
                    run_dir=batch_dir / "preflight_maintenance_image",
                    force_rebuild=False,
                    timeout_seconds=docker_timeout_seconds,
                    artifact_path=maintenance_metadata_path,
                    preparation=maintenance_preparation,
                    prewrite_cleanup=batch_prewrite,
                )
                maintenance_image_metadata = {
                    "path": str(maintenance_metadata_path),
                    "env_hash": resolution.env_hash,
                    "image_ref": resolution.image_ref,
                    "source": resolution.image_source,
                    "timings": resolution.metadata.get("timings", {}),
                    "artifacts": resolution.metadata.get("artifacts", {}),
                    "batch_prewrite": batch_prewrite,
                }
            except Exception as exc:  # noqa: BLE001
                blockers.append(
                    _blocker(
                        blocker_id="infra_transient",
                        failure_class="infra_transient",
                        summary=(
                            "Maintenance Docker image resolution failed during batch preflight."
                        ),
                        evidence={
                            "path": str(maintenance_metadata_path),
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
                )
    else:
        maintenance_image_metadata = None

    registry_blocker = _gitlab_registry_probe()
    if registry_blocker is not None:
        blockers.append(registry_blocker)

    return {
        "branch": branch,
        "head_sha": head_sha,
        "base_ci_run_url": base_ci_run_url,
        "maintenance_image_metadata": maintenance_image_metadata,
        "blockers": blockers,
    }
