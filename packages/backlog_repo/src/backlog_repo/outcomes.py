from __future__ import annotations

import json
import re
from typing import Any

OUTCOME_STATES: frozenset[str] = frozenset(
    {
        "planned",
        "implemented",
        "tests_verified",
        "original_scenario_verified",
        "live_verified",
        "resolved",
        "mitigated",
        "duplicate",
        "superseded",
        "unverified",
        "integrity_unknown",
    }
)
OUTCOME_SCOPES: frozenset[str] = frozenset({"case", "plan_copy"})

_IMPLEMENTED_STATES = frozenset(
    {
        "implemented",
        "tests_verified",
        "original_scenario_verified",
        "live_verified",
        "resolved",
        "mitigated",
    }
)
_TEST_VERIFIED_STATES = frozenset(
    {"tests_verified", "original_scenario_verified", "live_verified", "resolved", "mitigated"}
)
_ORIGINAL_SCENARIO_STATES = frozenset(
    {"original_scenario_verified", "live_verified", "resolved", "mitigated"}
)
_OUTCOME_BLOCK_RE = re.compile(
    r"\n?<!-- backlog-outcome:start -->.*?<!-- backlog-outcome:end -->\n?",
    flags=re.DOTALL,
)
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"implemented", "tests_verified", "mitigated", "unverified"}),
    "implemented": frozenset({"tests_verified", "mitigated", "unverified"}),
    "tests_verified": frozenset(
        {"original_scenario_verified", "live_verified", "resolved", "mitigated", "unverified"}
    ),
    "original_scenario_verified": frozenset(
        {"live_verified", "resolved", "mitigated", "unverified"}
    ),
    "live_verified": frozenset({"resolved", "mitigated", "unverified"}),
    "mitigated": frozenset(
        {"tests_verified", "original_scenario_verified", "live_verified", "resolved", "unverified"}
    ),
    "unverified": frozenset(
        {
            "implemented",
            "tests_verified",
            "original_scenario_verified",
            "live_verified",
            "mitigated",
        }
    ),
    "integrity_unknown": frozenset({"unverified", "duplicate", "superseded"}),
    "resolved": frozenset(),
    "duplicate": frozenset(),
    "superseded": frozenset(),
}

_TRANSITION_IDENTITY_FIELDS = frozenset(
    {
        "schema_version",
        "case_id",
        "plan_revision_id",
        "outcome_scope",
        "requires_live_verification",
        "ticket_provenance",
    }
)
_TRANSITION_CONTROLLED_FIELDS = frozenset(
    {
        *_TRANSITION_IDENTITY_FIELDS,
        "state",
        "recorded_at",
        "history",
    }
)
_WRITE_ONCE_PROVENANCE_FIELDS = frozenset(
    {
        "target_branch",
        "merged_commit",
        "pr_url",
    }
)
_STATES_AT_OR_BEYOND_TESTS_VERIFIED = frozenset(
    {
        "tests_verified",
        "original_scenario_verified",
        "live_verified",
        "resolved",
        "mitigated",
    }
)


def _required_string(record: dict[str, Any], field: str) -> str:
    """Return a required non-empty string from an outcome record."""

    raw = record.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"outcome_record_missing_required_string: {field}")
    return raw.strip()


def _string_list(record: dict[str, Any], field: str) -> list[str]:
    """Validate and normalize a list of non-empty strings."""

    raw = record.get(field)
    if not isinstance(raw, list):
        raise ValueError(f"outcome_record_invalid_string_list: {field}")
    out: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"outcome_record_invalid_string_list_item: {field}[{index}]")
        out.append(item.strip())
    return out


