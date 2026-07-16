from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from backlog_repo.ticket_provenance import normalize_verification_commands
from runner_core import (
    validate_outcome_evidence_role_artifact,
    verification_command_safety_errors,
)

_SUPPORTED_EVIDENCE_KIND = "test"


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


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is missing or invalid: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return raw


def _required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value.strip()) != 64:
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value.strip())


def expected_ticket_identity(
    *,
    fingerprint: str,
    case_id: str | None,
    plan_revision_id: str | None,
) -> tuple[str, str]:
    normalized_case = (
        case_id.strip()
        if isinstance(case_id, str) and case_id.strip()
        else f"legacy-case:{fingerprint}"
    )
    normalized_plan = (
        plan_revision_id.strip()
        if isinstance(plan_revision_id, str) and plan_revision_id.strip()
        else f"legacy-plan:{fingerprint}"
    )
    return normalized_case, normalized_plan


def build_verification_binding(
    *,
    ticket_provenance: dict[str, Any],
    configured_commands: Sequence[str],
) -> dict[str, Any]:
    """Bind runner configuration to one immutable selected plan revision."""

    commands = [str(command).strip() for command in configured_commands]
    if any(not command for command in commands):
        raise ValueError("Configured verification commands must be non-empty strings")
    if len(commands) != len(set(commands)):
        raise ValueError("Configured verification commands must not contain duplicates")
    plan_contract_raw = ticket_provenance.get("verification_contract")
    plan_contract = plan_contract_raw if isinstance(plan_contract_raw, dict) else None
    plan_commands = (
        normalize_verification_commands(plan_contract.get("commands", []))
        if plan_contract is not None
        else []
    )
    plan_contract_sha256 = (
        _required_text(
            plan_contract.get("contract_sha256"),
            label="Plan verification contract hash",
        )
        if plan_contract is not None
        else None
    )
    if plan_contract is not None:
        for command in commands:
            safety_errors = verification_command_safety_errors(command)
            if safety_errors:
                raise ValueError(
                    f"Unsafe configured verification command {command!r}: "
                    + "; ".join(safety_errors)
                )
    eligible = bool(plan_contract is not None and commands == plan_commands)
    payload = {
        "schema_version": 1,
        "fingerprint": _required_text(
            ticket_provenance.get("fingerprint"),
            label="Ticket provenance fingerprint",
        ),
        "case_id": _required_text(
            ticket_provenance.get("case_id"),
            label="Ticket provenance case_id",
        ),
        "plan_revision_id": _required_text(
            ticket_provenance.get("plan_revision_id"),
            label="Ticket provenance plan_revision_id",
        ),
        "ticket_body_sha256": _required_text(
            ticket_provenance.get("ticket_body_sha256"),
            label="Ticket body hash",
        ),
        "local_plan_sha256": _required_text(
            ticket_provenance.get("local_plan_sha256"),
            label="Local plan hash",
        ),
        "plan_verification_contract_sha256": plan_contract_sha256,
        "plan_target_contract_sha256": (
            _required_text(
                ticket_provenance.get("target_contract_sha256"),
                label="Plan target contract hash",
            )
            if ticket_provenance.get("target_contract_sha256") is not None
            else None
        ),
        "plan_commands": plan_commands,
        "configured_commands": commands,
        "eligible_for_test_evidence": eligible,
    }
    return {**payload, "binding_sha256": _sha256_json(payload)}


