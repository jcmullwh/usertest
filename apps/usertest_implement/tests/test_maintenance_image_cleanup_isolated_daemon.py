"""Opt-in, paired isolated-DIND application-integration evidence.

This is deliberately not an ordinary Docker test.  It refuses every default or
non-loopback route and provisions disposable, digest-pinned daemons only after
an explicit opt-in.  The scenarios are retained as the live-evidence contract;
they are not executed by the normal test suite.
"""

import ipaddress
import os
import random
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import runner_core.execution_backend as backend_mod
from runner_core.execution_backend import MaintenanceDockerConfig, MaintenanceImagePreparation

from usertest_implement import batch_preflight

# Capture the unpatched production callable at import time.  Paired arms must
# never capture another arm's monkeypatched observer as their "real" cleanup.
_TRUE_PRODUCTION_CLEANUP = backend_mod.cleanup_local_maintenance_images

_OPT_IN = os.environ.get("USERTEST_RUN_ISOLATED_DOCKER_TESTS") == "1"
_ISOLATION_ACK = os.environ.get("USERTEST_ISOLATED_DOCKER_DAEMON") == "1"
_PROVISIONER = os.environ.get("USERTEST_DIND_PROVISIONER_HOST")
_DIND_IMAGE = os.environ.get("USERTEST_ISOLATED_DIND_IMAGE")
_TMPFS_BYTES = os.environ.get("USERTEST_ISOLATED_DIND_TMPFS_BYTES")
_NO_SPACE = re.compile(r"(?:no space left|enospc)", re.IGNORECASE)
_DOCKER_ROUTING_VARS = (
    "DOCKER_CONTEXT",
    "BUILDX_BUILDER",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)

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
    daemon_id: str


