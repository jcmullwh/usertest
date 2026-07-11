from __future__ import annotations

import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from agent_adapters import (
    CODEX_CHATGPT_SUBSCRIPTION_BASE_URL,
    CODEX_OPENAI_SUBSCRIPTION_BASE_URL,
    CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS,
    CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
)
from agent_adapters.read_attestation import observed_read_attestation
from backlog_core import infer_live_verification_requirement
from backlog_core.stage_contracts import (
    assess_research_readiness,
    evidence_assignment_sha256,
    evidence_verification_sha256,
)
from runner_core import RunnerConfig, RunRequest, RunResult
from runner_core.codex_execpolicy import (
    CONTROLLED_CODEX_WINDOWS_SANDBOX_CONFIG_OVERRIDE,
    codex_execpolicy_receipt_sha256,
)

import backlog_miner.research_runner as mod
from backlog_miner.research_evidence import (
    TrustedHostReplayExecutor,
    verify_persisted_research_evidence,
)


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _cfg(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path / "runs",
        agents={},
        policies={},
    )


def _write_valid_codex_subscription_receipt(run_dir: Path) -> Path:
    host_home = run_dir / "host-codex-home"
    host_home.mkdir(exist_ok=True)
    platform_overrides = (
        [CONTROLLED_CODEX_WINDOWS_SANDBOX_CONFIG_OVERRIDE] if os.name == "nt" else []
    )
    controlled_overrides = [
        *platform_overrides,
        *CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
    ]
    activation_overrides = [
        *platform_overrides,
        "model_reasoning_effort=low",
        *CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
    ]

    def _canonical_hash(value: object) -> str:
        return sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

    config_contract: dict[str, object] = {
        "schema_version": 2,
        "status": "bound",
        "platform_os_name": os.name,
        "user_config_ignored": True,
        "target_project_config_isolated": True,
        "canonical_route_overrides": list(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES),
        "canonical_subscription_route_verified": True,
        "native_windows_sandbox_mode": ("unelevated" if os.name == "nt" else "not_applicable"),
        "controlled_rules_enforcement_mode": (
            "ignored_native_windows_sandbox" if os.name == "nt" else "project_execpolicy"
        ),
        "controlled_rules_ignored": os.name == "nt",
        "controlled_rules_written": os.name != "nt",
        "activation_safe_delta": ["model_reasoning_effort=low"],
        "preflight_overrides": controlled_overrides,
        "preflight_overrides_sha256": _canonical_hash(controlled_overrides),
        "activation_overrides": activation_overrides,
        "activation_overrides_sha256": _canonical_hash(activation_overrides),
        "mission_overrides": controlled_overrides,
        "mission_overrides_sha256": _canonical_hash(controlled_overrides),
        "postcheck_overrides": controlled_overrides,
        "postcheck_overrides_sha256": _canonical_hash(controlled_overrides),
    }
    config_contract["contract_sha256"] = _canonical_hash(config_contract)
    config_contract_path = run_dir / "codex_execpolicy_config_overrides.json"
    _write_json(config_contract_path, config_contract)
    status = {
        "ok": True,
        "chatgpt_status_exact": True,
        "status_kind": "chatgpt",
        "auth_env_vars_blank": {name: True for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS},
    }
    receipt: dict[str, object] = {
        "schema_version": 2,
        "mode": "runner_controlled_project_execpolicy",
        "platform_os_name": os.name,
        "native_windows_sandbox_mode": ("unelevated" if os.name == "nt" else "not_applicable"),
        "controlled_rules_enforcement_mode": (
            "ignored_native_windows_sandbox" if os.name == "nt" else "project_execpolicy"
        ),
        "controlled_rules_ignored": os.name == "nt",
        "controlled_rules_written": os.name != "nt",
        "configuration_mode": "host_codex_home_with_isolated_config",
        "host_user_config_ignored": True,
        "target_project_config_isolated": True,
        "forced_login_method": "chatgpt",
        "model_provider": "openai",
        "chatgpt_base_url": CODEX_CHATGPT_SUBSCRIPTION_BASE_URL,
        "openai_base_url": CODEX_OPENAI_SUBSCRIPTION_BASE_URL,
        "auth_mode": "shared_host_chatgpt_subscription_cache",
        "api_fallback_allowed": False,
        "auth_cache_copied": False,
        "auth_cache_deleted": False,
        "auth_verification_status": "verified",
        "chatgpt_subscription_login_status_verified": True,
        "chatgpt_subscription_activation_probe_verified": True,
        "chatgpt_subscription_post_login_status_verified": True,
        "chatgpt_subscription_auth_verified": True,
        "api_key_auth_environment_disabled": True,
        "canonical_subscription_route_verified": True,
        "controlled_execution_mode_verified": True,
        "controlled_auth_env_vars": list(CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS),
        "login_status": status,
        "post_login_status": status,
        "activation_probe": {
            "ok": True,
            "marker_seen": True,
            "workspace_unchanged": True,
            "rules_ignored_observed": os.name == "nt",
            "sandbox_mode_observed": "workspace-write",
            "controlled_execution_mode_verified": True,
        },
        "controlled_config_overrides": controlled_overrides,
        "controlled_config_contract_path": str(config_contract_path),
        "controlled_config_contract_sha256": sha256(config_contract_path.read_bytes()).hexdigest(),
        "controlled_config_contract_status": "bound",
        "host_codex_home": str(host_home),
        "host_auth_path": str(host_home / "auth.json"),
        "host_auth_cache_preserved": True,
        "global_config_unchanged": True,
        "global_rules_loaded": os.name != "nt",
        "host_global_rules_unchanged": True,
        "restore_status": "restored",
        "restore_errors": [],
        "target_rules_manifest_before": [],
        "target_rules_manifest_after_restore": [],
        "target_config_manifest_before": [],
        "target_config_manifest_after_restore": [],
    }
    receipt["receipt_sha256"] = codex_execpolicy_receipt_sha256(receipt)
    path = run_dir / "codex_execpolicy_overlay.json"
    _write_json(path, receipt)
    return path


