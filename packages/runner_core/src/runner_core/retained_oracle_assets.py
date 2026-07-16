from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from runner_core.agent_prompt_files import _resolve_git_dir

RETAINED_ORACLE_AGENT_NOTE = """# Runner-owned retained research asset boundary

An authenticated retained research replay asset is bound to this ticket. The runner will
stage it only after a successful agent/report turn, immediately before runner-owned
post-agent verification. Do not create, edit, or delete `.usertest_research` paths. Use
the repository's ordinary tests while iterating; the retained replay is runner-owned.
"""


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validated_relative_path(raw: Any, *, label: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ValueError(f"{label}_unsafe")
    relative = PurePosixPath(raw)
    if (
        not relative.parts
        or relative.is_absolute()
        or PureWindowsPath(raw).is_absolute()
        or (relative.parts and ":" in relative.parts[0])
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != raw
    ):
        raise ValueError(f"{label}_unsafe")
    return relative


def _validated_manifest(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("retained_oracle_asset_manifest_invalid")
    manifest: dict[str, dict[str, Any]] = {}
    for raw_path, raw_entry in raw.items():
        relative = _validated_relative_path(
            raw_path,
            label="retained_oracle_asset_manifest_path",
        )
        if relative.parts[0] != ".usertest_research" or len(relative.parts) < 2:
            raise ValueError("retained_oracle_asset_manifest_path_unsafe")
        if not isinstance(raw_entry, Mapping):
            raise ValueError("retained_oracle_asset_manifest_entry_invalid")
        entry = dict(raw_entry)
        mode = entry.get("mode")
        size_bytes = entry.get("size_bytes")
        digest = entry.get("sha256")
        if (
            set(entry) != {"kind", "mode", "sha256", "size_bytes"}
            or entry.get("kind") != "file"
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode < 0
            or mode > 0o7777
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError("retained_oracle_asset_manifest_entry_invalid")
        manifest[relative.as_posix()] = entry
    return manifest


def _observed_manifest(root: Path) -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"retained_oracle_asset_entry_unsafe:{relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"retained_oracle_asset_entry_unsafe:{relative}")
        observed[relative] = {
            "kind": "file",
            "mode": stat.S_IMODE(path.stat().st_mode),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
    return observed


def nearest_existing_runs_ancestor(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    cursor = resolved if resolved.is_dir() else resolved.parent
    for candidate in (cursor, *cursor.parents):
        if candidate.name.casefold() == "runs" and candidate.is_dir():
            return candidate
    raise ValueError("retained_oracle_asset_trusted_runs_root_missing")


def resolve_retained_oracle_asset_transport(
    *,
    verification_contract: Mapping[str, Any] | None,
    tickets_export_path: Path | None,
) -> tuple[Path, dict[str, Any]] | None:
    if not isinstance(verification_contract, Mapping):
        return None
    roles = verification_contract.get("outcome_roles")
    original = roles.get("original_scenario") if isinstance(roles, Mapping) else None
    oracle = original.get("oracle") if isinstance(original, Mapping) else None
    asset = oracle.get("asset") if isinstance(oracle, Mapping) else None
    if (
        not isinstance(oracle, Mapping)
        or oracle.get("kind") not in {"staged_replay", "causal_proof_replay"}
        or not isinstance(asset, Mapping)
    ):
        return None
    oracle_projection = {
        key: value for key, value in oracle.items() if key != "outcome_oracle_id"
    }
    oracle_id = oracle.get("outcome_oracle_id")
    if oracle_id != f"outcome_oracle:{_sha256_json(oracle_projection)}":
        raise ValueError("retained_oracle_asset_oracle_hash_invalid")
    if tickets_export_path is None:
        raise ValueError("retained_oracle_asset_tickets_export_required")
    trusted_root = nearest_existing_runs_ancestor(tickets_export_path)
    projection = {
        "schema_version": 1,
        "role": "original_scenario",
        "outcome_oracle_id": oracle_id,
        "oracle_kind": oracle.get("kind"),
        "oracle_repo_revision": oracle.get("repo_revision"),
        "asset": deepcopy(dict(asset)),
    }
    spec = {**projection, "transport_sha256": _sha256_json(projection)}
    validate_retained_oracle_asset_source(spec=spec, trusted_runs_root=trusted_root)
    return trusted_root, spec


def _validated_transport_spec(raw: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not isinstance(raw, Mapping):
        raise ValueError("retained_oracle_asset_transport_invalid")
    spec = deepcopy(dict(raw))
    transport_sha256 = spec.pop("transport_sha256", None)
    if (
        spec.get("schema_version") != 1
        or spec.get("role") != "original_scenario"
        or spec.get("oracle_kind") not in {"staged_replay", "causal_proof_replay"}
        or not isinstance(spec.get("outcome_oracle_id"), str)
        or transport_sha256 != _sha256_json(spec)
    ):
        raise ValueError("retained_oracle_asset_transport_invalid")
    asset = spec.get("asset")
    if not isinstance(asset, Mapping):
        raise ValueError("retained_oracle_asset_invalid")
    manifest = _validated_manifest(asset.get("manifest"))
    if asset.get("manifest_sha256") != _sha256_json(manifest):
        raise ValueError("retained_oracle_asset_manifest_hash_mismatch")
    expected_asset_id = "outcome_asset:" + _sha256_json(
        {"schema_version": 1, "manifest": manifest}
    )
    if asset.get("asset_id") != expected_asset_id:
        raise ValueError("retained_oracle_asset_id_mismatch")
    return {**spec, "transport_sha256": transport_sha256}, manifest


def validate_retained_oracle_asset_source(
    *,
    spec: Mapping[str, Any],
    trusted_runs_root: Path,
) -> tuple[Path, dict[str, dict[str, Any]]]:
    normalized, manifest = _validated_transport_spec(spec)
    root = trusted_runs_root.expanduser().resolve()
    if root.name.casefold() != "runs" or not root.is_dir():
        raise ValueError("retained_oracle_asset_trusted_runs_root_invalid")
    asset = normalized["asset"]
    relative = _validated_relative_path(
        asset.get("runs_relative_path"),
        label="retained_oracle_asset_runs_relative_path",
    )
    source_root = (root / Path(*relative.parts)).resolve()
    if not source_root.is_relative_to(root) or not source_root.is_dir():
        raise ValueError("retained_oracle_asset_bundle_missing")
    observed = _observed_manifest(source_root)
    if observed != manifest:
        raise ValueError("retained_oracle_asset_bundle_tampered")
    return source_root, manifest


def _validate_workspace_manifest(
    *, workspace: Path, manifest: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    research_root = (workspace / ".usertest_research").resolve()
    if not research_root.is_relative_to(workspace.resolve()) or not research_root.is_dir():
        raise ValueError("retained_oracle_asset_destination_missing")
    observed = {
        f".usertest_research/{path}": entry
        for path, entry in _observed_manifest(research_root).items()
    }
    if observed != manifest:
        raise ValueError("retained_oracle_asset_destination_tampered")
    return observed


def validate_staged_retained_oracle_asset(
    *, workspace: Path, spec: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    _, manifest = _validated_transport_spec(spec)
    return _validate_workspace_manifest(workspace=workspace, manifest=manifest)


def stage_retained_oracle_asset(
    *,
    workspace: Path,
    trusted_runs_root: Path,
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    source_root, manifest = validate_retained_oracle_asset_source(
        spec=spec,
        trusted_runs_root=trusted_runs_root,
    )
    workspace_root = workspace.expanduser().resolve()
    if not workspace_root.is_dir():
        raise ValueError("retained_oracle_asset_workspace_invalid")
    research_root = workspace_root / ".usertest_research"
    reuse_existing = False
    if research_root.exists() or research_root.is_symlink():
        if research_root.is_symlink() or not research_root.is_dir():
            raise ValueError(
                "retained_oracle_asset_materialization_collision:.usertest_research"
            )
        _validate_workspace_manifest(workspace=workspace_root, manifest=manifest)
        reuse_existing = True

    planned: list[tuple[Path, Path, int]] = []
    for relative_raw, entry in sorted(manifest.items()):
        relative = _validated_relative_path(
            relative_raw,
            label="retained_oracle_asset_manifest_path",
        )
        source = (source_root / Path(*relative.parts)).resolve()
        destination = (workspace_root / Path(*relative.parts)).resolve()
        if (
            not source.is_relative_to(source_root)
            or not destination.is_relative_to(workspace_root)
            or (not reuse_existing and destination.exists())
            or (not reuse_existing and destination.is_symlink())
        ):
            raise ValueError(
                f"retained_oracle_asset_materialization_collision:{relative_raw}"
            )
        cursor = destination.parent
        while cursor != workspace_root:
            if cursor.exists() and not cursor.is_dir():
                raise ValueError(
                    f"retained_oracle_asset_materialization_collision:{relative_raw}"
                )
            cursor = cursor.parent
        if not reuse_existing:
            planned.append((source, destination, int(entry["mode"])))

    git_dir = _resolve_git_dir(workspace_root)
    if git_dir is None or not git_dir.is_dir():
        raise ValueError("retained_oracle_asset_git_exclude_unavailable")
    exclude_path = git_dir.resolve() / "info" / "exclude"
    if exclude_path.is_symlink() or (exclude_path.exists() and not exclude_path.is_file()):
        raise ValueError("retained_oracle_asset_git_exclude_unsafe")

    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    existing_lines = set(existing.splitlines())
    additions = [f"/{path}" for path in sorted(manifest) if f"/{path}" not in existing_lines]
    if additions:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude_path.open("a", encoding="utf-8", newline="\n") as exclude_f:
            exclude_f.write(prefix + "\n".join(additions) + "\n")

    copied: list[str] = []
    for source, destination, mode in planned:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as source_f, destination.open("xb") as destination_f:
            while chunk := source_f.read(1024 * 1024):
                destination_f.write(chunk)
        os.chmod(destination, mode)
        copied.append(destination.relative_to(workspace_root).as_posix())

    observed = _validate_workspace_manifest(workspace=workspace_root, manifest=manifest)

    normalized, _ = _validated_transport_spec(spec)
    return {
        "schema_version": 1,
        "transport_sha256": normalized["transport_sha256"],
        "outcome_oracle_id": normalized["outcome_oracle_id"],
        "asset_id": normalized["asset"]["asset_id"],
        "trusted_runs_root": str(trusted_runs_root.expanduser().resolve()),
        "source_root": str(source_root),
        "manifest_sha256": normalized["asset"]["manifest_sha256"],
        "manifest_entry_count": len(manifest),
        "copied_paths": copied,
        "reused_existing": reuse_existing,
        "git_exclude_path": str(exclude_path),
        "git_exclude_entries": additions,
        "destination_manifest_sha256": _sha256_json(observed),
    }


def retained_oracle_asset_summary(
    *, trusted_runs_root: Path, spec: Mapping[str, Any]
) -> dict[str, Any]:
    normalized, manifest = _validated_transport_spec(spec)
    asset = normalized["asset"]
    return {
        "trusted_runs_root": str(trusted_runs_root.expanduser().resolve()),
        "transport_sha256": normalized["transport_sha256"],
        "outcome_oracle_id": normalized["outcome_oracle_id"],
        "oracle_kind": normalized["oracle_kind"],
        "asset_id": asset["asset_id"],
        "runs_relative_path": asset["runs_relative_path"],
        "manifest_sha256": asset["manifest_sha256"],
        "manifest_entry_count": len(manifest),
    }