@dataclass(frozen=True)
class _ArmSpec:
    image_digest: str
    tmpfs_bytes: int
    seed_bytes: int
    keep_count: int
    protected_alias_groups: tuple[tuple[str, ...], ...]
    inventory_alias_groups: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class _Seed:
    spec: _ArmSpec
    protected_ref_to_id: dict[str, str]
    ordinary_ref_to_id: dict[str, str]
    free_before_seed: int
    free_after_seed: int
    resolver_write_bytes: int
    scratch_write_bytes: int


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
    endpoint: str, *args: str, cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Use an explicit endpoint for provisioner and independent attestations."""

    return subprocess.run(
        ["docker", "--host", endpoint, *args],
        cwd=str(cwd),
        capture_output=True,
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _plain_docker(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Production subprocesses use this route after _bind_arm_environment."""

    return subprocess.run(
        ["docker", *args],
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
    """Provision one DIND via an explicit loopback TCP provisioner endpoint."""

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
            info = _docker(target, "info", "--format", "{{.ID}}", cwd=cwd, check=False)
            if info.returncode == 0 and info.stdout.strip():
                yield _Daemon(
                    provisioner,
                    target,
                    container_id,
                    image_digest,
                    tmpfs_bytes,
                    info.stdout.strip(),
                )
                return
            time.sleep(0.5)
        raise AssertionError(f"DIND daemon did not become ready at {target}")
    finally:
        _docker(provisioner, "rm", "--force", container_id, cwd=cwd, check=False)


@pytest.fixture
def dind_factory(tmp_path: Path) -> Callable[[], Iterator[_Daemon]]:
    return lambda: _disposable_dind(cwd=tmp_path)


def _bind_arm_environment(
    monkeypatch: pytest.MonkeyPatch, daemon: _Daemon, arm_dir: Path
) -> None:
    """Make plain Docker/buildx calls, including production calls, target this DIND."""

    docker_config = arm_dir / "empty-docker-config"
    docker_config.mkdir(parents=True, exist_ok=False)
    for key in _DOCKER_ROUTING_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_HOST", daemon.endpoint)
    plain_id = _plain_docker("info", "--format", "{{.ID}}", cwd=arm_dir).stdout.strip()
    addressed_id = _docker(
        daemon.endpoint, "info", "--format", "{{.ID}}", cwd=arm_dir
    ).stdout.strip()
    assert plain_id == addressed_id == daemon.daemon_id, "arm routing did not reach its DIND"


def _write_payload(path: Path, *, byte_count: int, seed: int) -> None:
    rng = random.Random(seed)
    with path.open("wb") as handle:
        remaining = byte_count
        while remaining:
            chunk_size = min(1024 * 1024, remaining)
            handle.write(rng.randbytes(chunk_size))
            remaining -= chunk_size


def _scratch_context(work_dir: Path, *, name: str, payload_bytes: int, seed: int) -> Path:
    context = work_dir / name
    context.mkdir(parents=True)
    _write_payload(context / "payload", byte_count=payload_bytes, seed=seed)
    (context / "Dockerfile").write_text(
        "FROM scratch\nCOPY payload /payload\n",
        encoding="utf-8",
    )
    return context


def _build_scratch_identity(
    daemon: _Daemon,
    *,
    work_dir: Path,
    repository: str,
    tag: str,
    payload_bytes: int,
    seed: int,
) -> str:
    context = _scratch_context(
        work_dir, name=f"context-{tag}", payload_bytes=payload_bytes, seed=seed
    )
    ref = f"{repository}:{tag}"
    _docker(daemon.endpoint, "build", "--no-cache", "--tag", ref, str(context), cwd=work_dir)
    return _image_id(daemon, ref, cwd=work_dir)


def _image_id(daemon: _Daemon, ref: str, *, cwd: Path) -> str:
    return _docker(
        daemon.endpoint, "image", "inspect", "--format", "{{.Id}}", ref, cwd=cwd
    ).stdout.strip()


def _image_exists(daemon: _Daemon, image_id: str, *, cwd: Path) -> bool:
    result = _docker(daemon.endpoint, "image", "inspect", image_id, cwd=cwd, check=False)
    return result.returncode == 0


def _free_bytes(daemon: _Daemon, *, cwd: Path) -> int:
    output = _docker(
        daemon.provisioner,
        "exec",
        daemon.container_id,
        "sh",
        "-ec",
        "df -B1 --output=avail /var/lib/docker | tail -n 1",
        cwd=cwd,
    ).stdout.strip()
    try:
        return int(output)
    except ValueError as exc:
        raise AssertionError(f"could not measure DIND free bytes: {output!r}") from exc


def _remove_image_only_cache(daemon: _Daemon, *, cwd: Path) -> None:
    """Eliminate probe/seed build cache only inside the disposable fixture."""

    _docker(daemon.endpoint, "builder", "prune", "--force", cwd=cwd, check=False)
    _docker(daemon.endpoint, "buildx", "prune", "--force", cwd=cwd, check=False)


def _attest_buildx_output(daemon: _Daemon, *, work_dir: Path) -> None:
    """Prove buildx --load writes to the same daemon used by production calls."""

    ref = f"usertest-isolated-attestation:{uuid4().hex[:12]}"
    context = _scratch_context(
        work_dir, name=f"buildx-{uuid4().hex}", payload_bytes=1024, seed=17
    )
    _plain_docker(
        "buildx", "build", "--load", "--no-cache", "--tag", ref, str(context), cwd=work_dir
    )
    plain_id = _plain_docker(
        "image", "inspect", "--format", "{{.Id}}", ref, cwd=work_dir
    ).stdout.strip()
    addressed_id = _image_id(daemon, ref, cwd=work_dir)
    assert plain_id == addressed_id, "buildx output did not land on the arm target daemon"
    _docker(daemon.endpoint, "image", "rm", "--force", ref, cwd=work_dir)
    _remove_image_only_cache(daemon, cwd=work_dir)


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
        # Paired arms vary this single policy switch.  The configured prepare
        # policy remains identical so neither arm gains a second intervention.
        cleanup_on_prepare=True,
        cleanup_dry_run_default=False,
    )