def test_runner_transport_artifacts_do_not_invent_live_verification(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "successful-static-research"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "report.json", {"status": "complete"})
    (run_dir / "report.md").write_text("Static parser research\n", encoding="utf-8")
    (run_dir / "normalized_events.jsonl").write_text('{"type":"run_completed"}\n', encoding="utf-8")
    (run_dir / "target_ref.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")

    empty_stderr_refs = mod._runner_artifact_refs(run_dir)
    assert all(ref["artifact_id"] != "runner:agent_stderr" for ref in empty_stderr_refs)
    required, reasons = infer_live_verification_requirement(
        {
            "title": "Static parser chooses the wrong default",
            "problem": "A local AST branch returns the wrong value.",
        },
        {
            "research_method": "static_trace",
            "artifact_refs": [
                *empty_stderr_refs,
                {"artifact_id": "origin:log", "kind": "log", "path": "parser.log"},
                {"artifact_id": "origin:trace", "kind": "trace", "path": "parser.trace"},
            ],
        },
    )
    assert required is False
    assert reasons == []

    (run_dir / "agent_stderr.txt").write_text("runner diagnostic retained\n", encoding="utf-8")
    nonempty_stderr_refs = mod._runner_artifact_refs(run_dir)
    assert any(ref["artifact_id"] == "runner:agent_stderr" for ref in nonempty_stderr_refs)
    required, reasons = infer_live_verification_requirement(
        {"title": "Static parser defect", "problem": "The parser branch is local."},
        {"research_method": "static_trace", "artifact_refs": nonempty_stderr_refs},
    )
    assert required is False
    assert reasons == []

    required, reasons = infer_live_verification_requirement(
        {"title": "Originating command failed", "problem": "Captured origin evidence."},
        {
            "research_method": "reproduction",
            "artifact_refs": [
                {
                    "artifact_id": "origin:agent-stderr",
                    "kind": "agent_stderr",
                    "path": "origin/agent_stderr.txt",
                }
            ],
        },
    )
    assert required is True
    assert reasons == ["runtime_artifact:agent_stderr"]

    verified_live_experiment = {
        "experiment_id": "experiment:receipt-live",
        "scenario_kind": "live_runtime",
        "platform_requirement": "windows",
    }
    required, reasons = infer_live_verification_requirement(
        {"title": "Live receipt boundary", "problem": "A neutral summary."},
        {
            "experiments": [verified_live_experiment],
            "evidence_verification": {
                "status": "verified",
                "experiments": [verified_live_experiment],
            },
        },
    )
    assert required is True
    assert "research_verified_live_runtime_boundary" in reasons
    assert "research_requires_platform:windows" in reasons

    mechanism_evidence: dict[str, Any] = {
        "evidence_type": "live_runtime",
        "experiment_ids": ["experiment:live"],
        "platform_requirement": "windows",
    }
    mechanism_projection = json.dumps(
        mechanism_evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    mechanism_evidence["mechanism_evidence_id"] = (
        "mechanism_evidence:" + sha256(mechanism_projection).hexdigest()
    )
    required, reasons = infer_live_verification_requirement(
        {"title": "Live boundary", "problem": "A neutral summary."},
        {
            "experiments": [
                {
                    "experiment_id": "experiment:live",
                    "scenario_kind": "live_runtime",
                    "platform_requirement": "windows",
                }
            ],
            "evidence_verification": {
                "status": "verified",
                "mechanism_evidence": [mechanism_evidence],
            },
        },
    )
    assert required is True
    assert "research_verified_live_runtime_boundary" in reasons
    assert "research_requires_platform:windows" in reasons
    assert "research_mechanism_evidence_live_runtime" in reasons
    assert "research_mechanism_evidence_requires_platform:windows" in reasons


def _init_workspace(path: Path) -> str:
    (path / "src").mkdir(parents=True)
    (path / "src" / "core.py").write_text(
        "def run(*, guarded=False, alternative=True):\n"
        "    if not guarded:\n"
        "        raise RuntimeError('reported failure')\n"
        "    return True\n",
        encoding="utf-8",
    )
    (path / "tests").mkdir()
    (path / "tests" / "test_core.py").write_text(
        "from src.core import run\n\n"
        "def test_reported_failure():\n    assert run() is True\n\n"
        "def test_guarded_control():\n"
        "    assert run(guarded=True) is True\n\n"
        "def test_alternative_removed():\n    run(alternative=False)\n",
        encoding="utf-8",
    )
    (path / "repro.txt").write_text("captured reproduction\n", encoding="utf-8")
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Tests"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "fixture"],
    ):
        subprocess.run(command, cwd=path, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_existing_workspace(path: Path, message: str) -> str:
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Tests"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", message],
    ):
        subprocess.run(command, cwd=path, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _research_extension(**overrides: object) -> dict[str, object]:
    extension: dict[str, object] = {
        "case_id": "case:test-1",
        "problem_id": "problem:test-1",
        "research_method": "reproduction",
        "reproduction_status": "reproduced",
        "research_status": "evidence_sufficient",
        "writes_used": False,
        "writes_purpose": ["none"],
        "implementation_performed": False,
        "artifact_refs": [
            {"artifact_id": "artifact:repro", "kind": "repro", "path": "repro.txt"},
            {"artifact_id": "artifact:source", "kind": "source", "path": "src/core.py"},
        ],
        "experiments": [
            {
                "experiment_id": "exp-1",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:origin"],
                "command": (
                    "python -m pytest -q --tb=native tests/test_core.py::test_reported_failure"
                ),
                "result": "Failed as reported",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "artifact_refs": ["artifact:repro", "artifact:source"],
            },
            {
                "experiment_id": "exp-control",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "exp-1",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "guard enabled",
                    "expected_difference": "The guarded control succeeds without the symptom.",
                },
                "addresses_atom_ids": ["atom:origin"],
                "command": ("python -m pytest -q tests/test_core.py::test_guarded_control"),
                "result": "The guarded control succeeds",
                "outcome": "refutes",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 0,
                },
                "artifact_refs": ["artifact:repro", "artifact:source"],
            },
            {
                "experiment_id": "exp-challenge",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "exp-1",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "alternative input disabled",
                    "expected_difference": (
                        "The failure disappears only if the alternative is causal."
                    ),
                },
                "addresses_atom_ids": ["atom:origin"],
                "command": ("python -m pytest -q tests/test_core.py::test_alternative_removed"),
                "result": "The original failure remains",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "artifact_refs": ["artifact:repro", "artifact:source"],
            },
        ],
        "inspected_files": ["src/core.py"],
        "inspected_symbols": ["core.run"],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "Validation is missing",
                "supporting_evidence": ["exp-1", "exp-challenge"],
                "counterevidence": ["exp-control"],
                "mechanism_symbols": ["core.run"],
                "disposition": "primary",
                "disposition_evidence": ["exp-1", "exp-control"],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:guarded-control",
                        "hypothesis_id": "h1",
                        "claim": "Validation is missing",
                        "baseline_experiment_id": "exp-1",
                        "challenge_experiment_id": "exp-challenge",
                        "disproof_condition": {
                            "source": "exit_code",
                            "operator": "equals",
                            "expected": 0,
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
        "root_cause_confidence": 0.9,
        "broader_class_assessment": "unknown",
        "material_unknowns": [],
        "blocking_reasons": [],
        "evidence_boundaries": [],
    }
    extension.update(overrides)
    return extension


def test_diff_classification_only_allows_dedicated_research_overlay() -> None:
    assert mod._classify_diff([".usertest_research/repro.txt"])[0] == ("allowed_research_edits")
    for path in (
        "tests/test_repro.py",
        "scripts/probe.py",
        "tools/inspect.py",
        "configs/probe.yaml",
    ):
        classification, reasons = mod._classify_diff([path])
        assert classification == "suspicious_implementation"
        assert reasons == [f"suspicious_path: {path}"]


def test_partial_wrapper_preserves_honest_insufficient_research_status() -> None:
    assert mod._report_status_blocking_reason("partial", "insufficient_evidence") is None
    assert mod._report_status_blocking_reason("partial", "blocked") is None
    assert mod._report_status_blocking_reason("partial", "evidence_sufficient") == (
        "runner_report_status:partial"
    )
    assert mod._report_status_blocking_reason("failure", "insufficient_evidence") is None
    assert mod._report_status_blocking_reason("failure", "blocked") is None
    assert mod._report_status_blocking_reason("failure", "evidence_sufficient") == (
        "runner_report_status:failure"
    )


def test_codex_research_execpolicy_covers_bounded_inspection_and_replay_tools() -> None:
    prefixes = set(mod._CODEX_RESEARCH_EXEC_ALLOW_PREFIXES)
    assert {
        ("git", "rev-parse"),
        ("git", "diff"),
        ("pytest",),
        ("python", "-m", "pytest"),
        ("python",),
        ("pdm", "run", "pytest"),
        ("npm", "test"),
        ("pnpm", "test"),
        ("yarn", "test"),
        ("cargo", "test"),
        ("go", "test"),
        ("dotnet", "test"),
        ("docker", "version"),
        ("docker", "info"),
        ("Get-FileHash",),
        ("Get-Command",),
        ("Test-Path",),
    }.issubset(prefixes)


