from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import backlog_core.stage_contracts as stage_contracts
import pytest
from agent_adapters.read_attestation import observed_read_attestation
from backlog_core import bind_plan_outcome_oracle
from runner_core.outcome_roles import run_outcome_evidence_role

import backlog_miner.research_evidence as mod
from backlog_miner.origin_evidence import (
    materialize_origin_attachments,
    origin_attachment_requirements,
)


def _required_powershell_executable() -> str:
    """Select an installed PowerShell without silently weakening CI coverage."""

    for executable in ("pwsh", "powershell.exe"):
        if shutil.which(executable) is not None:
            return executable
    ci_value = os.environ.get("CI", "").strip().casefold()
    if ci_value not in {"", "0", "false", "no"}:
        pytest.fail("PowerShell replay tests require pwsh or powershell.exe in CI", pytrace=False)
    pytest.skip("PowerShell replay tests require pwsh or powershell.exe")


def test_persisted_attempt_rescore_receipt_is_rehashed_even_for_invocation_failure(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "rescore.json"
    receipt.write_text('{"status":"authenticated"}\n', encoding="utf-8")
    dossier = {
        "research_attempts": [
            {
                "outcome": "invocation_failed",
                "validation_error_rescore": {
                    "rescore_receipt_path": str(receipt),
                    "rescore_receipt_sha256": sha256(receipt.read_bytes()).hexdigest(),
                },
            }
        ]
    }

    assert mod._persisted_research_attempt_errors(dossier) == []

    receipt.write_text('{"status":"changed"}\n', encoding="utf-8")
    assert mod._persisted_research_attempt_errors(dossier) == [
        "research_attempt_rescore_receipt_changed:0"
    ]
    receipt.unlink()
    assert mod._persisted_research_attempt_errors(dossier) == [
        "research_attempt_rescore_receipt_missing:0"
    ]


def test_persisted_attempt_persistence_replay_receipt_is_rehashed(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "persistence-replay.json"
    receipt.write_text('{"status":"verified"}\n', encoding="utf-8")
    dossier = {
        "research_attempts": [
            {
                "attempt_kind": "evidence_verification_persistence_replay",
                "outcome": "invocation_failed",
                "repair_progress": {
                    "replay_receipt_path": str(receipt),
                    "replay_receipt_sha256": sha256(receipt.read_bytes()).hexdigest(),
                },
            }
        ]
    }

    assert mod._persisted_research_attempt_errors(dossier) == []

    receipt.write_text('{"status":"changed"}\n', encoding="utf-8")
    assert mod._persisted_research_attempt_errors(dossier) == [
        "research_attempt_persistence_replay_receipt_changed:0"
    ]
    receipt.unlink()
    assert mod._persisted_research_attempt_errors(dossier) == [
        "research_attempt_persistence_replay_receipt_missing:0"
    ]


def test_persisted_attempt_promotion_receipt_is_rehashed_even_for_invocation_failure(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "promotion.json"
    receipt.write_text('{"status":"authenticated"}\n', encoding="utf-8")
    dossier = {
        "research_attempts": [
            {
                "attempt_kind": "evidence_verification_promotion",
                "outcome": "invocation_failed",
                "repair_progress": {
                    "replay_receipt_path": str(receipt),
                    "replay_receipt_sha256": sha256(receipt.read_bytes()).hexdigest(),
                },
            }
        ]
    }

    assert mod._persisted_research_attempt_errors(dossier) == []

    receipt.write_text('{"status":"changed"}\n', encoding="utf-8")
    assert mod._persisted_research_attempt_errors(dossier) == [
        "research_attempt_promotion_receipt_changed:0"
    ]
    receipt.unlink()
    assert mod._persisted_research_attempt_errors(dossier) == [
        "research_attempt_promotion_receipt_missing:0"
    ]


def test_assignment_verifier_accepts_exact_whitelisted_nested_run_context(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "source"
    shell_probe_events = run_dir / "agent_shell_probe" / "raw_events.jsonl"
    shell_probe_events.parent.mkdir(parents=True)
    shell_probe_events.write_text(
        '{"type":"item.completed","item":{"type":"command_execution",'
        '"aggregated_output":"shell_probe=ok\\n","exit_code":0}}\n',
        encoding="utf-8",
    )
    atom_id = "atom:nested-shell-probe"
    snapshot = {"atom_id": atom_id, "run_dir": str(run_dir)}
    assignment = {
        "status": "complete",
        "expected_atom_ids": [atom_id],
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_sha256": mod._canonical_json_sha256(snapshot),
                "atom_snapshot": snapshot,
                "artifact_receipts": [
                    {
                        "path": str(shell_probe_events),
                        "source_relpath": "agent_shell_probe/raw_events.jsonl",
                        "research_context_role": "agent_shell_probe_events",
                        "sha256": sha256(shell_probe_events.read_bytes()).hexdigest(),
                        "size_bytes": shell_probe_events.stat().st_size,
                    }
                ],
            }
        ],
    }
    assignment["assignment_sha256"] = mod.evidence_assignment_sha256(assignment)

    assert mod._verify_assignment_files(
        assignment,
        expected_atom_ids=[atom_id],
    ) == []

    assignment["atom_receipts"][0]["artifact_receipts"][0]["source_relpath"] = (
        "unapproved/raw_events.jsonl"
    )
    assignment["assignment_sha256"] = mod.evidence_assignment_sha256(assignment)
    assert mod._verify_assignment_files(
        assignment,
        expected_atom_ids=[atom_id],
    ) == [
        f"origin_atom_context_artifact_invalid:{atom_id}:{shell_probe_events}"
    ]


def test_required_powershell_executable_prefers_cross_platform_pwsh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda executable: f"/bin/{executable}")

    assert _required_powershell_executable() == "pwsh"


def test_required_powershell_executable_fails_when_ci_has_no_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _executable: None)
    monkeypatch.setenv("CI", "true")

    with pytest.raises(pytest.fail.Exception, match="require pwsh or powershell.exe in CI"):
        _required_powershell_executable()


def test_trusted_host_replay_does_not_substitute_an_unavailable_executable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing_executable = "usertest-intentionally-missing-replay-runtime"
    assert shutil.which(missing_executable) is None

    with pytest.raises(FileNotFoundError):
        mod.TrustedHostReplayExecutor(approved_source_roots=[tmp_path]).execute(
            [missing_executable, "--version"],
            cwd=workspace,
            source_workspace=workspace,
            timeout_seconds=None,
        )


def _write_normalized_events(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _event_source_attempt(
    *,
    run_dir: Path,
    workspace: Path,
    revision: str,
    case_id: str,
    problem_id: str,
    session_id: str,
    events: list[dict[str, object]],
) -> dict[str, object]:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text('{"status":"complete"}\n', encoding="utf-8")
    (run_dir / "workspace_ref.json").write_text(
        json.dumps({"workspace_dir": str(workspace)}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "target_ref.json").write_text(
        json.dumps({"agent": "codex", "commit_sha": revision, "ref": revision}) + "\n",
        encoding="utf-8",
    )
    _write_normalized_events(run_dir / "normalized_events.jsonl", events)
    (run_dir / "codex_execpolicy_overlay.json").write_text("{}\n", encoding="utf-8")

    def artifact(kind: str, filename: str) -> dict[str, object]:
        path = run_dir / filename
        return {
            "kind": kind,
            "path": str(path.resolve()),
            "exists": True,
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }

    attempted_dossier = {"case_id": case_id, "problem_id": problem_id}
    attempt: dict[str, object] = {
        "attempt_number": 1,
        "attempt_kind": "full_research",
        "outcome": "evidence_verification_invalid",
        "run_dir": str(run_dir.resolve()),
        "report_path": str((run_dir / "report.json").resolve()),
        "attempted_dossier": attempted_dossier,
        "attempted_dossier_sha256": mod._canonical_json_sha256(attempted_dossier),
        "agent_session_id": session_id,
        "observed_agent_session_id": session_id,
        "attempt_artifacts": [
            artifact("report", "report.json"),
            artifact("workspace_ref", "workspace_ref.json"),
            artifact("target_ref", "target_ref.json"),
            artifact("normalized_events", "normalized_events.jsonl"),
            artifact("codex_subscription_auth", "codex_execpolicy_overlay.json"),
        ],
    }
    attempt["attempt_sha256"] = stage_contracts.research_attempt_sha256(attempt)
    return attempt


def _write_current_correction_lineage(
    *,
    run_dir: Path,
    revision: str,
    session_id: str,
) -> dict[str, object]:
    (run_dir / "target_ref.json").write_text(
        json.dumps(
            {
                "agent": "codex",
                "commit_sha": revision,
                "ref": revision,
                "requested_codex_resume_session_id": session_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "codex_execpolicy_overlay.json").write_text("{}\n", encoding="utf-8")
    errors: list[str] = []
    binding = mod._current_correction_lineage_binding(
        run_dir=run_dir,
        expected_agent_session_id=session_id,
        errors=errors,
    )
    assert errors == []
    assert binding is not None
    return binding


def test_command_observation_normalizes_equivalent_relative_path_spelling() -> None:
    declared = r"python .usertest_research\shell_capability_research.py gemini-run-once-block"
    observed = r"python .\.usertest_research\shell_capability_research.py gemini-run-once-block"

    assert mod._normalize_command(declared) == mod._normalize_command(observed)
    assert mod._normalize_command(declared) != mod._normalize_command(
        r"python .\.usertest_research\different_probe.py gemini-run-once-block"
    )


def test_command_observation_normalizes_doubled_windows_path_separators() -> None:
    declared = (
        r"python .usertest_research\probe.py "
        r"--out .usertest_research\result.json"
    )
    observed = (
        r"python .usertest_research\\probe.py "
        r"--out .usertest_research\\result.json"
    )

    assert mod._normalize_command(declared) == mod._normalize_command(observed)


def test_command_observation_normalizes_doubled_windows_executable_separators() -> None:
    declared = (
        r"C:\Users\jason\AppData\Local\Python\python.exe "
        r".usertest_research\probe.py source_failure"
    )
    observed = (
        r"C:\\Users\\jason\\AppData\\Local\\Python\\python.exe "
        r".usertest_research\probe.py source_failure"
    )

    assert mod._normalize_command(declared) == mod._normalize_command(observed)


def test_repository_python_import_environment_uses_only_pinned_src_projects(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    for relative in (
        "packages/alpha",
        "apps/tool",
        ".usertest_research/untrusted",
        "packages/no_src",
    ):
        project = workspace / relative
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (workspace / "packages" / "alpha" / "src").mkdir()
    (workspace / "apps" / "tool" / "src").mkdir()
    (workspace / ".usertest_research" / "untrusted" / "src").mkdir()

    pythonpath, receipt = mod._repository_python_import_environment(
        workspace,
        execution_root="/workspace",
        path_separator=":",
    )

    assert pythonpath == "/workspace/apps/tool/src:/workspace/packages/alpha/src"
    assert receipt["runner_applied"] is True
    assert receipt["source_roots"] == ["apps/tool/src", "packages/alpha/src"]
    assert receipt["repository_python_import_sha256"] == mod._canonical_json_sha256(
        {key: value for key, value in receipt.items() if key != "repository_python_import_sha256"}
    )


def test_trusted_host_replay_imports_package_from_pinned_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    package = workspace / "packages" / "fixture_dep"
    module = package / "src" / "fixture_dep"
    module.mkdir(parents=True)
    (package / "pyproject.toml").write_text("[project]\nname='fixture-dep'\n", encoding="utf-8")
    (module / "__init__.py").write_text("VALUE = 'pinned'\n", encoding="utf-8")
    harness = workspace / ".usertest_research" / "probe.py"
    harness.parent.mkdir()
    harness.write_text(
        "from fixture_dep import VALUE\nprint(VALUE)\n",
        encoding="utf-8",
    )

    completed = mod.TrustedHostReplayExecutor(approved_source_roots=[tmp_path]).execute(
        [sys.executable, str(harness)],
        cwd=workspace,
        source_workspace=workspace,
        timeout_seconds=None,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "pinned"
    import_receipt = completed.execution_metadata["repository_python_import"]
    assert import_receipt["runner_applied"] is True
    assert import_receipt["source_roots"] == ["packages/fixture_dep/src"]


def test_evidence_event_stream_retains_fresh_events_before_empty_current_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "verify_controlled_codex_execpolicy_receipt", lambda _path: [])
    fresh_run = tmp_path / "fresh-run"
    current_run = tmp_path / "current-correction"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    revision = "a" * 40
    session_id = "33333333-3333-4333-8333-333333333333"
    fresh_events: list[dict[str, object]] = [
        {"type": "run_command", "data": {"command": "python proof.py", "exit_code": 0}},
        {"type": "read_file", "data": {"path": "src/core.py"}},
    ]
    attempt = _event_source_attempt(
        run_dir=fresh_run,
        workspace=workspace,
        revision=revision,
        case_id="case:one",
        problem_id="problem:one",
        session_id=session_id,
        events=fresh_events,
    )
    latest_attempt = deepcopy(attempt)
    latest_attempt["attempt_number"] = 2
    latest_attempt["validation_errors"] = ["corrected_metadata"]
    latest_attempt["attempt_sha256"] = stage_contracts.research_attempt_sha256(latest_attempt)
    _write_normalized_events(current_run / "normalized_events.jsonl", [])
    current_lineage = _write_current_correction_lineage(
        run_dir=current_run,
        revision=revision,
        session_id=session_id,
    )

    errors: list[str] = []
    events, sources, sources_sha256 = mod._load_evidence_event_stream(
        run_dir=current_run,
        evidence_attempts=[attempt, latest_attempt],
        case_id="case:one",
        problem_id="problem:one",
        repo_revision=revision,
        workspace=workspace,
        agent_session_id=session_id,
        current_run_lineage=current_lineage,
        errors=errors,
    )

    assert errors == []
    assert events == fresh_events
    assert [source["run_dir"] for source in sources] == [
        str(fresh_run.resolve()),
        str(current_run.resolve()),
    ]
    assert sources[0]["global_start_index"] == 0
    assert sources[0]["global_end_index_exclusive"] == 2
    assert sources[0]["source_kind"] == "prior_attempt"
    assert sources[0]["attempt_sha256"] == latest_attempt["attempt_sha256"]
    assert sources[0]["binding_sha256"] == mod._canonical_json_sha256(
        {
            key: sources[0][key]
            for key in mod._RESEARCH_ATTEMPT_EVENT_BINDING_FIELDS
            if key != "binding_sha256"
        }
    )
    assert sources[1]["source_kind"] == "current_run"
    assert sources[1]["event_count"] == 0
    assert sources[1]["global_start_index"] == 2
    assert sources[1]["global_end_index_exclusive"] == 2
    assert sources_sha256 == mod._canonical_json_sha256(sources)


def test_persisted_evidence_event_stream_rejects_tamper_and_reordering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "verify_controlled_codex_execpolicy_receipt", lambda _path: [])
    fresh_run = tmp_path / "fresh-run"
    current_run = tmp_path / "current-correction"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    revision = "b" * 40
    session_id = "44444444-4444-4444-8444-444444444444"
    fresh_events: list[dict[str, object]] = [
        {"type": "run_command", "data": {"command": "python proof.py", "exit_code": 0}}
    ]
    attempt = _event_source_attempt(
        run_dir=fresh_run,
        workspace=workspace,
        revision=revision,
        case_id="case:one",
        problem_id="problem:one",
        session_id=session_id,
        events=fresh_events,
    )
    _write_normalized_events(current_run / "normalized_events.jsonl", [])
    current_lineage = _write_current_correction_lineage(
        run_dir=current_run,
        revision=revision,
        session_id=session_id,
    )
    build_errors: list[str] = []
    _events, sources, sources_sha256 = mod._load_evidence_event_stream(
        run_dir=current_run,
        evidence_attempts=[attempt],
        case_id="case:one",
        problem_id="problem:one",
        repo_revision=revision,
        workspace=workspace,
        agent_session_id=session_id,
        current_run_lineage=current_lineage,
        errors=build_errors,
    )
    assert build_errors == []
    receipt: dict[str, object] = {
        "run_dir": str(current_run.resolve()),
        "case_id": "case:one",
        "problem_id": "problem:one",
        "repo_revision": revision,
        "workspace_dir": str(workspace.resolve()),
        "evidence_agent_session_id": session_id,
        "evidence_event_sources": sources,
        "evidence_event_sources_sha256": sources_sha256,
    }

    persisted_errors: list[str] = []
    assert (
        mod._load_persisted_evidence_event_stream(
            receipt,
            current_run_dir=current_run,
            research_attempts=[attempt],
            errors=persisted_errors,
        )
        == fresh_events
    )
    assert persisted_errors == []

    reordered = deepcopy(receipt)
    reordered_sources_raw = reordered["evidence_event_sources"]
    assert isinstance(reordered_sources_raw, list)
    reordered_sources = list(reversed(reordered_sources_raw))
    reordered["evidence_event_sources"] = reordered_sources
    reordered["evidence_event_sources_sha256"] = mod._canonical_json_sha256(reordered_sources)
    reordered_errors: list[str] = []
    mod._load_persisted_evidence_event_stream(
        reordered,
        current_run_dir=current_run,
        research_attempts=[attempt],
        errors=reordered_errors,
    )
    assert "research_evidence_event_source_invalid:0" in reordered_errors
    assert "research_evidence_event_current_source_not_last" in reordered_errors

    wrong_binding = deepcopy(receipt)
    wrong_sources = wrong_binding["evidence_event_sources"]
    assert isinstance(wrong_sources, list)
    assert isinstance(wrong_sources[0], dict)
    wrong_sources[0]["case_id"] = "case:other"
    wrong_binding["evidence_event_sources_sha256"] = mod._canonical_json_sha256(wrong_sources)
    wrong_binding_errors: list[str] = []
    mod._load_persisted_evidence_event_stream(
        wrong_binding,
        current_run_dir=current_run,
        research_attempts=[attempt],
        errors=wrong_binding_errors,
    )
    assert "research_evidence_event_source_binding_changed:0" in wrong_binding_errors

    current_target_path = current_run / "target_ref.json"
    current_target_bytes = current_target_path.read_bytes()
    current_target_path.write_text(
        json.dumps(
            {
                "agent": "codex",
                "commit_sha": revision,
                "ref": revision,
                "requested_codex_resume_session_id": "different-session",
            }
        ),
        encoding="utf-8",
    )
    wrong_current_errors: list[str] = []
    mod._load_persisted_evidence_event_stream(
        receipt,
        current_run_dir=current_run,
        research_attempts=[attempt],
        errors=wrong_current_errors,
    )
    assert any(
        error.endswith("research_evidence_current_resume_session_mismatch")
        for error in wrong_current_errors
    )
    current_target_path.write_bytes(current_target_bytes)

    with (fresh_run / "normalized_events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "agent_message", "data": {"text": "tampered"}}) + "\n")
    tampered_errors: list[str] = []
    mod._load_persisted_evidence_event_stream(
        receipt,
        current_run_dir=current_run,
        research_attempts=[attempt],
        errors=tampered_errors,
    )
    assert "research_evidence_event_source_changed:0" in tampered_errors


def test_recovered_event_source_attempt_is_persisted_outside_authoring_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "verify_controlled_codex_execpolicy_receipt", lambda _path: [])
    source_run = tmp_path / "recovered-source"
    current_run = tmp_path / "current-correction"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    revision = "d" * 40
    session_id = "55555555-5555-4555-8555-555555555555"
    source_events: list[dict[str, object]] = [
        {"type": "run_command", "data": {"command": "python proof.py", "exit_code": 0}}
    ]
    recovered_attempt = _event_source_attempt(
        run_dir=source_run,
        workspace=workspace,
        revision=revision,
        case_id="case:one",
        problem_id="problem:one",
        session_id=session_id,
        events=source_events,
    )
    _write_normalized_events(current_run / "normalized_events.jsonl", [])
    current_lineage = _write_current_correction_lineage(
        run_dir=current_run,
        revision=revision,
        session_id=session_id,
    )
    build_errors: list[str] = []
    _events, sources, sources_sha256 = mod._load_evidence_event_stream(
        run_dir=current_run,
        evidence_attempts=[recovered_attempt],
        case_id="case:one",
        problem_id="problem:one",
        repo_revision=revision,
        workspace=workspace,
        agent_session_id=session_id,
        current_run_lineage=current_lineage,
        errors=build_errors,
    )
    assert build_errors == []

    source_attempts = mod._evidence_source_attempt_catalog([recovered_attempt], sources)
    assert source_attempts == [recovered_attempt]
    receipt: dict[str, object] = {
        "run_dir": str(current_run.resolve()),
        "case_id": "case:one",
        "problem_id": "problem:one",
        "repo_revision": revision,
        "workspace_dir": str(workspace.resolve()),
        "evidence_agent_session_id": session_id,
        "evidence_event_sources": sources,
        "evidence_event_sources_sha256": sources_sha256,
        "evidence_source_attempts": source_attempts,
        "evidence_source_attempts_sha256": mod._canonical_json_sha256(source_attempts),
    }

    catalog_errors: list[str] = []
    persisted_catalog = mod._persisted_evidence_attempt_catalog(
        receipt,
        [],
        errors=catalog_errors,
    )
    assert catalog_errors == []
    persisted_errors: list[str] = []
    assert mod._load_persisted_evidence_event_stream(
        receipt,
        current_run_dir=current_run,
        research_attempts=persisted_catalog,
        errors=persisted_errors,
    ) == source_events
    assert persisted_errors == []

    without_catalog_errors: list[str] = []
    mod._load_persisted_evidence_event_stream(
        receipt,
        current_run_dir=current_run,
        research_attempts=[],
        errors=without_catalog_errors,
    )
    assert without_catalog_errors == ["research_evidence_event_source_attempt_unmatched:0"]

    tampered = deepcopy(receipt)
    tampered_attempts = tampered["evidence_source_attempts"]
    assert isinstance(tampered_attempts, list)
    assert isinstance(tampered_attempts[0], dict)
    tampered_attempts[0]["outcome"] = "changed"
    tamper_errors: list[str] = []
    mod._persisted_evidence_attempt_catalog(tampered, [], errors=tamper_errors)
    assert "research_evidence_source_attempts_hash_changed" in tamper_errors


def test_evidence_event_stream_current_run_only_and_legacy_receipt_compatibility(
    tmp_path: Path,
) -> None:
    current_run = tmp_path / "current-run"
    current_events: list[dict[str, object]] = [
        {"type": "agent_message", "data": {"text": "complete"}}
    ]
    _write_normalized_events(current_run / "normalized_events.jsonl", current_events)
    errors: list[str] = []
    events, sources, _sources_sha256 = mod._load_evidence_event_stream(
        run_dir=current_run,
        evidence_attempts=[],
        case_id="case:one",
        problem_id="problem:one",
        repo_revision="c" * 40,
        workspace=None,
        agent_session_id=None,
        current_run_lineage=None,
        errors=errors,
    )
    assert errors == []
    assert events == current_events
    assert len(sources) == 1
    assert sources[0]["run_dir"] == str(current_run.resolve())

    legacy_errors: list[str] = []
    assert (
        mod._load_persisted_evidence_event_stream(
            {"run_dir": str(current_run.resolve())},
            current_run_dir=current_run,
            research_attempts=[],
            errors=legacy_errors,
        )
        == current_events
    )
    assert legacy_errors == []


def test_experiment_receipt_prefers_latest_current_duplicate() -> None:
    prior_event = {
        "type": "run_command",
        "data": {
            "command": "python proof.py",
            "exit_code": 0,
            "output_excerpt": "prior observation",
        },
    }
    current_event = {
        "type": "run_command",
        "data": {
            "command": "python proof.py",
            "exit_code": 0,
            "output_excerpt": "corrected current observation",
        },
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "experiment:one",
                "command": "python proof.py",
                "exit_code": 0,
                "outcome": "supports",
                "artifact_refs": ["artifact:one"],
            }
        ]
    }
    errors: list[str] = []

    receipts, outcomes = mod._experiment_receipts(
        dossier,
        events=[prior_event, current_event],
        artifact_keys={"artifact:one"},
        clean_replays={"experiment:one": {"experiment_id": "experiment:one"}},
        errors=errors,
    )

    assert errors == []
    assert outcomes == {"experiment:one": "supports"}
    assert receipts[0]["agent_event_index"] == 1
    assert receipts[0]["agent_event_sha256"] == mod._canonical_json_sha256(current_event)


def test_clean_revision_view_reuses_effective_relocated_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    requested = tmp_path / "requested" / "revision-view"
    relocated = tmp_path / "windows-temp" / "revision-view"
    relocated.mkdir(parents=True)
    revision = "a" * 40

    def collide_with_relocated_workspace(**_kwargs: object) -> object:
        raise FileExistsError(relocated)

    monkeypatch.setattr(mod, "acquire_target", collide_with_relocated_workspace)
    monkeypatch.setattr(mod, "_workspace_head", lambda workspace: revision)
    monkeypatch.setattr(mod, "_workspace_clean", lambda workspace: True)

    workspace, head, clean, errors = mod.materialize_clean_revision_view(
        source_workspace=source,
        destination=requested,
        repo_revision=revision,
    )

    assert workspace == relocated.resolve()
    assert head == revision
    assert clean is True
    assert errors == []


def _falsification_replay(experiment: dict[str, object]) -> dict[str, object]:
    return {
        "experiment_id": experiment["experiment_id"],
        "command": experiment["command"],
        "declared_result": experiment["result"],
        "exit_code": experiment["exit_code"],
        "outcome": experiment["outcome"],
        "scenario_kind": experiment["scenario_kind"],
        "observable_assertion": experiment["observable_assertion"],
        "assertion_passed": True,
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
    }


def _selected_mechanism_binding(
    *,
    hypothesis_id: str,
    mechanism_evidence: list[dict[str, object]],
    causal_root_evidence_ids: list[str],
) -> dict[str, object]:
    code_paths = sorted(
        {
            (str(point["symbol"]), str(point["path"]))
            for evidence in mechanism_evidence
            for point in evidence.get("code_paths", [])
            if isinstance(point, dict) and "symbol" in point and "path" in point
        }
    )
    verified = {
        "schema_version": 3,
        "mechanism_symbols": sorted({symbol for symbol, _path in code_paths}),
        "code_paths": [{"symbol": symbol, "path": path} for symbol, path in code_paths],
    }
    provenance = {
        "schema_version": 2,
        "primary_hypothesis_id": hypothesis_id,
        "mechanism_evidence_ids": sorted(
            str(evidence["mechanism_evidence_id"]) for evidence in mechanism_evidence
        ),
        "causal_root_evidence_ids": sorted(causal_root_evidence_ids),
    }
    return {
        "verified_mechanism": verified,
        "verified_mechanism_sha256": mod._canonical_json_sha256(verified),
        "verified_mechanism_provenance": provenance,
        "verified_mechanism_provenance_sha256": mod._canonical_json_sha256(provenance),
    }


@pytest.mark.parametrize(
    ("outcome", "disproof", "observed", "expected"),
    [
        (
            "disproved",
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            True,
        ),
        (
            "disproved",
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            {"source": "stdout", "operator": "not_contains", "expected": "fixed"},
            False,
        ),
        (
            "survived",
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            {"source": "stdout", "operator": "not_contains", "expected": "fixed"},
            True,
        ),
        (
            "survived",
            {"source": "stdout", "operator": "not_contains", "expected": "failure"},
            {"source": "stdout", "operator": "contains", "expected": "failure"},
            True,
        ),
        (
            "survived",
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            False,
        ),
        (
            "survived",
            {"source": "exit_code", "operator": "equals", "expected": 0},
            {"source": "exit_code", "operator": "equals", "expected": 1},
            True,
        ),
        (
            "survived",
            {"source": "exit_code", "operator": "equals", "expected": 0},
            {"source": "exit_code", "operator": "equals", "expected": 0},
            False,
        ),
        (
            "inconclusive",
            {"source": "stderr", "operator": "contains", "expected": "x"},
            {"source": "stdout", "operator": "equals", "expected": "y"},
            True,
        ),
    ],
)
def test_falsification_polarity_membership_truth_table_matches_stage_contract(
    outcome: str,
    disproof: dict[str, object],
    observed: dict[str, object],
    expected: bool,
) -> None:
    assert (
        mod._falsification_assertion_relation(
            disproof,
            observed,
            outcome=outcome,
        )
        is expected
    )
    assert (
        stage_contracts._falsification_assertion_relation(
            disproof,
            observed,
            outcome=outcome,
        )
        is expected
    )


def test_falsification_attempt_binding_rejects_unrelated_refuting_experiment() -> None:
    baseline = {
        "experiment_id": "exp-baseline",
        "scenario_kind": "original_replay",
        "addresses_atom_ids": ["atom:one"],
        "command": "python tools/replay.py baseline",
        "result": "The failure is present",
        "outcome": "supports",
        "exit_code": 1,
        "observable_assertion": {
            "source": "stderr",
            "operator": "contains",
            "expected": "failure",
        },
        "artifact_refs": ["artifact:source"],
    }
    challenge = {
        **baseline,
        "experiment_id": "exp-challenge",
        "command": "python tools/replay.py alternative-removed",
        "result": "The failure remains after removing the alternative",
    }
    unrelated = {
        **baseline,
        "experiment_id": "exp-unrelated",
        "command": "python tools/unrelated.py",
        "result": "An unrelated check is green",
        "outcome": "refutes",
        "exit_code": 0,
        "observable_assertion": {
            "source": "exit_code",
            "operator": "equals",
            "expected": 0,
        },
    }
    claim = "The selected mechanism causes the failure."
    attempt = {
        "attempt_id": "attempt:selected-cause",
        "hypothesis_id": "h1",
        "claim": claim,
        "baseline_experiment_id": "exp-baseline",
        "challenge_experiment_id": "exp-challenge",
        "disproof_condition": {
            "source": "stderr",
            "operator": "not_contains",
            "expected": "failure",
        },
        "outcome": "survived",
    }
    dossier = {
        "research_status": "evidence_sufficient",
        "experiments": [baseline, challenge, unrelated],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": claim,
                "mechanism_symbols": ["core.run"],
                "supporting_evidence": ["exp-baseline", "exp-challenge"],
                "counterevidence": ["exp-unrelated"],
                "falsification_attempts": [attempt],
            }
        ],
    }
    clean_replays = {
        experiment["experiment_id"]: _falsification_replay(experiment)
        for experiment in (baseline, challenge, unrelated)
    }
    mechanism_evidence = [
        {
            "mechanism_evidence_id": "mechanism_evidence:baseline",
            "hypothesis_id": "h1",
            "experiment_ids": ["exp-baseline", "exp-challenge"],
            "mechanism_symbols": ["core.run"],
        }
    ]
    errors: list[str] = []
    intervention = {
        "hypothesis_id": "h1",
        "attempt_id": "attempt:selected-cause",
        "intervention_receipt_id": "falsification_intervention:verified-delta",
        "shared_verified_mechanism_symbols": ["core.run"],
    }

    receipts = mod._falsification_attempt_receipts(
        dossier,
        clean_replays=clean_replays,
        mechanism_evidence=mechanism_evidence,
        falsification_interventions=[intervention],
        deterministic_closures=[],
        errors=errors,
    )

    assert errors == []
    assert receipts["h1"][0]["outcome"] == "survived"
    attempt["challenge_experiment_id"] = "exp-unrelated"
    errors = []
    receipts = mod._falsification_attempt_receipts(
        dossier,
        clean_replays=clean_replays,
        mechanism_evidence=mechanism_evidence,
        falsification_interventions=[intervention],
        deterministic_closures=[],
        errors=errors,
    )
    assert receipts["h1"] == []
    assert any(
        error.startswith("falsification_attempt_unbound:h1:attempt:selected-cause")
        for error in errors
    )


def test_falsification_adapter_binds_declared_causal_link_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = {
        "experiment_id": "exp-baseline",
        "scenario_kind": "controlled_replay",
        "addresses_atom_ids": ["atom:one"],
        "command": "python tools/replay.py failed",
        "result": "The failed probe blocks dispatch.",
        "outcome": "supports",
        "exit_code": 0,
        "observable_assertion": {
            "source": "stdout",
            "operator": "contains",
            "expected": "blocked",
        },
        "artifact_refs": ["artifact:source"],
    }
    challenge = {
        **baseline,
        "experiment_id": "exp-challenge",
        "scenario_kind": "control",
        "command": "python tools/replay.py passed",
        "result": "The passed probe permits dispatch.",
        "observable_assertion": {
            "source": "stdout",
            "operator": "contains",
            "expected": "available",
        },
        "control_relationship": {
            "supports_experiment_id": "exp-baseline",
            "controlled_variable": "probe_result",
            "expected_difference": "Only the probe result changes from failed to passed.",
            "mechanism_symbols": ["core.resolve"],
        },
    }
    statement = "The parser result reaches the resolver and controls dispatch."
    dossier = {
        "research_status": "evidence_sufficient",
        "experiments": [baseline, challenge],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": statement,
                "mechanism_symbols": ["core.parse", "core.resolve"],
                "supporting_evidence": ["exp-baseline", "exp-challenge"],
                "counterevidence": [],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:passed-probe-still-blocked",
                        "hypothesis_id": "h1",
                        "claim": statement,
                        "baseline_experiment_id": "exp-baseline",
                        "challenge_experiment_id": "exp-challenge",
                        "disproof_condition": {
                            "source": "stdout",
                            "operator": "not_contains",
                            "expected": "available",
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
    }
    proof = {
        "proof_receipt_id": "causal_proof:" + "d" * 64,
        "hypothesis_id": "h1",
        "observations": {
            "baseline": {"experiment_id": "exp-baseline"},
            "challenge": {"experiment_id": "exp-challenge"},
        },
        "intervention": {
            "target": "core.resolve:probe_result",
            "baseline_experiment_id": "exp-baseline",
            "challenge_experiment_id": "exp-challenge",
        },
        "mechanism_graph": {
            "root_node_id": "proof:root",
            "outcome_node_id": "proof:outcome",
            "nodes": [
                {"node_id": "proof:root", "kind": "source", "locator": "origin"},
                {
                    "node_id": "proof:mechanism",
                    "kind": "argument",
                    "locator": "core.resolve:probe_result",
                },
                {
                    "node_id": "proof:outcome",
                    "kind": "outcome",
                    "locator": "stdout",
                },
            ],
            "edges": [],
        },
        "adapter_evidence": {
            "implementation_touchpoints": [
                {
                    "causal_locator": "core.resolve:probe_result",
                    "path": "src/core.py",
                    "symbols": ["core.resolve"],
                    "runner_attested": True,
                }
            ]
        },
    }
    monkeypatch.setattr(mod, "validate_causal_proof_receipt", lambda _proof: [])
    errors: list[str] = []

    receipts = mod._falsification_attempt_receipts(
        dossier,
        clean_replays={
            experiment["experiment_id"]: _falsification_replay(experiment)
            for experiment in (baseline, challenge)
        },
        mechanism_evidence=[
            {
                "mechanism_evidence_id": "mechanism_evidence:resolver-control",
                "hypothesis_id": "h1",
                "experiment_ids": ["exp-baseline", "exp-challenge"],
                "mechanism_symbols": ["core.resolve"],
            }
        ],
        falsification_interventions=[],
        deterministic_closures=[],
        proof_adapter_receipts=[proof],
        errors=errors,
    )

    assert errors == []
    assert receipts["h1"][0]["outcome"] == "survived"
    assert receipts["h1"][0]["mechanism_symbols"] == ["core.resolve"]


def _git(command: list[str], *, cwd: Path) -> str:
    return subprocess.run(
        ["git", *command],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _baseline_repo(path: Path) -> str:
    (path / "src").mkdir(parents=True)
    (path / "src" / "core.py").write_text("def run():\n    return True\n", encoding="utf-8")
    (path / "tests").mkdir()
    (path / "tests" / "test_core.py").write_text(
        "def test_guarded_control():\n    assert True\n",
        encoding="utf-8",
    )
    _git(["init"], cwd=path)
    _git(["config", "user.email", "tests@example.invalid"], cwd=path)
    _git(["config", "user.name", "Tests"], cwd=path)
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-m", "baseline"], cwd=path)
    return _git(["rev-parse", "HEAD"], cwd=path)


def _role_contract(payload: dict[str, object]) -> dict[str, object]:
    return {**payload, "role_contract_sha256": mod._canonical_json_sha256(payload)}


def test_semantic_basis_requires_matching_relevant_falsification_attempt() -> None:
    quote = "The materialized verification path is not readable by the implementing agent."
    atom = {
        "atom_id": "atom:path",
        "text": quote,
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    experiment = {
        "experiment_id": "exp-path",
        "addresses_atom_ids": ["atom:path"],
        "positive_outcome_contract": {
            "contract_kind": "retained_harness_semantic_assertion",
            "expected_value": True,
            "semantic_relation": "required_operational_property",
            "semantic_rationale": (
                "The assertion requires the same materialized path to be readable after the fix."
            ),
            "semantic_basis": {
                "kind": "source_atom_quote",
                "atom_id": "atom:path",
                "field_path": "$.text",
                "exact_quote": quote,
            },
        },
    }
    assignment = {
        "atom_receipts": [
            {
                "atom_id": "atom:path",
                "atom_sha256": mod._canonical_json_sha256(atom),
                "atom_snapshot": atom,
            }
        ]
    }
    intervention = {
        "hypothesis_id": "h1",
        "attempt_id": "attempt:path-alternative",
        "baseline_experiment_id": "exp-path",
        "intervention_receipt_id": "falsification_intervention:path",
    }
    kwargs = {
        "expected_value": True,
        "evidence_assignment": assignment,
        "planning_workspace": None,
        "inspected_file_receipts": [],
        "inspected_symbol_receipts": [],
        "falsification_interventions": [intervention],
        "hypothesis_ids": {"h1"},
        "mechanism_symbols": {"paths.materialize"},
    }

    assert mod._semantic_basis_receipt(experiment=experiment, **kwargs) is None
    experiment["positive_outcome_contract"]["adversarial_review_reference"] = (
        "attempt:path-alternative"
    )
    receipt = mod._semantic_basis_receipt(experiment=experiment, **kwargs)
    assert receipt is not None
    assert receipt["adversarial_basis"] == {
        "attempt_id": "attempt:path-alternative",
        "intervention_receipt_id": "falsification_intervention:path",
    }


def test_post_merge_replays_hash_attested_research_harness_in_clean_commit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "src").mkdir()
    (source / "src" / "core.py").write_text(
        "def run():\n    return 'bad'\n",
        encoding="utf-8",
    )
    _git(["init"], cwd=source)
    _git(["config", "user.email", "tests@example.invalid"], cwd=source)
    _git(["config", "user.name", "Tests"], cwd=source)
    _git(["add", "-A"], cwd=source)
    _git(["commit", "-m", "bug"], cwd=source)
    researched = _git(["rev-parse", "HEAD"], cwd=source)
    planning = tmp_path / "planning"
    research = tmp_path / "research"
    _git(["clone", str(source), str(planning)], cwd=tmp_path)
    _git(["clone", str(source), str(research)], cwd=tmp_path)
    harness = research / ".usertest_research" / "repro.py"
    harness.parent.mkdir()
    harness.write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path.cwd()))\n"
        "from src.core import run\nassert run() == 'fixed'\n",
        encoding="utf-8",
    )
    overlay = {
        key: value
        for key, value in mod._workspace_manifest(research).items()
        if key.startswith(".usertest_research/")
    }
    experiment = {
        "experiment_id": "exp-original",
        "scenario_kind": "original_replay",
        "addresses_atom_ids": ["atom:original"],
        "command": "python .usertest_research/repro.py",
        "outcome": "supports",
        "exit_code": 1,
        "observable_assertion": {
            "source": "exit_code",
            "operator": "equals",
            "expected": 1,
        },
        "positive_outcome_contract": {
            "contract_kind": "retained_harness_semantic_assertion",
            "expected_value": "fixed",
            "semantic_relation": "logical_correction_of_source_failure",
            "semantic_rationale": (
                "The source evidence names the wrong return value and the required "
                "replacement for the same default call."
            ),
            "semantic_basis": {
                "kind": "source_atom_quote",
                "atom_id": "atom:original",
                "field_path": "$.text",
                "exact_quote": "core.run returns bad; the required result is fixed",
            },
        },
    }
    stderr_path = tmp_path / "baseline-stderr.txt"
    stderr_path.write_text(
        f'Traceback (most recent call last):\n  File "{harness}", line 5\nAssertionError\n',
        encoding="utf-8",
    )
    replay = {
        "experiment_id": "exp-original",
        "executed_argv": ["python", ".usertest_research/repro.py"],
        "command_authorization": mod._command_authorization_receipt(
            {
                "authorization_kind": "standard_test_or_research_harness",
                "executed_argv_sha256": mod._canonical_json_sha256(
                    ["python", ".usertest_research/repro.py"]
                ),
                "shell": False,
                "workspace_confined": True,
                "artifact_id": "artifact:retained-repro",
                "entrypoint_path": ".usertest_research/repro.py",
                "entrypoint_sha256": sha256(harness.read_bytes()).hexdigest(),
            }
        ),
        "exit_code": 1,
        "workspace_dir": str(research),
        "stderr_path": str(stderr_path),
        "stdout_sha256": "a" * 64,
        "stderr_sha256": sha256(stderr_path.read_bytes()).hexdigest(),
        "assertion_passed": True,
    }
    mechanism = {
        "mechanism_evidence_id": "mechanism_evidence:harness",
        "hypothesis_id": "h-harness",
        "evidence_type": "temporary_harness",
        "experiment_ids": ["exp-original"],
        "origin_atom_ids": ["atom:original"],
        "mechanism_symbols": ["core.run"],
        "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
        "harness_path": ".usertest_research/repro.py",
        "adversarial_effect": "supports_selection",
    }
    selected_binding = _selected_mechanism_binding(
        hypothesis_id="h-harness",
        mechanism_evidence=[mechanism],
        causal_root_evidence_ids=["mechanism_evidence:harness"],
    )
    errors: list[str] = []
    atom = {
        "atom_id": "atom:original",
        "text": "core.run returns bad; the required result is fixed",
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    assignment = {
        "atom_receipts": [
            {
                "atom_id": "atom:original",
                "atom_sha256": mod._canonical_json_sha256(atom),
                "atom_snapshot": atom,
            }
        ]
    }
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "usertest" / "research-run"
    run_dir.mkdir(parents=True)
    oracles = mod._outcome_oracle_receipts(
        {
            "case_id": "case:harness",
            "repo_revision": researched,
            "experiments": [experiment],
            "root_cause_hypotheses": [
                {
                    "hypothesis_id": "h-harness",
                    "mechanism_symbols": ["core.run"],
                }
            ],
        },
        clean_replays={"exp-original": replay},
        mechanism_evidence=[mechanism],
        **selected_binding,
        control_verifications=[],
        falsification_interventions=[],
        inspected_file_receipts=[],
        inspected_symbol_receipts=[],
        evidence_assignment=assignment,
        atom_bindings=[],
        planning_workspace=planning,
        research_workspace=research,
        overlay_manifest=overlay,
        run_dir=run_dir,
        repo_revision=researched,
        errors=errors,
    )
    assert errors == []
    assert len(oracles) == 1
    oracle = oracles[0]
    assert oracle["kind"] == "staged_replay"
    assert oracle["proof_scope"] == "behavioral"
    assert (
        stage_contracts._validate_outcome_oracles(
            {
                "case_id": "case:harness",
                "repo_revision": researched,
                "evidence_assignment": assignment,
                "root_cause_hypotheses": [
                    {
                        "hypothesis_id": "h-harness",
                        "mechanism_symbols": ["core.run"],
                    }
                ],
            },
            {
                "outcome_oracles": [oracle],
                "experiments": [experiment],
                "mechanism_evidence": [mechanism],
                **selected_binding,
                "control_verifications": [],
                "atom_bindings": [],
            },
            pid="problem:harness",
        )
        == []
    )
    (source / "src" / "core.py").write_text(
        "def run():\n    return 'fixed'\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], cwd=source)
    _git(["commit", "-m", "fix"], cwd=source)
    merged = _git(["rev-parse", "HEAD"], cwd=source)
    role = _role_contract(
        {
            "description": "Replay the retained original scenario.",
            "research_experiment_id": "exp-original",
            "commands": [],
            "predicates": [
                {"type": "command_exit_code", "command_index": 0, "equals": 0},
            ],
            "oracle": oracle,
            "required_proof_scope": "behavioral",
        }
    )
    output = runs_root / "usertest_implement" / "outcome-role.json"
    artifact = run_outcome_evidence_role(
        workspace=source,
        output_path=output,
        role="original_scenario",
        role_contract=role,
        case_id="case:harness",
        plan_revision_id="planrev:harness",
        merged_commit=merged,
        verification_contract_sha256="c" * 64,
        target_contract_sha256="d" * 64,
        verified_implementation_head=merged,
        timeout_seconds=None,
        trusted_oracle_assets_root=runs_root,
    )
    assert artifact["passed"] is True
    assert artifact["timeout_seconds"] is None
    assert artifact["commands"][0]["shell"] is False
    assert artifact["commands"][0]["argv"] == [
        "python",
        ".usertest_research/repro.py",
    ]
    assert artifact["oracle_materialization"]["cleanup_confirmed"] is True
    assert artifact["oracle_materialization"]["final_status_clean"] is True
    assert artifact["oracle_materialization"]["final_head"] == merged
    assert not (source / ".usertest_research").exists()
    assert _git(["status", "--porcelain"], cwd=source) == ""

    asset = oracle["asset"]
    assert isinstance(asset, dict)
    asset_file = runs_root / str(asset["runs_relative_path"]) / ".usertest_research" / "repro.py"
    asset_file.write_text(asset_file.read_text(encoding="utf-8") + "# tamper\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outcome_oracle_asset_hash_mismatch"):
        run_outcome_evidence_role(
            workspace=source,
            output_path=output.with_name("tampered.json"),
            role="original_scenario",
            role_contract=role,
            case_id="case:harness",
            plan_revision_id="planrev:harness",
            merged_commit=merged,
            verification_contract_sha256="c" * 64,
            target_contract_sha256="d" * 64,
            verified_implementation_head=merged,
            timeout_seconds=None,
            trusted_oracle_assets_root=runs_root,
        )
    assert not (source / ".usertest_research").exists()
    assert _git(["status", "--porcelain"], cwd=source) == ""


def test_independent_fail_first_harness_binds_selected_mechanism_without_selecting_it(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    (research / "src").mkdir(parents=True)
    (research / "src" / "core.py").write_text(
        "def evaluate():\n    return [1, 2, 3, 4]\n",
        encoding="utf-8",
    )
    harness_path = ".usertest_research/test_outcome.py"
    harness = research / harness_path
    harness.parent.mkdir()
    harness.write_text(
        "from src.core import evaluate\n\n"
        "def test_outcome():\n"
        "    result = evaluate()\n"
        "    bounded = len(result) <= 3\n"
        "    outcome = bounded\n"
        "    assert outcome is True\n",
        encoding="utf-8",
    )
    stdout_path = tmp_path / "stdout.txt"
    stderr_path = tmp_path / "stderr.txt"
    node_id = f"{harness_path}::test_outcome"
    stdout_path.write_text(
        f"collected 1 item\nFAILED {node_id} - assert False\n"
        f"{harness_path}:7: AssertionError\n1 failed in 0.01s\n",
        encoding="utf-8",
    )
    stderr_path.write_text("", encoding="utf-8")
    argv = ["python", "-m", "pytest", node_id]
    authorization = mod._command_authorization_receipt(
        {
            "authorization_kind": "standard_test_or_research_harness",
            "executed_argv_sha256": mod._canonical_json_sha256(argv),
            "shell": False,
            "workspace_confined": True,
            "artifact_id": "artifact:outcome-harness",
            "entrypoint_path": harness_path,
            "entrypoint_sha256": sha256(harness.read_bytes()).hexdigest(),
        }
    )
    setup = mod._replay_setup_receipt(
        environment_overrides={},
        disposable_state_paths=[],
    )
    revision = "a" * 40
    observable_assertion = {
        "source": "exit_code",
        "operator": "equals",
        "expected": 1,
    }
    experiment = {
        "experiment_id": "exp-outcome",
        "scenario_kind": "faithful_replay",
        "addresses_atom_ids": ["atom:retention"],
        "command": f"python -m pytest {node_id}",
        "result": "The production-derived boundedness property is false.",
        "outcome": "supports",
        "exit_code": 1,
        "observable_assertion": observable_assertion,
        "artifact_refs": ["artifact:outcome-harness"],
        "positive_outcome_contract": {
            "contract_kind": "retained_harness_semantic_assertion",
            "binds_hypothesis_id": "h-retention",
            "expected_value": True,
            "semantic_relation": "required_operational_property",
            "semantic_rationale": (
                "The source describes bounded retention as the required operational property."
            ),
            "semantic_basis": {
                "kind": "authenticated_semantic_citation",
                "atom_id": "atom:retention",
                "field_path": "$.text",
            },
        },
    }
    replay = {
        "experiment_id": "exp-outcome",
        "scenario_kind": "faithful_replay",
        "addresses_atom_ids": ["atom:retention"],
        "command": experiment["command"],
        "executed_argv": argv,
        "command_authorization": authorization,
        "declared_result": experiment["result"],
        "outcome": "supports",
        "exit_code": 1,
        "workspace_dir": str(research),
        "workspace_head": revision,
        "undeclared_post_replay_mutations": [],
        "replay_setup_receipt": setup,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_sha256": sha256(stdout_path.read_bytes()).hexdigest(),
        "stderr_sha256": sha256(stderr_path.read_bytes()).hexdigest(),
        "observable_assertion": observable_assertion,
        "assertion_passed": True,
        "artifact_refs": ["artifact:outcome-harness"],
    }
    mechanism = {
        "mechanism_evidence_id": "mechanism_evidence:selected-before-outcome",
        "hypothesis_id": "h-retention",
        "evidence_type": "controlled_scenario",
        "experiment_ids": ["exp-causal-baseline"],
        "origin_atom_ids": ["atom:retention"],
        "mechanism_symbols": ["src.core.evaluate"],
        "code_paths": [{"symbol": "src.core.evaluate", "path": "src/core.py"}],
        "causal_root_bindings": [{"kind": "origin_symptom_observation"}],
        "adversarial_effect": "supports_selection",
    }
    selected = _selected_mechanism_binding(
        hypothesis_id="h-retention",
        mechanism_evidence=[mechanism],
        causal_root_evidence_ids=["mechanism_evidence:selected-before-outcome"],
    )
    atom = {
        "atom_id": "atom:retention",
        "text": "Managed maintenance-image retention must remain bounded after a burst.",
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    assignment = {
        "atom_receipts": [
            {
                "atom_id": "atom:retention",
                "atom_sha256": mod._canonical_json_sha256(atom),
                "atom_snapshot": atom,
            }
        ]
    }
    overlay = {
        key: value
        for key, value in mod._workspace_manifest(research).items()
        if key.startswith(".usertest_research/")
    }
    run_dir = tmp_path / "runs" / "usertest" / "research-run"
    run_dir.mkdir(parents=True)
    errors: list[str] = []
    oracles = mod._outcome_oracle_receipts(
        {
            "case_id": "case:retention",
            "repo_revision": revision,
            "experiments": [experiment],
            "root_cause_hypotheses": [
                {
                    "hypothesis_id": "h-retention",
                    "mechanism_symbols": ["src.core.evaluate"],
                }
            ],
        },
        clean_replays={"exp-outcome": replay},
        mechanism_evidence=[mechanism],
        **selected,
        control_verifications=[],
        falsification_interventions=[],
        inspected_file_receipts=[],
        inspected_symbol_receipts=[],
        evidence_assignment=assignment,
        atom_bindings=[],
        planning_workspace=research,
        research_workspace=research,
        overlay_manifest=overlay,
        run_dir=run_dir,
        repo_revision=revision,
        errors=errors,
    )

    assert errors == []
    assert len(oracles) == 1
    oracle = oracles[0]
    binding = oracle["outcome_mechanism_binding"]
    assert binding["research_experiment_id"] == "exp-outcome"
    assert binding["selected_mechanism_evidence_ids"] == [
        "mechanism_evidence:selected-before-outcome"
    ]
    assert "exp-outcome" not in mechanism["experiment_ids"]
    assert binding["assertion_receipts"][0]["line"] == 7
    assert binding["dataflow_receipt"]["assignment_chain"][-1]["local"] == "outcome"
    contract = oracle["positive_outcome_contracts"][0]
    assert contract["kind"] == "retained_research_harness_assertion"
    assert contract["semantic_review_required"] is True
    assert contract["semantic_basis"]["provenance"]["basis_kind"] == (
        "authenticated_semantic_citation"
    )
    assert (
        stage_contracts._validate_outcome_oracles(
            {
                "case_id": "case:retention",
                "repo_revision": revision,
                "evidence_assignment": assignment,
            },
            {
                "outcome_oracles": [oracle],
                "experiments": [replay],
                "mechanism_evidence": [mechanism],
                **selected,
                "inspected_files": [],
                "inspected_symbols": [],
                "falsification_interventions": [],
                "proof_adapter_receipts": [],
                "atom_bindings": [],
            },
            pid="problem:retention",
        )
        == []
    )
    tampered = json.loads(json.dumps(oracle))
    tampered_binding = tampered["outcome_mechanism_binding"]
    tampered_binding["hypothesis_id"] = "h-unrelated"
    tampered_binding["outcome_mechanism_binding_id"] = (
        "outcome_mechanism_binding:"
        + mod._canonical_json_sha256(
            {
                key: value
                for key, value in tampered_binding.items()
                if key != "outcome_mechanism_binding_id"
            }
        )
    )
    tampered_contract = tampered["positive_outcome_contracts"][0]
    tampered_contract["outcome_mechanism_binding_id"] = tampered_binding[
        "outcome_mechanism_binding_id"
    ]
    tampered_contract["positive_outcome_contract_id"] = (
        "positive_outcome_contract:"
        + mod._canonical_json_sha256(
            {
                key: value
                for key, value in tampered_contract.items()
                if key != "positive_outcome_contract_id"
            }
        )
    )
    tampered["outcome_oracle_id"] = "outcome_oracle:" + mod._canonical_json_sha256(
        {key: value for key, value in tampered.items() if key != "outcome_oracle_id"}
    )
    tamper_errors = stage_contracts._validate_outcome_oracles(
        {
            "case_id": "case:retention",
            "repo_revision": revision,
            "evidence_assignment": assignment,
        },
        {
            "outcome_oracles": [tampered],
            "experiments": [replay],
            "mechanism_evidence": [mechanism],
            **selected,
            "inspected_files": [],
            "inspected_symbols": [],
            "falsification_interventions": [],
            "proof_adapter_receipts": [],
            "atom_bindings": [],
        },
        pid="problem:retention",
    )
    assert "research_outcome_oracle_invalid: problem:retention: 0" in tamper_errors


@pytest.mark.parametrize(
    "body",
    [
        (
            "from src.core import evaluate\n\n"
            "def test_outcome():\n"
            "    result = evaluate()\n"
            "    result = []\n"
            "    outcome = len(result) <= 3\n"
            "    assert outcome is True\n"
        ),
        (
            "from src.core import evaluate\n\n"
            "def test_outcome():\n"
            "    result = evaluate()\n"
            "    outcome = (len(result) <= 3) or True\n"
            "    assert outcome is True\n"
        ),
        (
            "from src.core import evaluate\n\n"
            "def test_outcome():\n"
            "    result = evaluate()\n"
            "    outcome = (len(result) * 0) == 1\n"
            "    assert outcome is True\n"
        ),
        (
            "import src.core as core\n\n"
            "def test_outcome(monkeypatch):\n"
            "    monkeypatch.setattr(core, 'evaluate', lambda: [])\n"
            "    result = core.evaluate()\n"
            "    outcome = len(result) <= 3\n"
            "    assert outcome is True\n"
        ),
        (
            "from src.core import evaluate\n\n"
            "def test_outcome():\n"
            "    result = evaluate()\n"
            "    outcome = result == result\n"
            "    assert outcome is True\n"
        ),
        (
            "from src.core import evaluate\n\n"
            "def test_outcome():\n"
            "    result = evaluate()\n"
            "    if result:\n"
            "        outcome = False\n"
            "    assert outcome is True\n"
        ),
        (
            "from src.core import evaluate\n\n"
            "def test_outcome():\n"
            "    result = evaluate()\n"
            "    return\n"
            "    assert (len(result) <= 3) is True\n"
        ),
        (
            "import pytest\n"
            "from src.core import evaluate\n\n"
            "def test_outcome():\n"
            "    result = evaluate()\n"
            "    pytest.skip('not today')\n"
            "    assert (len(result) <= 3) is True\n"
        ),
        (
            "import pytest\n"
            "from src.core import evaluate\n\n"
            "@pytest.mark.xfail\n"
            "def test_outcome():\n"
            "    result = evaluate()\n"
            "    assert (len(result) <= 3) is True\n"
        ),
    ],
)
def test_outcome_binding_rejects_non_immutable_or_constant_dominated_flow(
    tmp_path: Path,
    body: str,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "core.py").write_text(
        "def evaluate():\n    return [1, 2, 3, 4]\n",
        encoding="utf-8",
    )
    harness_path = ".usertest_research/test_outcome.py"
    harness = workspace / harness_path
    harness.parent.mkdir()
    harness.write_text(body, encoding="utf-8")
    stderr = tmp_path / "stderr.txt"
    assertion_line = len(body.splitlines())
    stderr.write_text(
        "",
        encoding="utf-8",
    )
    stdout = tmp_path / "stdout.txt"
    node_id = f"{harness_path}::test_outcome"
    stdout.write_text(
        f"FAILED {node_id} - assert False\n"
        f"{harness_path}:{assertion_line}: AssertionError\n1 failed in 0.01s\n",
        encoding="utf-8",
    )
    replay = {
        "executed_argv": ["python", "-m", "pytest", node_id],
        "workspace_dir": str(workspace),
        "stdout_path": str(stdout),
        "stderr_path": str(stderr),
    }

    assert (
        mod._restricted_outcome_assertion_dataflow(
            experiment={},
            replay=replay,
            expected_value=True,
            mechanism_symbols=["src.core.evaluate"],
            symbol_paths={"src.core.evaluate": "src/core.py"},
        )
        is None
    )


def test_config_oracle_closes_config_state_without_claiming_behavior(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "configs").mkdir()
    config = source / "configs" / "app.yaml"
    config.write_text("tool:\n  mode: legacy\n", encoding="utf-8")
    _git(["init"], cwd=source)
    _git(["config", "user.email", "tests@example.invalid"], cwd=source)
    _git(["config", "user.name", "Tests"], cwd=source)
    _git(["add", "-A"], cwd=source)
    _git(["commit", "-m", "legacy config"], cwd=source)
    researched = _git(["rev-parse", "HEAD"], cwd=source)
    planning = tmp_path / "planning"
    _git(["clone", str(source), str(planning)], cwd=tmp_path)
    experiment = {
        "experiment_id": "exp-config",
        "scenario_kind": "static_trace",
        "addresses_atom_ids": ["atom:config"],
        "command": "python tools/read_config.py",
        "outcome": "supports",
        "exit_code": 0,
        "observable_assertion": {
            "source": "stdout",
            "operator": "contains",
            "expected": "legacy",
        },
        "static_trace": {
            "deterministic": True,
            "environment_dependencies": [],
            "code_path": [
                {
                    "symbol": "config:/tool/mode",
                    "path": "configs/app.yaml",
                }
            ],
        },
        "origin_evidence_bindings": [
            {
                "role": "expected_behavior",
                "atom_id": "atom:config",
                "field_path": "$.expected_mode",
                "value": "safe",
                "value_sha256": mod._canonical_json_sha256("safe"),
            }
        ],
        "positive_outcome_contract": {
            "contract_kind": "origin_atom_exact_value",
            "atom_id": "atom:config",
            "field_path": "$.expected_mode",
            "postcondition": {
                "type": "config_state_equals",
                "mechanism_symbol": "config:/tool/mode",
                "exists": True,
                "equals": "safe",
            },
        },
    }
    replay = {
        "experiment_id": "exp-config",
        "executed_argv": ["python", "tools/read_config.py"],
        "command_authorization": {"shell": False},
        "exit_code": 0,
        "stdout_sha256": "e" * 64,
        "stderr_sha256": "f" * 64,
        "assertion_passed": True,
    }
    mechanism = {
        "hypothesis_id": "h-config",
        "evidence_type": "static_trace",
        "experiment_ids": ["exp-config"],
        "origin_atom_ids": ["atom:config"],
        "mechanism_symbols": ["config:/tool/mode"],
        "code_paths": [{"symbol": "config:/tool/mode", "path": "configs/app.yaml"}],
        "mechanism_link": {
            "verification_method": "runner_deterministic_static_trace_v1",
            "entrypoint": "config:/tool/mode",
        },
        "causal_root_bindings": [
            {
                "kind": "origin_symptom_observation",
                "root_mechanism_symbol": "config:/tool/mode",
            }
        ],
        "adversarial_effect": "supports_selection",
    }
    mechanism["mechanism_evidence_id"] = "mechanism_evidence:" + mod._canonical_json_sha256(
        mechanism
    )
    mechanism_evidence_id = str(mechanism["mechanism_evidence_id"])
    selected_binding = _selected_mechanism_binding(
        hypothesis_id="h-config",
        mechanism_evidence=[mechanism],
        causal_root_evidence_ids=[mechanism_evidence_id],
    )
    dossier = {
        "case_id": "case:config",
        "repo_revision": researched,
        "writes_used": False,
        "writes_purpose": ["none"],
        "experiments": [experiment],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h-config",
                "statement": "The exact config pointer retains legacy mode.",
                "mechanism_symbols": ["config:/tool/mode"],
                "supporting_evidence": ["exp-config"],
                "counterevidence": [],
                "falsification_attempts": [],
                "disposition": "primary",
            }
        ],
        "material_unknowns": [],
    }
    closure = mod._deterministic_mechanism_closure_receipts(
        dossier,
        clean_replays={"exp-config": replay},
        symbol_receipts=[{"symbol": "config:/tool/mode", "path": "configs/app.yaml"}],
        mechanism_evidence=[mechanism],
    )
    assert len(closure) == 1
    assert closure[0]["closure_basis"] == "rooted_connected_support_component"
    assert closure[0]["support_experiment_ids"] == ["exp-config"]
    assert closure[0]["verification_method"] == ("runner_deterministic_mechanism_closure_v2")
    assert dossier["writes_used"] is False
    assert dossier["writes_purpose"] == ["none"]
    assert _git(["status", "--porcelain"], cwd=source) == ""
    assert _git(["status", "--porcelain"], cwd=planning) == ""
    errors: list[str] = []
    atom_snapshot = {
        "expected_mode": "safe",
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    atom_receipt = {
        "atom_id": "atom:config",
        "atom_snapshot": atom_snapshot,
        "atom_sha256": mod._canonical_json_sha256(atom_snapshot),
    }
    assignment = {"atom_receipts": [atom_receipt]}
    atom_binding = {
        "experiment_id": "exp-config",
        "atom_id": "atom:config",
        "binding_role": "expected_behavior",
        "origin_atom_field_path": "$.expected_mode",
    }
    oracle = mod._outcome_oracle_receipts(
        dossier,
        clean_replays={"exp-config": replay},
        mechanism_evidence=[mechanism],
        **selected_binding,
        control_verifications=[],
        falsification_interventions=[],
        inspected_file_receipts=[],
        inspected_symbol_receipts=[],
        evidence_assignment=assignment,
        atom_bindings=[atom_binding],
        planning_workspace=planning,
        research_workspace=None,
        overlay_manifest={},
        run_dir=tmp_path / "runs" / "usertest" / "config-run",
        repo_revision=researched,
        errors=errors,
    )[0]
    assert errors == []
    target = oracle["state_targets"][0]
    assert target["baseline_value"] == "legacy"
    assert oracle["proof_scope"] == "configuration_state"
    assert (
        stage_contracts._validate_outcome_oracles(
            {
                "case_id": "case:config",
                "repo_revision": researched,
                "evidence_assignment": assignment,
                "root_cause_hypotheses": dossier["root_cause_hypotheses"],
            },
            {
                "outcome_oracles": [oracle],
                "experiments": [experiment],
                "mechanism_evidence": [mechanism],
                **selected_binding,
                "control_verifications": [],
                "atom_bindings": [atom_binding],
            },
            pid="problem:config",
        )
        == []
    )
    research = {
        "evidence_verification": {
            "status": "verified",
            "mechanism_evidence": [mechanism],
            **selected_binding,
            "outcome_oracles": [oracle],
        }
    }
    plan = bind_plan_outcome_oracle(
        {
            "before_after_reproduction": {
                "research_experiment_id": "exp-config",
                "after_change": {
                    "expected_exit_code": 0,
                    "state_expectations": [
                        {
                            "target_id": target["target_id"],
                            "exists": True,
                            "equals": "safe",
                        }
                    ],
                },
            },
            "outcome_verification_roles": {
                "original_scenario": {"description": "Verify config state."},
                "live": None,
                "mitigation_effect": None,
                "recurrence": None,
            },
        },
        research=research,
    )
    role = _role_contract(dict(plan["outcome_verification_roles"]["original_scenario"]))
    config.write_text("tool:\n  mode: safe\n", encoding="utf-8")
    _git(["add", "-A"], cwd=source)
    _git(["commit", "-m", "safe config"], cwd=source)
    merged = _git(["rev-parse", "HEAD"], cwd=source)
    artifact = run_outcome_evidence_role(
        workspace=source,
        output_path=tmp_path / "runs" / "usertest_implement" / "config-role.json",
        role="original_scenario",
        role_contract=role,
        case_id="case:config",
        plan_revision_id="planrev:config",
        merged_commit=merged,
        verification_contract_sha256="1" * 64,
        target_contract_sha256="2" * 64,
        verified_implementation_head=merged,
        timeout_seconds=None,
    )
    assert artifact["passed"] is True
    assert artifact["commands"] == []
    assert artifact["proof_scope"] == "configuration_state"
    assert artifact["oracle_states"][0]["value"] == "safe"
    assert _git(["status", "--porcelain"], cwd=source) == ""

    forged_payload = {key: value for key, value in role.items() if key != "role_contract_sha256"}
    forged_payload["required_proof_scope"] = "behavioral"
    forged = _role_contract(forged_payload)
    with pytest.raises(ValueError, match="outcome_role_oracle_scope_mismatch"):
        run_outcome_evidence_role(
            workspace=source,
            output_path=tmp_path / "runs" / "usertest_implement" / "forged.json",
            role="original_scenario",
            role_contract=forged,
            case_id="case:config",
            plan_revision_id="planrev:config",
            merged_commit=merged,
            verification_contract_sha256="1" * 64,
            target_contract_sha256="2" * 64,
            verified_implementation_head=merged,
            timeout_seconds=None,
        )


def test_outcome_oracles_ignore_support_from_nonprimary_hypothesis(tmp_path: Path) -> None:
    def experiment(experiment_id: str) -> dict[str, object]:
        return {
            "experiment_id": experiment_id,
            "scenario_kind": "faithful_replay",
            "addresses_atom_ids": ["atom:one"],
            "command": f"python tools/{experiment_id}.py",
            "outcome": "supports",
            "exit_code": 1,
            "observable_assertion": {
                "source": "exit_code",
                "operator": "equals",
                "expected": 1,
            },
            "artifact_refs": ["artifact:one"],
        }

    primary_experiment = experiment("primary-support")
    alternative_experiment = experiment("alternative-support")

    def replay(experiment_id: str) -> dict[str, object]:
        argv = ["python", f"tools/{experiment_id}.py"]
        return {
            "executed_argv": argv,
            "command_authorization": mod._command_authorization_receipt(
                {
                    "authorization_kind": ("declared_inspected_repository_entrypoint"),
                    "executed_argv_sha256": mod._canonical_json_sha256(argv),
                    "shell": False,
                    "workspace_confined": True,
                    "entrypoint_path": f"tools/{experiment_id}.py",
                    "entrypoint_sha256": "c" * 64,
                    "entrypoint_git_blob_sha": "d" * 40,
                }
            ),
            "assertion_passed": True,
            "exit_code": 1,
            "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64,
        }

    primary_evidence = {
        "mechanism_evidence_id": "mechanism_evidence:primary",
        "hypothesis_id": "h-primary",
        "adversarial_effect": "supports_selection",
        "experiment_ids": ["primary-support"],
        "origin_atom_ids": ["atom:one"],
        "mechanism_symbols": ["core.primary"],
        "code_paths": [{"symbol": "core.primary", "path": "src/core.py"}],
    }
    alternative_evidence = {
        "mechanism_evidence_id": "mechanism_evidence:alternative",
        "hypothesis_id": "h-alternative",
        "adversarial_effect": "supports_selection",
        "experiment_ids": ["alternative-support"],
        "origin_atom_ids": ["atom:one"],
        "mechanism_symbols": ["core.alternative"],
        "code_paths": [{"symbol": "core.alternative", "path": "src/core.py"}],
    }
    selected_binding = _selected_mechanism_binding(
        hypothesis_id="h-primary",
        mechanism_evidence=[primary_evidence],
        causal_root_evidence_ids=["mechanism_evidence:primary"],
    )
    errors: list[str] = []

    oracles = mod._outcome_oracle_receipts(
        {
            "case_id": "case:primary",
            "root_cause_hypotheses": [
                {"hypothesis_id": "h-primary"},
                {"hypothesis_id": "h-alternative"},
            ],
            "experiments": [primary_experiment, alternative_experiment],
        },
        clean_replays={
            "primary-support": replay("primary-support"),
            "alternative-support": replay("alternative-support"),
        },
        mechanism_evidence=[primary_evidence, alternative_evidence],
        **selected_binding,
        control_verifications=[],
        falsification_interventions=[],
        inspected_file_receipts=[],
        inspected_symbol_receipts=[],
        evidence_assignment={},
        atom_bindings=[],
        planning_workspace=None,
        research_workspace=None,
        overlay_manifest={},
        run_dir=tmp_path / "runs" / "usertest" / "primary-only",
        repo_revision="a" * 40,
        errors=errors,
    )

    assert errors == []
    assert [oracle["research_experiment_id"] for oracle in oracles] == ["primary-support"]
    assert oracles[0]["primary_hypothesis_id"] == "h-primary"
    assert oracles[0]["mechanism_evidence_ids"] == ["mechanism_evidence:primary"]


def test_verified_mechanism_identity_is_stable_across_case_provenance() -> None:
    def projection(
        hypothesis_id: str,
        *,
        evidence_id: str,
        control_id: str,
        probe_slot: str,
    ) -> tuple[dict[str, object] | None, str | None, dict[str, object] | None, str | None]:
        return mod._verified_mechanism_projection(
            {
                "root_cause_hypotheses": [
                    {
                        "hypothesis_id": hypothesis_id,
                        "statement": f"Case-specific prose for {hypothesis_id}",
                        "mechanism_symbols": ["router.route"],
                    }
                ]
            },
            mechanism_evidence=[
                {
                    "hypothesis_id": hypothesis_id,
                    "mechanism_symbols": ["router.route"],
                    "mechanism_evidence_id": evidence_id,
                    "code_paths": [{"symbol": "router.route", "path": "src/router.py"}],
                    "adversarial_effect": "supports_selection",
                    "origin_symptom_bindings": [
                        {
                            "experiment_id": "support",
                            "atom_id": "atom:one",
                            "match_kind": "command_and_atom_evidence_symptom",
                            "origin_atom_sha256": "a" * 64,
                        }
                    ],
                    "origin_atom_ids": ["atom:one"],
                    "experiment_ids": ["support"],
                    "mechanism_link": {
                        "entrypoint": "router.route",
                        "verification_method": "runner_exception_symbol_trace_v1",
                    },
                }
            ],
            control_verifications=[
                {
                    "hypothesis_id": hypothesis_id,
                    "mechanism_symbols": ["router.route"],
                    "control_verification_id": control_id,
                    "controlled_input_difference": {
                        "verification_method": ("python_ast_explicit_argument_delta_v1"),
                        "difference": {
                            "mechanism_symbol": "router.route",
                            "slot": probe_slot,
                        },
                    },
                }
            ],
            falsification_interventions=[],
            deterministic_closures=[],
        )

    first = projection(
        "hypothesis:case-a",
        evidence_id="mechanism_evidence:case-a",
        control_id="control_verification:case-a",
        probe_slot="keyword:policy",
    )
    second = projection(
        "hypothesis:case-b",
        evidence_id="mechanism_evidence:case-b",
        control_id="control_verification:case-b",
        probe_slot="keyword:fixture",
    )

    assert (
        first[0]
        == second[0]
        == {
            "schema_version": 3,
            "mechanism_symbols": ["router.route"],
            "code_paths": [{"symbol": "router.route", "path": "src/router.py"}],
        }
    )
    assert first[1] == second[1]
    assert first[2] != second[2]
    assert first[3] != second[3]


def test_persisted_origin_attachment_receipt_revalidates_chunks_and_reads(
    tmp_path: Path,
) -> None:
    origin_run = tmp_path / "runs" / "origin"
    origin_run.mkdir(parents=True)
    signature = "ONLY_MIDDLE_RESEARCH_SIGNATURE"
    artifact = origin_run / "agent_stderr.txt"
    artifact.write_text(("prefix\n" * 2_000) + signature + ("\nsuffix" * 2_000))
    workspace = tmp_path / "research-workspace"
    manifest = materialize_origin_attachments(
        atoms=[
            {
                "atom_id": "atom:origin",
                "run_dir": str(origin_run),
                "attachments": [
                    {
                        "artifact_ref": {
                            "path": artifact.name,
                            "sha256": sha256(artifact.read_bytes()).hexdigest(),
                            "size_bytes": artifact.stat().st_size,
                        }
                    }
                ],
            }
        ],
        workspace_dir=workspace,
        source_root=tmp_path,
        relative_root=Path(".usertest_research") / "origin_evidence",
    )
    events: list[dict[str, object]] = []
    attestations: list[dict[str, object]] = []
    requirements = origin_attachment_requirements(manifest)
    for requirement in requirements:
        chunk = workspace / str(requirement["file"])
        event: dict[str, object] = {
            "ts": "2026-07-10T00:00:00Z",
            "type": "read_file",
            "data": {
                "path": str(requirement["file"]),
                "read_source": "tool",
                "source_exit_code": 0,
                **observed_read_attestation(
                    path=chunk,
                    observed_text=chunk.read_text(encoding="utf-8"),
                    source_exit_code=0,
                    allow_partial=False,
                ),
            },
        }
        events.append(event)
        attestations.append(
            {
                "artifact_sha256": requirement["artifact_sha256"],
                "file": requirement["file"],
                "file_sha256": requirement["sha256"],
                "file_size_bytes": requirement["size_bytes"],
                "read_event_index": len(events) - 1,
                "read_event_sha256": mod._canonical_json_sha256(event),
            }
        )
    assignment = {"origin_attachment_evidence": manifest}
    receipt = {
        "origin_attachment_evidence": manifest,
        "origin_attachment_read_attestations": attestations,
    }

    selective_dossier = {"artifact_refs": []}
    mandatory_requirements = [
        requirement
        for requirement in requirements
        if requirement.get("content_role")
        in {"assigned_evidence_index", "source_run_context_index"}
    ]
    selective_events: list[dict[str, object]] = []
    selective_attestations: list[dict[str, object]] = []
    for requirement in mandatory_requirements:
        requirement_index = requirements.index(requirement)
        event = events[requirement_index]
        selective_events.append(event)
        attestation = dict(attestations[requirement_index])
        attestation["read_event_index"] = len(selective_events) - 1
        selective_attestations.append(attestation)
    selective_receipt = {
        "origin_attachment_evidence": manifest,
        "origin_attachment_read_attestations": selective_attestations,
        "atom_bindings": [],
    }
    selective_receipt["origin_attachment_read_coverage"] = (
        mod.origin_attachment_read_scope(
            manifest,
            dossier=selective_dossier,
            verification=selective_receipt,
            observed_files=[
                str(requirement["file"]) for requirement in mandatory_requirements
            ],
        )
    )
    assert (
        mod._persisted_origin_attachment_errors(
            assignment=assignment,
            receipt=selective_receipt,
            research_workspace=workspace,
            persisted_events=selective_events,
            dossier=selective_dossier,
        )
        == []
    )
    selective_receipt["origin_attachment_read_coverage"][
        "unread_optional_file_count"
    ] += 1
    assert "research_origin_attachment_read_coverage_changed" in (
        mod._persisted_origin_attachment_errors(
            assignment=assignment,
            receipt=selective_receipt,
            research_workspace=workspace,
            persisted_events=selective_events,
            dossier=selective_dossier,
        )
    )
    selective_receipt["origin_attachment_read_coverage"] = (
        mod.origin_attachment_read_scope(
            manifest,
            dossier=selective_dossier,
            verification=selective_receipt,
            observed_files=[
                str(requirement["file"]) for requirement in mandatory_requirements
            ],
        )
    )

    assert (
        mod._persisted_origin_attachment_errors(
            assignment=assignment,
            receipt=receipt,
            research_workspace=workspace,
            persisted_events=events,
        )
        == []
    )

    middle_requirement = next(
        requirement
        for requirement in requirements
        if signature in (workspace / str(requirement["file"])).read_text(encoding="utf-8")
    )
    (workspace / str(middle_requirement["file"])).write_text("tampered\n")
    errors = mod._persisted_origin_attachment_errors(
        assignment=assignment,
        receipt=receipt,
        research_workspace=workspace,
        persisted_events=events,
    )
    assert any("origin_attachment_chunk_changed" in error for error in errors)
    selective_errors = mod._persisted_origin_attachment_errors(
        assignment=assignment,
        receipt=selective_receipt,
        research_workspace=workspace,
        persisted_events=selective_events,
        dossier=selective_dossier,
    )
    assert any("origin_attachment_chunk_changed" in error for error in selective_errors)


def test_runner_materialized_origin_evidence_is_not_misclassified_as_agent_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _baseline_repo(source)
    research = tmp_path / "research"
    baseline = tmp_path / "baseline"
    for destination in (research, baseline):
        subprocess.run(
            ["git", "clone", str(source), str(destination)],
            check=True,
            capture_output=True,
        )
    origin_run = tmp_path / "runs" / "origin"
    origin_run.mkdir(parents=True)
    artifact = origin_run / "agent_stderr.txt"
    artifact.write_text("retained failure\n")
    manifest = materialize_origin_attachments(
        atoms=[
            {
                "atom_id": "atom:origin",
                "run_dir": str(origin_run),
                "attachments": [
                    {
                        "artifact_ref": {
                            "path": artifact.name,
                            "sha256": sha256(artifact.read_bytes()).hexdigest(),
                        }
                    }
                ],
            }
        ],
        workspace_dir=research,
        source_root=tmp_path,
        relative_root=Path(".usertest_research") / "origin_evidence",
    )
    assert manifest["errors"] == []

    errors, overlay = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )

    assert errors == []
    assert overlay["research_overlay_paths"] == []
    assert overlay["runner_materialized_evidence_paths"]
    assert mod._verified_diff_classification("no_changes", overlay) == "no_changes"


def _baseline_repo_commit_existing(path: Path, message: str) -> str:
    _git(["init"], cwd=path)
    _git(["config", "user.email", "tests@example.invalid"], cwd=path)
    _git(["config", "user.name", "Tests"], cwd=path)
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-m", message], cwd=path)
    return _git(["rev-parse", "HEAD"], cwd=path)


def _causal_control_repo(path: Path) -> None:
    (path / "src").mkdir(parents=True)
    (path / "src" / "core.py").write_text(
        "def run(*, guarded=False, extra=False):\n"
        "    if not guarded:\n"
        "        raise RuntimeError('reported failure')\n"
        "    return True\n",
        encoding="utf-8",
    )
    (path / "tests").mkdir()
    (path / "tests" / "test_core.py").write_text(
        "from src.core import run\n\n"
        "def test_reported_failure():\n"
        "    run()\n\n"
        "def test_unrelated_same_file():\n"
        "    assert 2 + 2 == 4\n\n"
        "def test_shadowed_mechanism_name():\n"
        "    run = lambda: True\n"
        "    assert run() is True\n\n"
        "def test_guarded_control():\n"
        "    assert run(guarded=True) is True\n\n"
        "def test_same_input_control():\n"
        "    run()\n\n"
        "def test_two_input_control():\n"
        "    assert run(guarded=True, extra=True) is True\n",
        encoding="utf-8",
    )
    (path / "tests" / "test_other.py").write_text(
        "def test_unrelated_other_file():\n    assert 'ready'.upper() == 'READY'\n",
        encoding="utf-8",
    )
    _git(["init"], cwd=path)
    _git(["config", "user.email", "tests@example.invalid"], cwd=path)
    _git(["config", "user.name", "Tests"], cwd=path)
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-m", "causal control baseline"], cwd=path)


def _control_dossier(control_target: str) -> tuple[dict[str, object], dict[str, dict]]:
    support_command = "pytest -q tests/test_core.py::test_reported_failure"
    control_command = f"pytest -q {control_target}"
    dossier: dict[str, object] = {
        "experiments": [
            {
                "experiment_id": "support",
                "scenario_kind": "original_replay",
                "command": support_command,
                "outcome": "supports",
                "exit_code": 1,
                "addresses_atom_ids": ["atom:support"],
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
            },
            {
                "experiment_id": "control",
                "scenario_kind": "control",
                "command": control_command,
                "outcome": "refutes",
                "exit_code": 0,
                "addresses_atom_ids": ["atom:support"],
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 0,
                },
                "control_relationship": {
                    "supports_experiment_id": "support",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "guarded input",
                    "expected_difference": "guarded call succeeds",
                },
            },
        ],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "supporting_evidence": ["support"],
                "counterevidence": ["control"],
                "mechanism_symbols": ["core.run"],
            }
        ],
    }
    replays = {
        "support": {
            "executed_argv": mod._parse_replay_argv(support_command),
            "exit_code": 1,
            "assertion_passed": True,
            "stdout_sha256": "1" * 64,
            "stderr_sha256": "2" * 64,
        },
        "control": {
            "executed_argv": mod._parse_replay_argv(control_command),
            "exit_code": 0,
            "assertion_passed": True,
            "stdout_sha256": "3" * 64,
            "stderr_sha256": "4" * 64,
        },
    }
    return dossier, replays


def _attested_research_pytest_control(
    workspace: Path,
    *,
    helper_source: str | None = None,
    baseline_call: str = "_probe(fatal=True)",
    challenge_call: str = "_probe(fatal=False)",
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    production = workspace / "src" / "core.py"
    production.parent.mkdir(parents=True)
    production.write_text(
        "def classify(*, fatal, extra=False):\n"
        "    return 'fatal' if fatal else 'notice'\n",
        encoding="utf-8",
    )
    _baseline_repo_commit_existing(workspace, "add classifier")
    harness = workspace / ".usertest_research" / "test_probe.py"
    harness.parent.mkdir()
    helper = helper_source or (
        "def _probe(*, fatal):\n"
        "    return classify(fatal=fatal)\n"
    )
    harness.write_text(
        "from src.core import classify\n\n"
        f"{helper}\n"
        "def test_baseline():\n"
        f"    result = {baseline_call}\n"
        "    print(result)\n"
        "    assert result in {'fatal', 'notice', 'fixed'}\n\n"
        "def test_challenge():\n"
        f"    result = {challenge_call}\n"
        "    print(result)\n"
        "    assert result in {'fatal', 'notice', 'fixed'}\n",
        encoding="utf-8",
    )
    baseline_command = (
        "pytest -p no:cacheprovider "
        ".usertest_research/test_probe.py::test_baseline -q -s "
        "--junitxml .usertest_research/baseline.xml"
    )
    challenge_command = (
        "pytest -p no:cacheprovider "
        ".usertest_research/test_probe.py::test_challenge -q -s "
        "--junitxml .usertest_research/challenge.xml"
    )
    experiments: list[dict[str, object]] = [
        {
            "experiment_id": "support",
            "scenario_kind": "production_function_pytest_replay",
            "command": baseline_command,
            "outcome": "supports",
            "exit_code": 0,
            "addresses_atom_ids": ["atom:support"],
            "artifact_refs": ["artifact:test-probe"],
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": "fatal",
            },
            "repository_bindings": [
                {
                    "path": "src/core.py",
                    "relationship": "The retained harness directly calls this classifier.",
                }
            ],
        },
        {
            "experiment_id": "control",
            "scenario_kind": "control",
            "command": challenge_command,
            "outcome": "supports",
            "exit_code": 0,
            "addresses_atom_ids": ["atom:support"],
            "artifact_refs": ["artifact:test-probe"],
            "observable_assertion": {
                "source": "stdout",
                "operator": "not_contains",
                "expected": "fatal",
            },
            "repository_bindings": [
                {
                    "path": "src/core.py",
                    "relationship": "The retained harness directly calls this classifier.",
                }
            ],
            "control_relationship": {
                "supports_experiment_id": "support",
                "controlled_variable": "fatal classifier input",
                "expected_difference": "fatal changes to notice",
                "mechanism_symbols": ["core.classify"],
            },
        },
    ]
    dossier: dict[str, object] = {
        "inspected_files": ["src/core.py"],
        "artifact_refs": [
            {
                "artifact_id": "artifact:test-probe",
                "kind": "research_harness",
                "path": ".usertest_research/test_probe.py",
            }
        ],
        "experiments": experiments,
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "The fatal input controls the classifier result.",
                "supporting_evidence": ["support", "control"],
                "counterevidence": [],
                "mechanism_symbols": ["core.classify"],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:toggle-fatal",
                        "hypothesis_id": "h1",
                        "claim": "The fatal input controls the classifier result.",
                        "baseline_experiment_id": "support",
                        "challenge_experiment_id": "control",
                        "disproof_condition": {
                            "source": "stdout",
                            "operator": "contains",
                            "expected": "fatal",
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
    }
    replays: dict[str, dict[str, object]] = {}
    for experiment in experiments:
        command = str(experiment["command"])
        authorized = mod._authorized_replay_invocation(
            command=command,
            experiment=experiment,
            dossier=dossier,
            assignment={},
            workspace=workspace,
        )
        assert authorized is not None
        argv, authorization = authorized
        replays[str(experiment["experiment_id"])] = {
            "executed_argv": argv,
            "command_authorization": authorization,
            "workspace_dir": str(workspace),
            "command": command,
            "declared_result": experiment.get("result"),
            "exit_code": 0,
            "outcome": experiment.get("outcome"),
            "scenario_kind": experiment.get("scenario_kind"),
            "observable_assertion": experiment.get("observable_assertion"),
            "assertion_passed": True,
            "stdout_sha256": "1" * 64,
            "stderr_sha256": "2" * 64,
        }
    return dossier, replays


def test_complete_manifest_detects_staged_hidden_and_untracked_production_edits(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    _baseline_repo(baseline)
    research = tmp_path / "research"
    _git(["clone", str(baseline), str(research)], cwd=tmp_path)
    (research / "src" / "core.py").write_text(
        "def run():\n    return False\n",
        encoding="utf-8",
    )
    _git(["add", "src/core.py"], cwd=research)
    _git(["update-index", "--assume-unchanged", "src/core.py"], cwd=research)
    (research / "tests" / "fake_test.py").write_text(
        "def test_fake():\n    assert True\n",
        encoding="utf-8",
    )
    (research / ".usertest_research").mkdir()
    (research / ".usertest_research" / "notes.txt").write_text(
        "allowed research overlay\n",
        encoding="utf-8",
    )

    errors, receipt = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )

    assert "baseline_file_changed:src/core.py" in errors
    assert "untracked_workspace_file:tests/fake_test.py" in errors
    assert "git_index_changed" in errors
    assert receipt["git_index_changed"] is True
    assert receipt["research_overlay_paths"] == [".usertest_research/notes.txt"]


def test_workspace_overlay_records_but_does_not_block_generated_virtualenv(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    _baseline_repo(baseline)
    research = tmp_path / "research"
    _git(["clone", str(baseline), str(research)], cwd=tmp_path)
    environment_root = research / "packages" / "runner_core" / ".venv"
    (environment_root / "Scripts").mkdir(parents=True)
    (environment_root / "pyvenv.cfg").write_text("home = python\n", encoding="utf-8")
    (environment_root / "Scripts" / "python.exe").write_bytes(b"tool environment")
    (environment_root.parent / ".pdm-python").write_text(".venv\n", encoding="utf-8")
    (research / "tests" / "unexpected.py").write_text("changed\n", encoding="utf-8")

    errors, receipt = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )

    assert "untracked_workspace_file:tests/unexpected.py" in errors
    assert not any("packages/runner_core/.venv" in error for error in errors)
    assert "untracked_workspace_file:packages/runner_core/.pdm-python" not in errors
    assert receipt["ignored_tool_environment_roots"] == ["packages/runner_core/.venv"]
    assert receipt["ignored_tool_environment_paths"] == [
        "packages/runner_core/.pdm-python",
        "packages/runner_core/.venv/Scripts/python.exe",
        "packages/runner_core/.venv/pyvenv.cfg",
    ]


def test_suspicious_diff_classification_is_monotonic() -> None:
    clean_overlay = {
        "changed_baseline_paths": [],
        "research_overlay_paths": [],
        "suspicious_extra_paths": [],
        "git_index_changed": False,
    }

    assert (
        mod._verified_diff_classification("suspicious_implementation", clean_overlay)
        == "suspicious_implementation"
    )


def test_canonical_manifest_detects_mode_symlink_and_index_state(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    _baseline_repo(baseline)
    research = tmp_path / "research"
    _git(["clone", str(baseline), str(research)], cwd=tmp_path)

    _git(["update-index", "--chmod=+x", "src/core.py"], cwd=research)
    symlink_path = research / ".usertest_research" / "source-link.py"
    symlink_path.parent.mkdir()
    try:
        os.symlink(research / "src" / "core.py", symlink_path)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    errors, receipt = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )

    assert "git_index_changed" in errors
    assert "untracked_workspace_file:.usertest_research/source-link.py" not in errors
    assert receipt["excluded_non_regular_research_paths"] == [".usertest_research/source-link.py"]
    assert ".usertest_research/source-link.py" not in receipt["research_overlay_manifest"]
    assert receipt["git_index_changed"] is True
    assert receipt["baseline_git_index_sha256"] != receipt["research_git_index_sha256"]


def test_unreadable_research_scratch_is_excluded_without_hiding_product_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "baseline"
    _baseline_repo(baseline)
    research = tmp_path / "research"
    _git(["clone", str(baseline), str(research)], cwd=tmp_path)
    scratch = research / ".usertest_research" / "pytest-current"
    scratch.parent.mkdir()
    scratch.write_text("host-unreadable reparse surrogate\n", encoding="utf-8")
    (research / "tests" / "unexpected.py").write_text("changed\n", encoding="utf-8")
    real_sha256_path = mod._sha256_path

    def fake_sha256_path(path: Path) -> str:
        if path == scratch:
            raise OSError(22, "invalid host filename")
        return real_sha256_path(path)

    monkeypatch.setattr(mod, "_sha256_path", fake_sha256_path)

    errors, receipt = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )

    assert "untracked_workspace_file:tests/unexpected.py" in errors
    assert "untracked_workspace_file:.usertest_research/pytest-current" not in errors
    assert receipt["excluded_non_regular_research_paths"] == [".usertest_research/pytest-current"]
    assert ".usertest_research/pytest-current" not in receipt["research_overlay_manifest"]


def test_canonical_manifest_detects_filesystem_mode_change(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    _baseline_repo(baseline)
    research = tmp_path / "research"
    _git(["clone", str(baseline), str(research)], cwd=tmp_path)
    source = research / "src" / "core.py"
    original_mode = stat.S_IMODE(source.stat().st_mode)
    os.chmod(source, original_mode & ~stat.S_IWUSR)
    changed_mode = stat.S_IMODE(source.stat().st_mode)
    if changed_mode == original_mode:
        pytest.skip("filesystem does not expose chmod changes")

    errors, _ = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )

    assert "baseline_file_changed:src/core.py" in errors


def test_one_agent_event_cannot_attest_two_experiments() -> None:
    dossier = {
        "experiments": [
            {
                "experiment_id": "support",
                "command": "pytest -q",
                "exit_code": 1,
                "outcome": "supports",
                "artifact_refs": ["artifact:one"],
            },
            {
                "experiment_id": "control",
                "command": "pytest -q",
                "exit_code": 1,
                "outcome": "refutes",
                "artifact_refs": ["artifact:one"],
            },
        ]
    }
    event = {"type": "run_command", "data": {"command": "pytest -q", "exit_code": 1}}
    clean_replays = {
        experiment_id: {
            "experiment_id": experiment_id,
            "command": "pytest -q",
            "exit_code": 1,
            "artifact_refs": ["artifact:one"],
        }
        for experiment_id in ("support", "control")
    }
    errors: list[str] = []

    receipts, _ = mod._experiment_receipts(
        dossier,
        events=[event],
        artifact_keys={"artifact:one"},
        clean_replays=clean_replays,
        errors=errors,
    )

    assert [receipt["experiment_id"] for receipt in receipts] == ["support"]
    assert "experiment_command_not_observed:control" in errors


def test_experiment_receipt_matches_doubled_windows_executable_separators() -> None:
    declared_command = (
        r"C:\Users\jason\AppData\Local\Python\python.exe "
        r".usertest_research\probe.py source_failure"
    )
    observed_command = (
        r"C:\\Users\\jason\\AppData\\Local\\Python\\python.exe "
        r".usertest_research\probe.py source_failure"
    )
    dossier = {
        "experiments": [
            {
                "experiment_id": "source-signature",
                "command": declared_command,
                "exit_code": 0,
                "outcome": "supports",
                "artifact_refs": ["artifact:result"],
            }
        ]
    }
    event = {
        "type": "run_command",
        "data": {"command": observed_command, "exit_code": 0},
    }
    clean_replay = {
        "experiment_id": "source-signature",
        "command": declared_command,
        "exit_code": 0,
        "artifact_refs": ["artifact:result"],
    }
    errors: list[str] = []

    receipts, outcomes = mod._experiment_receipts(
        dossier,
        events=[event],
        artifact_keys={"artifact:result"},
        clean_replays={"source-signature": clean_replay},
        errors=errors,
    )

    assert errors == []
    assert [receipt["experiment_id"] for receipt in receipts] == ["source-signature"]
    assert outcomes == {"source-signature": "supports"}


def test_clean_replay_rejects_agent_claim_that_baseline_does_not_reproduce(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    revision = _baseline_repo(baseline)
    dossier = {
        "artifact_refs": [],
        "inspected_files": ["tests/test_core.py"],
        "experiments": [
            {
                "experiment_id": "claimed-failure",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:one"],
                "command": "pytest -q tests/test_core.py -k guarded_control",
                "result": "The control allegedly fails",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "artifact_refs": [],
            }
        ],
    }
    errors: list[str] = []

    receipts = mod._clean_replay_receipts(
        dossier,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=30,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[baseline],
            source_identity=baseline,
        ),
    )

    assert receipts["claimed-failure"]["exit_code"] == 0
    assert any(error.startswith("experiment_replay_exit_mismatch") for error in errors)
    assert "experiment_observable_assertion_failed:claimed-failure" in errors


@pytest.mark.parametrize("exit_code", [124, 137])
def test_clean_replay_never_executes_interrupted_inconclusive_attempt(
    tmp_path: Path,
    exit_code: int,
) -> None:
    class NeverExecute:
        def isolation_receipt(self, *, source_workspace: Path) -> dict[str, object]:
            return {
                "trust_decision": "approved",
                "trust_reason": "unit_test",
                "platform": "windows",
                "source_workspace": str(source_workspace),
            }

        def execute(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("interrupted inconclusive attempt must not execute")

    dossier = {
        "problem_id": "problem:interrupted",
        "artifact_refs": [],
        "experiments": [
            {
                "experiment_id": "interrupted-control",
                "command": "python .usertest_research/probe.py",
                "outcome": "inconclusive",
                "exit_code": exit_code,
            }
        ],
    }
    errors: list[str] = []

    receipts = mod._clean_replay_receipts(
        dossier,
        baseline_workspace=tmp_path / "baseline",
        research_workspace=tmp_path / "research",
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision="a" * 40,
        timeout_seconds=None,
        errors=errors,
        replay_executor=NeverExecute(),  # type: ignore[arg-type]
    )

    assert receipts == {}
    assert errors == [
        "research_dossier_interrupted_inconclusive_not_replayable:"
        f"problem:interrupted:experiment=interrupted-control:exit_code={exit_code}"
    ]


def test_clean_replay_copies_hash_attested_overlay_harness(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    revision = _baseline_repo(baseline)
    research = tmp_path / "research"
    _git(["clone", str(baseline), str(research)], cwd=tmp_path)
    harness = research / ".usertest_research" / "test_repro.py"
    harness.parent.mkdir()
    harness.write_text(
        "from pathlib import Path\n\n"
        "def test_overlay_repro():\n"
        "    assert 'def run' in Path('src/core.py').read_text(encoding='utf-8')\n",
        encoding="utf-8",
    )
    overlay_errors, overlay = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )
    assert overlay_errors == []
    dossier = {
        "artifact_refs": [
            {
                "artifact_id": "artifact:overlay-harness",
                "path": ".usertest_research/test_repro.py",
            }
        ],
        "experiments": [
            {
                "experiment_id": "overlay-repro",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": ["atom:one"],
                "command": "python -m pytest -q .usertest_research/test_repro.py",
                "result": "The isolated overlay harness passes",
                "outcome": "refutes",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 0,
                },
                "artifact_refs": [],
            }
        ],
    }
    errors: list[str] = []

    receipts = mod._clean_replay_receipts(
        dossier,
        baseline_workspace=baseline,
        research_workspace=research,
        overlay_manifest=overlay["research_overlay_manifest"],
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=30,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[research],
            source_identity=research,
        ),
    )
    second_errors: list[str] = []
    second_receipts = mod._clean_replay_receipts(
        dossier,
        baseline_workspace=baseline,
        research_workspace=research,
        overlay_manifest=overlay["research_overlay_manifest"],
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=30,
        errors=second_errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[research],
            source_identity=research,
        ),
    )
    third_errors: list[str] = []
    third_receipts = mod._clean_replay_receipts(
        dossier,
        baseline_workspace=baseline,
        research_workspace=research,
        overlay_manifest=overlay["research_overlay_manifest"],
        replay_root=tmp_path / "correction-run" / "evidence_replays",
        repo_revision=revision,
        timeout_seconds=30,
        errors=third_errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[research],
            source_identity=research,
        ),
    )

    receipt = receipts["overlay-repro"]
    second_receipt = second_receipts["overlay-repro"]
    third_receipt = third_receipts["overlay-repro"]
    replay_harness = Path(receipt["workspace_dir"]) / ".usertest_research/test_repro.py"
    assert replay_harness.is_file()
    assert receipt["overlay_manifest_sha256"] == overlay["research_overlay_manifest_sha256"]
    assert receipt["post_replay_mutations"] is False
    assert errors == []
    assert second_errors == []
    assert third_errors == []
    assert (
        len(
            {
                receipt["workspace_dir"],
                second_receipt["workspace_dir"],
                third_receipt["workspace_dir"],
            }
        )
        == 3
    )
    replay_refs = [
        ref
        for ref in dossier["artifact_refs"]
        if str(ref.get("artifact_id", "")).startswith("runner:replay:")
    ]
    assert [ref["artifact_id"] for ref in replay_refs] == [
        "runner:replay:overlay-repro:stdout",
        "runner:replay:overlay-repro:stderr",
    ]


def test_clean_replay_detects_persisted_tracked_file_mutation(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    revision = _baseline_repo(baseline)
    mutation_test = baseline / "tests" / "test_mutation.py"
    mutation_test.write_text(
        "from pathlib import Path\n\n"
        "def test_mutates_checkout():\n"
        "    Path('src/core.py').write_text('mutated\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _git(["add", "tests/test_mutation.py"], cwd=baseline)
    _git(["commit", "-m", "mutation fixture"], cwd=baseline)
    revision = _git(["rev-parse", "HEAD"], cwd=baseline)
    dossier = {
        "artifact_refs": [],
        "inspected_files": ["tests/test_mutation.py"],
        "experiments": [
            {
                "experiment_id": "mutating-replay",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:one"],
                "command": "pytest -q tests/test_mutation.py",
                "result": "The test passes after mutating the checkout",
                "outcome": "supports",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 0,
                },
                "artifact_refs": [],
            }
        ],
    }
    errors: list[str] = []

    receipts = mod._clean_replay_receipts(
        dossier,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=30,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[baseline],
            source_identity=baseline,
        ),
    )

    assert receipts["mutating-replay"]["post_replay_mutations"] is True
    assert (
        "experiment_replay_workspace_mutated:mutating-replay:src/core.py" in errors
    )


def test_partial_read_cannot_attest_unobserved_symbol(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    revision = _baseline_repo(workspace)
    del revision
    source = workspace / "src" / "core.py"
    source.write_text(
        "def observed():\n    return True\n\ndef unseen():\n    return False\n",
        encoding="utf-8",
    )
    _git(["add", "src/core.py"], cwd=workspace)
    _git(["commit", "-m", "two symbols"], cwd=workspace)
    observed = "def observed():\n    return True\n"
    attestation = observed_read_attestation(
        path=source,
        observed_text=observed,
        source_exit_code=0,
        allow_partial=True,
    )
    event = {
        "type": "read_file",
        "data": {
            "path": "src/core.py",
            "bytes": source.stat().st_size,
            "read_source": "tool",
            "source_exit_code": 0,
            **attestation,
        },
    }
    dossier = {
        "inspected_files": ["src/core.py"],
        "inspected_symbols": ["core.unseen"],
    }
    errors: list[str] = []

    files, symbols = mod._inspection_receipts(
        dossier,
        workspace=workspace,
        events=[event],
        errors=errors,
    )

    assert len(files) == 1
    assert symbols == []
    assert "inspected_symbol_unresolved:core.unseen" in errors


def test_multiple_partial_reads_retain_earlier_attested_symbol(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _baseline_repo(workspace)
    source = workspace / "src" / "core.py"
    source.write_text(
        "def earlier():\n    return True\n\ndef later():\n    return False\n",
        encoding="utf-8",
    )
    _git(["add", "src/core.py"], cwd=workspace)
    _git(["commit", "-m", "two observed ranges"], cwd=workspace)

    events: list[dict[str, object]] = []
    for observed in (
        "def earlier():\n    return True\n",
        "def later():\n    return False\n",
    ):
        attestation = observed_read_attestation(
            path=source,
            observed_text=observed,
            source_exit_code=0,
            allow_partial=True,
        )
        events.append(
            {
                "type": "read_file",
                "data": {
                    "path": "src/core.py",
                    "bytes": source.stat().st_size,
                    "read_source": "tool",
                    "source_exit_code": 0,
                    **attestation,
                },
            }
        )
    errors: list[str] = []

    _files, symbols = mod._inspection_receipts(
        {
            "inspected_files": ["src/core.py"],
            "inspected_symbols": ["core.earlier"],
        },
        workspace=workspace,
        events=events,
        errors=errors,
    )

    assert errors == []
    assert symbols == [{"symbol": "core.earlier", "path": "src/core.py"}]


def _exact_range_read_event(
    *,
    source: Path,
    relative_path: str,
    skip_lines: int,
    first_lines: int,
) -> dict[str, object]:
    normalized = source.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    observed = "".join(
        normalized.splitlines(keepends=True)[skip_lines : skip_lines + first_lines]
    )
    attestation = observed_read_attestation(
        path=source,
        observed_text=observed,
        source_exit_code=0,
        allow_partial=True,
    )
    return {
        "type": "read_file",
        "data": {
            "path": relative_path,
            "bytes": source.stat().st_size,
            "read_source": "shell_command",
            "attestation_kind": "exact_line_range",
            "source_exit_code": 0,
            "requested_skip_lines": skip_lines,
            "requested_first_lines": first_lines,
            **attestation,
        },
    }


def test_exact_range_read_revalidation_rejects_unmintable_partial_shell_shapes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text(
        "def earlier():\n    return True\n\ndef later():\n    return False\n",
        encoding="utf-8",
    )
    event = _exact_range_read_event(
        source=source,
        relative_path="source.py",
        skip_lines=0,
        first_lines=2,
    )
    data = event["data"]
    assert isinstance(data, dict)

    assert mod._revalidated_read_event_attestation(path=source, data=data) is not None

    generic_shell = {key: value for key, value in data.items() if key != "attestation_kind"}
    assert mod._revalidated_read_event_attestation(path=source, data=generic_shell) is None

    wrong_range = {**data, "requested_skip_lines": 3}
    assert mod._revalidated_read_event_attestation(path=source, data=wrong_range) is None

    invalid_count = {**data, "requested_first_lines": 2_001}
    assert mod._revalidated_read_event_attestation(path=source, data=invalid_count) is None


class _LocalDockerReceiptExecutor:
    """Local test double that emits the same durable metadata contract as Docker."""

    def __init__(self, image_ref: str) -> None:
        self.image_ref = image_ref

    def isolation_receipt(self, *, source_workspace: Path) -> dict[str, object]:
        pythonpath, _receipt = mod._repository_python_import_environment(
            source_workspace,
            execution_root="/workspace",
            path_separator=":",
        )
        return {
            "executor": "docker",
            "platform": "linux",
            "os_sandbox": True,
            "network": "none",
            "filesystem_isolation": "dedicated_clone_bind_mount",
            "trust_decision": "explicit_image",
            "trust_reason": self.image_ref,
            "source_workspace": str(source_workspace.resolve()),
            "sanitized_environment_keys": [
                "CI",
                *(["PYTHONPATH"] if pythonpath is not None else []),
            ],
        }

    def execute(
        self,
        argv: list[str],
        *,
        cwd: Path,
        source_workspace: Path,
        timeout_seconds: float | None,
        environment_overrides: dict[str, str | None] | None = None,
    ) -> mod.ReplayExecutionResult:
        del source_workspace
        environment = os.environ.copy()
        environment.update({"CI": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        for key, value in (environment_overrides or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        executed = list(argv)
        if Path(executed[0]).name.casefold() in {"python", "python3", "python.exe"}:
            executed[0] = sys.executable
        completed = subprocess.run(
            executed,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
            env=environment,
        )
        container_name = "test-" + sha256(str(cwd).encode()).hexdigest()[:12]
        metadata_dir = cwd.parent / f".{cwd.name}.docker_replay"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = metadata_dir / "sandbox.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "backend": "docker",
                    "network_mode": "none",
                    "container_name": container_name,
                    "image_tag": self.image_ref,
                }
            ),
            encoding="utf-8",
        )
        pythonpath, import_receipt = mod._repository_python_import_environment(
            cwd,
            execution_root="/workspace",
            path_separator=":",
        )
        applied_environment = {
            "CI": "1",
            **({"PYTHONPATH": pythonpath} if pythonpath is not None else {}),
        }
        return mod.ReplayExecutionResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            execution_metadata={
                "executor": "docker",
                "sandbox_metadata_path": str(metadata_path),
                "sandbox_metadata_sha256": sha256(metadata_path.read_bytes()).hexdigest(),
                "backend": "docker",
                "image_tag": self.image_ref,
                "image_hash": None,
                "image_id": "sha256:" + sha256(self.image_ref.encode()).hexdigest(),
                "network": "none",
                "container_name": container_name,
                "cleanup_attempted": True,
                "cleanup_confirmed": True,
                "environment_attestation": mod.environment_attestation(applied_environment),
                "repository_python_import": import_receipt,
            },
        )


def _persisted_router_overlay_dossier(tmp_path: Path) -> dict[str, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "persisted-symmetry"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    source = workspace / "src" / "core.py"
    source.parent.mkdir()
    source.write_text(
        "def earlier():\n"
        "    return True\n\n"
        "padding_1 = 1\n"
        "padding_2 = 2\n"
        "padding_3 = 3\n\n"
        "def later():\n"
        "    return False\n",
        encoding="utf-8",
    )
    revision = _baseline_repo_commit_existing(workspace, "persisted verifier symmetry")
    harness = workspace / ".usertest_research" / "probe.py"
    harness.parent.mkdir()
    harness.write_text('print("{\\"signal\\":\\"probe-ok\\"}")\n', encoding="utf-8")

    atom_id = "atom:persisted-symmetry"
    assignment = _runner_bound_atom_assignment(
        atom_id=atom_id,
        atom_snapshot={
            "atom_id": atom_id,
            "signal": "probe-ok",
            "text": "The observed signal is probe-ok.",
            "evidence_role": "observation",
            "origin_stage": "runtime",
        },
    )
    assignment.update(
        case_id="case:persisted-symmetry",
        problem_id="problem:persisted-symmetry",
    )
    assignment["assignment_sha256"] = mod.evidence_assignment_sha256(assignment)
    experiments: list[dict[str, object]] = []
    for experiment_id, platform_requirement in (
        ("experiment:router-default", None),
        ("experiment:router-linux", "linux"),
    ):
        experiment: dict[str, object] = {
            "experiment_id": experiment_id,
            "scenario_kind": "diagnostic_probe",
            "addresses_atom_ids": [atom_id],
            "origin_evidence_bindings": [
                {
                    "atom_id": atom_id,
                    "role": "symptom",
                    "field_path": "$.signal",
                    "value": "probe-ok",
                }
            ],
            "command": "python .usertest_research/probe.py",
            "result": "The retained research harness emitted the assigned probe signal.",
            "outcome": "supports",
            "exit_code": 0,
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": '"signal":"probe-ok"',
            },
            "artifact_refs": ["artifact:probe-harness"],
        }
        if platform_requirement is not None:
            experiment["platform_requirement"] = platform_requirement
        experiments.append(experiment)
    dossier: dict[str, object] = {
        "research_schema_version": 3,
        "case_id": "case:persisted-symmetry",
        "problem_id": "problem:persisted-symmetry",
        "repo_revision": revision,
        "research_method": "persisted verifier symmetry regression",
        "reproduction_status": "partial",
        "research_status": "insufficient_evidence",
        "writes_used": True,
        "writes_purpose": ["retained research harness"],
        "implementation_performed": False,
        "artifact_refs": [
            {
                "artifact_id": "artifact:probe-harness",
                "kind": "research_harness",
                "path": ".usertest_research/probe.py",
                "description": "Retained research-only probe",
            }
        ],
        "experiments": experiments,
        "inspected_files": ["src/core.py"],
        "inspected_symbols": ["core.earlier"],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:incomplete-mechanism",
                "statement": "The available probe does not yet establish the production mechanism.",
                "supporting_evidence": ["experiment:router-default"],
                "counterevidence": [],
                "mechanism_symbols": ["core.earlier"],
                "disposition": "primary",
                "disposition_evidence": ["experiment:router-default"],
                "falsification_attempts": [],
            }
        ],
        "root_cause_confidence": 0.2,
        "broader_class_assessment": "unknown",
        "material_unknowns": ["The production causal path remains unverified."],
        "blocking_reasons": ["mechanism evidence is incomplete"],
        "evidence_boundaries": [],
        "case_relation_assessment": {
            "disposition": "retain",
            "rationale": "The regression fixture retains one signed occurrence.",
            "facets": [],
            "material_unknowns": ["The mechanism remains incomplete."],
        },
        "evidence_assignment": assignment,
    }
    run_dir = tmp_path / "research-run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text('{"status":"complete"}\n', encoding="utf-8")
    (run_dir / "workspace_ref.json").write_text(
        json.dumps({"workspace_dir": str(workspace)}),
        encoding="utf-8",
    )
    (run_dir / "target_ref.json").write_text(
        json.dumps({"ref": revision, "commit_sha": revision, "agent": "claude"}),
        encoding="utf-8",
    )
    events: list[dict[str, object]] = [
        {
            "type": "run_command",
            "data": {"command": "python .usertest_research/probe.py", "exit_code": 0},
        },
        {
            "type": "run_command",
            "data": {"command": "python .usertest_research/probe.py", "exit_code": 0},
        },
        _exact_range_read_event(
            source=source,
            relative_path="src/core.py",
            skip_lines=0,
            first_lines=2,
        ),
        _exact_range_read_event(
            source=source,
            relative_path="src/core.py",
            skip_lines=7,
            first_lines=2,
        ),
    ]
    _write_normalized_events(run_dir / "normalized_events.jsonl", events)
    artifact_refs = dossier["artifact_refs"]
    assert isinstance(artifact_refs, list)
    artifact_refs.append(
        {
            "artifact_id": "runner:target_ref",
            "kind": "runner_provenance",
            "path": str(run_dir / "target_ref.json"),
        }
    )
    default_executor = _LocalDockerReceiptExecutor("test/default:immutable")
    linux_executor = _LocalDockerReceiptExecutor("test/linux:immutable")
    receipt = mod.verify_research_evidence(
        dossier,
        run_dir=run_dir,
        repo_revision=revision,
        case_id="case:persisted-symmetry",
        problem_id="problem:persisted-symmetry",
        expected_case_id="case:persisted-symmetry",
        expected_problem_id="problem:persisted-symmetry",
        evidence_assignment=assignment,
        evidence_atom_ids=[atom_id],
        revision_view_destination=tmp_path / "revision-view",
        replay_timeout_seconds=None,
        requested_repo_ref=revision,
        resolved_repo_ref=revision,
        replay_executor=mod.PlatformRoutingReplayExecutor(
            default_executor=default_executor,
            platform_executors={"linux": linux_executor},
        ),
    )
    dossier["evidence_verification"] = receipt
    assert receipt["status"] == "verified", receipt["errors"]
    return dossier


def test_persisted_verifier_roundtrips_router_overlay_ranges_and_incomplete_mechanism(
    tmp_path: Path,
) -> None:
    dossier = _persisted_router_overlay_dossier(tmp_path)
    receipt = dossier["evidence_verification"]
    assert isinstance(receipt, dict)

    valid, errors = mod.verify_persisted_research_evidence(dossier)

    assert valid is True
    assert errors == []
    assert receipt["experiments"][0]["execution_isolation"]["trust_reason"] == (
        "test/default:immutable"
    )
    assert receipt["experiments"][1]["execution_isolation"]["trust_reason"] == (
        "test/linux:immutable"
    )
    assert any(
        item["component"] == "blocked_or_insufficient_mechanism_projection"
        for item in receipt["quarantined_diagnostics"]
    )

    route_tampered = deepcopy(dossier)
    route_receipt = route_tampered["evidence_verification"]
    assert isinstance(route_receipt, dict)
    route_receipt["experiments"][0]["execution_isolation"] = deepcopy(
        route_receipt["replay_isolation"]["routes"]["linux"]
    )
    route_receipt["receipt_sha256"] = stage_contracts.evidence_verification_sha256(route_receipt)
    route_valid, route_errors = mod.verify_persisted_research_evidence(route_tampered)
    assert route_valid is False
    assert (
        "research_replay_isolation_changed:experiment:router-default:route_mismatch"
        in route_errors
    )

    quarantine_tampered = deepcopy(dossier)
    quarantine_receipt = quarantine_tampered["evidence_verification"]
    assert isinstance(quarantine_receipt, dict)
    quarantine_receipt["quarantined_diagnostics"] = []
    quarantine_receipt["receipt_sha256"] = stage_contracts.evidence_verification_sha256(
        quarantine_receipt
    )
    quarantine_valid, quarantine_errors = mod.verify_persisted_research_evidence(
        quarantine_tampered
    )
    assert quarantine_valid is False
    assert "research_quarantined_diagnostics_changed" in quarantine_errors


def test_persisted_verifier_accepts_legacy_absent_additive_empty_overlay_fields(
    tmp_path: Path,
) -> None:
    dossier = _persisted_router_overlay_dossier(tmp_path)
    receipt = dossier["evidence_verification"]
    assert isinstance(receipt, dict)
    overlay = receipt["workspace_overlay"]
    assert isinstance(overlay, dict)
    for field in (
        "excluded_non_regular_research_paths",
        "ignored_tool_environment_roots",
        "ignored_tool_environment_paths",
    ):
        assert overlay.pop(field) == []
    receipt["receipt_sha256"] = stage_contracts.evidence_verification_sha256(receipt)

    valid, errors = mod.verify_persisted_research_evidence(dossier)

    assert valid is True
    assert errors == []


@pytest.mark.parametrize(
    "field",
    [
        "excluded_non_regular_research_paths",
        "ignored_tool_environment_roots",
        "ignored_tool_environment_paths",
    ],
)
def test_persisted_overlay_legacy_omission_rejects_nonempty_current_value(field: str) -> None:
    recomputed = {
        "stable_field": "unchanged",
        "excluded_non_regular_research_paths": [],
        "ignored_tool_environment_roots": [],
        "ignored_tool_environment_paths": [],
    }
    recomputed[field] = ["current/nonempty"]
    persisted = {key: value for key, value in recomputed.items() if key != field}

    assert mod._persisted_workspace_overlay_matches(persisted, recomputed) is False


def _retained_overlay_asset_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], dict[str, object], Path, Path, Path]:
    planning = tmp_path / "planning"
    source = planning / "packages" / "runtime" / "src" / "runtime" / "backend.py"
    source.parent.mkdir(parents=True)
    source.write_text("def mechanism():\n    return 49\n", encoding="utf-8")
    revision = _baseline_repo_commit_existing(planning, "add retained-overlay mechanism")
    research = tmp_path / "research"
    _git(["clone", str(planning), str(research)], cwd=tmp_path)
    harness = research / ".usertest_research" / "test_probe.py"
    harness.parent.mkdir()
    harness.write_text(
        "from runtime.backend import mechanism\n\n"
        "def test_probe():\n"
        "    assert mechanism() == 5\n",
        encoding="utf-8",
    )
    overlay_errors, overlay = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=planning,
    )
    assert overlay_errors == []
    run_dir = tmp_path / "runs" / "stage3" / "accepted-run"
    run_dir.mkdir(parents=True)
    asset_errors: list[str] = []
    asset = mod._persist_outcome_overlay_asset(
        run_dir=run_dir,
        research_workspace=research,
        overlay_manifest=overlay["research_overlay_manifest"],
        errors=asset_errors,
    )
    assert asset_errors == []
    assert isinstance(asset, dict)
    dossier: dict[str, object] = {
        "case_id": "case:retained-overlay",
        "problem_id": "problem:retained-overlay",
        "repo_revision": revision,
        "inspected_files": ["packages/runtime/src/runtime/backend.py"],
        "artifact_refs": [
            {
                "artifact_id": "artifact:test-probe",
                "kind": "research_harness",
                "path": ".usertest_research/test_probe.py",
            }
        ],
    }
    receipt: dict[str, object] = {
        "run_dir": str(run_dir),
        "workspace_dir": str(research),
        "workspace_overlay": overlay,
        "verified_mechanism_sha256": "a" * 64,
        "verified_mechanism_provenance_sha256": "b" * 64,
        "outcome_oracles": [
            {
                "case_id": dossier["case_id"],
                "repo_revision": revision,
                "primary_verified_mechanism_sha256": "a" * 64,
                "primary_verified_mechanism_provenance_sha256": "b" * 64,
                "asset": asset,
            }
        ],
    }
    return dossier, receipt, planning, research, harness


def test_persisted_verifier_recovers_exact_overlay_and_authorization_from_same_run_asset(
    tmp_path: Path,
) -> None:
    dossier, receipt, planning, research, harness = _retained_overlay_asset_fixture(tmp_path)
    experiment = {
        "scenario_kind": "faithful_replay",
        "repository_bindings": [
            {
                "path": "packages/runtime/src/runtime/backend.py",
                "relationship": "The retained harness imports this inspected production module.",
            }
        ],
    }
    command = "python -B -m pytest -q .usertest_research/test_probe.py::test_probe"
    original = mod._authorized_replay_invocation(
        command=command,
        experiment=experiment,
        dossier=dossier,
        assignment={},
        workspace=research,
    )
    assert original is not None
    original_sha256 = mod._sha256_path(harness)
    original_size = harness.stat().st_size
    harness.write_text("def test_probe():\n    assert False\n", encoding="utf-8")

    bundle, manifest, errors = mod._authenticated_retained_overlay_workspace(
        dossier=dossier,
        receipt=receipt,
        run_dir=Path(str(receipt["run_dir"])),
        planning_workspace=planning,
    )

    assert errors == []
    assert bundle is not None
    assert mod._sha256_path(bundle / ".usertest_research" / "test_probe.py") == original_sha256
    artifact = {
        "artifact_id": "artifact:test-probe",
        "path": str(harness),
        "sha256": original_sha256,
        "size_bytes": original_size,
    }
    assert mod._persisted_artifact_path(
        artifact,
        original_research_workspace=research,
        retained_overlay_workspace=bundle,
        retained_overlay_manifest=manifest,
        planning_workspace=planning,
    ) == bundle / ".usertest_research" / "test_probe.py"
    reconstructed = mod._persisted_retained_authorized_invocation(
        command=command,
        experiment=experiment,
        dossier=dossier,
        assignment={},
        retained_workspace=bundle,
        retained_manifest=manifest,
        planning_workspace=planning,
        persisted_authorization=original[1],
    )
    assert reconstructed == original

    legacy = mod._command_authorization_receipt(
        {
            "authorization_kind": "declared_repository_bindings",
            "executed_argv_sha256": mod._canonical_json_sha256(original[0]),
            "shell": False,
            "workspace_confined": True,
            "repository_bindings": original[1]["repository_bindings"],
        }
    )
    assert mod._persisted_retained_authorized_invocation(
        command=command,
        experiment=experiment,
        dossier=dossier,
        assignment={},
        retained_workspace=bundle,
        retained_manifest=manifest,
        planning_workspace=planning,
        persisted_authorization=legacy,
    ) == (original[0], legacy)


def test_persisted_retained_overlay_rejects_content_tamper(tmp_path: Path) -> None:
    dossier, receipt, planning, _research, _harness = _retained_overlay_asset_fixture(tmp_path)
    asset = receipt["outcome_oracles"][0]["asset"]
    bundle = tmp_path / "runs" / str(asset["runs_relative_path"])
    retained_harness = bundle / ".usertest_research" / "test_probe.py"
    retained_harness.write_text(retained_harness.read_text() + "# tamper\n", encoding="utf-8")

    resolved, manifest, errors = mod._authenticated_retained_overlay_workspace(
        dossier=dossier,
        receipt=receipt,
        run_dir=Path(str(receipt["run_dir"])),
        planning_workspace=planning,
    )

    assert resolved is None
    assert manifest == {}
    assert "research_retained_overlay_asset_content_changed" in errors


@pytest.mark.parametrize("tamper", ["manifest", "declared_path", "asset_id", "oracle_binding"])
def test_persisted_retained_overlay_rejects_untrusted_asset_bindings(
    tmp_path: Path,
    tamper: str,
) -> None:
    dossier, original_receipt, planning, _research, _harness = (
        _retained_overlay_asset_fixture(tmp_path)
    )
    receipt = deepcopy(original_receipt)
    oracle = receipt["outcome_oracles"][0]
    asset = oracle["asset"]
    if tamper == "manifest":
        asset["manifest"][".usertest_research/test_probe.py"]["sha256"] = "0" * 64
    elif tamper == "declared_path":
        asset["runs_relative_path"] = "stage3/accepted-run/../forged/bundle"
    elif tamper == "asset_id":
        asset["asset_id"] = "outcome_asset:" + "0" * 64
    else:
        oracle["repo_revision"] = "0" * 40

    resolved, manifest, errors = mod._authenticated_retained_overlay_workspace(
        dossier=dossier,
        receipt=receipt,
        run_dir=Path(str(receipt["run_dir"])),
        planning_workspace=planning,
    )

    assert resolved is None
    assert manifest == {}
    assert errors


@pytest.mark.parametrize("drift", ["tracked_content", "planning_head"])
def test_persisted_retained_overlay_does_not_forgive_pinned_planning_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    dossier, receipt, planning, _research, _harness = _retained_overlay_asset_fixture(tmp_path)
    source = planning / "packages" / "runtime" / "src" / "runtime" / "backend.py"
    source.write_text("def mechanism():\n    return 5\n", encoding="utf-8")
    if drift == "planning_head":
        _git(["add", "-A"], cwd=planning)
        _git(["commit", "-m", "drift"], cwd=planning)

    resolved, manifest, errors = mod._authenticated_retained_overlay_workspace(
        dossier=dossier,
        receipt=receipt,
        run_dir=Path(str(receipt["run_dir"])),
        planning_workspace=planning,
    )

    assert resolved is None
    assert manifest == {}
    assert any(error.startswith("research_retained_overlay_baseline_") for error in errors)


def test_persisted_verifier_still_rejects_runner_artifact_drift(tmp_path: Path) -> None:
    dossier = _persisted_router_overlay_dossier(tmp_path)
    receipt = dossier["evidence_verification"]
    receipt["normalized_events_sha256"] = "0" * 64
    receipt["receipt_sha256"] = stage_contracts.evidence_verification_sha256(receipt)

    valid, errors = mod.verify_persisted_research_evidence(dossier)

    assert valid is False
    assert "research_runner_artifact_changed:normalized_events.jsonl" in errors


def _verified_retained_harness_dossier(tmp_path: Path) -> tuple[dict[str, object], Path, Path]:
    workspace = tmp_path / "workspace"
    source = workspace / "packages" / "runtime" / "src" / "runtime" / "backend.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import os\n\ndef mode():\n    return os.getenv('BACKLOG_DEPTH_VERIFY_MODE')\n",
        encoding="utf-8",
    )
    (workspace / "packages" / "runtime" / "pyproject.toml").write_text(
        "[project]\nname = 'runtime'\nversion = '0.0.0'\n",
        encoding="utf-8",
    )
    revision = _baseline_repo_commit_existing(workspace, "add environment consumer")
    harness = workspace / ".usertest_research" / "environment_probe.py"
    harness.parent.mkdir()
    harness.write_text(
        "import json\n"
        "from runtime.backend import mode\n"
        "print(json.dumps({'mode': mode()}, separators=(',', ':')))\n",
        encoding="utf-8",
    )
    command = "python .usertest_research/environment_probe.py"
    atom_id = "atom:retained-harness"
    atom = {
        "atom_id": atom_id,
        "observed_output": '{"mode":null}',
        "expected_mode": "ready",
        "text": "The mode was null even though the required mode is ready.",
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    assignment = _runner_bound_atom_assignment(atom_id=atom_id, atom_snapshot=atom)
    assignment.update(
        case_id="case:retained-harness",
        problem_id="problem:retained-harness",
    )
    assignment["atom_receipts"][0]["origin_evidence_mode"] = "signed_snapshot"
    assignment["assignment_sha256"] = mod.evidence_assignment_sha256(assignment)
    claim = {
        "adapter_id": "environment.v1",
        "hypothesis_id": "hypothesis:retained-environment",
        "baseline_experiment_id": "experiment:mode-absent",
        "challenge_experiment_id": "experiment:mode-ready",
        "intervention": {
            "kind": "child_environment_variable",
            "target": "env:BACKLOG_DEPTH_VERIFY_MODE",
            "predicted_polarity": "absent_to_ready",
            "before": None,
            "after": "ready",
        },
        "observations": {
            "baseline": {"source": "stdout_json", "json_pointer": "/mode"},
            "challenge": {"source": "stdout_json", "json_pointer": "/mode"},
        },
        "positive_outcome": {
            "predicate": {"kind": "equals", "expected": "ready"},
            "semantic_basis": {
                "kind": "origin_exact_value",
                "atom_id": atom_id,
                "field_path": "$.expected_mode",
            },
        },
        "implementation_touchpoints": [
            {
                "causal_locator": "env:BACKLOG_DEPTH_VERIFY_MODE",
                "path": "packages/runtime/src/runtime/backend.py",
                "symbols": ["runtime.backend.mode"],
                "relationship": "This inspected production function consumes the controlled mode.",
            }
        ],
    }
    experiments = [
        {
            "experiment_id": "experiment:mode-absent",
            "scenario_kind": "environment_mode_absent",
            "addresses_atom_ids": [atom_id],
            "command": command,
            "result": "The clean child reports a null mode.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"environment": {"BACKLOG_DEPTH_VERIFY_MODE": None}},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": '{"mode":null}',
            },
            "origin_evidence_bindings": [
                {
                    "atom_id": atom_id,
                    "role": "symptom",
                    "field_path": "$.observed_output",
                    "value": '{"mode":null}',
                    "value_sha256": mod._canonical_json_sha256('{"mode":null}'),
                }
            ],
            "artifact_refs": ["artifact:environment-probe"],
        },
        {
            "experiment_id": "experiment:mode-ready",
            "scenario_kind": "environment_mode_ready",
            "addresses_atom_ids": [atom_id],
            "command": command,
            "result": "The controlled child reports ready.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"environment": {"BACKLOG_DEPTH_VERIFY_MODE": "ready"}},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": '{"mode":"ready"}',
            },
            "origin_evidence_bindings": [
                {
                    "atom_id": atom_id,
                    "role": "symptom",
                    "field_path": "$.expected_mode",
                    "value": "ready",
                    "value_sha256": mod._canonical_json_sha256("ready"),
                }
            ],
            "artifact_refs": ["artifact:environment-probe"],
            "proof_adapter": claim,
        },
    ]
    statement = "The child environment value controls the emitted mode."
    dossier: dict[str, object] = {
        "research_schema_version": 3,
        "case_id": "case:retained-harness",
        "problem_id": "problem:retained-harness",
        "repo_revision": revision,
        "research_method": "runner_retained_environment_harness",
        "reproduction_status": "reproduced",
        "research_status": "evidence_sufficient",
        "writes_used": True,
        "writes_purpose": ["retained research harness"],
        "implementation_performed": False,
        "diff_classification": "allowed_research_edits",
        "artifact_refs": [
            {
                "artifact_id": "artifact:environment-probe",
                "kind": "research_harness",
                "path": ".usertest_research/environment_probe.py",
            }
        ],
        "experiments": experiments,
        "inspected_files": ["packages/runtime/src/runtime/backend.py"],
        "inspected_symbols": ["runtime.backend.mode"],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:retained-environment",
                "statement": statement,
                "supporting_evidence": [
                    "experiment:mode-absent",
                    "experiment:mode-ready",
                ],
                "counterevidence": [],
                "mechanism_symbols": ["env:BACKLOG_DEPTH_VERIFY_MODE"],
                "disposition": "primary",
                "disposition_evidence": ["experiment:mode-ready"],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:environment-does-not-control-output",
                        "hypothesis_id": "hypothesis:retained-environment",
                        "claim": statement,
                        "baseline_experiment_id": "experiment:mode-absent",
                        "challenge_experiment_id": "experiment:mode-ready",
                        "disproof_condition": {
                            "source": "stdout",
                            "operator": "not_contains",
                            "expected": '{"mode":"ready"}',
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
        "root_cause_confidence": 0.75,
        "broader_class_assessment": "unknown",
        "case_relation_assessment": {
            "disposition": "retain",
            "rationale": "The source occurrence and verified environment mechanism are one unit.",
            "facets": [],
            "material_unknowns": [],
        },
        "material_unknowns": [],
        "blocking_reasons": [],
        "evidence_boundaries": [],
        "evidence_assignment": assignment,
    }
    run_dir = tmp_path / "runs" / "stage3" / "accepted-run"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps({"schema_version": 1, "kind": "troubleshoot_v1", "status": "success"}),
        encoding="utf-8",
    )
    (run_dir / "workspace_ref.json").write_text(
        json.dumps({"workspace_dir": str(workspace)}),
        encoding="utf-8",
    )
    (run_dir / "target_ref.json").write_text(
        json.dumps({"ref": revision, "commit_sha": revision, "agent": "claude"}),
        encoding="utf-8",
    )
    dossier["artifact_refs"].append(
        {
            "artifact_id": "runner:target_ref",
            "kind": "runner_provenance",
            "path": str(run_dir / "target_ref.json"),
        }
    )
    source_text = source.read_text(encoding="utf-8")
    harness_text = harness.read_text(encoding="utf-8")
    events = [
        {"type": "run_command", "data": {"command": command, "exit_code": 0}},
        {"type": "run_command", "data": {"command": command, "exit_code": 0}},
        {
            "type": "read_file",
            "data": {
                "path": "packages/runtime/src/runtime/backend.py",
                "bytes": source.stat().st_size,
                "read_source": "tool",
                "source_exit_code": 0,
                **observed_read_attestation(
                    path=source,
                    observed_text=source_text,
                    source_exit_code=0,
                    allow_partial=False,
                ),
            },
        },
        {
            "type": "read_file",
            "data": {
                "path": ".usertest_research/environment_probe.py",
                "bytes": harness.stat().st_size,
                "read_source": "tool",
                "source_exit_code": 0,
                **observed_read_attestation(
                    path=harness,
                    observed_text=harness_text,
                    source_exit_code=0,
                    allow_partial=False,
                ),
            },
        },
    ]
    _write_normalized_events(run_dir / "normalized_events.jsonl", events)
    receipt = mod.verify_research_evidence(
        dossier,
        run_dir=run_dir,
        repo_revision=revision,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        expected_case_id=str(dossier["case_id"]),
        expected_problem_id=str(dossier["problem_id"]),
        evidence_assignment=assignment,
        evidence_atom_ids=[atom_id],
        revision_view_destination=tmp_path / "revision-view",
        replay_timeout_seconds=None,
        requested_repo_ref=revision,
        resolved_repo_ref=revision,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=workspace,
        ),
    )
    dossier["evidence_verification"] = receipt
    assert receipt["status"] == "verified", receipt["errors"]
    assert len(receipt["outcome_oracles"]) == 1
    assert "asset" in receipt["outcome_oracles"][0]
    return dossier, workspace, harness


def test_full_persisted_verifier_recovers_drifted_overlay_and_rejects_asset_tamper(
    tmp_path: Path,
) -> None:
    dossier, _workspace, harness = _verified_retained_harness_dossier(tmp_path)
    original = harness.read_bytes()
    harness.write_text("print('later correction turn')\n", encoding="utf-8")

    recovered, recovery_errors = mod.verify_persisted_research_evidence(dossier)

    assert recovered is True, recovery_errors
    assert recovery_errors == []
    receipt = dossier["evidence_verification"]
    asset = receipt["outcome_oracles"][0]["asset"]
    runs_root = mod._runs_root_for(Path(str(receipt["run_dir"])))
    assert runs_root is not None
    retained_harness = (
        runs_root
        / str(asset["runs_relative_path"])
        / ".usertest_research"
        / "environment_probe.py"
    )
    assert retained_harness.read_bytes() == original
    retained_harness.write_bytes(original + b"# tamper\n")

    tampered, tamper_errors = mod.verify_persisted_research_evidence(dossier)

    assert tampered is False
    assert "research_retained_overlay_asset_content_changed" in tamper_errors


def test_partial_python_read_attests_local_assignment_definition() -> None:
    observed = (
        "        codex_personality_warning_detected = bool(warning_lines)\n"
        "        if codex_personality_warning_detected:\n"
        "            handle_warning()\n"
    )

    assert mod._symbol_definition_exists(
        path="packages/runner_core/src/runner_core/runner.py",
        content=observed,
        symbol="runner_core.runner.codex_personality_warning_detected",
    )


def test_unrelated_assertion_failure_has_no_mechanism_causal_link(tmp_path: Path) -> None:
    output = "tests/test_repro.py:4: in test_repro\n    assert False\nE   assert False\n"

    assert (
        mod._causal_trace_match(
            output=output,
            relative_path="src/core.py",
            symbol="core.run",
        )
        is None
    )
    linked = mod._causal_trace_match(
        output='  File "/workspace/src/core.py", line 2, in run\n',
        relative_path="src/core.py",
        symbol="core.run",
    )
    assert linked is not None
    assert linked[0] == "python_traceback"


def test_model_overlay_cannot_print_its_own_mechanism_causal_link(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "stdout.txt"
    output_path.write_text(
        '  File "/workspace/src/core.py", line 2, in run\n',
        encoding="utf-8",
    )
    dossier = {
        "experiments": [
            {
                "experiment_id": "self-authored-replay",
                "scenario_kind": "faithful_replay",
                "command": "pytest -q .usertest_research/test_fake_trace.py",
                "outcome": "supports",
            }
        ],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "supporting_evidence": ["self-authored-replay"],
                "mechanism_symbols": ["core.run"],
            }
        ],
    }
    errors: list[str] = []

    links = mod._causal_link_receipts(
        dossier,
        clean_replays={
            "self-authored-replay": {
                "executed_argv": [
                    "pytest",
                    "-q",
                    ".usertest_research/test_fake_trace.py",
                ],
                "stdout_path": str(output_path),
                "stderr_path": str(tmp_path / "missing-stderr.txt"),
            }
        },
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert links == []
    assert any("model_overlay_untrusted" in error for error in errors)
    assert "mechanism_causal_trace_missing:h1:core.run" in errors


def test_one_replay_cannot_cover_unrelated_commandless_atoms_by_exit_code(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin.json"
    origin.write_text('{"message":"two unrelated failures"}', encoding="utf-8")
    dossier = {
        "experiments": [
            {
                "experiment_id": "generic-failure",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:one", "atom:two"],
                "command": "pytest -q tests/test_one.py",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
            }
        ]
    }
    assignment = {
        "status": "complete",
        "expected_atom_ids": ["atom:one", "atom:two"],
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_snapshot": {
                    "atom_id": atom_id,
                    "text": text,
                    "exit_code": 1,
                },
                "artifact_receipts": [{"path": str(origin)}],
            }
            for atom_id, text in (
                ("atom:one", "Database migration failed"),
                ("atom:two", "Browser launch failed"),
            )
        ],
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert bindings == []
    assert any("experiment_not_bound_to_atom:generic-failure:atom:one" in e for e in errors)
    assert any("experiment_not_bound_to_atom:generic-failure:atom:two" in e for e in errors)
    assert "supporting_experiments_do_not_cover_origin_atoms" in errors


@pytest.mark.parametrize("relation_disposition", ["retain", "keep_separate"])
def test_signed_case_aggregate_covers_redundant_occurrence_shape(
    relation_disposition: str,
) -> None:
    dossier = {
        "research_status": "evidence_sufficient",
        "case_relation_assessment": {
            "disposition": relation_disposition,
            "rationale": "One signed aggregate authenticates the repeated evidence shape.",
            "facets": [],
            "material_unknowns": [],
        },
        "experiments": [
            {
                "experiment_id": "aggregate-replay",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": ["atom:aggregate", "atom:one", "atom:two"],
                "command": "python replay.py",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "contains",
                    "expected": "codex_model_messages_missing",
                },
            }
        ],
    }
    assignment = {
        "status": "complete",
        "expected_atom_ids": ["atom:aggregate", "atom:one", "atom:two"],
        "case_evidence_atom_ids": ["atom:aggregate"],
        "occurrence_evidence_atom_ids": ["atom:one", "atom:two"],
        "atom_receipts": [
            {
                "atom_id": "atom:aggregate",
                "atom_snapshot": {
                    "atom_id": "atom:aggregate",
                    "error_code": "codex_model_messages_missing",
                },
            },
            {"atom_id": "atom:one", "atom_snapshot": {"atom_id": "atom:one"}},
            {"atom_id": "atom:two", "atom_snapshot": {"atom_id": "atom:two"}},
        ],
    }
    dossier["evidence_assignment"] = assignment
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert [binding["atom_id"] for binding in bindings] == ["atom:aggregate"]
    assert errors == []


def test_persisted_receipt_json_projection_is_stable() -> None:
    assert mod._canonical_json_sha256({"b": 2, "a": 1}) == mod._canonical_json_sha256(
        json.loads('{"a": 1, "b": 2}')
    )


def test_inspected_symbol_supports_exact_python_import_and_constant_bindings() -> None:
    content = (
        "import os as operating_system\n"
        "from pathlib import Path as RepoPath\n"
        "DEFAULT_LIMIT = 3\n"
        "class Settings:\n"
        "    ENABLED: bool = True\n"
    )

    for symbol in (
        "module.operating_system",
        "module.RepoPath",
        "module.DEFAULT_LIMIT",
        "module.Settings.ENABLED",
    ):
        assert mod._symbol_definition_exists(
            path="src/module.py",
            content=content,
            symbol=symbol,
        )
    assert not mod._symbol_definition_exists(
        path="src/module.py",
        content=content,
        symbol="module.MISSING",
    )


@pytest.mark.parametrize(
    ("path", "content", "symbol"),
    [
        (
            "config.json",
            '{"tool":{"with/slash":{"~key":true}}}',
            "config:/tool/with~1slash/~0key",
        ),
        (
            "pyproject.toml",
            '[tool.pytest.ini_options]\naddopts = "-q"\n',
            "config:/tool/pytest/ini_options/addopts",
        ),
        (
            "pipeline.yaml",
            "pipelines:\n  - name: primary\n",
            "config:/pipelines/0/name",
        ),
    ],
)
def test_inspected_symbol_supports_unambiguous_rfc6901_config_keys(
    path: str,
    content: str,
    symbol: str,
) -> None:
    assert mod._symbol_definition_exists(path=path, content=content, symbol=symbol)


@pytest.mark.parametrize(
    ("path", "content", "symbol"),
    [
        ("config.json", '{"tool":1,"tool":2}', "config:/tool"),
        ("config.yaml", "tool: 1\ntool: 2\n", "config:/tool"),
        ("config.json", '{"tool":{"value":1}}', "tool.value"),
        ("config.json", '{"tool":{"value":1}}', "config:/tool/~2value"),
    ],
)
def test_config_symbol_fails_closed_on_duplicates_or_ambiguous_syntax(
    path: str,
    content: str,
    symbol: str,
) -> None:
    assert not mod._symbol_definition_exists(path=path, content=content, symbol=symbol)


def test_replay_command_parser_rejects_shell_and_control_injection() -> None:
    assert mod._parse_replay_argv("pytest -q tests/test_core.py") == [
        "pytest",
        "-q",
        "tests/test_core.py",
    ]
    assert mod._parse_replay_argv("pdm run python -m pytest -q") == [
        "pdm",
        "run",
        "python",
        "-m",
        "pytest",
        "-q",
    ]
    assert mod._parse_replay_argv(r"python .usertest_research\route_contract_probe.py") == [
        "python",
        ".usertest_research/route_contract_probe.py",
    ]
    assert mod._parse_replay_argv(
        r"python .usertest_research\route_contract_probe.py "
        r"--out .usertest_research\observations\result.json"
    ) == [
        "python",
        ".usertest_research/route_contract_probe.py",
        "--out",
        ".usertest_research/observations/result.json",
    ]
    assert mod._parse_replay_argv(
        r"python .usertest_research\route_contract_probe.py "
        r"--out=.usertest_research\observations\result.json"
    ) == [
        "python",
        ".usertest_research/route_contract_probe.py",
        "--out=.usertest_research/observations/result.json",
    ]
    assert mod._parse_replay_argv(
        r'pdm run python ".usertest_research\route contract probe.py"'
    ) == [
        "pdm",
        "run",
        "python",
        ".usertest_research/route contract probe.py",
    ]
    assert mod._parse_replay_argv(
        r"pytest packages\runner_core\tests\test_codex_execpolicy.py"
    ) == [
        "pytest",
        "packages/runner_core/tests/test_codex_execpolicy.py",
    ]
    assert mod._parse_replay_argv(
        r"python -m pytest packages\runner_core\tests\test_codex_execpolicy.py"
    ) == [
        "python",
        "-m",
        "pytest",
        "packages/runner_core/tests/test_codex_execpolicy.py",
    ]
    assert mod._parse_replay_argv(
        r"python -m pytest tests\test_probe.py "
        r"--basetemp=.usertest_research\pytest_tmp "
        r"--junitxml=.usertest_research\pytest.xml"
    ) == [
        "python",
        "-m",
        "pytest",
        "tests/test_probe.py",
        "--basetemp=.usertest_research/pytest_tmp",
        "--junitxml=.usertest_research/pytest.xml",
    ]
    assert mod._parse_replay_argv(
        r"pytest tests\test_probe.py --basetemp .usertest_research\pytest_tmp"
    ) == [
        "pytest",
        "tests/test_probe.py",
        "--basetemp",
        ".usertest_research/pytest_tmp",
    ]
    assert mod._parse_replay_argv(
        r"pytest packages\runner_core\tests\test_x.py::test_path[param\value]"
    ) == [
        "pytest",
        r"packages/runner_core/tests/test_x.py::test_path[param\value]",
    ]
    assert mod._parse_replay_argv('pytest -q -k="foo or bar"') == [
        "pytest",
        "-q",
        "-k=foo or bar",
    ]
    assert mod._parse_replay_argv('python -m pytest --override-ini="addopts=-ra -q"') == [
        "python",
        "-m",
        "pytest",
        "--override-ini=addopts=-ra -q",
    ]
    for command in (
        "pytest -q\nWrite-Output forged",
        "pytest -q\r\nwhoami",
        "pytest -q; whoami",
        "pytest -q | whoami",
        "pytest -q && whoami",
        "pytest -q > forged.txt",
        "pytest -q `whoami`",
        r"pytest tests/foo\ bar.py",
    ):
        assert mod._parse_replay_argv(command) is None
    assert mod._replay_argv_is_workspace_confined(["pytest", "-q", "tests/test_core.py"])
    assert not mod._replay_argv_is_workspace_confined(
        ["pytest", "-q", "../../outside/test_payload.py"]
    )
    assert not mod._replay_argv_is_workspace_confined(["pytest", "--rootdir=C:\\outside"])
    for command in (
        r"python .usertest_research\..\outside.py",
        r"python C:\outside\probe.py",
        r"pytest ..\outside\test_probe.py",
        r"pytest C:\outside\test_probe.py",
        r"pytest C:outside\test_probe.py",
        r"pytest --rootdir=C:outside tests\test_probe.py",
        r"pytest --basetemp=\outside tests\test_probe.py",
    ):
        argv = mod._parse_replay_argv(command)
        assert argv is None or not mod._replay_argv_is_workspace_confined(argv)


def test_workspace_manifest_records_unreadable_regular_file_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readable = tmp_path / "readable.txt"
    unreadable = tmp_path / "unreadable.txt"
    readable.write_text("readable\n", encoding="utf-8")
    unreadable.write_text("unreadable\n", encoding="utf-8")
    real_sha256_path = mod._sha256_path

    def fake_sha256_path(path: Path) -> str:
        if path == unreadable:
            raise OSError(22, "invalid host filename")
        return real_sha256_path(path)

    monkeypatch.setattr(mod, "_sha256_path", fake_sha256_path)

    manifest = mod._workspace_manifest(tmp_path)

    assert manifest["readable.txt"]["kind"] == "file"
    assert manifest["unreadable.txt"] == {
        "kind": "unreadable_file",
        "mode": unreadable.stat().st_mode & 0o777,
        "size_bytes": unreadable.stat().st_size,
        "error": "OSError",
    }


def test_practical_config_cli_replay_proves_wrong_value_to_correct_value(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    tool = baseline / "tools" / "show_mode.py"
    tool.parent.mkdir()
    tool.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "print(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))['mode'])\n",
        encoding="utf-8",
    )
    (baseline / "bad.json").write_text('{"mode":"bad"}\n', encoding="utf-8")
    (baseline / "correct.json").write_text('{"mode":"correct"}\n', encoding="utf-8")
    revision = _baseline_repo_commit_existing(baseline, "practical config cli")
    dossier = {
        "inspected_files": ["tools/show_mode.py"],
        "experiments": [
            {
                "experiment_id": "support",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:config"],
                "command": "python tools/show_mode.py bad.json",
                "outcome": "supports",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "bad",
                },
            },
            {
                "experiment_id": "control",
                "scenario_kind": "control",
                "addresses_atom_ids": ["atom:config"],
                "command": "python tools/show_mode.py correct.json",
                "outcome": "refutes",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "correct",
                },
            },
        ],
    }
    assignment = {
        "atom_receipts": [
            {
                "atom_id": "atom:config",
                "atom_sha256": "a" * 64,
                "atom_snapshot": {
                    "atom_id": "atom:config",
                    "command": "python tools/show_mode.py bad.json",
                    "output_excerpt": "bad",
                },
            }
        ]
    }
    errors: list[str] = []

    receipts = mod._clean_replay_receipts(
        dossier,
        evidence_assignment=assignment,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=300.0,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=baseline,
        ),
    )

    assert errors == []
    assert receipts["support"]["command_authorization"]["authorization_kind"] == (
        "immutable_source_command"
    )
    assert receipts["support"]["command_authorization"]["origin_atom_field_path"] == ("$.command")
    assert receipts["control"]["command_authorization"]["authorization_kind"] == (
        "declared_inspected_repository_entrypoint"
    )
    difference_errors: list[str] = []
    difference = mod._observable_controlled_difference(
        hypothesis_id="h1",
        control_id="control",
        support=dossier["experiments"][0],
        control=dossier["experiments"][1],
        support_replay=receipts["support"],
        control_replay=receipts["control"],
        errors=difference_errors,
    )
    assert difference_errors == []
    assert difference is not None
    assert difference["difference_kind"] == "wrong_value_corrected"
    assert difference["support_expected_sha256"] == mod._canonical_json_sha256("bad")
    assert difference["control_expected_sha256"] == mod._canonical_json_sha256("correct")


def test_preexisting_repository_cli_is_hash_bound_without_language_whitelist(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    tool = baseline / "tools" / "show_mode.py"
    tool.parent.mkdir()
    tool.write_text("print('bad')\n", encoding="utf-8")
    revision = _baseline_repo_commit_existing(baseline, "unbound practical cli")
    dossier = {
        "experiments": [
            {
                "experiment_id": "unbound",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:other"],
                "command": "python tools/show_mode.py",
                "outcome": "supports",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "bad",
                },
            }
        ]
    }
    assignment = {
        "atom_receipts": [
            {
                "atom_id": "atom:other",
                "atom_snapshot": {"command": "python -m unrelated"},
            }
        ]
    }
    errors: list[str] = []

    receipts = mod._clean_replay_receipts(
        dossier,
        evidence_assignment=assignment,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=300.0,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=baseline,
        ),
    )

    assert errors == []
    assert receipts["unbound"]["command_authorization"]["authorization_kind"] == (
        "immutable_repository_entrypoint"
    )
    assert receipts["unbound"]["command_authorization"]["entrypoint_path"] == ("tools/show_mode.py")
    assert receipts["unbound"]["command_authorization"]["entrypoint_git_blob_sha"]


@pytest.mark.parametrize(
    ("manifest_name", "manifest_content", "command"),
    [
        ("Cargo.toml", "[package]\nname='depth-test'\nversion='0.1.0'\n", "cargo test"),
        ("go.mod", "module example.invalid/depth\n\ngo 1.23\n", "go test ./..."),
        ("pom.xml", "<project><modelVersion>4.0.0</modelVersion></project>\n", "mvn test"),
        ("build.gradle", "plugins { id 'java' }\n", "gradle test"),
        ("Depth.Tests.csproj", '<Project Sdk="Microsoft.NET.Sdk" />\n', "dotnet test"),
    ],
)
def test_repository_native_runner_authorization_uses_declared_tracked_bindings(
    tmp_path: Path,
    manifest_name: str,
    manifest_content: str,
    command: str,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / manifest_name).write_text(manifest_content, encoding="utf-8")
    _baseline_repo_commit_existing(workspace, f"add {manifest_name}")
    experiment = {
        "scenario_kind": "original_replay",
        "repository_bindings": [
            {
                "path": manifest_name,
                "relationship": "Tracked project manifest governing this exact runner command.",
            }
        ],
    }
    dossier = {"inspected_files": [manifest_name]}

    authorized = mod._authorized_replay_invocation(
        command=command,
        experiment=experiment,
        dossier=dossier,
        assignment={},
        workspace=workspace,
    )

    assert authorized is not None
    argv, receipt = authorized
    assert receipt["authorization_kind"] == "declared_repository_bindings"
    assert receipt["repository_bindings"][0]["path"] == manifest_name
    assert receipt["repository_bindings"][0]["git_blob_sha"]
    assert mod._command_authorization_attested(receipt, argv=argv)


def test_declared_bindings_preserve_attested_research_harness_entrypoint(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    source = workspace / "packages" / "runtime" / "src" / "runtime" / "backend.py"
    source.parent.mkdir(parents=True)
    source.write_text("def mechanism():\n    return 1\n", encoding="utf-8")
    _baseline_repo_commit_existing(workspace, "add production mechanism")
    harness = workspace / ".usertest_research" / "test_probe.py"
    harness.parent.mkdir()
    harness.write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    experiment = {
        "scenario_kind": "control",
        "repository_bindings": [
            {
                "path": "packages/runtime/src/runtime/backend.py",
                "relationship": "The harness imports the inspected production module.",
            }
        ],
    }
    dossier = {
        "inspected_files": ["packages/runtime/src/runtime/backend.py"],
        "artifact_refs": [
            {
                "artifact_id": "artifact:test-probe",
                "path": ".usertest_research/test_probe.py",
            }
        ],
    }

    authorized = mod._authorized_replay_invocation(
        command=(
            "python -B -m pytest -q "
            ".usertest_research/test_probe.py::test_probe"
        ),
        experiment=experiment,
        dossier=dossier,
        assignment={},
        workspace=workspace,
    )

    assert authorized is not None
    argv, receipt = authorized
    assert receipt["authorization_kind"] == "declared_repository_bindings"
    assert receipt["entrypoint_path"] == ".usertest_research/test_probe.py"
    assert receipt["entrypoint_sha256"] == mod._sha256_path(harness)
    assert receipt["artifact_id"] == "artifact:test-probe"
    assert mod._command_authorization_attested(receipt, argv=argv)


def test_declared_repository_binding_cannot_bypass_inspection_or_tracking(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    _baseline_repo_commit_existing(workspace, "tracked manifest")
    experiment = {
        "scenario_kind": "original_replay",
        "repository_bindings": [{"path": "Cargo.toml", "relationship": "governs the workspace"}],
    }

    assert (
        mod._authorized_replay_invocation(
            command="cargo test",
            experiment=experiment,
            dossier={"inspected_files": []},
            assignment={},
            workspace=workspace,
        )
        is None
    )


def test_git_attestation_helpers_do_not_impose_convenience_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def run(_argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[object]:
        calls.append(dict(kwargs))
        output = "a" * 40 + "\n"
        return subprocess.CompletedProcess(
            _argv,
            0,
            stdout=output if kwargs.get("text") is True else output.encode("utf-8"),
            stderr="" if kwargs.get("text") is True else b"",
        )

    monkeypatch.setattr(mod.subprocess, "run", run)

    assert mod._workspace_head(tmp_path) == "a" * 40
    assert mod._workspace_clean(tmp_path) is False
    assert mod._git_output_bytes(tmp_path, "status") == ("a" * 40 + "\n").encode()
    assert mod._git_blob_sha(tmp_path, "Cargo.toml") == "a" * 40
    assert calls
    assert all("timeout" not in kwargs for kwargs in calls)


def _runner_bound_atom_assignment(
    *, atom_id: str, atom_snapshot: dict[str, object]
) -> dict[str, object]:
    receipt = {
        "atom_id": atom_id,
        "atom_sha256": mod._canonical_json_sha256(atom_snapshot),
        "atom_snapshot": atom_snapshot,
    }
    assignment: dict[str, object] = {
        "status": "complete",
        "errors": [],
        "expected_atom_ids": [atom_id],
        "atom_receipts": [receipt],
    }
    assignment["assignment_sha256"] = mod.evidence_assignment_sha256(assignment)
    return assignment


def test_powershell_environment_adapter_runs_through_production_replay_and_oracle(
    tmp_path: Path,
) -> None:
    powershell = _required_powershell_executable()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    probe = baseline / "tools" / "environment_probe.ps1"
    probe.parent.mkdir()
    probe.write_text(
        "@{ mode = $env:BACKLOG_DEPTH_TEST_MODE } | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    revision = _baseline_repo_commit_existing(baseline, "powershell environment probe")
    atom_id = "atom:powershell-environment"
    command = f"{powershell} -NoProfile -File tools/environment_probe.ps1"
    assignment = _runner_bound_atom_assignment(
        atom_id=atom_id,
        atom_snapshot={
            "command": command,
            "exit_code": 0,
            "expected_mode": "ready",
            "text": "The signed-in host path should report ready when the mode is supplied.",
            "evidence_role": "observation",
            "origin_stage": "runtime",
        },
    )
    claim = {
        "adapter_id": "environment.v1",
        "hypothesis_id": "hypothesis:environment",
        "baseline_experiment_id": "experiment:without-mode",
        "challenge_experiment_id": "experiment:with-mode",
        "intervention": {
            "kind": "child_environment_variable",
            "target": "env:BACKLOG_DEPTH_TEST_MODE",
            "predicted_polarity": "missing_to_present",
            "before": None,
            "after": "ready",
        },
        "observations": {
            "baseline": {"source": "stdout_json", "json_pointer": "/mode"},
            "challenge": {"source": "stdout_json", "json_pointer": "/mode"},
        },
        "positive_outcome": {
            "predicate": {"kind": "equals", "expected": "ready"},
            "semantic_basis": {
                "kind": "origin_exact_value",
                "atom_id": atom_id,
                "field_path": "$.expected_mode",
            },
        },
        "implementation_touchpoints": [
            {
                "causal_locator": "env:BACKLOG_DEPTH_TEST_MODE",
                "path": "tools/environment_probe.ps1",
                "symbols": [],
                "relationship": (
                    "This inspected production entrypoint reads the controlled child variable."
                ),
            }
        ],
    }
    experiments = [
        {
            "experiment_id": "experiment:without-mode",
            "scenario_kind": "runtime_environment_absent",
            "addresses_atom_ids": [atom_id],
            "command": command,
            "result": "The child process reports no mode.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"environment": {"BACKLOG_DEPTH_TEST_MODE": None}},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": '"mode":null',
            },
            "verification_boundary": {
                "boundary_kind": "isolated_child_environment_equivalence",
                "requires_live_verification": False,
                "faithful_equivalence": True,
                "rationale": (
                    "The isolated replay executes the same tracked entrypoint with the exact "
                    "controlled child input and evaluates the original positive predicate."
                ),
            },
            "artifact_refs": [],
        },
        {
            "experiment_id": "experiment:with-mode",
            "scenario_kind": "runtime_environment_present",
            "addresses_atom_ids": [atom_id],
            "command": command,
            "result": "The child process reports the controlled mode.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"environment": {"BACKLOG_DEPTH_TEST_MODE": "ready"}},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": '"mode":"ready"',
            },
            "artifact_refs": [],
            "proof_adapter": claim,
        },
    ]
    dossier: dict[str, object] = {
        "case_id": "case:powershell-environment",
        "problem_id": "problem:powershell-environment",
        "repo_revision": revision,
        "experiments": experiments,
        "inspected_files": ["tools/environment_probe.ps1"],
        "artifact_refs": [],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:environment",
                "statement": "The child environment controls the observed mode.",
                "mechanism_symbols": ["env:BACKLOG_DEPTH_TEST_MODE"],
                "supporting_evidence": [
                    "experiment:without-mode",
                    "experiment:with-mode",
                ],
                "counterevidence": [],
                "falsification_attempts": [],
                "disposition": "primary",
            }
        ],
    }
    replay_errors: list[str] = []
    replays = mod._clean_replay_receipts(
        dossier,
        evidence_assignment=assignment,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=None,
        errors=replay_errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=baseline,
        ),
    )
    assert replay_errors == []
    assert set(replays) == {"experiment:without-mode", "experiment:with-mode"}
    assert all(
        replay["command_authorization"]["authorization_kind"]
        in {"immutable_source_command", "immutable_repository_entrypoint"}
        for replay in replays.values()
    )
    atom_receipt = assignment["atom_receipts"][0]
    atom_bindings = [
        {
            "experiment_id": experiment_id,
            "atom_id": atom_id,
            "match_kind": "adapter_declared_symptom",
            "origin_atom_sha256": atom_receipt["atom_sha256"],
        }
        for experiment_id in replays
    ]
    experiment_index = {str(experiment["experiment_id"]): experiment for experiment in experiments}
    probe_sha256 = mod.sha256(probe.read_bytes()).hexdigest()
    inspected_file_receipts = [
        {
            "path": "tools/environment_probe.ps1",
            "sha256": probe_sha256,
            "whole_file_observed": True,
            "observed_content_sha256": probe_sha256,
        }
    ]
    proofs, diagnostics = mod._proof_adapter_receipts(
        dossier,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        experiments=experiment_index,
        clean_replays=replays,
        evidence_assignment=assignment,
        atom_bindings=atom_bindings,
        planning_workspace=baseline,
        symbol_receipts=[],
        artifact_receipts=[],
        inspected_file_receipts=inspected_file_receipts,
    )
    assert diagnostics == []
    assert len(proofs) == 1
    assert proofs[0]["adapter_id"] == "environment.v1"
    assert proofs[0]["positive_outcome"]["passed"] is True
    touchpoint = proofs[0]["adapter_evidence"]["implementation_touchpoints"][0]
    assert touchpoint == {
        "touchpoint_id": f"implementation_touchpoint:{touchpoint['evidence_sha256']}",
        "causal_locator": "env:BACKLOG_DEPTH_TEST_MODE",
        "path": "tools/environment_probe.ps1",
        "symbols": [],
        "relationship": (
            "This inspected production entrypoint reads the controlled child variable."
        ),
        "runner_attested": True,
        "inspected_content_sha256": probe_sha256,
        "evidence_sha256": touchpoint["evidence_sha256"],
    }
    first_consumer = mod._adapter_executed_consumer_receipt(
        proofs[0],
        clean_replays=replays,
        implementation_touchpoints=[touchpoint],
    )
    repeated_consumer = mod._adapter_executed_consumer_receipt(
        proofs[0],
        clean_replays=replays,
        implementation_touchpoints=[touchpoint],
    )
    assert first_consumer == repeated_consumer

    second_replays = deepcopy(replays)
    for replay in second_replays.values():
        argv = [
            (
                "tools/environment_probe_secondary.ps1"
                if argument == "tools/environment_probe.ps1"
                else argument
            )
            for argument in replay["executed_argv"]
        ]
        authorization = replay["command_authorization"]
        replay["executed_argv"] = argv
        replay["command_authorization"] = mod._command_authorization_receipt(
            {
                **{
                    key: value
                    for key, value in authorization.items()
                    if key not in {"authorization_sha256", "runner_attested"}
                },
                "executed_argv_sha256": mod._canonical_json_sha256(argv),
                "entrypoint_path": "tools/environment_probe_secondary.ps1",
                "entrypoint_sha256": "6" * 64,
                "entrypoint_git_blob_sha": "7" * 40,
            }
        )
    second_touchpoint_projection = {
        key: value
        for key, value in touchpoint.items()
        if key not in {"touchpoint_id", "evidence_sha256"}
    }
    second_touchpoint_projection["path"] = "tools/environment_probe_secondary.ps1"
    second_touchpoint_projection["inspected_content_sha256"] = "6" * 64
    second_touchpoint_hash = mod._canonical_json_sha256(second_touchpoint_projection)
    second_touchpoint = {
        "touchpoint_id": f"implementation_touchpoint:{second_touchpoint_hash}",
        **second_touchpoint_projection,
        "evidence_sha256": second_touchpoint_hash,
    }
    second_consumer = mod._adapter_executed_consumer_receipt(
        proofs[0],
        clean_replays=second_replays,
        implementation_touchpoints=[second_touchpoint],
    )
    assert first_consumer is not None
    assert second_consumer is not None
    assert first_consumer["causal_target"] == second_consumer["causal_target"]
    assert first_consumer["consumer_identity"] != second_consumer["consumer_identity"]
    proofs_with_ancillary, ancillary_diagnostics = mod._proof_adapter_receipts(
        dossier,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        experiments={
            **experiment_index,
            "experiment:ancillary-invalid": {
                "experiment_id": "experiment:ancillary-invalid",
                "proof_adapter": {**claim, "adapter_id": "vendor.unregistered.v1"},
            },
        },
        clean_replays=replays,
        evidence_assignment=assignment,
        atom_bindings=atom_bindings,
        planning_workspace=baseline,
        symbol_receipts=[],
        artifact_receipts=[],
        inspected_file_receipts=inspected_file_receipts,
    )
    assert proofs_with_ancillary == proofs
    assert ancillary_diagnostics == [
        {
            "experiment_id": "experiment:ancillary-invalid",
            "adapter_id": "vendor.unregistered.v1",
            "claim_sha256": mod._canonical_json_sha256(
                {**claim, "adapter_id": "vendor.unregistered.v1"}
            ),
            "diagnostics": ["proof_adapter_unavailable:vendor.unregistered.v1"],
        }
    ]
    invalid_touchpoint_claim = {
        **claim,
        "implementation_touchpoints": [
            {
                "causal_locator": "env:BACKLOG_DEPTH_TEST_MODE",
                "path": ".usertest_research/probe.ps1",
                "symbols": [],
                "relationship": "A research harness is not a production change surface.",
            }
        ],
    }
    invalid_proofs, invalid_touchpoint_diagnostics = mod._proof_adapter_receipts(
        dossier,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        experiments={
            **experiment_index,
            "experiment:with-mode": {
                **experiment_index["experiment:with-mode"],
                "proof_adapter": invalid_touchpoint_claim,
            },
        },
        clean_replays=replays,
        evidence_assignment=assignment,
        atom_bindings=atom_bindings,
        planning_workspace=baseline,
        symbol_receipts=[],
        artifact_receipts=[],
        inspected_file_receipts=inspected_file_receipts,
    )
    assert len(invalid_proofs) == 1
    assert "implementation_touchpoints" not in invalid_proofs[0]["adapter_evidence"]
    assert invalid_touchpoint_diagnostics[0]["diagnostics"] == [
        "proof_adapter_implementation_touchpoint_invalid:0"
    ]
    mechanism_errors: list[str] = []
    mechanism = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=[],
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        proof_adapter_receipts=proofs,
        atom_bindings=atom_bindings,
        errors=mechanism_errors,
    )
    assert mechanism_errors == []
    assert len(mechanism) == 1
    assert mechanism[0]["implementation_touchpoints"] == [touchpoint]
    selected = mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=mechanism,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    )
    assert selected[0] is not None
    oracle_errors: list[str] = []
    oracles = mod._outcome_oracle_receipts(
        dossier,
        clean_replays=replays,
        mechanism_evidence=mechanism,
        proof_adapter_receipts=proofs,
        verified_mechanism=selected[0],
        verified_mechanism_sha256=selected[1],
        verified_mechanism_provenance=selected[2],
        verified_mechanism_provenance_sha256=selected[3],
        control_verifications=[],
        falsification_interventions=[],
        inspected_file_receipts=[],
        inspected_symbol_receipts=[],
        evidence_assignment=assignment,
        atom_bindings=atom_bindings,
        planning_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        run_dir=tmp_path / "runs" / "environment",
        repo_revision=revision,
        errors=oracle_errors,
    )
    assert oracle_errors == []
    assert len(oracles) == 1
    assert oracles[0]["kind"] == "causal_proof_replay"
    assert oracles[0]["execution"]["replay_inputs"] == proofs[0]["replay_inputs"]
    assert oracles[0]["execution"]["replay_observation"] == proofs[0]["replay_observation"]
    contract = oracles[0]["positive_outcome_contracts"][0]
    assert contract["kind"] == "causal_proof_predicate"
    assert contract["postconditions"][0]["predicate"] == {
        "kind": "equals",
        "expected": "ready",
    }
    contrast_proof = deepcopy(proofs[0])
    contrast_proof["positive_outcome"]["contract_role"] = "causal_contrast"
    contrast_proof["proof_receipt_id"] = mod.proof_receipt_id_for(contrast_proof)
    contrast_evidence = deepcopy(mechanism[0])
    contrast_evidence["proof_receipt_id"] = contrast_proof["proof_receipt_id"]
    contrast_evidence["mechanism_evidence_id"] = "mechanism_evidence:contrast"
    assert (
        mod._causal_proof_positive_contract(
            experiment_id="experiment:without-mode",
            evidence=[contrast_evidence],
            proof_receipt=contrast_proof,
        )
        is None
    )
    boundary_errors: list[str] = []
    boundaries, boundary_errors = mod._verification_boundary_receipts(
        experiments=experiment_index,
        clean_replays=replays,
        mechanism_evidence=mechanism,
        proof_adapter_receipts=proofs,
        outcome_oracles=oracles,
        verified_mechanism_provenance=selected[2],
    )
    assert boundary_errors == []
    assert len(boundaries) == 1
    assert boundaries[0]["experiment_id"] == "experiment:without-mode"
    assert boundaries[0]["requires_live_verification"] is False
    assert boundaries[0]["faithful_equivalence"] is True
    equivalence = boundaries[0]["equivalence_proof"]
    assert equivalence["source_experiment_id"] == "experiment:without-mode"
    assert equivalence["origin_atom_ids"] == [atom_id]
    assert equivalence["proof_receipt_id"] == proofs[0]["proof_receipt_id"]
    assert equivalence["replay_inputs_sha256"] == proofs[0]["replay_inputs"]["replay_inputs_sha256"]
    assert (
        equivalence["replay_observation_sha256"]
        == proofs[0]["replay_observation"]["replay_observation_sha256"]
    )
    assert mechanism[0]["mechanism_evidence_id"] in boundaries[0]["provenance_refs"]
    assert oracles[0]["outcome_oracle_id"] in boundaries[0]["provenance_refs"]
    assert boundaries[0]["boundary_sha256"] == mod._canonical_json_sha256(
        {key: value for key, value in boundaries[0].items() if key != "boundary_sha256"}
    )
    rejected, rejected_errors = mod._verification_boundary_receipts(
        experiments=experiment_index,
        clean_replays=replays,
        mechanism_evidence=mechanism,
        proof_adapter_receipts=proofs,
        outcome_oracles=[],
        verified_mechanism_provenance=selected[2],
    )
    assert rejected == []
    assert rejected_errors == [
        "verification_boundary_invalid:experiment:without-mode:faithful_equivalence_unattested"
    ]
    source_replay = dict(replays["experiment:without-mode"])
    source_authorization = source_replay["command_authorization"]
    source_replay["command_authorization"] = mod._command_authorization_receipt(
        {
            key: value
            for key, value in source_authorization.items()
            if key
            not in {
                "authorization_sha256",
                "runner_attested",
                "origin_atom_id",
                "origin_atom_sha256",
                "origin_atom_field_path",
                "origin_command_value_sha256",
            }
        }
    )
    no_identity, no_identity_errors = mod._verification_boundary_receipts(
        experiments=experiment_index,
        clean_replays={**replays, "experiment:without-mode": source_replay},
        mechanism_evidence=mechanism,
        proof_adapter_receipts=proofs,
        outcome_oracles=oracles,
        verified_mechanism_provenance=selected[2],
    )
    assert no_identity == []
    assert no_identity_errors == [
        "verification_boundary_invalid:experiment:without-mode:faithful_equivalence_unattested"
    ]
    verification = {
        "experiments": list(replays.values()),
        "mechanism_evidence": mechanism,
        "proof_adapter_receipts": proofs,
        "verified_mechanism": selected[0],
        "verified_mechanism_sha256": selected[1],
        "verified_mechanism_provenance": selected[2],
        "verified_mechanism_provenance_sha256": selected[3],
        "outcome_oracles": oracles,
        "inspected_files": [],
        "inspected_symbols": [],
        "falsification_interventions": [],
        "atom_bindings": atom_bindings,
    }
    dossier["evidence_assignment"] = assignment
    assert (
        stage_contracts._validate_outcome_oracles(
            dossier,
            verification,
            pid=str(dossier["problem_id"]),
        )
        == []
    )


def test_filesystem_adapter_attests_disposable_state_without_tracked_mutation(
    tmp_path: Path,
) -> None:
    powershell = _required_powershell_executable()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    probe = baseline / "tools" / "state_probe.ps1"
    probe.parent.mkdir()
    probe.write_text(
        "param([string]$Mode)\n"
        "$path = Join-Path (Get-Location) 'tmp/state.json'\n"
        "if ($Mode -eq 'create') {\n"
        "  New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null\n"
        "  '{\"ready\":true}' | Set-Content -NoNewline $path\n"
        "}\n"
        "if (Test-Path $path) { 'present' } else { 'absent' }\n",
        encoding="utf-8",
    )
    revision = _baseline_repo_commit_existing(baseline, "powershell state probe")
    atom_id = "atom:filesystem-state"
    baseline_command = f"{powershell} -NoProfile -File tools/state_probe.ps1 -Mode absent"
    challenge_command = f"{powershell} -NoProfile -File tools/state_probe.ps1 -Mode create"
    assignment = _runner_bound_atom_assignment(
        atom_id=atom_id,
        atom_snapshot={
            "command": baseline_command,
            "expected_exists": True,
            "text": "The original scenario requires the state artifact to be created.",
            "evidence_role": "observation",
            "origin_stage": "runtime",
        },
    )
    claim = {
        "adapter_id": "filesystem_state.v1",
        "hypothesis_id": "hypothesis:filesystem",
        "baseline_experiment_id": "experiment:absent",
        "challenge_experiment_id": "experiment:created",
        "intervention": {
            "kind": "disposable_workspace_state",
            "target": "fs:tmp/state.json",
            "predicted_polarity": "absent_to_present",
            "before": False,
            "after": True,
        },
        "state_inputs": {
            "observation_kind": "existence",
            "baseline_path": "tmp/state.json",
            "challenge_path": "tmp/state.json",
        },
        "positive_outcome": {
            "predicate": {"kind": "existence", "expected": True},
            "semantic_basis": {
                "kind": "origin_exact_value",
                "atom_id": atom_id,
                "field_path": "$.expected_exists",
            },
        },
    }
    experiments = [
        {
            "experiment_id": "experiment:absent",
            "scenario_kind": "disposable_state_absent",
            "addresses_atom_ids": [atom_id],
            "command": baseline_command,
            "result": "The state file is absent.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"disposable_state_paths": ["tmp/state.json"]},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": "absent",
            },
            "artifact_refs": [],
        },
        {
            "experiment_id": "experiment:created",
            "scenario_kind": "disposable_state_created",
            "addresses_atom_ids": [atom_id],
            "command": challenge_command,
            "result": "The state file is created.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"disposable_state_paths": ["tmp/state.json"]},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": "present",
            },
            "artifact_refs": [],
            "proof_adapter": claim,
        },
    ]
    dossier = {
        "case_id": "case:filesystem-state",
        "problem_id": "problem:filesystem-state",
        "experiments": experiments,
        "artifact_refs": [],
        "inspected_files": [],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:filesystem",
                "mechanism_symbols": ["fs:tmp/state.json"],
            }
        ],
    }
    errors: list[str] = []
    replays = mod._clean_replay_receipts(
        dossier,
        evidence_assignment=assignment,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=None,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=baseline,
        ),
    )
    assert errors == []
    assert replays["experiment:absent"]["post_replay_mutations"] is False
    assert replays["experiment:created"]["post_replay_mutations"] is True
    assert replays["experiment:created"]["undeclared_post_replay_mutations"] == []
    assert replays["experiment:created"]["declared_state_transitions"]
    atom_receipt = assignment["atom_receipts"][0]
    bindings = [
        {
            "experiment_id": experiment_id,
            "atom_id": atom_id,
            "match_kind": "adapter_declared_symptom",
            "origin_atom_sha256": atom_receipt["atom_sha256"],
        }
        for experiment_id in replays
    ]
    proofs, diagnostics = mod._proof_adapter_receipts(
        dossier,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        experiments={str(item["experiment_id"]): item for item in experiments},
        clean_replays=replays,
        evidence_assignment=assignment,
        atom_bindings=bindings,
        planning_workspace=baseline,
        symbol_receipts=[],
        artifact_receipts=[],
    )
    assert diagnostics == []
    assert len(proofs) == 1
    assert proofs[0]["adapter_id"] == "filesystem_state.v1"
    assert proofs[0]["positive_outcome"]["observed"] == {"exists": True}


def test_exact_origin_scenario_can_attest_equivalence_without_redundant_adapter() -> None:
    experiment_id = "experiment:exact-original"
    atom_id = "atom:exact-original"
    argv = ["pytest", "tests/test_exact.py::test_original"]
    authorization = mod._command_authorization_receipt(
        {
            "authorization_kind": "immutable_source_command",
            "executed_argv_sha256": mod._canonical_json_sha256(argv),
            "shell": False,
            "workspace_confined": True,
            "origin_atom_id": atom_id,
            "origin_atom_sha256": "a" * 64,
            "origin_atom_field_path": "$.command",
            "origin_command_value_sha256": "b" * 64,
        }
    )
    replay_inputs = mod._replay_inputs_receipt(
        source_experiment_id=experiment_id,
        environment_overrides={},
        disposable_state_paths=[],
    )
    replay = {
        "experiment_id": experiment_id,
        "executed_argv": argv,
        "command_authorization": authorization,
        "assertion_passed": True,
        "exit_code": 1,
        "stdout_sha256": "c" * 64,
        "stderr_sha256": "d" * 64,
        "execution_isolation": {"executor": "docker", "network": "none"},
        "replay_inputs": replay_inputs,
    }
    contract = {
        "schema_version": 1,
        "kind": "repository_test_assertion",
        "postconditions": [{"type": "command_exit_code", "command_index": 0, "equals": 0}],
    }
    contract["positive_outcome_contract_id"] = mod._content_addressed_receipt_id(
        "positive_outcome_contract",
        contract,
        "positive_outcome_contract_id",
    )
    replay_observation = mod._exact_original_replay_observation(
        experiment_id=experiment_id,
        replay=replay,
        positive_outcome_contracts=[contract],
    )
    assert replay_observation is not None
    oracle = {
        "schema_version": 1,
        "research_experiment_id": experiment_id,
        "kind": "staged_replay",
        "execution": {
            "argv": argv,
            "command_authorization": authorization,
            "replay_inputs": replay_inputs,
            "replay_observation": replay_observation,
        },
        "positive_outcome_contracts": [contract],
    }
    oracle["outcome_oracle_id"] = mod._content_addressed_receipt_id(
        "outcome_oracle",
        oracle,
        "outcome_oracle_id",
    )
    mechanism_id = "mechanism_evidence:exact-original"
    mechanism = {
        "mechanism_evidence_id": mechanism_id,
        "experiment_ids": [experiment_id],
        "origin_atom_ids": [atom_id],
    }
    experiments = {
        experiment_id: {
            "experiment_id": experiment_id,
            "verification_boundary": {
                "boundary_kind": "repository_original_scenario",
                "requires_live_verification": False,
                "faithful_equivalence": True,
                "rationale": "The exact source command is the original local scenario.",
            },
        }
    }

    boundaries, errors = mod._verification_boundary_receipts(
        experiments=experiments,
        clean_replays={experiment_id: replay},
        mechanism_evidence=[mechanism],
        proof_adapter_receipts=[],
        outcome_oracles=[oracle],
        verified_mechanism_provenance={"mechanism_evidence_ids": [mechanism_id]},
    )

    assert errors == []
    assert len(boundaries) == 1
    equivalence = boundaries[0]["equivalence_proof"]
    assert equivalence["equivalence_mode"] == "exact_origin_scenario_identity"
    assert equivalence["source_identity"]["origin_atom_id"] == atom_id
    assert equivalence["positive_outcome_contract_ids"] == [
        contract["positive_outcome_contract_id"]
    ]

    tampered_observation = dict(replay_observation)
    tampered_observation["selector"] = {"source": "stderr_text"}
    tampered_oracle = {
        **oracle,
        "execution": {**oracle["execution"], "replay_observation": tampered_observation},
    }
    rejected, rejected_errors = mod._verification_boundary_receipts(
        experiments=experiments,
        clean_replays={experiment_id: replay},
        mechanism_evidence=[mechanism],
        proof_adapter_receipts=[],
        outcome_oracles=[tampered_oracle],
        verified_mechanism_provenance={"mechanism_evidence_ids": [mechanism_id]},
    )
    assert rejected == []
    assert rejected_errors == [
        f"verification_boundary_invalid:{experiment_id}:faithful_equivalence_unattested"
    ]


def test_top_level_verifier_dispatches_powershell_adapter_and_persists_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "verify_controlled_codex_execpolicy_receipt", lambda _path: [])
    powershell = _required_powershell_executable()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probe = workspace / "tools" / "environment_probe.ps1"
    probe.parent.mkdir()
    probe.write_text(
        "@{ mode = $env:BACKLOG_DEPTH_VERIFY_MODE } | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    revision = _baseline_repo_commit_existing(workspace, "top-level powershell proof")
    command = f"{powershell} -NoProfile -File tools/environment_probe.ps1"
    atom_id = "atom:top-level-powershell"
    atom = {
        "atom_id": atom_id,
        "command": command,
        "exit_code": 0,
        "expected_mode": "ready",
        "text": (
            'The observed states are {"mode":null} and {"mode":"ready"}; '
            "the required mode is ready."
        ),
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    assignment = _runner_bound_atom_assignment(atom_id=atom_id, atom_snapshot=atom)
    assignment.update(
        case_id="case:top-level-powershell",
        problem_id="problem:top-level-powershell",
    )
    assignment["atom_receipts"][0]["origin_evidence_mode"] = "signed_snapshot"
    assignment["assignment_sha256"] = mod.evidence_assignment_sha256(assignment)
    claim = {
        "adapter_id": "environment.v1",
        "hypothesis_id": "hypothesis:top-level-environment",
        "baseline_experiment_id": "experiment:mode-absent",
        "challenge_experiment_id": "experiment:mode-ready",
        "intervention": {
            "kind": "child_environment_variable",
            "target": "env:BACKLOG_DEPTH_VERIFY_MODE",
            "predicted_polarity": "absent_to_ready",
            "before": None,
            "after": "ready",
        },
        "observations": {
            "baseline": {"source": "stdout_json", "json_pointer": "/mode"},
            "challenge": {"source": "stdout_json", "json_pointer": "/mode"},
        },
        "positive_outcome": {
            "predicate": {"kind": "equals", "expected": "ready"},
            "semantic_basis": {
                "kind": "origin_exact_value",
                "atom_id": atom_id,
                "field_path": "$.expected_mode",
            },
        },
        "implementation_touchpoints": [
            {
                "causal_locator": "env:BACKLOG_DEPTH_VERIFY_MODE",
                "path": "tools/environment_probe.ps1",
                "symbols": [],
                "relationship": (
                    "This inspected production entrypoint consumes the controlled mode."
                ),
            }
        ],
    }
    experiments = [
        {
            "experiment_id": "experiment:mode-absent",
            "scenario_kind": "environment_mode_absent",
            "addresses_atom_ids": [atom_id],
            "command": command,
            "result": "The clean child reports a null mode.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"environment": {"BACKLOG_DEPTH_VERIFY_MODE": None}},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": '{"mode":null}',
            },
            "verification_boundary": {
                "boundary_kind": "isolated_child_environment_equivalence",
                "requires_live_verification": False,
                "faithful_equivalence": True,
                "rationale": (
                    "The isolated replay preserves the tracked entrypoint, controlled input, "
                    "and runner-evaluated positive predicate."
                ),
            },
            "artifact_refs": [],
        },
        {
            "experiment_id": "experiment:mode-ready",
            "scenario_kind": "environment_mode_ready",
            "addresses_atom_ids": [atom_id],
            "command": command,
            "result": "The controlled child reports ready.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"environment": {"BACKLOG_DEPTH_VERIFY_MODE": "ready"}},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": '{"mode":"ready"}',
            },
            "artifact_refs": [],
            "proof_adapter": claim,
        },
    ]
    statement = "The child environment value controls the emitted mode."
    dossier: dict[str, object] = {
        "research_schema_version": 3,
        "case_id": "case:top-level-powershell",
        "problem_id": "problem:top-level-powershell",
        "repo_revision": revision,
        "research_method": "runner_registered_environment_adapter",
        "reproduction_status": "reproduced",
        "research_status": "evidence_sufficient",
        "writes_used": False,
        "writes_purpose": ["none"],
        "implementation_performed": False,
        "diff_classification": "no_changes",
        "artifact_refs": [],
        "experiments": experiments,
        "inspected_files": ["tools/environment_probe.ps1"],
        "inspected_symbols": [],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:top-level-environment",
                "statement": statement,
                "supporting_evidence": [
                    "experiment:mode-absent",
                    "experiment:mode-ready",
                ],
                "counterevidence": [],
                "mechanism_symbols": ["env:BACKLOG_DEPTH_VERIFY_MODE"],
                "disposition": "primary",
                "disposition_evidence": ["experiment:mode-ready"],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:environment-does-not-control-output",
                        "hypothesis_id": "hypothesis:top-level-environment",
                        "claim": statement,
                        "baseline_experiment_id": "experiment:mode-absent",
                        "challenge_experiment_id": "experiment:mode-ready",
                        "disproof_condition": {
                            "source": "stdout",
                            "operator": "not_contains",
                            "expected": '{"mode":"ready"}',
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
        "root_cause_confidence": 0.51,
        "broader_class_assessment": "unknown",
        "case_relation_assessment": {
            "disposition": "retain",
            "rationale": "The signed occurrence and verified environment mechanism are one unit.",
            "facets": [],
            "material_unknowns": [],
        },
        "material_unknowns": [],
        "blocking_reasons": [],
        "evidence_boundaries": [],
        "evidence_assignment": assignment,
    }
    run_dir = tmp_path / "research-run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps({"schema_version": 1, "kind": "troubleshoot_v1", "status": "success"}),
        encoding="utf-8",
    )
    (run_dir / "workspace_ref.json").write_text(
        json.dumps({"workspace_dir": str(workspace)}),
        encoding="utf-8",
    )
    (run_dir / "target_ref.json").write_text(
        json.dumps({"ref": revision, "commit_sha": revision, "agent": "claude"}),
        encoding="utf-8",
    )
    dossier["artifact_refs"] = [
        {
            "artifact_id": "runner:target_ref",
            "kind": "runner_provenance",
            "path": str(run_dir / "target_ref.json"),
        }
    ]
    observed_probe = probe.read_text(encoding="utf-8")
    events = [
        {"type": "run_command", "data": {"command": command, "exit_code": 0}},
        {"type": "run_command", "data": {"command": command, "exit_code": 0}},
        {
            "type": "read_file",
            "data": {
                "path": "tools/environment_probe.ps1",
                "bytes": probe.stat().st_size,
                "read_source": "tool",
                "source_exit_code": 0,
                **observed_read_attestation(
                    path=probe,
                    observed_text=observed_probe,
                    source_exit_code=0,
                    allow_partial=False,
                ),
            },
        },
    ]
    (run_dir / "normalized_events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    unverified_dossier = deepcopy(dossier)
    receipt = mod.verify_research_evidence(
        dossier,
        run_dir=run_dir,
        repo_revision=revision,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        expected_case_id=str(dossier["case_id"]),
        expected_problem_id=str(dossier["problem_id"]),
        evidence_assignment=assignment,
        evidence_atom_ids=[atom_id],
        revision_view_destination=tmp_path / "revision-view",
        replay_timeout_seconds=None,
        requested_repo_ref=revision,
        resolved_repo_ref=revision,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=workspace,
        ),
    )
    first_persisted_dossier = deepcopy(dossier)
    first_persisted_dossier["evidence_verification"] = receipt
    repeated_receipt = mod.verify_research_evidence(
        dossier,
        run_dir=run_dir,
        repo_revision=revision,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        expected_case_id=str(dossier["case_id"]),
        expected_problem_id=str(dossier["problem_id"]),
        evidence_assignment=assignment,
        evidence_atom_ids=[atom_id],
        revision_view_destination=tmp_path / "revision-view",
        replay_timeout_seconds=None,
        requested_repo_ref=revision,
        resolved_repo_ref=revision,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=workspace,
        ),
    )
    source_session_id = "55555555-5555-4555-8555-555555555555"
    source_run_dir = tmp_path / "research-source-run"
    source_attempt = _event_source_attempt(
        run_dir=source_run_dir,
        workspace=workspace,
        revision=revision,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        session_id=source_session_id,
        events=events,
    )
    correction_run_dir = tmp_path / "research-correction-run"
    correction_run_dir.mkdir()
    for filename in ("report.json", "workspace_ref.json", "target_ref.json"):
        (correction_run_dir / filename).write_bytes((run_dir / filename).read_bytes())
    _write_normalized_events(correction_run_dir / "normalized_events.jsonl", [])
    (correction_run_dir / "target_ref.json").write_text(
        json.dumps(
            {
                "ref": revision,
                "commit_sha": revision,
                "agent": "codex",
                "requested_codex_resume_session_id": source_session_id,
            }
        ),
        encoding="utf-8",
    )
    (correction_run_dir / "codex_execpolicy_overlay.json").write_text("{}\n", encoding="utf-8")
    correction_dossier = deepcopy(dossier)
    correction_dossier["research_attempts"] = [source_attempt]
    correction_artifact_refs = correction_dossier["artifact_refs"]
    assert isinstance(correction_artifact_refs, list)
    for artifact_ref in correction_artifact_refs:
        assert isinstance(artifact_ref, dict)
        if artifact_ref.get("artifact_id") == "runner:target_ref":
            artifact_ref["path"] = str(correction_run_dir / "target_ref.json")
    correction_artifact_refs.append(
        {
            "artifact_id": "runner:codex_subscription_auth",
            "kind": "codex_subscription_auth",
            "path": str(correction_run_dir / "codex_execpolicy_overlay.json"),
        }
    )
    correction_receipt = mod.verify_research_evidence(
        correction_dossier,
        run_dir=correction_run_dir,
        repo_revision=revision,
        case_id=str(correction_dossier["case_id"]),
        problem_id=str(correction_dossier["problem_id"]),
        expected_case_id=str(correction_dossier["case_id"]),
        expected_problem_id=str(correction_dossier["problem_id"]),
        evidence_assignment=assignment,
        evidence_atom_ids=[atom_id],
        revision_view_destination=tmp_path / "revision-view-correction",
        replay_timeout_seconds=None,
        requested_repo_ref=revision,
        resolved_repo_ref=revision,
        evidence_attempts=[source_attempt],
        evidence_agent_session_id=source_session_id,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=workspace,
        ),
    )
    dossier["evidence_verification"] = repeated_receipt

    assert receipt["status"] == "verified", receipt["errors"]
    assert repeated_receipt["status"] == "verified", repeated_receipt["errors"]
    assert correction_receipt["status"] == "verified", correction_receipt["errors"]
    assert [source["run_dir"] for source in correction_receipt["evidence_event_sources"]] == [
        str(source_run_dir.resolve()),
        str(correction_run_dir.resolve()),
    ]
    assert (
        correction_receipt["evidence_event_sources"][0]["attempt_sha256"]
        == source_attempt["attempt_sha256"]
    )
    assert correction_receipt["evidence_source_attempts"] == [source_attempt]
    assert correction_receipt["evidence_source_attempts_sha256"] == (
        mod._canonical_json_sha256([source_attempt])
    )
    assert correction_receipt["evidence_event_sources"][-1]["event_count"] == 0
    correction_dossier["evidence_verification"] = correction_receipt
    correction_persisted_valid, correction_persisted_errors = (
        mod.verify_persisted_research_evidence(correction_dossier)
    )
    assert correction_persisted_errors == []
    assert correction_persisted_valid is True
    first_replay_paths = {
        str(artifact["path"])
        for artifact in receipt["artifacts"]
        if str(artifact.get("artifact_id", "")).startswith("runner:replay:")
    }
    repeated_replay_paths = {
        str(artifact["path"])
        for artifact in repeated_receipt["artifacts"]
        if str(artifact.get("artifact_id", "")).startswith("runner:replay:")
    }
    assert first_replay_paths.isdisjoint(repeated_replay_paths)
    first_persisted_valid, first_persisted_errors = mod.verify_persisted_research_evidence(
        first_persisted_dossier
    )
    assert first_persisted_errors == []
    assert first_persisted_valid is True
    assert len(receipt["proof_adapter_receipts"]) == 1
    assert receipt["proof_adapter_receipts"][0]["adapter_id"] == "environment.v1"
    assert len(receipt["outcome_oracles"]) == 1
    assert receipt["outcome_oracles"][0]["kind"] == "causal_proof_replay"
    assert len(receipt["verification_boundaries"]) == 1
    assert receipt["verification_boundaries"][0]["requires_live_verification"] is False
    assert receipt["quarantined_diagnostics"]
    assert receipt["verified_mechanism"] is not None
    adapter_mechanism = receipt["mechanism_evidence"][0]
    assert adapter_mechanism["causal_target"] == "env:BACKLOG_DEPTH_VERIFY_MODE"
    assert adapter_mechanism["consumer_identity"]["runner_attested"] is True
    assert adapter_mechanism["consumer_identity"]["entrypoint"] == ("tools/environment_probe.ps1")
    assert adapter_mechanism["executed_consumer"]["causal_target"] == (
        "env:BACKLOG_DEPTH_VERIFY_MODE"
    )
    persisted_valid, persisted_errors = mod.verify_persisted_research_evidence(dossier)
    assert persisted_errors == []
    assert persisted_valid is True
    ready, readiness_reasons = stage_contracts.assess_research_readiness(dossier)
    assert ready is True, "\n".join(readiness_reasons)

    injected_outcome_error = (
        "outcome_mechanism_binding_invalid:experiment:mode-ready:"
        "production_assertion_dataflow_unresolved"
    )
    original_outcome_oracle_receipts = mod._outcome_oracle_receipts

    def outcome_candidate_with_invalid_future_binding(*args, errors, **kwargs):
        projected = original_outcome_oracle_receipts(*args, errors=errors, **kwargs)
        errors.append(injected_outcome_error)
        return projected

    with monkeypatch.context() as outcome_patch:
        outcome_patch.setattr(
            mod,
            "_outcome_oracle_receipts",
            outcome_candidate_with_invalid_future_binding,
        )
        optional_outcome_dossier = deepcopy(unverified_dossier)
        optional_outcome_receipt = mod.verify_research_evidence(
            optional_outcome_dossier,
            run_dir=run_dir,
            repo_revision=revision,
            case_id=str(optional_outcome_dossier["case_id"]),
            problem_id=str(optional_outcome_dossier["problem_id"]),
            expected_case_id=str(optional_outcome_dossier["case_id"]),
            expected_problem_id=str(optional_outcome_dossier["problem_id"]),
            evidence_assignment=assignment,
            evidence_atom_ids=[atom_id],
            revision_view_destination=tmp_path / "revision-view-optional-outcome",
            replay_timeout_seconds=None,
            requested_repo_ref=revision,
            resolved_repo_ref=revision,
            replay_executor=mod.TrustedHostReplayExecutor(
                approved_source_roots=[tmp_path],
                source_identity=workspace,
            ),
        )

        assert optional_outcome_receipt["status"] == "verified", optional_outcome_receipt[
            "errors"
        ]
        assert optional_outcome_receipt["errors"] == []
        assert optional_outcome_receipt["verified_mechanism"] is not None
        assert optional_outcome_receipt["outcome_oracles"] == []
        diagnostics_by_component = {
            str(item["component"]): item["diagnostics"]
            for item in optional_outcome_receipt["quarantined_diagnostics"]
        }
        assert injected_outcome_error in diagnostics_by_component["optional_post_change_outcome"]

        optional_outcome_dossier["evidence_verification"] = optional_outcome_receipt
        optional_persisted_valid, optional_persisted_errors = (
            mod.verify_persisted_research_evidence(optional_outcome_dossier)
        )
        assert optional_persisted_errors == []
        assert optional_persisted_valid is True

    for mode in ("missing", "unconnected"):
        rejected_dossier = deepcopy(unverified_dossier)
        rejected_claim = rejected_dossier["experiments"][1]["proof_adapter"]
        if mode == "missing":
            rejected_claim.pop("implementation_touchpoints")
        else:
            rejected_claim["implementation_touchpoints"][0]["causal_locator"] = "unrelated:mode"
        rejected_run_dir = tmp_path / f"research-run-{mode}"
        rejected_run_dir.mkdir()
        for filename in (
            "report.json",
            "workspace_ref.json",
            "target_ref.json",
            "normalized_events.jsonl",
        ):
            (rejected_run_dir / filename).write_bytes((run_dir / filename).read_bytes())
        rejected_dossier["artifact_refs"][0]["path"] = str(rejected_run_dir / "target_ref.json")
        rejected_receipt = mod.verify_research_evidence(
            rejected_dossier,
            run_dir=rejected_run_dir,
            repo_revision=revision,
            case_id=str(rejected_dossier["case_id"]),
            problem_id=str(rejected_dossier["problem_id"]),
            expected_case_id=str(rejected_dossier["case_id"]),
            expected_problem_id=str(rejected_dossier["problem_id"]),
            evidence_assignment=assignment,
            evidence_atom_ids=[atom_id],
            revision_view_destination=tmp_path / f"revision-view-{mode}",
            replay_timeout_seconds=None,
            requested_repo_ref=revision,
            resolved_repo_ref=revision,
            replay_executor=mod.TrustedHostReplayExecutor(
                approved_source_roots=[tmp_path],
                source_identity=workspace,
            ),
        )
        rejected_dossier["evidence_verification"] = rejected_receipt
        assert rejected_receipt["status"] == "verified", rejected_receipt["errors"]
        rejected_ready, rejected_reasons = stage_contracts.assess_research_readiness(
            rejected_dossier
        )
        assert rejected_ready is False
        assert any(
            "hypothesis_symbol_uninspected" in reason
            or "connected_mechanism_touchpoint_inspection_missing" in reason
            for reason in rejected_reasons
        ), rejected_reasons


def test_adapter_consumer_requires_authenticated_harness_dependency_and_keeps_identity(
    tmp_path: Path,
) -> None:
    experiment_ids = ("experiment:fresh", "experiment:aged")
    proof = {
        "observations": {
            "baseline": {"experiment_id": experiment_ids[0]},
            "challenge": {"experiment_id": experiment_ids[1]},
        },
        "intervention": {
            "target": "runner_core.execution_backend.cleanup_local_maintenance_images"
        },
    }
    workspace = tmp_path / "workspace"
    harness_root = workspace / ".usertest_research"
    harness_root.mkdir(parents=True)
    entrypoint = ".usertest_research/test_cleanup.py"
    (workspace / entrypoint).write_text(
        """from pathlib import Path
import importlib.util

def _harness():
    path = Path(__file__).with_name("cleanup_probe.py")
    spec = importlib.util.spec_from_file_location("cleanup_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_fresh():
    return _harness().run()

def test_aged():
    return _harness().run()
""",
        encoding="utf-8",
    )
    (harness_root / "cleanup_probe.py").write_text(
        """import runner_core.execution_backend as backend

def run():
    return backend.cleanup_local_maintenance_images
""",
        encoding="utf-8",
    )
    entrypoint_sha256 = mod._sha256_path(workspace / entrypoint)
    binding_projection = {
        "path": "packages/runner_core/src/runner_core/execution_backend.py",
        "relationship": "The harness exercises this inspected production module.",
        "file_sha256": "9" * 64,
        "git_blob_sha": "8" * 40,
        "runner_attested": True,
    }
    repository_binding = {
        **binding_projection,
        "repository_binding_sha256": mod._canonical_json_sha256(binding_projection),
    }
    replays: dict[str, dict[str, object]] = {}
    for experiment_id, selector in zip(experiment_ids, ("test_fresh", "test_aged"), strict=True):
        argv = [
            "python",
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "-s",
            f"{entrypoint}::{selector}",
        ]
        arm_binding = repository_binding
        if selector == "test_aged":
            arm_projection = {
                **binding_projection,
                "relationship": (
                    "The control arm reaches the same inspected production module."
                ),
            }
            arm_binding = {
                **arm_projection,
                "repository_binding_sha256": mod._canonical_json_sha256(arm_projection),
            }
        replays[experiment_id] = {
            "executed_argv": argv,
            "command_authorization": mod._command_authorization_receipt(
                {
                    "authorization_kind": "declared_repository_bindings",
                    "executed_argv_sha256": mod._canonical_json_sha256(argv),
                    "shell": False,
                    "workspace_confined": True,
                    "repository_bindings": [arm_binding],
                    "artifact_id": "artifact:cleanup-harness",
                    "entrypoint_kind": "repository_argv_entrypoint",
                    "entrypoint_path": entrypoint,
                    "entrypoint_argv_index": 8,
                    "runtime_executable": "python",
                    "entrypoint_sha256": entrypoint_sha256,
                    "entrypoint_git_blob_sha": None,
                    "project_runner": None,
                }
            ),
            "workspace_dir": str(workspace),
        }
    touchpoint = {
        "touchpoint_id": "implementation_touchpoint:" + "b" * 64,
        "causal_locator": "runner_core.execution_backend.cleanup_local_maintenance_images",
        "path": "packages/runner_core/src/runner_core/execution_backend.py",
        "symbols": ["runner_core.execution_backend.cleanup_local_maintenance_images"],
        "relationship": "Implements maintenance image retention.",
        "runner_attested": True,
        "inspected_content_sha256": "c" * 64,
        "evidence_sha256": "b" * 64,
    }
    unconnected_touchpoint = {
        "touchpoint_id": "implementation_touchpoint:" + "d" * 64,
        "causal_locator": "runner_core.execution_backend.cleanup_local_maintenance_images",
        "path": "configs/maintenance_docker.yaml",
        "symbols": [],
        "relationship": "Defines the configured retention count.",
        "runner_attested": True,
        "inspected_content_sha256": "e" * 64,
        "evidence_sha256": "d" * 64,
    }

    receipt = mod._adapter_executed_consumer_receipt(
        proof,
        clean_replays=replays,
        implementation_touchpoints=[touchpoint, unconnected_touchpoint],
    )

    assert receipt is not None
    identity = receipt["consumer_identity"]
    assert identity["kind"] == "runner_observed_research_harness_consumer"
    assert identity["entrypoint"] == entrypoint
    assert identity["command_authorization_identity"] == {
        "identity_kind": "research_harness_entrypoint",
        "source_authorization_kind": "repository_bindings",
        "research_harness_entrypoint": {
            "entrypoint_path": entrypoint,
            "entrypoint_sha256": entrypoint_sha256,
        },
    }
    assert identity["attestation_basis"] == (
        "executed_research_harness_with_authenticated_production_dependency"
    )
    assert identity["change_surfaces"] == [
        {
            "path": touchpoint["path"],
            "symbols": touchpoint["symbols"],
            "inspected_content_sha256": touchpoint["inspected_content_sha256"],
        }
    ]
    assert len(identity["authenticated_dependency_edges"]) == 1
    edge = identity["authenticated_dependency_edges"][0]
    assert edge["touchpoint_id"] == touchpoint["touchpoint_id"]
    assert edge["touchpoint_symbol"] == (
        "runner_core.execution_backend.cleanup_local_maintenance_images"
    )
    assert edge["source_path"] == ".usertest_research/cleanup_probe.py"
    assert [hop["edge_kind"] for hop in edge["local_dependency_chain"]] == [
        "python_local_module_reference"
    ]
    assert receipt["implementation_touchpoint_ids"] == [touchpoint["touchpoint_id"]]
    assert receipt["causal_target"] == (
        "runner_core.execution_backend.cleanup_local_maintenance_images"
    )
    assert receipt == stage_contracts._expected_adapter_executed_consumer(
        proof,
        experiments=replays,
        implementation_touchpoints=[touchpoint, unconnected_touchpoint],
        authenticated_dependency_edges=identity["authenticated_dependency_edges"],
    )
    disguised_replays = deepcopy(replays)
    for replay in disguised_replays.values():
        authorization = dict(replay["command_authorization"])
        authorization.pop("authorization_sha256")
        for field in (
            "artifact_id",
            "entrypoint_kind",
            "entrypoint_path",
            "entrypoint_argv_index",
            "runtime_executable",
            "entrypoint_sha256",
            "entrypoint_git_blob_sha",
            "project_runner",
        ):
            authorization.pop(field, None)
        replay["command_authorization"] = mod._command_authorization_receipt(authorization)
    assert (
        mod._adapter_executed_consumer_receipt(
            proof,
            clean_replays=disguised_replays,
            implementation_touchpoints=[touchpoint, unconnected_touchpoint],
        )
        is None
    )


def _authenticated_adapter_harness_replays(
    tmp_path: Path,
    *,
    source: str,
) -> tuple[dict[str, dict[str, object]], str]:
    workspace = tmp_path / "workspace"
    harness_root = workspace / ".usertest_research"
    harness_root.mkdir(parents=True)
    entrypoint = ".usertest_research/test_cleanup.py"
    (workspace / entrypoint).write_text(source, encoding="utf-8")
    entrypoint_sha256 = mod._sha256_path(workspace / entrypoint)
    binding_projection = {
        "path": "packages/runner_core/src/runner_core/execution_backend.py",
        "relationship": "The harness exercises this inspected production module.",
        "file_sha256": "9" * 64,
        "git_blob_sha": "8" * 40,
        "runner_attested": True,
    }
    repository_binding = {
        **binding_projection,
        "repository_binding_sha256": mod._canonical_json_sha256(binding_projection),
    }
    replays: dict[str, dict[str, object]] = {}
    for experiment_id, selector in (
        ("experiment:baseline", "test_baseline"),
        ("experiment:challenge", "test_challenge"),
    ):
        argv = [
            "python",
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "-s",
            f"{entrypoint}::{selector}",
        ]
        replays[experiment_id] = {
            "executed_argv": argv,
            "command_authorization": mod._command_authorization_receipt(
                {
                    "authorization_kind": "declared_repository_bindings",
                    "executed_argv_sha256": mod._canonical_json_sha256(argv),
                    "shell": False,
                    "workspace_confined": True,
                    "repository_bindings": [repository_binding],
                    "artifact_id": "artifact:cleanup-harness",
                    "entrypoint_kind": "repository_argv_entrypoint",
                    "entrypoint_path": entrypoint,
                    "entrypoint_argv_index": 8,
                    "runtime_executable": "python",
                    "entrypoint_sha256": entrypoint_sha256,
                    "entrypoint_git_blob_sha": None,
                    "project_runner": None,
                }
            ),
            "workspace_dir": str(workspace),
        }
    return replays, entrypoint


def test_adapter_mechanism_maps_field_locator_to_inspected_function_symbol(
    tmp_path: Path,
) -> None:
    symbol = "runner_core.execution_backend.cleanup_local_maintenance_images"
    locator = symbol + ":active_image_refs"
    replays, _entrypoint = _authenticated_adapter_harness_replays(
        tmp_path,
        source="""import runner_core.execution_backend as backend

def test_baseline():
    return backend.cleanup_local_maintenance_images

def test_challenge():
    return backend.cleanup_local_maintenance_images
""",
    )
    touchpoint = {
        "touchpoint_id": "implementation_touchpoint:" + "b" * 64,
        "causal_locator": locator,
        "path": "packages/runner_core/src/runner_core/execution_backend.py",
        "symbols": [symbol],
        "relationship": "The field-level control is consumed by this function.",
        "runner_attested": True,
        "inspected_content_sha256": "c" * 64,
        "evidence_sha256": "b" * 64,
    }
    proof = {
        "adapter_id": "structured_replay.v1",
        "adapter_version": "1",
        "hypothesis_id": "hypothesis:cleanup",
        "proof_receipt_id": "causal_proof:" + "d" * 64,
        "intervention_id": "intervention:" + "e" * 64,
        "observations": {
            "baseline": {"experiment_id": "experiment:baseline"},
            "challenge": {"experiment_id": "experiment:challenge"},
        },
        "intervention": {"kind": "argument", "target": locator},
        "source_root": {
            "root_kind": "origin_symptom",
            "origin_atom_ids": ["atom:cleanup"],
            "source_root_sha256": "f" * 64,
        },
        "mechanism_graph": {
            "root_node_id": "proof:root",
            "outcome_node_id": "proof:outcome",
            "nodes": [
                {"node_id": "proof:root", "kind": "source", "locator": "origin"},
                {
                    "node_id": "proof:mechanism",
                    "kind": "command",
                    "locator": locator,
                    "runner_attested": True,
                    "evidence_sha256": "1" * 64,
                },
                {"node_id": "proof:outcome", "kind": "outcome", "locator": "stdout"},
            ],
            "edges": [],
        },
        "adapter_evidence": {"implementation_touchpoints": [touchpoint]},
        "artifacts": [],
        "positive_outcome": {"passed": True},
    }
    atom_bindings = [
        {
            "experiment_id": "experiment:baseline",
            "atom_id": "atom:cleanup",
            "match_kind": "adapter_declared_symptom",
        }
    ]

    receipt = mod._adapter_mechanism_evidence_receipt(
        proof,
        hypothesis_symbols=[symbol],
        atom_bindings=atom_bindings,
        clean_replays=replays,
    )

    assert receipt is not None
    assert receipt["mechanism_symbols"] == [symbol]
    assert receipt["causal_target"] == locator
    assert receipt["code_paths"] == [
        {
            "symbol": symbol,
            "path": touchpoint["path"],
            "node_id": "proof:mechanism",
            "node_kind": "implementation_touchpoint",
            "evidence_sha256": touchpoint["evidence_sha256"],
        }
    ]
    assert receipt["mechanism_link"]["causal_locator_mappings"] == [
        {
            "causal_locator": locator,
            "mechanism_symbols": [symbol],
            "runner_attested": True,
        }
    ]
    assert receipt["executed_consumer"]["consumer_identity"]["runner_attested"] is True
    assert [binding["root_mechanism_symbol"] for binding in receipt["causal_root_bindings"]] == [
        symbol
    ]
    assert (
        mod._adapter_mechanism_evidence_receipt(
            proof,
            hypothesis_symbols=[locator],
            atom_bindings=atom_bindings,
            clean_replays=replays,
        )
        is None
    )


def test_adapter_mechanism_connects_exact_intervention_to_shared_touchpoint(
    tmp_path: Path,
) -> None:
    parser_symbol = "agent_adapters.shell_probe._codex_marker_source"
    resolver_symbol = "runner_core.shell_capability._resolve_shell_capability"
    replays, _entrypoint = _authenticated_adapter_harness_replays(
        tmp_path,
        source="""import runner_core.shell_capability as capability

def test_baseline():
    return capability._resolve_shell_capability

def test_challenge():
    return capability._resolve_shell_capability
""",
    )
    touchpoint = {
        "touchpoint_id": "implementation_touchpoint:" + "b" * 64,
        "causal_locator": parser_symbol,
        "path": "packages/runner_core/src/runner_core/shell_capability.py",
        "symbols": [resolver_symbol],
        "relationship": "Both intervention sides carry the result through the resolver.",
        "runner_attested": True,
        "inspected_content_sha256": "c" * 64,
        "evidence_sha256": "b" * 64,
    }
    proof = {
        "adapter_id": "structured_replay.v1",
        "adapter_version": "1",
        "hypothesis_id": "hypothesis:probe",
        "proof_receipt_id": "causal_proof:" + "d" * 64,
        "intervention_id": "intervention:" + "e" * 64,
        "observations": {
            "baseline": {"experiment_id": "experiment:baseline"},
            "challenge": {"experiment_id": "experiment:challenge"},
        },
        "intervention": {"kind": "implementation_revision", "target": parser_symbol},
        "source_root": {
            "root_kind": "origin_symptom",
            "origin_atom_ids": ["atom:probe"],
            "source_root_sha256": "f" * 64,
        },
        "mechanism_graph": {
            "root_node_id": "proof:root",
            "outcome_node_id": "proof:outcome",
            "nodes": [
                {"node_id": "proof:root", "kind": "source", "locator": "origin"},
                {
                    "node_id": "proof:mechanism",
                    "kind": "command",
                    "locator": parser_symbol,
                    "runner_attested": True,
                    "evidence_sha256": "1" * 64,
                },
                {"node_id": "proof:outcome", "kind": "outcome", "locator": "stdout"},
            ],
            "edges": [],
        },
        "adapter_evidence": {"implementation_touchpoints": [touchpoint]},
        "artifacts": [],
        "positive_outcome": {"passed": True},
    }
    atom_bindings = [
        {
            "experiment_id": "experiment:baseline",
            "atom_id": "atom:probe",
            "match_kind": "adapter_declared_symptom",
        }
    ]

    receipt = mod._adapter_mechanism_evidence_receipt(
        proof,
        hypothesis_symbols=[parser_symbol, resolver_symbol],
        atom_bindings=atom_bindings,
        clean_replays=replays,
        inspected_symbols=[parser_symbol, resolver_symbol],
    )

    assert receipt is not None
    assert receipt["mechanism_symbols"] == [parser_symbol, resolver_symbol]
    assert [binding["root_mechanism_symbol"] for binding in receipt["causal_root_bindings"]] == [
        parser_symbol
    ]
    assert receipt["mechanism_link"]["verified_directed_edges"] == [
        {
            "from_locator": parser_symbol,
            "to_locator": resolver_symbol,
            "kind": "adapter_intervention_to_shared_production_touchpoint",
            "runner_attested": True,
            "evidence_sha256": receipt["mechanism_link"]["verified_directed_edges"][0][
                "evidence_sha256"
            ],
        }
    ]
    connected, symbols, _trace, disconnected = mod._rooted_support_connectivity(
        [receipt],
        hypothesis_symbols=[parser_symbol, resolver_symbol],
    )
    assert connected == [receipt]
    assert symbols == {parser_symbol, resolver_symbol}
    assert disconnected == []


def test_adapter_harness_dependency_identity_ignores_controlled_call_arguments(
    tmp_path: Path,
) -> None:
    replays, entrypoint = _authenticated_adapter_harness_replays(
        tmp_path,
        source="""from pathlib import Path
import runner_core.execution_backend as backend

def test_baseline():
    return backend.cleanup_local_maintenance_images(
        repo_root=Path("baseline"), dry_run=True, active_image_refs=()
    )

def test_challenge():
    return backend.cleanup_local_maintenance_images(
        repo_root=Path("challenge"), dry_run=False, active_image_refs=("active",)
    )
""",
    )
    touchpoint = {
        "touchpoint_id": "implementation_touchpoint:" + "b" * 64,
        "causal_locator": "runner_core.execution_backend.cleanup_local_maintenance_images",
        "path": "packages/runner_core/src/runner_core/execution_backend.py",
        "symbols": ["runner_core.execution_backend.cleanup_local_maintenance_images"],
        "relationship": "Implements maintenance image retention.",
        "runner_attested": True,
        "inspected_content_sha256": "c" * 64,
        "evidence_sha256": "b" * 64,
    }
    proof = {
        "observations": {
            "baseline": {"experiment_id": "experiment:baseline"},
            "challenge": {"experiment_id": "experiment:challenge"},
        },
        "intervention": {"target": touchpoint["causal_locator"]},
    }

    baseline_edges = mod._research_harness_dependency_edges(
        replays["experiment:baseline"],
        implementation_touchpoints=[touchpoint],
    )
    challenge_edges = mod._research_harness_dependency_edges(
        replays["experiment:challenge"],
        implementation_touchpoints=[touchpoint],
    )
    receipt = mod._adapter_executed_consumer_receipt(
        proof,
        clean_replays=replays,
        implementation_touchpoints=[touchpoint],
    )

    assert baseline_edges == challenge_edges
    assert receipt is not None
    assert receipt["consumer_identity"]["entrypoint"] == entrypoint
    assert receipt["consumer_identity"]["authenticated_dependency_edges"] == baseline_edges
    assert len(receipt["invocations"]) == 2


def test_adapter_harness_dependency_identity_rejects_different_production_calls(
    tmp_path: Path,
) -> None:
    replays, _entrypoint = _authenticated_adapter_harness_replays(
        tmp_path,
        source="""from pathlib import Path
import runner_core.execution_backend as backend

def test_baseline():
    return backend.cleanup_local_maintenance_images(repo_root=Path("repo"), dry_run=True)

def test_challenge():
    return backend.list_local_maintenance_images(repo_root=Path("repo"))
""",
    )
    touchpoint = {
        "touchpoint_id": "implementation_touchpoint:" + "b" * 64,
        "causal_locator": "runner_core.execution_backend.cleanup_local_maintenance_images",
        "path": "packages/runner_core/src/runner_core/execution_backend.py",
        "symbols": [
            "runner_core.execution_backend.cleanup_local_maintenance_images",
            "runner_core.execution_backend.list_local_maintenance_images",
        ],
        "relationship": "Contains the maintenance image inventory and cleanup mechanisms.",
        "runner_attested": True,
        "inspected_content_sha256": "c" * 64,
        "evidence_sha256": "b" * 64,
    }
    proof = {
        "observations": {
            "baseline": {"experiment_id": "experiment:baseline"},
            "challenge": {"experiment_id": "experiment:challenge"},
        },
        "intervention": {"target": touchpoint["causal_locator"]},
    }

    baseline_edges = mod._research_harness_dependency_edges(
        replays["experiment:baseline"],
        implementation_touchpoints=[touchpoint],
    )
    challenge_edges = mod._research_harness_dependency_edges(
        replays["experiment:challenge"],
        implementation_touchpoints=[touchpoint],
    )

    assert baseline_edges != challenge_edges
    assert (
        mod._adapter_executed_consumer_receipt(
            proof,
            clean_replays=replays,
            implementation_touchpoints=[touchpoint],
        )
        is None
    )


def test_adapter_consumer_rejects_harness_with_only_relationship_prose(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    harness = workspace / ".usertest_research" / "probe.py"
    harness.parent.mkdir(parents=True)
    harness.write_text(
        'CLAIMED = "runner_core.execution_backend.cleanup_local_maintenance_images"\n'
        'print("unrelated")\n',
        encoding="utf-8",
    )
    experiment_ids = ("experiment:before", "experiment:after")
    proof = {
        "observations": {
            "baseline": {"experiment_id": experiment_ids[0]},
            "challenge": {"experiment_id": experiment_ids[1]},
        },
        "intervention": {
            "target": "runner_core.execution_backend.cleanup_local_maintenance_images"
        },
    }
    replays: dict[str, dict[str, object]] = {}
    for experiment_id in experiment_ids:
        argv = ["python", ".usertest_research/probe.py"]
        replays[experiment_id] = {
            "executed_argv": argv,
            "workspace_dir": str(workspace),
            "command_authorization": mod._command_authorization_receipt(
                {
                    "authorization_kind": "attested_research_harness",
                    "executed_argv_sha256": mod._canonical_json_sha256(argv),
                    "shell": False,
                    "workspace_confined": True,
                    "artifact_id": "artifact:probe",
                    "entrypoint_kind": "python_script",
                    "entrypoint_path": ".usertest_research/probe.py",
                    "entrypoint_sha256": mod._sha256_path(harness),
                    "entrypoint_git_blob_sha": None,
                    "project_runner": None,
                }
            ),
        }
    touchpoint = {
        "touchpoint_id": "implementation_touchpoint:" + "b" * 64,
        "causal_locator": "runner_core.execution_backend.cleanup_local_maintenance_images",
        "path": "packages/runner_core/src/runner_core/execution_backend.py",
        "symbols": ["runner_core.execution_backend.cleanup_local_maintenance_images"],
        "relationship": "Prose claims the harness exercises this production symbol.",
        "runner_attested": True,
        "inspected_content_sha256": "c" * 64,
        "evidence_sha256": "b" * 64,
    }

    assert (
        mod._adapter_executed_consumer_receipt(
            proof,
            clean_replays=replays,
            implementation_touchpoints=[touchpoint],
        )
        is None
    )


def test_non_python_research_harness_is_unverified_not_repository_consumer(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    harness = workspace / ".usertest_research" / "probe.js"
    harness.parent.mkdir(parents=True)
    harness.write_text(
        'import { mechanism } from "../packages/runtime/backend.js";\n'
        "console.log(mechanism());\n",
        encoding="utf-8",
    )
    experiment_ids = ("experiment:before", "experiment:after")
    argv = ["node", ".usertest_research/probe.js"]
    binding_projection = {
        "path": "packages/runtime/backend.js",
        "relationship": "The harness claims this production dependency.",
        "file_sha256": "9" * 64,
        "git_blob_sha": "8" * 40,
        "runner_attested": True,
    }
    repository_binding = {
        **binding_projection,
        "repository_binding_sha256": mod._canonical_json_sha256(binding_projection),
    }
    replays = {
        experiment_id: {
            "executed_argv": argv,
            "workspace_dir": str(workspace),
            "command_authorization": mod._command_authorization_receipt(
                {
                    "authorization_kind": "declared_repository_bindings",
                    "executed_argv_sha256": mod._canonical_json_sha256(argv),
                    "shell": False,
                    "workspace_confined": True,
                    "repository_bindings": [repository_binding],
                    "artifact_id": "artifact:js-probe",
                    "entrypoint_kind": "node_script",
                    "entrypoint_path": ".usertest_research/probe.js",
                    "entrypoint_sha256": mod._sha256_path(harness),
                    "entrypoint_git_blob_sha": None,
                    "project_runner": None,
                }
            ),
        }
        for experiment_id in experiment_ids
    }
    proof = {
        "proof_receipt_id": "proof:js-harness",
        "hypothesis_id": "hypothesis:runtime",
        "observations": {
            "baseline": {"experiment_id": experiment_ids[0]},
            "challenge": {"experiment_id": experiment_ids[1]},
        },
        "intervention": {"target": "runtime.backend.mechanism"},
    }
    touchpoint = {
        "touchpoint_id": "implementation_touchpoint:" + "b" * 64,
        "causal_locator": "runtime.backend.mechanism",
        "path": "packages/runtime/backend.js",
        "symbols": ["runtime.backend.mechanism"],
        "relationship": "The production function supplies the observed value.",
        "runner_attested": True,
        "inspected_content_sha256": "c" * 64,
        "evidence_sha256": "b" * 64,
    }
    proof["source_root"] = {
        "root_kind": "origin_symptom",
        "origin_atom_ids": ["atom:runtime"],
        "source_root_sha256": "f" * 64,
    }
    proof["mechanism_graph"] = {
        "root_node_id": "proof:root",
        "outcome_node_id": "proof:outcome",
        "nodes": [
            {"node_id": "proof:root", "kind": "source", "locator": "origin"},
            {
                "node_id": "proof:mechanism",
                "kind": "function",
                "locator": "runtime.backend.mechanism",
            },
            {"node_id": "proof:outcome", "kind": "outcome", "locator": "stdout"},
        ],
        "edges": [],
    }
    proof["adapter_evidence"] = {"implementation_touchpoints": [touchpoint]}
    proof["positive_outcome"] = {"passed": True}

    assert (
        mod._adapter_executed_consumer_receipt(
            proof,
            clean_replays=replays,
            implementation_touchpoints=[touchpoint],
        )
        is None
    )
    errors: list[str] = []
    receipts = mod._typed_mechanism_evidence_receipts(
        {
            "experiments": [],
            "root_cause_hypotheses": [
                {
                    "hypothesis_id": "hypothesis:runtime",
                    "mechanism_symbols": ["runtime.backend.mechanism"],
                    "supporting_evidence": [experiment_ids[0]],
                }
            ],
        },
        clean_replays=replays,
        symbol_receipts=[],
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=[
            {
                "experiment_id": experiment_ids[0],
                "atom_id": "atom:runtime",
                "match_kind": "adapter_declared_symptom",
            }
        ],
        errors=errors,
        proof_adapter_receipts=[proof],
    )

    assert receipts == []
    assert (
        "proof_adapter_harness_dependency_unverified:"
        "hypothesis:runtime:proof:js-harness"
    ) in errors


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "-q", "-kguarded", "tests/test_core.py::test_guarded_control"],
        ["pytest", "-q", "-mcontrol", "tests/test_core.py::test_guarded_control"],
        ["pytest", "-q", "tests/test_core.py::test_guarded_control[param]"],
    ],
)
def test_exact_pytest_selector_fails_closed_for_ambiguous_or_parameterized_selection(
    argv: list[str],
) -> None:
    assert mod._exact_pytest_selector(argv) is None


def test_exact_pytest_selector_accepts_attested_research_live_argv_shape() -> None:
    argv = [
        "pytest",
        "-p",
        "no:cacheprovider",
        ".usertest_research/test_probe.py::test_baseline",
        "-q",
        "-s",
        "--junitxml",
        ".usertest_research/baseline.xml",
    ]

    assert mod._exact_pytest_selector(argv) is None
    assert mod._exact_pytest_selector(argv, allow_research_harness=True) == (
        ".usertest_research/test_probe.py",
        ["test_baseline"],
    )


@pytest.mark.parametrize(
    "argv",
    [
        [
            "python",
            "-B",
            "-m",
            "pytest",
            "-q",
            "tests/test_core.py::test_guarded_control",
        ],
        [
            "pdm",
            "run",
            "python",
            "-I",
            "-B",
            "-m",
            "pytest",
            "tests/test_core.py::test_guarded_control",
        ],
    ],
)
def test_exact_pytest_selector_accepts_safe_python_interpreter_flags(
    argv: list[str],
) -> None:
    assert mod._exact_pytest_selector(argv) == (
        "tests/test_core.py",
        ["test_guarded_control"],
    )


@pytest.mark.parametrize(
    "argv",
    [
        [
            "python",
            "-c",
            "pass",
            "-m",
            "pytest",
            "tests/test_core.py::test_guarded_control",
        ],
        [
            "python",
            "-X",
            "utf8",
            "-m",
            "pytest",
            "tests/test_core.py::test_guarded_control",
        ],
        [
            "python",
            "-B",
            "-m",
            "pytest",
            "-k",
            "guarded",
            "tests/test_core.py::test_guarded_control",
        ],
        [
            "python",
            "-B",
            "-m",
            "pytest",
            "../tests/test_core.py::test_guarded_control",
        ],
    ],
)
def test_exact_pytest_selector_rejects_unsafe_or_ambiguous_flagged_invocations(
    argv: list[str],
) -> None:
    assert mod._exact_pytest_selector(argv) is None


def test_attested_research_pytest_shared_helper_delta_is_verified(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    dossier, replays = _attested_research_pytest_control(workspace)
    planning_workspace = tmp_path / "planning"
    _git(["clone", str(workspace), str(planning_workspace)], cwd=tmp_path)
    assert not (planning_workspace / ".usertest_research").exists()
    errors: list[str] = []

    receipts = mod._falsification_intervention_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=planning_workspace,
        symbol_receipts=[{"symbol": "core.classify", "path": "src/core.py"}],
        errors=errors,
    )

    assert errors == []
    assert len(receipts) == 1
    delta = receipts[0]["controlled_input_difference"]
    assert delta["verification_method"] == "python_ast_shared_helper_parameter_delta_v1"
    assert delta["difference"]["helper_function"] == "_probe"
    assert delta["difference"]["helper_parameter"] == "fatal"
    assert delta["difference"]["mechanism_slots"] == ["keyword:fatal"]
    assert receipts[0]["shared_verified_mechanism_symbols"] == ["core.classify"]

    attempt_errors: list[str] = []
    attempts = mod._falsification_attempt_receipts(
        dossier,
        clean_replays=replays,
        mechanism_evidence=[
            {
                "hypothesis_id": "h1",
                "mechanism_evidence_id": "mechanism_evidence:baseline",
                "experiment_ids": ["support"],
                "mechanism_symbols": ["core.classify"],
            }
        ],
        falsification_interventions=receipts,
        deterministic_closures=[],
        errors=attempt_errors,
    )
    assert attempt_errors == []
    assert attempts["h1"][0]["outcome"] == "survived"
    assert attempts["h1"][0]["mechanism_evidence_ids"] == [
        "mechanism_evidence:baseline"
    ]
    assert attempts["h1"][0]["intervention_receipt_id"] == receipts[0][
        "intervention_receipt_id"
    ]


def test_adapter_proof_supersedes_only_matching_falsification_intervention() -> None:
    covered = {
        "hypothesis_id": "hypothesis:primary",
        "baseline_experiment_id": "experiment:baseline",
        "challenge_experiment_id": "experiment:challenge",
        "intervention_receipt_id": "falsification_intervention:covered",
    }
    uncovered = {
        "hypothesis_id": "hypothesis:primary",
        "baseline_experiment_id": "experiment:baseline",
        "challenge_experiment_id": "experiment:other-challenge",
        "intervention_receipt_id": "falsification_intervention:uncovered",
    }
    proof = {
        "hypothesis_id": "hypothesis:primary",
        "intervention": {
            "baseline_experiment_id": "experiment:baseline",
            "challenge_experiment_id": "experiment:challenge",
        },
    }

    assert mod._falsification_interventions_without_adapter_proof(
        [covered, uncovered],
        proof_adapter_receipts=[proof],
    ) == [uncovered]
    assert mod._falsification_interventions_without_adapter_proof(
        [covered, uncovered],
        proof_adapter_receipts=[{"hypothesis_id": "hypothesis:primary"}],
    ) == [covered, uncovered]


def test_attested_research_pytest_shared_helper_accepts_utf8_bom(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    dossier, replays = _attested_research_pytest_control(workspace)
    harness = workspace / ".usertest_research" / "test_probe.py"
    harness.write_bytes(b"\xef\xbb\xbf" + harness.read_bytes())
    for replay in replays.values():
        authorization = dict(replay["command_authorization"])
        authorization.pop("authorization_sha256")
        authorization["entrypoint_sha256"] = mod._sha256_path(harness)
        replay["command_authorization"] = mod._command_authorization_receipt(authorization)
    planning_workspace = tmp_path / "planning"
    _git(["clone", str(workspace), str(planning_workspace)], cwd=tmp_path)
    errors: list[str] = []

    receipts = mod._falsification_intervention_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=planning_workspace,
        symbol_receipts=[{"symbol": "core.classify", "path": "src/core.py"}],
        errors=errors,
    )

    assert errors == []
    assert len(receipts) == 1
    assert receipts[0]["shared_verified_mechanism_symbols"] == ["core.classify"]


@pytest.mark.parametrize("authorization_change", ["missing", "wrong_sha"])
def test_research_pytest_shared_helper_requires_attested_entrypoint(
    tmp_path: Path,
    authorization_change: str,
) -> None:
    workspace = tmp_path / "workspace"
    dossier, replays = _attested_research_pytest_control(workspace)
    replay = replays["support"]
    if authorization_change == "missing":
        replay.pop("command_authorization")
    else:
        authorization = dict(replay["command_authorization"])
        authorization.pop("authorization_sha256")
        authorization["entrypoint_sha256"] = "0" * 64
        replay["command_authorization"] = mod._command_authorization_receipt(authorization)
    experiment = next(
        item
        for item in dossier["experiments"]
        if isinstance(item, dict) and item.get("experiment_id") == "support"
    )
    errors: list[str] = []

    selection = mod._pytest_test_selection_receipt(
        hypothesis_id="h1",
        experiment_id="support",
        experiment=experiment,
        replay=replay,
        mechanism_symbols=["core.classify"],
        symbol_paths={"core.classify": "src/core.py"},
        planning_workspace=workspace,
        errors=errors,
    )

    assert selection is None
    assert errors == [
        "causal_control_research_harness_unattested:"
        "h1:support:.usertest_research/test_probe.py"
    ]


@pytest.mark.parametrize(
    ("helper_source", "baseline_call", "challenge_call"),
    [
        (
            "def _probe(*, fatal):\n"
            "    classify(fatal=fatal)\n"
            "    return 'fixed'\n",
            "_probe(fatal=True)",
            "_probe(fatal=False)",
        ),
        (
            "def _probe(*, fatal, stable=True):\n"
            "    return classify(fatal=stable)\n",
            "_probe(fatal=True, stable=True)",
            "_probe(fatal=False, stable=True)",
        ),
        (
            "def _probe(*, fatal, extra):\n"
            "    return classify(fatal=fatal, extra=extra)\n",
            "_probe(fatal=True, extra=False)",
            "_probe(fatal=False, extra=True)",
        ),
    ],
    ids=["hardcoded-return", "changed-unused-parameter", "two-changed-parameters"],
)
def test_research_pytest_shared_helper_rejects_noncausal_or_ambiguous_delta(
    tmp_path: Path,
    helper_source: str,
    baseline_call: str,
    challenge_call: str,
) -> None:
    workspace = tmp_path / "workspace"
    dossier, replays = _attested_research_pytest_control(
        workspace,
        helper_source=helper_source,
        baseline_call=baseline_call,
        challenge_call=challenge_call,
    )
    errors: list[str] = []

    receipts = mod._falsification_intervention_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.classify", "path": "src/core.py"}],
        errors=errors,
    )

    assert receipts == []
    assert any(
        error.startswith("falsification_intervention_unverified:h1:attempt:toggle-fatal")
        for error in errors
    )


def test_research_pytest_shared_helper_rejects_locally_shadowed_helper(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    dossier, replays = _attested_research_pytest_control(workspace)
    harness = workspace / ".usertest_research" / "test_probe.py"
    content = harness.read_text(encoding="utf-8")
    content = content.replace(
        "def test_baseline():\n",
        "def test_baseline():\n    _probe = lambda **_kwargs: 'fatal'\n",
    ).replace(
        "def test_challenge():\n",
        "def test_challenge():\n    _probe = lambda **_kwargs: 'notice'\n",
    )
    harness.write_text(content, encoding="utf-8")
    for replay in replays.values():
        authorization = dict(replay["command_authorization"])
        authorization.pop("authorization_sha256")
        authorization["entrypoint_sha256"] = mod._sha256_path(harness)
        replay["command_authorization"] = mod._command_authorization_receipt(authorization)
    errors: list[str] = []

    receipts = mod._falsification_intervention_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.classify", "path": "src/core.py"}],
        errors=errors,
    )

    assert receipts == []
    assert any(
        error.startswith("falsification_intervention_unverified:h1:attempt:toggle-fatal")
        for error in errors
    )


@pytest.mark.parametrize(
    "control_target",
    [
        "tests/test_core.py::test_unrelated_same_file",
        "tests/test_core.py::test_shadowed_mechanism_name",
        "tests/test_other.py::test_unrelated_other_file",
    ],
)
def test_unrelated_passing_test_cannot_be_causal_counterevidence(
    tmp_path: Path,
    control_target: str,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier(control_target)
    errors: list[str] = []

    selections, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert len(selections) == 2
    assert controls == []
    assert "causal_control_mechanism_not_called:h1:control:core.run" in errors
    assert "causal_control_mechanism_coverage_missing:h1:control" in errors


def test_focused_guarded_control_calling_same_mechanism_is_verified(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_guarded_control")
    errors: list[str] = []

    selections, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert errors == []
    assert {selection["experiment_id"] for selection in selections} == {
        "support",
        "control",
    }
    assert all(
        selection["mechanism_touches"][0]["symbol"] == "core.run" for selection in selections
    )
    assert len(controls) == 1
    control = controls[0]
    assert control["verification_method"] == "pytest_ast_controlled_difference_v2"
    assert control["controlled_input_difference"] == {
        "verification_method": "python_ast_explicit_argument_delta_v1",
        "difference_count": 1,
        "difference": {
            "mechanism_symbol": "core.run",
            "slot": "keyword:guarded",
            "difference_kind": "added_in_control",
            "support_argument": None,
            "control_argument": {
                "slot": "keyword:guarded",
                "expression": "True",
                "ast_sha256": mod.sha256(b"Constant(value=True)").hexdigest(),
            },
        },
    }
    assert control["observable_difference"]["difference_kind"] == "failing_exit_to_zero"
    assert control["observable_difference"]["support"]["exit_code"] == 1
    assert control["observable_difference"]["control"]["exit_code"] == 0
    assert control["adversarial_effect"] == "limits_scope"
    assert control["control_verification_id"] == mod._content_addressed_receipt_id(
        "control_verification",
        control,
        "control_verification_id",
    )

    failure_paths = mod._failure_path_receipts(
        dossier,
        test_selections=selections,
        control_verifications=controls,
        errors=errors,
    )
    assert errors == []
    assert len(failure_paths) == 1
    assert failure_paths[0]["path_name"] == ("tests/test_core.py::test_reported_failure")
    assert failure_paths[0]["origin_atom_ids"] == ["atom:support"]
    assert failure_paths[0]["failure_path_id"] == mod._content_addressed_receipt_id(
        "failure_path",
        failure_paths[0],
        "failure_path_id",
    )


@pytest.mark.parametrize(
    ("control_target", "expected_error"),
    [
        (
            "tests/test_core.py::test_same_input_control",
            "causal_control_requires_exactly_one_structural_difference:h1:control:0",
        ),
        (
            "tests/test_core.py::test_two_input_control",
            "causal_control_requires_exactly_one_structural_difference:h1:control:2",
        ),
    ],
)
def test_control_requires_exactly_one_runner_observed_input_delta(
    tmp_path: Path,
    control_target: str,
    expected_error: str,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier(control_target)
    errors: list[str] = []

    _, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert controls == []
    assert expected_error in errors


def test_falsification_challenge_requires_runner_observed_causal_input_delta(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_same_input_control")
    hypothesis = dossier["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["statement"] = "The default core.run input causes the failure."
    hypothesis["supporting_evidence"] = ["support", "control"]
    hypothesis["counterevidence"] = []
    hypothesis["falsification_attempts"] = [
        {
            "attempt_id": "attempt:same-input",
            "hypothesis_id": "h1",
            "claim": hypothesis["statement"],
            "baseline_experiment_id": "support",
            "challenge_experiment_id": "control",
            "disproof_condition": {
                "source": "exit_code",
                "operator": "equals",
                "expected": 0,
            },
            "outcome": "survived",
        }
    ]
    challenge = dossier["experiments"][1]
    assert isinstance(challenge, dict)
    challenge["outcome"] = "supports"
    challenge["exit_code"] = 1
    challenge["observable_assertion"] = {
        "source": "exit_code",
        "operator": "equals",
        "expected": 1,
    }
    replays["control"]["exit_code"] = 1
    errors: list[str] = []

    receipts = mod._falsification_intervention_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert receipts == []
    assert any(
        error.startswith("falsification_intervention_unverified:h1:attempt:same-input")
        for error in errors
    )


def test_model_control_prose_cannot_turn_an_invalid_pair_into_causal_proof(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_same_input_control")
    control = dossier["experiments"][1]
    assert isinstance(control, dict)
    relationship = control["control_relationship"]
    assert isinstance(relationship, dict)
    relationship["controlled_variable"] = "author insists this is different"
    relationship["expected_difference"] = "author insists this succeeds"
    errors: list[str] = []

    _, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert controls == []
    assert "causal_control_requires_exactly_one_structural_difference:h1:control:0" in errors


def test_control_requires_complementary_runner_observation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_guarded_control")
    control = dossier["experiments"][1]
    assert isinstance(control, dict)
    control["exit_code"] = 2
    assertion = control["observable_assertion"]
    assert isinstance(assertion, dict)
    assertion["expected"] = 2
    replays["control"]["exit_code"] = 2
    errors: list[str] = []

    _, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert controls == []
    assert "causal_control_observable_not_complementary:h1:control" in errors


def test_supporting_experiment_must_reproduce_the_atom_symptom(tmp_path: Path) -> None:
    origin = tmp_path / "origin.json"
    origin.write_text(
        '{"error":"shell_probe_failed prevented the mission"}',
        encoding="utf-8",
    )
    assignment = {
        "status": "complete",
        "expected_atom_ids": ["atom:shell"],
        "atom_receipts": [
            {
                "atom_id": "atom:shell",
                "atom_snapshot": {
                    "atom_id": "atom:shell",
                    "command": "pytest -q",
                    "text": "shell_probe_failed prevented the mission",
                    "exit_code": 1,
                },
                "artifact_receipts": [{"path": str(origin)}],
            }
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "absence-is-not-support",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:shell"],
                "command": "pytest -q",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "combined",
                    "operator": "not_contains",
                    "expected": "shell_probe_failed",
                },
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert bindings == []
    assert "experiment_not_bound_to_atom:absence-is-not-support:atom:shell" in errors
    assert "supporting_experiments_do_not_cover_origin_atoms" in errors

    dossier["experiments"][0]["observable_assertion"]["operator"] = "contains"
    errors = []
    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)
    assert len(bindings) == 1
    assert bindings[0]["experiment_id"] == "absence-is-not-support"
    assert bindings[0]["atom_id"] == "atom:shell"
    assert bindings[0]["match_kind"] == "command_and_atom_evidence_symptom"
    assert bindings[0]["origin_atom_field_path"] == "$.text"
    assert bindings[0]["origin_artifact_path"] == str(origin)
    assert bindings[0]["origin_artifact_sha256"] == mod._sha256_path(origin)
    assert errors == []


def test_harness_call_discard_with_hard_coded_symptom_is_not_mechanism_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "replay"
    harness = workspace / ".usertest_research" / "probe.py"
    harness.parent.mkdir(parents=True)
    harness.write_text(
        "from core import run\nrun()\nprint('shell_probe_failed')\n",
        encoding="utf-8",
    )
    replay = {
        "executed_argv": ["python", ".usertest_research/probe.py"],
        "workspace_dir": str(workspace),
    }

    path, touched, link = mod._harness_mechanism_touches(
        replay=replay,
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion={
            "source": "stdout",
            "operator": "contains",
            "expected": "shell_probe_failed",
        },
    )

    assert path == ".usertest_research/probe.py"
    assert touched == []
    assert link is None

    harness.write_text(
        "from core import run\nprint(f'{run()} :: shell_probe_failed')\n",
        encoding="utf-8",
    )
    _, touched, link = mod._harness_mechanism_touches(
        replay=replay,
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion={
            "source": "stdout",
            "operator": "contains",
            "expected": "shell_probe_failed",
        },
    )
    assert touched == []
    assert link is None

    harness.write_text(
        "from core import run\nresult = run()\nprint(result)\n",
        encoding="utf-8",
    )
    _, touched, link = mod._harness_mechanism_touches(
        replay=replay,
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion={
            "source": "stdout",
            "operator": "contains",
            "expected": "shell_probe_failed",
        },
    )
    assert touched == ["core.run"]
    assert link is not None
    assert link["verification_method"] == "runner_harness_observable_dataflow_v1"


def test_harness_mechanism_flow_follows_local_return_and_exact_json_field(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "replay"
    harness = workspace / ".usertest_research" / "probe.py"
    harness.parent.mkdir(parents=True)
    replay = {
        "executed_argv": ["python", ".usertest_research/probe.py"],
        "workspace_dir": str(workspace),
    }
    assertion = {
        "source": "stdout",
        "operator": "contains",
        "expected": '"validator_issue_present": true',
    }
    harness.write_text(
        "import json\n"
        "from core import run\n\n"
        "def investigate():\n"
        "    issue = run()\n"
        "    observation = {'validator_issue_present': issue is not None}\n"
        "    return observation\n\n"
        "def main():\n"
        "    observation = investigate()\n"
        "    print(json.dumps(observation))\n\n"
        "main()\n",
        encoding="utf-8",
    )

    _, touched, link = mod._harness_mechanism_touches(
        replay=replay,
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion=assertion,
    )

    assert touched == ["core.run"]
    assert link is not None
    assert link["symbol_sinks"] == [{"symbol": "core.run", "sink": "stdout"}]

    harness.write_text(
        "import json\n"
        "from core import run\n\n"
        "def investigate():\n"
        "    issue = run()\n"
        "    observation = {\n"
        "        'unrelated_diagnostic': issue,\n"
        "        'validator_issue_present': True,\n"
        "    }\n"
        "    return observation\n\n"
        "def main():\n"
        "    observation = investigate()\n"
        "    print(json.dumps(observation))\n\n"
        "main()\n",
        encoding="utf-8",
    )

    _, touched, link = mod._harness_mechanism_touches(
        replay=replay,
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion=assertion,
    )

    assert touched == []
    assert link is None


@pytest.mark.parametrize("operator", ["contains", "not_contains"])
def test_harness_mechanism_flow_accepts_tainted_inline_json_field(
    tmp_path: Path,
    operator: str,
) -> None:
    workspace = tmp_path / "replay"
    harness = workspace / ".usertest_research" / "probe.py"
    harness.parent.mkdir(parents=True)
    harness.write_text(
        "import json\n"
        "from core import run\n"
        "result = run()\n"
        "print(json.dumps({'reason_code': result[0], 'state': 'blocked'}))\n",
        encoding="utf-8",
    )

    _, touched, link = mod._harness_mechanism_touches(
        replay={
            "executed_argv": ["python", ".usertest_research/probe.py"],
            "workspace_dir": str(workspace),
        },
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion={
            "source": "stdout",
            "operator": operator,
            "expected": '"reason_code": "shell_probe_failed"',
        },
    )

    assert touched == ["core.run"]
    assert link is not None
    assert link["symbol_sinks"] == [{"symbol": "core.run", "sink": "stdout"}]


def test_harness_mechanism_touch_resolves_function_local_imports(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "replay"
    harness = workspace / ".usertest_research" / "probe.py"
    harness.parent.mkdir(parents=True)
    replay = {
        "executed_argv": ["python", ".usertest_research/probe.py"],
        "workspace_dir": str(workspace),
    }
    assertion = {
        "source": "stdout",
        "operator": "contains",
        "expected": "available",
    }
    harness.write_text(
        "def main():\n"
        "    import json\n"
        "    from pathlib import Path\n"
        "    from core import run\n"
        "    state = run()\n"
        "    Path('observation.txt').write_text(str(state))\n"
        "    print(state)\n\n"
        "main()\n",
        encoding="utf-8",
    )

    _, touched, link = mod._harness_mechanism_touches(
        replay=replay,
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion=assertion,
    )

    assert touched == ["core.run"]
    assert link is not None
    assert link["symbol_sinks"] == [{"symbol": "core.run", "sink": "stdout"}]


def test_harness_mechanism_touch_follows_nested_local_helper_return(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "replay"
    harness = workspace / ".usertest_research" / "probe.py"
    harness.parent.mkdir(parents=True)
    harness.write_text(
        "import json\n"
        "from core import run\n\n"
        "def main():\n"
        "    def invoke(marker):\n"
        "        result = run(marker)\n"
        "        return result[0]\n\n"
        "    reason_code = invoke('historical-marker')\n"
        "    print(json.dumps({'reason_code': reason_code}))\n\n"
        "main()\n",
        encoding="utf-8",
    )

    _, touched, link = mod._harness_mechanism_touches(
        replay={
            "executed_argv": ["python", ".usertest_research/probe.py"],
            "workspace_dir": str(workspace),
        },
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion={
            "source": "stdout",
            "operator": "contains",
            "expected": '"reason_code": "historical-failure"',
        },
    )

    assert touched == ["core.run"]
    assert link is not None
    assert link["symbol_sinks"] == [{"symbol": "core.run", "sink": "stdout"}]


def test_harness_mechanism_touch_rejects_shadowed_module_import(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "replay"
    harness = workspace / ".usertest_research" / "probe.py"
    harness.parent.mkdir(parents=True)
    harness.write_text(
        "from core import run\n\n"
        "def main(run):\n"
        "    print(run())\n\n"
        "main(lambda: 'available')\n",
        encoding="utf-8",
    )

    _, touched, link = mod._harness_mechanism_touches(
        replay={
            "executed_argv": ["python", ".usertest_research/probe.py"],
            "workspace_dir": str(workspace),
        },
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion={
            "source": "stdout",
            "operator": "contains",
            "expected": "available",
        },
    )

    assert touched == []
    assert link is None


@pytest.mark.parametrize("research_status", ["blocked", "insufficient_evidence"])
def test_incomplete_research_quarantines_missing_mechanism_without_claiming_it_verified(
    research_status: str,
) -> None:
    mechanism_errors = [
        "temporary_harness_mechanism_call_missing:hypothesis:one:experiment:one",
        "primary_hypothesis_mechanism_evidence_missing:hypothesis:one",
    ]

    fatal, diagnostics = mod._partition_mechanism_validation_errors(
        {"research_status": research_status},
        mechanism_errors,
    )

    assert fatal == []
    assert diagnostics == mechanism_errors

    fatal, diagnostics = mod._partition_mechanism_validation_errors(
        {"research_status": "evidence_sufficient"},
        mechanism_errors,
    )
    assert fatal == mechanism_errors
    assert diagnostics == []


def test_sufficient_research_keeps_primary_mechanism_errors_fatal_and_quarantines_secondary(
) -> None:
    dossier = {
        "research_status": "evidence_sufficient",
        "root_cause_hypotheses": [
            {"hypothesis_id": "hypothesis:primary"},
            {"hypothesis_id": "hypothesis:alternative"},
        ],
    }
    primary_error = (
        "primary_hypothesis_mechanism_coverage_incomplete:hypothesis:primary"
    )
    secondary_error = (
        "observed_output_mechanism_link_missing:"
        "hypothesis:alternative:experiment:alternative"
    )
    unattributed_error = "mechanism_receipt_integrity_invalid"

    fatal, diagnostics = mod._partition_mechanism_validation_errors(
        dossier,
        [secondary_error, primary_error, unattributed_error],
    )

    assert fatal == [primary_error, unattributed_error]
    assert diagnostics == [secondary_error]


@pytest.mark.parametrize("disposition", ["already_addressed", "non_actionable"])
def test_complete_negative_quarantines_planning_mechanism_errors(
    disposition: str,
) -> None:
    mechanism_errors = [
        "primary_hypothesis_mechanism_evidence_missing:hypothesis:external-runtime",
        "primary_hypothesis_causal_root_missing:hypothesis:external-runtime",
    ]
    dossier = {
        "research_status": "evidence_sufficient",
        "actionability_assessment": {"disposition": disposition},
        "root_cause_hypotheses": [
            {"hypothesis_id": "hypothesis:external-runtime"}
        ],
    }

    fatal, diagnostics = mod._partition_mechanism_validation_errors(
        dossier,
        mechanism_errors,
    )

    assert fatal == []
    assert diagnostics == mechanism_errors


def test_complete_negative_does_not_require_causal_falsification_receipt() -> None:
    errors: list[str] = []

    receipts = mod._falsification_attempt_receipts(
        {
            "research_status": "evidence_sufficient",
            "actionability_assessment": {"disposition": "already_addressed"},
            "experiments": [],
            "root_cause_hypotheses": [
                {
                    "hypothesis_id": "hypothesis:external-runtime",
                    "statement": "The retained failure occurred outside the repository.",
                    "supporting_evidence": ["artifact:origin"],
                    "counterevidence": [],
                    "falsification_attempts": [],
                    "mechanism_symbols": [],
                }
            ],
        },
        clean_replays={},
        mechanism_evidence=[],
        falsification_interventions=[],
        deterministic_closures=[],
        errors=errors,
    )

    assert receipts == {"hypothesis:external-runtime": []}
    assert errors == []


def test_harness_mechanism_touch_follows_only_immediate_result_method_chain(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "replay"
    harness = workspace / ".usertest_research" / "shell_probe.py"
    harness.parent.mkdir(parents=True)
    replay = {
        "executed_argv": ["python", ".usertest_research/shell_probe.py"],
        "workspace_dir": str(workspace),
    }
    kwargs = {
        "replay": replay,
        "mechanism_symbols": ["_resolve_shell_capability"],
        "symbol_paths": {
            "_resolve_shell_capability": (
                "packages/runner_core/src/runner_core/shell_capability.py"
            )
        },
        "observable_assertion": {
            "source": "stdout",
            "operator": "contains",
            "expected": "available",
        },
    }
    harness.write_text(
        "import json\n"
        "from runner_core.shell_capability import _resolve_shell_capability\n"
        "observation = _resolve_shell_capability(probe_result=True).to_dict()\n"
        "print(json.dumps(observation))\n",
        encoding="utf-8",
    )

    path, touched, link = mod._harness_mechanism_touches(**kwargs)

    assert path == ".usertest_research/shell_probe.py"
    assert touched == ["_resolve_shell_capability"]
    assert link is not None
    assert link["symbol_sinks"] == [{"symbol": "_resolve_shell_capability", "sink": "stdout"}]

    harness.write_text(
        "import json\n"
        "from runner_core.shell_capability import _resolve_shell_capability\n"
        "def unrelated(value):\n"
        "    return value\n"
        "observation = unrelated(\n"
        "    _resolve_shell_capability(probe_result=True)\n"
        ").to_dict()\n"
        "print(json.dumps(observation))\n",
        encoding="utf-8",
    )
    _, touched, link = mod._harness_mechanism_touches(**kwargs)
    assert touched == []
    assert link is None

    harness.write_text(
        "import json\n"
        "from runner_core.shell_capability import _resolve_shell_capability\n"
        "observation = _resolve_shell_capability(probe_result=True).to_dict()\n"
        'print(json.dumps({"state": "available"}))\n',
        encoding="utf-8",
    )
    _, touched, link = mod._harness_mechanism_touches(**kwargs)
    assert touched == []
    assert link is None


def test_proof_adapter_quote_cross_binding_requires_exact_same_contract() -> None:
    quote = "available is the only state that may dispatch shell-required missions"
    path = "packages/runner_core/src/runner_core/shell_capability.py"
    experiment = {
        "positive_outcome_contract": {
            "contract_kind": "retained_harness_semantic_assertion",
            "expected_value": "available",
            "semantic_basis": {
                "kind": "repository_contract_quote",
                "contract_type": "api_contract",
                "path": path,
                "exact_quote": quote,
            },
        }
    }
    claim = {
        "intervention": {"target": "runner_core.shell_capability.resolve:probe_result"},
        "implementation_touchpoints": [
            {
                "causal_locator": "runner_core.shell_capability.resolve:probe_result",
                "path": path,
                "symbols": ["resolve"],
            }
        ],
    }
    semantic = {
        "kind": "repository_contract_quote",
        "path": path,
        "exact_quote": quote,
    }

    resolved = mod._resolved_proof_adapter_semantic_basis(
        experiment=experiment,
        claim=claim,
        semantic_basis=semantic,
        predicate={"kind": "equals", "expected": "available"},
    )

    assert resolved == {
        **experiment["positive_outcome_contract"]["semantic_basis"],
        "symbol": "resolve",
    }

    conflicting = mod._resolved_proof_adapter_semantic_basis(
        experiment=experiment,
        claim=claim,
        semantic_basis={**semantic, "exact_quote": "different contract"},
        predicate={"kind": "equals", "expected": "available"},
    )
    assert conflicting == {**semantic, "exact_quote": "different contract"}

    wrong_expected = mod._resolved_proof_adapter_semantic_basis(
        experiment=experiment,
        claim=claim,
        semantic_basis=semantic,
        predicate={"kind": "equals", "expected": "blocked"},
    )
    assert wrong_expected == semantic


def test_proof_adapter_quote_resolves_exact_intervention_locator_without_legacy_contract() -> None:
    path = "packages/runner_core/src/runner_core/shell_capability.py"
    locator = "runner_core.shell_capability._codex_shell_probe_failure_reason"
    semantic = {
        "kind": "repository_contract_quote",
        "contract_type": "api_contract",
        "path": path,
        "exact_quote": 'return "codex_windows_sandbox_panic"',
        "locator": locator,
    }
    claim = {
        "intervention": {"target": locator},
        "implementation_touchpoints": [
            {
                "causal_locator": locator,
                "path": path,
                "symbols": [locator],
            }
        ],
    }

    resolved = mod._resolved_proof_adapter_semantic_basis(
        experiment={},
        claim=claim,
        semantic_basis=semantic,
        predicate={"kind": "contains", "expected": "codex_windows_sandbox_panic"},
    )

    assert resolved == {**semantic, "symbol": locator}
    assert mod._resolved_proof_adapter_semantic_basis(
        experiment={},
        claim={**claim, "intervention": {"target": "different"}},
        semantic_basis=semantic,
        predicate={"kind": "contains", "expected": "codex_windows_sandbox_panic"},
    ) == semantic


def test_failed_mechanism_surfaces_adapter_rejection_without_making_it_fatal() -> None:
    diagnostics = [
        {
            "experiment_id": "experiment:challenge",
            "adapter_id": "structured_replay.v1",
            "diagnostics": ["repository_contract_quote_positive_basis_unattested"],
        }
    ]

    assert mod._proof_adapter_failure_diagnostics(
        diagnostics=diagnostics,
        fatal_mechanism_errors=[],
    ) == []
    assert mod._proof_adapter_failure_diagnostics(
        diagnostics=diagnostics,
        fatal_mechanism_errors=["primary_hypothesis_mechanism_evidence_missing:h1"],
    ) == [
        "proof_adapter_unverified:experiment:challenge:structured_replay.v1:"
        "repository_contract_quote_positive_basis_unattested"
    ]


def test_atom_binding_uses_structured_snapshot_output_without_ancillary_artifact() -> None:
    assignment = {
        "status": "complete",
        "expected_atom_ids": ["atom:structured"],
        "atom_receipts": [
            {
                "atom_id": "atom:structured",
                "atom_snapshot": {
                    "atom_id": "atom:structured",
                    "command": "python -m tool verify",
                    "exit_code": 3,
                    "text": "The verification command failed.",
                    "output_excerpt": "classifier selected the wrong recovery path",
                },
                "artifact_receipts": [],
            }
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "structured-symptom",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:structured"],
                "command": "python -m tool verify",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stderr",
                    "operator": "contains",
                    "expected": "classifier selected the wrong recovery path",
                },
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert errors == []
    assert bindings[0]["match_kind"] == "command_and_atom_evidence_symptom"
    assert bindings[0]["origin_atom_field_path"] == "$.output_excerpt"
    assert "origin_artifact_path" not in bindings[0]


def test_implicit_atom_binding_accepts_json_fragment_wrapping_exact_scalar() -> None:
    atom_id = "atom:error-code"
    error_code = "codex_model_messages_missing"
    assignment = {
        "status": "complete",
        "expected_atom_ids": [atom_id],
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_sha256": mod._canonical_json_sha256({"code": error_code}),
                "atom_snapshot": {"code": error_code},
                "artifact_receipts": [],
            }
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "wrapped-error-code",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": [atom_id],
                "command": "python .usertest_research/probe.py",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "contains",
                    "expected": f'"error_code": "{error_code}"',
                },
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert errors == []
    assert bindings[0]["origin_atom_field_path"] == "$.code"


def test_implicit_atom_binding_rejects_unquoted_prose_containing_short_scalar() -> None:
    atom_id = "atom:error-code"
    assignment = {
        "status": "complete",
        "expected_atom_ids": [atom_id],
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_snapshot": {"code": "error"},
                "artifact_receipts": [],
            }
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "negated-error-prose",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": [atom_id],
                "command": "python .usertest_research/probe.py",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "contains",
                    "expected": "no error occurred",
                },
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert bindings == []
    assert f"experiment_not_bound_to_atom:negated-error-prose:{atom_id}" in errors


def test_explicit_field_bindings_accept_short_symptom_and_context_atoms() -> None:
    assignment = {
        "status": "complete",
        "expected_atom_ids": ["atom:symptom", "atom:context"],
        "atom_receipts": [
            {
                "atom_id": "atom:symptom",
                "atom_sha256": "1" * 64,
                "atom_snapshot": {
                    "atom_id": "atom:symptom",
                    "command": "python -m product show",
                    "output_excerpt": "bad",
                },
                "artifact_receipts": [],
            },
            {
                "atom_id": "atom:context",
                "atom_sha256": "2" * 64,
                "atom_snapshot": {
                    "atom_id": "atom:context",
                    "platform": "win",
                },
                "artifact_receipts": [],
            },
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "short-wrong-value",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": ["atom:symptom", "atom:context"],
                "command": "python -m product show",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "bad",
                },
                "origin_evidence_bindings": [
                    {
                        "atom_id": "atom:symptom",
                        "role": "symptom",
                        "field_path": "$.output_excerpt",
                        "value": "bad",
                        "value_sha256": mod._canonical_json_sha256("bad"),
                    },
                    {
                        "atom_id": "atom:context",
                        "role": "context",
                        "field_path": "$.platform",
                        "value": "win",
                    },
                ],
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert errors == []
    assert [binding["binding_role"] for binding in bindings] == [
        "symptom",
        "context",
    ]
    assert bindings[0]["origin_atom_field_path"] == "$.output_excerpt"
    assert bindings[0]["origin_atom_value_sha256"] == mod._canonical_json_sha256("bad")
    assert bindings[1]["origin_atom_sha256"] == "2" * 64


def test_source_value_typo_cannot_bind_null_and_reports_exact_candidate_paths() -> None:
    atom_id = "atom:error-code"
    error_code = "codex_model_messages_missing"
    snapshot = {
        "nullable": None,
        "nested": {"code": error_code},
        "prose": f"Observed {error_code} while starting the agent",
    }
    errors: list[str] = []

    bindings, direct = mod._explicit_atom_binding_receipts(
        experiment={
            "origin_evidence_bindings": [
                {
                    "atom_id": atom_id,
                    "role": "context",
                    "field_path": "$.nullable",
                    "source_value": error_code,
                }
            ]
        },
        experiment_id="experiment:baseline",
        atom_id=atom_id,
        atom_receipt={
            "atom_id": atom_id,
            "atom_sha256": mod._canonical_json_sha256(snapshot),
            "atom_snapshot": snapshot,
        },
        assertion={},
        command="runner verify",
        errors=errors,
    )

    assert bindings == []
    assert direct is False
    assert len(errors) == 1
    assert ":value_key:details=" in errors[0]
    details = json.loads(errors[0].split(":details=", 1)[1])
    assert details == {
        "candidate_field_path_count": 1,
        "candidate_field_paths": ["$.nested.code"],
        "declared_value_key": "source_value",
        "truncated": False,
    }


def test_missing_value_key_does_not_suggest_unrelated_null_atom_paths() -> None:
    atom_id = "atom:error-code"
    snapshot = {
        "nullable": None,
        "nested": {"code": "codex_model_messages_missing"},
    }
    errors: list[str] = []

    bindings, direct = mod._explicit_atom_binding_receipts(
        experiment={
            "origin_evidence_bindings": [
                {
                    "atom_id": atom_id,
                    "role": "context",
                    "field_path": "$.nested.code",
                }
            ]
        },
        experiment_id="experiment:baseline",
        atom_id=atom_id,
        atom_receipt={
            "atom_id": atom_id,
            "atom_sha256": mod._canonical_json_sha256(snapshot),
            "atom_snapshot": snapshot,
        },
        assertion={},
        command="runner verify",
        errors=errors,
    )

    assert bindings == []
    assert direct is False
    assert len(errors) == 1
    details = json.loads(errors[0].split(":details=", 1)[1])
    assert details == {
        "candidate_search_performed": False,
        "declared_value_key": "missing",
        "required_value_key": "value",
    }


def test_explicit_null_value_remains_a_valid_context_binding() -> None:
    atom_id = "atom:nullable"
    snapshot = {"nullable": None}
    errors: list[str] = []

    bindings, direct = mod._explicit_atom_binding_receipts(
        experiment={
            "origin_evidence_bindings": [
                {
                    "atom_id": atom_id,
                    "role": "context",
                    "field_path": "$.nullable",
                    "value": None,
                }
            ]
        },
        experiment_id="experiment:baseline",
        atom_id=atom_id,
        atom_receipt={
            "atom_id": atom_id,
            "atom_sha256": mod._canonical_json_sha256(snapshot),
            "atom_snapshot": snapshot,
        },
        assertion={},
        command="runner verify",
        errors=errors,
    )

    assert errors == []
    assert direct is False
    assert len(bindings) == 1
    assert bindings[0]["origin_atom_value_sha256"] == mod._canonical_json_sha256(None)


def test_wrong_atom_path_reports_the_exact_nested_path_for_the_declared_value() -> None:
    atom_id = "atom:error-code"
    snapshot = {"error": {"code": "codex_model_messages_missing"}}
    errors: list[str] = []

    bindings, direct = mod._explicit_atom_binding_receipts(
        experiment={
            "origin_evidence_bindings": [
                {
                    "atom_id": atom_id,
                    "role": "context",
                    "field_path": "$.code",
                    "value": "codex_model_messages_missing",
                }
            ]
        },
        experiment_id="experiment:baseline",
        atom_id=atom_id,
        atom_receipt={
            "atom_id": atom_id,
            "atom_sha256": mod._canonical_json_sha256(snapshot),
            "atom_snapshot": snapshot,
        },
        assertion={},
        command="runner verify",
        errors=errors,
    )

    assert bindings == []
    assert direct is False
    details = json.loads(errors[0].split(":details=", 1)[1])
    assert details["candidate_field_paths"] == ["$.error.code"]
    assert details["candidate_field_path_count"] == 1


def test_atom_path_candidates_use_json_type_identity_and_shared_path_grammar() -> None:
    snapshot = {
        "integer": 1,
        "boolean": True,
        "float": 1.0,
        "bad-key": True,
    }

    assert mod._atom_field_paths_matching_value(snapshot, True) == (["$.boolean"], 1)
    assert mod._atom_field_paths_matching_value(snapshot, 1) == (["$.integer"], 1)
    assert mod._atom_field_paths_matching_value(snapshot, 1.0) == (["$.float"], 1)


def test_explicit_symptom_binding_accepts_output_fragment_wrapping_atom_scalar() -> None:
    atom_id = "atom:error-code"
    error_code = "codex_model_messages_missing"
    snapshot = {"error": {"code": error_code}}
    assignment = {
        "status": "complete",
        "expected_atom_ids": [atom_id],
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_sha256": mod._canonical_json_sha256(snapshot),
                "atom_snapshot": snapshot,
                "artifact_receipts": [],
            }
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "wrapped-error-code",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": [atom_id],
                "command": "python .usertest_research/probe.py",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "contains",
                    "expected": f'"simulated_error_code": "{error_code}"',
                },
                "origin_evidence_bindings": [
                    {
                        "atom_id": atom_id,
                        "role": "symptom",
                        "field_path": "$.error.code",
                        "value": error_code,
                    }
                ],
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert errors == []
    assert bindings[0]["binding_role"] == "symptom"
    assert bindings[0]["origin_atom_value_sha256"] == mod._canonical_json_sha256(error_code)


def test_explicit_symptom_binding_rejects_unquoted_prose_containing_atom_scalar() -> None:
    atom_id = "atom:error-code"
    snapshot = {"error": {"code": "error"}}
    assignment = {
        "status": "complete",
        "expected_atom_ids": [atom_id],
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_sha256": mod._canonical_json_sha256(snapshot),
                "atom_snapshot": snapshot,
                "artifact_receipts": [],
            }
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "negated-error-prose",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": [atom_id],
                "command": "python .usertest_research/probe.py",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "contains",
                    "expected": "no error occurred",
                },
                "origin_evidence_bindings": [
                    {
                        "atom_id": atom_id,
                        "role": "symptom",
                        "field_path": "$.error.code",
                        "value": "error",
                    }
                ],
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert bindings == []
    assert errors == [
        "experiment_atom_binding_invalid:negated-error-prose:atom:error-code:0:"
        "not_bound_to_observation",
        "supporting_experiments_do_not_cover_origin_atoms",
        "supporting_experiments_have_no_direct_symptom_binding",
    ]


def test_explicit_symptom_binding_accepts_complete_key_value_token() -> None:
    atom_id = "atom:error-code"
    error_code = "codex_model_messages_missing"
    snapshot = {"error": {"code": error_code}}
    assignment = {
        "status": "complete",
        "expected_atom_ids": [atom_id],
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_sha256": mod._canonical_json_sha256(snapshot),
                "atom_snapshot": snapshot,
                "artifact_receipts": [],
            }
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "structured-error-line",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": [atom_id],
                "command": "python .usertest_research/probe.py",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "contains",
                    "expected": (
                        "code=codex_model_messages_missing "
                        "classification=invalid_agent_config"
                    ),
                },
                "origin_evidence_bindings": [
                    {
                        "atom_id": atom_id,
                        "role": "symptom",
                        "field_path": "$.error.code",
                        "value": error_code,
                    }
                ],
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert errors == []
    assert [binding["atom_id"] for binding in bindings] == [atom_id]


def test_explicit_field_binding_rejects_changed_value_or_unrelated_symptom() -> None:
    assignment = {
        "status": "complete",
        "expected_atom_ids": ["atom:one"],
        "atom_receipts": [
            {
                "atom_id": "atom:one",
                "atom_sha256": "1" * 64,
                "atom_snapshot": {"output_excerpt": "bad"},
                "artifact_receipts": [],
            }
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "forged",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": ["atom:one"],
                "command": "python -m product show",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "different",
                },
                "origin_evidence_bindings": [
                    {
                        "atom_id": "atom:one",
                        "role": "symptom",
                        "field_path": "$.output_excerpt",
                        "value": "bad",
                        "value_sha256": mod._canonical_json_sha256("bad"),
                    }
                ],
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert bindings == []
    assert any(error.endswith(":not_bound_to_observation") for error in errors)
    assert "supporting_experiments_do_not_cover_origin_atoms" in errors
    assert "supporting_experiments_have_no_direct_symptom_binding" in errors


@pytest.mark.parametrize(
    ("atom_value", "predicate"),
    [
        (5, {"kind": "range", "minimum": 3, "maximum": 7}),
        (False, {"kind": "equals", "expected": False}),
        (
            {"status": "broken", "attempts": 2},
            {
                "kind": "schema",
                "schema": {
                    "type": "object",
                    "required": ["status", "attempts"],
                    "properties": {
                        "status": {"type": "string"},
                        "attempts": {"type": "integer"},
                    },
                },
            },
        ),
        ({"exists": False}, {"kind": "existence", "expected": False}),
        (
            ["started", "failed"],
            {"kind": "event_sequence", "events": ["started", "failed"]},
        ),
    ],
)
def test_explicit_symptom_binding_accepts_registered_structured_predicates(
    atom_value: object,
    predicate: dict[str, object],
) -> None:
    snapshot = {"observed_symptom": atom_value}
    experiment = {
        "origin_evidence_bindings": [
            {
                "role": "symptom",
                "atom_id": "atom:structured",
                "field_path": "$.observed_symptom",
                "value": atom_value,
                "value_sha256": mod._canonical_json_sha256(atom_value),
                "observation_predicate": predicate,
            }
        ]
    }
    errors: list[str] = []

    bindings, direct = mod._explicit_atom_binding_receipts(
        experiment=experiment,
        experiment_id="experiment:baseline",
        atom_id="atom:structured",
        atom_receipt={
            "atom_id": "atom:structured",
            "atom_sha256": mod._canonical_json_sha256(snapshot),
            "atom_snapshot": snapshot,
        },
        assertion={},
        command="runner verify",
        errors=errors,
    )

    assert errors == []
    assert direct is True
    assert len(bindings) == 1
    assert bindings[0]["observation_predicate"] == predicate
    assert bindings[0]["declared_binding_sha256"] == mod._canonical_json_sha256(
        {key: value for key, value in bindings[0].items() if key != "declared_binding_sha256"}
    )


def test_explicit_symptom_predicate_binds_runner_owned_large_atom_value() -> None:
    atom_value = "windows-sandbox-rs:" + (" retained panic context" * 4096)
    snapshot = {"excerpt_tail": atom_value}
    experiment = {
        "origin_evidence_bindings": [
            {
                "role": "symptom",
                "atom_id": "atom:stderr",
                "field_path": "$.excerpt_tail",
                "observation_predicate": {
                    "kind": "contains",
                    "expected": "windows-sandbox-rs",
                },
            }
        ]
    }
    errors: list[str] = []

    bindings, direct = mod._explicit_atom_binding_receipts(
        experiment=experiment,
        experiment_id="experiment:baseline",
        atom_id="atom:stderr",
        atom_receipt={
            "atom_id": "atom:stderr",
            "atom_sha256": mod._canonical_json_sha256(snapshot),
            "atom_snapshot": snapshot,
        },
        assertion={},
        command="runner verify",
        errors=errors,
    )

    assert errors == []
    assert direct is True
    assert len(bindings) == 1
    assert bindings[0]["origin_atom_value"] == atom_value
    assert bindings[0]["origin_atom_value_sha256"] == mod._canonical_json_sha256(
        atom_value
    )


def test_structured_atom_predicate_supports_source_to_mechanism_output_transform(
    tmp_path: Path,
) -> None:
    atom_id = "atom:stderr"
    snapshot = {
        "excerpt_tail": "worker panic at windows-sandbox-rs",
        "expected_reason": "shell_probe_failed",
    }
    atom_sha256 = mod._canonical_json_sha256(snapshot)
    assignment = {
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_sha256": atom_sha256,
                "atom_snapshot": snapshot,
            }
        ]
    }
    declaration = {
        "role": "symptom",
        "atom_id": atom_id,
        "field_path": "$.excerpt_tail",
        "observation_predicate": {
            "kind": "contains",
            "expected": "windows-sandbox-rs",
        },
    }
    errors: list[str] = []
    bindings, direct = mod._explicit_atom_binding_receipts(
        experiment={"origin_evidence_bindings": [declaration]},
        experiment_id="experiment:baseline",
        atom_id=atom_id,
        atom_receipt=assignment["atom_receipts"][0],
        assertion={},
        command="runner verify",
        errors=errors,
    )
    assert errors == []
    assert direct is True

    def replay(experiment_id: str, reason_code: str) -> dict[str, object]:
        stdout = tmp_path / f"{experiment_id.replace(':', '-')}.json"
        stderr = tmp_path / f"{experiment_id.replace(':', '-')}.stderr"
        stdout.write_text(json.dumps({"reason_code": reason_code}), encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return {
            "experiment_id": experiment_id,
            "executed_argv": ["runner", "verify"],
            "exit_code": 0,
            "execution_isolation": {"platform": "windows"},
            "stdout_path": str(stdout),
            "stderr_path": str(stderr),
            "stdout_sha256": sha256(stdout.read_bytes()).hexdigest(),
            "stderr_sha256": sha256(stderr.read_bytes()).hexdigest(),
            "replay_inputs": mod._replay_inputs_receipt(
                source_experiment_id=experiment_id,
                environment_overrides={},
                disposable_state_paths=[],
            ),
        }

    claim = {
        "adapter_id": "structured_replay.v1",
        "hypothesis_id": "hypothesis:sandbox-classifier",
        "baseline_experiment_id": "experiment:baseline",
        "challenge_experiment_id": "experiment:challenge",
        "intervention": {
            "kind": "signature_mode",
            "target": "runner_core.shell_capability._resolve_shell_capability",
            "predicted_polarity": "panic_to_generic_failure",
            "before": "retained",
            "after": "removed",
        },
        "observations": {
            "baseline": {"source": "stdout_json", "json_pointer": "/reason_code"},
            "challenge": {"source": "stdout_json", "json_pointer": "/reason_code"},
        },
        "positive_outcome": {
            "predicate": {"kind": "equals", "expected": "shell_probe_failed"},
            "semantic_basis": {
                "kind": "origin_exact_value",
                "atom_id": atom_id,
                "field_path": "$.expected_reason",
            },
        },
    }
    experiments = {
        "experiment:baseline": {"experiment_id": "experiment:baseline"},
        "experiment:challenge": {
            "experiment_id": "experiment:challenge",
            "proof_adapter": claim,
        },
    }
    dossier = {
        "root_cause_hypotheses": [{"hypothesis_id": "hypothesis:sandbox-classifier"}]
    }

    proofs, diagnostics = mod._proof_adapter_receipts(
        dossier,
        case_id="case:sandbox-classifier",
        problem_id="problem:sandbox-classifier",
        experiments=experiments,
        clean_replays={
            "experiment:baseline": replay(
                "experiment:baseline", "codex_windows_sandbox_panic"
            ),
            "experiment:challenge": replay("experiment:challenge", "shell_probe_failed"),
        },
        evidence_assignment=assignment,
        atom_bindings=bindings,
        planning_workspace=None,
        symbol_receipts=[],
        artifact_receipts=[],
    )

    assert diagnostics == []
    assert len(proofs) == 1
    proof = proofs[0]
    attested = proof["source_root"]["atom_field_predicate_bindings"]
    assert len(attested) == 1
    assert attested[0]["atom_id"] == atom_id
    assert attested[0]["baseline_experiment_id"] == "experiment:baseline"
    assert attested[0]["declared_binding_sha256"] == bindings[0][
        "declared_binding_sha256"
    ]
    assert attested[0]["runner_attested"] is True
    assert attested[0]["binding_verification_method"] == (
        "runner_bound_source_predicate_with_baseline_experiment_v1"
    )
    assert proof["replay_observation"]["selector"] == {
        "source": "stdout_json",
        "json_pointer": "/reason_code",
    }
    assert proof["replay_inputs"]["source_experiment_id"] == "experiment:baseline"
    assert mod.validate_causal_proof_receipt(proof) == []


def test_structured_atom_predicate_rejects_binding_from_challenge_experiment(
    tmp_path: Path,
) -> None:
    atom_id = "atom:stderr"
    snapshot = {"excerpt_tail": "worker panic at windows-sandbox-rs"}
    assignment = {
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_sha256": mod._canonical_json_sha256(snapshot),
                "atom_snapshot": snapshot,
            }
        ]
    }
    errors: list[str] = []
    bindings, direct = mod._explicit_atom_binding_receipts(
        experiment={
            "origin_evidence_bindings": [
                {
                    "role": "symptom",
                    "atom_id": atom_id,
                    "field_path": "$.excerpt_tail",
                    "observation_predicate": {
                        "kind": "contains",
                        "expected": "windows-sandbox-rs",
                    },
                }
            ]
        },
        experiment_id="experiment:challenge",
        atom_id=atom_id,
        atom_receipt=assignment["atom_receipts"][0],
        assertion={},
        command="runner verify",
        errors=errors,
    )
    assert errors == []
    assert direct is True

    def replay(experiment_id: str, reason_code: str) -> dict[str, object]:
        stdout = tmp_path / f"{experiment_id.replace(':', '-')}.json"
        stderr = tmp_path / f"{experiment_id.replace(':', '-')}.stderr"
        stdout.write_text(json.dumps({"reason_code": reason_code}), encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return {
            "experiment_id": experiment_id,
            "executed_argv": ["runner", "verify"],
            "exit_code": 0,
            "execution_isolation": {"platform": "windows"},
            "stdout_path": str(stdout),
            "stderr_path": str(stderr),
            "stdout_sha256": sha256(stdout.read_bytes()).hexdigest(),
            "stderr_sha256": sha256(stderr.read_bytes()).hexdigest(),
            "replay_inputs": mod._replay_inputs_receipt(
                source_experiment_id=experiment_id,
                environment_overrides={},
                disposable_state_paths=[],
            ),
        }

    claim = {
        "adapter_id": "structured_replay.v1",
        "hypothesis_id": "hypothesis:sandbox-classifier",
        "baseline_experiment_id": "experiment:baseline",
        "challenge_experiment_id": "experiment:challenge",
        "intervention": {
            "kind": "signature_mode",
            "target": "runner_core.shell_capability._resolve_shell_capability",
            "predicted_polarity": "panic_to_generic_failure",
            "before": "retained",
            "after": "removed",
        },
        "observations": {
            "baseline": {"source": "stdout_json", "json_pointer": "/reason_code"},
            "challenge": {"source": "stdout_json", "json_pointer": "/reason_code"},
        },
        "positive_outcome": {
            "predicate": {"kind": "equals", "expected": "shell_probe_failed"},
            "semantic_basis": {
                "kind": "authenticated_semantic_citation",
                "atom_id": atom_id,
                "field_path": "$.excerpt_tail",
                "semantic_relation": "causal_contrast",
                "semantic_rationale": (
                    "The retained source identifies the historical classifier input."
                ),
            },
        },
    }
    experiments = {
        "experiment:baseline": {"experiment_id": "experiment:baseline"},
        "experiment:challenge": {
            "experiment_id": "experiment:challenge",
            "proof_adapter": claim,
        },
    }

    proofs, diagnostics = mod._proof_adapter_receipts(
        {
            "root_cause_hypotheses": [
                {"hypothesis_id": "hypothesis:sandbox-classifier"}
            ]
        },
        case_id="case:sandbox-classifier",
        problem_id="problem:sandbox-classifier",
        experiments=experiments,
        clean_replays={
            "experiment:baseline": replay(
                "experiment:baseline", "codex_windows_sandbox_panic"
            ),
            "experiment:challenge": replay("experiment:challenge", "shell_probe_failed"),
        },
        evidence_assignment=assignment,
        atom_bindings=bindings,
        planning_workspace=None,
        symbol_receipts=[],
        artifact_receipts=[],
    )

    assert proofs == []
    assert diagnostics == [
        {
            "experiment_id": "experiment:challenge",
            "adapter_id": "structured_replay.v1",
            "claim_sha256": mod._canonical_json_sha256(claim),
            "diagnostics": [
                f"proof_adapter_atom_predicate_binding_invalid:{atom_id}",
                "proof_adapter_source_root_unbound",
            ],
        }
    ]


def test_declared_mechanism_link_requires_runner_observed_python_call_chain(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    source.mkdir(parents=True)
    (source / "api.py").write_text(
        "from core import run\ndef execute():\n    return run()\n",
        encoding="utf-8",
    )
    (source / "core.py").write_text("def run():\n    return 'bad'\n", encoding="utf-8")
    experiment = {
        "mechanism_link": {
            "kind": "entrypoint_dataflow",
            "entrypoint": "api.execute",
            "code_path": [
                {
                    "path": "src/api.py",
                    "symbol": "api.execute",
                    "observation": "Calls the result-producing boundary.",
                },
                {
                    "path": "src/core.py",
                    "symbol": "core.run",
                    "observation": "Returns the observed wrong value.",
                },
            ],
        }
    }
    link = mod._verified_declared_mechanism_link(
        experiment=experiment,
        mechanism_symbols=["core.run"],
        symbol_paths={
            "api.execute": "src/api.py",
            "core.run": "src/core.py",
        },
        workspace=workspace,
    )
    assert link is not None
    assert link["verified_call_edges"][0]["resolved_call"] == "core.run"

    (source / "api.py").write_text(
        "def execute():\n    return 'invented nearby explanation'\n",
        encoding="utf-8",
    )
    assert (
        mod._verified_declared_mechanism_link(
            experiment=experiment,
            mechanism_symbols=["core.run"],
            symbol_paths={
                "api.execute": "src/api.py",
                "core.run": "src/core.py",
            },
            workspace=workspace,
        )
        is None
    )


def _aggregate_mechanism_fixture(
    tmp_path: Path,
) -> tuple[
    dict[str, object],
    dict[str, dict[str, object]],
    list[dict[str, str]],
    list[dict[str, object]],
]:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "core.py").write_text(
        "def enter():\n    return bridge()\n\n"
        "def bridge():\n    return resolve()\n\n"
        "def resolve():\n    return 'reported symptom'\n",
        encoding="utf-8",
    )
    overlay = workspace / ".usertest_research"
    overlay.mkdir()
    (overlay / "entry.py").write_text(
        "from src.core import enter\nvalue = enter()\nprint(value)\n",
        encoding="utf-8",
    )
    (overlay / "bridge.py").write_text(
        "from src.core import enter\nprint(enter())\n",
        encoding="utf-8",
    )
    (overlay / "resolver.py").write_text(
        "from src.core import bridge\nprint(bridge())\n",
        encoding="utf-8",
    )

    def experiment(
        experiment_id: str,
        harness: str,
        *,
        mechanism_link: dict[str, object] | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "experiment_id": experiment_id,
            "scenario_kind": "faithful_replay",
            "command": f"python .usertest_research/{harness}",
            "outcome": "supports",
            "addresses_atom_ids": ["atom:one"],
            "artifact_refs": ["artifact:one"],
            "observable_assertion": {
                "source": "stdout",
                "operator": "equals",
                "expected": "reported symptom",
            },
        }
        if mechanism_link is not None:
            value["mechanism_link"] = mechanism_link
        return value

    enter_to_bridge = {
        "kind": "entrypoint_dataflow",
        "entrypoint": "core.enter",
        "code_path": [
            {
                "path": "src/core.py",
                "symbol": "core.enter",
                "observation": "The observed entrypoint calls the production bridge.",
            },
            {
                "path": "src/core.py",
                "symbol": "core.bridge",
                "observation": "The bridge continues the production failure path.",
            },
        ],
    }
    bridge_to_resolve = {
        "kind": "entrypoint_dataflow",
        "entrypoint": "core.bridge",
        "code_path": [
            {
                "path": "src/core.py",
                "symbol": "core.bridge",
                "observation": "The bridge calls the result-producing mechanism.",
            },
            {
                "path": "src/core.py",
                "symbol": "core.resolve",
                "observation": "The resolver produces the observed symptom.",
            },
        ],
    }

    experiments = [
        experiment("entry-support", "entry.py"),
        experiment(
            "bridge-support",
            "bridge.py",
            mechanism_link=enter_to_bridge,
        ),
        experiment(
            "resolver-support",
            "resolver.py",
            mechanism_link=bridge_to_resolve,
        ),
    ]
    dossier: dict[str, object] = {
        "research_status": "evidence_sufficient",
        "experiments": experiments,
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "mechanism_symbols": ["core.enter", "core.bridge", "core.resolve"],
                "supporting_evidence": [
                    "entry-support",
                    "bridge-support",
                    "resolver-support",
                ],
                "counterevidence": [],
            }
        ],
    }
    replays = {
        str(item["experiment_id"]): {
            "executed_argv": mod._parse_replay_argv(str(item["command"])),
            "workspace_dir": str(workspace),
            "assertion_passed": True,
            "exit_code": 0,
            "stdout_sha256": str(index + 1) * 64,
            "stderr_sha256": str(index + 3) * 64,
        }
        for index, item in enumerate(experiments)
    }
    symbol_receipts = [
        {"symbol": "core.enter", "path": "src/core.py"},
        {"symbol": "core.bridge", "path": "src/core.py"},
        {"symbol": "core.resolve", "path": "src/core.py"},
    ]
    atom_bindings: list[dict[str, object]] = [
        {
            "experiment_id": "entry-support",
            "atom_id": "atom:one",
            "match_kind": "faithful_atom_evidence_symptom",
            "origin_atom_sha256": "a" * 64,
        }
    ]
    return dossier, replays, symbol_receipts, atom_bindings


def test_primary_mechanism_coverage_aggregates_verified_multi_hop_supports(
    tmp_path: Path,
) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    assert errors == []
    advancing = [item for item in receipts if item["adversarial_effect"] == "supports_selection"]
    assert {tuple(item["mechanism_symbols"]) for item in advancing} == {
        ("core.enter",),
        ("core.enter", "core.bridge"),
        ("core.bridge", "core.resolve"),
    }
    assert set().union(*(set(item["mechanism_symbols"]) for item in advancing)) == {
        "core.enter",
        "core.bridge",
        "core.resolve",
    }
    projection = mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    )
    assert projection[0] == {
        "schema_version": 3,
        "mechanism_symbols": ["core.bridge", "core.enter", "core.resolve"],
        "code_paths": [
            {"symbol": "core.bridge", "path": "src/core.py"},
            {"symbol": "core.enter", "path": "src/core.py"},
            {"symbol": "core.resolve", "path": "src/core.py"},
        ],
    }
    assert projection[2] is not None
    assert sorted(item["connection_kind"] for item in projection[2]["support_connectivity"]) == [
        "causal_root",
        "runner_verified_causal_edge",
        "runner_verified_causal_edge",
    ]
    assert len(projection[2]["causal_root_evidence_ids"]) == 1
    closures = mod._deterministic_mechanism_closure_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        mechanism_evidence=receipts,
    )
    assert len(closures) == 1
    assert closures[0]["support_experiment_ids"] == [
        "bridge-support",
        "entry-support",
        "resolver-support",
    ]
    assert closures[0]["mechanism_evidence_ids"] == sorted(
        receipt["mechanism_evidence_id"] for receipt in advancing
    )
    assert closures[0]["closure_basis"] == "rooted_connected_support_component"
    reversed_projection = mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=list(reversed(receipts)),
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    )
    assert reversed_projection == projection


def test_pair_print_harness_overlap_cannot_manufacture_production_connectivity(
    tmp_path: Path,
) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    workspace = Path(str(replays["entry-support"]["workspace_dir"]))
    (workspace / "src" / "core.py").write_text(
        "def enter():\n    return 'reported symptom'\n\n"
        "def bridge():\n    return 'mechanism bridge'\n\n"
        "def resolve():\n    return 'root mechanism'\n",
        encoding="utf-8",
    )
    (workspace / ".usertest_research" / "bridge.py").write_text(
        "from src.core import bridge, enter\nprint(f'{enter()}|{bridge()}')\n",
        encoding="utf-8",
    )
    (workspace / ".usertest_research" / "resolver.py").write_text(
        "from src.core import bridge, resolve\nprint(f'{bridge()}|{resolve()}')\n",
        encoding="utf-8",
    )
    experiments = dossier["experiments"]
    assert isinstance(experiments, list)
    for experiment in experiments:
        assert isinstance(experiment, dict)
        experiment.pop("mechanism_link", None)
    experiments[1]["observable_assertion"]["expected"] = "reported symptom|mechanism bridge"
    experiments[2]["observable_assertion"]["expected"] = "mechanism bridge|root mechanism"
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    advancing = [
        receipt for receipt in receipts if receipt.get("adversarial_effect") == "supports_selection"
    ]
    assert [receipt["experiment_ids"] for receipt in advancing] == [["entry-support"]]
    assert {
        "primary_hypothesis_support_disconnected:h1:bridge-support",
        "primary_hypothesis_support_disconnected:h1:resolver-support",
        "primary_hypothesis_mechanism_coverage_incomplete:h1:core.bridge,core.resolve",
    }.issubset(set(errors))


def test_primary_mechanism_coverage_rejects_aggregate_symbol_omission(tmp_path: Path) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    dossier["experiments"] = dossier["experiments"][:2]
    replays.pop("resolver-support")
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    assert "primary_hypothesis_mechanism_coverage_incomplete:h1:core.resolve" in errors
    assert mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    ) == (None, None, None, None)


def test_primary_mechanism_coverage_requires_origin_symptom_entrypoint(tmp_path: Path) -> None:
    dossier, replays, symbol_receipts, _atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=[],
        errors=errors,
    )

    assert "primary_hypothesis_causal_root_missing:h1" in errors
    assert mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    ) == (None, None, None, None)


def test_disconnected_symbol_union_cannot_advance_or_create_positive_evidence(
    tmp_path: Path,
) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    experiments = dossier["experiments"]
    assert isinstance(experiments, list)
    dossier["experiments"] = [experiments[0], experiments[2]]
    hypothesis = dossier["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["mechanism_symbols"] = ["core.enter", "core.resolve"]
    hypothesis["supporting_evidence"] = ["entry-support", "resolver-support"]
    workspace = Path(str(replays["entry-support"]["workspace_dir"]))
    (workspace / ".usertest_research" / "resolver.py").write_text(
        "from src.core import resolve\nprint(resolve())\n",
        encoding="utf-8",
    )
    assert isinstance(experiments[2], dict)
    experiments[2].pop("mechanism_link", None)
    replays.pop("bridge-support")
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    assert "primary_hypothesis_support_disconnected:h1:resolver-support" in errors
    assert "primary_hypothesis_mechanism_coverage_incomplete:h1:core.resolve" in errors
    advancing_experiments = {
        experiment_id
        for receipt in receipts
        if receipt.get("adversarial_effect") == "supports_selection"
        for experiment_id in receipt.get("experiment_ids", [])
    }
    assert advancing_experiments == {"entry-support"}
    assert mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    ) == (None, None, None, None)


def test_two_independently_rooted_disconnected_supports_cannot_union(
    tmp_path: Path,
) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    experiments = dossier["experiments"]
    assert isinstance(experiments, list)
    dossier["experiments"] = [experiments[0], experiments[2]]
    hypothesis = dossier["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["mechanism_symbols"] = ["core.enter", "core.resolve"]
    hypothesis["supporting_evidence"] = ["entry-support", "resolver-support"]
    workspace = Path(str(replays["entry-support"]["workspace_dir"]))
    (workspace / ".usertest_research" / "resolver.py").write_text(
        "from src.core import resolve\nprint(resolve())\n",
        encoding="utf-8",
    )
    assert isinstance(experiments[2], dict)
    experiments[2].pop("mechanism_link", None)
    replays.pop("bridge-support")
    atom_bindings.append(
        {
            "experiment_id": "resolver-support",
            "atom_id": "atom:one",
            "match_kind": "faithful_atom_evidence_symptom",
            "origin_atom_sha256": "a" * 64,
        }
    )
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    assert len(receipts) == 1
    selected_experiment = receipts[0]["experiment_ids"][0]
    if selected_experiment == "entry-support":
        expected_errors = {
            "primary_hypothesis_support_disconnected:h1:resolver-support",
            "primary_hypothesis_mechanism_coverage_incomplete:h1:core.resolve",
        }
    else:
        assert selected_experiment == "resolver-support"
        expected_errors = {
            "primary_hypothesis_support_disconnected:h1:entry-support",
            "primary_hypothesis_mechanism_coverage_incomplete:h1:core.enter",
        }
    assert set(errors) == expected_errors
    assert mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    ) == (None, None, None, None)


def test_exact_immutable_source_command_can_root_connected_supports(tmp_path: Path) -> None:
    dossier, replays, symbol_receipts, _atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    entry_replay = replays["entry-support"]
    argv = entry_replay["executed_argv"]
    assert isinstance(argv, list)
    entry_replay["command_authorization"] = mod._command_authorization_receipt(
        {
            "authorization_kind": "immutable_source_command",
            "executed_argv_sha256": mod._canonical_json_sha256(argv),
            "shell": False,
            "workspace_confined": True,
            "origin_atom_id": "atom:one",
            "origin_atom_sha256": "a" * 64,
            "origin_atom_field_path": "$.command",
            "origin_command_value_sha256": "b" * 64,
        }
    )
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=[],
        errors=errors,
    )

    assert errors == []
    root = next(
        receipt for receipt in receipts if receipt.get("experiment_ids") == ["entry-support"]
    )
    assert root["causal_root_bindings"][0]["kind"] == "immutable_source_command"
    assert root["consumer_identity"] == {
        "kind": "research_harness",
        "entrypoint": ".usertest_research/entry.py",
    }
    projection = mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    )
    assert projection[0] is not None
    assert projection[2] is not None
    assert len(projection[2]["causal_root_evidence_ids"]) == 1


def test_inspected_entrypoint_authorization_is_not_an_immutable_causal_root(
    tmp_path: Path,
) -> None:
    dossier, replays, symbol_receipts, _atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    entry_replay = replays["entry-support"]
    argv = entry_replay["executed_argv"]
    assert isinstance(argv, list)
    entry_replay["command_authorization"] = {
        "authorization_kind": "declared_inspected_repository_entrypoint",
        "executed_argv_sha256": mod._canonical_json_sha256(argv),
        "shell": False,
        "workspace_confined": True,
    }
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=[],
        errors=errors,
    )

    assert "primary_hypothesis_causal_root_missing:h1" in errors
    assert not any(
        receipt.get("adversarial_effect") == "supports_selection" for receipt in receipts
    )


@pytest.mark.parametrize(
    "mechanism_symbols",
    [
        ["core.enter", "core.enter"],
        ["core.enter", " core.enter "],
    ],
)
def test_duplicate_hypothesis_mechanism_symbols_are_rejected_before_evidence(
    tmp_path: Path,
    mechanism_symbols: list[str],
) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    hypothesis = dossier["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["mechanism_symbols"] = mechanism_symbols
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    assert errors == ["hypothesis_mechanism_symbols_duplicate:h1:core.enter"]
    assert receipts == []
    assert mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=[],
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    ) == (None, None, None, None)


def test_research_harness_identity_is_not_promoted_by_a_production_link() -> None:
    identity = mod._experiment_consumer_identity(
        experiment={"experiment_id": "support"},
        replay={"executed_argv": ["python", ".usertest_research/probe.py"]},
        mechanism_link={
            "verification_method": "runner_python_call_chain_v1",
            "entrypoint": "api.execute",
        },
        harness_path=".usertest_research/probe.py",
    )

    assert identity == {
        "kind": "research_harness",
        "entrypoint": ".usertest_research/probe.py",
    }


def _connectivity_edge_supports() -> list[dict[str, object]]:
    link: dict[str, object] = {
        "verification_method": "runner_python_call_chain_v1",
        "entrypoint": "core.enter",
        "code_path": [
            {"symbol": "core.enter", "path": "src/core.py", "observation": "caller"},
            {"symbol": "core.resolve", "path": "src/core.py", "observation": "callee"},
        ],
        "verified_call_edges": [
            {
                "caller_symbol": "core.enter",
                "caller_path": "src/core.py",
                "callee_symbol": "core.resolve",
                "callee_path": "src/core.py",
                "line": 4,
                "resolved_call": "core.resolve",
                "call_ast_sha256": "c" * 64,
            }
        ],
    }
    link["mechanism_link_sha256"] = mod._canonical_json_sha256(link)
    return [
        {
            "mechanism_evidence_id": "mechanism_evidence:root",
            "mechanism_symbols": ["core.enter"],
            "experiment_ids": ["root"],
            "causal_root_bindings": [
                {
                    "kind": "origin_symptom_observation",
                    "experiment_ids": ["root"],
                    "origin_atom_ids": ["atom:one"],
                    "root_mechanism_symbol": "core.enter",
                }
            ],
            "mechanism_link": None,
        },
        {
            "mechanism_evidence_id": "mechanism_evidence:tail",
            "mechanism_symbols": ["core.resolve"],
            "experiment_ids": ["tail"],
            "causal_root_bindings": [],
            "mechanism_link": link,
        },
    ]


def test_runner_minted_causal_edge_can_connect_disjoint_support_symbol_sets() -> None:
    connected, symbols, trace, disconnected = mod._rooted_support_connectivity(
        _connectivity_edge_supports(),
        hypothesis_symbols=["core.enter", "core.resolve"],
    )

    assert len(connected) == 2
    assert symbols == {"core.enter", "core.resolve"}
    assert disconnected == []
    assert sorted(item["connection_kind"] for item in trace) == [
        "causal_root",
        "runner_verified_causal_edge",
    ]


def test_unattested_causal_edge_cannot_connect_disjoint_supports() -> None:
    supports = _connectivity_edge_supports()
    mechanism_link = supports[1]["mechanism_link"]
    assert isinstance(mechanism_link, dict)
    mechanism_link["mechanism_link_sha256"] = "0" * 64

    connected, symbols, trace, disconnected = mod._rooted_support_connectivity(
        supports,
        hypothesis_symbols=["core.enter", "core.resolve"],
    )

    assert len(connected) == 1
    assert symbols == {"core.enter"}
    assert [item["connection_kind"] for item in trace] == ["causal_root"]
    assert disconnected == ["mechanism_evidence:tail"]


def test_runner_edge_cannot_leak_from_its_receipt_to_unrelated_support() -> None:
    supports = _connectivity_edge_supports()
    mechanism_link = supports[1]["mechanism_link"]
    supports[1]["mechanism_link"] = None
    supports.append(
        {
            "mechanism_evidence_id": "mechanism_evidence:edge-owner",
            "mechanism_symbols": ["core.other"],
            "experiment_ids": ["edge-owner"],
            "causal_root_bindings": [],
            "mechanism_link": mechanism_link,
        }
    )

    connected, symbols, trace, disconnected = mod._rooted_support_connectivity(
        supports,
        hypothesis_symbols=["core.enter", "core.resolve", "core.other"],
    )

    assert [item["mechanism_evidence_id"] for item in connected] == ["mechanism_evidence:root"]
    assert symbols == {"core.enter"}
    assert [item["connection_kind"] for item in trace] == ["causal_root"]
    assert disconnected == ["mechanism_evidence:edge-owner", "mechanism_evidence:tail"]


def test_runner_minted_causal_edge_cannot_be_traversed_from_callee_to_caller() -> None:
    supports = _connectivity_edge_supports()
    supports[1]["causal_root_bindings"] = [
        {
            "kind": "origin_symptom_observation",
            "experiment_ids": ["tail"],
            "origin_atom_ids": ["atom:one"],
            "root_mechanism_symbol": "core.resolve",
        }
    ]
    supports[0]["causal_root_bindings"] = []

    connected, symbols, trace, disconnected = mod._rooted_support_connectivity(
        supports,
        hypothesis_symbols=["core.enter", "core.resolve"],
    )

    assert [item["mechanism_evidence_id"] for item in connected] == ["mechanism_evidence:tail"]
    assert symbols == {"core.resolve"}
    assert [item["connection_kind"] for item in trace] == ["causal_root"]
    assert disconnected == ["mechanism_evidence:root"]


def test_root_selection_prefers_broader_component_then_lexical_tie() -> None:
    bridge_link: dict[str, object] = {
        "verification_method": "runner_python_call_chain_v1",
        "entrypoint": "core.bridge",
        "code_path": [
            {"symbol": "core.bridge", "path": "src/core.py", "observation": "caller"},
            {"symbol": "core.resolve", "path": "src/core.py", "observation": "callee"},
        ],
        "verified_call_edges": [
            {
                "caller_symbol": "core.bridge",
                "caller_path": "src/core.py",
                "callee_symbol": "core.resolve",
                "callee_path": "src/core.py",
                "line": 4,
                "resolved_call": "core.resolve",
                "call_ast_sha256": "d" * 64,
            }
        ],
    }
    bridge_link["mechanism_link_sha256"] = mod._canonical_json_sha256(bridge_link)
    supports = [
        {
            "mechanism_evidence_id": "mechanism_evidence:a",
            "mechanism_symbols": ["core.enter"],
            "experiment_ids": ["entry"],
            "causal_root_bindings": [
                {
                    "kind": "origin_symptom_observation",
                    "root_mechanism_symbol": "core.enter",
                }
            ],
        },
        {
            "mechanism_evidence_id": "mechanism_evidence:b",
            "mechanism_symbols": ["core.bridge"],
            "experiment_ids": ["bridge-root"],
            "causal_root_bindings": [
                {
                    "kind": "origin_symptom_observation",
                    "root_mechanism_symbol": "core.bridge",
                }
            ],
        },
        {
            "mechanism_evidence_id": "mechanism_evidence:c",
            "mechanism_symbols": ["core.bridge", "core.resolve"],
            "experiment_ids": ["bridge-tail"],
            "causal_root_bindings": [],
            "mechanism_link": bridge_link,
        },
    ]

    connected, symbols, _trace, disconnected = mod._rooted_support_connectivity(
        supports,
        hypothesis_symbols=["core.enter", "core.bridge", "core.resolve"],
    )

    assert [item["mechanism_evidence_id"] for item in connected] == [
        "mechanism_evidence:b",
        "mechanism_evidence:c",
    ]
    assert symbols == {"core.bridge", "core.resolve"}
    assert disconnected == ["mechanism_evidence:a"]

    tied, tied_symbols, _tied_trace, tied_disconnected = mod._rooted_support_connectivity(
        supports[:2],
        hypothesis_symbols=["core.enter", "core.bridge", "core.resolve"],
    )

    assert [item["mechanism_evidence_id"] for item in tied] == ["mechanism_evidence:a"]
    assert tied_symbols == {"core.enter"}
    assert tied_disconnected == ["mechanism_evidence:b"]


def test_control_can_verify_a_shared_nonempty_hypothesis_subset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_guarded_control")
    hypothesis = dossier["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["mechanism_symbols"] = ["core.run", "core.other"]
    errors: list[str] = []

    _, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[
            {"symbol": "core.run", "path": "src/core.py"},
            {"symbol": "core.other", "path": "src/core.py"},
        ],
        errors=errors,
    )

    assert errors == []
    assert len(controls) == 1
    assert controls[0]["mechanism_symbols"] == ["core.run"]
    assert controls[0]["support_verified_mechanism_symbols"] == ["core.run"]
    assert controls[0]["control_verified_mechanism_symbols"] == ["core.run"]


@pytest.mark.parametrize(
    ("relationship_symbols", "expected_error"),
    [
        (
            ["core.other"],
            "causal_control_mechanism_coverage_missing:h1:control",
        ),
        (
            ["outside.mode"],
            "causal_control_mechanism_subset_invalid:h1:control",
        ),
    ],
)
def test_control_rejects_mismatched_or_unverified_mechanism_subset(
    tmp_path: Path,
    relationship_symbols: list[str],
    expected_error: str,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_guarded_control")
    hypothesis = dossier["root_cause_hypotheses"][0]
    control = dossier["experiments"][1]
    assert isinstance(hypothesis, dict)
    assert isinstance(control, dict)
    hypothesis["mechanism_symbols"] = ["core.run", "core.other"]
    relationship = control["control_relationship"]
    assert isinstance(relationship, dict)
    relationship["mechanism_symbols"] = relationship_symbols
    errors: list[str] = []

    _, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[
            {"symbol": "core.run", "path": "src/core.py"},
            {"symbol": "core.other", "path": "src/core.py"},
        ],
        errors=errors,
    )

    assert controls == []
    assert expected_error in errors


@pytest.mark.parametrize(
    "python_prefix",
    [
        pytest.param(["python"], id="direct"),
        pytest.param(["python", "-B"], id="safe-interpreter-flag"),
        pytest.param(["pdm", "run", "python", "-B"], id="project-runner-safe-flag"),
    ],
)
def test_retained_harness_scalar_intervention_survives_with_runner_bound_flow(
    tmp_path: Path,
    python_prefix: list[str],
) -> None:
    baseline_workspace = tmp_path / "baseline-workspace"
    challenge_workspace = tmp_path / "challenge-workspace"
    harness = ".usertest_research/probe.py"
    for workspace in (baseline_workspace, challenge_workspace):
        (workspace / "src").mkdir(parents=True)
        (workspace / "pyproject.toml").write_text(
            "[project]\nname = 'retained-harness-fixture'\nversion = '0.0.0'\n",
            encoding="utf-8",
        )
        (workspace / "src" / "core.py").write_text(
            "def run(mode):\n    return 'bad'\n",
            encoding="utf-8",
        )
        (workspace / ".usertest_research").mkdir()
        (workspace / harness).write_text(
            "import sys\nfrom src.core import run\nvalue = run(sys.argv[1])\nprint(value)\n",
            encoding="utf-8",
        )

    def experiment(experiment_id: str, value: str, *, scenario_kind: str) -> dict[str, object]:
        result: dict[str, object] = {
            "experiment_id": experiment_id,
            "scenario_kind": scenario_kind,
            "addresses_atom_ids": ["atom:one"],
            "artifact_refs": ["artifact:one"],
            "command": " ".join([*python_prefix, harness, value]),
            "outcome": "supports",
            "observable_assertion": {
                "source": "stdout",
                "operator": "equals",
                "expected": "bad",
            },
        }
        if scenario_kind == "control":
            result["control_relationship"] = {
                "supports_experiment_id": "baseline",
                "mechanism_symbols": ["core.run"],
                "controlled_variable": "mode scalar",
                "expected_difference": "Changing the mode should remove the failure.",
            }
        return result

    baseline = experiment("baseline", "legacy", scenario_kind="faithful_replay")
    challenge = experiment("challenge", "alternative", scenario_kind="control")
    dossier = {
        "experiments": [baseline, challenge],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "core.run ignores the mode and returns bad.",
                "mechanism_symbols": ["core.run"],
                "supporting_evidence": ["baseline", "challenge"],
                "counterevidence": [],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:scalar",
                        "hypothesis_id": "h1",
                        "claim": "core.run ignores the mode and returns bad.",
                        "baseline_experiment_id": "baseline",
                        "challenge_experiment_id": "challenge",
                        "disproof_condition": {
                            "source": "stdout",
                            "operator": "equals",
                            "expected": "correct",
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
    }

    def replay_context(workspace: Path) -> dict[str, object]:
        pythonpath, repository_import = mod._repository_python_import_environment(workspace)
        assert pythonpath is not None
        environment = {"CI": "1", "PYTHONPATH": pythonpath}
        return {
            "replay_setup_receipt": mod._replay_setup_receipt(
                environment_overrides={},
                disposable_state_paths=[],
            ),
            "execution_isolation": {
                "executor": "trusted_host",
                "platform": "windows",
                "os_sandbox": False,
                "network": "not_enforced",
                "filesystem_isolation": "dedicated_clone_only_not_os_sandbox",
                "trust_decision": "approved_local_source_root",
                "sanitized_environment_keys": sorted(environment),
            },
            "execution_metadata": {
                "executor": "trusted_host",
                "environment_attestation": mod.environment_attestation(environment),
                "repository_python_import": repository_import,
            },
        }

    replays = {
        experiment_id: {
            "executed_argv": mod._parse_replay_argv(str(experiment["command"])),
            "workspace_dir": str(
                baseline_workspace if experiment_id == "baseline" else challenge_workspace
            ),
            "workspace_head": "a" * 40,
            "overlay_manifest_sha256": "b" * 64,
            **replay_context(
                baseline_workspace if experiment_id == "baseline" else challenge_workspace
            ),
            "assertion_passed": True,
            "exit_code": 0,
            "stdout_sha256": hash_character * 64,
            "stderr_sha256": "0" * 64,
        }
        for experiment_id, experiment, hash_character in (
            ("baseline", baseline, "1"),
            ("challenge", challenge, "2"),
        )
    }
    errors: list[str] = []

    receipts = mod._falsification_intervention_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=baseline_workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert errors == []
    assert len(receipts) == 1
    assert receipts[0]["verification_method"] == "runner_argv_falsification_intervention_v2"
    assert receipts[0]["mechanism_verification_mode"] == ("retained_harness_observable_dataflow")
    difference = receipts[0]["controlled_input_difference"]
    assert difference["verification_method"] == "retained_harness_scalar_argv_delta_v1"
    assert difference["difference"]["runtime_argv_index"] == 1
    assert difference["difference"]["mechanism_argument_bindings"][0]["symbol"] == "core.run"
    assert difference["difference"]["workspace_head"] == "a" * 40
    baseline_environment = replays["baseline"]["execution_metadata"]["environment_attestation"]
    challenge_environment = replays["challenge"]["execution_metadata"]["environment_attestation"]
    assert (
        baseline_environment["environment_attestation_sha256"]
        != challenge_environment["environment_attestation_sha256"]
    )
    assert mod._retained_harness_replay_context(
        replays["baseline"]
    ) == mod._retained_harness_replay_context(replays["challenge"])

    for workspace in (baseline_workspace, challenge_workspace):
        (workspace / harness).write_text(
            "import sys\nfrom src.core import run\nvalue = run(sys.argv[1])\n"
            "print('unrelated')\n",
            encoding="utf-8",
        )
    unverified_errors: list[str] = []
    assert (
        mod._falsification_intervention_receipts(
            dossier,
            clean_replays=replays,
            planning_workspace=baseline_workspace,
            symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
            errors=unverified_errors,
        )
        == []
    )
    assert "falsification_intervention_shared_mechanism_missing:h1:attempt:scalar" in (
        unverified_errors
    )
    assert (
        "falsification_intervention_unverified:h1:attempt:scalar:"
        "runner_argv_shared_mechanism_missing"
    ) in unverified_errors

    (challenge_workspace / harness).write_text(
        "import sys\nfrom src.core import run\nprint('forged', run(sys.argv[1]))\n",
        encoding="utf-8",
    )
    assert (
        mod._retained_harness_scalar_argv_difference(
            baseline_replay=replays["baseline"],
            challenge_replay=replays["challenge"],
            mechanism_symbols=["core.run"],
            symbol_paths={"core.run": "src/core.py"},
        )
        is None
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-c", ".usertest_research/probe.py"],
        ["python", "-X", "dev", ".usertest_research/probe.py"],
        ["python", "-m", ".usertest_research.probe"],
    ],
)
def test_research_harness_path_rejects_python_modes_with_unbound_semantics(
    argv: list[str],
) -> None:
    assert mod._research_harness_relative_path(argv) is None


@pytest.mark.parametrize("mismatch", ["setup_environment", "effective_environment", "platform"])
def test_retained_harness_scalar_intervention_rejects_material_replay_context_mismatch(
    tmp_path: Path,
    mismatch: str,
) -> None:
    baseline_workspace = tmp_path / "baseline-workspace"
    challenge_workspace = tmp_path / "challenge-workspace"
    harness = ".usertest_research/probe.py"
    for workspace in (baseline_workspace, challenge_workspace):
        (workspace / "src").mkdir(parents=True)
        (workspace / "pyproject.toml").write_text(
            "[project]\nname = 'retained-harness-fixture'\nversion = '0.0.0'\n",
            encoding="utf-8",
        )
        (workspace / "src" / "core.py").write_text(
            "def run(mode):\n    return 'bad'\n",
            encoding="utf-8",
        )
        (workspace / ".usertest_research").mkdir()
        (workspace / harness).write_text(
            "import sys\nfrom src.core import run\nvalue = run(sys.argv[1])\nprint(value)\n",
            encoding="utf-8",
        )

    def replay(workspace: Path, value: str) -> dict[str, object]:
        pythonpath, repository_import = mod._repository_python_import_environment(workspace)
        assert pythonpath is not None
        environment = {"CI": "1", "PYTHONPATH": pythonpath}
        return {
            "executed_argv": ["python", harness, value],
            "workspace_dir": str(workspace),
            "workspace_head": "a" * 40,
            "overlay_manifest_sha256": "b" * 64,
            "replay_setup_receipt": mod._replay_setup_receipt(
                environment_overrides={},
                disposable_state_paths=[],
            ),
            "execution_isolation": {
                "executor": "trusted_host",
                "platform": "windows",
                "os_sandbox": False,
                "network": "not_enforced",
                "filesystem_isolation": "dedicated_clone_only_not_os_sandbox",
                "trust_decision": "approved_local_source_root",
                "sanitized_environment_keys": sorted(environment),
            },
            "execution_metadata": {
                "executor": "trusted_host",
                "environment_attestation": mod.environment_attestation(environment),
                "repository_python_import": repository_import,
            },
        }

    baseline = replay(baseline_workspace, "legacy")
    challenge = replay(challenge_workspace, "alternative")
    if mismatch == "setup_environment":
        challenge["replay_setup_receipt"] = mod._replay_setup_receipt(
            environment_overrides={"FEATURE_MODE": "alternative"},
            disposable_state_paths=[],
        )
        metadata = challenge["execution_metadata"]
        assert isinstance(metadata, dict)
        effective_environment = {
            "CI": "1",
            "PYTHONPATH": str(challenge_workspace / "src"),
            "FEATURE_MODE": "alternative",
        }
        metadata["environment_attestation"] = mod.environment_attestation(effective_environment)
    elif mismatch == "effective_environment":
        metadata = challenge["execution_metadata"]
        assert isinstance(metadata, dict)
        metadata["environment_attestation"] = mod.environment_attestation(
            {
                "CI": "0",
                "PYTHONPATH": str(challenge_workspace / "src"),
            }
        )
    else:
        isolation = challenge["execution_isolation"]
        assert isinstance(isolation, dict)
        isolation["platform"] = "linux"

    assert (
        mod._retained_harness_scalar_argv_difference(
            baseline_replay=baseline,
            challenge_replay=challenge,
            mechanism_symbols=["core.run"],
            symbol_paths={"core.run": "src/core.py"},
        )
        is None
    )


@pytest.mark.parametrize(
    ("challenge_relative", "expected_mode", "expected_error"),
    [
        (
            ".usertest_research/challenge.py",
            None,
            "falsification_intervention_unverified:h1:attempt:subset",
        ),
        (
            "tools/challenge.py",
            None,
            "falsification_intervention_unverified:h1:attempt:subset",
        ),
    ],
)
def test_falsification_pair_requires_same_independently_verified_subset_mode(
    tmp_path: Path,
    challenge_relative: str,
    expected_mode: str | None,
    expected_error: str | None,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "core.py").write_text(
        "def run():\n    return 'bad'\n\ndef other():\n    return 'other'\n",
        encoding="utf-8",
    )
    (workspace / ".usertest_research").mkdir()
    baseline_relative = ".usertest_research/baseline.py"
    (workspace / baseline_relative).write_text(
        "from src.core import run\nvalue = run()\nprint(value)\n",
        encoding="utf-8",
    )
    challenge_path = workspace / challenge_relative
    challenge_path.parent.mkdir(parents=True, exist_ok=True)
    if challenge_relative.startswith(".usertest_research/"):
        challenge_path.write_text(
            "from src.core import run\nvalue = run()\nprint(value)\n",
            encoding="utf-8",
        )
        challenge_link = None
    else:
        challenge_path.write_text(
            "from src.core import run\n\ndef execute():\n    return run()\n\n"
            "if __name__ == '__main__':\n    print(execute())\n",
            encoding="utf-8",
        )
        challenge_link = {
            "kind": "entrypoint_dataflow",
            "entrypoint": "challenge.execute",
            "code_path": [
                {
                    "path": "tools/challenge.py",
                    "symbol": "challenge.execute",
                    "observation": "Calls the selected mechanism.",
                },
                {
                    "path": "src/core.py",
                    "symbol": "core.run",
                    "observation": "Returns the observed value.",
                },
            ],
        }
    baseline_command = f"python {baseline_relative}"
    challenge_command = f"python {challenge_relative}"
    baseline: dict[str, object] = {
        "experiment_id": "baseline",
        "scenario_kind": "faithful_replay",
        "addresses_atom_ids": ["atom:one"],
        "artifact_refs": ["artifact:one"],
        "command": baseline_command,
        "outcome": "supports",
        "observable_assertion": {
            "source": "stdout",
            "operator": "equals",
            "expected": "bad",
        },
    }
    challenge: dict[str, object] = {
        "experiment_id": "challenge",
        "scenario_kind": "control",
        "addresses_atom_ids": ["atom:one"],
        "artifact_refs": ["artifact:one"],
        "command": challenge_command,
        "outcome": "supports",
        "observable_assertion": {
            "source": "stdout",
            "operator": "equals",
            "expected": "bad",
        },
        "control_relationship": {
            "supports_experiment_id": "baseline",
            "mechanism_symbols": ["core.run"],
            "controlled_variable": "input program",
            "expected_difference": "The alternative program disproves the mechanism.",
        },
    }
    if challenge_link is not None:
        challenge["mechanism_link"] = challenge_link
    dossier: dict[str, object] = {
        "experiments": [baseline, challenge],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "core.run produces the wrong value.",
                "mechanism_symbols": ["core.run", "core.other"],
                "supporting_evidence": ["baseline", "challenge"],
                "counterevidence": [],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:subset",
                        "hypothesis_id": "h1",
                        "claim": "core.run produces the wrong value.",
                        "baseline_experiment_id": "baseline",
                        "challenge_experiment_id": "challenge",
                        "disproof_condition": {
                            "source": "stdout",
                            "operator": "equals",
                            "expected": "correct",
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
    }
    replays = {
        "baseline": {
            "executed_argv": mod._parse_replay_argv(baseline_command),
            "workspace_dir": str(workspace),
            "assertion_passed": True,
            "exit_code": 0,
            "stdout_sha256": "1" * 64,
            "stderr_sha256": "2" * 64,
        },
        "challenge": {
            "executed_argv": ["python", challenge_relative],
            "workspace_dir": str(workspace),
            "assertion_passed": True,
            "exit_code": 0,
            "stdout_sha256": "3" * 64,
            "stderr_sha256": "4" * 64,
        },
    }
    errors: list[str] = []

    receipts = mod._falsification_intervention_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[
            {"symbol": "core.run", "path": "src/core.py"},
            {"symbol": "core.other", "path": "src/core.py"},
            {"symbol": "challenge.execute", "path": "tools/challenge.py"},
        ],
        errors=errors,
    )

    if expected_error is not None:
        assert receipts == []
        assert any(error.startswith(expected_error) for error in errors)
    else:
        assert errors == []
        assert len(receipts) == 1
        assert receipts[0]["mechanism_symbols"] == ["core.run"]
        assert receipts[0]["baseline_verified_mechanism_symbols"] == ["core.run"]
        assert receipts[0]["challenge_verified_mechanism_symbols"] == ["core.run"]
        assert receipts[0]["mechanism_verification_mode"] == expected_mode
