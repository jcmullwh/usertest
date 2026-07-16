"""Opt-in, paired isolated-DIND application-integration evidence.

This is deliberately not an ordinary Docker test.  It refuses every default or
non-loopback route and provisions disposable, digest-pinned daemons only after
an explicit opt-in.  The scenarios are retained as the live-evidence contract;
they are not executed by the normal test suite.
"""

import io
import ipaddress
import json
import os
import random
import re
import subprocess
import sys
import tarfile
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

# This is deliberately a repository-level integration test.  The exact live
# evidence nodes are owned by runner_core, while batch preflight is the
# application boundary whose prewrite ordering they must exercise.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_IMPLEMENT_APP_SOURCE = _REPOSITORY_ROOT / "apps" / "usertest_implement" / "src"
if str(_IMPLEMENT_APP_SOURCE) not in sys.path:
    sys.path.insert(0, str(_IMPLEMENT_APP_SOURCE))

from usertest_implement import batch_preflight  # noqa: E402

# Capture the unpatched production callable at import time.  Paired arms must
# never capture another arm's monkeypatched observer as their "real" cleanup.
_TRUE_PRODUCTION_CLEANUP = backend_mod.cleanup_local_maintenance_images

_OPT_IN = os.environ.get("USERTEST_RUN_ISOLATED_DOCKER_TESTS") == "1"
_ISOLATION_ACK = os.environ.get("USERTEST_ISOLATED_DOCKER_DAEMON") == "1"
_PROVISIONER = os.environ.get("USERTEST_DIND_PROVISIONER_HOST")
_DIND_IMAGE = os.environ.get("USERTEST_ISOLATED_DIND_IMAGE")
_TMPFS_BYTES = os.environ.get("USERTEST_ISOLATED_DIND_TMPFS_BYTES")
_NO_SPACE = re.compile(r"(?:no space left|enospc)", re.IGNORECASE)
_BATCH_SCRATCH_PAYLOAD_BYTES = 4 * 1024 * 1024
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
        "--pull",
        "never",
        "--tmpfs",
        f"/var/lib/docker:rw,size={tmpfs_bytes}",
        "--env",
        "DOCKER_TLS_CERTDIR=",
        "--publish",
        "127.0.0.1::2375",
        image_digest,
        "dockerd",
        "--host=tcp://0.0.0.0:2375",
        "--tls=false",
        cwd=cwd,
    )
    container_id = proc.stdout.strip()
    try:
        deadline = time.monotonic() + 60
        target: str | None = None
        last_port_output = ""
        while time.monotonic() < deadline:
            port = _docker(
                provisioner, "port", container_id, "2375/tcp", cwd=cwd, check=False
            )
            last_port_output = f"stdout={port.stdout!r} stderr={port.stderr!r}"
            mappings = [line.strip() for line in port.stdout.splitlines() if line.strip()]
            for mapping in mappings:
                try:
                    target = _loopback_tcp_endpoint(
                        f"tcp://{mapping}", label="published DIND endpoint"
                    )
                    break
                except pytest.UsageError:
                    continue
            if target is not None:
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
        raise AssertionError(
            "DIND daemon did not publish a ready loopback endpoint within 60 seconds; "
            f"last docker port result: {last_port_output}"
        )
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
    context.mkdir(parents=True, exist_ok=True)
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
    """Create a scratch-rootfs image without retaining build cache as pressure."""

    payload = random.Random(seed).randbytes(payload_bytes)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        entry = tarfile.TarInfo("payload")
        entry.size = len(payload)
        entry.mode = 0o644
        entry.mtime = 0
        tar.addfile(entry, io.BytesIO(payload))
    ref = f"{repository}:{tag}"
    proc = subprocess.run(
        ["docker", "--host", daemon.endpoint, "import", "-", ref],
        cwd=str(work_dir),
        input=archive.getvalue(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args, output=proc.stdout, stderr=proc.stderr
        )
    return _image_id(daemon, ref, cwd=work_dir)


def _image_id(daemon: _Daemon, ref: str, *, cwd: Path) -> str:
    return _docker(
        daemon.endpoint, "image", "inspect", "--format", "{{.Id}}", ref, cwd=cwd
    ).stdout.strip()


def _image_exists(daemon: _Daemon, image_id: str, *, cwd: Path) -> bool:
    result = _docker(daemon.endpoint, "image", "inspect", image_id, cwd=cwd, check=False)
    return result.returncode == 0


def _maintenance_inventory_id(image_id: str) -> str:
    """Match Docker's short ID representation used by image ls inventory."""

    return image_id.removeprefix("sha256:")[:12]


def _free_bytes(daemon: _Daemon, *, cwd: Path) -> int:
    """Read available bytes with BusyBox-compatible tools and visible failures."""

    proc = _docker(
        daemon.provisioner,
        "exec",
        daemon.container_id,
        "sh",
        "-ec",
        "set -e; df -Pk /var/lib/docker > /tmp/usertest-df.out; "
        "awk 'NR == 2 { print $4 * 1024 }' /tmp/usertest-df.out",
        cwd=cwd,
    )
    output = proc.stdout.strip()
    try:
        free_bytes = int(output)
    except ValueError as exc:
        raise AssertionError(
            "could not measure DIND free bytes with portable df probe: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        ) from exc
    if free_bytes < 0:
        raise AssertionError(f"portable df probe reported negative free space: {free_bytes}")
    return free_bytes


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


def _measure_batch_scratch_write_bytes(
    daemon: _Daemon, *, work_dir: Path, payload_bytes: int = 3
) -> int:
    """Measure the exact buildx scratch operation used by batch preflight."""

    before = _free_bytes(daemon, cwd=work_dir)
    context = work_dir / "batch-scratch-probe"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\nCOPY sentinel /sentinel\n", encoding="utf-8")
    if payload_bytes == 3:
        (context / "sentinel").write_text("ok\n", encoding="utf-8")
    else:
        _write_payload(context / "sentinel", byte_count=payload_bytes, seed=404)
    ref = "usertest-batch-preflight:latest"
    _plain_docker(
        "buildx", "build", "--progress=plain", "--load", "-t", ref, str(context), cwd=work_dir
    )
    _remove_image_only_cache(daemon, cwd=work_dir)
    after = _free_bytes(daemon, cwd=work_dir)
    _docker(daemon.endpoint, "image", "rm", "--force", ref, cwd=work_dir)
    _remove_image_only_cache(daemon, cwd=work_dir)
    _free_bytes(daemon, cwd=work_dir)
    # The exact production scratch context contains only a two-byte sentinel.
    # BusyBox df reports whole KiB, so a successful scratch build can consume
    # no observable block even though it still needs writable daemon metadata.
    return max(1024, before - after)


def _batch_scratch_has_capacity(
    daemon: _Daemon, *, work_dir: Path, payload_bytes: int = 3
) -> bool:
    """Probe the exact scratch write and retain only a binary capacity result."""

    context = work_dir / "batch-scratch-capacity-probe"
    context.mkdir(exist_ok=True)
    (context / "Dockerfile").write_text("FROM scratch\nCOPY sentinel /sentinel\n", encoding="utf-8")
    if payload_bytes == 3:
        (context / "sentinel").write_text("ok\n", encoding="utf-8")
    else:
        _write_payload(context / "sentinel", byte_count=payload_bytes, seed=405)
    ref = "usertest-batch-preflight:capacity-probe"
    proc = _plain_docker(
        "buildx", "build", "--progress=plain", "--load", "-t", ref, str(context), cwd=work_dir,
        check=False,
    )
    if proc.returncode == 0:
        _docker(daemon.endpoint, "image", "rm", "--force", ref, cwd=work_dir, check=False)
    elif not _NO_SPACE.search(f"{proc.stdout}\n{proc.stderr}"):
        pytest.fail(
            "invalid capacity calibration: scratch probe failed for a reason other than ENOSPC: "
            f"{proc.stderr or proc.stdout}"
        )
    _remove_image_only_cache(daemon, cwd=work_dir)
    return proc.returncode == 0


def _seed_burst(
    daemon: _Daemon,
    *,
    work_dir: Path,
    cfg: MaintenanceDockerConfig,
    batch_scratch_payload_bytes: int = 3,
) -> _Seed:
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
    scratch_write = _measure_batch_scratch_write_bytes(
        daemon, work_dir=work_dir, payload_bytes=batch_scratch_payload_bytes
    )
    write_requirement = max(resolver_write, scratch_write)
    # The diagnostic proves Docker can mutate its repositories metadata at
    # 512 KiB, while the measured operation needs several MiB.  Stop at a
    # measured fraction of the operation rather than an unrelated reserve.
    target_headroom = (write_requirement * 3) // 4
    seed_count = cfg.keep_local_count + 6
    # Keep the retained-image recipe itself independent of a few KiB of
    # daemon bookkeeping variation.  The remaining calibration loop provides
    # the measured final headroom; this fixed initial payload only establishes
    # an identical paired inventory before that loop runs.
    bytes_per_seed = min(
        (probe_bytes * 23) // 4,
        free_before_seed // (seed_count + 4),
    )
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
        # The paired capacity condition is image-only.  Discard every
        # intermediate build cache before adding the next retained identity,
        # otherwise cache accumulation becomes an unmeasured intervention.
        _remove_image_only_cache(daemon, cwd=work_dir)
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

    next_number = seed_count
    while _free_bytes(daemon, cwd=work_dir) > target_headroom:
        available = _free_bytes(daemon, cwd=work_dir)
        filler_bytes = max(
            4096,
            min(bytes_per_seed, (available - target_headroom) // 8),
        )
        tag = f"{next_number:016x}"
        while True:
            try:
                _build_scratch_identity(
                    daemon,
                    work_dir=work_dir,
                    repository=cfg.local_image_repo,
                    tag=tag,
                    payload_bytes=filler_bytes,
                    seed=next_number,
                )
                break
            except subprocess.CalledProcessError:
                _remove_image_only_cache(daemon, cwd=work_dir)
                if _free_bytes(daemon, cwd=work_dir) <= target_headroom:
                    break
                if filler_bytes <= 4096:
                    pytest.fail(
                        "invalid capacity calibration: image-only filler cannot be built "
                        "while the exact scratch write still has capacity"
                    )
                filler_bytes = max(4096, filler_bytes // 2)
        if not _image_exists(daemon, f"{cfg.local_image_repo}:{tag}", cwd=work_dir):
            break
        _remove_image_only_cache(daemon, cwd=work_dir)
        local_ref = f"{cfg.local_image_repo}:{tag}"
        published_ref = f"{cfg.published_image_repo}:{tag}"
        published_tag = _docker(
            daemon.endpoint, "tag", local_ref, published_ref, cwd=work_dir, check=False
        )
        if published_tag.returncode == 0:
            ordinary_refs.extend((local_ref, published_ref))
            all_refs.extend((local_ref, published_ref))
        elif _image_exists(daemon, local_ref, cwd=work_dir):
            # The final local-only identity is a real image-only capacity
            # pressure candidate.  Its paired predecessors already prove the
            # paired-alias accounting; do not discard a successful saturation
            # write merely because the second alias cannot fit.
            ordinary_refs.append(local_ref)
            all_refs.append(local_ref)
            break
        else:
            raise AssertionError(
                "capacity filler build succeeded but its local maintenance ref vanished: "
                f"{published_tag.stderr or published_tag.stdout}"
            )
        next_number += 1

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
    if not 512 * 1024 < free_after_seed < write_requirement:
        pytest.fail(
            "invalid capacity calibration: seed did not leave mutable-but-insufficient "
            f"headroom (available={free_after_seed}, requirement={write_requirement})"
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


def _exercise_direct_resolver_capacity_pair(
    dind_factory: Callable[[], Iterator[_Daemon]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, bool]:
    """Only cleanup policy differs between calibrated direct-resolver arms."""

    true_cleanup = _TRUE_PRODUCTION_CLEANUP
    baseline_failed = False
    recovery_prewrite_before_storage = False
    recovery_physical_reclamation = False
    recovery_continued = False
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
            baseline_failed = isinstance(baseline, RuntimeError)
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
            recovery_prewrite_before_storage = (
                len(recovery_headroom) == 2
                and recovery.metadata["cleanup"]["prewrite"] == cleanup
            )
            recovery_physical_reclamation = bool(cleanup["physical_identity_bounded"])
            recovery_cleanup_errors = recovery.metadata["cleanup"]["errors"]
            recovery_continued = recovery.image_source == "built" and not recovery_cleanup_errors

    assert baseline_seed.spec == recovery_seed.spec
    assert isinstance(baseline, RuntimeError), "baseline must fail the resolver storage write"
    assert baseline_headroom == []
    return {
        "direct_pair_seed_fingerprints_equal": baseline_seed.spec == recovery_seed.spec,
        "direct_pair_capacity_fingerprints_equal": (
            baseline_seed.spec.image_digest == recovery_seed.spec.image_digest
            and baseline_seed.spec.tmpfs_bytes == recovery_seed.spec.tmpfs_bytes
        ),
        "direct_baseline_capacity_exhaustion": baseline_failed,
        "resolver_prewrite_before_first_storage_attempt": recovery_prewrite_before_storage,
        "direct_physical_reclamation_verified": recovery_physical_reclamation,
        "direct_recovery_continued": recovery_continued,
    }


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
    seed = _seed_burst(
        daemon,
        work_dir=arm_dir,
        cfg=cfg,
        batch_scratch_payload_bytes=_BATCH_SCRATCH_PAYLOAD_BYTES,
    )
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
        docker_scratch_payload_bytes=_BATCH_SCRATCH_PAYLOAD_BYTES,
    )
    return seed, result, cleanup_headroom


def _exercise_batch_capacity_pair(
    dind_factory: Callable[[], Iterator[_Daemon]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, bool]:
    """Both arms request resolution; cleanup policy is their sole intervention."""

    true_cleanup = _TRUE_PRODUCTION_CLEANUP
    baseline_failed = False
    batch_prewrite_before_scratch = False
    recovery_physical_reclamation = False
    recovery_continued = False
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
            baseline_failed = any(
                "Docker buildx scratch build failed" in item["summary"]
                for item in baseline["blockers"]
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
            scratch_log = tmp_path / "recovery" / "batch" / "preflight" / "docker_build.log"
            prewrite_artifact = (
                tmp_path
                / "recovery"
                / "batch"
                / "preflight"
                / "maintenance_image_batch_prewrite.json"
            )
            assert scratch_log.is_file()
            assert prewrite_artifact.is_file()
            batch_prewrite_before_scratch = (
                prewrite_artifact.stat().st_mtime_ns <= scratch_log.stat().st_mtime_ns
            )
            recovery_physical_reclamation = bool(cleanup["physical_identity_bounded"])
            recovery_continued = not recovery["blockers"] and metadata["source"] == "built"

    assert baseline_seed.spec == recovery_seed.spec
    assert baseline_headroom == []
    return {
        "batch_pair_seed_fingerprints_equal": baseline_seed.spec == recovery_seed.spec,
        "batch_pair_capacity_fingerprints_equal": (
            baseline_seed.spec.image_digest == recovery_seed.spec.image_digest
            and baseline_seed.spec.tmpfs_bytes == recovery_seed.spec.tmpfs_bytes
        ),
        "batch_baseline_capacity_exhaustion": baseline_failed,
        "batch_prewrite_before_scratch_build": batch_prewrite_before_scratch,
        "batch_physical_reclamation_verified": recovery_physical_reclamation,
        "batch_recovery_continued": recovery_continued,
    }


def test_batch_and_direct_capacity_recovery_pairs(
    dind_factory: Callable[[], Iterator[_Daemon]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Prove the contract's paired batch and direct-recovery predicates together."""

    evidence = {
        **_exercise_batch_capacity_pair(
            dind_factory=dind_factory,
            monkeypatch=monkeypatch,
            tmp_path=tmp_path / "batch-pair",
        ),
        **_exercise_direct_resolver_capacity_pair(
            dind_factory=dind_factory,
            monkeypatch=monkeypatch,
            tmp_path=tmp_path / "direct-pair",
        ),
    }
    assert all(evidence.values())
    print(json.dumps(evidence, sort_keys=True))


def _exercise_safe_partial_cleanup_control(
    *,
    dind_factory: Callable[[], Iterator[_Daemon]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise a real tag-removal race whose cleanup can safely continue.

    Docker's tag list can change between cleanup's inventory read and a later
    tag removal.  Make that race explicit against an isolated daemon: one
    managed alias disappears after the real inventory read, the other alias is
    still removed by production cleanup, and the following resolver build must
    succeed from the verified post-cleanup inventory.
    """

    with dind_factory() as daemon:
        with monkeypatch.context() as safe_patch:
            cfg = _maintenance_config(keep_count=1, cleanup_enabled=True)
            work_dir = tmp_path / "safe-partial"
            _bind_arm_environment(safe_patch, daemon, work_dir)
            seed = _seed_burst(
                daemon,
                work_dir=work_dir,
                cfg=cfg,
                batch_scratch_payload_bytes=_BATCH_SCRATCH_PAYLOAD_BYTES,
            )
            safe_patch.setattr(
                backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg
            )
            planned = backend_mod.cleanup_local_maintenance_images(
                repo_root=work_dir,
                dry_run=True,
                protected_refs=tuple(seed.protected_ref_to_id),
            )
            candidate_ids = set(planned["candidate_image_ids"])
            candidate_refs = [
                (ref, image_id)
                for ref, image_id in sorted(seed.ordinary_ref_to_id.items())
                if (
                    ref.startswith(f"{cfg.local_image_repo}:")
                    and _maintenance_inventory_id(image_id) in candidate_ids
                )
            ]
            assert candidate_refs, "safe partial-error control needs an ordinary candidate"
            raced_ref, raced_image_id = candidate_refs[0]
            original_list = backend_mod.list_local_maintenance_images
            race_applied = False

            def list_then_remove_one_managed_alias(**kwargs: object) -> dict[str, object]:
                nonlocal race_applied
                inventory = original_list(**kwargs)
                if not race_applied:
                    removal = _plain_docker("image", "rm", raced_ref, cwd=work_dir)
                    assert removal.returncode == 0, removal.stderr or removal.stdout
                    race_applied = True
                return inventory

            safe_patch.setattr(
                backend_mod, "list_local_maintenance_images", list_then_remove_one_managed_alias
            )
            summary = backend_mod.cleanup_local_maintenance_images(
                repo_root=work_dir,
                dry_run=False,
                protected_refs=tuple(seed.protected_ref_to_id),
            )
            assert race_applied
            assert any(raced_ref in error for error in summary["errors"])
            assert summary["physical_identity_bounded"] is True
            assert summary["managed_tag_bounded"] is True
            assert summary["bounded"] is True
            for image_id in summary["candidate_image_ids"]:
                assert not _image_exists(daemon, image_id, cwd=work_dir)
            # Reinspect every configured/current protected alias after the
            # partial error rather than merely proving it was never selected.
            assert _ref_to_id(daemon, list(seed.protected_ref_to_id), cwd=work_dir) == (
                seed.protected_ref_to_id
            )
            assert not _image_exists(daemon, raced_image_id, cwd=work_dir)

            preparation = _resolver_preparation(
                work_dir=work_dir,
                cfg=cfg,
                payload_bytes=seed.spec.seed_bytes,
            )
            resolution = backend_mod.resolve_maintenance_docker_image(
                repo_root=work_dir,
                run_dir=work_dir / "safe-partial-run",
                force_rebuild=True,
                timeout_seconds=60,
                preparation=preparation,
            )
            assert resolution.image_source == "built"
            assert _ref_to_id(daemon, list(seed.protected_ref_to_id), cwd=work_dir) == (
                seed.protected_ref_to_id
            )
            _assert_selected_aliases(daemon, cwd=work_dir, cfg=cfg)


def test_protected_external_container_and_partial_error_controls(
    dind_factory: Callable[[], Iterator[_Daemon]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise blocked and safe partial-cleanup controls on real isolated DINDs."""

    with dind_factory() as daemon:
        cfg = _maintenance_config(keep_count=1, cleanup_enabled=True)
        _bind_arm_environment(monkeypatch, daemon, tmp_path)
        control_payload_bytes = 32 * 1024 * 1024
        seed = _seed_burst(
            daemon,
            work_dir=tmp_path,
            cfg=cfg,
            batch_scratch_payload_bytes=control_payload_bytes,
        )
        monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
        planned = backend_mod.cleanup_local_maintenance_images(
            repo_root=tmp_path,
            dry_run=True,
            protected_refs=tuple(seed.protected_ref_to_id),
        )
        candidate_inventory_ids = set(planned["candidate_image_ids"])
        candidate_local_refs = [
            (ref, image_id)
            for ref, image_id in sorted(seed.ordinary_ref_to_id.items())
            if (
                ref.startswith(f"{cfg.local_image_repo}:")
                and _maintenance_inventory_id(image_id) in candidate_inventory_ids
            )
        ]
        assert candidate_local_refs, "dry-run cleanup found no ordinary candidate identities"
        container_source, container_image_id = candidate_local_refs[0]
        asserted_candidate_ids = {
            _maintenance_inventory_id(image_id) for _, image_id in candidate_local_refs
        }
        assert asserted_candidate_ids <= candidate_inventory_ids
        protected_inventory_ids = {
            _maintenance_inventory_id(image_id) for image_id in seed.protected_ref_to_id.values()
        }
        assert not asserted_candidate_ids & protected_inventory_ids
        external_refs: dict[str, str] = {}
        for index, (ref, image_id) in enumerate(candidate_local_refs[1:]):
            external_ref = f"usertest-isolated-external:keep-{index:03d}"
            _docker(daemon.endpoint, "tag", ref, external_ref, cwd=tmp_path)
            assert _image_id(daemon, external_ref, cwd=tmp_path) == image_id
            external_refs[image_id] = external_ref
        assert external_refs
        assert {
            _maintenance_inventory_id(image_id) for image_id in external_refs
        } == asserted_candidate_ids - {_maintenance_inventory_id(container_image_id)}
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

        container_inventory_id = _maintenance_inventory_id(container_image_id)
        assert set(summary["candidate_image_ids"]) == candidate_inventory_ids
        assert container_inventory_id in summary["candidate_image_ids"]
        for image_id, external_ref in external_refs.items():
            inventory_id = _maintenance_inventory_id(image_id)
            assert inventory_id in summary["candidate_image_ids"]
            assert summary["externally_retained_refs"][inventory_id] == [external_ref]
        assert container_inventory_id in summary["retained_candidate_image_ids"]
        assert summary["physical_candidate_status"][container_inventory_id]["exists"] is True
        container_prefix = created_container_id[:12]
        candidate_aliases = {
            container_source,
            f"{cfg.published_image_repo}:{container_source.rsplit(':', 1)[1]}",
        }
        container_errors = [
            error
            for error in summary["errors"]
            if "container" in error.lower()
            and (created_container_id in error or container_prefix in error)
            and any(alias in error for alias in candidate_aliases)
        ]
        assert container_errors, "cleanup did not report the exact blocking container"
        for image_id in external_refs:
            assert _image_exists(daemon, image_id, cwd=tmp_path)
        assert _image_exists(daemon, container_image_id, cwd=tmp_path)
        assert summary["physical_identity_bounded"] is False
        assert summary["bounded"] is False
        next_operation_has_capacity = _batch_scratch_has_capacity(
            daemon,
            work_dir=tmp_path,
            payload_bytes=control_payload_bytes,
        )
        endpoint_id = _plain_docker("info", "--format", "{{.ID}}", cwd=tmp_path).stdout.strip()
        evidence = {
            "daemon_endpoint_verified": endpoint_id == daemon.daemon_id,
            "blocked_insufficient_capacity": (
                bool(external_refs)
                and bool(container_errors)
                and summary["physical_identity_bounded"] is False
            ),
            "blocked_next_operation_failed": not next_operation_has_capacity,
        }
        assert all(evidence.values())
        print(json.dumps(evidence, sort_keys=True))

    _exercise_safe_partial_cleanup_control(
        dind_factory=dind_factory,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
