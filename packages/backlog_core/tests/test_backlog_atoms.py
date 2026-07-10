from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from run_artifacts.history import iter_report_history

from backlog_core.backlog import (
    add_atom_links,
    build_backlog_document,
    build_merge_candidates,
    dedupe_tickets,
    extract_backlog_atoms,
    parse_ticket_list,
    render_backlog_markdown,
    write_backlog_atoms,
)
from backlog_core.case_lineage import eligible_problem_mining_atoms, normalize_atom_lineage


class _DeterministicEmbedder:
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            if ("readme" in lowered) or ("quickstart" in lowered):
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


def test_extract_backlog_atoms_quarantines_model_lineage_from_runner_research(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "target_ref": {"requested_mission_id": "backlog_repro_research"},
            "report": {
                "confusion_points": [
                    {
                        "summary": "Research found another symptom.",
                        "impact": "The original case needs more evidence.",
                    }
                ],
                "extensions": {
                    "backlog_lineage": {
                        "origin_stage": "observation",
                        "evidence_role": "observation",
                        "parent_case_id": "case:attacker-selected",
                        "disposition": "novel_case",
                        "novel_case_rationale": "Model prose is not a classification.",
                    }
                },
            },
        }
    ]

    extracted = extract_backlog_atoms(records, repo_root=tmp_path)["atoms"]
    atoms = normalize_atom_lineage(extracted, strict_new_output=True)

    assert atoms
    assert {atom["origin_stage"] for atom in atoms} == {"repro_research"}
    assert {atom["evidence_role"] for atom in atoms} == {"research"}
    assert {atom["parent_case_id"] for atom in atoms} == {None}
    assert {atom["disposition"] for atom in atoms} == {"unresolved"}
    assert all("novel_case_rationale" not in atom for atom in atoms)
    assert eligible_problem_mining_atoms(atoms) == []


def test_extract_backlog_atoms_uses_ticket_ref_as_implementation_parent(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "target_ref": {"mission_id": "implement_maintenance_backlog_ticket_v1"},
            "ticket_ref": {"case_id": "case:trusted", "fingerprint": "ticket:trusted"},
            "report": {
                "confusion_points": [{"summary": "Implementation exposed a follow-up."}],
                "extensions": {
                    "backlog_lineage": {
                        "parent_case_id": "case:attacker-selected",
                        "case_id": "case:attacker-selected",
                    }
                },
            },
        }
    ]

    atoms = extract_backlog_atoms(records, repo_root=tmp_path)["atoms"]

    assert atoms
    assert {atom["origin_stage"] for atom in atoms} == {"implementation"}
    assert {atom["evidence_role"] for atom in atoms} == {"implementation"}
    assert {atom["parent_case_id"] for atom in atoms} == {"case:trusted"}
    assert {atom["case_id"] for atom in atoms} == {"case:trusted"}
    assert {atom["disposition"] for atom in atoms} == {"supports_case"}