def _problem_payload(tmp_path: Path) -> dict[str, object]:
    origin_path = tmp_path / "origin.json"
    origin_path.write_text('{"failure": true}\n', encoding="utf-8")
    digest = sha256(origin_path.read_bytes()).hexdigest()
    assignment: dict[str, object] = {
        "status": "complete",
        "errors": [],
        "case_id": "case:test-1",
        "problem_id": "problem:test-1",
        "expected_atom_ids": ["atom:origin"],
        "atom_receipts": [
            {
                "atom_id": "atom:origin",
                "atom_sha256": sha256(
                    json.dumps(
                        {
                            "atom_id": "atom:origin",
                            "text": "failure",
                            "command": (
                                "python -m pytest -q --tb=native "
                                "tests/test_core.py::test_reported_failure"
                            ),
                            "exit_code": 1,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "atom_snapshot": {
                    "atom_id": "atom:origin",
                    "text": "failure",
                    "command": (
                        "python -m pytest -q --tb=native tests/test_core.py::test_reported_failure"
                    ),
                    "exit_code": 1,
                },
                "artifact_receipts": [
                    {
                        "path": str(origin_path),
                        "sha256": digest,
                        "size_bytes": origin_path.stat().st_size,
                    }
                ],
            }
        ],
    }
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    return {
        "case_id": "case:test-1",
        "problem_id": "problem:test-1",
        "evidence_atoms": [{"atom_id": "atom:origin"}],
        "evidence_assignment": assignment,
    }


def _insufficient_extension() -> dict[str, object]:
    return _research_extension(
        reproduction_status="partial",
        research_status="insufficient_evidence",
        artifact_refs=[],
        experiments=[],
        inspected_files=[],
        inspected_symbols=[],
        root_cause_hypotheses=[],
        root_cause_confidence=0.25,
        material_unknowns=[
            {
                "unknown": "The exact mechanism is not established",
                "affects": ["root_cause", "change_surface"],
                "evidence_needed": "Capture a faithful runtime reproduction",
            }
        ],
        evidence_boundaries=["The retained evidence verifies the investigation, not a cause"],
    )


def _write_run_provenance(
    *,
    run_dir: Path,
    workspace: Path,
    revision: str,
    ref: str | None,
    events: list[dict[str, object]] | None = None,
) -> None:
    _write_json(run_dir / "diff_numstat.json", [])
    _write_json(
        run_dir / "target_ref.json",
        {"commit_sha": revision, "ref": ref, "agent": "codex"},
    )
    _write_valid_codex_subscription_receipt(run_dir)
    _write_json(run_dir / "workspace_ref.json", {"workspace_dir": str(workspace)})
    (run_dir / "normalized_events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in (events or [])),
        encoding="utf-8",
    )


@pytest.mark.parametrize("outer_status", ["partial", "failure"])
def test_full_runner_preserves_verified_insufficient_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outer_status: str,
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")
    workspace = tmp_path / "insufficient_workspace"
    revision = _init_workspace(workspace)
    extension = _insufficient_extension()
    calls = 0

    def fake_run_once(*, config: RunnerConfig, request: RunRequest) -> RunResult:
        nonlocal calls
        calls += 1
        run_dir = tmp_path / "run_insufficient"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": outer_status,
                "goal": "Bound the assigned failure",
                "failure_point": "The exact mechanism remains unknown",
                "evidence": {"what_happened": "The investigation was inconclusive"},
                "attempted_fixes": [],
                "recommended_fix_path": ["Gather the named missing evidence"],
                "extensions": {"backlog_repro_research": extension},
            },
        )
        _write_run_provenance(
            run_dir=run_dir,
            workspace=workspace,
            revision=revision,
            ref=request.ref,
        )
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)
    document = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=str(workspace),
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[_problem_payload(tmp_path)],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
        replay_executor=TrustedHostReplayExecutor(
            approved_source_roots=[workspace],
            source_identity=workspace,
        ),
        replay_executor_metadata={"executor": "trusted_host"},
    )

    dossier = document["items"][0]
    assert calls == 1
    assert dossier["research_status"] == "insufficient_evidence", (
        dossier["blocking_reasons"],
        dossier["evidence_verification"]["errors"],
        [attempt["validation_errors"] for attempt in dossier.get("research_attempts", [])],
    )
    assert dossier["reproduction_status"] == "partial"
    assert dossier["evidence_verification"]["status"] == "verified", dossier[
        "evidence_verification"
    ]["errors"]
    assert dossier["evidence_verification"]["atom_bindings"] == []
    ready, reasons = assess_research_readiness(dossier)
    assert ready is False
    assert "research_status_insufficient_evidence" in reasons


def test_output_retry_is_fresh_full_case_and_invalid_then_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")
    first_workspace = tmp_path / "retry_workspace_1"
    revision = _init_workspace(first_workspace)
    second_workspace = tmp_path / "retry_workspace_2"
    subprocess.run(
        ["git", "clone", "--quiet", str(first_workspace), str(second_workspace)],
        check=True,
        capture_output=True,
    )
    prompts: list[str] = []
    calls = 0

    def fake_run_once(*, config: RunnerConfig, request: RunRequest) -> RunResult:
        nonlocal calls
        calls += 1
        prompts.append(request.agent_append_system_prompt or "")
        workspace = first_workspace if calls == 1 else second_workspace
        run_dir = tmp_path / f"retry_run_{calls}"
        run_dir.mkdir(parents=True, exist_ok=True)
        extension = None if calls == 1 else _insufficient_extension()
        report: dict[str, object] = {
            "schema_version": 1,
            "kind": "troubleshoot_v1",
            "status": "failure" if calls == 1 else "partial",
            "goal": "Research the assigned case",
            "failure_point": "The exact mechanism remains unknown",
            "evidence": {"what_happened": "The first report omitted its proof"},
            "attempted_fixes": [],
            "recommended_fix_path": ["Retain the honest unknown"],
        }
        if extension is not None:
            report["extensions"] = {"backlog_repro_research": extension}
        _write_json(run_dir / "report.json", report)
        _write_run_provenance(
            run_dir=run_dir,
            workspace=workspace,
            revision=revision,
            ref=request.ref,
        )
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)
    document = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=str(first_workspace),
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[_problem_payload(tmp_path)],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
        replay_executor=TrustedHostReplayExecutor(
            approved_source_roots=[first_workspace],
            source_identity=first_workspace,
        ),
        replay_executor_metadata={"executor": "trusted_host"},
    )

    dossier = document["items"][0]
    assert calls == 2
    assert dossier["research_status"] == "insufficient_evidence", (
        dossier.get("blocking_reasons"),
        dossier["evidence_verification"]["errors"],
    )
    assert dossier["evidence_verification"]["status"] == "verified"
    attempts = dossier["research_attempts"]
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]
    assert [attempt["outcome"] for attempt in attempts] == [
        "output_contract_invalid",
        "output_contract_valid",
    ]
    assert attempts[0]["validation_errors"] == ["research_extension_missing:backlog_repro_research"]
    assert attempts[1]["validation_errors"] == []
    assert attempts[0]["run_dir"] != attempts[1]["run_dir"]
    assert mod._research_attempt_workspace(attempts[0]) != mod._research_attempt_workspace(
        attempts[1]
    )
    assert mod._research_attempt_revision(attempts[0]) == revision.casefold()
    assert mod._research_attempt_revision(attempts[1]) == revision.casefold()
    assert all(
        artifact["exists"] is True
        for attempt in attempts
        for artifact in attempt["attempt_artifacts"]
    )
    assert all('"atom_id": "atom:origin"' in prompt for prompt in prompts)
    assert "Rerun the complete research assignment" in prompts[1]
    persisted, persisted_errors = verify_persisted_research_evidence(dossier)
    assert persisted is True, persisted_errors
    Path(attempts[0]["report_path"]).write_text("{}\n", encoding="utf-8")
    persisted, persisted_errors = verify_persisted_research_evidence(dossier)
    assert persisted is False
    assert "research_attempt_artifact_changed:0:report" in persisted_errors


