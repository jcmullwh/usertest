from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from backlog_repo.case_relation_receipts import validate_case_relation_receipt
from backlog_repo.outcomes import validate_outcome_record
from backlog_repo.plan_scope import parse_plan_target_contract_markdown
from backlog_repo.ticket_provenance import (
    canonical_plan_sha256,
    canonical_ticket_body_sha256,
    parse_verification_contract_markdown,
)

_EXTERNALLY_VERIFIED_STATES = frozenset(
    {
        "implemented",
        "tests_verified",
        "original_scenario_verified",
        "live_verified",
        "resolved",
        "mitigated",
    }
)
_RELATIONSHIP_STATES = frozenset({"duplicate", "superseded"})
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_SAFE_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_IMMUTABLE_TICKET_PROVENANCE_FIELDS = (
    "fingerprint",
    "case_id",
    "plan_revision_id",
    "ticket_body_sha256",
    "local_plan_sha256",
    "local_plan_filename",
    "verification_contract_sha256",
    "target_contract_sha256",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_utf8_raw(path: Path) -> str:
    """Decode UTF-8 without universal-newline translation.

    Historical outcome writers could produce CRCRLF.  The canonical markdown
    normalizer deliberately collapses that sequence, but ``Path.read_text`` first
    translates it into two logical newlines on Windows and irreversibly changes
    the content being hashed.
    """

    return path.read_bytes().decode("utf-8")


def _immutable_ticket_provenance(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Project provenance to the identity fixed before implementation review.

    Review artifacts predate the later binding of ``verified_implementation_head``.
    Comparing their whole object to the enriched durable record therefore rejects a
    valid handoff.  The reviewed head remains mandatory, but is checked separately
    against the review artifact's own ``reviewed_head_oid`` field.
    """

    return {field: value.get(field) for field in _IMMUTABLE_TICKET_PROVENANCE_FIELDS}


def _verify_review_ticket_binding(
    review: Mapping[str, Any],
    *,
    label: str,
    provenance: Mapping[str, Any],
    errors: list[str],
) -> None:
    reviewed_provenance = review.get("ticket_provenance")
    if not isinstance(reviewed_provenance, Mapping) or (
        _immutable_ticket_provenance(reviewed_provenance)
        != _immutable_ticket_provenance(provenance)
    ):
        errors.append(f"{label}_ticket_provenance_mismatch")

    reviewed_head = str(review.get("reviewed_head_oid") or "").casefold()
    verified_head = str(provenance.get("verified_implementation_head") or "").casefold()
    if not reviewed_head or reviewed_head != verified_head:
        errors.append(f"{label}_verified_implementation_head_mismatch")


def _read_json(path: Path, *, label: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append(f"{label}_missing_or_invalid:{path}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label}_not_object:{path}")
        return None
    return value


def _under_any(path: Path, roots: Sequence[Path]) -> bool:
    resolved = path.expanduser().resolve()
    return any(resolved.is_relative_to(root.expanduser().resolve()) for root in roots)


def _trusted_absolute_path(
    value: Any,
    *,
    label: str,
    roots: Sequence[Path],
    errors: list[str],
) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label}_missing")
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        errors.append(f"{label}_not_absolute:{value}")
        return None
    resolved = path.resolve()
    if not _under_any(resolved, roots):
        errors.append(f"{label}_outside_trusted_roots:{resolved}")
        return None
    return resolved


def _ticket_provenance(record: dict[str, Any], errors: list[str]) -> dict[str, Any] | None:
    provenance = record.get("ticket_provenance")
    if not isinstance(provenance, dict) or provenance.get("schema_version") != 1:
        errors.append("outcome_ticket_provenance_missing_or_invalid")
        return None
    for field, expected in (
        ("case_id", record["case_id"]),
        ("plan_revision_id", record["plan_revision_id"]),
    ):
        if provenance.get(field) != expected:
            errors.append(f"outcome_ticket_provenance_{field}_mismatch")
    fingerprint = provenance.get("fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{16}", fingerprint) is None:
        errors.append("outcome_ticket_provenance_fingerprint_invalid")
    for field in (
        "ticket_body_sha256",
        "local_plan_sha256",
        "verification_contract_sha256",
        "target_contract_sha256",
    ):
        value = provenance.get(field)
        if field == "target_contract_sha256" and value is None:
            continue
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            errors.append(f"outcome_ticket_provenance_{field}_invalid")
    filename = provenance.get("local_plan_filename")
    if not isinstance(filename, str) or not filename.strip() or Path(filename).name != filename:
        errors.append("outcome_ticket_provenance_local_plan_filename_invalid")
    for field in ("verified_implementation_head",):
        value = provenance.get(field)
        if not isinstance(value, str) or re.fullmatch(
            r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value
        ) is None:
            errors.append(f"outcome_ticket_provenance_{field}_invalid")
    return provenance


def _find_verified_plan(
    provenance: dict[str, Any],
    *,
    owner_roots: Sequence[Path],
    errors: list[str],
) -> tuple[Path, Path] | None:
    filename = str(provenance.get("local_plan_filename") or "")
    candidates: list[tuple[Path, Path]] = []
    for owner_root in owner_roots:
        root = owner_root.expanduser().resolve()
        plans_root = root / ".agents" / "plans"
        if not plans_root.is_dir():
            continue
        for candidate in plans_root.rglob(filename):
            if candidate.is_file():
                candidates.append((root, candidate.resolve()))
    if not candidates:
        errors.append(f"outcome_local_plan_missing:{filename}")
        return None

    matching: list[tuple[Path, Path]] = []
    for root, candidate in candidates:
        try:
            markdown = _read_utf8_raw(candidate)
        except (OSError, UnicodeError):
            continue
        if "\x00" in markdown:
            continue
        if canonical_plan_sha256(markdown) != provenance.get("local_plan_sha256"):
            continue
        if canonical_ticket_body_sha256(markdown) != provenance.get("ticket_body_sha256"):
            continue
        metadata_pairs = (
            ("Fingerprint", provenance.get("fingerprint")),
            ("Case ID", provenance.get("case_id")),
            ("Plan revision ID", provenance.get("plan_revision_id")),
        )
        if all(
            re.search(
                rf"^-\s*{re.escape(label)}:\s*`{re.escape(str(expected))}`\s*$",
                markdown,
                flags=re.MULTILINE,
            )
            is not None
            for label, expected in metadata_pairs
        ):
            matching.append((root, candidate))
    if not matching:
        errors.append(f"outcome_local_plan_hash_or_identity_mismatch:{filename}")
        return None
    matching.sort(key=lambda item: (str(item[0]), str(item[1])))
    return matching[0]


def _role_contracts_from_verified_plan(
    plan_path: Path,
    *,
    provenance: Mapping[str, Any],
    errors: list[str],
) -> Mapping[str, Any]:
    try:
        verification_contract = parse_verification_contract_markdown(
            _read_utf8_raw(plan_path)
        )
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(f"outcome_plan_verification_contract_invalid:{type(exc).__name__}")
        return {}
    if not isinstance(verification_contract, dict):
        errors.append("outcome_plan_verification_contract_invalid:missing")
        return {}
    if verification_contract.get("contract_sha256") != provenance.get(
        "verification_contract_sha256"
    ):
        errors.append("outcome_plan_verification_contract_hash_mismatch")
        return {}
    roles_raw = verification_contract.get("outcome_roles")
    return roles_raw if isinstance(roles_raw, Mapping) else {}


def _case_identity_from_verified_plan(
    plan_path: Path,
    *,
    provenance: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Recover stable case identity only from an exact, provenance-bound plan."""

    target_contract_sha256 = provenance.get("target_contract_sha256")
    if target_contract_sha256 is None:
        return None, []
    errors: list[str] = []
    try:
        contract = parse_plan_target_contract_markdown(_read_utf8_raw(plan_path))
    except (OSError, UnicodeError, ValueError) as exc:
        return None, [f"verified_case_identity_target_contract_invalid:{type(exc).__name__}"]
    if not isinstance(contract, dict):
        return None, ["verified_case_identity_target_contract_missing"]
    if contract.get("contract_sha256") != target_contract_sha256:
        errors.append("verified_case_identity_target_contract_hash_mismatch")
    case_id = contract.get("case_id")
    if case_id != provenance.get("case_id"):
        errors.append("verified_case_identity_case_id_mismatch")
    problem_id = contract.get("problem_id")
    if not isinstance(problem_id, str) or not problem_id.strip():
        errors.append("verified_case_identity_problem_id_missing")
    if errors:
        return None, errors
    return (
        {
            "schema_version": 1,
            "source": "verified_plan_target_contract",
            "case_id": case_id,
            "problem_id": problem_id.strip(),
            "plan_revision_id": provenance.get("plan_revision_id"),
            "fingerprint": provenance.get("fingerprint"),
            "target_contract_sha256": target_contract_sha256,
            "verified_plan_path": str(plan_path),
        },
        [],
    )


def _verify_ticket_ref(
    ticket_ref: dict[str, Any],
    *,
    provenance: dict[str, Any],
    record: dict[str, Any],
    implementation_run_dir: Path | None = None,
    errors: list[str],
) -> dict[str, Any] | None:
    if ticket_ref.get("schema_version") != 2:
        errors.append("outcome_ticket_ref_schema_invalid")
        return None
    for field, expected in (
        ("fingerprint", provenance.get("fingerprint")),
        ("case_id", record["case_id"]),
        ("plan_revision_id", record["plan_revision_id"]),
    ):
        if ticket_ref.get(field) != expected:
            errors.append(f"outcome_ticket_ref_{field}_mismatch")
    stored_provenance = ticket_ref.get("ticket_provenance")
    if not isinstance(stored_provenance, dict):
        errors.append("outcome_ticket_ref_provenance_missing")
        return None
    for field in (
        "fingerprint",
        "case_id",
        "plan_revision_id",
        "ticket_body_sha256",
        "local_plan_sha256",
        "local_plan_filename",
        "verification_contract_sha256",
        "target_contract_sha256",
    ):
        if stored_provenance.get(field) != provenance.get(field):
            errors.append(f"outcome_ticket_ref_provenance_{field}_mismatch")

    implementation = ticket_ref.get("implementation_provenance")
    implementation_schema = (
        implementation.get("schema_version")
        if isinstance(implementation, dict)
        else None
    )
    if implementation is not None and implementation_schema not in {1, 2}:
        errors.append("outcome_ticket_ref_implementation_provenance_schema_invalid")
    elif provenance.get("target_contract_sha256") is not None and implementation is None:
        errors.append("outcome_ticket_ref_implementation_provenance_missing")
    elif isinstance(implementation, dict):
        required_fields = {
            "schema_version",
            "repo_revision",
            "execution_base_revision",
            "verified_implementation_head",
            "verification_sha256",
            "target_ref_sha256",
            "git_ref_sha256",
            "receipt_sha256",
        }
        if implementation_schema == 2:
            required_fields.update({"provenance_mode", "workspace_ref_sha256"})
        if set(implementation) != required_fields:
            errors.append("outcome_ticket_ref_implementation_provenance_fields_invalid")
        receipt_hash = implementation.get("receipt_sha256")
        unsigned_receipt = {
            key: value for key, value in implementation.items() if key != "receipt_sha256"
        }
        if not isinstance(receipt_hash, str) or _sha256_json(unsigned_receipt) != receipt_hash:
            errors.append("outcome_ticket_ref_implementation_provenance_hash_mismatch")
        for field in ("verified_implementation_head",):
            if str(implementation.get(field) or "").casefold() != str(
                provenance.get(field) or ""
            ).casefold():
                errors.append(
                    f"outcome_ticket_ref_implementation_provenance_{field}_mismatch"
                )
        target_contract = stored_provenance.get("target_contract")
        if not isinstance(target_contract, Mapping):
            errors.append(
                "outcome_ticket_ref_implementation_provenance_target_contract_missing"
            )
        else:
            if target_contract.get("contract_sha256") != provenance.get(
                "target_contract_sha256"
            ):
                errors.append(
                    "outcome_ticket_ref_implementation_provenance_target_contract_hash_mismatch"
                )
            if implementation.get("repo_revision") != target_contract.get(
                "repo_revision"
            ):
                errors.append(
                    "outcome_ticket_ref_implementation_provenance_repo_revision_mismatch"
                )
        if implementation_schema == 2 and implementation.get(
            "provenance_mode"
        ) != "existing_clean_head":
            errors.append("outcome_ticket_ref_implementation_provenance_mode_invalid")
        if implementation_run_dir is not None:
            artifact_hashes = [
                ("verification_sha256", "verification.json"),
                ("target_ref_sha256", "target_ref.json"),
                ("git_ref_sha256", "git_ref.json"),
            ]
            if implementation_schema == 2:
                artifact_hashes.append(("workspace_ref_sha256", "workspace_ref.json"))
            for hash_field, filename in artifact_hashes:
                artifact_path = implementation_run_dir / filename
                if not artifact_path.is_file():
                    errors.append(
                        "outcome_ticket_ref_implementation_provenance_artifact_missing:"
                        f"{filename}"
                    )
                elif _sha256_file(artifact_path) != implementation.get(hash_field):
                    errors.append(
                        "outcome_ticket_ref_implementation_provenance_artifact_hash_mismatch:"
                        f"{filename}"
                    )

            verification = _read_json(
                implementation_run_dir / "verification.json",
                label="outcome_implementation_verification",
                errors=errors,
            )
            target_ref = _read_json(
                implementation_run_dir / "target_ref.json",
                label="outcome_implementation_target_ref",
                errors=errors,
            )
            git_ref = _read_json(
                implementation_run_dir / "git_ref.json",
                label="outcome_implementation_git_ref",
                errors=errors,
            )
            workspace_ref = (
                _read_json(
                    implementation_run_dir / "workspace_ref.json",
                    label="outcome_implementation_workspace_ref",
                    errors=errors,
                )
                if implementation_schema == 2
                else None
            )
            if verification is not None and verification.get("passed") is not True:
                errors.append(
                    "outcome_ticket_ref_implementation_provenance_verification_not_passed"
                )
            if target_ref is not None and str(
                target_ref.get("commit_sha") or ""
            ).casefold() != str(
                implementation.get("execution_base_revision") or ""
            ).casefold():
                errors.append(
                    "outcome_ticket_ref_implementation_provenance_execution_base_mismatch"
                )
            if git_ref is not None:
                verified_head = str(
                    implementation.get("verified_implementation_head") or ""
                ).casefold()
                if str(git_ref.get("head_commit") or "").casefold() != verified_head:
                    errors.append(
                        "outcome_ticket_ref_implementation_provenance_git_head_mismatch"
                    )
                if implementation_schema == 2 and (
                    not isinstance(git_ref.get("commit_attempted"), bool)
                    or git_ref.get("commit_performed") is not False
                    or git_ref.get("commit_observed") is not True
                    or str(git_ref.get("base_commit") or "").casefold()
                    != verified_head
                ):
                    errors.append(
                        "outcome_ticket_ref_implementation_provenance_existing_head_invalid"
                    )
            if implementation_schema == 2 and workspace_ref is not None:
                workspace_strategy = workspace_ref.get("workspace_strategy")
                if (
                    workspace_ref.get("schema_version") != 1
                    or not isinstance(workspace_ref.get("workspace_dir"), str)
                    or not str(workspace_ref.get("workspace_dir") or "").strip()
                    or (
                        workspace_strategy is not None
                        and workspace_strategy != implementation.get("provenance_mode")
                    )
                ):
                    errors.append(
                        "outcome_ticket_ref_implementation_provenance_workspace_invalid"
                    )

    binding = ticket_ref.get("verification_binding")
    if not isinstance(binding, dict) or binding.get("schema_version") != 1:
        errors.append("outcome_verification_binding_missing_or_invalid")
        return None
    binding_hash = binding.get("binding_sha256")
    without_hash = {key: value for key, value in binding.items() if key != "binding_sha256"}
    if not isinstance(binding_hash, str) or _sha256_json(without_hash) != binding_hash:
        errors.append("outcome_verification_binding_hash_mismatch")
    for field in (
        "fingerprint",
        "case_id",
        "plan_revision_id",
        "ticket_body_sha256",
        "local_plan_sha256",
    ):
        if binding.get(field) != stored_provenance.get(field):
            errors.append(f"outcome_verification_binding_{field}_mismatch")
    if (
        binding.get("plan_verification_contract_sha256")
        != provenance.get("verification_contract_sha256")
    ):
        errors.append("outcome_verification_binding_contract_hash_mismatch")
    if binding.get("plan_target_contract_sha256") != provenance.get(
        "target_contract_sha256"
    ):
        errors.append("outcome_verification_binding_target_contract_hash_mismatch")
    return binding


def _verify_receipt(
    receipt: dict[str, Any],
    *,
    evidence_kind: str,
    record: dict[str, Any],
    provenance: dict[str, Any],
    trusted_runs_roots: Sequence[Path],
    expected_implementation_run_dir: Path | None,
    errors: list[str],
) -> None:
    prefix = f"outcome_{evidence_kind}_receipt"
    run_dir = _trusted_absolute_path(
        receipt.get("run_dir"),
        label=f"{prefix}_run_dir",
        roots=trusted_runs_roots,
        errors=errors,
    )
    verification_path = _trusted_absolute_path(
        receipt.get("verification_path"),
        label=f"{prefix}_verification_path",
        roots=trusted_runs_roots,
        errors=errors,
    )
    ticket_ref_path = _trusted_absolute_path(
        receipt.get("ticket_ref_path"),
        label=f"{prefix}_ticket_ref_path",
        roots=trusted_runs_roots,
        errors=errors,
    )
    if run_dir is None or verification_path is None or ticket_ref_path is None:
        return
    if verification_path != run_dir / "verification.json":
        errors.append(f"{prefix}_verification_path_not_canonical")
    if ticket_ref_path != run_dir / "ticket_ref.json":
        errors.append(f"{prefix}_ticket_ref_path_not_canonical")
    if expected_implementation_run_dir is not None and run_dir != expected_implementation_run_dir:
        errors.append(f"{prefix}_implementation_run_mismatch")
    for path, hash_field in (
        (verification_path, "verification_sha256"),
        (ticket_ref_path, "ticket_ref_sha256"),
    ):
        if not path.is_file():
            errors.append(f"{prefix}_{hash_field}_artifact_missing:{path}")
            continue
        if _sha256_file(path).casefold() != str(receipt.get(hash_field) or "").casefold():
            errors.append(f"{prefix}_{hash_field}_mismatch")

    ticket_ref = _read_json(ticket_ref_path, label=f"{prefix}_ticket_ref", errors=errors)
    binding = (
        _verify_ticket_ref(
            ticket_ref,
            provenance=provenance,
            record=record,
            implementation_run_dir=run_dir,
            errors=errors,
        )
        if ticket_ref is not None
        else None
    )
    verification = _read_json(
        verification_path,
        label=f"{prefix}_verification",
        errors=errors,
    )
    if verification is None or binding is None:
        return
    if (
        verification.get("schema_version") != 1
        or verification.get("passed") is not True
        or str(verification.get("status") or "").casefold() != "passed"
        or str(verification.get("terminal_reason") or "").casefold() != "passed"
        or verification.get("timed_out") is not False
        or verification.get("cancelled") is not False
    ):
        errors.append(f"{prefix}_verification_not_terminal_pass")
    configured = verification.get("commands_configured")
    executed = verification.get("commands")
    bound_commands = binding.get("configured_commands")
    if configured != bound_commands or receipt.get("commands") != bound_commands:
        errors.append(f"{prefix}_command_contract_mismatch")
        return
    if not isinstance(configured, list) or not configured or not isinstance(executed, list):
        errors.append(f"{prefix}_commands_missing")
        return
    if len(configured) != len(executed):
        errors.append(f"{prefix}_command_coverage_mismatch")
        return
    for index, (command, result) in enumerate(zip(configured, executed, strict=True)):
        if not isinstance(result, dict) or result.get("command") != command:
            errors.append(f"{prefix}_command_{index}_identity_mismatch")
            continue
        if (
            isinstance(result.get("exit_code"), bool)
            or result.get("exit_code") != 0
            or result.get("timed_out") is not False
            or result.get("cancelled") is not False
            or result.get("dispatch_blocked") is True
            or result.get("rejected_sentinel") is True
        ):
            errors.append(f"{prefix}_command_{index}_not_passed")
    if receipt.get("evidence_kind") != evidence_kind:
        errors.append(f"{prefix}_evidence_kind_mismatch")
    for field in (
        "fingerprint",
        "case_id",
        "plan_revision_id",
        "ticket_body_sha256",
        "local_plan_sha256",
        "local_plan_filename",
        "verification_contract_sha256",
        "target_contract_sha256",
    ):
        expected = (
            record[field]
            if field in {"case_id", "plan_revision_id"}
            else provenance.get(field)
        )
        if receipt.get(field) != expected:
            errors.append(f"{prefix}_{field}_mismatch")
    if receipt.get("verification_binding_sha256") != binding.get("binding_sha256"):
        errors.append(f"{prefix}_binding_hash_mismatch")


def _verify_role_receipt(
    receipt: dict[str, Any],
    *,
    evidence_kind: str,
    record: dict[str, Any],
    provenance: dict[str, Any],
    role_contract: Mapping[str, Any] | None,
    trusted_runs_roots: Sequence[Path],
    errors: list[str],
) -> None:
    prefix = f"outcome_{evidence_kind}_receipt"
    path = _trusted_absolute_path(
        receipt.get("role_artifact_path"),
        label=f"{prefix}_role_artifact_path",
        roots=trusted_runs_roots,
        errors=errors,
    )
    if path is None:
        return
    if not path.is_file():
        errors.append(f"{prefix}_artifact_missing:{path}")
        return
    if _sha256_file(path).casefold() != str(
        receipt.get("role_artifact_sha256") or ""
    ).casefold():
        errors.append(f"{prefix}_artifact_hash_mismatch")
    artifact = _read_json(path, label=f"{prefix}_artifact", errors=errors)
    if artifact is None:
        return
    content_hash = artifact.get("artifact_content_sha256")
    unsigned_artifact = {
        key: value for key, value in artifact.items() if key != "artifact_content_sha256"
    }
    if content_hash != _sha256_json(unsigned_artifact):
        errors.append(f"{prefix}_content_hash_mismatch")
    if not isinstance(role_contract, Mapping):
        errors.append(f"{prefix}_stage6_role_contract_missing")
        return
    role_contract_hash = role_contract.get("role_contract_sha256")
    receipt_schema = receipt.get("receipt_schema_version")
    amendment = record.get("verification_amendment")
    amended_receipt = receipt_schema == 4
    execution_commit = (
        amendment.get("verification_commit")
        if amended_receipt and isinstance(amendment, Mapping)
        else None
    )
    amendment_id = (
        amendment.get("amendment_id")
        if amended_receipt and isinstance(amendment, Mapping)
        else None
    )
    expected = {
        "schema_version": 1,
        "producer": "runner_core",
        "role": evidence_kind,
        "case_id": record.get("case_id"),
        "plan_revision_id": record.get("plan_revision_id"),
        "merged_commit": str(record.get("merged_commit") or "").casefold(),
        "verification_contract_sha256": provenance.get("verification_contract_sha256"),
        "target_contract_sha256": provenance.get("target_contract_sha256"),
        "verified_implementation_head": provenance.get("verified_implementation_head"),
        "role_contract_sha256": role_contract_hash,
        "role_contract": dict(role_contract),
    }
    if amended_receipt:
        expected["execution_commit"] = execution_commit
        expected["verification_amendment_id"] = amendment_id
    for field, expected_value in expected.items():
        observed = artifact.get(field)
        if isinstance(expected_value, str) and (
            field.endswith("sha256")
            or field
            in {"merged_commit", "execution_commit", "verified_implementation_head"}
        ):
            observed = str(observed or "").casefold()
            expected_value = expected_value.casefold()
        if observed != expected_value:
            errors.append(f"{prefix}_{field}_mismatch")
    receipt_expected = [
        ("receipt_schema_version", receipt_schema),
        ("producer", "usertest_implement"),
        ("verification_producer", "runner_core"),
        ("evidence_kind", evidence_kind),
        ("case_id", record.get("case_id")),
        ("plan_revision_id", record.get("plan_revision_id")),
        ("merged_commit", str(record.get("merged_commit") or "").casefold()),
        ("fingerprint", provenance.get("fingerprint")),
        ("ticket_body_sha256", provenance.get("ticket_body_sha256")),
        ("local_plan_sha256", provenance.get("local_plan_sha256")),
        ("local_plan_filename", provenance.get("local_plan_filename")),
        ("verification_contract_sha256", provenance.get("verification_contract_sha256")),
        ("target_contract_sha256", provenance.get("target_contract_sha256")),
        (
            "verified_implementation_head",
            provenance.get("verified_implementation_head"),
        ),
        ("role_contract_sha256", role_contract_hash),
    ]
    if amended_receipt:
        receipt_expected.extend(
            [
                ("execution_commit", execution_commit),
                ("verification_amendment_id", amendment_id),
            ]
        )
    if receipt_schema not in {3, 4}:
        errors.append(f"{prefix}_receipt_schema_version_mismatch")
    for field, expected_value in receipt_expected:
        observed = receipt.get(field)
        if isinstance(expected_value, str) and (
            field.endswith("sha256")
            or field
            in {"merged_commit", "execution_commit", "verified_implementation_head"}
        ):
            observed = str(observed or "").casefold()
            expected_value = expected_value.casefold()
        if observed != expected_value:
            errors.append(f"{prefix}_{field}_mismatch")
    commands = artifact.get("commands")
    predicates = artifact.get("predicate_results")
    configured_commands = role_contract.get("commands")
    configured_predicates = role_contract.get("predicates")
    oracle = role_contract.get("oracle")
    expected_commands = configured_commands
    if isinstance(oracle, Mapping) and oracle.get("kind") == "staged_replay":
        execution = oracle.get("execution")
        argv = execution.get("argv") if isinstance(execution, Mapping) else None
        expected_commands = [" ".join(argv)] if isinstance(argv, list) else None
    if (
        not isinstance(commands, list)
        or not isinstance(expected_commands, list)
        or [item.get("command") if isinstance(item, dict) else None for item in commands]
        != expected_commands
    ):
        errors.append(f"{prefix}_command_contract_mismatch")
    elif any(
        item.get("timed_out") is not False
        or item.get("cancelled") is not False
        for item in commands
        if isinstance(item, dict)
    ):
        errors.append(f"{prefix}_command_blocked")
    if isinstance(oracle, Mapping):
        if (
            artifact.get("outcome_oracle_id") != oracle.get("outcome_oracle_id")
            or artifact.get("proof_scope") != oracle.get("proof_scope")
            or receipt.get("outcome_oracle_id") != oracle.get("outcome_oracle_id")
            or receipt.get("proof_scope") != oracle.get("proof_scope")
            or artifact.get("execution_integrity") is not True
        ):
            errors.append(f"{prefix}_oracle_binding_mismatch")
    if (
        not isinstance(predicates, list)
        or not isinstance(configured_predicates, list)
        or len(predicates) != len(configured_predicates)
    ):
        errors.append(f"{prefix}_predicate_coverage_mismatch")
    else:
        for index, (configured, result) in enumerate(
            zip(configured_predicates, predicates, strict=True)
        ):
            if (
                not isinstance(result, dict)
                or result.get("predicate_index") != index
                or result.get("predicate") != configured
                or result.get("passed") is not True
                or result.get("error") is not None
            ):
                errors.append(f"{prefix}_predicate_{index}_not_passed")
            snapshot_receipt = result.get("artifact_receipt") if isinstance(result, dict) else None
            if isinstance(snapshot_receipt, dict):
                snapshot = _trusted_absolute_path(
                    snapshot_receipt.get("snapshot_path"),
                    label=f"{prefix}_predicate_{index}_snapshot_path",
                    roots=trusted_runs_roots,
                    errors=errors,
                )
                if snapshot is not None and (
                    not snapshot.is_file()
                    or _sha256_file(snapshot) != snapshot_receipt.get("snapshot_sha256")
                ):
                    errors.append(f"{prefix}_predicate_{index}_snapshot_changed")
    if (
        artifact.get("passed") is not True
        or artifact.get("timed_out") is not False
        or artifact.get("cancelled") is not False
    ):
        errors.append(f"{prefix}_artifact_not_terminal_pass")


def _git_output(repo_root: Path, argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *argv],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 127, ""
    return int(proc.returncode), str(proc.stdout or "").strip()


def _verify_merge_provenance(
    record: dict[str, Any],
    *,
    owner_root: Path,
    errors: list[str],
) -> None:
    commit = str(record.get("merged_commit") or "").strip()
    branch = str(record.get("target_branch") or "").strip()
    if not commit or not branch or _SAFE_BRANCH_RE.fullmatch(branch) is None:
        errors.append("outcome_merge_provenance_invalid")
        return
    commit_rc, resolved_commit = _git_output(
        owner_root,
        ["rev-parse", "--verify", f"{commit}^{{commit}}"],
    )
    if commit_rc != 0 or not resolved_commit:
        errors.append(f"outcome_merged_commit_missing:{commit}")
        return
    ticket_provenance = record.get("ticket_provenance")
    verified_head = (
        ticket_provenance.get("verified_implementation_head")
        if isinstance(ticket_provenance, Mapping)
        else None
    )
    if isinstance(verified_head, str) and verified_head.strip():
        head_rc, resolved_head = _git_output(
            owner_root,
            ["rev-parse", "--verify", f"{verified_head.strip()}^{{commit}}"],
        )
        if head_rc != 0 or not resolved_head:
            errors.append(f"outcome_verified_implementation_head_missing:{verified_head}")
        else:
            implementation_ancestor_rc, _ = _git_output(
                owner_root,
                ["merge-base", "--is-ancestor", resolved_head, resolved_commit],
            )
            if implementation_ancestor_rc != 0:
                errors.append(
                    "outcome_verified_implementation_head_not_in_merged_commit:"
                    f"{verified_head}:{commit}"
                )
    target_commits: list[str] = []
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        rc, value = _git_output(owner_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
        if rc == 0 and value and value not in target_commits:
            target_commits.append(value)
    if not target_commits:
        errors.append(f"outcome_target_branch_missing:{branch}")
        return
    if not any(
        _git_output(
            owner_root,
            ["merge-base", "--is-ancestor", resolved_commit, target_commit],
        )[0]
        == 0
        for target_commit in target_commits
    ):
        errors.append(f"outcome_merged_commit_not_on_target_branch:{commit}:{branch}")
    amendment = record.get("verification_amendment")
    if not isinstance(amendment, Mapping):
        return
    verification_commit = str(amendment.get("verification_commit") or "").strip()
    verification_rc, resolved_verification = _git_output(
        owner_root,
        ["rev-parse", "--verify", f"{verification_commit}^{{commit}}"],
    )
    if verification_rc != 0 or not resolved_verification:
        errors.append(
            f"outcome_verification_amendment_commit_missing:{verification_commit}"
        )
        return
    implementation_ancestor_rc, _ = _git_output(
        owner_root,
        ["merge-base", "--is-ancestor", resolved_commit, resolved_verification],
    )
    if implementation_ancestor_rc != 0 or resolved_commit == resolved_verification:
        errors.append(
            "outcome_verification_amendment_not_descendant:"
            f"{commit}:{verification_commit}"
        )
    if not any(
        _git_output(
            owner_root,
            ["merge-base", "--is-ancestor", resolved_verification, target_commit],
        )[0]
        == 0
        for target_commit in target_commits
    ):
        errors.append(
            "outcome_verification_amendment_not_on_target_branch:"
            f"{verification_commit}:{branch}"
        )


def _registry_relation_target(entry: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    targets = [
        value.strip()
        for field in ("alias_of", "duplicate_of", "superseded_by")
        for value in [entry.get(field)]
        if isinstance(value, str) and value.strip()
    ]
    distinct = list(dict.fromkeys(targets))
    if len(distinct) > 1:
        return None, distinct
    return (distinct[0] if distinct else None), distinct


def _verify_registry_relation(
    *,
    source_case_id: str,
    target_case_id: str,
    relation_kind: str,
    relation_reference: Mapping[str, Any],
    case_registry: Mapping[str, Any] | None,
    errors: list[str],
) -> None:
    if source_case_id == target_case_id:
        errors.append(f"outcome_relationship_self_relation:{source_case_id}")
        return
    cases_raw = case_registry.get("cases") if isinstance(case_registry, Mapping) else None
    cases = cases_raw if isinstance(cases_raw, Mapping) else None
    if cases is None:
        errors.append("outcome_relationship_case_registry_missing")
        return
    source_raw = cases.get(source_case_id)
    target_raw = cases.get(target_case_id)
    if not isinstance(source_raw, Mapping):
        errors.append(f"outcome_relationship_source_case_missing:{source_case_id}")
        return
    if not isinstance(target_raw, Mapping):
        errors.append(f"outcome_relationship_target_case_missing:{target_case_id}")
        return

    source_target, conflicting = _registry_relation_target(source_raw)
    if conflicting and source_target is None:
        errors.append(f"outcome_relationship_registry_source_conflict:{source_case_id}")
        return
    expected_field = "alias_of" if relation_kind == "canonical_absorption" else "superseded_by"
    if source_raw.get(expected_field) != target_case_id or source_target != target_case_id:
        errors.append(
            "outcome_relationship_registry_direction_mismatch:"
            f"{source_case_id}:{target_case_id}:{expected_field}"
        )
    stored_reference = source_raw.get("relation_receipt")
    if not isinstance(stored_reference, Mapping) or any(
        stored_reference.get(field) != relation_reference.get(field)
        for field in (
            "schema_version",
            "producer",
            "receipt_path",
            "receipt_sha256",
            "relation_sha256",
            "source_case_id",
            "target_case_id",
            "direction",
            "relation_kind",
        )
    ):
        errors.append(f"outcome_relationship_registry_receipt_mismatch:{source_case_id}")

    target_target, target_conflicting = _registry_relation_target(target_raw)
    if target_conflicting and target_target is None:
        errors.append(f"outcome_relationship_registry_target_conflict:{target_case_id}")
    elif target_target is not None:
        errors.append(f"outcome_relationship_target_not_canonical:{target_case_id}:{target_target}")

    seen: set[str] = set()
    cursor = source_case_id
    while True:
        if cursor in seen:
            errors.append(f"outcome_relationship_registry_cycle:{source_case_id}")
            break
        seen.add(cursor)
        entry = cases.get(cursor)
        if not isinstance(entry, Mapping):
            break
        next_case, next_conflicting = _registry_relation_target(entry)
        if next_conflicting and next_case is None:
            errors.append(f"outcome_relationship_registry_cycle_check_conflict:{cursor}")
            break
        if next_case is None:
            break
        cursor = next_case


def _verify_relationship_provenance(
    record: dict[str, Any],
    *,
    trusted_runs_roots: Sequence[Path],
    case_registry: Mapping[str, Any] | None,
    errors: list[str],
) -> None:
    source_case_id = str(record["case_id"])
    related_case = record.get("related_case_id")
    if not isinstance(related_case, str) or not related_case.strip():
        errors.append("outcome_relationship_related_case_required")
        return
    target_case_id = related_case.strip()
    if source_case_id == target_case_id:
        errors.append(f"outcome_relationship_self_relation:{source_case_id}")

    reference = record.get("relation_receipt")
    if not isinstance(reference, Mapping):
        errors.append("outcome_relationship_receipt_missing")
        return
    for field, expected in (
        ("schema_version", 1),
        ("producer", "usertest_backlog"),
        ("source_case_id", source_case_id),
        ("target_case_id", target_case_id),
        ("direction", "source_to_canonical"),
    ):
        if reference.get(field) != expected:
            errors.append(f"outcome_relationship_receipt_{field}_mismatch")

    trusted_roots = tuple(root.expanduser().resolve() for root in trusted_runs_roots)
    if not trusted_roots:
        errors.append("outcome_trusted_runs_roots_empty")
        return
    receipt_path = _trusted_absolute_path(
        reference.get("receipt_path"),
        label="outcome_relationship_receipt_path",
        roots=trusted_roots,
        errors=errors,
    )
    if receipt_path is None:
        return
    if not receipt_path.is_file():
        errors.append(f"outcome_relationship_receipt_artifact_missing:{receipt_path}")
        return
    if _sha256_file(receipt_path).casefold() != str(
        reference.get("receipt_sha256") or ""
    ).casefold():
        errors.append("outcome_relationship_receipt_file_hash_mismatch")

    payload = _read_json(
        receipt_path,
        label="outcome_relationship_receipt",
        errors=errors,
    )
    if payload is None:
        return
    try:
        normalized_receipt = validate_case_relation_receipt(payload)
    except ValueError as exc:
        errors.append(str(exc))
        return

    response_path = _trusted_absolute_path(
        normalized_receipt.get("relation_review_response_path"),
        label="outcome_relationship_review_response_path",
        roots=trusted_roots,
        errors=errors,
    )
    if response_path is not None:
        if response_path.parent != receipt_path.parent:
            errors.append("outcome_relationship_receipt_response_not_sibling")
        if not response_path.is_file():
            errors.append(f"outcome_relationship_review_response_missing:{response_path}")
        elif _sha256_file(response_path) != normalized_receipt.get(
            "relation_review_response_sha256"
        ):
            errors.append("outcome_relationship_review_response_hash_mismatch")

    relation_hash = reference.get("relation_sha256")
    matching = [
        relation
        for relation in normalized_receipt["relations"]
        if relation.get("relation_sha256") == relation_hash
    ]
    if len(matching) != 1:
        errors.append("outcome_relationship_receipt_relation_not_unique")
        return
    relation = matching[0]
    for field, expected in (
        ("source_case_id", source_case_id),
        ("target_case_id", target_case_id),
        ("direction", "source_to_canonical"),
        ("relation_kind", reference.get("relation_kind")),
    ):
        if relation.get(field) != expected:
            errors.append(f"outcome_relationship_relation_{field}_mismatch")

    relation_kind = str(relation.get("relation_kind") or "")
    expected_kind = (
        "canonical_absorption" if record.get("state") == "duplicate" else "supersession"
    )
    if relation_kind != expected_kind:
        errors.append(
            f"outcome_relationship_relation_kind_state_mismatch:{relation_kind}:{record['state']}"
        )
    _verify_registry_relation(
        source_case_id=source_case_id,
        target_case_id=target_case_id,
        relation_kind=relation_kind,
        relation_reference=reference,
        case_registry=case_registry,
        errors=errors,
    )


def verify_outcome_record_provenance(
    record: dict[str, Any],
    *,
    trusted_runs_roots: Sequence[Path],
    owner_roots: Sequence[Path],
    case_registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Separate structural OutcomeRecord validity from retained-evidence trust.

    Structural validation is necessary for storage and rendering, but it is never
    sufficient to advance or close a case. States that claim implementation or
    verification are trusted only after their plan, run artifacts, review handoff,
    receipt hashes, and merged commit/branch are re-opened under configured roots.
    """

    errors: list[str] = []
    try:
        normalized = validate_outcome_record(record)
    except ValueError as exc:
        return {
            "schema_version": 1,
            "structural_status": "invalid",
            "provenance_status": "not_checked",
            "verified": False,
            "errors": [str(exc)],
            "outcome_record": None,
        }

    state = str(normalized["state"])
    if state in {"planned", "unverified", "integrity_unknown"}:
        return {
            "schema_version": 1,
            "structural_status": "valid",
            "provenance_status": "not_required_nonterminal",
            "verified": True,
            "errors": [],
            "outcome_record": normalized,
        }
    if state in _RELATIONSHIP_STATES:
        if normalized.get("outcome_scope") == "plan_copy":
            return {
                "schema_version": 1,
                "structural_status": "valid",
                "provenance_status": "not_required_plan_copy",
                "verified": True,
                "errors": [],
                "outcome_record": normalized,
            }
        _verify_relationship_provenance(
            normalized,
            trusted_runs_roots=trusted_runs_roots,
            case_registry=case_registry,
            errors=errors,
        )
        return {
            "schema_version": 1,
            "structural_status": "valid",
            "provenance_status": "verified" if not errors else "failed",
            "verified": not errors,
            "errors": errors,
            "outcome_record": normalized,
        }
    if state not in _EXTERNALLY_VERIFIED_STATES:
        errors.append(f"outcome_state_verification_policy_missing:{state}")

    trusted_roots = tuple(root.expanduser().resolve() for root in trusted_runs_roots)
    owners = tuple(root.expanduser().resolve() for root in owner_roots)
    if not trusted_roots:
        errors.append("outcome_trusted_runs_roots_empty")
    if not owners:
        errors.append("outcome_owner_roots_empty")
    provenance = _ticket_provenance(normalized, errors)
    verified_plan = (
        _find_verified_plan(provenance, owner_roots=owners, errors=errors)
        if provenance is not None and owners
        else None
    )

    implementation_run_dir: Path | None = None
    if provenance is not None and trusted_roots:
        review_run_dir = _trusted_absolute_path(
            normalized.get("review_run_dir"),
            label="outcome_review_run_dir",
            roots=trusted_roots,
            errors=errors,
        )
        if review_run_dir is not None:
            review_ref = _read_json(
                review_run_dir / "review_ref.json",
                label="outcome_review_ref",
                errors=errors,
            )
            review_summary = _read_json(
                review_run_dir / "review_summary.json",
                label="outcome_review_summary",
                errors=errors,
            )
            merge_ref = _read_json(
                review_run_dir / "merge_ref.json",
                label="outcome_merge_ref",
                errors=errors,
            )
            if review_ref is not None:
                if review_ref.get("schema_version") != 2:
                    errors.append("outcome_review_ref_schema_invalid")
                implementation_run_dir = _trusted_absolute_path(
                    review_ref.get("implementation_run_dir"),
                    label="outcome_implementation_run_dir",
                    roots=trusted_roots,
                    errors=errors,
                )
                _verify_review_ticket_binding(
                    review_ref,
                    label="outcome_review_ref",
                    provenance=provenance,
                    errors=errors,
                )
                if implementation_run_dir is not None:
                    ticket_ref_path = implementation_run_dir / "ticket_ref.json"
                    if not ticket_ref_path.is_file():
                        errors.append("outcome_implementation_ticket_ref_missing")
                    else:
                        observed_hash = _sha256_file(ticket_ref_path)
                        if review_ref.get("implementation_ticket_ref_sha256") != observed_hash:
                            errors.append("outcome_review_ref_ticket_ref_hash_mismatch")
                        ticket_ref = _read_json(
                            ticket_ref_path,
                            label="outcome_implementation_ticket_ref",
                            errors=errors,
                        )
                        if ticket_ref is not None:
                            _verify_ticket_ref(
                                ticket_ref,
                                provenance=provenance,
                                record=normalized,
                                implementation_run_dir=implementation_run_dir,
                                errors=errors,
                            )
            if review_summary is not None:
                _verify_review_ticket_binding(
                    review_summary,
                    label="outcome_review_summary",
                    provenance=provenance,
                    errors=errors,
                )
                if review_ref is not None and (
                    review_summary.get("implementation_ticket_ref_sha256")
                    != review_ref.get("implementation_ticket_ref_sha256")
                ):
                    errors.append("outcome_review_summary_ticket_ref_hash_mismatch")
            if merge_ref is not None:
                if (
                    merge_ref.get("merged") is not True
                    or merge_ref.get("target_branch") != normalized.get("target_branch")
                    or merge_ref.get("merged_commit") != normalized.get("merged_commit")
                ):
                    errors.append("outcome_merge_ref_provenance_mismatch")

    if verified_plan is not None:
        owner_root, _plan_path = verified_plan
        _verify_merge_provenance(normalized, owner_root=owner_root, errors=errors)

    role_contracts: Mapping[str, Any] = {}
    verified_case_identity: dict[str, Any] | None = None
    verified_case_identity_errors: list[str] = []
    if verified_plan is not None and provenance is not None:
        _owner_root, plan_path = verified_plan
        role_contracts = _role_contracts_from_verified_plan(
            plan_path,
            provenance=provenance,
            errors=errors,
        )
        verified_case_identity, verified_case_identity_errors = (
            _case_identity_from_verified_plan(
                plan_path,
                provenance=provenance,
            )
        )

    if provenance is not None and trusted_roots:
        evidence_groups = (
            ("test", normalized.get("test_evidence", [])),
            ("original_scenario", normalized.get("original_scenario_evidence", [])),
            ("live", normalized.get("live_evidence", [])),
            ("mitigation_effect", normalized.get("mitigation_evidence", [])),
            ("recurrence", normalized.get("recurrence_check", {}).get("evidence", [])),
        )
        for evidence_kind, items in evidence_groups:
            for item in items if isinstance(items, list) else []:
                if (
                    not isinstance(item, dict)
                    or str(item.get("result") or "").casefold() != "passed"
                ):
                    continue
                receipt = item.get("runner_receipt")
                if not isinstance(receipt, dict):
                    errors.append(f"outcome_{evidence_kind}_receipt_missing")
                    continue
                if evidence_kind == "test":
                    _verify_receipt(
                        receipt,
                        evidence_kind=evidence_kind,
                        record=normalized,
                        provenance=provenance,
                        trusted_runs_roots=trusted_roots,
                        expected_implementation_run_dir=implementation_run_dir,
                        errors=errors,
                    )
                else:
                    role_contract = role_contracts.get(evidence_kind)
                    _verify_role_receipt(
                        receipt,
                        evidence_kind=evidence_kind,
                        record=normalized,
                        provenance=provenance,
                        role_contract=(
                            role_contract if isinstance(role_contract, Mapping) else None
                        ),
                        trusted_runs_roots=trusted_roots,
                        errors=errors,
                    )
    errors = list(dict.fromkeys(errors))
    if errors:
        verified_case_identity = None
    return {
        "schema_version": 1,
        "structural_status": "valid",
        "provenance_status": "verified" if not errors else "failed",
        "verified": not errors,
        "errors": errors,
        "outcome_record": normalized,
        "verified_case_identity": verified_case_identity,
        "verified_case_identity_errors": verified_case_identity_errors,
    }


__all__ = ["verify_outcome_record_provenance"]
