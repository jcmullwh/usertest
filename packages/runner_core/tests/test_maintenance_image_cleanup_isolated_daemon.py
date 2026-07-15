"""Explicitly opt-in isolated-DIND evidence for maintenance-image cleanup.

This module is intentionally excluded from ordinary test execution.  A future
approved run provisions disposable Docker-in-Docker daemons through loopback
TCP only, then exercises the real batch and resolver paths against them.
"""

import ipaddress
import os
import random
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from usertest_implement import batch_preflight

import runner_core.execution_backend as backend_mod
from runner_core.execution_backend import MaintenanceDockerConfig, MaintenanceImagePreparation

_OPT_IN = os.environ.get("USERTEST_RUN_ISOLATED_DOCKER_TESTS") == "1"
_ISOLATION_ACK = os.environ.get("USERTEST_ISOLATED_DOCKER_DAEMON") == "1"
_PROVISIONER = os.environ.get("USERTEST_DIND_PROVISIONER_HOST")
_DIND_IMAGE = os.environ.get("USERTEST_ISOLATED_DIND_IMAGE")
_TMPFS_BYTES = os.environ.get("USERTEST_ISOLATED_DIND_TMPFS_BYTES")

pytestmark = pytest.mark.skipif(
    not (_OPT_IN and _ISOLATION_ACK and _PROVISIONER and _DIND_IMAGE and _TMPFS_BYTES),
    reason=(
        "requires USERTEST_RUN_ISOLATED_DOCKER_TESTS=1, "
        "USERTEST_ISOLATED_DOCKER_DAEMON=1, USERTEST_DIND_PROVISIONER_HOST, "
        "USERTEST_ISOLATED_DIND_IMAGE, and USERTEST_ISOLATED_DIND_TMPFS_BYTES"
    ),
)


@dataclass(frozen=True)
class _Daemon:
    provisioner: str
    endpoint: str
    container_id: str
    image_digest: str
    tmpfs_bytes: int


@dataclass(frozen=True)
class _ArmSpec:
    image_digest: str
    tmpfs_bytes: int
    seed_bytes: int
    keep_count: int
    protected_refs: tuple[str, ...]
    inventory_refs: tuple[str, ...]


def _loopback_tcp_endpoint(raw: str, *, label: str) -> str:
    """Reject default, npipe, Unix, hostname, and non-loopback Docker routing."""

    parsed = urlparse(raw.strip())
    if parsed.scheme != "tcp" or not parsed.hostname or parsed.port is None:
        raise pytest.UsageError(f"{label} must be an explicit tcp://127.0.0.1:PORT endpoint")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise pytest.UsageError(f"{label} must use a literal loopback IP address") from exc
    if not address.is_loopback:
        raise pytest.UsageError(f"{label} must target loopback, not {parsed.hostname!r}")
    return f"tcp://{parsed.hostname}:{parsed.port}"