def test_successful_output_retry_rejects_reused_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")
    workspace = tmp_path / "reused_workspace"
    revision = _init_workspace(workspace)
    calls = 0

    def fake_run_once(*, config: RunnerConfig, request: RunRequest) -> RunResult:
        nonlocal calls
        calls += 1
        run_dir = tmp_path / f"reused_workspace_run_{calls}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report: dict[str, object] = {
            "schema_version": 1,
            "kind": "troubleshoot_v1",
            "status": "failure" if calls == 1 else "partial",
            "goal": "Research the assigned case",
            "failure_point": "The exact mechanism remains unknown",
            "evidence": {"what_happened": "The first proof was omitted"},
            "attempted_fixes": [],
            "recommended_fix_path": ["retain the unknown"],
        }
        if calls == 2:
            report["extensions"] = {"backlog_repro_research": _insufficient_extension()}
        _write_json(run_dir / "report.json", report)
        _write_run_provenance(
            run_dir=run_dir,
            workspace=workspace,
            revision=revision,
            ref=request.ref,
        )
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)
    document = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=str(workspace),
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[_problem_payload(tmp_path)],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
        replay_executor=TrustedHostReplayExecutor(
            approved_source_roots=[workspace],
            source_identity=workspace,
        ),
        replay_executor_metadata={"executor": "trusted_host"},
    )

    assert calls == 2
    dossier = document["items"][0]
    assert dossier["research_status"] == "blocked"
    assert dossier["blocking_reasons"] == ["research_output_contract_retry_not_fresh"]
    assert len(dossier["research_attempts"]) == 2


def test_research_workspace_materializes_and_attests_large_origin_attachment(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    revision = _init_workspace(target)
    origin_run = tmp_path / "runs" / "origin"
    origin_run.mkdir(parents=True)
    signature = "ROOT_CAUSE_SIGNATURE_ONLY_IN_RETAINED_ARTIFACT_MIDDLE"
    artifact = origin_run / "agent_stderr.txt"
    artifact.write_text(("prefix-context\n" * 1_500) + signature + ("\nsuffix-context" * 1_500))
    assert artifact.stat().st_size > 24 * 1024
    atom = {
        "atom_id": "atom:origin",
        "run_dir": str(origin_run),
        "text": "The atom excerpt does not contain the retained diagnostic signature.",
        "attachments": [
            {
                "kind": "agent_stderr",
                "artifact_ref": {
                    "path": artifact.name,
                    "sha256": sha256(artifact.read_bytes()).hexdigest(),
                    "size_bytes": artifact.stat().st_size,
                },
            }
        ],
    }

    workspace, manifest = mod._prepare_origin_evidence_workspace(
        repo_input=str(target),
        repo_ref=revision,
        preferred_workspace_dir=tmp_path / "prepared-research-workspace",
        evidence_atoms=[atom],
        source_root=tmp_path,
    )

    assert (workspace / "src" / "core.py").is_file()
    assert manifest["errors"] == []
    requirements = mod.origin_attachment_requirements(manifest)
    assert len(requirements) >= 2
    assert all(int(item["size_bytes"]) < 24 * 1024 for item in requirements)
    assert any(
        signature in (workspace / str(item["file"])).read_text(encoding="utf-8")
        for item in requirements
    )
    prompt = mod._append_prompt_for_problem(
        repo_root=Path(__file__).resolve().parents[3],
        problem_payload={
            "problem_id": "problem:test-1",
            "evidence_assignment": {"origin_attachment_evidence": manifest},
        },
    )
    assert "Required origin-attachment reads" in prompt
    assert str(artifact) not in prompt
    assert '"chunks":' not in prompt
    assert str(manifest["artifacts"][0]["manifest_file"]) in prompt

    research_run = tmp_path / "research-run"
    research_run.mkdir()
    events: list[dict[str, object]] = []
    for requirement in requirements:
        chunk_path = workspace / str(requirement["file"])
        content = chunk_path.read_text(encoding="utf-8")
        events.append(
            {
                "ts": "2026-07-10T00:00:00Z",
                "type": "read_file",
                "data": {
                    "path": str(requirement["file"]),
                    "read_source": "tool",
                    "source_exit_code": 0,
                    **observed_read_attestation(
                        path=chunk_path,
                        observed_text=content,
                        source_exit_code=0,
                        allow_partial=False,
                    ),
                },
            }
        )
    (research_run / "normalized_events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    receipts, errors = mod._origin_attachment_read_receipts(
        run_dir=research_run,
        workspace_dir=workspace,
        manifest=manifest,
    )

    assert errors == []
    assert len(receipts) == len(requirements)
    assert any(receipt["file"] == requirement["file"] for receipt in receipts)


def test_run_repro_research_stage_dry_run_writes_requests_and_placeholders(tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "compiled" / "target_a.backlog_artifacts"
    doc = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=None,
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[{"problem_id": "problem:test-1", "title": "Test"}],
        artifacts_dir=artifacts_dir,
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=True,
        replay_executor_metadata={
            "executor": "docker",
            "docker_image": "example.invalid/replay@sha256:" + "a" * 64,
            "network": "none",
        },
    )

    assert doc.get("stage") == "repro_research"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert doc.get("input_meta", {}).get("replay_executor", {}).get("network") == "none"
    assert isinstance(doc.get("items"), list)
    assert len(doc["items"]) == 1
    dossier = doc["items"][0]
    assert dossier.get("implementation_performed") is False
    assert dossier.get("diff_classification") == "no_changes"
    assert dossier.get("writes_used") is False
    assert dossier.get("research_status") == "blocked"
    assert dossier.get("reproduction_status") == "blocked"

    requests_path = Path(doc["artifacts"]["requests_json"])
    assert requests_path.exists()
    requests = json.loads(requests_path.read_text(encoding="utf-8"))
    assert requests["replay_executor"]["executor"] == "docker"


def test_run_repro_research_stage_retries_missing_extension_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")

    calls = 0

    def fake_run_once(*, config: RunnerConfig, request: object) -> RunResult:
        nonlocal calls
        calls += 1
        run_dir = tmp_path / f"run_missing_ext_{calls}"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": "failure",
                "goal": "x",
                "failure_point": "y",
                "evidence": {"what_happened": "z"},
                "attempted_fixes": [],
                "recommended_fix_path": ["investigate"],
            },
        )
        _write_json(run_dir / "diff_numstat.json", [])
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)

    document = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:agent-adapters",
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[_problem_payload(tmp_path)],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
    )

    assert document["items"][0]["research_status"] == "blocked"
    assert document["items"][0]["blocking_reasons"] == ["research_dossier_output_contract_invalid"]
    assert calls == 2
    attempts = document["items"][0]["research_attempts"]
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]
    assert all(
        attempt["validation_errors"] == ["research_extension_missing:backlog_repro_research"]
        for attempt in attempts
    )