def _ref_to_id(daemon: _Daemon, refs: list[str], *, cwd: Path) -> dict[str, str]:
    return {ref: _image_id(daemon, ref, cwd=cwd) for ref in sorted(refs)}


def _alias_groups(ref_to_id: dict[str, str]) -> tuple[tuple[str, ...], ...]:
    groups: dict[str, list[str]] = {}
    for ref, image_id in ref_to_id.items():
        groups.setdefault(image_id, []).append(ref)
    return tuple(sorted(tuple(sorted(refs)) for refs in groups.values()))


def _resolver_preparation(
    *, work_dir: Path, cfg: MaintenanceDockerConfig, payload_bytes: int
) -> MaintenanceImagePreparation:
    context = _scratch_context(
        work_dir, name="resolver-context", payload_bytes=payload_bytes, seed=999
    )
    tag = "f" * 16
    return MaintenanceImagePreparation(
        context_dir=context,
        context_metadata={"isolated": True},
        env_hash=tag * 4,
        local_ref=f"{cfg.local_image_repo}:{tag}",
        published_ref=f"{cfg.published_image_repo}:{tag}",
    )


def _measure_resolver_write_bytes(
    daemon: _Daemon,
    *,
    work_dir: Path,
    payload_bytes: int,
    seed: int,
) -> int:
    """Measure the resolver's real two-tag ``docker build`` storage write."""

    before = _free_bytes(daemon, cwd=work_dir)
    context = _scratch_context(
        work_dir, name=f"probe-{seed}", payload_bytes=payload_bytes, seed=seed
    )
    local_ref = "usertest-isolated-probe:resolver-local"
    published_ref = "usertest-isolated-probe:resolver-published"
    _plain_docker(
        "build",
        "--progress=plain",
        "-t",
        local_ref,
        "-t",
        published_ref,
        "-f",
        "Dockerfile",
        ".",
        cwd=context,
    )
    _remove_image_only_cache(daemon, cwd=work_dir)
    after = _free_bytes(daemon, cwd=work_dir)
    _docker(
        daemon.endpoint,
        "image",
        "rm",
        "--force",
        local_ref,
        published_ref,
        cwd=work_dir,
    )
    _remove_image_only_cache(daemon, cwd=work_dir)
    reclaimed = _free_bytes(daemon, cwd=work_dir)
    if before <= after or reclaimed < after:
        pytest.fail("invalid capacity calibration: probe did not consume measurable image storage")
    return before - after


def _measure_batch_scratch_write_bytes(daemon: _Daemon, *, work_dir: Path) -> int:
    """Measure the exact buildx scratch operation used by batch preflight."""

    before = _free_bytes(daemon, cwd=work_dir)
    context = work_dir / "batch-scratch-probe"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\nCOPY sentinel /sentinel\n", encoding="utf-8")
    (context / "sentinel").write_text("ok\n", encoding="utf-8")
    ref = "usertest-batch-preflight:latest"
    _plain_docker(
        "buildx", "build", "--progress=plain", "--load", "-t", ref, str(context), cwd=work_dir
    )
    _remove_image_only_cache(daemon, cwd=work_dir)
    after = _free_bytes(daemon, cwd=work_dir)
    _docker(daemon.endpoint, "image", "rm", "--force", ref, cwd=work_dir)
    _remove_image_only_cache(daemon, cwd=work_dir)
    reclaimed = _free_bytes(daemon, cwd=work_dir)
    if before <= after or reclaimed < after:
        pytest.fail("invalid capacity calibration: batch scratch did not consume image storage")
    return before - after