def _runner_receipt(
    item: dict[str, Any],
    *,
    field: str,
    index: int,
    evidence_kind: str,
    case_id: str,
    plan_revision_id: str,
) -> dict[str, Any] | None:
    raw = item.get("runner_receipt")
    result = str(item.get("result") or "").strip().lower()
    if raw is None and result != "passed":
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"outcome_record_runner_receipt_required: {field}[{index}]")
    expected_schema = 2 if evidence_kind == "test" else 3
    if (
        raw.get("receipt_schema_version") != expected_schema
        or raw.get("producer") != "usertest_implement"
        or raw.get("verification_producer") != "runner_core"
    ):
        raise ValueError(f"outcome_record_runner_receipt_schema_invalid: {field}[{index}]")
    if raw.get("evidence_kind") != evidence_kind:
        raise ValueError(f"outcome_record_runner_receipt_kind_mismatch: {field}[{index}]")
    if raw.get("case_id") != case_id or raw.get("plan_revision_id") != plan_revision_id:
        raise ValueError(f"outcome_record_runner_receipt_identity_mismatch: {field}[{index}]")
    fingerprint = raw.get("fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{16}", fingerprint) is None:
        raise ValueError(f"outcome_record_runner_receipt_fingerprint_invalid: {field}[{index}]")
    path_fields = (
        ("run_dir", "verification_path", "ticket_ref_path")
        if evidence_kind == "test"
        else ("role_artifact_path",)
    )
    for receipt_field in path_fields:
        value = raw.get(receipt_field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"outcome_record_runner_receipt_field_invalid: {field}[{index}].{receipt_field}"
            )
    hash_fields = [
        "ticket_body_sha256",
        "local_plan_sha256",
        "verification_contract_sha256",
    ]
    if evidence_kind == "test":
        if raw.get("target_contract_sha256") is not None:
            hash_fields.append("target_contract_sha256")
        hash_fields.extend(
            ["verification_sha256", "ticket_ref_sha256", "verification_binding_sha256"]
        )
    else:
        hash_fields.append("target_contract_sha256")
        hash_fields.extend(["role_artifact_sha256", "role_contract_sha256"])
    for receipt_field in hash_fields:
        value = raw.get(receipt_field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value.strip()) is None:
            raise ValueError(
                f"outcome_record_runner_receipt_field_invalid: {field}[{index}].{receipt_field}"
            )
    if evidence_kind == "test":
        commands = raw.get("commands")
        if not isinstance(commands, list) or not commands or any(
            not isinstance(command, str) or not command.strip() for command in commands
        ):
            raise ValueError(f"outcome_record_runner_receipt_commands_invalid: {field}[{index}]")
        if len(commands) != len(set(commands)):
            raise ValueError(
                f"outcome_record_runner_receipt_commands_duplicate: {field}[{index}]"
            )
    else:
        merged_commit = raw.get("merged_commit")
        if not isinstance(merged_commit, str) or not merged_commit.strip():
            raise ValueError(
                f"outcome_record_runner_receipt_field_invalid: {field}[{index}].merged_commit"
            )
        for receipt_field in (
            "verified_implementation_head",
        ):
            value = raw.get(receipt_field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "outcome_record_runner_receipt_field_invalid: "
                    f"{field}[{index}].{receipt_field}"
                )
        oracle_id = raw.get("outcome_oracle_id")
        proof_scope = raw.get("proof_scope")
        if oracle_id is not None or proof_scope is not None:
            if (
                not isinstance(oracle_id, str)
                or not oracle_id.startswith("outcome_oracle:")
                or proof_scope not in {"behavioral", "configuration_state"}
                or item.get("outcome_oracle_id") != oracle_id
                or item.get("proof_scope") != proof_scope
            ):
                raise ValueError(
                    f"outcome_record_runner_receipt_oracle_invalid: {field}[{index}]"
                )
    plan_filename = raw.get("local_plan_filename")
    if not isinstance(plan_filename, str) or not plan_filename.strip():
        raise ValueError(
            f"outcome_record_runner_receipt_field_invalid: {field}[{index}].local_plan_filename"
        )
    return dict(raw)


