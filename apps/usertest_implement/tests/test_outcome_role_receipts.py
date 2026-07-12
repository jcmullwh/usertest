from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from backlog_repo.ticket_provenance import verification_contract_payload
from runner_core import run_outcome_evidence_role

from usertest_implement.outcome_evidence import validate_bound_outcome_role_receipt


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _roles() -> dict[str, object]:
    return {
        "original_scenario": {
            "description": "Replay the research-established original scenario.",
            "research_experiment_id": "experiment:original",
            "commands": ["python probe.py"],
            "predicates": [
                {"type": "command_exit_code", "command_index": 0, "equals": 0}
            ],
        },
        "live": None,
        "mitigation_effect": None,
        "recurrence": {
            "description": "Inspect fresh same-class recurrence evidence.",
            "commands": ["python recurrence_probe.py"],
            "command_bindings": [
                {
                    "command_index": 0,
                    "research_experiment_id": "experiment:recurrence",
                }
            ],
            "predicates": [
                {"type": "command_exit_code", "command_index": 0, "equals": 0}
            ],
        },
    }


def test_role_receipt_is_bound_to_case_plan_commit_and_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "tests@example.com")
    _git(workspace, "config", "user.name", "Tests")
    (workspace / "probe.py").write_text("print('original passed')\n", encoding="utf-8")
    (workspace / "recurrence_probe.py").write_text("print('none')\n", encoding="utf-8")
    _git(workspace, "add", "probe.py", "recurrence_probe.py")
    _git(workspace, "commit", "-m", "role probes")
    commit = _git(workspace, "rev-parse", "HEAD")
    verification_contract = verification_contract_payload(
        ["python -m pytest tests/test_feature.py -q"],
        outcome_roles=_roles(),
    )
    provenance = {
        "schema_version": 1,
        "fingerprint": "0123456789abcdef",
        "case_id": "case:one",
        "plan_revision_id": "plan:one:v1",
        "ticket_body_sha256": "a" * 64,
        "local_plan_sha256": "b" * 64,
        "local_plan_filename": "plan.md",
        "verification_contract": verification_contract,
        "verification_contract_sha256": verification_contract["contract_sha256"],
        "target_contract_sha256": "c" * 64,
        "verified_implementation_head": commit,
    }
    runs_root = tmp_path / "runs"
    artifact_path = runs_root / "original" / "outcome_role.json"
    original_contract = verification_contract["outcome_roles"]["original_scenario"]
    assert isinstance(original_contract, dict)
    run_outcome_evidence_role(
        workspace=workspace,
        output_path=artifact_path,
        role="original_scenario",
        role_contract=original_contract,
        case_id="case:one",
        plan_revision_id="plan:one:v1",
        merged_commit=commit,
        verification_contract_sha256=str(verification_contract["contract_sha256"]),
        target_contract_sha256="c" * 64,
        verified_implementation_head=commit,
    )

    receipt = validate_bound_outcome_role_receipt(
        role_artifact_path=artifact_path,
        evidence_kind="original_scenario",
        case_id="case:one",
        plan_revision_id="plan:one:v1",
        merged_commit=commit,
        expected_ticket_provenance=provenance,
        trusted_runs_root=runs_root,
    )

    assert receipt["receipt_schema_version"] == 3
    assert receipt["merged_commit"] == commit
    assert receipt["target_contract_sha256"] == "c" * 64
    assert receipt["role_contract_sha256"] == original_contract[
        "role_contract_sha256"
    ]

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["target_contract_sha256"] = "d" * 64
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        validate_bound_outcome_role_receipt(
            role_artifact_path=artifact_path,
            evidence_kind="original_scenario",
            case_id="case:one",
            plan_revision_id="plan:one:v1",
            merged_commit=commit,
            expected_ticket_provenance=provenance,
            trusted_runs_root=runs_root,
            expected_role_artifact_sha256=str(receipt["role_artifact_sha256"]),
        )
