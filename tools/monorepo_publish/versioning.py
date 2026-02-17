from __future__ import annotations

import time
from typing import Mapping

from packaging.version import Version


class VersioningError(ValueError):
    pass


def compute_snapshot_id(env: Mapping[str, str]) -> int:
    explicit = env.get("MONOREPO_SNAPSHOT_ID")
    if explicit is not None:
        try:
            return int(explicit)
        except ValueError as e:
            raise VersioningError("MONOREPO_SNAPSHOT_ID must be numeric.") from e

    run_id = env.get("GITHUB_RUN_ID")
    if run_id is not None:
        try:
            run_id_i = int(run_id)
        except ValueError as e:
            raise VersioningError("GITHUB_RUN_ID must be numeric.") from e
        attempt_raw = env.get("GITHUB_RUN_ATTEMPT") or "1"
        try:
            attempt_i = int(attempt_raw)
        except ValueError as e:
            raise VersioningError("GITHUB_RUN_ATTEMPT must be numeric.") from e
        return run_id_i * 100 + attempt_i

    pipeline_id = env.get("CI_PIPELINE_ID")
    if pipeline_id is not None:
        try:
            return int(pipeline_id)
        except ValueError as e:
            raise VersioningError("CI_PIPELINE_ID must be numeric.") from e

    return int(time.time())


def snapshot_version(base_version: str, snapshot_id: int) -> str:
    if snapshot_id < 0:
        raise VersioningError("snapshot_id must be non-negative.")
    base = Version(base_version)  # validates
    release = ".".join(str(x) for x in base.release)
    prefix = f"{base.epoch}!" if base.epoch else ""
    return f"{prefix}{release}.dev{snapshot_id}"