def _evidence_list(
    record: dict[str, Any],
    field: str,
    *,
    required: bool,
    evidence_kind: str,
    case_id: str,
    plan_revision_id: str,
) -> list[dict[str, Any]]:
    """Validate structured evidence entries without inventing missing evidence."""

    raw = record.get(field)
    if not isinstance(raw, list):
        raise ValueError(f"outcome_record_invalid_evidence_list: {field}")
    if required and not raw:
        raise ValueError(f"outcome_record_evidence_required: {field}")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"outcome_record_invalid_evidence_item: {field}[{index}]")
        kind = item.get("kind")
        reference = item.get("reference")
        result = item.get("result")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(f"outcome_record_evidence_missing_kind: {field}[{index}]")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f"outcome_record_evidence_missing_reference: {field}[{index}]")
        if not isinstance(result, str) or not result.strip():
            raise ValueError(f"outcome_record_evidence_missing_result: {field}[{index}]")
        normalized = {
            **item,
            "kind": kind.strip(),
            "reference": reference.strip(),
            "result": result.strip(),
        }
        receipt = _runner_receipt(
            normalized,
            field=field,
            index=index,
            evidence_kind=evidence_kind,
            case_id=case_id,
            plan_revision_id=plan_revision_id,
        )
        if receipt is not None:
            normalized["runner_receipt"] = receipt
        out.append(normalized)
    return out


def _ticket_provenance(
    raw: Any,
    *,
    case_id: str,
    plan_revision_id: str,
) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("outcome_record_ticket_provenance_schema_invalid")
    if raw.get("case_id") != case_id or raw.get("plan_revision_id") != plan_revision_id:
        raise ValueError("outcome_record_ticket_provenance_identity_mismatch")
    fingerprint = raw.get("fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{16}", fingerprint) is None:
        raise ValueError("outcome_record_ticket_provenance_fingerprint_invalid")
    for field in (
        "ticket_body_sha256",
        "local_plan_sha256",
        "verification_contract_sha256",
        "target_contract_sha256",
    ):
        value = raw.get(field)
        if field in {"verification_contract_sha256", "target_contract_sha256"} and value is None:
            continue
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
            raise ValueError(f"outcome_record_ticket_provenance_field_invalid: {field}")
    filename = raw.get("local_plan_filename")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("outcome_record_ticket_provenance_field_invalid: local_plan_filename")
    if raw.get("target_contract_sha256") is not None:
        for field in ("verified_implementation_head",):
            value = raw.get(field)
            if not isinstance(value, str) or re.fullmatch(
                r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value
            ) is None:
                raise ValueError(f"outcome_record_ticket_provenance_field_invalid: {field}")
    return dict(raw)