def test_extract_backlog_atoms_preserves_structured_fields(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text(
        "EPIPE writing to socket\n"
        + ("x" * 200 + "\n") * 6000
        + "TAIL ROOT CAUSE: provider closed the final stream\n",
        encoding="utf-8",
    )
    (run_dir / "agent_last_message.txt").write_text(
        "I could not find the entrypoint.\nTried several commands.\nNeed docs.\n",
        encoding="utf-8",
    )

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "timestamp_utc": "2026-01-01T00:00:00Z",
            "agent": "codex",
            "status": "ok",
            "report": {
                "confusion_points": [
                    {
                        "summary": "No quickstart in README.",
                        "impact": "User cannot complete first run.",
                        "evidence": [{"kind": "file", "value": "README.md"}],
                    }
                ],
                "suggested_changes": [
                    {
                        "change": "Add quickstart examples.",
                        "type": "docs",
                        "location": "README.md",
                        "priority": "p0",
                        "expected_impact": "Faster onboarding.",
                    }
                ],
                "confidence_signals": {
                    "missing": ["No test command documented."]
                },
            },
            "report_validation_errors": ["$: failed to parse JSON from agent output"],
            "error": {"type": "AgentExecFailed", "message": "command not found"},
            "terminal_artifact_reads": {
                "report.json": {
                    "path": "report.json",
                    "exists": True,
                    "decode_ok": True,
                    "parse_ok": False,
                    "error_phase": "parse",
                    "error_type": "JSONDecodeError",
                    "error_message": "Expecting value",
                    "error_line": 1,
                    "error_column": 1,
                }
            },
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    atoms = atoms_doc["atoms"]
    assert atoms

    atom_ids = [item["atom_id"] for item in atoms]
    assert len(set(atom_ids)) == len(atom_ids)

    confusion = next(item for item in atoms if item["source"] == "confusion_point")
    assert confusion["impact"] == "User cannot complete first run."
    assert confusion["evidence"][0]["value"] == "README.md"

    suggested = next(item for item in atoms if item["source"] == "suggested_change")
    assert suggested["location"] == "README.md"
    assert suggested["priority"] == "p0"
    assert suggested["severity_hint"] == "high"

    failure_atom = next(item for item in atoms if item["source"] == "run_failure_event")
    assert failure_atom["failure_kind"] == "error"
    assert failure_atom["report_validation_errors"] == [
        "$: failed to parse JSON from agent output"
    ]
    assert failure_atom["error"]["type"] == "AgentExecFailed"
    assert failure_atom["error"]["message"] == "command not found"
    assert failure_atom["terminal_artifact_reads"]["report.json"]["error_phase"] == "parse"
    stderr_atom = next(item for item in atoms if item["source"] == "agent_stderr_artifact")
    assert "EPIPE writing to socket" in stderr_atom["text"]
    assert "TAIL ROOT CAUSE: provider closed the final stream" in stderr_atom["text"]
    assert stderr_atom["atom_id"] in failure_atom["linked_atom_ids"]

    attachments = failure_atom["attachments"]
    stderr_attachment = next(item for item in attachments if item["path"] == "agent_stderr.txt")
    assert stderr_attachment["truncated"] is True
    assert "EPIPE writing to socket" in stderr_attachment["excerpt_head"]
    assert stderr_attachment["artifact_ref"]["path"] == "agent_stderr.txt"
    assert stderr_attachment["artifact_ref"]["sha256"]

    last_message_attachment = next(
        item for item in attachments if item["path"] == "agent_last_message.txt"
    )
    assert "Tried several commands." in last_message_attachment["excerpt_head"]
    assert "\nNeed docs." in last_message_attachment["excerpt_head"]

    capture_manifest = atoms_doc["capture_manifest"]
    run_manifest = capture_manifest["target_a/20260101T000000Z/codex/0"]
    assert any(
        item.get("path") == "agent_stderr.txt" and item.get("truncated") is True
        for item in run_manifest
    )
    assert any(item.get("path") == "agent_last_message.txt" for item in run_manifest)

    totals = atoms_doc["totals"]
    assert totals["source_counts"]["run_failure_event"] == 1
    assert totals["source_counts"]["agent_stderr_artifact"] == 1
    assert totals["source_counts"]["agent_last_message_artifact"] == 1


def test_extract_backlog_atoms_does_not_emit_missing_report_for_nonterminal_run(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "target_ref.json").write_text(
        json.dumps({"repo_input": "C:/repo/target_a"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "effective_run_spec.json").write_text("{}\n", encoding="utf-8")

    records = list(iter_report_history(runs_dir, target_slug="target_a", embed="none"))
    assert [record["status"] for record in records] == ["nonterminal"]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)

    assert atoms_doc["totals"]["source_counts"].get("run_failure_event", 0) == 0
    assert atoms_doc["capture_manifest"]["target_a/20260101T000000Z/codex/0"]


def test_extract_backlog_atoms_emits_missing_report_for_completed_run_without_report(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "target_ref.json").write_text(
        json.dumps({"repo_input": "C:/repo/target_a"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "effective_run_spec.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "run_meta.json").write_text(
        json.dumps({"run_finished_utc": "2026-01-01T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    records = list(iter_report_history(runs_dir, target_slug="target_a", embed="none"))
    assert [record["status"] for record in records] == ["missing_report"]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)

    failure = next(
        atom for atom in atoms_doc["atoms"] if atom.get("source") == "run_failure_event"
    )
    assert failure["failure_kind"] == "missing_report"
    assert failure["severity_hint"] == "high"


def test_extract_backlog_atoms_extracts_task_run_v1_report_blocks(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {
                "schema_version": 1,
                "kind": "task_run_v1",
                "status": "success",
                "goal": "Do the thing",
                "summary": "Done",
                "steps": [{"name": "step", "attempts": [{"action": "a"}], "outcome": "ok"}],
                "outputs": [],
                "issues": [
                    {
                        "severity": "error",
                        "title": "README quickstart missing",
                        "details": "No copy/paste install commands.",
                        "evidence": "README.md",
                        "suggested_fix": "Add a quickstart section to README.md.",
                    }
                ],
                "user_experience": {
                    "unclear_points": ["Not sure how to run tests."],
                },
                "next_actions": ["Run pytest -q apps/usertest/tests/test_smoke.py"],
            },
            "report_validation_errors": None,
            "error": None,
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    atoms = atoms_doc["atoms"]
    assert atoms

    assert any(
        atom.get("source") == "confusion_point"
        and atom.get("report_kind") == "task_run_v1"
        and atom.get("issue_severity") == "error"
        and atom.get("text") == "README quickstart missing"
        for atom in atoms
    )
    assert any(
        atom.get("source") == "suggested_change"
        and atom.get("report_kind") == "task_run_v1"
        and atom.get("issue_title") == "README quickstart missing"
        and "quickstart" in str(atom.get("text", "")).lower()
        for atom in atoms
    )
    assert any(
        atom.get("source") == "confidence_missing"
        and atom.get("report_kind") == "task_run_v1"
        and atom.get("report_ux_block") == "unclear_points"
        for atom in atoms
    )


def test_extract_backlog_atoms_retains_every_structured_issue_by_default(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    issue_titles = [f"Observed issue {index:02d}" for index in range(25)]
    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {
                "schema_version": 1,
                "kind": "task_run_v1",
                "status": "success",
                "issues": [
                    {
                        "severity": "warn",
                        "title": title,
                        "details": f"Evidence details for {title}",
                    }
                    for title in issue_titles
                ],
            },
        }
    ]

    atoms = extract_backlog_atoms(records, repo_root=tmp_path)["atoms"]
    extracted_titles = {
        str(atom.get("text"))
        for atom in atoms
        if atom.get("report_issue_block") == "issues"
        and atom.get("source") == "confusion_point"
    }

    assert extracted_titles == set(issue_titles)


def test_extract_backlog_atoms_preserves_failed_task_outcome_steps_and_verification(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    count = 25
    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {
                "schema_version": 1,
                "kind": "task_run_v1",
                "status": "failure",
                "goal": "Exercise the canonical workflow",
                "summary": "The workflow failed before producing output.",
                "steps": [
                    {
                        "name": f"step-{index}",
                        "attempts": [
                            {
                                "action": f"run-{index}",
                                "result": f"failed-{index}",
                                "evidence": f"exact-output-{index}",
                            }
                        ],
                        "outcome": f"outcome-{index}",
                    }
                    for index in range(count)
                ],
                "outputs": [],
                "verification": [
                    {
                        "check": f"oracle-{index}",
                        "result": f"not-satisfied-{index}",
                        "evidence": f"oracle-evidence-{index}",
                    }
                    for index in range(count)
                ],
                # Deliberately omit optional issues[]. The observed failure must not be
                # replaced by this proposal atom.
                "next_actions": ["Repair the execution path."],
            },
        }
    ]

    atoms = extract_backlog_atoms(records, repo_root=tmp_path)["atoms"]
    outcomes = [atom for atom in atoms if atom.get("source") == "report_outcome"]
    steps = [atom for atom in atoms if atom.get("source") == "task_step_observation"]
    attempts = [
        atom for atom in atoms if atom.get("source") == "task_attempt_observation"
    ]
    verification = [
        atom for atom in atoms if atom.get("source") == "verification_observation"
    ]
    proposals = [atom for atom in atoms if atom.get("source") == "suggested_change"]

    assert len(outcomes) == 1
    assert outcomes[0]["report_status"] == "failure"
    assert outcomes[0]["report_summary"] == "The workflow failed before producing output."
    assert outcomes[0]["evidence_class"] == "observed"
    assert len(steps) == count
    assert len(attempts) == count
    assert len(verification) == count
    assert attempts[-1]["task_attempt"] == {
        "action": "run-24",
        "result": "failed-24",
        "evidence": "exact-output-24",
    }
    assert verification[-1]["verification_check"]["evidence"] == "oracle-evidence-24"
    assert proposals[0]["evidence_class"] == "proposal"


def test_extract_backlog_atoms_preserves_every_boundary_risk_observation(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    count = 25
    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {
                "schema_version": 1,
                "kind": "boundary_v1",
                "status": "partial",
                "constraints": ["read only"],
                "observations": [
                    {
                        "category": "credentials",
                        "summary": f"Shareable artifact exposes secret {index}",
                        "where_found": f"artifact-{index}.txt",
                        "evidence": f"token-{index}",
                        "how_to_disable_or_avoid": f"disable-path-{index}",
                        "risk_level": "high" if index == count - 1 else "medium",
                    }
                    for index in range(count)
                ],
                # Deliberately omit optional risks[].
                "recommendations": ["Redact shareable artifacts."],
            },
        }
    ]

    atoms = extract_backlog_atoms(records, repo_root=tmp_path)["atoms"]
    observations = [
        atom for atom in atoms if atom.get("source") == "boundary_observation"
    ]

    assert len(observations) == count
    assert observations[-1]["severity_hint"] == "high"
    assert observations[-1]["boundary_observation"] == {
        "category": "credentials",
        "summary": "Shareable artifact exposes secret 24",
        "where_found": "artifact-24.txt",
        "evidence": "token-24",
        "how_to_disable_or_avoid": "disable-path-24",
        "risk_level": "high",
    }
    assert all(atom["evidence_class"] == "observed" for atom in observations)
    assert any(atom.get("source") == "report_outcome" for atom in atoms)


def test_extract_backlog_atoms_preserves_every_failed_batch_result(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    count = 25
    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {
                "schema_version": 1,
                "kind": "batch_v1",
                "status": "partial",
                "goal": "Process every input",
                "inputs": [f"input-{index}" for index in range(count)],
                "results": [
                    {
                        "input": f"input-{index}",
                        "status": "failure",
                        "outputs": [
                            {
                                "label": f"diagnostic-{index}",
                                "path": f"diagnostics/{index}.json",
                                "description": f"full diagnostic {index}",
                            }
                        ],
                        "notes": f"parser rejected valid input {index}",
                    }
                    for index in range(count)
                ],
                "summary": "Every input failed.",
                # Deliberately omit optional issues[].
                "next_actions": ["Repair the parser."],
            },
        }
    ]

    atoms = extract_backlog_atoms(records, repo_root=tmp_path)["atoms"]
    failures = [
        atom for atom in atoms if atom.get("source") == "batch_result_failure"
    ]

    assert len(failures) == count
    assert failures[-1]["batch_result"]["outputs"] == [
        {
            "label": "diagnostic-24",
            "path": "diagnostics/24.json",
            "description": "full diagnostic 24",
        }
    ]
    assert failures[-1]["batch_result"]["notes"] == "parser rejected valid input 24"
    assert all(atom["evidence_class"] == "observed" for atom in failures)
    assert any(atom.get("source") == "report_outcome" for atom in atoms)


def test_extract_backlog_atoms_extracts_boundary_v1_risks_and_recommendations(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {
                "schema_version": 1,
                "kind": "boundary_v1",
                "status": "success",
                "constraints": ["No network"],
                "observations": [],
                "risks": [
                    {
                        "severity": "warn",
                        "title": "Potentially unsafe default",
                        "details": "This might leak data.",
                        "evidence": "README.md",
                        "suggested_fix": "Document the safe default in README.md.",
                    }
                ],
                "recommendations": ["Add a safety note to README.md."],
            },
            "report_validation_errors": None,
            "error": None,
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    atoms = atoms_doc["atoms"]
    assert any(
        atom.get("source") == "confusion_point"
        and atom.get("report_kind") == "boundary_v1"
        and atom.get("report_issue_block") == "risks"
        and atom.get("text") == "Potentially unsafe default"
        for atom in atoms
    )
    assert any(
        atom.get("source") == "suggested_change"
        and atom.get("report_kind") == "boundary_v1"
        and atom.get("report_block") == "recommendations"
        for atom in atoms
    )


def test_add_atom_links_links_suggestions_to_evidence_by_path_anchor() -> None:
    atoms = [
        {
            "atom_id": "run:confusion_point:1",
            "run_rel": "target/20260101T000000Z/codex/0",
            "source": "confusion_point",
            "text": "README.md is missing a quickstart section.",
        },
        {
            "atom_id": "run:suggested_change:1",
            "run_rel": "target/20260101T000000Z/codex/0",
            "source": "suggested_change",
            "text": "Add a quickstart to README.md.",
        },
    ]

    linked = add_atom_links(atoms)
    suggested = next(item for item in linked if item["source"] == "suggested_change")
    assert "readme.md" in suggested.get("path_anchors", [])
    assert suggested.get("linked_atom_ids") == ["run:confusion_point:1"]


def test_extract_backlog_atoms_omits_missing_agent_artifact_attachments_on_failure(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "gemini" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/gemini/0",
            "agent": "gemini",
            "status": "error",
            "report": None,
            "report_validation_errors": None,
            "error": {
                "type": "AgentPreflightFailed",
                "message": "Mission requires edits, but policy has allow_edits=false.",
            },
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    failure_atom = next(
        item for item in atoms_doc["atoms"] if item["source"] == "run_failure_event"
    )
    assert failure_atom["attachments"] == []

    run_manifest = atoms_doc["capture_manifest"]["target_a/20260101T000000Z/gemini/0"]
    assert any(
        item.get("path") == "agent_stderr.txt" and item.get("exists") is False
        for item in run_manifest
    )
    assert any(
        item.get("path") == "agent_last_message.txt" and item.get("exists") is False
        for item in run_manifest
    )


def test_extract_backlog_atoms_handles_missing_files(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": None,
            "report_validation_errors": None,
            "error": None,
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    assert atoms_doc["totals"]["runs"] == 1
    assert atoms_doc["totals"]["atoms"] == 0
    assert atoms_doc["capture_manifest"]
    run_manifest = atoms_doc["capture_manifest"]["target_a/20260101T000000Z/codex/0"]
    assert any(
        item.get("path") == "agent_stderr.txt" and item.get("exists") is False
        for item in run_manifest
    )
    assert any(
        item.get("path") == "agent_last_message.txt" and item.get("exists") is False
        for item in run_manifest
    )

    out_path = tmp_path / "atoms.jsonl"
    write_backlog_atoms(atoms_doc, out_path)
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8") == ""


def test_extract_backlog_atoms_prefers_error_json_over_duplicate_validation_error(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "claude" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_dir / "agent_last_message.txt").write_text("", encoding="utf-8")

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/claude/0",
            "agent": "claude",
            "status": "report_validation_error",
            "report": None,
            "report_validation_errors": ["claude exited with code 1"],
            "error": {"type": "AgentExecFailed", "message": "claude exited with code 1"},
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    sources = [atom["source"] for atom in atoms_doc["atoms"]]
    assert "run_failure_event" in sources
    assert "error_json" not in sources
    assert "report_validation_error" not in sources


def test_extract_backlog_atoms_skips_empty_stderr_on_success(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_dir / "agent_last_message.txt").write_text("ok\n", encoding="utf-8")

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {},
            "report_validation_errors": None,
            "error": None,
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    sources = {item["source"] for item in atoms_doc["atoms"]}
    assert "agent_stderr_artifact" not in sources
    assert "agent_last_message_artifact" in sources


def test_extract_backlog_atoms_reclassifies_known_warning_only_stderr(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text(
        "\n".join(
            [
                "[codex_notice_summary] code=shell_snapshot_powershell_unsupported "
                "occurrences=4 classification=capability_notice",
                (
                    "hint=PowerShell shell snapshot unsupported; "
                    "continuing without shell snapshot metadata."
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "agent_last_message.txt").write_text("ok\n", encoding="utf-8")

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {},
            "report_validation_errors": None,
            "error": None,
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    sources = {item["source"] for item in atoms_doc["atoms"]}
    assert "agent_stderr_artifact" not in sources
    assert "capability_notice_artifact" in sources
    notice_atom = next(
        atom for atom in atoms_doc["atoms"] if atom.get("source") == "capability_notice_artifact"
    )
    assert notice_atom.get("severity_hint") == "low"
    assert "shell_snapshot_powershell_unsupported" in notice_atom.get("warning_codes", [])


def test_extract_backlog_atoms_emits_command_failure_atoms_from_metrics(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "timestamp_utc": "2026-01-01T00:00:00Z",
            "agent": "codex",
            "status": "ok",
            "report": {},
            "report_validation_errors": None,
            "error": None,
            "metrics": {
                "commands_executed": 3,
                "commands_failed": 3,
                "failed_commands": [
                    {
                        "command": "python -m pip install -e .",
                        "exit_code": 1,
                        "cwd": "C:/ws",
                        "output_excerpt": (
                            "ERROR: Could not find a version that satisfies the requirement ..."
                        ),
                        "output_excerpt_truncated": True,
                    },
                    {
                        "command": "rg -n \"WindowsApps\" README.md docs -S",
                        "exit_code": 1,
                    },
                    {
                        "command": "python -m pytest -q",
                        "exit_code": 2,
                        "output_excerpt": "ImportError: No module named foo",
                    },
                ],
                "failed_commands_truncated": True,
                "failed_commands_omitted_count": 3,
            },
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    failures = [atom for atom in atoms_doc["atoms"] if atom.get("source") == "command_failure"]
    assert len(failures) == 2
    assert all(
        not str(atom.get("command", "")).lstrip().lower().startswith("rg ") for atom in failures
    )

    first = failures[0]
    assert first.get("from_metrics") is True
    assert first.get("command") == "python -m pip install -e ."
    assert first.get("exit_code") == 1
    assert first.get("cwd") == "C:/ws"
    assert first.get("output_excerpt_truncated") is True

    trunc = next(
        atom
        for atom in atoms_doc["atoms"]
        if atom.get("source") == "command_failure_truncated"
    )
    assert trunc.get("omitted_count") == 3


def test_extract_backlog_atoms_retains_every_command_failure_by_default(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    commands = [f"python tool_{index:02d}.py" for index in range(25)]
    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {},
            "metrics": {
                "commands_executed": len(commands),
                "commands_failed": len(commands),
                "failed_commands": [
                    {"command": command, "exit_code": 1} for command in commands
                ],
            },
        }
    ]

    atoms = extract_backlog_atoms(records, repo_root=tmp_path)["atoms"]
    failures = [atom for atom in atoms if atom.get("source") == "command_failure"]

    assert [atom.get("command") for atom in failures] == commands
    assert not any(
        atom.get("source") == "command_failure_truncated" for atom in atoms
    )


def test_extract_backlog_atoms_reconciles_incomplete_metrics_with_events(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "run_command",
            "data": {
                "command": "python -m pip install -e .",
                "exit_code": 1,
                "cwd": "C:/ws",
                "output_excerpt": "ERROR: could not build wheels",
                "failure_artifacts": {
                    "stderr": "command_failures/cmd_01/stderr.txt",
                },
            },
        },
        {
            "type": "run_command",
            "data": {
                "command": "python -m pytest -q",
                "exit_code": 2,
                "output_excerpt": "ImportError: No module named foo",
                "failure_artifacts": {
                    "stderr": "command_failures/cmd_02/stderr.txt",
                },
            },
        },
        {
            "type": "run_command",
            "data": {
                "command": "python tools/scaffold/scaffold.py run test --all",
                "exit_code": 1,
                "output_excerpt": "FAILED tests/test_example.py::test_x",
                "failure_artifacts": {
                    "stderr": "command_failures/cmd_03/stderr.txt",
                },
            },
        },
        {
            "type": "run_command",
            "data": {
                "command": "python -m pip install -e .",
                "exit_code": 1,
                "cwd": "C:/ws",
                "output_excerpt": "ERROR: could not build wheels",
                "failure_artifacts": {
                    "stderr": "command_failures/cmd_01/stderr.txt",
                },
            },
        },
    ]
    (run_dir / "normalized_events.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "timestamp_utc": "2026-01-01T00:00:00Z",
            "agent": "codex",
            "status": "ok",
            "report": {},
            "report_validation_errors": None,
            "error": None,
            "metrics": {
                "commands_executed": 3,
                "commands_failed": 3,
                "failed_commands": [
                    {
                        "command": "python -m pip install -e .",
                        "exit_code": 1,
                        "cwd": "C:/ws",
                        "output_excerpt": "ERROR: could not build wheels",
                        "artifacts": {
                            "stderr": "command_failures/cmd_01/stderr.txt",
                        },
                    }
                ],
                "failed_commands_truncated": True,
                "failed_commands_omitted_count": 2,
            },
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    failures = [atom for atom in atoms_doc["atoms"] if atom.get("source") == "command_failure"]
    assert [atom.get("command") for atom in failures] == [
        "python -m pip install -e .",
        "python -m pytest -q",
        "python tools/scaffold/scaffold.py run test --all",
    ]
    assert len(failures) == 3
    assert failures[0].get("from_metrics") is True
    assert failures[0].get("from_events") is None
    assert failures[1].get("from_events") is True
    assert failures[2].get("from_events") is True
    assert [
        atom
        for atom in atoms_doc["atoms"]
        if atom.get("source") == "command_failure_truncated"
    ] == []


def test_extract_backlog_atoms_does_not_reconcile_complete_metrics_with_events(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "normalized_events.jsonl").write_text(
        json.dumps(
            {
                "type": "run_command",
                "data": {
                    "command": "python -m pip install -e .",
                    "exit_code": 1,
                },
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {
                "type": "run_command",
                "data": {
                    "command": "python -m pytest -q",
                    "exit_code": 2,
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "timestamp_utc": "2026-01-01T00:00:00Z",
            "agent": "codex",
            "status": "ok",
            "report": {},
            "report_validation_errors": None,
            "error": None,
            "metrics": {
                "commands_executed": 1,
                "commands_failed": 1,
                "failed_commands": [
                    {
                        "command": "python -m pip install -e .",
                        "exit_code": 1,
                    }
                ],
            },
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    failures = [atom for atom in atoms_doc["atoms"] if atom.get("source") == "command_failure"]
    assert [atom.get("command") for atom in failures] == ["python -m pip install -e ."]
    assert failures[0].get("from_metrics") is True
    assert failures[0].get("from_events") is None


def test_extract_backlog_atoms_ignores_ripgrep_no_matches_from_events(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "normalized_events.jsonl").write_text(
        json.dumps(
            {
                "type": "run_command",
                "data": {
                    "command": "rg -n \"WindowsApps\" README.md docs -S",
                    "exit_code": 1,
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "timestamp_utc": "2026-01-01T00:00:00Z",
            "agent": "codex",
            "status": "ok",
            "report": {},
            "report_validation_errors": None,
            "error": None,
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    assert [
        atom
        for atom in atoms_doc["atoms"]
        if atom.get("source") in {"command_failure", "command_failure_truncated"}
    ] == []


def test_extract_backlog_atoms_emits_token_monitoring_signal_atoms(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "token_monitoring.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "signals": [
                    {
                        "signal_id": "broad_source_config_read",
                        "confidence": "authoritative",
                        "causal_mechanism": (
                            "Source/config exploration was retained in context."
                        ),
                        "token_dimensions_affected": {
                            "total_tokens": 130000,
                            "input_tokens": 125000,
                            "cached_input_tokens": 100000,
                            "uncached_input_tokens": 25000,
                            "output_tokens": 5000,
                            "reasoning_output_tokens": 500,
                        },
                        "evidence": {
                            "call_count": 3,
                            "call_indexes": [4, 5, "6"],
                            "paths_from_calls": ["packages/runner_core/src/runner_core/runner.py"],
                            "raw_prompt": "do not copy this raw prompt text",
                        },
                        "mitigation_lever": "Use targeted section reads.",
                        "false_positive_risk": "Low.",
                        "confirmed_by_counters": True,
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "timestamp_utc": "2026-01-01T00:00:00Z",
            "agent": "codex",
            "status": "ok",
            "report": {},
            "report_validation_errors": None,
            "error": None,
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    signal = next(
        atom for atom in atoms_doc["atoms"] if atom.get("source") == "token_monitoring_signal"
    )
    assert signal["token_signal_id"] == "broad_source_config_read"
    assert signal["severity_hint"] == "high"
    assert signal["token_dimensions_affected"]["input_tokens"] == 125000
    assert signal["evidence_call_count"] == 3
    assert signal["evidence_call_indexes"] == [4, 5, 6]
    assert signal["evidence_paths_preview"] == [
        "packages/runner_core/src/runner_core/runner.py"
    ]
    assert signal["confirmed_by_counters"] is True
    assert "do not copy this raw prompt text" not in json.dumps(signal)
    assert atoms_doc["totals"]["source_counts"]["token_monitoring_signal"] == 1


def test_extract_backlog_atoms_emits_token_monitoring_error_atoms(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "token_monitoring_error.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "RuntimeError",
                "message": "monitor failed",
                "generated_at_utc": "2026-01-01T00:00:01Z",
                "non_fatal": True,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "timestamp_utc": "2026-01-01T00:00:00Z",
            "agent": "codex",
            "status": "ok",
            "report": {},
            "report_validation_errors": None,
            "error": None,
        }
    ]

    atoms_doc = extract_backlog_atoms(records, repo_root=tmp_path)
    error_atom = next(
        atom for atom in atoms_doc["atoms"] if atom.get("source") == "token_monitoring_error"
    )
    assert error_atom["severity_hint"] == "high"
    assert error_atom["error_type"] == "RuntimeError"
    assert error_atom["non_fatal"] is True
    assert atoms_doc["totals"]["source_counts"]["token_monitoring_error"] == 1


def test_parse_ticket_list_recovers_array_and_normalizes() -> None:
    raw = """
    Notes before JSON.
    [
      {
        "title": "Improve quickstart docs",
        "problem": "Users cannot find first command",
        "user_impact": "Blocked onboarding",
        "severity": "high",
        "confidence": "0.8",
        "evidence_atom_ids": ["runA:confusion_point:1"],
        "investigation_steps": ["Reproduce setup from README"],
        "success_criteria": ["Fresh clone reaches first output"],
        "suggested_owner": "docs"
      },
      {
        "title": "Bad ticket",
        "severity": "low",
        "evidence_atom_ids": []
      }
    ]
    """

    tickets, errors = parse_ticket_list(raw)
    assert len(tickets) == 1
    assert tickets[0]["title"] == "Improve quickstart docs"
    assert tickets[0]["confidence"] == 0.8
    assert errors


def test_dedupe_and_merge_candidate_generation() -> None:
    ticket_a = {
        "title": "Fix README quickstart",
        "problem": "missing steps",
        "user_impact": "onboarding blocked",
        "severity": "high",
        "confidence": 0.6,
        "evidence_atom_ids": ["a:1", "a:2"],
        "investigation_steps": ["read README"],
        "success_criteria": ["run command works"],
    }
    ticket_b = {
        "title": "README quickstart fix",
        "problem": "setup unclear",
        "user_impact": "user confusion",
        "severity": "medium",
        "confidence": 0.7,
        "evidence_atom_ids": ["a:2", "a:3"],
        "investigation_steps": ["compare docs"],
        "success_criteria": ["new users finish"],
    }
    embedder = _DeterministicEmbedder()
    deduped = dedupe_tickets([ticket_a, ticket_b], embedder=embedder)
    assert len(deduped) == 1
    assert sorted(deduped[0]["evidence_atom_ids"]) == ["a:1", "a:2", "a:3"]

    candidates = build_merge_candidates([ticket_a, ticket_b], embedder=embedder)
    assert candidates == [(0, 1)]


def test_build_backlog_document_and_markdown(tmp_path: Path) -> None:
    atoms_doc = {
        "atoms": [
            {
                "atom_id": "runA:confusion_point:1",
                "run_rel": "runA",
                "agent": "codex",
                "source": "confusion_point",
                "severity_hint": "high",
                "text": "No quickstart docs",
            },
            {
                "atom_id": "runB:confidence_missing:1",
                "run_rel": "runB",
                "agent": "claude",
                "source": "confidence_missing",
                "severity_hint": "low",
                "text": "No smoke test command",
            },
        ],
        "totals": {
            "runs": 2,
            "atoms": 2,
            "source_counts": {"confusion_point": 1, "confidence_missing": 1},
            "severity_hint_counts": {"high": 1, "low": 1},
        },
    }
    tickets = [
        {
            "title": "Add quickstart section",
            "problem": "No quickstart docs",
            "user_impact": "Users blocked",
            "severity": "high",
            "confidence": 0.9,
            "evidence_atom_ids": ["runA:confusion_point:1"],
            "proposed_fix": "Document one-command path",
            "investigation_steps": ["Review current README"],
            "success_criteria": ["Fresh clone to first output in < 5 min"],
            "suggested_owner": "docs",
        }
    ]

    summary = build_backlog_document(
        atoms_doc=atoms_doc,
        tickets=tickets,
        input_meta={"target": "target_a"},
        artifacts={"atoms_jsonl": "atoms.jsonl"},
        miners_meta={"miners_total": 3, "miners_completed": 3, "miners_failed": 0},
    )

    assert summary["totals"]["tickets"] == 1
    assert summary["coverage"]["covered_atoms"] == 1
    assert summary["coverage"]["uncovered_atoms"] == 1

    md = render_backlog_markdown(summary, title="Backlog Test")
    assert "# Backlog Test" in md
    assert "## Untriaged Tail" in md
    assert "runB:confidence_missing:1" in md

    out_json = tmp_path / "backlog.json"
    out_md = tmp_path / "backlog.md"
    out_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    out_md.write_text(md, encoding="utf-8")
    assert out_json.exists()
    assert out_md.exists()


def test_ticket_below_high_blocked_when_evidence_has_single_run() -> None:
    atoms_doc = {
        "atoms": [
            {
                "atom_id": "runA:confusion_point:1",
                "run_rel": "runA",
                "agent": "codex",
                "source": "confusion_point",
                "severity_hint": "low",
                "text": "No quickstart docs",
            },
            {
                "atom_id": "runB:confusion_point:1",
                "run_rel": "runB",
                "agent": "codex",
                "source": "confusion_point",
                "severity_hint": "low",
                "text": "No quickstart docs",
            },
        ],
        "totals": {"runs": 2, "atoms": 2},
    }
    tickets = [
        {
            "title": "Improve quickstart docs",
            "problem": "README lacks examples",
            "user_impact": "Onboarding slowed",
            "severity": "medium",
            "confidence": 0.6,
            "evidence_atom_ids": ["runA:confusion_point:1"],
            "investigation_steps": ["Review README"],
            "success_criteria": ["Fresh clone to first output"],
        }
    ]

    summary = build_backlog_document(atoms_doc=atoms_doc, tickets=tickets, input_meta={})
    out = summary["tickets"][0]
    assert out["severity"] == "medium"
    assert out["stage"] == "blocked"
    assert "insufficient_run_breadth_for_non_high_severity" in out.get("risks", [])


def test_ticket_low_blocked_when_evidence_lacks_model_breadth() -> None:
    atoms_doc = {
        "atoms": [
            {
                "atom_id": "runA:confusion_point:1",
                "run_rel": "runA",
                "agent": "codex",
                "source": "confusion_point",
                "severity_hint": "low",
                "text": "No quickstart docs",
            },
            {
                "atom_id": "runB:confusion_point:1",
                "run_rel": "runB",
                "agent": "codex",
                "source": "confusion_point",
                "severity_hint": "low",
                "text": "No quickstart docs",
            },
        ],
        "totals": {"runs": 2, "atoms": 2},
    }
    tickets = [
        {
            "title": "Minor docs nit appears repeatedly",
            "problem": "Docs could be clearer",
            "user_impact": "Small friction",
            "severity": "low",
            "confidence": 0.7,
            "evidence_atom_ids": ["runA:confusion_point:1", "runB:confusion_point:1"],
            "investigation_steps": ["Review docs"],
            "success_criteria": ["Docs updated"],
        }
    ]

    summary = build_backlog_document(atoms_doc=atoms_doc, tickets=tickets, input_meta={})
    out = summary["tickets"][0]
    assert out["severity"] == "low"
    assert out["stage"] == "blocked"
    assert "insufficient_model_breadth_for_low_severity" in out.get("risks", [])


def test_ticket_low_allowed_when_evidence_spans_two_models() -> None:
    atoms_doc = {
        "atoms": [
            {
                "atom_id": "runA:confusion_point:1",
                "run_rel": "runA",
                "agent": "codex",
                "source": "confusion_point",
                "severity_hint": "low",
                "text": "No quickstart docs",
            },
            {
                "atom_id": "runB:confusion_point:1",
                "run_rel": "runB",
                "agent": "claude",
                "source": "confusion_point",
                "severity_hint": "low",
                "text": "No quickstart docs",
            },
        ],
        "totals": {"runs": 2, "atoms": 2},
    }
    tickets = [
        {
            "title": "Minor docs nit confirmed across models",
            "problem": "Docs could be clearer",
            "user_impact": "Small friction",
            "severity": "low",
            "confidence": 0.7,
            "evidence_atom_ids": ["runA:confusion_point:1", "runB:confusion_point:1"],
            "investigation_steps": ["Review docs"],
            "success_criteria": ["Docs updated"],
        }
    ]

    summary = build_backlog_document(atoms_doc=atoms_doc, tickets=tickets, input_meta={})
    out = summary["tickets"][0]
    assert out["severity"] == "low"
    assert out["stage"] == "triage"
    assert "insufficient_run_breadth_for_non_high_severity" not in out.get("risks", [])
    assert "insufficient_model_breadth_for_low_severity" not in out.get("risks", [])