def _validate_ticket_provenance_payload(
    raw: Any,
    *,
    fingerprint: str,
    case_id: str,
    plan_revision_id: str,
    expected: dict[str, Any] | None,
    owner_root: Path | None,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("Runner ticket_ref.json is missing ticket_provenance schema 1")
    exact = {
        "fingerprint": fingerprint,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
    }
    for field, expected_value in exact.items():
        if raw.get(field) != expected_value:
            raise ValueError(
                f"Runner ticket provenance {field} mismatch: "
                f"expected={expected_value!r} observed={raw.get(field)!r}"
            )
    for field in ("ticket_body_sha256", "local_plan_sha256"):
        if not _is_sha256(raw.get(field)):
            raise ValueError(f"Runner ticket provenance {field} must be a SHA-256 digest")
    target_contract_hash = raw.get("target_contract_sha256")
    if target_contract_hash is not None and not _is_sha256(target_contract_hash):
        raise ValueError(
            "Runner ticket provenance target_contract_sha256 must be null or SHA-256"
        )
    contract_hash = raw.get("verification_contract_sha256")
    if contract_hash is not None and not _is_sha256(contract_hash):
        raise ValueError(
            "Runner ticket provenance verification_contract_sha256 must be null or SHA-256"
        )
    plan_path = raw.get("local_plan_path")
    plan_filename = raw.get("local_plan_filename")
    if not isinstance(plan_path, str) or not plan_path.strip():
        raise ValueError("Runner ticket provenance is missing local_plan_path")
    if not isinstance(plan_filename, str) or not plan_filename.strip():
        raise ValueError("Runner ticket provenance is missing local_plan_filename")
    if Path(plan_path).name != plan_filename:
        raise ValueError("Runner ticket provenance plan filename does not match its path")
    if owner_root is not None:
        plans_root = (owner_root.expanduser().resolve() / ".agents" / "plans").resolve()
        if not Path(plan_path).expanduser().resolve().is_relative_to(plans_root):
            raise ValueError("Runner ticket provenance plan path is outside owner plan root")

    if expected is not None:
        for field in (
            "fingerprint",
            "case_id",
            "plan_revision_id",
            "ticket_body_sha256",
            "local_plan_sha256",
            "verification_contract_sha256",
            "target_contract_sha256",
            "local_plan_filename",
        ):
            if raw.get(field) != expected.get(field):
                raise ValueError(
                    f"Runner ticket provenance is stale or cross-plan: {field} "
                    f"expected={expected.get(field)!r} observed={raw.get(field)!r}"
                )
    return dict(raw)


def _validate_verification_binding(
    raw: Any,
    *,
    ticket_provenance: dict[str, Any],
    require_test_eligible: bool,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("Runner ticket_ref.json is missing verification_binding schema 1")
    binding_hash = raw.get("binding_sha256")
    if not _is_sha256(binding_hash):
        raise ValueError("Runner verification binding hash is invalid")
    without_hash = {key: value for key, value in raw.items() if key != "binding_sha256"}
    if _sha256_json(without_hash) != binding_hash:
        raise ValueError("Runner verification binding self-hash mismatch")
    for field in (
        "fingerprint",
        "case_id",
        "plan_revision_id",
        "ticket_body_sha256",
        "local_plan_sha256",
    ):
        if raw.get(field) != ticket_provenance.get(field):
            raise ValueError(f"Runner verification binding ticket mismatch: {field}")
    if (
        raw.get("plan_verification_contract_sha256")
        != ticket_provenance.get("verification_contract_sha256")
    ):
        raise ValueError("Runner verification binding plan contract hash mismatch")
    if raw.get("plan_target_contract_sha256") != ticket_provenance.get(
        "target_contract_sha256"
    ):
        raise ValueError("Runner verification binding target contract hash mismatch")

    configured_raw = raw.get("configured_commands")
    plan_raw = raw.get("plan_commands")
    if not isinstance(configured_raw, list) or not isinstance(plan_raw, list):
        raise ValueError("Runner verification binding command lists are invalid")
    configured = [str(command).strip() for command in configured_raw]
    plan_commands = [str(command).strip() for command in plan_raw]
    if any(not command for command in configured + plan_commands):
        raise ValueError("Runner verification binding contains an empty command")
    if len(configured) != len(set(configured)) or len(plan_commands) != len(set(plan_commands)):
        raise ValueError("Runner verification binding contains duplicate commands")
    for command in configured:
        safety_errors = verification_command_safety_errors(command)
        if safety_errors:
            raise ValueError(
                f"Unsafe runner verification binding command {command!r}: "
                + "; ".join(safety_errors)
            )
    eligible = bool(
        ticket_provenance.get("verification_contract_sha256")
        and configured
        and configured == plan_commands
    )
    if raw.get("eligible_for_test_evidence") is not eligible:
        raise ValueError("Runner verification binding eligibility flag is inconsistent")
    if require_test_eligible and not eligible:
        raise ValueError(
            "Runner verification is not bound to an explicit stage-6 plan command contract"
        )
    return dict(raw)


def validate_runner_ticket_ref(
    *,
    run_dir: Path,
    fingerprint: str,
    case_id: str,
    plan_revision_id: str,
    owner_root: Path | None = None,
    expected_ticket_provenance: dict[str, Any] | None = None,
    require_test_eligible: bool = False,
) -> tuple[dict[str, Any], Path]:
    """Validate that an implementation run belongs to one selected plan."""

    resolved_run_dir = run_dir.expanduser().resolve()
    ticket_ref_path = resolved_run_dir / "ticket_ref.json"
    ticket_ref = _read_json_object(ticket_ref_path, label="Runner ticket_ref.json")
    schema_version = ticket_ref.get("schema_version")
    if schema_version not in {1, 2}:
        raise ValueError("Runner ticket_ref.json schema_version must be 1 or 2")
    if ticket_ref.get("fingerprint") != fingerprint:
        raise ValueError(
            "Implementation run fingerprint mismatch: "
            f"expected={fingerprint!r} observed={ticket_ref.get('fingerprint')!r}"
        )

    observed_case = ticket_ref.get("case_id")
    if case_id.startswith("legacy-case:"):
        if observed_case not in {None, "", case_id}:
            raise ValueError(
                "Implementation run case identity mismatch: "
                f"expected={case_id!r} observed={observed_case!r}"
            )
    elif observed_case != case_id:
        raise ValueError(
            "Implementation run case identity mismatch: "
            f"expected={case_id!r} observed={observed_case!r}"
        )

    observed_plan = ticket_ref.get("plan_revision_id")
    if plan_revision_id.startswith("legacy-plan:"):
        if observed_plan not in {None, "", plan_revision_id}:
            raise ValueError(
                "Implementation run plan identity mismatch: "
                f"expected={plan_revision_id!r} observed={observed_plan!r}"
            )
    elif observed_plan != plan_revision_id:
        raise ValueError(
            "Implementation run plan identity mismatch: "
            f"expected={plan_revision_id!r} observed={observed_plan!r}"
        )

    if owner_root is not None:
        owner_repo = ticket_ref.get("owner_repo")
        owner_repo_dict = owner_repo if isinstance(owner_repo, dict) else {}
        observed_root = owner_repo_dict.get("root")
        if not isinstance(observed_root, str) or not observed_root.strip():
            raise ValueError("Runner ticket_ref.json is missing owner_repo.root")
        if Path(observed_root).expanduser().resolve() != owner_root.expanduser().resolve():
            raise ValueError(
                "Implementation run owner root mismatch: "
                f"expected={owner_root.resolve()} observed={Path(observed_root).resolve()}"
            )

    if schema_version == 1:
        expected_requires_v2 = bool(
            isinstance(expected_ticket_provenance, dict)
            and (
                expected_ticket_provenance.get("legacy_identity") is False
                or expected_ticket_provenance.get("verification_contract") is not None
            )
        )
        if require_test_eligible or expected_requires_v2:
            raise ValueError(
                "Legacy runner ticket_ref lacks immutable plan and verification provenance"
            )
        return ticket_ref, ticket_ref_path

    ticket_provenance = _validate_ticket_provenance_payload(
        ticket_ref.get("ticket_provenance"),
        fingerprint=fingerprint,
        case_id=case_id,
        plan_revision_id=plan_revision_id,
        expected=expected_ticket_provenance,
        owner_root=owner_root,
    )
    _validate_verification_binding(
        ticket_ref.get("verification_binding"),
        ticket_provenance=ticket_provenance,
        require_test_eligible=require_test_eligible,
    )
    return ticket_ref, ticket_ref_path


def validate_bound_runner_verification(
    *,
    run_dir: Path,
    fingerprint: str,
    case_id: str,
    plan_revision_id: str,
    evidence_kind: str,
    expected_verification_sha256: str | None = None,
    owner_root: Path | None = None,
    trusted_runs_root: Path | None = None,
    expected_ticket_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate exact plan-bound test coverage and return a durable receipt."""

    normalized_kind = evidence_kind.strip().lower()
    if normalized_kind != _SUPPORTED_EVIDENCE_KIND:
        raise ValueError(
            "Only ticket-bound test evidence is supported; original scenario, live, "
            "and recurrence evidence require dedicated runner-role workflows"
        )
    if expected_ticket_provenance is None:
        raise ValueError("Expected selected-ticket provenance is required")

    resolved_run_dir = run_dir.expanduser().resolve()
    if trusted_runs_root is not None:
        resolved_runs_root = trusted_runs_root.expanduser().resolve()
        if not resolved_run_dir.is_relative_to(resolved_runs_root):
            raise ValueError(
                "Runner evidence run_dir is outside the configured runs root: "
                f"run_dir={resolved_run_dir} runs_root={resolved_runs_root}"
            )

    ticket_ref, ticket_ref_path = validate_runner_ticket_ref(
        run_dir=resolved_run_dir,
        fingerprint=fingerprint,
        case_id=case_id,
        plan_revision_id=plan_revision_id,
        owner_root=owner_root,
        expected_ticket_provenance=expected_ticket_provenance,
        require_test_eligible=True,
    )
    ticket_provenance = ticket_ref["ticket_provenance"]
    binding = ticket_ref["verification_binding"]

    verification_path = resolved_run_dir / "verification.json"
    verification = _read_json_object(
        verification_path,
        label="Runner verification.json",
    )
    observed_verification_sha256 = _sha256_file(verification_path)
    if expected_verification_sha256 is not None:
        if not _is_sha256(expected_verification_sha256):
            raise ValueError("runner_receipt.verification_sha256 must be a SHA-256 digest")
        if observed_verification_sha256.casefold() != expected_verification_sha256.casefold():
            raise ValueError("Runner verification artifact hash mismatch")

    if verification.get("schema_version") != 1:
        raise ValueError("Runner verification schema_version must be 1")
    if verification.get("passed") is not True:
        raise ValueError("Runner verification does not record passed=true")
    if str(verification.get("status") or "").strip().lower() != "passed":
        raise ValueError("Runner verification status must be passed")
    if str(verification.get("terminal_reason") or "").strip().lower() != "passed":
        raise ValueError("Runner verification terminal_reason must be passed")
    if verification.get("timed_out") is not False:
        raise ValueError("Runner verification must explicitly record timed_out=false")
    if verification.get("cancelled") is not False:
        raise ValueError("Runner verification must explicitly record cancelled=false")

    expected_commands = binding["configured_commands"]
    configured_raw = verification.get("commands_configured")
    if configured_raw is None and "commands_configured" not in verification:
        # Early schema-v1 runner receipts retained every executed command but
        # accidentally omitted the redundant configured-command projection.
        # The immutable ticket binding remains the authority; exact executed
        # coverage below must still match it in count, order, and text.
        configured = list(expected_commands)
    else:
        if not isinstance(configured_raw, list):
            raise ValueError("Runner verification configured commands must be a list")
        configured = normalize_verification_commands(configured_raw)
    if configured != expected_commands:
        raise ValueError(
            "Runner verification commands do not match the selected plan contract"
        )
    for command in configured:
        safety_errors = verification_command_safety_errors(command)
        if safety_errors:
            raise ValueError(
                f"Unsafe verification receipt command {command!r}: "
                + "; ".join(safety_errors)
            )

    commands_raw = verification.get("commands")
    commands = (
        [item for item in commands_raw if isinstance(item, dict)]
        if isinstance(commands_raw, list)
        else []
    )
    if len(commands) != len(configured):
        raise ValueError(
            "Runner verification executed-command coverage mismatch: "
            f"configured={len(configured)} executed={len(commands)}"
        )
    for index, (configured_command, command) in enumerate(
        zip(configured, commands, strict=True)
    ):
        if command.get("command") != configured_command:
            raise ValueError(
                "Runner verification command mismatch: "
                f"index={index} configured={configured_command!r} "
                f"executed={command.get('command')!r}"
            )
        exit_code = command.get("exit_code")
        if isinstance(exit_code, bool) or exit_code != 0:
            raise ValueError(f"Runner verification command did not pass: index={index}")
        if command.get("timed_out") is not False:
            raise ValueError(
                f"Runner verification command timed out or is unreceipted: index={index}"
            )
        if command.get("cancelled") is not False:
            raise ValueError(
                f"Runner verification command was cancelled or is unreceipted: index={index}"
            )
        if command.get("dispatch_blocked") is True or command.get("rejected_sentinel") is True:
            raise ValueError(f"Runner verification command dispatch was rejected: index={index}")

    return {
        "receipt_schema_version": 2,
        "producer": "usertest_implement",
        "verification_producer": "runner_core",
        "evidence_kind": _SUPPORTED_EVIDENCE_KIND,
        "run_dir": str(resolved_run_dir),
        "verification_path": str(verification_path),
        "verification_sha256": observed_verification_sha256,
        "ticket_ref_path": str(ticket_ref_path),
        "ticket_ref_sha256": _sha256_file(ticket_ref_path),
        "fingerprint": fingerprint,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "ticket_body_sha256": ticket_provenance["ticket_body_sha256"],
        "local_plan_sha256": ticket_provenance["local_plan_sha256"],
        "local_plan_filename": ticket_provenance["local_plan_filename"],
        "verification_contract_sha256": ticket_provenance[
            "verification_contract_sha256"
        ],
        "target_contract_sha256": ticket_provenance.get("target_contract_sha256"),
        "verification_binding_sha256": binding["binding_sha256"],
        "commands": configured,
    }


def validate_bound_outcome_role_receipt(
    *,
    role_artifact_path: Path,
    evidence_kind: str,
    case_id: str,
    plan_revision_id: str,
    merged_commit: str,
    expected_ticket_provenance: dict[str, Any],
    trusted_runs_root: Path,
    expected_role_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Re-open and verify a dedicated runner-owned post-merge evidence role."""

    role = evidence_kind.strip().lower()
    if role not in {"original_scenario", "live", "mitigation_effect", "recurrence"}:
        raise ValueError(f"Unsupported outcome evidence role: {role}")
    resolved_path = role_artifact_path.expanduser().resolve()
    resolved_runs_root = trusted_runs_root.expanduser().resolve()
    if not resolved_path.is_relative_to(resolved_runs_root):
        raise ValueError("Outcome role artifact is outside the configured runs root")
    if not resolved_path.is_file():
        raise ValueError(f"Outcome role artifact does not exist: {resolved_path}")
    observed_sha256 = _sha256_file(resolved_path)
    if expected_role_artifact_sha256 is not None:
        if not _is_sha256(expected_role_artifact_sha256):
            raise ValueError("Outcome role artifact hash must be SHA-256")
        if observed_sha256.casefold() != expected_role_artifact_sha256.casefold():
            raise ValueError("Outcome role artifact hash mismatch")
    artifact = _read_json_object(resolved_path, label="Outcome role artifact")
    contract = expected_ticket_provenance.get("verification_contract")
    if not isinstance(contract, dict) or contract.get("schema_version") != 2:
        raise ValueError("Selected plan has no outcome-role verification contract")
    roles = contract.get("outcome_roles")
    role_contract = roles.get(role) if isinstance(roles, dict) else None
    if not isinstance(role_contract, dict):
        raise ValueError(f"Selected plan does not define outcome role: {role}")
    target_contract_hash = _required_text(
        expected_ticket_provenance.get("target_contract_sha256"),
        label="Selected plan target contract hash",
    )
    verification_contract_hash = _required_text(
        expected_ticket_provenance.get("verification_contract_sha256"),
        label="Selected plan verification contract hash",
    )
    verified_implementation_head = _required_text(
        expected_ticket_provenance.get("verified_implementation_head"),
        label="Verified implementation head",
    )
    normalized = validate_outcome_evidence_role_artifact(
        artifact,
        role=role,
        case_id=case_id,
        plan_revision_id=plan_revision_id,
        merged_commit=merged_commit,
        verification_contract_sha256=verification_contract_hash,
        target_contract_sha256=target_contract_hash,
        verified_implementation_head=verified_implementation_head,
        role_contract=role_contract,
    )
    return {
        "receipt_schema_version": 3,
        "producer": "usertest_implement",
        "verification_producer": "runner_core",
        "evidence_kind": role,
        "role_artifact_path": str(resolved_path),
        "role_artifact_sha256": observed_sha256,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "merged_commit": str(normalized["merged_commit"]),
        "fingerprint": _required_text(
            expected_ticket_provenance.get("fingerprint"),
            label="Ticket fingerprint",
        ),
        "ticket_body_sha256": _required_text(
            expected_ticket_provenance.get("ticket_body_sha256"),
            label="Ticket body hash",
        ),
        "local_plan_sha256": _required_text(
            expected_ticket_provenance.get("local_plan_sha256"),
            label="Local plan hash",
        ),
        "local_plan_filename": _required_text(
            expected_ticket_provenance.get("local_plan_filename"),
            label="Local plan filename",
        ),
        "verification_contract_sha256": verification_contract_hash,
        "target_contract_sha256": target_contract_hash,
        "verified_implementation_head": verified_implementation_head,
        "role_contract_sha256": str(normalized["role_contract_sha256"]),
        "outcome_oracle_id": normalized.get("outcome_oracle_id"),
        "proof_scope": normalized.get("proof_scope"),
    }


__all__ = [
    "build_verification_binding",
    "expected_ticket_identity",
    "validate_bound_runner_verification",
    "validate_bound_outcome_role_receipt",
    "validate_runner_ticket_ref",
]
