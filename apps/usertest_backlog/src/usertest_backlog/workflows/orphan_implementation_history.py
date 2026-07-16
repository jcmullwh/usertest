from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_artifacts.lifecycle import JsonArtifactReadResult, classify_run_lifecycle

from usertest_backlog.workflows.derived_evidence import inferred_implementation_runs_root

_RECOVERY_SCHEMA_VERSION = 1
_RECOVERY_PRODUCER = "usertest_backlog.orphan_implementation_history"
_TIMESTAMP_DIR_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
_AGENT_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SEED_DIR_RE = re.compile(r"^[0-9]+$")
_REQUIRED_ARTIFACTS = ("error.json", "run_meta.json", "ticket_ref.json")
_ALLOWED_EXPORT_KINDS = frozenset({"implementation", "verification"})


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _path_identity(path: Path) -> str:
    return os.path.normcase(str(path))


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _resolved_contained_path(path: Path, *, root: Path) -> Path | None:
    if _is_reparse_point(path):
        return None
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def _direct_child_dirs(path: Path) -> tuple[list[Path], str | None]:
    try:
        return sorted(
            (child for child in path.iterdir() if child.is_dir()),
            key=lambda child: child.name,
        ), None
    except OSError as exc:
        return [], type(exc).__name__


def _read_required_json(
    path: Path,
    *,
    root: Path,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    resolved = _resolved_contained_path(path, root=root)
    if resolved is None or not resolved.is_file():
        return None, None, "required_artifact_missing_or_untrusted"
    try:
        raw = resolved.read_bytes()
    except OSError:
        return None, None, "required_artifact_unreadable"
    artifact_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, artifact_sha256, "required_artifact_invalid_json"
    if not isinstance(value, dict):
        return None, artifact_sha256, "required_artifact_not_object"
    return dict(value), artifact_sha256, None


def _missing_json_read(name: str) -> JsonArtifactReadResult:
    return JsonArtifactReadResult(
        path=name,
        exists=False,
        decode_ok=None,
        parse_ok=None,
        value=None,
        error_phase=None,
        error_type=None,
        error_message=None,
    )


def _parsed_json_read(name: str, value: Mapping[str, Any]) -> JsonArtifactReadResult:
    return JsonArtifactReadResult(
        path=name,
        exists=True,
        decode_ok=True,
        parse_ok=True,
        value=dict(value),
        error_phase=None,
        error_type=None,
        error_message=None,
    )


def _timestamp_utc(name: str) -> str | None:
    if _TIMESTAMP_DIR_RE.fullmatch(name) is None:
        return None
    try:
        parsed = datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def _metadata(
    *,
    primary_runs_dir: Path,
    source_root: Path,
    target_slug: str | None,
    scoped_repo_root: Path,
    exclusions: Counter[str],
    directories_considered: int,
    records: list[dict[str, Any]],
    scan_error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": _RECOVERY_SCHEMA_VERSION,
        "producer": _RECOVERY_PRODUCER,
        "primary_runs_root": str(primary_runs_dir),
        "trusted_inferred_source_root": str(source_root),
        "requested_target_slug": target_slug,
        "scoped_repo_root": str(scoped_repo_root),
        "directories_considered": directories_considered,
        "records_recovered": len(records),
        "records_excluded": sum(exclusions.values()),
        "exclusion_reason_counts": dict(sorted(exclusions.items())),
        "scan_error": scan_error,
        "recovery_receipts": [
            dict(record["orphan_history_recovery_receipt"])
            for record in records
            if isinstance(record.get("orphan_history_recovery_receipt"), Mapping)
        ],
    }


def recover_orphan_implementation_history(
    primary_runs_dir: Path,
    *,
    target_slug: str | None,
    scoped_repo_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recover narrowly attested setup failures omitted by generic history discovery.

    Generic run history intentionally requires ``target_ref.json``.  A runner can fail
    before writing that file, so the implementation-history ingestion path has one
    constrained recovery route: the inferred ``usertest_implement`` sibling, the exact
    four-level runner layout, three parseable runner-owned artifacts, and a ticket owner
    that resolves to the already-scoped repository.  No mission, repository, case, or
    parent identity is synthesized.
    """

    primary_root = primary_runs_dir.expanduser().resolve()
    inferred_root = inferred_implementation_runs_root(primary_root)
    exclusions: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    directories_considered = 0

    requested_target = _clean_string(target_slug)
    if (
        requested_target is None
        or requested_target.startswith("_")
        or "/" in requested_target
        or "\\" in requested_target
        or Path(requested_target).name != requested_target
    ):
        exclusions["requested_target_invalid"] += 1
        return records, _metadata(
            primary_runs_dir=primary_root,
            source_root=inferred_root,
            target_slug=requested_target,
            scoped_repo_root=scoped_repo_root,
            exclusions=exclusions,
            directories_considered=directories_considered,
            records=records,
        )

    if _is_reparse_point(inferred_root):
        exclusions["trusted_source_root_untrusted"] += 1
        return records, _metadata(
            primary_runs_dir=primary_root,
            source_root=inferred_root,
            target_slug=requested_target,
            scoped_repo_root=scoped_repo_root,
            exclusions=exclusions,
            directories_considered=directories_considered,
            records=records,
        )
    try:
        source_root = inferred_root.resolve(strict=True)
    except (OSError, RuntimeError):
        exclusions["trusted_source_root_missing"] += 1
        return records, _metadata(
            primary_runs_dir=primary_root,
            source_root=inferred_root,
            target_slug=requested_target,
            scoped_repo_root=scoped_repo_root,
            exclusions=exclusions,
            directories_considered=directories_considered,
            records=records,
        )
    if not source_root.is_dir():
        exclusions["trusted_source_root_not_directory"] += 1
        return records, _metadata(
            primary_runs_dir=primary_root,
            source_root=source_root,
            target_slug=requested_target,
            scoped_repo_root=scoped_repo_root,
            exclusions=exclusions,
            directories_considered=directories_considered,
            records=records,
        )

    try:
        scope_root = scoped_repo_root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        exclusions["scoped_repo_root_missing"] += 1
        return records, _metadata(
            primary_runs_dir=primary_root,
            source_root=source_root,
            target_slug=requested_target,
            scoped_repo_root=scoped_repo_root,
            exclusions=exclusions,
            directories_considered=directories_considered,
            records=records,
        )
    if not scope_root.is_dir():
        exclusions["scoped_repo_root_not_directory"] += 1
        return records, _metadata(
            primary_runs_dir=primary_root,
            source_root=source_root,
            target_slug=requested_target,
            scoped_repo_root=scope_root,
            exclusions=exclusions,
            directories_considered=directories_considered,
            records=records,
        )

    target_dirs, scan_error = _direct_child_dirs(source_root)
    if scan_error is not None:
        exclusions["trusted_source_root_unreadable"] += 1
        return records, _metadata(
            primary_runs_dir=primary_root,
            source_root=source_root,
            target_slug=requested_target,
            scoped_repo_root=scope_root,
            exclusions=exclusions,
            directories_considered=directories_considered,
            records=records,
            scan_error=scan_error,
        )

    matched_target = False
    for target_dir in target_dirs:
        if target_dir.name.startswith("_"):
            exclusions["underscore_target_excluded"] += 1
            continue
        if target_dir.name != requested_target:
            exclusions["target_slug_mismatch"] += 1
            continue
        matched_target = True
        resolved_target = _resolved_contained_path(target_dir, root=source_root)
        if resolved_target is None:
            exclusions["target_directory_untrusted"] += 1
            continue

        timestamp_dirs, timestamp_scan_error = _direct_child_dirs(resolved_target)
        if timestamp_scan_error is not None:
            exclusions["target_directory_unreadable"] += 1
            continue
        for timestamp_dir in timestamp_dirs:
            timestamp = _timestamp_utc(timestamp_dir.name)
            if timestamp is None:
                exclusions["timestamp_directory_invalid"] += 1
                continue
            resolved_timestamp = _resolved_contained_path(
                timestamp_dir,
                root=source_root,
            )
            if resolved_timestamp is None:
                exclusions["timestamp_directory_untrusted"] += 1
                continue

            agent_dirs, agent_scan_error = _direct_child_dirs(resolved_timestamp)
            if agent_scan_error is not None:
                exclusions["timestamp_directory_unreadable"] += 1
                continue
            for agent_dir in agent_dirs:
                if (
                    agent_dir.name.startswith("_")
                    or _AGENT_DIR_RE.fullmatch(agent_dir.name) is None
                ):
                    exclusions["agent_directory_invalid"] += 1
                    continue
                resolved_agent = _resolved_contained_path(agent_dir, root=source_root)
                if resolved_agent is None:
                    exclusions["agent_directory_untrusted"] += 1
                    continue

                seed_dirs, seed_scan_error = _direct_child_dirs(resolved_agent)
                if seed_scan_error is not None:
                    exclusions["agent_directory_unreadable"] += 1
                    continue
                for seed_dir in seed_dirs:
                    if (
                        seed_dir.name.startswith("_")
                        or _SEED_DIR_RE.fullmatch(seed_dir.name) is None
                    ):
                        exclusions["seed_directory_invalid"] += 1
                        continue
                    directories_considered += 1
                    resolved_seed = _resolved_contained_path(seed_dir, root=source_root)
                    if resolved_seed is None:
                        exclusions["seed_directory_untrusted"] += 1
                        continue
                    target_ref_path = resolved_seed / "target_ref.json"
                    if _is_reparse_point(target_ref_path) or target_ref_path.exists():
                        exclusions["target_ref_present_or_untrusted"] += 1
                        continue

                    parsed: dict[str, dict[str, Any]] = {}
                    artifact_hashes: dict[str, str] = {}
                    artifact_error: str | None = None
                    for filename in _REQUIRED_ARTIFACTS:
                        value, digest, error_reason = _read_required_json(
                            resolved_seed / filename,
                            root=source_root,
                        )
                        if error_reason is not None or value is None or digest is None:
                            artifact_error = error_reason or "required_artifact_invalid"
                            break
                        parsed[filename] = value
                        artifact_hashes[filename] = digest
                    if artifact_error is not None:
                        exclusions[artifact_error] += 1
                        continue

                    error = parsed["error.json"]
                    run_meta = parsed["run_meta.json"]
                    ticket_ref = parsed["ticket_ref.json"]
                    if _clean_string(error.get("type")) is None:
                        exclusions["error_type_missing"] += 1
                        continue
                    if (
                        run_meta.get("schema_version") != 1
                        or _clean_string(run_meta.get("run_started_utc")) is None
                    ):
                        exclusions["run_meta_contract_invalid"] += 1
                        continue
                    export_kind = _clean_string(ticket_ref.get("export_kind"))
                    if export_kind not in _ALLOWED_EXPORT_KINDS:
                        exclusions["ticket_export_kind_invalid"] += 1
                        continue
                    if ticket_ref.get("schema_version") not in {1, 2}:
                        exclusions["ticket_ref_schema_invalid"] += 1
                        continue
                    owner_raw = ticket_ref.get("owner_repo")
                    owner = owner_raw if isinstance(owner_raw, Mapping) else {}
                    owner_root_raw = _clean_string(owner.get("root"))
                    if owner_root_raw is None:
                        exclusions["ticket_owner_root_missing"] += 1
                        continue
                    owner_root_path = Path(owner_root_raw).expanduser()
                    if not owner_root_path.is_absolute():
                        exclusions["ticket_owner_root_not_absolute"] += 1
                        continue
                    try:
                        owner_root = owner_root_path.resolve(strict=True)
                    except (OSError, RuntimeError):
                        exclusions["ticket_owner_root_unresolvable"] += 1
                        continue
                    if not owner_root.is_dir() or _path_identity(owner_root) != _path_identity(
                        scope_root
                    ):
                        exclusions["ticket_owner_root_scope_mismatch"] += 1
                        continue

                    report_read = _missing_json_read("report.json")
                    error_read = _parsed_json_read("error.json", error)
                    validation_read = _missing_json_read("report_validation_errors.json")
                    run_meta_read = _parsed_json_read("run_meta.json", run_meta)
                    lifecycle = classify_run_lifecycle(
                        report_read=report_read,
                        error_read=error_read,
                        report_validation_errors_read=validation_read,
                        run_meta_read=run_meta_read,
                    )
                    if lifecycle.status != "error":
                        exclusions["recovered_lifecycle_not_error"] += 1
                        continue

                    run_rel = "/".join(
                        (
                            requested_target,
                            timestamp_dir.name,
                            agent_dir.name,
                            seed_dir.name,
                        )
                    )
                    receipt_payload: dict[str, Any] = {
                        "schema_version": _RECOVERY_SCHEMA_VERSION,
                        "producer": _RECOVERY_PRODUCER,
                        "trusted_source_root": str(source_root),
                        "run_rel": run_rel,
                        "requested_target_slug": requested_target,
                        "scoped_repo_root": str(scope_root),
                        "resolved_ticket_owner_root": str(owner_root),
                        "export_kind": export_kind,
                        "artifact_sha256": dict(sorted(artifact_hashes.items())),
                    }
                    receipt = {
                        **receipt_payload,
                        "receipt_sha256": _canonical_sha256(receipt_payload),
                    }
                    exit_code_raw = error.get("exit_code")
                    records.append(
                        {
                            "run_dir": str(resolved_seed),
                            "run_rel": run_rel,
                            "target_slug": requested_target,
                            "timestamp_dir": timestamp_dir.name,
                            "timestamp_utc": timestamp,
                            "agent": agent_dir.name,
                            "seed": int(seed_dir.name),
                            "status": lifecycle.status,
                            "agent_exit_code": (
                                exit_code_raw
                                if isinstance(exit_code_raw, int)
                                and not isinstance(exit_code_raw, bool)
                                else None
                            ),
                            "target_ref": None,
                            "evidence_assignment": None,
                            "evidence_assignment_read_status": "missing_target_ref",
                            "effective_run_spec": None,
                            "report": None,
                            "metrics": None,
                            "preflight": None,
                            "error": error,
                            "report_validation_errors": None,
                            "run_meta": run_meta,
                            "agent_attempts": None,
                            "ticket_ref": ticket_ref,
                            "timing": None,
                            "terminal_artifact_reads": lifecycle.terminal_artifact_reads,
                            "embedded": {},
                            "embedded_capture_manifest": {},
                            "orphan_history_recovery_receipt": receipt,
                            "orphan_history_recovery_receipt_sha256": receipt["receipt_sha256"],
                        }
                    )

    if not matched_target:
        exclusions["requested_target_not_found"] += 1
    records.sort(key=lambda record: str(record.get("run_rel") or ""))
    return records, _metadata(
        primary_runs_dir=primary_root,
        source_root=source_root,
        target_slug=requested_target,
        scoped_repo_root=scope_root,
        exclusions=exclusions,
        directories_considered=directories_considered,
        records=records,
    )


__all__ = ["recover_orphan_implementation_history"]