@pytest.mark.parametrize(
    ("failure_kind", "expected_error"),
    [
        ("missing_report", "research_report_missing"),
        ("malformed_json", "research_report_malformed:JSONDecodeError:"),
        ("top_level_list", "research_report_malformed:ValueError:"),
        ("empty_object", "research_extension_missing:backlog_repro_research"),
        ("schema_invalid", "research_report_schema_invalid:"),
    ],
)
def test_recoverable_report_contract_failures_retry_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected_error: str,
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")
    calls = 0
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "configs"
            / "report_schemas"
            / "troubleshoot_v1.schema.json"
        ).read_text(encoding="utf-8")
    )

    def fake_run_once(*, config: RunnerConfig, request: RunRequest) -> RunResult:
        nonlocal calls
        calls += 1
        run_dir = tmp_path / f"report_failure_{failure_kind}_{calls}"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(run_dir / "diff_numstat.json", [])
        if failure_kind == "malformed_json":
            (run_dir / "report.json").write_text("{", encoding="utf-8")
        elif failure_kind == "top_level_list":
            _write_json(run_dir / "report.json", [])
        elif failure_kind == "empty_object":
            _write_json(run_dir / "report.json", {})
        elif failure_kind == "schema_invalid":
            _write_json(run_dir / "report.schema.json", schema)
            _write_json(
                run_dir / "report.json",
                {
                    "schema_version": 1,
                    "kind": "troubleshoot_v1",
                    "status": "partial",
                    "failure_point": "missing required goal",
                    "evidence": {"what_happened": "schema-invalid report"},
                    "attempted_fixes": [],
                    "recommended_fix_path": ["emit the required report fields"],
                    "extensions": {"backlog_repro_research": _insufficient_extension()},
                },
            )
        elif failure_kind != "missing_report":
            raise AssertionError(f"unexpected failure kind: {failure_kind}")
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)
    document = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:agent-adapters",
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[_problem_payload(tmp_path)],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
    )

    assert calls == 2
    attempts = document["items"][0]["research_attempts"]
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]
    assert all(attempt["outcome"] == "output_contract_invalid" for attempt in attempts)
    assert all(
        any(error.startswith(expected_error) for error in attempt["validation_errors"])
        for attempt in attempts
    )


@pytest.mark.parametrize(
    "failure_kind",
    [
        "evidence_verification",
        "runner_report_verification",
        "suspicious_diff_with_missing_report",
        "implementation_with_schema_error",
        "nonzero_exit_with_missing_report",
    ],
)
def test_substantive_failures_never_consume_output_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "configs"
            / "report_schemas"
            / "troubleshoot_v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    calls = 0

    def fake_run_once(*, config: RunnerConfig, request: RunRequest) -> RunResult:
        nonlocal calls
        calls += 1
        run_dir = tmp_path / f"nonretry_{failure_kind}"
        run_dir.mkdir(parents=True, exist_ok=True)
        suspicious = failure_kind == "suspicious_diff_with_missing_report"
        _write_json(
            run_dir / "diff_numstat.json",
            (
                [{"path": "src/production.py", "lines_added": 1, "lines_removed": 0}]
                if suspicious
                else []
            ),
        )
        if failure_kind not in {
            "suspicious_diff_with_missing_report",
            "nonzero_exit_with_missing_report",
        }:
            extension = _insufficient_extension()
            if failure_kind == "implementation_with_schema_error":
                extension["implementation_performed"] = True
            report: dict[str, object] = {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": "partial",
                "failure_point": "bounded failure",
                "evidence": {"what_happened": "retained evidence"},
                "attempted_fixes": [],
                "recommended_fix_path": ["continue research"],
                "extensions": {"backlog_repro_research": extension},
            }
            if failure_kind != "implementation_with_schema_error":
                report["goal"] = "Research the assigned case"
            _write_json(run_dir / "report.json", report)
            _write_json(run_dir / "report.schema.json", schema)
        report_errors = (
            ["codex_execpolicy_overlay_restore_failed"]
            if failure_kind == "runner_report_verification"
            else []
        )
        exit_code = 1 if failure_kind == "nonzero_exit_with_missing_report" else 0
        return RunResult(
            run_dir=run_dir,
            exit_code=exit_code,
            report_validation_errors=report_errors,
        )

    monkeypatch.setattr(mod, "run_once", fake_run_once)
    document = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:agent-adapters",
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[_problem_payload(tmp_path)],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
    )

    dossier = document["items"][0]
    assert calls == 1
    assert dossier["research_status"] == "blocked"
    assert len(dossier["research_attempts"]) == 1
    if failure_kind == "evidence_verification":
        assert dossier["research_attempts"][0]["outcome"] == "output_contract_valid"
        assert "research_evidence_verification_failed" in dossier["blocking_reasons"]
    elif failure_kind == "runner_report_verification":
        assert dossier["research_attempts"][0]["outcome"] == "output_contract_valid"
        assert "runner_report_validation_errors" in dossier["blocking_reasons"]
    elif failure_kind == "suspicious_diff_with_missing_report":
        assert dossier["diff_classification"] == "suspicious_implementation"
        assert dossier["blocking_reasons"] == ["suspicious_implementation_diff"]
    elif failure_kind == "implementation_with_schema_error":
        assert dossier["blocking_reasons"] == ["research_implementation_performed_forbidden"]
    else:
        assert dossier["blocking_reasons"] == ["runner_exit_code:1"]


def test_run_repro_research_stage_classifies_suspicious_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")

    ext = _research_extension(writes_purpose=["temporary_instrumentation"])
    calls = 0

    def fake_run_once(*, config: RunnerConfig, request: object) -> RunResult:
        nonlocal calls
        calls += 1
        run_dir = tmp_path / "run_suspicious"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": "partial",
                "goal": "x",
                "failure_point": "y",
                "evidence": {"what_happened": "z"},
                "attempted_fixes": [],
                "recommended_fix_path": ["investigate"],
                "extensions": {"backlog_repro_research": ext},
            },
        )
        _write_json(
            run_dir / "diff_numstat.json",
            [
                {
                    "path": "packages/example/src/example/core.py",
                    "lines_added": 1,
                    "lines_removed": 0,
                }
            ],
        )
        _write_json(run_dir / "target_ref.json", {"commit_sha": "canonical-sha"})
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)

    doc = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:agent-adapters",
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[_problem_payload(tmp_path)],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
    )

    items = doc.get("items") or []
    assert isinstance(items, list)
    assert items
    assert items[0]["diff_classification"] == "suspicious_implementation"
    assert items[0]["research_status"] == "blocked"
    assert "suspicious_implementation_diff" in items[0]["blocking_reasons"]
    assert items[0]["repo_revision"] == "canonical-sha"
    assert calls == 1
    assert [attempt["outcome"] for attempt in items[0]["research_attempts"]] == [
        "runner_contract_invalid"
    ]


