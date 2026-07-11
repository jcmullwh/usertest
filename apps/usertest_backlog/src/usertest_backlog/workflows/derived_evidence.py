from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backlog_core import (
    apply_atom_disposition_decision,
    extract_backlog_atoms,
    normalize_atom_lineage,
)

_DIRECT_BINDING_AUTHORITY = "verified_runner_ticket_ref_v2"
_LEGACY_BINDING_AUTHORITY = "atom_action_fingerprint_case_membership"
_SOURCE_ROOT_KIND = "usertest_implement"


@dataclass(frozen=True)
class DerivedEvidenceIngestion:
    records: list[dict[str, Any]]
    atoms: list[dict[str, Any]]
    parent_bindings_by_run: dict[str, dict[str, Any]]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PrimaryDerivedEvidence:
    atoms: list[dict[str, Any]]
    parent_bindings_by_run: dict[str, dict[str, Any]]
    metadata: dict[str, Any]


def inferred_implementation_runs_root(primary_runs_dir: Path) -> Path:
    """Return the conventional implementation-run sibling for a backlog run root."""

    return primary_runs_dir.expanduser().resolve().parent / "usertest_implement"


def _is_remote_repo_input(value: str) -> bool:
    cleaned = value.strip()
    return "://" in cleaned or cleaned.startswith("git@")


def _normalize_remote_repo_input(value: str) -> str:
    raw = value.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[: -len(".git")]
    if "://" in raw:
        parsed = urlparse(raw)
        host = (parsed.hostname or parsed.netloc or "").strip().casefold()
        path = (parsed.path or "").strip().strip("/")
        return f"{host}/{path.casefold()}" if host and path else (host or raw.casefold())
    match = re.fullmatch(r"[^@]+@(?P<host>[^:]+):(?P<path>.+)", raw)
    if match is not None:
        host = match.group("host").strip().casefold()
        path = match.group("path").strip().strip("/")
        return f"{host}/{path.casefold()}"
    return raw.casefold()


def _resolved_local_repo_input(value: str, *, repo_root: Path) -> str | None:
    if _is_remote_repo_input(value) or value.startswith(("pip:", "pdm:")):
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if not resolved.is_dir():
        return None
    return os.path.normcase(str(resolved))