def validate_outcome_record(record: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the durable outcome contract.

    Outcome state is deliberately separate from a plan's queue folder. A merged
    implementation can be ``tests_verified`` without claiming the original or
    live failure is resolved. Runtime failures may only be ``resolved`` when the
    record includes both original-scenario evidence and live evidence.
    """

    if not isinstance(record, dict):
        raise ValueError("outcome_record_must_be_object")
    if record.get("schema_version") != 1:
        raise ValueError("outcome_record_schema_version_must_be_1")

    case_id = _required_string(record, "case_id")
    plan_revision_id = _required_string(record, "plan_revision_id")
    ticket_provenance = _ticket_provenance(
        record.get("ticket_provenance"),
        case_id=case_id,
        plan_revision_id=plan_revision_id,
    )
    state = _required_string(record, "state").lower()
    if state not in OUTCOME_STATES:
        raise ValueError(f"outcome_record_invalid_state: {state}")
    recorded_at = _required_string(record, "recorded_at")
    outcome_scope_raw = record.get("outcome_scope", "case")
    if not isinstance(outcome_scope_raw, str):
        raise ValueError("outcome_record_scope_must_be_string")
    outcome_scope = outcome_scope_raw.strip().lower()
    if outcome_scope not in OUTCOME_SCOPES:
        raise ValueError(f"outcome_record_invalid_scope: {outcome_scope}")
    if outcome_scope == "plan_copy" and state not in {
        "duplicate",
        "superseded",
        "integrity_unknown",
    }:
        raise ValueError(
            "outcome_record_plan_copy_scope_requires_relationship_or_integrity_state"
        )

    requires_live = record.get("requires_live_verification")
    if not isinstance(requires_live, bool):
        raise ValueError("outcome_record_requires_live_verification_must_be_bool")

    remaining_risks = _string_list(record, "remaining_risks")
    test_evidence = _evidence_list(
        record,
        "test_evidence",
        required=state in _TEST_VERIFIED_STATES,
        evidence_kind="test",
        case_id=case_id,
        plan_revision_id=plan_revision_id,
    )
    original_evidence = _evidence_list(
        record,
        "original_scenario_evidence",
        required=state in _ORIGINAL_SCENARIO_STATES,
        evidence_kind="original_scenario",
        case_id=case_id,
        plan_revision_id=plan_revision_id,
    )
    live_evidence = _evidence_list(
        record,
        "live_evidence",
        required=state == "live_verified" or (state == "resolved" and requires_live),
        evidence_kind="live",
        case_id=case_id,
        plan_revision_id=plan_revision_id,
    )
    mitigation_evidence = _evidence_list(
        {
            **record,
            "mitigation_evidence": record.get("mitigation_evidence", []),
        },
        "mitigation_evidence",
        required=state == "mitigated",
        evidence_kind="mitigation_effect",
        case_id=case_id,
        plan_revision_id=plan_revision_id,
    )
    for evidence_field, evidence_items in (
        ("test_evidence", test_evidence),
        ("original_scenario_evidence", original_evidence),
        ("live_evidence", live_evidence),
        ("mitigation_evidence", mitigation_evidence),
    ):
        for index, item in enumerate(evidence_items):
            receipt = item.get("runner_receipt")
            if not isinstance(receipt, dict) or ticket_provenance is None:
                continue
            for provenance_field in (
                "fingerprint",
                "ticket_body_sha256",
                "local_plan_sha256",
                "local_plan_filename",
                "verification_contract_sha256",
                "target_contract_sha256",
            ):
                if receipt.get(provenance_field) != ticket_provenance.get(provenance_field):
                    raise ValueError(
                        "outcome_record_runner_receipt_ticket_provenance_mismatch: "
                        f"{evidence_field}[{index}].{provenance_field}"
                    )
            if (
                receipt.get("receipt_schema_version") == 3
                and receipt.get("verified_implementation_head")
                != ticket_provenance.get("verified_implementation_head")
            ):
                raise ValueError(
                    "outcome_record_runner_receipt_ticket_provenance_mismatch: "
                    f"{evidence_field}[{index}].verified_implementation_head"
                )
    if state in _TEST_VERIFIED_STATES and not any(
        item.get("result", "").strip().lower() == "passed" for item in test_evidence
    ):
        raise ValueError("outcome_record_verified_state_requires_passing_test_evidence")
    if state in _ORIGINAL_SCENARIO_STATES and not any(
        item.get("result", "").strip().lower() == "passed" for item in original_evidence
    ):
        raise ValueError("outcome_record_original_scenario_state_requires_passing_evidence")
    if state == "live_verified" and not any(
        item.get("result", "").strip().lower() == "passed" for item in live_evidence
    ):
        raise ValueError("outcome_record_live_verified_requires_passing_evidence")
    if state == "mitigated" and not any(
        item.get("result", "").strip().lower() == "passed"
        for item in mitigation_evidence
    ):
        raise ValueError("outcome_record_mitigated_requires_passing_effect_evidence")

    target_branch = record.get("target_branch")
    merged_commit = record.get("merged_commit")
    if state in _IMPLEMENTED_STATES:
        if not isinstance(target_branch, str) or not target_branch.strip():
            raise ValueError("outcome_record_target_branch_required_after_implementation")
        if not isinstance(merged_commit, str) or not merged_commit.strip():
            raise ValueError("outcome_record_merged_commit_required_after_implementation")

    recurrence_check = record.get("recurrence_check")
    if not isinstance(recurrence_check, dict):
        raise ValueError("outcome_record_recurrence_check_must_be_object")
    recurrence_status = recurrence_check.get("status")
    if not isinstance(recurrence_status, str) or not recurrence_status.strip():
        raise ValueError("outcome_record_recurrence_check_status_required")
    recurrence_evidence = _evidence_list(
        {"evidence": recurrence_check.get("evidence", [])},
        "evidence",
        required=False,
        evidence_kind="recurrence",
        case_id=case_id,
        plan_revision_id=plan_revision_id,
    )

    if state in {"duplicate", "superseded"}:
        related_case = record.get("related_case_id")
        related_plan = record.get("related_plan_revision_id")
        if not (
            isinstance(related_case, str)
            and related_case.strip()
            or isinstance(related_plan, str)
            and related_plan.strip()
        ):
            raise ValueError(f"outcome_record_{state}_requires_related_identity")

    if state == "resolved":
        recurrence_status_normalized = recurrence_status.strip().lower()
        if recurrence_status_normalized not in {
            "completed",
            "no_recurrence",
            "not_observed",
        }:
            raise ValueError(
                "outcome_record_resolved_requires_recorded_recurrence_disposition"
            )
        recurrence_result = recurrence_check.get("result")
        if recurrence_status_normalized == "not_observed":
            if (
                not isinstance(recurrence_result, str)
                or recurrence_result.strip().lower() != "no_new_source_window"
                or recurrence_evidence
            ):
                raise ValueError(
                    "outcome_record_unobserved_recurrence_contract_invalid"
                )
            normalized_risks = " ".join(remaining_risks).casefold()
            if (
                "recurrence" not in normalized_risks
                or "not" not in normalized_risks
                or "observed" not in normalized_risks
            ):
                raise ValueError(
                    "outcome_record_unobserved_recurrence_risk_required"
                )
        else:
            if (
                not isinstance(recurrence_result, str)
                or recurrence_result.strip().lower() != "passed"
            ):
                raise ValueError("outcome_record_resolved_requires_passing_recurrence_result")
            if not any(
                item.get("result", "").strip().lower() == "passed"
                for item in recurrence_evidence
            ):
                raise ValueError("outcome_record_resolved_requires_passing_recurrence_evidence")
        if requires_live and not any(
            item.get("result", "").strip().lower() == "passed" for item in live_evidence
        ):
            raise ValueError("outcome_record_resolved_runtime_case_requires_passing_live_evidence")

    return {
        **record,
        "schema_version": 1,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "ticket_provenance": ticket_provenance,
        "state": state,
        "outcome_scope": outcome_scope,
        "recorded_at": recorded_at,
        "requires_live_verification": requires_live,
        "remaining_risks": remaining_risks,
        "test_evidence": test_evidence,
        "original_scenario_evidence": original_evidence,
        "live_evidence": live_evidence,
        "mitigation_evidence": mitigation_evidence,
        "recurrence_check": {
            **recurrence_check,
            "status": recurrence_status.strip().lower(),
            "evidence": recurrence_evidence,
        },
    }


def outcome_suppresses_new_case_discovery(record: dict[str, Any]) -> bool:
    """Return whether a validated outcome closes the case for discovery.

    ``implemented`` and verification-only states intentionally do not suppress
    recurrence. Only a resolved case or an explicitly linked duplicate or
    superseded case is closed for new-case mining.
    """

    normalized = validate_outcome_record(record)
    return normalized["outcome_scope"] == "case" and normalized["state"] in {
        "resolved",
        "duplicate",
        "superseded",
    }


def transition_outcome_record(
    current: dict[str, Any],
    *,
    state: str,
    recorded_at: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Advance an outcome through an explicit evidence-backed transition.

    Terminal resolution and relationship outcomes are immutable. Callers must
    provide all evidence required by the target state in ``updates``; validation
    never manufactures proof or carries a failed check as a pass.
    """

    previous = validate_outcome_record(current)
    target = state.strip().lower()
    allowed = _ALLOWED_TRANSITIONS[previous["state"]]
    if target not in allowed:
        raise ValueError(
            "outcome_transition_not_allowed: "
            f"{previous['state']} -> {target}; allowed={sorted(allowed)!r}"
        )
    if not isinstance(updates, dict):
        raise ValueError("outcome_transition_updates_must_be_object")
    for field in sorted(_TRANSITION_CONTROLLED_FIELDS):
        if field not in updates:
            continue
        if field in _TRANSITION_IDENTITY_FIELDS and updates[field] == previous.get(field):
            continue
        raise ValueError(f"outcome_transition_immutable_field: {field}")
    for field in sorted(_WRITE_ONCE_PROVENANCE_FIELDS):
        if field not in updates:
            continue
        previous_value = previous.get(field)
        proposed_value = updates[field]
        if previous_value is not None and previous_value != proposed_value:
            raise ValueError(f"outcome_transition_immutable_provenance: {field}")
    history_raw = previous.get("history")
    history = (
        [item for item in history_raw if isinstance(item, dict)]
        if isinstance(history_raw, list)
        else []
    )
    history.append(
        {
            "state": previous["state"],
            "recorded_at": previous["recorded_at"],
            "merged_commit": previous.get("merged_commit"),
        }
    )
    candidate = {
        **previous,
        **updates,
        "state": target,
        "recorded_at": recorded_at,
        "history": history,
    }
    return validate_outcome_record(candidate)


def reconcile_outcome_records(
    current: dict[str, Any],
    proposed: dict[str, Any],
) -> dict[str, Any]:
    """Return a monotonic evidence-backed reconciliation of two outcome records.

    Persistence layers use this helper when a retry proposes a freshly-created
    ``tests_verified`` record for a case that has already advanced. The advanced
    record wins, but identity, live-verification classification, and merge
    provenance must still match. Other state changes must be explicit legal
    transitions and receive transition history.
    """

    previous = validate_outcome_record(current)
    candidate = validate_outcome_record(proposed)

    for field in sorted(_TRANSITION_IDENTITY_FIELDS - {"schema_version"}):
        if previous.get(field) != candidate.get(field):
            raise ValueError(f"outcome_reconcile_identity_mismatch: {field}")
    for field in sorted(_WRITE_ONCE_PROVENANCE_FIELDS):
        previous_value = previous.get(field)
        candidate_value = candidate.get(field)
        if previous_value is not None and candidate_value != previous_value:
            raise ValueError(f"outcome_reconcile_provenance_mismatch: {field}")

    if previous == candidate or previous["state"] == candidate["state"]:
        return previous

    if (
        candidate["state"] == "tests_verified"
        and previous["state"] in _STATES_AT_OR_BEYOND_TESTS_VERIFIED
    ):
        return previous

    updates = {
        key: value
        for key, value in candidate.items()
        if key not in _TRANSITION_CONTROLLED_FIELDS
    }
    return transition_outcome_record(
        previous,
        state=candidate["state"],
        recorded_at=candidate["recorded_at"],
        updates=updates,
    )


def render_outcome_markdown(record: dict[str, Any]) -> str:
    """Render a validated outcome as a replaceable machine-readable Markdown block."""

    normalized = validate_outcome_record(record)
    payload = json.dumps(normalized, indent=2, ensure_ascii=False, sort_keys=True)
    return (
        "<!-- backlog-outcome:start -->\n"
        "## Outcome record\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n"
        "<!-- backlog-outcome:end -->"
    )


def upsert_outcome_markdown(markdown: str, record: dict[str, Any]) -> str:
    """Insert or replace the outcome block in ticket Markdown."""

    block = render_outcome_markdown(record)
    without_existing = _OUTCOME_BLOCK_RE.sub("\n", markdown).rstrip()
    return f"{without_existing}\n\n{block}\n"


def extract_outcome_markdown(markdown: str) -> dict[str, Any] | None:
    """Extract and validate an embedded outcome record, if present."""

    match = _OUTCOME_BLOCK_RE.search(markdown)
    if match is None:
        return None
    block = match.group(0)
    payload_match = re.search(r"```json\s*(\{.*\})\s*```", block, flags=re.DOTALL)
    if payload_match is None:
        raise ValueError("outcome_markdown_missing_json_payload")
    parsed = json.loads(payload_match.group(1))
    if not isinstance(parsed, dict):
        raise ValueError("outcome_markdown_payload_must_be_object")
    return validate_outcome_record(parsed)