def test_run_repro_research_stage_retries_and_retains_incomplete_output_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")
    incomplete = _research_extension()
    del incomplete["experiments"]
    calls = 0

    def fake_run_once(*, config: RunnerConfig, request: RunRequest) -> RunResult:
        nonlocal calls
        calls += 1
        assert ("git", "rev-parse") in request.codex_execpolicy_allow_prefixes
        assert ("python",) in request.codex_execpolicy_allow_prefixes
        if calls == 2:
            retry_prompt = request.agent_append_system_prompt or ""
            assert '"research_output_contract_retry"' in retry_prompt
            assert "research_dossier_missing_required_field" in retry_prompt
            assert "Rerun the complete research assignment" in retry_prompt
        run_dir = tmp_path / f"run_incomplete_{calls}"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": "success",
                "goal": "x",
                "failure_point": "y",
                "evidence": {"what_happened": "z"},
                "attempted_fixes": [],
                "recommended_fix_path": ["investigate"],
                "extensions": {"backlog_repro_research": incomplete},
            },
        )
        _write_json(run_dir / "diff_numstat.json", [])
        _write_json(run_dir / "target_ref.json", {"commit_sha": "canonical-sha"})
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)

    document = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:agent-adapters",
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[_problem_payload(tmp_path)],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
    )

    assert document["items"][0]["research_status"] == "blocked"
    assert document["items"][0]["blocking_reasons"] == ["research_dossier_output_contract_invalid"]
    assert calls == 2
    attempts = document["items"][0]["research_attempts"]
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]
    assert [attempt["outcome"] for attempt in attempts] == [
        "output_contract_invalid",
        "output_contract_invalid",
    ]
    assert all(
        any("missing_required_field" in error for error in attempt["validation_errors"])
        for attempt in attempts
    )
    assert all("experiments" not in attempt["attempted_dossier"] for attempt in attempts)
    requests = json.loads(Path(document["artifacts"]["requests_json"]).read_text(encoding="utf-8"))
    assert [attempt["attempt_number"] for attempt in requests["requests"][0]["attempts"]] == [
        1,
        2,
    ]


def test_model_owned_identity_mismatch_retries_without_silent_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")
    calls = 0
    wrong = _insufficient_extension()
    wrong["problem_id"] = "problem:wrong"

    def fake_run_once(*, config: RunnerConfig, request: RunRequest) -> RunResult:
        nonlocal calls
        calls += 1
        run_dir = tmp_path / f"wrong_identity_{calls}"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": "partial",
                "goal": "research",
                "failure_point": "identity mismatch",
                "evidence": {"what_happened": "wrong identity emitted"},
                "attempted_fixes": [],
                "recommended_fix_path": ["copy assigned identity"],
                "extensions": {"backlog_repro_research": wrong},
            },
        )
        _write_json(run_dir / "diff_numstat.json", [])
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)
    document = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:agent-adapters",
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[_problem_payload(tmp_path)],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
    )

    assert calls == 2
    dossier = document["items"][0]
    assert dossier["problem_id"] == "problem:test-1"
    attempts = dossier["research_attempts"]
    assert all(
        attempt["attempted_dossier"]["problem_id"] == "problem:wrong" for attempt in attempts
    )
    assert all(
        any("problem_id_mismatch" in error for error in attempt["validation_errors"])
        for attempt in attempts
    )


def test_one_malformed_case_does_not_abort_later_research_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")
    calls = 0

    def fake_run_once(*, config: RunnerConfig, request: object) -> RunResult:
        nonlocal calls
        calls += 1
        run_dir = tmp_path / f"run_missing_ext_{calls}"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(run_dir / "report.json", {"schema_version": 1, "status": "failure"})
        _write_json(run_dir / "diff_numstat.json", [])
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)
    first = _problem_payload(tmp_path)
    second = json.loads(json.dumps(first))
    second["case_id"] = "case:test-2"
    second["problem_id"] = "problem:test-2"
    second["evidence_assignment"]["case_id"] = second["case_id"]
    second["evidence_assignment"]["problem_id"] = second["problem_id"]
    second["evidence_assignment"]["assignment_sha256"] = evidence_assignment_sha256(
        second["evidence_assignment"]
    )

    document = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:agent-adapters",
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[first, second],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
    )

    assert calls == 4
    assert [item["problem_id"] for item in document["items"]] == [
        "problem:test-1",
        "problem:test-2",
    ]
    assert all(item["research_status"] == "blocked" for item in document["items"])


def _run_verified_research_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, dict[str, object]]:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")
    workspace = tmp_path / "research_workspace"
    revision = _init_workspace(workspace)
    extension = _research_extension()

    def fake_run_once(*, config: RunnerConfig, request: RunRequest) -> RunResult:
        assert request.keep_workspace is True
        run_dir = tmp_path / "run_verified"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": "success",
                "goal": "x",
                "failure_point": "y",
                "evidence": {"what_happened": "z"},
                "attempted_fixes": [],
                "recommended_fix_path": ["investigate"],
                "extensions": {"backlog_repro_research": extension},
            },
        )
        _write_json(run_dir / "diff_numstat.json", [])
        _write_json(
            run_dir / "target_ref.json",
            {"commit_sha": revision, "ref": request.ref, "agent": "codex"},
        )
        _write_valid_codex_subscription_receipt(run_dir)
        _write_json(run_dir / "workspace_ref.json", {"workspace_dir": str(workspace)})
        events = [
            {
                "type": "run_command",
                "data": {
                    "command": (
                        "python -m pytest -q --tb=native tests/test_core.py::test_reported_failure"
                    ),
                    "exit_code": 1,
                    "output_excerpt": "failure reproduced",
                },
            },
            {
                "type": "run_command",
                "data": {
                    "command": ("python -m pytest -q tests/test_core.py::test_alternative_removed"),
                    "exit_code": 1,
                    "output_excerpt": "failure remains after alternative removal",
                },
            },
            {
                "type": "run_command",
                "data": {
                    "command": ("python -m pytest -q tests/test_core.py::test_guarded_control"),
                    "exit_code": 0,
                },
            },
            {
                "type": "read_file",
                "data": {
                    "path": "src/core.py",
                    "bytes": (workspace / "src" / "core.py").stat().st_size,
                    "read_source": "tool",
                    "source_exit_code": 0,
                    **observed_read_attestation(
                        path=workspace / "src" / "core.py",
                        observed_text=(workspace / "src" / "core.py").read_text(encoding="utf-8"),
                        source_exit_code=0,
                        allow_partial=True,
                    ),
                },
            },
        ]
        (run_dir / "normalized_events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)

    doc = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=str(workspace),
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[_problem_payload(tmp_path)],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
        replay_executor=TrustedHostReplayExecutor(
            approved_source_roots=[workspace],
            source_identity=workspace,
        ),
        replay_executor_metadata={
            "executor": "trusted_host",
            "approved_source_roots": [str(workspace.resolve())],
            "source_identity": str(workspace.resolve()),
        },
    )

    dossier = doc["items"][0]
    assert isinstance(dossier, dict)
    return workspace, revision, dossier