def _docker(
    endpoint: str,
    *args: str,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Invoke Docker only within an explicitly opted-in test body or fixture."""

    return subprocess.run(
        ["docker", "--host", endpoint, *args],
        cwd=str(cwd),
        capture_output=True,
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _dind_parameters() -> tuple[str, str, int]:
    assert _PROVISIONER is not None and _DIND_IMAGE is not None and _TMPFS_BYTES is not None
    provisioner = _loopback_tcp_endpoint(_PROVISIONER, label="USERTEST_DIND_PROVISIONER_HOST")
    if "@sha256:" not in _DIND_IMAGE:
        raise pytest.UsageError("USERTEST_ISOLATED_DIND_IMAGE must be pinned by digest")
    try:
        tmpfs_bytes = int(_TMPFS_BYTES)
    except ValueError as exc:
        raise pytest.UsageError("USERTEST_ISOLATED_DIND_TMPFS_BYTES must be an integer") from exc
    if tmpfs_bytes < 32 * 1024 * 1024:
        raise pytest.UsageError("isolated DIND tmpfs capacity must be at least 32 MiB")
    return provisioner, _DIND_IMAGE, tmpfs_bytes


@contextmanager
def _disposable_dind(*, cwd: Path) -> Iterator[_Daemon]:
    """Provision a one-test DIND and expose only its loopback-published TCP API."""

    provisioner, image_digest, tmpfs_bytes = _dind_parameters()
    proc = _docker(
        provisioner,
        "run",
        "--detach",
        "--rm",
        "--privileged",
        "--tmpfs",
        f"/var/lib/docker:rw,size={tmpfs_bytes}",
        "--publish",
        "127.0.0.1::2375",
        image_digest,
        "dockerd",
        "--host=tcp://0.0.0.0:2375",
        cwd=cwd,
    )
    container_id = proc.stdout.strip()
    try:
        mapping = _docker(provisioner, "port", container_id, "2375/tcp", cwd=cwd).stdout.strip()
        target = _loopback_tcp_endpoint(f"tcp://{mapping}", label="published DIND endpoint")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if _docker(target, "info", cwd=cwd, check=False).returncode == 0:
                yield _Daemon(provisioner, target, container_id, image_digest, tmpfs_bytes)
                return
            time.sleep(0.5)
        raise AssertionError(f"DIND daemon did not become ready at {target}")
    finally:
        _docker(provisioner, "rm", "--force", container_id, cwd=cwd, check=False)


@pytest.fixture
def dind_factory(tmp_path: Path) -> Callable[[], Iterator[_Daemon]]:
    """Return a context-manager factory so each arm gets a fresh daemon."""

    return lambda: _disposable_dind(cwd=tmp_path)


def _write_payload(path: Path, *, byte_count: int, seed: int) -> None:
    rng = random.Random(seed)
    with path.open("wb") as handle:
        remaining = byte_count
        while remaining:
            chunk_size = min(1024 * 1024, remaining)
            handle.write(rng.randbytes(chunk_size))
            remaining -= chunk_size


def _build_scratch_identity(
    daemon: _Daemon,
    *,
    work_dir: Path,
    repository: str,
    tag: str,
    payload_bytes: int,
    seed: int,
) -> str:
    context = work_dir / f"context-{tag}"
    context.mkdir()
    _write_payload(context / "payload", byte_count=payload_bytes, seed=seed)
    (context / "Dockerfile").write_text(
        f"FROM scratch\nCOPY payload /payload\nLABEL isolated_identity={tag!r}\n",
        encoding="utf-8",
    )
    ref = f"{repository}:{tag}"
    _docker(daemon.endpoint, "build", "--no-cache", "--tag", ref, str(context), cwd=work_dir)
    return _docker(
        daemon.endpoint,
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        ref,
        cwd=work_dir,
    ).stdout.strip()


def _maintenance_config(*, keep_count: int, cleanup_enabled: bool) -> MaintenanceDockerConfig:
    return MaintenanceDockerConfig(
        local_image_repo="usertest-isolated-maintenance",
        published_image_repo="usertest-isolated-maintenance-published",
        pull_policy="never",
        seed_root="/unused",
        cache_root_subdir="unused",
        publish_branches=(),
        cleanup_enabled=cleanup_enabled,
        keep_local_count=keep_count,
        protect_tags=("required-latest", "protected-latest", "running-latest"),
        cleanup_on_prepare=cleanup_enabled,
        cleanup_dry_run_default=False,
    )


def _seed_burst(
    daemon: _Daemon,
    *,
    work_dir: Path,
    cfg: MaintenanceDockerConfig,
) -> tuple[_ArmSpec, dict[str, str]]:
    """Create deterministic paired aliases plus required/current safety identities."""

    work_dir.mkdir(parents=True, exist_ok=True)
    # Three protected safety identities plus enough ordinary identities to exceed
    # the identity budget in both baseline and recovery arms.
    seed_count = cfg.keep_local_count + 6
    # Leave only a small headroom margin so the baseline's first storage write is
    # constrained, while reclaiming all but ``keep_count`` leaves recovery room.
    seed_bytes = daemon.tmpfs_bytes * 95 // 100 // seed_count
    identity_ids: dict[str, str] = {}
    inventory_refs: list[str] = []
    for number in range(seed_count):
        tag = f"{number:016x}"
        identity_ids[tag] = _build_scratch_identity(
            daemon,
            work_dir=work_dir,
            repository=cfg.local_image_repo,
            tag=tag,
            payload_bytes=seed_bytes,
            seed=number,
        )
        local_ref = f"{cfg.local_image_repo}:{tag}"
        published_ref = f"{cfg.published_image_repo}:{tag}"
        _docker(daemon.endpoint, "tag", local_ref, published_ref, cwd=work_dir)
        inventory_refs.extend([local_ref, published_ref])

    safety_refs: list[str] = []
    for offset, alias in enumerate(("required-latest", "protected-latest", "running-latest")):
        tag = f"{offset:016x}"
        source_ref = f"{cfg.local_image_repo}:{tag}"
        safety_ref = f"{cfg.local_image_repo}:{alias}"
        _docker(daemon.endpoint, "tag", source_ref, safety_ref, cwd=work_dir)
        safety_refs.append(safety_ref)
        inventory_refs.append(safety_ref)

    current_refs = (
        f"{cfg.local_image_repo}:{0:016x}",
        f"{cfg.published_image_repo}:{0:016x}",
    )
    spec = _ArmSpec(
        image_digest=daemon.image_digest,
        tmpfs_bytes=daemon.tmpfs_bytes,
        seed_bytes=seed_bytes,
        keep_count=cfg.keep_local_count,
        protected_refs=tuple(sorted((*current_refs, *safety_refs))),
        inventory_refs=tuple(sorted(inventory_refs)),
    )
    return spec, identity_ids


def _resolver_preparation(
    *,
    work_dir: Path,
    cfg: MaintenanceDockerConfig,
    payload_bytes: int,
) -> MaintenanceImagePreparation:
    context = work_dir / "resolver-context"
    context.mkdir()
    _write_payload(context / "payload", byte_count=payload_bytes, seed=999)
    (context / "Dockerfile").write_text("FROM scratch\nCOPY payload /payload\n", encoding="utf-8")
    tag = "f" * 16
    return MaintenanceImagePreparation(
        context_dir=context,
        context_metadata={"isolated": True},
        env_hash=tag * 4,
        local_ref=f"{cfg.local_image_repo}:{tag}",
        published_ref=f"{cfg.published_image_repo}:{tag}",
    )


def _run_direct_resolver_arm(
    daemon: _Daemon,
    *,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    cleanup_enabled: bool,
) -> tuple[_ArmSpec, backend_mod.MaintenanceImageResolution | Exception]:
    cfg = _maintenance_config(keep_count=1, cleanup_enabled=cleanup_enabled)
    spec, _ = _seed_burst(daemon, work_dir=work_dir, cfg=cfg)
    monkeypatch.setenv("DOCKER_HOST", daemon.endpoint)
    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
    preparation = _resolver_preparation(work_dir=work_dir, cfg=cfg, payload_bytes=spec.seed_bytes)
    try:
        return spec, backend_mod.resolve_maintenance_docker_image(
            repo_root=work_dir,
            run_dir=work_dir / "run",
            force_rebuild=True,
            timeout_seconds=60,
            preparation=preparation,
        )
    except Exception as exc:  # expected for the no-cleanup baseline
        return spec, exc


def test_direct_resolver_baseline_fails_but_prewrite_recovery_proceeds(
    dind_factory: Callable[[], Iterator[_Daemon]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Compare identical constrained arms around the resolver's real cleanup path."""

    with dind_factory() as baseline_daemon:
        baseline_spec, baseline = _run_direct_resolver_arm(
            baseline_daemon,
            monkeypatch=monkeypatch,
            work_dir=tmp_path / "baseline",
            cleanup_enabled=False,
        )
    with dind_factory() as recovery_daemon:
        recovery_spec, recovery = _run_direct_resolver_arm(
            recovery_daemon,
            monkeypatch=monkeypatch,
            work_dir=tmp_path / "recovery",
            cleanup_enabled=True,
        )

    assert baseline_spec == recovery_spec
    assert isinstance(baseline, Exception), (
        "baseline must fail its first constrained resolver write"
    )
    assert not isinstance(recovery, Exception), "prewrite cleanup must recover resolver capacity"
    assert recovery.image_source == "built"
    assert recovery.metadata["cleanup"]["prewrite"]["managed_tag_bounded"] is True


def test_batch_baseline_fails_scratch_write_but_prewrite_recovery_resolves(
    dind_factory: Callable[[], Iterator[_Daemon]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise run_batch_preflight's actual scratch-build and resolver ordering."""

    def run_arm(daemon: _Daemon, *, enabled: bool, arm_dir: Path):
        cfg = _maintenance_config(keep_count=1, cleanup_enabled=enabled)
        spec, _ = _seed_burst(daemon, work_dir=arm_dir, cfg=cfg)
        monkeypatch.setenv("DOCKER_HOST", daemon.endpoint)
        monkeypatch.setattr(
            batch_preflight,
            "_load_maintenance_docker_config",
            lambda **_kwargs: cfg,
        )
        monkeypatch.setattr(
            backend_mod,
            "_load_maintenance_docker_config",
            lambda **_kwargs: cfg,
        )
        monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)
        monkeypatch.setattr(
            batch_preflight,
            "_batch_remote_handoff_requested",
            lambda **_kwargs: False,
        )
        monkeypatch.setattr(batch_preflight, "_git_branch", lambda _root: "isolated")
        monkeypatch.setattr(batch_preflight, "_git_head", lambda _root: "isolated-head")
        preparation = _resolver_preparation(
            work_dir=arm_dir,
            cfg=cfg,
            payload_bytes=spec.seed_bytes,
        )
        monkeypatch.setattr(
            batch_preflight,
            "prepare_maintenance_docker_image",
            lambda **_kwargs: preparation,
        )
        return spec, batch_preflight.run_batch_preflight(
            repo_root=arm_dir,
            batch_dir=arm_dir / "batch",
            batch_config={
                "defaults": {
                    "require_clean_git": False,
                    "require_local_green": False,
                    "require_ci_green_for_base": False,
                }
            },
            worker_roster=[],
            exec_backend="docker",
            exec_docker_profile="maintenance",
            resolve_maintenance_image=enabled,
            docker_timeout_seconds=60,
        )

    with dind_factory() as baseline_daemon:
        baseline_spec, baseline = run_arm(
            baseline_daemon,
            enabled=False,
            arm_dir=tmp_path / "baseline",
        )
    with dind_factory() as recovery_daemon:
        recovery_spec, recovery = run_arm(
            recovery_daemon,
            enabled=True,
            arm_dir=tmp_path / "recovery",
        )

    assert baseline_spec == recovery_spec
    scratch_failure = "Docker buildx scratch build failed"
    assert any(item["summary"].startswith(scratch_failure) for item in baseline["blockers"])
    assert not any(item["summary"].startswith(scratch_failure) for item in recovery["blockers"])
    assert recovery["maintenance_image_metadata"]["source"] == "built"
    assert recovery["maintenance_image_metadata"]["batch_prewrite"]["managed_tag_bounded"] is True


def test_external_and_container_references_remain_physical_reclamation_blockers(
    dind_factory: Callable[[], Iterator[_Daemon]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """External tags and container references must be reported as mitigated blockers."""

    with dind_factory() as daemon:
        cfg = _maintenance_config(keep_count=1, cleanup_enabled=True)
        spec, identities = _seed_burst(daemon, work_dir=tmp_path, cfg=cfg)
        monkeypatch.setenv("DOCKER_HOST", daemon.endpoint)
        monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
        external_ref = "usertest-isolated-external:keep"
        blocked_tag = f"{3:016x}"
        blocked_ref = f"{cfg.local_image_repo}:{blocked_tag}"
        _docker(daemon.endpoint, "tag", blocked_ref, external_ref, cwd=tmp_path)
        container_name = f"usertest-isolated-{uuid4().hex[:12]}"
        _docker(
            daemon.endpoint,
            "create",
            "--name",
            container_name,
            f"{cfg.local_image_repo}:{4:016x}",
            cwd=tmp_path,
        )
        try:
            summary = backend_mod.cleanup_local_maintenance_images(
                repo_root=tmp_path,
                dry_run=False,
                protected_refs=spec.protected_refs,
            )
        finally:
            _docker(daemon.endpoint, "rm", "--force", container_name, cwd=tmp_path, check=False)

        assert identities[blocked_tag] in summary["externally_retained_image_ids"]
        assert summary["externally_retained_refs"][identities[blocked_tag]] == [external_ref]
        assert identities[f"{4:016x}"] in summary["retained_candidate_image_ids"]
        assert summary["physical_identity_bounded"] is False
        assert summary["bounded"] is False
