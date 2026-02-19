from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from backlog_core.backlog import (
    build_backlog_document,
    build_merge_candidates,
    dedupe_tickets,
    extract_backlog_atoms,
    parse_ticket_list,
    render_backlog_markdown,
    write_backlog_atoms,
)


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


def test_extract_backlog_atoms_preserves_structured_fields(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text(
        "EPIPE writing to socket\n" + ("x" * 200 + "\n") * 6000,
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
    assert totals["source_counts"].get("agent_stderr_artifact", 0) == 0
    assert totals["source_counts"].get("agent_last_message_artifact", 0) == 0


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
                "[codex_warning_summary] code=shell_snapshot_powershell_unsupported "
                "occurrences=4 classification=capability_notice",
                "hint=PowerShell shell snapshot unsupported; "
                "continuing without shell snapshot metadata.",
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
    assert "capability_warning_artifact" in sources
    warning_atom = next(
        atom for atom in atoms_doc["atoms"] if atom.get("source") == "capability_warning_artifact"
    )
    assert warning_atom.get("severity_hint") == "low"
    assert "shell_snapshot_powershell_unsupported" in warning_atom.get("warning_codes", [])


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
                "commands_failed": 2,
                "failed_commands": [
                    {
                        "command": "python -m pip install -e .",
                        "exit_code": 1,
                        "cwd": "C:/ws",
                        "output_excerpt": "ERROR: Could not find a version that satisfies the requirement ...",
                        "output_excerpt_truncated": True,
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

    first = failures[0]
    assert first.get("from_metrics") is True
    assert first.get("command") == "python -m pip install -e ."
    assert first.get("exit_code") == 1
    assert first.get("cwd") == "C:/ws"
    assert first.get("output_excerpt_truncated") is True

    trunc = next(atom for atom in atoms_doc["atoms"] if atom.get("source") == "command_failure_truncated")
    assert trunc.get("omitted_count") == 3


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