def test_run_repro_research_stage_binds_claims_to_runner_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _, dossier = _run_verified_research_stage(tmp_path, monkeypatch)
    assert dossier["research_status"] == "evidence_sufficient", dossier["evidence_verification"][
        "errors"
    ]
    assert dossier["evidence_verification"]["status"] == "verified"
    assert dossier["evidence_verification"]["origin_atom_ids"] == ["atom:origin"]
    attempt_receipt = dossier["evidence_verification"]["hypothesis_refs"][0][
        "falsification_attempts"
    ][0]
    assert attempt_receipt["outcome"] == "survived"
    assert attempt_receipt["challenge_experiment_id"] == "exp-challenge"
    assert attempt_receipt["command"].endswith("::test_alternative_removed")
    assert len(attempt_receipt["stdout_sha256"]) == 64
    evidenced_experiments = {
        experiment_id
        for evidence in dossier["evidence_verification"]["mechanism_evidence"]
        for experiment_id in evidence["experiment_ids"]
    }
    assert {"exp-1", "exp-challenge"}.issubset(evidenced_experiments)
    sidecar = json.loads(
        (Path(dossier["run_dir"]) / "evidence_assignment.json").read_text(encoding="utf-8")
    )
    assert sidecar["producer"] == "backlog_miner.research_runner"
    assert sidecar["evidence_assignment"]["case_id"] == "case:test-1"
    planning_workspace = Path(dossier["repo_workspace"])
    assert planning_workspace.is_dir()
    assert planning_workspace != workspace
    assert dossier["evidence_verification"]["planning_workspace_clean"] is True
    replay_workspaces = {
        receipt["workspace_dir"] for receipt in dossier["evidence_verification"]["experiments"]
    }
    assert len(replay_workspaces) == 3
    assert len(dossier["evidence_verification"]["test_selections"]) == 2
    assert dossier["evidence_verification"]["control_verifications"][0][
        "shared_verified_mechanism_symbols"
    ] == ["core.run"]
    persisted_ready, persisted_reasons = verify_persisted_research_evidence(dossier)
    assert persisted_ready is True
    assert persisted_reasons == []

    auth_ref = next(
        ref
        for ref in dossier["artifact_refs"]
        if ref.get("artifact_id") == "runner:codex_subscription_auth"
    )
    auth_path = Path(auth_ref["path"])
    auth_bytes = auth_path.read_bytes()
    auth_path.unlink()
    persisted_ready, persisted_reasons = verify_persisted_research_evidence(dossier)
    assert persisted_ready is False
    assert "research_codex_subscription_receipt_missing" in persisted_reasons
    auth_path.write_bytes(auth_bytes)

    replay_workspace = Path(next(iter(replay_workspaces)))
    replay_source = replay_workspace / "src" / "core.py"
    original_replay_source = replay_source.read_text(encoding="utf-8")
    replay_source.write_text("post-receipt mutation\n", encoding="utf-8")
    persisted_ready, persisted_reasons = verify_persisted_research_evidence(dossier)
    assert persisted_ready is False
    assert any(
        reason.startswith("research_replay_workspace_state_changed:")
        for reason in persisted_reasons
    )
    replay_source.write_text(original_replay_source, encoding="utf-8")

    control_receipt = dossier["evidence_verification"]["control_verifications"][0]
    control_receipt["same_test_file"] = False
    dossier["evidence_verification"]["receipt_sha256"] = evidence_verification_sha256(
        dossier["evidence_verification"]
    )
    persisted_ready, persisted_reasons = verify_persisted_research_evidence(dossier)
    assert persisted_ready is False
    assert "research_control_verifications_changed" in persisted_reasons
    control_receipt["same_test_file"] = True
    dossier["evidence_verification"]["receipt_sha256"] = evidence_verification_sha256(
        dossier["evidence_verification"]
    )

    dossier["root_cause_hypotheses"][0]["statement"] = "A different, unverified cause"
    persisted_ready, persisted_reasons = verify_persisted_research_evidence(dossier)
    assert persisted_ready is False
    assert "research_receipt_claims_changed" in persisted_reasons