def filter_derived_history_records(
    records: Sequence[Mapping[str, Any]],
    *,
    target_slug: str | None,
    repo_input: str | None,
    repo_root: Path,
    git_remote_urls: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep exact target/repository aliases from one trusted implementation-run root."""

    requested = _clean_string(repo_input)
    requested_remote = (
        _normalize_remote_repo_input(requested)
        if requested is not None and _is_remote_repo_input(requested)
        else None
    )
    requested_local = (
        _resolved_local_repo_input(requested, repo_root=repo_root)
        if requested is not None
        else None
    )
    remote_aliases = {
        _normalize_remote_repo_input(value)
        for value in git_remote_urls
        if _clean_string(value) is not None
    }
    if requested_remote is not None:
        remote_aliases.add(requested_remote)
    literal_requested = requested.casefold() if requested is not None else None

    included: list[dict[str, Any]] = []
    excluded_target = 0
    excluded_repo = 0
    matched_by: Counter[str] = Counter()
    for raw_record in records:
        record = dict(raw_record)
        if target_slug is not None and _clean_string(record.get("target_slug")) != target_slug:
            excluded_target += 1
            continue
        if requested is None:
            included.append(record)
            matched_by["target_only"] += 1
            continue

        target_ref_raw = record.get("target_ref")
        target_ref = target_ref_raw if isinstance(target_ref_raw, Mapping) else {}
        candidate = _clean_string(target_ref.get("repo_input"))
        ticket_ref_raw = record.get("ticket_ref")
        ticket_ref = ticket_ref_raw if isinstance(ticket_ref_raw, Mapping) else {}
        owner_raw = ticket_ref.get("owner_repo")
        owner = owner_raw if isinstance(owner_raw, Mapping) else {}
        owner_root = _clean_string(owner.get("root"))

        match_kind: str | None = None
        if candidate is not None and candidate.casefold() == literal_requested:
            match_kind = "literal"
        elif candidate is not None and _is_remote_repo_input(candidate):
            if _normalize_remote_repo_input(candidate) in remote_aliases:
                match_kind = "git_remote"
        elif candidate is not None and requested_local is not None:
            if _resolved_local_repo_input(candidate, repo_root=repo_root) == requested_local:
                match_kind = "local_path"
        if match_kind is None and owner_root is not None and requested_local is not None:
            if _resolved_local_repo_input(owner_root, repo_root=repo_root) == requested_local:
                match_kind = "ticket_owner_root"

        if match_kind is None:
            excluded_repo += 1
            continue
        included.append(record)
        matched_by[match_kind] += 1

    return included, {
        "records_scanned": len(records),
        "records_included": len(included),
        "records_excluded_target": excluded_target,
        "records_excluded_repo": excluded_repo,
        "match_counts": dict(sorted(matched_by.items())),
        "requested_repo_input": requested,
        "requested_local_root": requested_local,
        "accepted_git_remote_identities": sorted(remote_aliases),
    }


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _identity_path(value: Any) -> str | None:
    raw = _clean_string(value)
    if raw is None:
        return None
    resolved = str(Path(raw).expanduser().resolve())
    return os.path.normcase(resolved)


def _target_ref_sha256(record: Mapping[str, Any]) -> str:
    target_ref = record.get("target_ref")
    return _sha256_json(target_ref if isinstance(target_ref, Mapping) else None)


def _verified_orphan_recovery_receipt_sha256(record: Mapping[str, Any]) -> str | None:
    receipt_raw = record.get("orphan_history_recovery_receipt")
    if not isinstance(receipt_raw, Mapping):
        return None
    receipt = dict(receipt_raw)
    supplied = receipt.pop("receipt_sha256", None)
    if (
        receipt.get("schema_version") != 1
        or receipt.get("producer") != "usertest_backlog.orphan_implementation_history"
        or not _is_sha256(supplied)
        or supplied != _sha256_json(receipt)
        or record.get("orphan_history_recovery_receipt_sha256") != supplied
    ):
        return None
    return str(supplied)


def _prepare_derived_records(
    records: Sequence[Mapping[str, Any]],
    *,
    source_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resolved_source_root = source_root.expanduser().resolve()
    source_root_identity = os.path.normcase(str(resolved_source_root))
    prepared_by_identity: dict[str, dict[str, Any]] = {}
    duplicate_count = 0

    for index, raw_record in enumerate(records):
        record = dict(raw_record)
        original_run_rel = _clean_string(record.get("run_rel"))
        resolved_run_identity = _identity_path(record.get("run_dir"))
        if resolved_run_identity is None:
            resolved_run_identity = original_run_rel or f"record:{index + 1}"
        target_ref_sha256 = _target_ref_sha256(record)
        orphan_recovery_receipt_sha256 = _verified_orphan_recovery_receipt_sha256(record)
        identity_payload = {
            "source_root": source_root_identity,
            "resolved_run_identity": resolved_run_identity,
            "target_ref_sha256": target_ref_sha256,
            "orphan_recovery_receipt_sha256": orphan_recovery_receipt_sha256,
        }
        source_record_identity = _sha256_json(identity_payload)
        if source_record_identity in prepared_by_identity:
            duplicate_count += 1
            continue

        namespaced_run_rel = f"__derived__/{_SOURCE_ROOT_KIND}/{source_record_identity}"
        run_dir_raw = _clean_string(record.get("run_dir"))
        ticket_ref_sha256 = (
            _sha256_file(Path(run_dir_raw) / "ticket_ref.json") if run_dir_raw is not None else None
        )
        record.update(
            {
                "run_rel": namespaced_run_rel,
                "derived_source_root": str(resolved_source_root),
                "derived_source_root_kind": _SOURCE_ROOT_KIND,
                "derived_source_run_rel": original_run_rel,
                "derived_source_record_identity": source_record_identity,
                "derived_source_target_ref_sha256": target_ref_sha256,
                "derived_source_ticket_ref_sha256": ticket_ref_sha256,
                "derived_source_orphan_recovery_receipt_sha256": (orphan_recovery_receipt_sha256),
                "derived_resolved_run_identity": resolved_run_identity,
            }
        )
        prepared_by_identity[source_record_identity] = record

    prepared = [prepared_by_identity[key] for key in sorted(prepared_by_identity)]
    return prepared, {
        "records_seen": len(records),
        "records_ingested": len(prepared),
        "duplicate_records_suppressed": duplicate_count,
    }


def _target_contract_errors(
    target_contract: Any,
    *,
    case_id: str,
    expected_sha256: Any,
) -> list[str]:
    if not isinstance(target_contract, Mapping):
        return ["ticket_provenance_target_contract_missing"]
    contract = dict(target_contract)
    if contract.get("schema_version") != 2:
        return ["ticket_provenance_target_contract_schema_invalid"]
    supplied_sha256 = contract.get("contract_sha256")
    payload = {key: value for key, value in contract.items() if key != "contract_sha256"}
    errors: list[str] = []
    if not _is_sha256(supplied_sha256) or supplied_sha256 != _sha256_json(payload):
        errors.append("ticket_provenance_target_contract_hash_invalid")
    if supplied_sha256 != expected_sha256:
        errors.append("ticket_provenance_target_contract_digest_mismatch")
    if _clean_string(contract.get("case_id")) != case_id:
        errors.append("ticket_provenance_target_contract_case_mismatch")
    return errors


def _direct_ticket_binding(
    record: Mapping[str, Any],
) -> dict[str, Any] | None:
    ticket_ref_raw = record.get("ticket_ref")
    if not isinstance(ticket_ref_raw, Mapping):
        return None
    ticket_ref = dict(ticket_ref_raw)
    provenance_raw = ticket_ref.get("ticket_provenance")
    provenance = dict(provenance_raw) if isinstance(provenance_raw, Mapping) else None

    # Current usertest_implement also writes schema-2 wrappers for genuinely legacy
    # tickets.  Their synthetic legacy-case/legacy-plan values are not canonical case
    # provenance and must take the exact atom-action reconstruction path below.
    if provenance is not None and provenance.get("legacy_identity") is True:
        return None

    has_direct_claim = any(
        _clean_string(value) is not None
        for value in (
            ticket_ref.get("case_id"),
            ticket_ref.get("plan_revision_id"),
            provenance.get("case_id") if provenance is not None else None,
            provenance.get("plan_revision_id") if provenance is not None else None,
        )
    ) or (provenance is not None and provenance.get("legacy_identity") is False)
    if not has_direct_claim:
        return None

    errors: list[str] = []
    fingerprint = _clean_string(ticket_ref.get("fingerprint"))
    case_id = _clean_string(ticket_ref.get("case_id"))
    plan_revision_id = _clean_string(ticket_ref.get("plan_revision_id"))
    if ticket_ref.get("schema_version") != 2:
        errors.append("ticket_ref_schema_invalid")
    if fingerprint is None:
        errors.append("ticket_ref_fingerprint_missing")
    if case_id is None:
        errors.append("ticket_ref_case_id_missing")
    if plan_revision_id is None:
        errors.append("ticket_ref_plan_revision_id_missing")
    if provenance is None or provenance.get("schema_version") != 1:
        errors.append("ticket_provenance_schema_invalid")
    else:
        if provenance.get("legacy_identity") is not False:
            errors.append("ticket_provenance_not_canonical")
        if provenance.get("generated_ticket") is not True:
            errors.append("ticket_provenance_not_generated")
        for field, expected in (
            ("fingerprint", fingerprint),
            ("case_id", case_id),
            ("plan_revision_id", plan_revision_id),
        ):
            if expected is None or provenance.get(field) != expected:
                errors.append(f"ticket_provenance_{field}_mismatch")
        for field in ("ticket_body_sha256", "local_plan_sha256"):
            if not _is_sha256(provenance.get(field)):
                errors.append(f"ticket_provenance_{field}_invalid")
        target_contract_sha256 = provenance.get("target_contract_sha256")
        if not _is_sha256(target_contract_sha256):
            errors.append("ticket_provenance_target_contract_sha256_invalid")
        elif case_id is not None:
            errors.extend(
                _target_contract_errors(
                    provenance.get("target_contract"),
                    case_id=case_id,
                    expected_sha256=target_contract_sha256,
                )
            )

    binding_raw = ticket_ref.get("verification_binding")
    binding = dict(binding_raw) if isinstance(binding_raw, Mapping) else None
    if binding is None or binding.get("schema_version") != 1:
        errors.append("ticket_verification_binding_schema_invalid")
    else:
        supplied_binding_sha = binding.get("binding_sha256")
        binding_payload = {key: value for key, value in binding.items() if key != "binding_sha256"}
        if not _is_sha256(supplied_binding_sha) or supplied_binding_sha != _sha256_json(
            binding_payload
        ):
            errors.append("ticket_verification_binding_hash_invalid")
        for field, expected in (
            ("fingerprint", fingerprint),
            ("case_id", case_id),
            ("plan_revision_id", plan_revision_id),
        ):
            if expected is None or binding.get(field) != expected:
                errors.append(f"ticket_verification_binding_{field}_mismatch")
        if provenance is not None:
            for binding_field, provenance_field in (
                ("ticket_body_sha256", "ticket_body_sha256"),
                ("local_plan_sha256", "local_plan_sha256"),
                ("plan_target_contract_sha256", "target_contract_sha256"),
                ("plan_verification_contract_sha256", "verification_contract_sha256"),
            ):
                if binding.get(binding_field) != provenance.get(provenance_field):
                    errors.append(f"ticket_verification_binding_{binding_field}_mismatch")

    status = "verified" if not errors else "conflict"
    return {
        "status": status,
        "case_ids": [case_id] if status == "verified" and case_id is not None else [],
        "authority": _DIRECT_BINDING_AUTHORITY,
        "fingerprint": fingerprint,
        "plan_revision_id": plan_revision_id if status == "verified" else None,
        "matched_atom_ids": [],
        "errors": sorted(set(errors)),
    }


def _case_ids_for_atom(
    atom_id: str,
    *,
    case_registry: Mapping[str, Any],
) -> set[str]:
    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, Mapping) else {}
    candidate_case_ids: set[str] = set()

    primary_raw = case_registry.get("atom_id_to_case_id")
    primary = primary_raw if isinstance(primary_raw, Mapping) else {}
    primary_case_id = _clean_string(primary.get(atom_id))
    if primary_case_id is not None:
        candidate_case_ids.add(primary_case_id)

    memberships_raw = case_registry.get("atom_id_to_case_ids")
    memberships = memberships_raw if isinstance(memberships_raw, Mapping) else {}
    values = memberships.get(atom_id)
    if isinstance(values, list):
        candidate_case_ids.update(
            case_id for value in values for case_id in [_clean_string(value)] if case_id is not None
        )

    # A dangling mapping is not canonical case evidence.
    return {case_id for case_id in candidate_case_ids if case_id in cases}


def _legacy_ticket_binding(
    record: Mapping[str, Any],
    *,
    atom_actions: Mapping[str, Mapping[str, Any]],
    case_registry: Mapping[str, Any],
) -> dict[str, Any]:
    ticket_ref_raw = record.get("ticket_ref")
    ticket_ref = dict(ticket_ref_raw) if isinstance(ticket_ref_raw, Mapping) else {}
    fingerprint = _clean_string(ticket_ref.get("fingerprint"))
    matched_atom_ids: list[str] = []
    if fingerprint is not None:
        fingerprint_key = fingerprint.casefold()
        for atom_id, action in atom_actions.items():
            fingerprints_raw = action.get("fingerprints")
            fingerprints = fingerprints_raw if isinstance(fingerprints_raw, list) else []
            if any(
                cleaned is not None and cleaned.casefold() == fingerprint_key
                for value in fingerprints
                for cleaned in [_clean_string(value)]
            ):
                matched_atom_ids.append(str(atom_id))

    matched_atom_ids = sorted(set(matched_atom_ids))
    case_ids: set[str] = set()
    for atom_id in matched_atom_ids:
        case_ids.update(_case_ids_for_atom(atom_id, case_registry=case_registry))

    if len(case_ids) == 1:
        status = "reconstructed"
    elif len(case_ids) > 1:
        status = "conflict"
    else:
        status = "unavailable"
    errors: list[str] = []
    if fingerprint is None:
        errors.append("legacy_ticket_fingerprint_missing")
    elif not matched_atom_ids:
        errors.append("legacy_ticket_fingerprint_not_in_atom_actions")
    if matched_atom_ids and not case_ids:
        errors.append("legacy_ticket_atoms_have_no_canonical_case")
    if len(case_ids) > 1:
        errors.append("legacy_ticket_atoms_span_multiple_canonical_cases")

    return {
        "status": status,
        "case_ids": sorted(case_ids),
        "authority": _LEGACY_BINDING_AUTHORITY,
        "fingerprint": fingerprint,
        "plan_revision_id": None,
        "matched_atom_ids": matched_atom_ids,
        "errors": errors,
    }


def _bind_record(
    record: Mapping[str, Any],
    *,
    atom_actions: Mapping[str, Mapping[str, Any]],
    case_registry: Mapping[str, Any],
) -> dict[str, Any]:
    binding = _direct_ticket_binding(record)
    if binding is None:
        binding = _legacy_ticket_binding(
            record,
            atom_actions=atom_actions,
            case_registry=case_registry,
        )
    receipt_payload = {
        "schema_version": 1,
        "source_record_identity": record.get("derived_source_record_identity"),
        "run_rel": record.get("run_rel"),
        "source_root": record.get("derived_source_root"),
        "source_target_ref_sha256": record.get("derived_source_target_ref_sha256"),
        "source_ticket_ref_sha256": record.get("derived_source_ticket_ref_sha256"),
        "source_orphan_recovery_receipt_sha256": record.get(
            "derived_source_orphan_recovery_receipt_sha256"
        ),
        **binding,
    }
    return {**binding, "receipt_sha256": _sha256_json(receipt_payload)}


def _derived_role(record: Mapping[str, Any]) -> tuple[str, str]:
    target_ref_raw = record.get("target_ref")
    target_ref = target_ref_raw if isinstance(target_ref_raw, Mapping) else {}
    mission_id = _clean_string(target_ref.get("mission_id")) or _clean_string(
        target_ref.get("requested_mission_id")
    )
    if mission_id == "review_backlog_implementation_pr_v1":
        return "verification", "verification"
    return "implementation", "implementation"


def _bind_derived_atoms(
    atoms: Sequence[Mapping[str, Any]],
    *,
    records_by_run: Mapping[str, Mapping[str, Any]],
    bindings_by_run: Mapping[str, Mapping[str, Any]],
    case_registry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    bound: list[dict[str, Any]] = []
    for raw_atom in atoms:
        atom = dict(raw_atom)
        run_rel = _clean_string(atom.get("run_rel")) or _clean_string(atom.get("origin_run_id"))
        record = records_by_run.get(run_rel or "", {})
        binding = bindings_by_run.get(run_rel or "", {})
        role, stage = _derived_role(record)
        status = _clean_string(binding.get("status")) or "unavailable"
        case_ids_raw = binding.get("case_ids")
        case_ids = (
            sorted(
                {
                    case_id
                    for value in case_ids_raw
                    for case_id in [_clean_string(value)]
                    if case_id is not None
                }
            )
            if isinstance(case_ids_raw, list)
            else []
        )
        parent_case_id = case_ids[0] if status in {"verified", "reconstructed"} else None
        atom.update(
            {
                "origin_run_id": run_rel,
                "origin_stage": stage,
                "evidence_role": role,
                "parent_case_id": parent_case_id,
                "case_id": parent_case_id,
                "supporting_case_ids": [parent_case_id] if parent_case_id is not None else [],
                "derived_from_atom_ids": list(binding.get("matched_atom_ids") or []),
                "disposition": "supports_case" if parent_case_id is not None else "unresolved",
                "disposition_status": "pending",
                "disposition_receipt": None,
                "derived_parent_binding_status": status,
                "derived_parent_binding_authority": binding.get("authority"),
                "derived_parent_binding_receipt_sha256": binding.get("receipt_sha256"),
                "derived_parent_case_ids_considered": case_ids,
                "derived_parent_plan_revision_id": binding.get("plan_revision_id"),
                "derived_parent_binding_errors": list(binding.get("errors") or []),
                "derived_source_root": record.get("derived_source_root"),
                "derived_source_root_kind": record.get("derived_source_root_kind"),
                "derived_source_run_rel": record.get("derived_source_run_rel"),
                "derived_source_record_identity": record.get("derived_source_record_identity"),
                "derived_source_target_ref_sha256": record.get("derived_source_target_ref_sha256"),
                "derived_source_ticket_ref_sha256": record.get("derived_source_ticket_ref_sha256"),
                "derived_source_orphan_recovery_receipt_sha256": record.get(
                    "derived_source_orphan_recovery_receipt_sha256"
                ),
            }
        )
        if parent_case_id is None:
            atom["lineage_mining_blocker"] = f"derived_parent_binding_{status}"
        else:
            atom.pop("lineage_mining_blocker", None)
            atom.pop("lineage_validation_errors", None)
        bound.append(atom)
    normalized = normalize_atom_lineage(
        bound,
        case_registry=case_registry,
        strict_new_output=True,
    )
    finalized: list[dict[str, Any]] = []
    for atom in normalized:
        status = _clean_string(atom.get("derived_parent_binding_status")) or "unavailable"
        if atom.get("disposition") == "unresolved":
            atom = apply_atom_disposition_decision(
                atom,
                disposition="unresolved",
                source="runner_target_ref",
                rationale=(
                    "Derived evidence was retained for audit, but its exact canonical "
                    f"parent binding is {status}; it cannot originate a problem from prose."
                ),
            )
        finalized.append(atom)
    return normalize_atom_lineage(
        finalized,
        case_registry=case_registry,
        strict_new_output=True,
    )


def annotate_primary_derived_evidence(
    records: Sequence[Mapping[str, Any]],
    atoms: Sequence[Mapping[str, Any]],
    *,
    source_root: Path,
    case_registry: Mapping[str, Any],
) -> PrimaryDerivedEvidence:
    """Finalize parent binding for research evidence already read from the primary root."""

    resolved_source_root = source_root.expanduser().resolve()
    records_by_run = {
        run_rel: dict(record)
        for record in records
        for run_rel in [_clean_string(record.get("run_rel"))]
        if run_rel is not None
    }
    derived_atoms_by_run: dict[str, list[dict[str, Any]]] = {}
    for raw_atom in atoms:
        atom = dict(raw_atom)
        if _clean_string(atom.get("evidence_role")) not in {
            "research",
            "implementation",
            "verification",
        }:
            continue
        run_rel = _clean_string(atom.get("run_rel")) or _clean_string(atom.get("origin_run_id"))
        if run_rel is not None:
            derived_atoms_by_run.setdefault(run_rel, []).append(atom)

    bindings_by_run: dict[str, dict[str, Any]] = {}
    record_identity_by_run: dict[str, str] = {}
    for run_rel, run_atoms in sorted(derived_atoms_by_run.items()):
        record = records_by_run.get(run_rel, {})
        case_ids = sorted(
            {
                case_id
                for atom in run_atoms
                for case_id in [_clean_string(atom.get("parent_case_id"))]
                if case_id is not None
            }
        )
        authorities = sorted(
            {
                authority.strip()
                for atom in run_atoms
                for raw in [atom.get("lineage_authorities")]
                for authority in (raw if isinstance(raw, list) else [])
                if isinstance(authority, str) and authority.strip()
            }
        )
        has_lineage_errors = any(atom.get("lineage_validation_errors") for atom in run_atoms)
        if len(case_ids) > 1 or has_lineage_errors:
            status = "conflict"
        elif len(case_ids) == 1:
            status = (
                "verified"
                if set(authorities)
                & {"runner_evidence_assignment", "runner_ticket_ref", "runner_target_ref"}
                else "reconstructed"
            )
        else:
            status = "unavailable"
        resolved_run_identity = _identity_path(record.get("run_dir")) or run_rel
        source_record_identity = _sha256_json(
            {
                "source_root": os.path.normcase(str(resolved_source_root)),
                "resolved_run_identity": resolved_run_identity,
                "target_ref_sha256": _target_ref_sha256(record),
            }
        )
        record_identity_by_run[run_rel] = source_record_identity
        binding_payload = {
            "schema_version": 1,
            "source_record_identity": source_record_identity,
            "run_rel": run_rel,
            "source_root": str(resolved_source_root),
            "source_target_ref_sha256": _target_ref_sha256(record),
            "status": status,
            "case_ids": case_ids,
            "authority": ",".join(authorities) or "runner_target_ref",
        }
        bindings_by_run[run_rel] = {
            "status": status,
            "case_ids": case_ids,
            "authority": binding_payload["authority"],
            "receipt_sha256": _sha256_json(binding_payload),
        }

    annotated: list[dict[str, Any]] = []
    for raw_atom in atoms:
        atom = dict(raw_atom)
        role = _clean_string(atom.get("evidence_role"))
        if role not in {"research", "implementation", "verification"}:
            annotated.append(atom)
            continue
        run_rel = _clean_string(atom.get("run_rel")) or _clean_string(atom.get("origin_run_id"))
        binding = bindings_by_run.get(run_rel or "", {})
        status = _clean_string(binding.get("status")) or "unavailable"
        atom.update(
            {
                "derived_source_root": str(resolved_source_root),
                "derived_source_root_kind": "usertest",
                "derived_source_run_rel": run_rel,
                "derived_source_record_identity": record_identity_by_run.get(run_rel or ""),
                "derived_source_target_ref_sha256": _target_ref_sha256(
                    records_by_run.get(run_rel or "", {})
                ),
                "derived_parent_binding_status": status,
                "derived_parent_binding_authority": binding.get("authority"),
                "derived_parent_binding_receipt_sha256": binding.get("receipt_sha256"),
                "derived_parent_case_ids_considered": list(binding.get("case_ids") or []),
            }
        )
        if _clean_string(atom.get("parent_case_id")) is None:
            atom["lineage_mining_blocker"] = f"derived_parent_binding_{status}"
            atom["case_id"] = None
            atom["supporting_case_ids"] = []
            atom = apply_atom_disposition_decision(
                atom,
                disposition="unresolved",
                source="runner_target_ref",
                rationale=(
                    "Primary-run derived evidence has no authoritative canonical parent "
                    f"binding ({status}); it is retained but cannot originate from prose."
                ),
            )
        annotated.append(atom)

    normalized = normalize_atom_lineage(
        annotated,
        case_registry=case_registry,
        strict_new_output=True,
    )
    source_counts = Counter(
        str(atom.get("source") or "unknown")
        for atom in normalized
        if _clean_string(atom.get("evidence_role"))
        in {"research", "implementation", "verification"}
    )
    status_counts = Counter(
        str(binding.get("status") or "unavailable") for binding in bindings_by_run.values()
    )
    return PrimaryDerivedEvidence(
        atoms=normalized,
        parent_bindings_by_run=bindings_by_run,
        metadata={
            "source_root": str(resolved_source_root),
            "derived_records": len(derived_atoms_by_run),
            "derived_atoms": sum(len(values) for values in derived_atoms_by_run.values()),
            "atom_source_counts": dict(sorted(source_counts.items())),
            "binding_record_status_counts": dict(sorted(status_counts.items())),
        },
    )


def ingest_derived_evidence_records(
    records: Sequence[Mapping[str, Any]],
    *,
    source_root: Path,
    repo_root: Path,
    atom_actions: Mapping[str, Mapping[str, Any]],
    case_registry: Mapping[str, Any],
) -> DerivedEvidenceIngestion:
    """Normalize implementation/verification history without mining its prose as new work."""

    prepared, record_meta = _prepare_derived_records(records, source_root=source_root)
    records_by_run = {
        str(record["run_rel"]): record
        for record in prepared
        if _clean_string(record.get("run_rel")) is not None
    }
    bindings_by_run = {
        run_rel: _bind_record(
            record,
            atom_actions=atom_actions,
            case_registry=case_registry,
        )
        for run_rel, record in records_by_run.items()
    }

    extracted_doc = extract_backlog_atoms(prepared, repo_root=repo_root)
    extracted_raw = extracted_doc.get("atoms")
    extracted = (
        [item for item in extracted_raw if isinstance(item, Mapping)]
        if isinstance(extracted_raw, list)
        else []
    )
    atoms = _bind_derived_atoms(
        extracted,
        records_by_run=records_by_run,
        bindings_by_run=bindings_by_run,
        case_registry=case_registry,
    )

    source_counts = Counter(str(atom.get("source") or "unknown") for atom in atoms)
    role_counts = Counter(str(atom.get("evidence_role") or "unknown") for atom in atoms)
    class_counts = Counter(str(atom.get("evidence_class") or "unknown") for atom in atoms)
    binding_record_counts = Counter(
        str(binding.get("status") or "unavailable") for binding in bindings_by_run.values()
    )
    binding_atom_counts = Counter(
        str(atom.get("derived_parent_binding_status") or "unavailable") for atom in atoms
    )
    binding_receipts = [
        {
            "run_rel": run_rel,
            "source_record_identity": records_by_run[run_rel].get("derived_source_record_identity"),
            "status": binding.get("status"),
            "case_ids": list(binding.get("case_ids") or []),
            "authority": binding.get("authority"),
            "fingerprint": binding.get("fingerprint"),
            "plan_revision_id": binding.get("plan_revision_id"),
            "matched_atom_ids": list(binding.get("matched_atom_ids") or []),
            "errors": list(binding.get("errors") or []),
            "receipt_sha256": binding.get("receipt_sha256"),
        }
        for run_rel, binding in sorted(bindings_by_run.items())
    ]
    metadata = {
        "schema_version": 1,
        "primary_aggregate_population_includes_derived": False,
        "source_roots": [
            {
                "kind": _SOURCE_ROOT_KIND,
                "path": str(source_root.expanduser().resolve()),
                "records_seen": record_meta["records_seen"],
                "records_ingested": record_meta["records_ingested"],
            }
        ],
        **record_meta,
        "atoms_ingested": len(atoms),
        "atom_source_counts": dict(sorted(source_counts.items())),
        "atom_evidence_role_counts": dict(sorted(role_counts.items())),
        "atom_evidence_class_counts": dict(sorted(class_counts.items())),
        "binding_record_status_counts": dict(sorted(binding_record_counts.items())),
        "binding_atom_status_counts": dict(sorted(binding_atom_counts.items())),
        "binding_receipts": binding_receipts,
        "operational_failure_candidates": {
            "count": 0,
            "atom_ids": [],
            "receipts": [],
        },
    }
    return DerivedEvidenceIngestion(
        records=prepared,
        atoms=atoms,
        parent_bindings_by_run={
            run_rel: dict(binding) for run_rel, binding in bindings_by_run.items()
        },
        metadata=metadata,
    )


def annotate_operational_failure_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    records: Sequence[Mapping[str, Any]],
    source_atoms: Sequence[Mapping[str, Any]],
    primary_source_root: Path,
) -> list[dict[str, Any]]:
    """Preserve source-root identity on runner-owned synthetic observation atoms."""

    records_by_run = {
        str(record["run_rel"]): record
        for record in records
        if _clean_string(record.get("run_rel")) is not None
    }
    atoms_by_id = {
        str(atom["atom_id"]): atom
        for atom in source_atoms
        if _clean_string(atom.get("atom_id")) is not None
    }
    resolved_primary_root = str(primary_source_root.expanduser().resolve())
    annotated: list[dict[str, Any]] = []
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        source_atom_ids = [
            atom_id
            for value in candidate.get("source_derived_atom_ids") or []
            for atom_id in [_clean_string(value)]
            if atom_id is not None
        ]
        source_roots: set[str] = set()
        source_root_kinds: set[str] = set()
        source_record_identities: set[str] = set()
        for atom_id in source_atom_ids:
            atom = atoms_by_id.get(atom_id, {})
            run_rel = _clean_string(atom.get("run_rel")) or _clean_string(atom.get("origin_run_id"))
            record = records_by_run.get(run_rel or "", {})
            source_root = _clean_string(atom.get("derived_source_root")) or _clean_string(
                record.get("derived_source_root")
            )
            source_root_kind = _clean_string(atom.get("derived_source_root_kind")) or _clean_string(
                record.get("derived_source_root_kind")
            )
            source_record_identity = _clean_string(
                atom.get("derived_source_record_identity")
            ) or _clean_string(record.get("derived_source_record_identity"))
            if source_root is None:
                source_root = resolved_primary_root
                source_root_kind = "usertest"
            if source_record_identity is None and run_rel is not None:
                source_record_identity = _sha256_json(
                    {
                        "source_root": os.path.normcase(source_root),
                        "run_rel": run_rel,
                        "target_ref_sha256": _target_ref_sha256(record),
                    }
                )
            source_roots.add(source_root)
            if source_root_kind is not None:
                source_root_kinds.add(source_root_kind)
            if source_record_identity is not None:
                source_record_identities.add(source_record_identity)
        sorted_roots = sorted(source_roots)
        sorted_kinds = sorted(source_root_kinds)
        sorted_identities = sorted(source_record_identities)
        candidate.update(
            {
                "derived_source_roots": sorted_roots,
                "derived_source_root_kinds": sorted_kinds,
                "derived_source_record_identities": sorted_identities,
            }
        )
        if len(sorted_roots) == 1:
            candidate["derived_source_root"] = sorted_roots[0]
        if len(sorted_kinds) == 1:
            candidate["derived_source_root_kind"] = sorted_kinds[0]
        if len(sorted_identities) == 1:
            candidate["derived_source_record_identity"] = sorted_identities[0]
        annotated.append(candidate)
    return annotated


def with_operational_candidate_metadata(
    metadata: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    updated = dict(metadata)
    updated["operational_failure_candidates"] = {
        "count": len(candidates),
        "atom_ids": sorted(
            atom_id
            for candidate in candidates
            for atom_id in [_clean_string(candidate.get("atom_id"))]
            if atom_id is not None
        ),
        "receipts": [
            {
                "atom_id": candidate.get("atom_id"),
                "source_record_identity": candidate.get("derived_source_record_identity"),
                "source_record_identities": list(
                    candidate.get("derived_source_record_identities") or []
                ),
                "source_roots": list(candidate.get("derived_source_roots") or []),
                "operational_failure_class": candidate.get("operational_failure_class"),
                "operational_failure_phase": candidate.get("operational_failure_phase"),
                "operational_candidate_signature": candidate.get("operational_candidate_signature"),
                "operational_candidate_receipt_sha256": candidate.get(
                    "operational_candidate_receipt_sha256"
                ),
                "source_derived_atom_ids": list(candidate.get("source_derived_atom_ids") or []),
            }
            for candidate in sorted(candidates, key=lambda item: str(item.get("atom_id") or ""))
        ],
    }
    return updated


__all__ = [
    "DerivedEvidenceIngestion",
    "PrimaryDerivedEvidence",
    "annotate_primary_derived_evidence",
    "annotate_operational_failure_candidates",
    "filter_derived_history_records",
    "inferred_implementation_runs_root",
    "ingest_derived_evidence_records",
    "with_operational_candidate_metadata",
]