def _seed_burst(daemon: _Daemon, *, work_dir: Path, cfg: MaintenanceDockerConfig) -> _Seed:
    """Calibrate measured image pressure; never rely on a fixed percentage payload."""

    work_dir.mkdir(parents=True, exist_ok=True)
    _attest_buildx_output(daemon, work_dir=work_dir)
    free_before_seed = _free_bytes(daemon, cwd=work_dir)
    probe_bytes = max(1024 * 1024, min(4 * 1024 * 1024, free_before_seed // 16))
    resolver_write = _measure_resolver_write_bytes(
        daemon,
        work_dir=work_dir,
        payload_bytes=probe_bytes,
        seed=101,
    )
    scratch_write = _measure_batch_scratch_write_bytes(daemon, work_dir=work_dir)
    write_requirement = max(resolver_write, scratch_write)
    seed_count = cfg.keep_local_count + 6
    target_consumption = free_before_seed - max(1, write_requirement // 2)
    bytes_per_seed = probe_bytes * target_consumption // (resolver_write * seed_count)
    if not 1024 <= bytes_per_seed < free_before_seed:
        pytest.fail("invalid capacity calibration: seed payload cannot be constructed")

    all_refs: list[str] = []
    ordinary_refs: list[str] = []
    for number in range(seed_count):
        tag = f"{number:016x}"
        _build_scratch_identity(
            daemon,
            work_dir=work_dir,
            repository=cfg.local_image_repo,
            tag=tag,
            payload_bytes=bytes_per_seed,
            seed=number,
        )
        local_ref = f"{cfg.local_image_repo}:{tag}"
        published_ref = f"{cfg.published_image_repo}:{tag}"
        _docker(daemon.endpoint, "tag", local_ref, published_ref, cwd=work_dir)
        ordinary_refs.extend((local_ref, published_ref))
        all_refs.extend((local_ref, published_ref))

    safety_refs: list[str] = []
    for offset, alias in enumerate(("required-latest", "protected-latest", "running-latest")):
        source = f"{cfg.local_image_repo}:{offset:016x}"
        safety_ref = f"{cfg.local_image_repo}:{alias}"
        _docker(daemon.endpoint, "tag", source, safety_ref, cwd=work_dir)
        safety_refs.append(safety_ref)
        all_refs.append(safety_ref)
    # The capacity condition is image-only: discard all seed-build cache before
    # recording the headroom used by the paired baseline/recovery operations.
    _remove_image_only_cache(daemon, cwd=work_dir)
    protected_hash_aliases = [
        ref
        for number in range(3)
        for ref in (
            f"{cfg.local_image_repo}:{number:016x}",
            f"{cfg.published_image_repo}:{number:016x}",
        )
    ]
    protected_refs = [*protected_hash_aliases, *safety_refs]
    protected_ref_to_id = _ref_to_id(daemon, protected_refs, cwd=work_dir)
    ordinary_ref_to_id = _ref_to_id(daemon, ordinary_refs, cwd=work_dir)
    free_after_seed = _free_bytes(daemon, cwd=work_dir)
    if free_after_seed >= write_requirement:
        pytest.fail(
            "invalid capacity calibration: seed left enough space for the exact baseline write"
        )
    spec = _ArmSpec(
        image_digest=daemon.image_digest,
        tmpfs_bytes=daemon.tmpfs_bytes,
        seed_bytes=bytes_per_seed,
        keep_count=cfg.keep_local_count,
        protected_alias_groups=_alias_groups(protected_ref_to_id),
        inventory_alias_groups=_alias_groups(_ref_to_id(daemon, all_refs, cwd=work_dir)),
    )
    return _Seed(
        spec=spec,
        protected_ref_to_id=protected_ref_to_id,
        ordinary_ref_to_id=ordinary_ref_to_id,
        free_before_seed=free_before_seed,
        free_after_seed=free_after_seed,
        resolver_write_bytes=resolver_write,
        scratch_write_bytes=scratch_write,
    )


def _assert_no_space_log(path: Path) -> None:
    assert path.is_file(), f"missing operation log: {path}"
    assert _NO_SPACE.search(path.read_text(encoding="utf-8", errors="replace")), (
        f"baseline operation did not fail with ENOSPC/no-space evidence: {path}"
    )


def _assert_recovery_inventory(
    daemon: _Daemon,
    *,
    work_dir: Path,
    seed: _Seed,
    cleanup: dict[str, object],
) -> None:
    """Independently inspect the live daemon rather than trusting cleanup claims."""

    actual_protected = _ref_to_id(daemon, list(seed.protected_ref_to_id), cwd=work_dir)
    assert actual_protected == seed.protected_ref_to_id
    candidate_ids = cleanup["candidate_image_ids"]
    assert isinstance(candidate_ids, list)
    for image_id in candidate_ids:
        assert not _image_exists(daemon, image_id, cwd=work_dir), (
            f"eligible overflow identity still exists after recovery: {image_id}"
        )
    assert cleanup["physical_identity_bounded"] is True
    assert cleanup["managed_tag_bounded"] is True
    assert cleanup["errors"] == []
    remaining = cleanup["remaining_ordinary_image_ids"]
    assert isinstance(remaining, list)
    assert len(remaining) <= seed.spec.keep_count


def _assert_selected_aliases(daemon: _Daemon, *, cwd: Path, cfg: MaintenanceDockerConfig) -> None:
    """The resolver's current local/published aliases must share one live ID."""

    tag = "f" * 16
    aliases = (
        f"{cfg.local_image_repo}:{tag}",
        f"{cfg.published_image_repo}:{tag}",
    )
    assert len(set(_ref_to_id(daemon, list(aliases), cwd=cwd).values())) == 1


def _run_direct_resolver_arm(
    daemon: _Daemon,
    *,
    monkeypatch: pytest.MonkeyPatch,
    true_cleanup: Callable[..., dict[str, object]],
    work_dir: Path,
    cleanup_enabled: bool,
) -> tuple[_Seed, backend_mod.MaintenanceImageResolution | RuntimeError, list[int]]:
    cfg = _maintenance_config(keep_count=1, cleanup_enabled=cleanup_enabled)
    _bind_arm_environment(monkeypatch, daemon, work_dir)
    seed = _seed_burst(daemon, work_dir=work_dir, cfg=cfg)
    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
    cleanup_headroom: list[int] = []

    def observed_cleanup(**kwargs: object) -> dict[str, object]:
        # Exactly one call to the immutable production function per observer
        # invocation; this observer only closes over its own live daemon/workdir.
        result = true_cleanup(**kwargs)
        cleanup_headroom.append(_free_bytes(daemon, cwd=work_dir))
        return result

    monkeypatch.setattr(backend_mod, "cleanup_local_maintenance_images", observed_cleanup)
    preparation = _resolver_preparation(
        work_dir=work_dir, cfg=cfg, payload_bytes=seed.spec.seed_bytes
    )
    try:
        result = backend_mod.resolve_maintenance_docker_image(
            repo_root=work_dir,
            run_dir=work_dir / "run",
            force_rebuild=True,
            timeout_seconds=60,
            preparation=preparation,
        )
    except RuntimeError as exc:  # The calibrated baseline must fail its exact build write.
        return seed, exc, cleanup_headroom
    return seed, result, cleanup_headroom


def test_direct_resolver_baseline_fails_but_prewrite_recovery_proceeds(
    dind_factory: Callable[[], Iterator[_Daemon]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only cleanup policy differs between calibrated direct-resolver arms."""

    true_cleanup = _TRUE_PRODUCTION_CLEANUP
    with dind_factory() as baseline_daemon:
        with monkeypatch.context() as baseline_patch:
            baseline_seed, baseline, baseline_headroom = _run_direct_resolver_arm(
                baseline_daemon,
                monkeypatch=baseline_patch,
                true_cleanup=true_cleanup,
                work_dir=tmp_path / "baseline",
                cleanup_enabled=False,
            )
            _assert_no_space_log(
                tmp_path / "baseline" / "run" / "sandbox" / "maintenance_docker_build.log"
            )
    with dind_factory() as recovery_daemon:
        with monkeypatch.context() as recovery_patch:
            recovery_seed, recovery, recovery_headroom = _run_direct_resolver_arm(
                recovery_daemon,
                monkeypatch=recovery_patch,
                true_cleanup=true_cleanup,
                work_dir=tmp_path / "recovery",
                cleanup_enabled=True,
            )
            assert not isinstance(recovery, RuntimeError), "cleanup must restore resolver capacity"
            assert recovery.image_source == "built"
            cleanup = recovery.metadata["cleanup"]["prewrite"]
            assert recovery_seed.free_after_seed < recovery_seed.resolver_write_bytes
            assert recovery_headroom[0] >= recovery_seed.resolver_write_bytes
            assert len(recovery_headroom) == 2
            _assert_recovery_inventory(
                recovery_daemon,
                work_dir=tmp_path / "recovery",
                seed=recovery_seed,
                cleanup=cleanup,
            )
            _assert_selected_aliases(
                recovery_daemon,
                cwd=tmp_path / "recovery",
                cfg=_maintenance_config(keep_count=1, cleanup_enabled=True),
            )

    assert baseline_seed.spec == recovery_seed.spec
    assert isinstance(baseline, RuntimeError), "baseline must fail the resolver storage write"
    assert baseline_headroom == []


def _run_batch_arm(
    daemon: _Daemon,
    *,
    monkeypatch: pytest.MonkeyPatch,
    true_cleanup: Callable[..., dict[str, object]],
    arm_dir: Path,
    cleanup_enabled: bool,
) -> tuple[_Seed, dict[str, object], list[int]]:
    cfg = _maintenance_config(keep_count=1, cleanup_enabled=cleanup_enabled)
    _bind_arm_environment(monkeypatch, daemon, arm_dir)
    seed = _seed_burst(daemon, work_dir=arm_dir, cfg=cfg)
    preparation = _resolver_preparation(
        work_dir=arm_dir, cfg=cfg, payload_bytes=seed.spec.seed_bytes
    )
    monkeypatch.setattr(batch_preflight, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)
    monkeypatch.setattr(batch_preflight, "_batch_remote_handoff_requested", lambda **_kwargs: False)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _root: "isolated")
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _root: "isolated-head")
    monkeypatch.setattr(
        batch_preflight, "prepare_maintenance_docker_image", lambda **_kwargs: preparation
    )
    cleanup_headroom: list[int] = []

    def observed_cleanup(**kwargs: object) -> dict[str, object]:
        # Do not read a mutable module attribute: that can be a prior arm's
        # observer after pytest's shared monkeypatch fixture has been used.
        result = true_cleanup(**kwargs)
        cleanup_headroom.append(_free_bytes(daemon, cwd=arm_dir))
        return result

    monkeypatch.setattr(backend_mod, "cleanup_local_maintenance_images", observed_cleanup)
    monkeypatch.setattr(batch_preflight, "cleanup_local_maintenance_images", observed_cleanup)
    result = batch_preflight.run_batch_preflight(
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
        resolve_maintenance_image=True,
        docker_timeout_seconds=60,
    )
    return seed, result, cleanup_headroom


def test_batch_baseline_fails_scratch_write_but_prewrite_recovery_resolves(
    dind_factory: Callable[[], Iterator[_Daemon]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Both arms request resolution; cleanup policy is their sole intervention."""

    true_cleanup = _TRUE_PRODUCTION_CLEANUP
    with dind_factory() as baseline_daemon:
        with monkeypatch.context() as baseline_patch:
            baseline_seed, baseline, baseline_headroom = _run_batch_arm(
                baseline_daemon,
                monkeypatch=baseline_patch,
                true_cleanup=true_cleanup,
                arm_dir=tmp_path / "baseline",
                cleanup_enabled=False,
            )
            assert any(
                "Docker buildx scratch build failed" in item["summary"]
                for item in baseline["blockers"]
            )
            _assert_no_space_log(
                tmp_path / "baseline" / "batch" / "preflight" / "docker_build.log"
            )
    with dind_factory() as recovery_daemon:
        with monkeypatch.context() as recovery_patch:
            recovery_seed, recovery, recovery_headroom = _run_batch_arm(
                recovery_daemon,
                monkeypatch=recovery_patch,
                true_cleanup=true_cleanup,
                arm_dir=tmp_path / "recovery",
                cleanup_enabled=True,
            )
            assert recovery["blockers"] == []
            metadata = recovery["maintenance_image_metadata"]
            assert metadata["source"] == "built"
            cleanup = metadata["batch_prewrite"]
            assert recovery_headroom[0] >= recovery_seed.scratch_write_bytes
            assert len(recovery_headroom) == 2
            _assert_recovery_inventory(
                recovery_daemon,
                work_dir=tmp_path / "recovery",
                seed=recovery_seed,
                cleanup=cleanup,
            )
            _assert_selected_aliases(
                recovery_daemon,
                cwd=tmp_path / "recovery",
                cfg=_maintenance_config(keep_count=1, cleanup_enabled=True),
            )
            assert (tmp_path / "recovery" / "batch" / "preflight" / "docker_build.log").is_file()

    assert baseline_seed.spec == recovery_seed.spec
    assert baseline_headroom == []


def test_external_and_container_references_remain_physical_reclamation_blockers(
    dind_factory: Callable[[], Iterator[_Daemon]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Assert the exact external and stopped-container causes of blocked cleanup."""

    with dind_factory() as daemon:
        cfg = _maintenance_config(keep_count=1, cleanup_enabled=True)
        _bind_arm_environment(monkeypatch, daemon, tmp_path)
        seed = _seed_burst(daemon, work_dir=tmp_path, cfg=cfg)
        monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
        external_tag = f"{3:016x}"
        container_tag = f"{4:016x}"
        external_source = f"{cfg.local_image_repo}:{external_tag}"
        container_source = f"{cfg.local_image_repo}:{container_tag}"
        external_ref = "usertest-isolated-external:keep"
        _docker(daemon.endpoint, "tag", external_source, external_ref, cwd=tmp_path)
        external_id = _image_id(daemon, external_ref, cwd=tmp_path)
        assert external_id == seed.ordinary_ref_to_id[external_source]
        container_name = f"usertest-isolated-{uuid4().hex[:12]}"
        create_proc = _docker(
            daemon.endpoint,
            "create",
            "--name",
            container_name,
            container_source,
            "/explicit-stopped-container-command",
            cwd=tmp_path,
        )
        created_container_id = create_proc.stdout.strip()
        assert created_container_id, "docker create did not return a container ID"
        container_image_id = _image_id(daemon, container_source, cwd=tmp_path)
        container_image = _docker(
            daemon.endpoint,
            "inspect",
            "--format",
            "{{.Image}}",
            created_container_id,
            cwd=tmp_path,
        ).stdout.strip()
        assert container_image == container_image_id
        try:
            summary = backend_mod.cleanup_local_maintenance_images(
                repo_root=tmp_path,
                dry_run=False,
                protected_refs=tuple(seed.protected_ref_to_id),
            )
        finally:
            _docker(
                daemon.endpoint,
                "rm",
                "--force",
                created_container_id,
                cwd=tmp_path,
                check=False,
            )

        assert external_id in summary["candidate_image_ids"]
        assert container_image_id in summary["candidate_image_ids"]
        assert summary["externally_retained_refs"][external_id] == [external_ref]
        assert container_image_id in summary["retained_candidate_image_ids"]
        assert summary["physical_candidate_status"][container_image_id]["exists"] is True
        container_prefix = created_container_id[:12]
        candidate_aliases = {
            container_source,
            f"{cfg.published_image_repo}:{container_tag}",
        }
        container_errors = [
            error
            for error in summary["errors"]
            if "container" in error.lower()
            and (created_container_id in error or container_prefix in error)
            and any(alias in error for alias in candidate_aliases)
        ]
        assert container_errors, "cleanup did not report the exact blocking container"
        assert _image_exists(daemon, external_id, cwd=tmp_path)
        assert _image_exists(daemon, container_image_id, cwd=tmp_path)
        assert summary["physical_identity_bounded"] is False
        assert summary["bounded"] is False