def test_practical_cli_authorization_survives_persistence_and_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")

    workspace = tmp_path / "practical_workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "mode.py").write_text(
        "import json\n"
        "from pathlib import Path\n\n"
        "def read_mode(path):\n"
        "    return json.loads(Path(path).read_text(encoding='utf-8'))['mode']\n",
        encoding="utf-8",
    )
    (workspace / "tools").mkdir(parents=True)
    (workspace / "tools" / "show_mode.py").write_text(
        "import sys\n"
        "from src.mode import read_mode\n\n"
        "def main():\n"
        "    print(read_mode(sys.argv[1]))\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    (workspace / "bad.json").write_text('{"mode":"bad"}\n', encoding="utf-8")
    (workspace / "bad_again.json").write_text('{"mode":"bad"}\n', encoding="utf-8")
    (workspace / "correct.json").write_text('{"mode":"correct"}\n', encoding="utf-8")
    (workspace / "correct_again.json").write_text('{"mode":"correct"}\n', encoding="utf-8")
    (workspace / "repro.txt").write_text("bad\n", encoding="utf-8")
    revision = _commit_existing_workspace(workspace, "practical CLI fixture")

    support_command = "python -m tools.show_mode bad.json"
    challenge_command = "python -m tools.show_mode bad_again.json"
    control_command = "python -m tools.show_mode correct.json"
    challenge_control_command = "python -m tools.show_mode correct_again.json"
    mechanism_link = {
        "kind": "entrypoint_dataflow",
        "entrypoint": "show_mode.main",
        "code_path": [
            {
                "path": "tools/show_mode.py",
                "symbol": "show_mode.main",
                "observation": "The repository CLI entrypoint calls the mode reader.",
            },
            {
                "path": "src/mode.py",
                "symbol": "mode.read_mode",
                "observation": "The mode reader returns the wrong retained value.",
            },
        ],
    }
    extension = _research_extension(
        artifact_refs=[
            {"artifact_id": "artifact:repro", "kind": "repro", "path": "repro.txt"},
            {
                "artifact_id": "artifact:entrypoint",
                "kind": "source",
                "path": "tools/show_mode.py",
            },
            {
                "artifact_id": "artifact:mechanism",
                "kind": "source",
                "path": "src/mode.py",
            },
        ],
        experiments=[
            {
                "experiment_id": "exp-practical",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:origin"],
                "origin_evidence_bindings": [
                    {
                        "atom_id": "atom:origin",
                        "role": "command",
                        "field_path": "$.command",
                        "value": support_command,
                    },
                    {
                        "atom_id": "atom:origin",
                        "role": "symptom",
                        "field_path": "$.output_excerpt",
                        "value": "bad",
                    },
                    {
                        "atom_id": "atom:origin",
                        "role": "expected_behavior",
                        "field_path": "$.expected_output",
                        "value": "correct",
                    },
                ],
                "positive_outcome_contract": {
                    "contract_kind": "origin_atom_exact_value",
                    "atom_id": "atom:origin",
                    "field_path": "$.expected_output",
                    "postcondition": {
                        "type": "command_stdout_contains",
                        "value": "correct",
                    },
                },
                "command": support_command,
                "result": "The repository CLI prints the wrong retained mode.",
                "outcome": "supports",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "bad",
                },
                "mechanism_link": mechanism_link,
                "artifact_refs": [
                    "artifact:repro",
                    "artifact:entrypoint",
                    "artifact:mechanism",
                ],
            },
            {
                "experiment_id": "exp-practical-challenge",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "exp-practical",
                    "mechanism_symbols": ["mode.read_mode"],
                    "controlled_variable": "input file identity",
                    "expected_difference": (
                        "If file-specific state is causal, a distinct file with the same "
                        "retained value will not reproduce the result."
                    ),
                },
                "addresses_atom_ids": ["atom:origin"],
                "origin_evidence_bindings": [
                    {
                        "atom_id": "atom:origin",
                        "role": "symptom",
                        "field_path": "$.output_excerpt",
                        "value": "bad",
                    }
                ],
                "command": challenge_command,
                "result": "A distinct retained input still produces the wrong mode.",
                "outcome": "supports",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "bad",
                },
                "mechanism_link": mechanism_link,
                "artifact_refs": [
                    "artifact:repro",
                    "artifact:entrypoint",
                    "artifact:mechanism",
                ],
            },
            {
                "experiment_id": "exp-practical-control",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "exp-practical",
                    "mechanism_symbols": ["mode.read_mode"],
                    "controlled_variable": "retained mode value",
                    "expected_difference": "The CLI prints the specifically correct mode.",
                },
                "addresses_atom_ids": ["atom:origin"],
                "command": control_command,
                "result": "The same repository CLI prints the correct retained mode.",
                "outcome": "refutes",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "correct",
                },
                "mechanism_link": mechanism_link,
                "artifact_refs": [
                    "artifact:repro",
                    "artifact:entrypoint",
                    "artifact:mechanism",
                ],
            },
            {
                "experiment_id": "exp-practical-challenge-control",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "exp-practical-challenge",
                    "mechanism_symbols": ["mode.read_mode"],
                    "controlled_variable": "retained mode value",
                    "expected_difference": "The CLI prints the specifically correct mode.",
                },
                "addresses_atom_ids": ["atom:origin"],
                "command": challenge_control_command,
                "result": "The same repository CLI prints the correct retained mode.",
                "outcome": "refutes",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "correct",
                },
                "mechanism_link": mechanism_link,
                "artifact_refs": [
                    "artifact:repro",
                    "artifact:entrypoint",
                    "artifact:mechanism",
                ],
            },
        ],
        inspected_files=["tools/show_mode.py", "src/mode.py"],
        inspected_symbols=["show_mode.main", "mode.read_mode"],
        root_cause_hypotheses=[
            {
                "hypothesis_id": "h1",
                "statement": "The mode reader returns the retained wrong value.",
                "supporting_evidence": [
                    "exp-practical",
                    "exp-practical-challenge",
                ],
                "counterevidence": [
                    "exp-practical-control",
                ],
                "mechanism_symbols": ["mode.read_mode"],
                "disposition": "primary",
                "disposition_evidence": [
                    "exp-practical",
                    "exp-practical-challenge",
                    "exp-practical-control",
                ],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:correct-input",
                        "hypothesis_id": "h1",
                        "claim": "The mode reader returns the retained wrong value.",
                        "baseline_experiment_id": "exp-practical",
                        "challenge_experiment_id": "exp-practical-challenge",
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
    )

    origin_path = tmp_path / "practical_origin.json"
    origin_path.write_text('{"mode":"bad"}\n', encoding="utf-8")
    atom_snapshot = {
        "atom_id": "atom:origin",
        "text": "The repository CLI reports the wrong mode.",
        "command": support_command,
        "exit_code": 0,
        "output_excerpt": "bad",
        "expected_output": "correct",
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    assignment: dict[str, object] = {
        "status": "complete",
        "errors": [],
        "case_id": "case:test-1",
        "problem_id": "problem:test-1",
        "expected_atom_ids": ["atom:origin"],
        "atom_receipts": [
            {
                "atom_id": "atom:origin",
                "atom_sha256": sha256(
                    json.dumps(
                        atom_snapshot,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "atom_snapshot": atom_snapshot,
                "artifact_receipts": [
                    {
                        "path": str(origin_path),
                        "sha256": sha256(origin_path.read_bytes()).hexdigest(),
                        "size_bytes": origin_path.stat().st_size,
                    }
                ],
            }
        ],
    }
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    problem_payload = {
        "case_id": "case:test-1",
        "problem_id": "problem:test-1",
        "evidence_atoms": [{"atom_id": "atom:origin"}],
        "evidence_assignment": assignment,
    }

    def fake_run_once(*, config: RunnerConfig, request: RunRequest) -> RunResult:
        assert request.keep_workspace is True
        run_dir = tmp_path / "run_practical_verified"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": "success",
                "goal": "prove practical CLI persistence",
                "failure_point": "wrong retained value",
                "evidence": {"what_happened": "the practical CLI prints bad"},
                "attempted_fixes": [],
                "recommended_fix_path": ["correct the mode reader"],
                "extensions": {"backlog_repro_research": extension},
            },
        )
        _write_json(run_dir / "diff_numstat.json", [])
        _write_json(
            run_dir / "target_ref.json",
            {"commit_sha": revision, "ref": request.ref, "agent": "codex"},
        )
        _write_valid_codex_subscription_receipt(run_dir)
        _write_json(run_dir / "workspace_ref.json", {"workspace_dir": str(workspace)})
        events: list[dict[str, object]] = [
            {
                "type": "run_command",
                "data": {
                    "command": support_command,
                    "exit_code": 0,
                    "output_excerpt": "bad",
                },
            },
            {
                "type": "run_command",
                "data": {
                    "command": challenge_command,
                    "exit_code": 0,
                    "output_excerpt": "bad",
                },
            },
            {
                "type": "run_command",
                "data": {
                    "command": control_command,
                    "exit_code": 0,
                    "output_excerpt": "correct",
                },
            },
            {
                "type": "run_command",
                "data": {
                    "command": challenge_control_command,
                    "exit_code": 0,
                    "output_excerpt": "correct",
                },
            },
        ]
        for relative in ("tools/show_mode.py", "src/mode.py"):
            path = workspace / relative
            content = path.read_text(encoding="utf-8")
            events.append(
                {
                    "type": "read_file",
                    "data": {
                        "path": relative,
                        "bytes": path.stat().st_size,
                        "read_source": "tool",
                        "source_exit_code": 0,
                        **observed_read_attestation(
                            path=path,
                            observed_text=content,
                            source_exit_code=0,
                            allow_partial=True,
                        ),
                    },
                }
            )
        (run_dir / "normalized_events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)
    document = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=str(workspace),
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[problem_payload],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
        replay_executor=TrustedHostReplayExecutor(
            approved_source_roots=[workspace],
            source_identity=workspace,
        ),
        replay_executor_metadata={
            "executor": "trusted_host",
            "approved_source_roots": [str(workspace.resolve())],
            "source_identity": str(workspace.resolve()),
        },
    )

    dossier = json.loads(json.dumps(document["items"][0]))
    assert dossier["research_status"] == "evidence_sufficient", dossier["evidence_verification"][
        "errors"
    ]
    authorizations = {
        receipt["experiment_id"]: receipt["command_authorization"]
        for receipt in dossier["evidence_verification"]["experiments"]
    }
    assert authorizations["exp-practical"]["authorization_kind"] == ("immutable_source_command")
    assert authorizations["exp-practical-control"]["authorization_kind"] == (
        "declared_inspected_repository_entrypoint"
    )
    persisted_ready, persisted_reasons = verify_persisted_research_evidence(dossier)
    assert persisted_ready is True, persisted_reasons
    research_ready, readiness_reasons = assess_research_readiness(dossier)
    assert research_ready is True, readiness_reasons

    practical_receipt = dossier["evidence_verification"]["experiments"][0]
    practical_receipt["command_authorization"]["workspace_confined"] = False
    dossier["evidence_verification"]["receipt_sha256"] = evidence_verification_sha256(
        dossier["evidence_verification"]
    )
    persisted_ready, persisted_reasons = verify_persisted_research_evidence(dossier)
    assert persisted_ready is False
    assert (
        "research_experiment_receipt_changed:exp-practical:command_authorization"
    ) in persisted_reasons


def test_run_repro_research_stage_blocks_unresolved_origin_atoms_without_running_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_called(**kwargs: object) -> RunResult:
        raise AssertionError(f"run_once should not be called: {kwargs}")

    monkeypatch.setattr(mod, "run_once", fail_if_called)

    doc = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:example",
        repo_ref="HEAD",
        target_slug="target_a",
        selected_problems=[
            {
                "problem_id": "problem:test-1",
                "evidence_atoms": [{"atom_id": "atom:present"}],
                "missing_evidence_atom_ids": ["atom:missing"],
            }
        ],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
    )

    dossier = doc["items"][0]
    assert dossier["research_status"] == "blocked"
    assert dossier["evidence_verification"]["status"] == "failed"
    assert dossier["blocking_reasons"] == [
        "origin_evidence_atoms_unresolved:atom:missing,origin_evidence_assignment_missing"
    ]
