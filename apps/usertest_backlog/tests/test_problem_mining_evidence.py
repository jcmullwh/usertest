from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from agent_adapters import (
    CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS,
    CodexLoginStatusResult,
)
from backlog_core import build_operational_failure_candidates
from backlog_core.case_lineage import (
    apply_atom_dispositions,
    atom_disposition_receipt_errors,
    eligible_problem_mining_atoms,
)
from backlog_miner.ensemble import (
    _CODEX_CHATGPT_CONFIG_OVERRIDES,
    _codex_auth_receipt_path,
    _write_codex_auth_receipt,
)
from backlog_miner.origin_evidence import origin_attachment_requirements
from backlog_miner.pipeline import _write_model_invocation_manifest
from backlog_repo import write_case_relation_receipt

from usertest_backlog.workflows.problem_mining import (
    _atoms_for_problem_mining_prompt,
    _cross_job_leaf_routing_nodes,
    _partition_problem_mining_chunks,
    _preserve_primary_after_coverage_review_failure,
    _problem_mining_attempt_manifest_sha256,
    _problem_mining_job_batches,
    _reconcile_problem_mining_reviews,
    _relation_review_payload,
    _routing_record_keys,
    _run_cross_job_problem_synthesis,
    _run_problem_mining_job_with_response_retry,
    _run_problem_mining_stage,
    _run_relation_review_batches,
    _verified_relation_edges_from_case_registry,
    _write_chunked_problem_mining_atoms_workspace,
)
from usertest_backlog.workflows.problem_mining_evidence import (
    ProblemMiningResponseContractError,
    _attempt_history_errors,
    _cross_job_synthesis_errors,
    apply_problem_mining_decision_partition,
    build_dry_run_miner_receipt,
    build_failed_miner_receipt,
    build_live_miner_receipt,
    build_problem_mining_evidence_draft,
    finalize_problem_mining_evidence_receipt,
    normalize_problem_mining_events,
    parse_problem_mining_response_envelope,
    problem_mining_evidence_receipt_ref,
    verify_problem_mining_evidence_receipt,
)


def _atom(atom_id: str = "atom:one", *, role: str = "observation") -> dict[str, object]:
    return {
        "atom_id": atom_id,
        "run_id": "run:one",
        "run_rel": "run/one",
        "origin_run_id": "run:one",
        "origin_stage": "observation" if role == "observation" else "repro_research",
        "source": "command_failure",
        "severity_hint": "low",
        "text": "The command failed before the requested workflow could complete.",
        "derived_from_atom_ids": [],
        "evidence_role": role,
        "parent_case_id": None,
        "case_id": None,
        "supporting_case_ids": [],
        "disposition": "unresolved",
        "disposition_status": "pending",
        "disposition_receipt": None,
    }


def _problem(atom_id: str = "atom:one") -> dict[str, object]:
    return {
        "problem_id": "problem:one",
        "case_id": "case:one",
        "title": "Command aborts the workflow",
        "problem": "The observed command failure aborts the requested workflow.",
        "user_impact": "The workflow cannot complete.",
        "severity": "high",
        "confidence": 0.9,
        "evidence_atom_ids": [atom_id],
        "evidence_summary": "The full atom records the failed command.",
        "problem_status": "identified",
    }


def _valid_problem_mining_response(atom_id: str = "atom:one") -> str:
    problem = _problem(atom_id)
    problem.pop("case_id", None)
    return json.dumps(
        {
            "problem_records": [problem],
            "atom_decisions": [
                {
                    "atom_id": atom_id,
                    "disposition": "supports_case",
                    "problem_ids": ["problem:one"],
                    "rationale": "The complete atom records the blocking command failure.",
                    "revisit_when": None,
                }
            ],
        }
    )


def _write_full_read_event(
    path: Path,
    *,
    relative_path: str,
    file_path: Path,
    append: bool = False,
) -> None:
    content = file_path.read_text(encoding="utf-8")
    file_bytes = file_path.read_bytes()
    event = {
        "ts": "2026-07-10T00:00:00Z",
        "type": "read_file",
        "data": {
            "path": relative_path,
            "bytes": len(file_bytes),
            "read_source": "tool",
            "source_exit_code": 0,
            "content_observed": True,
            "whole_file_observed": True,
            "observed_content": content,
            "observed_content_sha256": sha256(content.encode("utf-8")).hexdigest(),
            "observed_bytes": len(content.encode("utf-8")),
            "observed_start_line": 1,
            "observed_end_line": max(1, content.count("\n")),
            "file_sha256": sha256(file_bytes).hexdigest(),
            "file_size_bytes": len(file_bytes),
        },
    }
    with path.open("a" if append else "w", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


def _write_required_workspace_read_events(
    path: Path,
    *,
    workspace: Path,
    manifest: dict[str, object],
    append: bool = False,
    include_chunks: bool = True,
) -> None:
    relative_paths = [
        "atoms.json",
        str(manifest["index_file"]),
    ]
    if include_chunks:
        relative_paths.extend(
            str(chunk["text_file"]) for chunk in manifest["chunks"] if isinstance(chunk, dict)
        )
    should_append = append
    for relative_path in relative_paths:
        _write_full_read_event(
            path,
            relative_path=relative_path,
            file_path=workspace / relative_path,
            append=should_append,
        )
        should_append = True


def _write_fake_codex_attempt_artifacts(
    *,
    kwargs: dict[str, object],
    response: str,
    read_chunks: bool = True,
) -> str:
    out_dir = Path(str(kwargs["out_dir"]))
    tag = str(kwargs["tag"])
    workspace = Path(str(kwargs["workspace_dir"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{tag}.prompt.txt").write_text(
        str(kwargs["prompt"]),
        encoding="utf-8",
        newline="\n",
    )
    (out_dir / f"{tag}.response.txt").write_text(
        response,
        encoding="utf-8",
        newline="\n",
    )
    manifest = json.loads((workspace / "atoms.json").read_text(encoding="utf-8"))
    relative_paths = ["atoms.json", str(manifest["index_file"])]
    if read_chunks:
        relative_paths.extend(str(chunk["text_file"]) for chunk in manifest["chunks"])
    events = []
    for index, relative_path in enumerate(relative_paths, start=1):
        file_path = workspace / relative_path
        events.append(
            {
                "id": f"read-{index}",
                "msg": {
                    "type": "exec_command_end",
                    "command": [
                        "Get-Content",
                        "-Raw",
                        "-Encoding",
                        "UTF8",
                        "-LiteralPath",
                        relative_path,
                    ],
                    "exit_code": 0,
                    "cwd": str(workspace),
                    "stdout": file_path.read_text(encoding="utf-8"),
                },
            }
        )
    (out_dir / f"{tag}.raw_events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    last_message_path = out_dir / f"{tag}.last_message.txt"
    stderr_path = out_dir / f"{tag}.stderr.txt"
    raw_events_path = out_dir / f"{tag}.raw_events.jsonl"
    last_message_path.write_text(response, encoding="utf-8", newline="\n")
    stderr_path.write_text("", encoding="utf-8")
    codex_home = out_dir / "host_codex_home"
    codex_home.mkdir(exist_ok=True)
    login_status = CodexLoginStatusResult(
        argv=["codex", "login", "status"],
        exit_code=0,
        stdout="Logged in using ChatGPT\n",
        stderr="",
        codex_home=str(codex_home),
        auth_env_vars_blank={name: True for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS},
    )
    process_env_overrides = {name: "" for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS}
    process_env_overrides["CODEX_HOME"] = str(codex_home)
    _write_codex_auth_receipt(
        receipt_path=_codex_auth_receipt_path(raw_events_path),
        prompt=str(kwargs["prompt"]),
        codex_home=codex_home,
        configured_overrides=[],
        effective_overrides=list(_CODEX_CHATGPT_CONFIG_OVERRIDES),
        process_env_overrides=process_env_overrides,
        preflight=login_status,
        postcheck=login_status,
        model_attempted=True,
        model_exit_code=0,
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
    )
    _write_model_invocation_manifest(
        stage=str(kwargs["stage"]),
        tag=tag,
        agent=str(kwargs["agent"]),
        out_dir=out_dir,
        prompt=str(kwargs["prompt"]),
        response=response,
        error_kind=None,
    )
    return response


def test_problem_mining_response_parser_reports_top_level_json_error() -> None:
    response = (
        r'{"problem_records":[{"problem_id":"problem:one"}],'
        r'"atom_decisions":[{"rationale":"Path .\scripts\run.ps1"}]}'
    )

    with pytest.raises(ProblemMiningResponseContractError) as raised:
        parse_problem_mining_response_envelope(response)

    error = str(raised.value)
    assert error.startswith("problem_mining_response_json_invalid:Invalid \\escape")
    assert "line=1" in error
    assert "column=" in error
    assert "envelope_fields_invalid" not in error


@pytest.mark.parametrize(
    "response",
    [
        'before {"problem_records": [], "atom_decisions": []}',
        '```json\n{"problem_records": [], "atom_decisions": []}\n```',
        '{"problem_records": [], "atom_decisions": []} trailing',
        '{"problem_records": [], "atom_decisions": []} {}',
    ],
)
def test_problem_mining_response_parser_rejects_non_json_only_output(
    response: str,
) -> None:
    with pytest.raises(
        ProblemMiningResponseContractError,
        match="problem_mining_response_json_invalid",
    ):
        parse_problem_mining_response_envelope(response)


def test_problem_miner_prompt_requires_valid_windows_path_json_escaping() -> None:
    prompt_path = (
        Path(__file__).resolve().parents[3]
        / "configs"
        / "backlog_prompts"
        / "problem_miner_default.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")

    assert "complete, valid JSON with no prose or markdown" in prompt
    assert "literal Windows path" in prompt
    assert "`\\\\`" in prompt


def test_primary_response_retry_reruns_full_job_and_retains_first_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atom = _atom()
    prompt_atoms = _atoms_for_problem_mining_prompt([atom])
    initial_workspace = (
        tmp_path / "artifacts" / "problem_mining" / "problem_mining_001" / "workspace"
    )
    initial_manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=initial_workspace,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
        source_root=tmp_path,
    )
    invalid_response = (
        r'{"problem_records":[{"problem_id":"problem:one"}],'
        r'"atom_decisions":[{"rationale":"Path .\scripts\run.ps1"}]}'
    )
    valid_response = _valid_problem_mining_response()
    calls: list[dict[str, object]] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        calls.append(dict(kwargs))
        response = invalid_response if len(calls) == 1 else valid_response
        return _write_fake_codex_attempt_artifacts(kwargs=dict(kwargs), response=response)

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )

    result = _run_problem_mining_job_with_response_retry(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="problem_mining_001",
        prompt="Return the strict response after reading all evidence.",
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=["atom:one"],
        max_records_per_miner=20,
        eligible_atom_ids=["atom:one"],
        template_name="problem_miner_default.md",
        record_contract_error_prefix="problem_mining_problem_record_contract_invalid",
        agent="codex",
        model=None,
        cfg=object(),
        initial_workspace_dir=initial_workspace,
        initial_manifest=initial_manifest,
    )

    assert result["failure"] is None
    assert [str(call["tag"]) for call in calls] == [
        "problem_mining_001",
        "problem_mining_001_format_retry_001",
    ]
    attempts = result["attempt_history"]
    assert [attempt["status"] for attempt in attempts] == [
        "response_contract_failed",
        "verified",
    ]
    assert attempts[0]["workspace_dir"] != attempts[1]["workspace_dir"]
    assert (
        attempts[0]["workspace_manifest_sha256"]
        == attempts[1]["workspace_manifest_sha256"]
        == _problem_mining_attempt_manifest_sha256(initial_manifest)
    )
    assert (
        attempts[0]["artifacts"]["response"]["sha256"]
        == sha256(invalid_response.encode("utf-8")).hexdigest()
    )
    assert result["receipt"]["successful_attempt_tag"] == ("problem_mining_001_format_retry_001")
    assert result["receipt"]["workspace_dir"] == attempts[1]["workspace_dir"]
    assert len(result["receipt"]["read_attestations"]) == 1
    retry_prompt = Path(attempts[1]["artifacts"]["prompt"]["path"]).read_text(encoding="utf-8")
    assert "Previous reads do not count" in retry_prompt
    assert "Do not copy, patch, or mechanically repair" in retry_prompt
    assert "`\\\\`" in retry_prompt
    assert invalid_response not in retry_prompt
    rejected_response = Path(attempts[0]["artifacts"]["response"]["path"])
    rejected_response.write_text("tampered rejected response\n", encoding="utf-8")
    assert any(
        error.startswith("problem_mining_attempt_artifact_changed")
        for error in _attempt_history_errors(
            result["receipt"],
            tag="problem_mining_001",
        )
    )


def test_problem_mining_workspace_rejects_stale_unmanifested_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "stale-evidence.txt").write_text("not part of this job\n", encoding="utf-8")

    with pytest.raises(ValueError, match="workspace must be new or empty"):
        _write_chunked_problem_mining_atoms_workspace(
            workspace_dir=workspace,
            prompt_atoms=_atoms_for_problem_mining_prompt([_atom()]),
            max_records_per_miner=20,
            assigned_atom_ids=["atom:one"],
        )


def test_primary_response_retry_cannot_reuse_first_attempt_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atom = _atom()
    prompt_atoms = _atoms_for_problem_mining_prompt([atom])
    initial_workspace = (
        tmp_path / "artifacts" / "problem_mining" / "problem_mining_001" / "workspace"
    )
    initial_manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=initial_workspace,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
        source_root=tmp_path,
    )
    invalid_response = (
        r'{"problem_records":[{"problem_id":"problem:one"}],'
        r'"atom_decisions":[{"rationale":"Path .\scripts\run.ps1"}]}'
    )
    valid_response = json.dumps(
        {
            "problem_records": [],
            "atom_decisions": [
                {
                    "atom_id": "atom:one",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "The retry could not establish a problem from the evidence.",
                    "revisit_when": None,
                }
            ],
        }
    )
    calls: list[dict[str, object]] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        calls.append(dict(kwargs))
        return _write_fake_codex_attempt_artifacts(
            kwargs=dict(kwargs),
            response=invalid_response if len(calls) == 1 else valid_response,
            read_chunks=len(calls) == 1,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )

    result = _run_problem_mining_job_with_response_retry(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="problem_mining_001",
        prompt="Return the strict response after reading all evidence.",
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=["atom:one"],
        max_records_per_miner=20,
        eligible_atom_ids=["atom:one"],
        template_name="problem_miner_default.md",
        record_contract_error_prefix="problem_mining_problem_record_contract_invalid",
        agent="codex",
        model=None,
        cfg=object(),
        initial_workspace_dir=initial_workspace,
        initial_manifest=initial_manifest,
    )

    assert len(calls) == 2
    assert isinstance(result["failure"], ValueError)
    assert "problem_mining_required_evidence_file_not_read_in_full" in str(result["failure"])
    assert [attempt["status"] for attempt in result["attempt_history"]] == [
        "response_contract_failed",
        "failed",
    ]
    assert "receipt" not in result


def test_response_contract_retry_is_bounded_to_one_complete_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atom = _atom()
    prompt_atoms = _atoms_for_problem_mining_prompt([atom])
    initial_workspace = (
        tmp_path / "artifacts" / "problem_mining" / "problem_mining_001" / "workspace"
    )
    initial_manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=initial_workspace,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
        source_root=tmp_path,
    )
    invalid_response = (
        r'{"problem_records":[{"problem_id":"problem:one"}],'
        r'"atom_decisions":[{"rationale":"Path .\scripts\run.ps1"}]}'
    )
    calls: list[dict[str, object]] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        calls.append(dict(kwargs))
        return _write_fake_codex_attempt_artifacts(kwargs=dict(kwargs), response=invalid_response)

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )

    result = _run_problem_mining_job_with_response_retry(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="problem_mining_001",
        prompt="Return the strict response after reading all evidence.",
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=["atom:one"],
        max_records_per_miner=20,
        eligible_atom_ids=["atom:one"],
        template_name="problem_miner_default.md",
        record_contract_error_prefix="problem_mining_problem_record_contract_invalid",
        agent="codex",
        model=None,
        cfg=object(),
        initial_workspace_dir=initial_workspace,
        initial_manifest=initial_manifest,
    )

    assert len(calls) == 2
    assert isinstance(result["failure"], ProblemMiningResponseContractError)
    assert len(result["attempt_history"]) == 2
    assert all(
        attempt["status"] == "response_contract_failed" for attempt in result["attempt_history"]
    )
    assert all("response" in attempt["artifacts"] for attempt in result["attempt_history"])


def _raise_empty_problem_mining_response(kwargs: dict[str, object]) -> None:
    """Model the generic stage helper's empty-output failure artifact behavior."""

    _write_fake_codex_attempt_artifacts(kwargs=kwargs, response="")
    (Path(str(kwargs["out_dir"])) / f"{kwargs['tag']}.response.txt").unlink()
    raise RuntimeError(
        "run_stage_prompt_json: empty response from agent "
        f"for stage=problem_mining tag={kwargs['tag']}"
    )


def test_empty_response_retry_retains_zero_byte_attempt_then_verifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atom = _atom()
    prompt_atoms = _atoms_for_problem_mining_prompt([atom])
    initial_workspace = (
        tmp_path / "artifacts" / "problem_mining" / "problem_mining_001" / "workspace"
    )
    initial_manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=initial_workspace,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
        source_root=tmp_path,
    )
    calls: list[dict[str, object]] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            _raise_empty_problem_mining_response(dict(kwargs))
        return _write_fake_codex_attempt_artifacts(
            kwargs=dict(kwargs),
            response=_valid_problem_mining_response(),
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )

    result = _run_problem_mining_job_with_response_retry(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="problem_mining_001",
        prompt="Return the strict response after reading all evidence.",
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=["atom:one"],
        max_records_per_miner=20,
        eligible_atom_ids=["atom:one"],
        template_name="problem_miner_default.md",
        record_contract_error_prefix="problem_mining_problem_record_contract_invalid",
        agent="codex",
        model=None,
        cfg=object(),
        initial_workspace_dir=initial_workspace,
        initial_manifest=initial_manifest,
    )

    assert result["failure"] is None
    attempts = result["attempt_history"]
    assert [attempt["status"] for attempt in attempts] == [
        "response_contract_failed",
        "verified",
    ]
    empty_ref = attempts[0]["artifacts"]["response"]
    assert empty_ref["bytes"] == 0
    assert empty_ref["sha256"] == sha256(b"").hexdigest()
    assert Path(empty_ref["path"]).read_bytes() == b""
    assert _attempt_history_errors(result["receipt"], tag="problem_mining_001") == []


def test_empty_response_retry_is_bounded_and_retains_both_empty_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_atoms = _atoms_for_problem_mining_prompt([_atom()])
    initial_workspace = (
        tmp_path / "artifacts" / "problem_mining" / "problem_mining_001" / "workspace"
    )
    initial_manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=initial_workspace,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
        source_root=tmp_path,
    )
    calls: list[dict[str, object]] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        calls.append(dict(kwargs))
        _raise_empty_problem_mining_response(dict(kwargs))

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )

    result = _run_problem_mining_job_with_response_retry(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="problem_mining_001",
        prompt="Return the strict response after reading all evidence.",
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=["atom:one"],
        max_records_per_miner=20,
        eligible_atom_ids=["atom:one"],
        template_name="problem_miner_default.md",
        record_contract_error_prefix="problem_mining_problem_record_contract_invalid",
        agent="codex",
        model=None,
        cfg=object(),
        initial_workspace_dir=initial_workspace,
        initial_manifest=initial_manifest,
    )

    assert len(calls) == 2
    assert isinstance(result["failure"], RuntimeError)
    attempts = result["attempt_history"]
    assert [attempt["status"] for attempt in attempts] == [
        "response_contract_failed",
        "response_contract_failed",
    ]
    assert all(attempt["artifacts"]["response"]["bytes"] == 0 for attempt in attempts)
    failed_receipt = {
        "status": "failed_unresolved",
        "attempt_history": attempts,
        "successful_attempt_tag": None,
    }
    assert _attempt_history_errors(failed_receipt, tag="problem_mining_001") == []


@pytest.mark.parametrize("mutation", ["tamper", "remove"])
def test_empty_response_attempt_artifact_tamper_or_removal_fails_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    prompt_atoms = _atoms_for_problem_mining_prompt([_atom()])
    initial_workspace = (
        tmp_path / "artifacts" / "problem_mining" / "problem_mining_001" / "workspace"
    )
    initial_manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=initial_workspace,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
        source_root=tmp_path,
    )
    call_count = 0

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            _raise_empty_problem_mining_response(dict(kwargs))
        return _write_fake_codex_attempt_artifacts(
            kwargs=dict(kwargs),
            response=_valid_problem_mining_response(),
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )
    result = _run_problem_mining_job_with_response_retry(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="problem_mining_001",
        prompt="Return the strict response after reading all evidence.",
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=["atom:one"],
        max_records_per_miner=20,
        eligible_atom_ids=["atom:one"],
        template_name="problem_miner_default.md",
        record_contract_error_prefix="problem_mining_problem_record_contract_invalid",
        agent="codex",
        model=None,
        cfg=object(),
        initial_workspace_dir=initial_workspace,
        initial_manifest=initial_manifest,
    )
    response_path = Path(result["attempt_history"][0]["artifacts"]["response"]["path"])
    if mutation == "tamper":
        response_path.write_bytes(b"not empty")
    else:
        response_path.unlink()

    assert any(
        error.startswith("problem_mining_attempt_artifact_changed")
        for error in _attempt_history_errors(result["receipt"], tag="problem_mining_001")
    )


def _verified_stage1(tmp_path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    atom = _atom()
    draft = build_problem_mining_evidence_draft(atoms=[atom], eligible_atoms=[atom], mode="live")
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
        append=True,
    )
    problem = _problem()
    response = json.dumps(
        {
            "problem_records": [problem],
            "atom_decisions": [
                {
                    "atom_id": "atom:one",
                    "disposition": "supports_case",
                    "problem_ids": ["problem:one"],
                    "rationale": "The full atom directly records the command failure.",
                }
            ],
        }
    )
    miner = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=["atom:one"],
        eligible_atom_ids=["atom:one"],
        records=[problem],
        decisions=json.loads(response)["atom_decisions"],
        response_text=response,
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )
    draft["miners"] = [miner]
    partitioned = apply_problem_mining_decision_partition(
        atoms=[atom], canonical_records=[problem], draft=draft
    )
    final_atoms = apply_atom_dispositions(partitioned, [problem])
    receipt_path = tmp_path / "problem_mining_evidence.json"
    receipt = finalize_problem_mining_evidence_receipt(
        draft=draft,
        atoms=final_atoms,
        receipt_path=receipt_path,
    )
    stage1 = {
        "items": [problem],
        "input_meta": {
            "problem_mining_evidence_receipt": problem_mining_evidence_receipt_ref(
                receipt=receipt,
                receipt_path=receipt_path,
            )
        },
        "artifacts": {"problem_mining_evidence_receipt": str(receipt_path)},
    }
    return stage1, final_atoms


def test_live_receipt_binds_full_read_and_exact_final_partition(tmp_path: Path) -> None:
    stage1, atoms = _verified_stage1(tmp_path)

    assert (
        verify_problem_mining_evidence_receipt(
            stage1=stage1,
            atoms=atoms,
            require_live=True,
        )
        == []
    )
    receipt = json.loads(
        Path(stage1["artifacts"]["problem_mining_evidence_receipt"]).read_text(encoding="utf-8")
    )
    assert receipt["eligible_source_atom_ids"] == ["atom:one"]
    assert receipt["eligible_derived_atom_ids"] == []
    assert receipt["decision_partition"][0]["case_ids"] == ["case:one"]


def test_per_atom_only_read_cannot_bypass_required_complete_workspace_reads(
    tmp_path: Path,
) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    normalized = tmp_path / "normalized_events.jsonl"
    atom_file = manifest["atom_files"][0]
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )

    with pytest.raises(ValueError, match="required_evidence_file_not_read_in_full"):
        build_live_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            eligible_atom_ids=["atom:one"],
            records=[_problem()],
            decisions=[
                {
                    "atom_id": "atom:one",
                    "disposition": "supports_case",
                    "problem_ids": ["problem:one"],
                    "rationale": "The preview looked relevant.",
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_atom_decision_unknown_fields_are_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([_atom()]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    normalized = tmp_path / "normalized_events.jsonl"
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
    )

    with pytest.raises(ValueError, match="atom_decision_fields_invalid"):
        build_live_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            eligible_atom_ids=["atom:one"],
            records=[],
            decisions=[
                {
                    "atom_id": "atom:one",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "The evidence is incomplete.",
                    "revisit_when": None,
                    "invented_field": "must not be silently discarded",
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_complete_chunk_read_attests_every_contained_atom(tmp_path: Path) -> None:
    atoms = [_atom(f"atom:{index:02d}") for index in range(20)]
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt(atoms),
        max_records_per_miner=20,
        assigned_atom_ids=[str(atom["atom_id"]) for atom in atoms],
    )
    assert manifest["chunk_count"] == 1
    chunk = manifest["chunks"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(chunk["text_file"]),
        file_path=workspace / str(chunk["text_file"]),
    )
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
        append=True,
    )
    problem = _problem("atom:00")
    decisions = [
        {
            "atom_id": str(atom["atom_id"]),
            "disposition": "supports_case" if index == 0 else "expected_noise",
            "problem_ids": ["problem:one"] if index == 0 else [],
            "rationale": (
                "The command failure is direct evidence."
                if index == 0
                else "This atom repeats non-actionable progress output."
            ),
        }
        for index, atom in enumerate(atoms)
    ]

    receipt = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=[str(atom["atom_id"]) for atom in atoms],
        eligible_atom_ids=[str(atom["atom_id"]) for atom in atoms],
        records=[problem],
        decisions=decisions,
        response_text="{}",
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )

    assert len(receipt["read_attestations"]) == 20
    assert {row["evidence_file_kind"] for row in receipt["read_attestations"]} == {"chunk_markdown"}


@pytest.mark.parametrize("model_disposition", ["duplicate", "expected_noise"])
def test_model_only_permanent_disposition_is_coerced_to_reconsiderable(
    tmp_path: Path,
    model_disposition: str,
) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
        append=True,
    )

    receipt = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=["atom:one"],
        eligible_atom_ids=["atom:one"],
        records=[],
        decisions=[
            {
                "atom_id": "atom:one",
                "disposition": model_disposition,
                "problem_ids": [],
                "rationale": "The model considered this non-actionable.",
                "revisit_when": None,
            }
        ],
        response_text="{}",
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )

    decision = receipt["atom_decisions"][0]
    assert decision["disposition"] == "deferred"
    assert decision["revisit_when"]
    assert "disposition_proof" not in decision


def test_runner_rule_can_prove_proposal_evidence_is_expected_noise(tmp_path: Path) -> None:
    atom = _atom()
    atom["source"] = "suggested_change"
    atom["evidence_class"] = "proposal"
    draft = build_problem_mining_evidence_draft(atoms=[atom], eligible_atoms=[atom], mode="live")
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
        append=True,
    )
    miner = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=["atom:one"],
        eligible_atom_ids=["atom:one"],
        records=[],
        decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "expected_noise",
                "problem_ids": [],
                "rationale": "This is proposal evidence, not an observed failure.",
                "revisit_when": None,
            }
        ],
        response_text="{}",
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )
    draft["miners"] = [miner]
    partitioned = apply_problem_mining_decision_partition(
        atoms=[atom], canonical_records=[], draft=draft
    )
    final_atom = apply_atom_dispositions(partitioned, [])[0]

    assert final_atom["disposition"] == "expected_noise"
    assert final_atom["disposition_proof"]["rule_id"] == "proposal_evidence_class_v1"
    assert atom_disposition_receipt_errors(final_atom, require_decided=True) == []
    assert eligible_problem_mining_atoms([final_atom]) == []


@pytest.mark.parametrize("tampered_field", ["whole_file_observed", "file_sha256"])
def test_partial_or_hash_mismatched_chunk_read_attests_nothing(
    tmp_path: Path,
    tampered_field: str,
) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    chunk = manifest["chunks"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
        include_chunks=False,
    )
    _write_full_read_event(
        normalized,
        relative_path=str(chunk["text_file"]),
        file_path=workspace / str(chunk["text_file"]),
        append=True,
    )
    event_lines = normalized.read_text(encoding="utf-8").splitlines()
    event = json.loads(event_lines[-1])
    event["data"][tampered_field] = False if tampered_field == "whole_file_observed" else "0" * 64
    event_lines[-1] = json.dumps(event)
    normalized.write_text("\n".join(event_lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="required_evidence_file_not_read_in_full"):
        build_live_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            eligible_atom_ids=["atom:one"],
            records=[],
            decisions=[
                {
                    "atom_id": "atom:one",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "The available text does not establish a problem.",
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_codex_raw_chunk_read_normalizes_into_live_receipt(tmp_path: Path) -> None:
    atoms = [_atom("atom:one"), _atom("atom:two")]
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt(atoms),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one", "atom:two"],
    )
    chunk = manifest["chunks"][0]
    raw_events = tmp_path / "raw_events.jsonl"
    required_paths = ["atoms.json", str(manifest["index_file"]), str(chunk["text_file"])]
    raw_events.write_text(
        "".join(
            json.dumps(
                {
                    "id": f"required-read-{index}",
                    "msg": {
                        "type": "exec_command_end",
                        "command": ["Get-Content", "-Raw", "-LiteralPath", relative_path],
                        "exit_code": 0,
                        "cwd": str(workspace),
                        "stdout": (workspace / relative_path).read_text(encoding="utf-8"),
                    },
                }
            )
            + "\n"
            for index, relative_path in enumerate(required_paths, start=1)
        ),
        encoding="utf-8",
    )
    normalized = tmp_path / "normalized_events.jsonl"

    normalize_problem_mining_events(
        agent="codex",
        raw_events_path=raw_events,
        normalized_events_path=normalized,
        workspace_dir=workspace,
    )
    receipt = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=["atom:one", "atom:two"],
        eligible_atom_ids=["atom:one", "atom:two"],
        records=[],
        decisions=[
            {
                "atom_id": atom_id,
                "disposition": "expected_noise",
                "problem_ids": [],
                "rationale": "The complete atom is routine non-error progress output.",
                "revisit_when": None,
            }
            for atom_id in ("atom:one", "atom:two")
        ],
        response_text="{}",
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )

    assert len(receipt["read_attestations"]) == 2
    assert {row["evidence_file_kind"] for row in receipt["read_attestations"]} == {"chunk_markdown"}


def test_large_corpus_jobs_are_bounded_and_partition_exactly(tmp_path: Path) -> None:
    atoms = [_atom(f"atom:{index:04d}") for index in range(1305)]
    for index, atom in enumerate(atoms):
        atom["text"] = f"Evidence {index}: " + ("detailed observed output " * 32)
    prompt_atoms = _atoms_for_problem_mining_prompt(atoms)

    batches = _problem_mining_job_batches(prompt_atoms)

    assigned_ids = [str(atom["atom_id"]) for batch in batches for atom in batch]
    assert sorted(assigned_ids) == sorted(str(atom["atom_id"]) for atom in atoms)
    assert len(assigned_ids) == len(set(assigned_ids))
    assert len(batches) < 50
    for index, batch in enumerate(batches):
        workspace = tmp_path / f"workspace-{index:02d}"
        manifest = _write_chunked_problem_mining_atoms_workspace(
            workspace_dir=workspace,
            prompt_atoms=batch,
            max_records_per_miner=20,
            assigned_atom_ids=[str(atom["atom_id"]) for atom in batch],
        )
        assert manifest["chunk_count"] <= 3
        assert manifest["total_atom_count"] <= 100
        assert manifest["total_chunk_bytes"] <= 150_000


def _workspace_prompt_atoms(workspace: Path) -> list[dict[str, object]]:
    manifest = json.loads((workspace / "atoms.json").read_text(encoding="utf-8"))
    atoms: list[dict[str, object]] = []
    for chunk in manifest["chunks"]:
        atoms.extend(json.loads((workspace / str(chunk["file"])).read_text(encoding="utf-8")))
    return atoms


@pytest.mark.parametrize(
    "related_indexes",
    [(0, 204), (47, 167)],
    ids=["first_last", "middle_middle"],
)
def test_recursive_cross_job_routing_converges_distant_jobs_and_reopens_exact_atoms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    related_indexes: tuple[int, int],
) -> None:
    atom_count = 205
    atoms = [_atom(f"atom:{index:03d}") for index in range(atom_count)]
    for index, atom in enumerate(atoms):
        atom["text"] = (
            "Shared cross-boundary lifecycle symptom: the retained run is classified as "
            "complete although its terminal report is absent."
            if index in set(related_indexes)
            else f"Unrelated observation {index}: unique evidence with no repeated mechanism."
        )
    prompt_atoms = _atoms_for_problem_mining_prompt(atoms)
    miner_receipts = [
        {
            "tag": f"problem_mining_{index + 1:03d}",
            "status": "verified",
            "atom_decisions": [
                {
                    "atom_id": f"atom:{index:03d}",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "This leaf alone does not establish recurrence.",
                    "revisit_when": None,
                }
            ],
        }
        for index in range(atom_count)
    ]
    leaf_nodes = _cross_job_leaf_routing_nodes(
        prompt_atoms=prompt_atoms,
        miner_receipts=miner_receipts,
    )
    assert len(leaf_nodes) == atom_count
    assert {atom_id for node in leaf_nodes for atom_id in node["member_atom_ids"]} == {
        f"atom:{index:03d}" for index in range(atom_count)
    }
    exact_assignments: list[list[str]] = []
    exact_assignment_bytes: list[int] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        workspace = Path(str(kwargs["workspace_dir"]))
        assigned = _workspace_prompt_atoms(workspace)
        assigned_ids = [str(atom["atom_id"]) for atom in assigned]
        prompt = str(kwargs["prompt"])
        records: list[dict[str, object]] = []
        supported_ids: set[str] = set()
        problem_id_by_atom: dict[str, str] = {}
        if "EXACT CROSS-JOB SYNTHESIS" in prompt:
            evidence_ids = [f"atom:{index:03d}" for index in related_indexes]
            exact_assignments.append(assigned_ids)
            exact_manifest = json.loads((workspace / "atoms.json").read_text(encoding="utf-8"))
            exact_assignment_bytes.append(int(exact_manifest["total_chunk_bytes"]))
            # Generic routing keys deliberately create additional bounded exact
            # reviews.  Those reviews must remain honest when they do not contain
            # both observations that establish the cause.
            if set(evidence_ids).issubset(assigned_ids):
                records = [
                    {
                        "problem_id": "problem:cross-boundary-lifecycle-recurrence",
                        "title": "Incomplete runs are classified as complete across distant jobs",
                        "problem": (
                            "Two exact observations from distant mining jobs record the same "
                            "incomplete-run lifecycle misclassification."
                        ),
                        "user_impact": "Incomplete work can be treated as successfully finished.",
                        "severity": "high",
                        "confidence": 0.95,
                        "evidence_atom_ids": evidence_ids,
                        "evidence_summary": (
                            "Both exact atoms record the same terminal-report absence and "
                            "wrong completion classification."
                        ),
                        "problem_status": "identified",
                    }
                ]
                supported_ids = set(evidence_ids)
                problem_id_by_atom.update(
                    {
                        atom_id: "problem:cross-boundary-lifecycle-recurrence"
                        for atom_id in evidence_ids
                    }
                )
        elif "CROSS-JOB ROUTING ONLY" in prompt:
            for atom in assigned:
                route_id = str(atom["atom_id"])
                related = "terminal report is absent" in str(atom.get("text", "")) or (
                    "incomplete-run-classification" in str(atom.get("text", ""))
                )
                routing_keys = (
                    [
                        "generic-observation",
                        "incomplete-run-classification",
                        "terminal-report-absence",
                    ]
                    if related
                    else [
                        "generic-observation",
                        f"unique-mechanism-{route_id[-12:]}",
                        f"unique-boundary-{route_id[-10:]}",
                    ]
                )
                records.append(
                    {
                        "problem_id": f"problem:routing-{route_id[-16:]}",
                        "title": f"Routing theme {route_id[-12:]}",
                        "problem": "This is a routing-only semantic theme, not a final claim.",
                        "user_impact": "Exact evidence must be reopened before promotion.",
                        "severity": "medium",
                        "confidence": 0.6,
                        "evidence_atom_ids": [route_id],
                        "evidence_summary": "The complete compact theme received semantic keys.",
                        "problem_status": "identified",
                        "routing_keys": routing_keys,
                    }
                )
                supported_ids.add(route_id)
                problem_id_by_atom[route_id] = f"problem:routing-{route_id[-16:]}"
        decisions = [
            {
                "atom_id": atom_id,
                "disposition": "supports_case" if atom_id in supported_ids else "unresolved",
                "problem_ids": ([problem_id_by_atom[atom_id]] if atom_id in supported_ids else []),
                "rationale": (
                    "The independently reviewed evidence supports this grouping."
                    if atom_id in supported_ids
                    else "No cross-job relationship is established at this routing level."
                ),
                "revisit_when": None,
            }
            for atom_id in assigned_ids
        ]
        response = json.dumps({"problem_records": records, "atom_decisions": decisions})
        return _write_fake_codex_attempt_artifacts(kwargs=dict(kwargs), response=response)

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )
    template_path = (
        Path(__file__).resolve().parents[3]
        / "configs"
        / "backlog_prompts"
        / "problem_miner_default.md"
    )
    result = _run_cross_job_problem_synthesis(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        template_text=template_path.read_text(encoding="utf-8"),
        stage_guidance_text="Mine observed problems without proposing fixes.",
        prompt_atoms=prompt_atoms,
        miner_receipts=miner_receipts,
        agent="codex",
        model=None,
        cfg=object(),
    )

    assert result["status"] == "verified"
    assert result["leaf_theme_count"] == atom_count
    assert result["routing_levels"][0]["batch_count"] > 1
    assert result["routing_levels"][-1]["batch_count"] == 1
    assert len(result["routing_levels"]) >= 2
    generic_signals = [
        signal
        for signal in result["routing_signals"]
        if signal["routing_key"] == "generic-observation"
    ]
    assert generic_signals
    assert any(
        signal["disposition"] == "partitioned_candidate"
        and len(signal["member_atom_ids"]) == atom_count
        and signal["measured_exact_job_count"] > 1
        and all(len(group) <= signal["max_atoms"] for group in signal["refinement_groups"])
        for signal in generic_signals
    )
    expected_related_ids = {f"atom:{index:03d}" for index in related_indexes}
    exact = next(
        synthesis
        for synthesis in result["exact_syntheses"]
        if set(synthesis["candidate_atom_ids"]) == expected_related_ids
    )
    assert exact_assignments
    assert all(len(assignment) <= 100 for assignment in exact_assignments)
    assert all(total_bytes <= 150_000 for total_bytes in exact_assignment_bytes)
    exact_workspace = Path(exact["receipt"]["workspace_dir"])
    exact_manifest = json.loads((exact_workspace / "atoms.json").read_text(encoding="utf-8"))
    assert exact_manifest["assigned_atom_count"] == len(exact["candidate_atom_ids"])
    assert exact_manifest["assigned_atom_count"] == 2
    assert exact_manifest["problem_record_limit"] is None
    assert {decision["atom_id"] for decision in result["decision_overrides"]} == (
        expected_related_ids
    )


def test_supported_leaf_is_cross_job_anchor_but_never_a_decision_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_id = "atom:known-case"
    unresolved_id = "atom:new-observation"
    atoms = [_atom(anchor_id), _atom(unresolved_id)]
    atoms[0]["text"] = (
        "Known mechanism: final lifecycle classification ignores an absent terminal report."
    )
    atoms[1]["text"] = "New observation: another run was marked complete without a terminal report."
    prompt_atoms = _atoms_for_problem_mining_prompt(atoms)
    owner_decisions = {
        anchor_id: {
            "atom_id": anchor_id,
            "disposition": "supports_case",
            "problem_ids": ["problem:known-lifecycle-mechanism"],
            "rationale": "The leaf directly supports the already identified mechanism.",
            "revisit_when": None,
        },
        unresolved_id: {
            "atom_id": unresolved_id,
            "disposition": "unresolved",
            "problem_ids": [],
            "rationale": "This isolated observation does not establish the mechanism.",
            "revisit_when": None,
        },
    }
    receipts = [
        {
            "tag": "problem_mining_001",
            "status": "verified",
            "atom_decisions": [owner_decisions[anchor_id]],
        },
        {
            "tag": "problem_mining_002",
            "status": "verified",
            "atom_decisions": [owner_decisions[unresolved_id]],
        },
    ]

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        workspace = Path(str(kwargs["workspace_dir"]))
        assigned = _workspace_prompt_atoms(workspace)
        assigned_ids = [str(atom["atom_id"]) for atom in assigned]
        prompt = str(kwargs["prompt"])
        if "EXACT CROSS-JOB SYNTHESIS" in prompt:
            assert set(assigned_ids) == {anchor_id, unresolved_id}
            problem_id = "problem:known-lifecycle-mechanism"
            records = [
                {
                    "problem_id": problem_id,
                    "title": "Incomplete runs share the known lifecycle misclassification",
                    "problem": (
                        "Both exact observations show completion classification proceeding "
                        "despite an absent terminal report."
                    ),
                    "user_impact": "Incomplete work can be treated as successfully finished.",
                    "severity": "high",
                    "confidence": 0.95,
                    "evidence_atom_ids": sorted(assigned_ids),
                    "evidence_summary": (
                        "The known case and new observation establish the same mechanism."
                    ),
                    "problem_status": "identified",
                }
            ]
            decisions = [
                {
                    "atom_id": atom_id,
                    "disposition": "supports_case",
                    "problem_ids": [problem_id],
                    "rationale": "Exact comparison establishes the shared mechanism.",
                    "revisit_when": None,
                }
                for atom_id in assigned_ids
            ]
        else:
            problem_id = "problem:routing-shared-lifecycle"
            records = [
                {
                    "problem_id": problem_id,
                    "title": "Possible shared lifecycle mechanism",
                    "problem": "The compact themes warrant exact comparison.",
                    "user_impact": "Exact evidence must be reopened before promotion.",
                    "severity": "medium",
                    "confidence": 0.6,
                    "evidence_atom_ids": assigned_ids,
                    "evidence_summary": "Both themes describe the same phase and boundary.",
                    "problem_status": "identified",
                    "routing_keys": [
                        "lifecycle-completion-classification",
                        "terminal-report-absence",
                    ],
                }
            ]
            decisions = [
                {
                    "atom_id": atom_id,
                    "disposition": "supports_case",
                    "problem_ids": [problem_id],
                    "rationale": "The routing themes share bounded causal keys.",
                    "revisit_when": None,
                }
                for atom_id in assigned_ids
            ]
        response = json.dumps({"problem_records": records, "atom_decisions": decisions})
        return _write_fake_codex_attempt_artifacts(kwargs=dict(kwargs), response=response)

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )
    template_path = (
        Path(__file__).resolve().parents[3]
        / "configs"
        / "backlog_prompts"
        / "problem_miner_default.md"
    )
    result = _run_cross_job_problem_synthesis(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        template_text=template_path.read_text(encoding="utf-8"),
        stage_guidance_text="Mine observed problems without proposing fixes.",
        prompt_atoms=prompt_atoms,
        miner_receipts=receipts,
        agent="codex",
        model=None,
        cfg=object(),
    )

    leaf_dispositions = {
        atom_id: disposition
        for leaf in result["leaf_membership"]
        for atom_id, disposition in leaf["original_disposition_by_atom"].items()
    }
    assert leaf_dispositions == {
        anchor_id: "supports_case",
        unresolved_id: "unresolved",
    }
    assert result["candidate_groups"] == [[anchor_id, unresolved_id]]
    assert [override["atom_id"] for override in result["decision_overrides"]] == [unresolved_id]
    assert [
        override["atom_id"] for override in result["exact_syntheses"][0]["decision_overrides"]
    ] == [unresolved_id]
    assert result["records"][0]["evidence_atom_ids"] == sorted([anchor_id, unresolved_id])

    leaf_hashes = {
        atom_id: evidence_sha
        for leaf in result["leaf_membership"]
        for atom_id, evidence_sha in leaf["evidence_sha256_by_atom"].items()
    }
    verification_args = {
        "eligible_ids": {anchor_id, unresolved_id},
        "eligible_evidence_sha256_by_atom": leaf_hashes,
        "require_live": True,
        "owner_decisions_by_atom": owner_decisions,
    }
    assert _cross_job_synthesis_errors(result, **verification_args) == []

    claim_tampered = deepcopy(result)
    anchor_leaf = next(
        leaf for leaf in claim_tampered["leaf_membership"] if leaf["member_atom_ids"] == [anchor_id]
    )
    anchor_leaf["leaf_claim_sha256_by_atom"][anchor_id] = "f" * 64
    claim_errors = _cross_job_synthesis_errors(claim_tampered, **verification_args)
    assert any("cross_job_leaf_claim_changed" in error for error in claim_errors)

    disposition_tampered = deepcopy(result)
    anchor_leaf = next(
        leaf
        for leaf in disposition_tampered["leaf_membership"]
        if leaf["member_atom_ids"] == [anchor_id]
    )
    anchor_leaf["original_disposition_by_atom"][anchor_id] = "unresolved"
    disposition_errors = _cross_job_synthesis_errors(disposition_tampered, **verification_args)
    assert any("cross_job_leaf_claim_changed" in error for error in disposition_errors)

    override_tampered = deepcopy(result)
    exact_tag = str(override_tampered["exact_syntheses"][0]["tag"])
    override_tampered["decision_overrides"].append(
        {
            "atom_id": anchor_id,
            "disposition": "supports_case",
            "problem_ids": ["problem:known-lifecycle-mechanism"],
            "rationale": "Tampered override of evidence that already supported the case.",
            "revisit_when": None,
            "exact_synthesis_provenance": [
                {
                    "tag": exact_tag,
                    "problem_ids": ["problem:known-lifecycle-mechanism"],
                }
            ],
        }
    )
    override_errors = _cross_job_synthesis_errors(override_tampered, **verification_args)
    assert "problem_mining_cross_job_supported_anchor_overridden" in override_errors
    with pytest.raises(
        ValueError,
        match="problem_mining_cross_job_supported_anchor_overridden",
    ):
        apply_problem_mining_decision_partition(
            atoms=atoms,
            canonical_records=result["records"],
            draft={
                "eligible_atom_ids": [anchor_id, unresolved_id],
                "miners": receipts,
                "cross_job_synthesis": override_tampered,
            },
        )


def test_cross_job_support_overrides_apply_before_case_dispositions(tmp_path: Path) -> None:
    atoms = [_atom("atom:first"), _atom("atom:last")]
    record = _problem("atom:first")
    record["evidence_atom_ids"] = ["atom:first", "atom:last"]
    record["case_id"] = "case:cross-job"
    draft = build_problem_mining_evidence_draft(
        atoms=atoms,
        eligible_atoms=atoms,
        mode="live",
    )
    draft["miners"] = [
        {
            "atom_decisions": [
                {
                    "atom_id": atom_id,
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "The leaf alone was inconclusive.",
                    "revisit_when": None,
                }
            ]
        }
        for atom_id in ("atom:first", "atom:last")
    ]
    draft["cross_job_synthesis"] = {
        "status": "verified",
        "decision_overrides": [
            {
                "atom_id": atom_id,
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
                "rationale": "Exact cross-job evidence establishes the shared problem.",
                "revisit_when": None,
            }
            for atom_id in ("atom:first", "atom:last")
        ],
    }

    partitioned = apply_problem_mining_decision_partition(
        atoms=atoms,
        canonical_records=[record],
        draft=draft,
    )
    dispositioned = apply_atom_dispositions(partitioned, [record])

    assert {atom["disposition"] for atom in dispositioned} == {"supports_case"}
    assert {atom["case_id"] for atom in dispositioned} == {"case:cross-job"}


@pytest.mark.parametrize(
    "routing_keys",
    [
        ["only-one"],
        ["one", "two", "three", "four", "five", "six"],
        ["a" * 81, "valid-key"],
    ],
)
def test_routing_key_contract_rejects_noncanonical_bounds(
    routing_keys: list[str],
) -> None:
    with pytest.raises(ValueError, match="cross_job_routing_keys_invalid"):
        _routing_record_keys(
            {
                "problem_id": "problem:routing-invalid",
                "routing_keys": routing_keys,
            }
        )


def test_cross_job_leaf_verifier_recomputes_evidence_and_membership_hashes() -> None:
    errors = _cross_job_synthesis_errors(
        {
            "status": "verified",
            "leaf_membership": [
                {
                    "member_atom_ids": ["atom:one"],
                    "evidence_sha256_by_atom": {"atom:one": "b" * 64},
                    "membership_sha256": "c" * 64,
                }
            ],
            "routing_levels": [],
            "candidate_groups": [],
            "routing_sha256": "d" * 64,
            "exact_syntheses": [],
            "decision_overrides": [],
        },
        eligible_ids={"atom:one"},
        eligible_evidence_sha256_by_atom={"atom:one": "a" * 64},
        require_live=True,
    )

    assert any("cross_job_leaf_evidence_invalid" in error for error in errors)


def test_overbroad_cross_job_bucket_is_retained_without_blocking_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atoms = [_atom(f"atom:{index}") for index in range(3)]
    prompt_atoms = _atoms_for_problem_mining_prompt(atoms)
    receipts = [
        {
            "tag": f"problem_mining_{index + 1:03d}",
            "status": "verified",
            "atom_decisions": [
                {
                    "atom_id": f"atom:{index}",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "The leaf alone is inconclusive.",
                    "revisit_when": None,
                }
            ],
        }
        for index in range(3)
    ]
    exact_assignments: list[list[str]] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        prompt = str(kwargs["prompt"])
        workspace = Path(str(kwargs["workspace_dir"]))
        assigned_ids = [str(atom["atom_id"]) for atom in _workspace_prompt_atoms(workspace)]
        if "EXACT CROSS-JOB SYNTHESIS" in prompt:
            exact_assignments.append(assigned_ids)
            response = json.dumps(
                {
                    "problem_records": [],
                    "atom_decisions": [
                        {
                            "atom_id": atom_id,
                            "disposition": "unresolved",
                            "problem_ids": [],
                            "rationale": (
                                "The exact bounded evidence does not establish a shared cause."
                            ),
                            "revisit_when": None,
                        }
                        for atom_id in assigned_ids
                    ],
                }
            )
        else:
            record = {
                "problem_id": "problem:routing-overbroad",
                "title": "Over-broad routing bucket",
                "problem": "The routing layer grouped every compact theme.",
                "user_impact": "Exact evidence would exceed one bounded model job.",
                "severity": "medium",
                "confidence": 0.5,
                "evidence_atom_ids": assigned_ids,
                "evidence_summary": "All compact themes share deliberately broad test keys.",
                "problem_status": "identified",
                "routing_keys": ["shared-test-mechanism", "shared-test-boundary"],
            }
            response = json.dumps(
                {
                    "problem_records": [record],
                    "atom_decisions": [
                        {
                            "atom_id": atom_id,
                            "disposition": "supports_case",
                            "problem_ids": ["problem:routing-overbroad"],
                            "rationale": "The routing-only keys match.",
                            "revisit_when": None,
                        }
                        for atom_id in assigned_ids
                    ],
                }
            )
        return _write_fake_codex_attempt_artifacts(kwargs=dict(kwargs), response=response)

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining._PROBLEM_MINING_JOB_MAX_ATOMS",
        2,
    )
    template_path = (
        Path(__file__).resolve().parents[3]
        / "configs"
        / "backlog_prompts"
        / "problem_miner_default.md"
    )

    result = _run_cross_job_problem_synthesis(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        template_text=template_path.read_text(encoding="utf-8"),
        stage_guidance_text="Mine observed problems without proposing fixes.",
        prompt_atoms=prompt_atoms,
        miner_receipts=receipts,
        agent="codex",
        model=None,
        cfg=object(),
    )

    assert exact_assignments
    assert all(1 < len(assignment) <= 2 for assignment in exact_assignments)
    assert result["status"] == "verified"
    assert result["candidate_groups"] == [["atom:0", "atom:1"]]
    assert len(result["exact_syntheses"]) == 1
    assert result["exact_syntheses"][0]["records"] == []
    assert result["decision_overrides"] == []
    assert result["leaf_theme_count"] == len(atoms)
    assert result["nondiscriminative_routing_signal_count"] == 0
    assert {signal["routing_key"] for signal in result["routing_signals"]} == {
        "shared-test-mechanism",
        "shared-test-boundary",
    }
    assert all(
        signal["disposition"] == "partitioned_candidate"
        and signal["member_atom_ids"] == ["atom:0", "atom:1", "atom:2"]
        and signal["measured_exact_job_count"] == 2
        and signal["refinement_groups"] == [["atom:0", "atom:1"]]
        for signal in result["routing_signals"]
    )

    leaf_hashes = {
        atom_id: evidence_sha
        for leaf in result["leaf_membership"]
        for atom_id, evidence_sha in leaf["evidence_sha256_by_atom"].items()
    }
    owner_decisions = {
        str(decision["atom_id"]): decision
        for receipt in receipts
        for decision in receipt["atom_decisions"]
    }
    assert (
        _cross_job_synthesis_errors(
            result,
            eligible_ids={"atom:0", "atom:1", "atom:2"},
            eligible_evidence_sha256_by_atom=leaf_hashes,
            require_live=True,
            owner_decisions_by_atom=owner_decisions,
        )
        == []
    )

    tampered = deepcopy(result)
    tampered["routing_signals"][0]["refinement_groups"] = [["atom:1", "atom:2"]]
    errors = _cross_job_synthesis_errors(
        tampered,
        eligible_ids={"atom:0", "atom:1", "atom:2"},
        eligible_evidence_sha256_by_atom=leaf_hashes,
        require_live=True,
        owner_decisions_by_atom=owner_decisions,
    )
    assert any("cross_job_routing_signal_ceiling_invalid" in error for error in errors)


def test_overlapping_bounded_keys_keep_independent_exact_cases_and_union_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atom_ids = ["atom:a", "atom:b", "atom:c"]
    atoms = [_atom(atom_id) for atom_id in atom_ids]
    for atom in atoms:
        atom["text"] = f"routing-origin-{str(atom['atom_id'])[-1]}"
    prompt_atoms = _atoms_for_problem_mining_prompt(atoms)
    receipts = [
        {
            "tag": f"problem_mining_{index:03d}",
            "status": "verified",
            "atom_decisions": [
                {
                    "atom_id": atom_id,
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "The isolated leaf does not establish recurrence.",
                    "revisit_when": None,
                }
            ],
        }
        for index, atom_id in enumerate(atom_ids, start=1)
    ]
    exact_assignments: list[frozenset[str]] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        workspace = Path(str(kwargs["workspace_dir"]))
        assigned = _workspace_prompt_atoms(workspace)
        assigned_ids = [str(atom["atom_id"]) for atom in assigned]
        prompt = str(kwargs["prompt"])
        if "EXACT CROSS-JOB SYNTHESIS" in prompt:
            assignment = frozenset(assigned_ids)
            exact_assignments.append(assignment)
            exact_contract = {
                frozenset({"atom:a", "atom:b"}): (
                    "problem:ab",
                    "A and B establish one exact shared mechanism",
                ),
                frozenset({"atom:a", "atom:c"}): (
                    "problem:ac",
                    "A and C establish a different exact shared mechanism",
                ),
            }
            problem_id, title = exact_contract[assignment]
            records = [
                {
                    "problem_id": problem_id,
                    "title": title,
                    "problem": "The exact pair establishes a shared causal failure.",
                    "user_impact": "The causal failure can recur across source jobs.",
                    "severity": "high",
                    "confidence": 0.95,
                    "evidence_atom_ids": sorted(assignment),
                    "evidence_summary": "Both full atoms directly establish this mechanism.",
                    "problem_status": "identified",
                }
            ]
            decisions = [
                {
                    "atom_id": atom_id,
                    "disposition": "supports_case",
                    "problem_ids": [problem_id],
                    "rationale": "The exact pair establishes the retained case.",
                    "revisit_when": None,
                }
                for atom_id in assigned_ids
            ]
        else:
            routing_keys_by_origin = {
                "a": ["pair-ab", "pair-ac"],
                "b": ["pair-ab", "boundary-b"],
                "c": ["pair-ac", "boundary-c"],
            }
            origin_by_route = {
                str(atom["atom_id"]): origin
                for atom in assigned
                for origin in ("a", "b", "c")
                if f"routing-origin-{origin}" in str(atom.get("text", ""))
            }
            records = [
                {
                    "problem_id": f"problem:routing-{origin_by_route[route_id]}",
                    "title": f"Routing theme {origin_by_route[route_id]}",
                    "problem": "Routing-only semantic theme.",
                    "user_impact": "Exact review is required before promotion.",
                    "severity": "medium",
                    "confidence": 0.6,
                    "evidence_atom_ids": [route_id],
                    "evidence_summary": "The compact theme received semantic keys.",
                    "problem_status": "identified",
                    "routing_keys": routing_keys_by_origin[origin_by_route[route_id]],
                }
                for route_id in assigned_ids
            ]
            decisions = [
                {
                    "atom_id": route_id,
                    "disposition": "supports_case",
                    "problem_ids": [f"problem:routing-{origin_by_route[route_id]}"],
                    "rationale": "The routing-only atom received semantic keys.",
                    "revisit_when": None,
                }
                for route_id in assigned_ids
            ]
        response = json.dumps({"problem_records": records, "atom_decisions": decisions})
        return _write_fake_codex_attempt_artifacts(kwargs=dict(kwargs), response=response)

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )
    template_path = (
        Path(__file__).resolve().parents[3]
        / "configs"
        / "backlog_prompts"
        / "problem_miner_default.md"
    )
    result = _run_cross_job_problem_synthesis(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        template_text=template_path.read_text(encoding="utf-8"),
        stage_guidance_text="Mine observed problems without proposing fixes.",
        prompt_atoms=prompt_atoms,
        miner_receipts=receipts,
        agent="codex",
        model=None,
        cfg=object(),
    )

    expected_groups = [["atom:a", "atom:b"], ["atom:a", "atom:c"]]
    assert result["candidate_groups"] == expected_groups
    assert set(exact_assignments) == {
        frozenset({"atom:a", "atom:b"}),
        frozenset({"atom:a", "atom:c"}),
    }
    assert frozenset({"atom:a", "atom:b", "atom:c"}) not in exact_assignments
    assert {record["problem_id"] for record in result["records"]} == {
        "problem:ab",
        "problem:ac",
    }
    overrides = {override["atom_id"]: override for override in result["decision_overrides"]}
    assert overrides["atom:a"]["problem_ids"] == ["problem:ab", "problem:ac"]
    assert [
        provenance["problem_ids"]
        for provenance in overrides["atom:a"]["exact_synthesis_provenance"]
    ] == [["problem:ab"], ["problem:ac"]]
    assert overrides["atom:b"]["problem_ids"] == ["problem:ab"]
    assert overrides["atom:c"]["problem_ids"] == ["problem:ac"]
    assert all(len(exact["decision_overrides"]) == 2 for exact in result["exact_syntheses"])

    leaf_hashes = {
        atom_id: evidence_sha
        for leaf in result["leaf_membership"]
        for atom_id, evidence_sha in leaf["evidence_sha256_by_atom"].items()
    }
    assert (
        _cross_job_synthesis_errors(
            result,
            eligible_ids=set(atom_ids),
            eligible_evidence_sha256_by_atom=leaf_hashes,
            require_live=True,
        )
        == []
    )


def test_tiny_atom_corpus_respects_max_atoms_per_job() -> None:
    atoms = [_atom(f"atom:{index:04d}") for index in range(150)]
    for atom in atoms:
        atom["text"] = "x"

    batches = _problem_mining_job_batches(_atoms_for_problem_mining_prompt(atoms))

    assert [len(batch) for batch in batches] == [100, 50]
    assigned_ids = [str(atom["atom_id"]) for batch in batches for atom in batch]
    assert assigned_ids == [str(atom["atom_id"]) for atom in atoms]


def test_single_workspace_chunk_is_split_to_job_byte_limit(tmp_path: Path) -> None:
    atoms = [_atom(f"atom:{index:04d}") for index in range(24)]
    for index, atom in enumerate(atoms):
        atom["text"] = f"evidence-{index}-" + ("detail " * 24)
    prompt_atoms = _atoms_for_problem_mining_prompt(atoms)
    max_bytes = 8_000

    batches = _problem_mining_job_batches(
        prompt_atoms,
        chunk_max_bytes=55_000,
        max_chunks=3,
        max_atoms=100,
        max_bytes=max_bytes,
    )

    assert len(batches) > 1
    assigned_ids = [str(atom["atom_id"]) for batch in batches for atom in batch]
    assert assigned_ids == [str(atom["atom_id"]) for atom in prompt_atoms]
    for index, batch in enumerate(batches):
        manifest = _write_chunked_problem_mining_atoms_workspace(
            workspace_dir=tmp_path / f"byte-workspace-{index}",
            prompt_atoms=batch,
            max_records_per_miner=20,
            assigned_atom_ids=[str(atom["atom_id"]) for atom in batch],
        )
        assert manifest["total_chunk_bytes"] <= max_bytes
        assert manifest["total_text_chunk_bytes"] <= max_bytes


def test_many_operational_occurrences_use_bounded_explicit_stage1_projection() -> None:
    records: list[dict[str, object]] = []
    atoms: list[dict[str, object]] = []
    for index in range(400):
        run_id = f"implementation/run/disk-volume-{index:04d}"
        records.append(
            {
                "run_rel": run_id,
                "status": "error",
                "agent_exit_code": 1,
                "target_ref": {
                    "mission_id": "implement_backlog_ticket_v1",
                    "execution_backend": "local",
                },
                "error": {
                    "type": "AgentExecFailed",
                    "subtype": "disk_full",
                    "agent": f"agent_{index:04d}",
                },
                "operational_failure_signals": [
                    {
                        "kind": "infrastructure",
                        "phase": "storage",
                        "prevented_stage": True,
                        "error_type": "AgentExecFailed",
                        "error_subtype": "disk_full",
                        "artifact_sha256": f"{index:064x}",
                    }
                ],
            }
        )
        atoms.append(
            {
                "atom_id": f"{run_id}:run_failure_event:1",
                "run_rel": run_id,
                "origin_run_id": run_id,
                "origin_stage": "implementation",
                "source": "run_failure_event",
                "text": "Derived runner failure.",
                "evidence_class": "observed",
                "evidence_role": "implementation",
                "parent_case_id": "case:parent",
                "case_id": "case:parent",
                "supporting_case_ids": ["case:parent"],
                "disposition": "supports_case",
                "disposition_status": "decided",
                "lineage_authorities": ["runner_evidence_assignment"],
            }
        )

    candidate = build_operational_failure_candidates(records, atoms)[0]
    full_receipt_bytes = len(
        json.dumps(
            candidate["operational_candidate_receipt"],
            separators=(",", ":"),
        ).encode()
    )
    assert full_receipt_bytes > 55_000

    projection = _atoms_for_problem_mining_prompt([candidate])[0]
    projection_bytes = len(json.dumps(projection, separators=(",", ":")).encode())
    assert projection_bytes < 55_000
    assert projection["operational_full_receipt_excluded_from_prompt"] is True
    assert "operational_candidate_receipt" not in projection
    assert "source_derived_atom_ids" not in projection
    assert "related_parent_case_ids" not in projection
    assert projection["derived_from_atom_ids"] == []
    assert projection["operational_candidate_prompt_projection"]["occurrence_count"] == 400
    assert projection["operational_candidate_prompt_projection"]["evidence_shape_count"] == 400
    assert (
        projection["operational_candidate_prompt_projection"]["evidence_shapes_omitted_count"] > 0
    )
    assert projection["operational_candidate_prompt_projection"]["full_occurrence_ledger"].endswith(
        "excluded_from_stage1_prompt"
    )
    assert _partition_problem_mining_chunks([projection], chunk_max_bytes=55_000) == [[projection]]


def test_problem_mining_projection_retains_unique_evidence_context(tmp_path: Path) -> None:
    atom = _atom()
    atom.update(
        {
            "run_dir": "runs/target/run",
            "impact": "The workflow cannot complete for Windows users.",
            "evidence_text": "The captured stderr names the missing executable.",
            "command": "python -m usertest",
            "exit_code": 1,
            "output_excerpt": "FileNotFoundError: executable was not found",
            "artifact_ref": {"path": "stderr.txt", "sha256": "a" * 64},
            "excerpt_head": str(atom["text"]),
            "attachments": [
                {
                    "path": "stderr.txt",
                    "excerpt_head": "duplicated large stderr",
                    "artifact_ref": {"path": "stderr.txt", "sha256": "a" * 64},
                }
            ],
        }
    )

    projection = _atoms_for_problem_mining_prompt([atom])[0]

    assert projection["impact"] == atom["impact"]
    assert projection["evidence_text"] == atom["evidence_text"]
    assert projection["output_excerpt"] == atom["output_excerpt"]
    assert projection["artifact_ref"] == atom["artifact_ref"]
    assert "excerpt_head" not in projection
    assert "excerpt_head" not in projection["attachments"][0]
    assert projection["attachments"][0]["artifact_ref"] == atom["artifact_ref"]

    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=[projection],
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    required_markdown = (workspace / str(manifest["chunks"][0]["text_file"])).read_text(
        encoding="utf-8"
    )
    assert "FileNotFoundError: executable was not found" in required_markdown
    assert "The workflow cannot complete for Windows users." in required_markdown
    assert '"artifact_ref"' in required_markdown
    assert manifest["chunks"][0]["text_bytes"] <= manifest["chunk_max_bytes"]


def test_problem_mining_reads_middle_of_large_materialized_attachment(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target" / "one"
    run_dir.mkdir(parents=True)
    signature = "REAL_UNACTIONED_FAILURE_SIGNATURE_IN_MIDDLE"
    artifact = run_dir / "agent_stderr.txt"
    artifact.write_text(("ordinary-prefix\n" * 1_400) + signature + ("\nordinary-suffix" * 1_400))
    assert artifact.stat().st_size > 24 * 1024
    atom = _atom()
    atom.update(
        {
            "run_dir": str(run_dir),
            "text": "The retained diagnostic is larger than the atom excerpt.",
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
    )
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
        source_root=tmp_path,
    )
    origin_manifest = manifest["origin_attachment_evidence"]
    requirements = origin_attachment_requirements(origin_manifest, atom_ids=["atom:one"])
    assert len(requirements) >= 2
    matching = [
        item
        for item in requirements
        if signature in (workspace / str(item["file"])).read_text(encoding="utf-8")
    ]
    assert matching
    assert all(int(item["size_bytes"]) < 24 * 1024 for item in requirements)

    normalized = tmp_path / "normalized_events.jsonl"
    atom_file = manifest["atom_files"][0]
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
        append=True,
    )
    for requirement in requirements:
        _write_full_read_event(
            normalized,
            relative_path=str(requirement["file"]),
            file_path=workspace / str(requirement["file"]),
            append=True,
        )
    problem = _problem()
    problem["problem"] = f"The retained diagnostic reports {signature}."
    response = json.dumps(
        {
            "problem_records": [problem],
            "atom_decisions": [
                {
                    "atom_id": "atom:one",
                    "disposition": "supports_case",
                    "problem_ids": ["problem:one"],
                    "rationale": f"The materialized attachment contains {signature}.",
                }
            ],
        }
    )

    receipt = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=["atom:one"],
        eligible_atom_ids=["atom:one"],
        records=[problem],
        decisions=json.loads(response)["atom_decisions"],
        response_text=response,
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )

    assert len(receipt["origin_attachment_read_attestations"]) == len(requirements)
    assert any(
        item["file"] == matching[0]["file"]
        for item in receipt["origin_attachment_read_attestations"]
    )


def test_linked_atoms_remain_in_the_same_bounded_job() -> None:
    atoms = [_atom(f"atom:{index:03d}") for index in range(180)]
    for atom in atoms:
        atom["text"] = "observed output " * 80
    atoms[0]["linked_atom_ids"] = ["atom:179"]
    atoms[179]["linked_atom_ids"] = ["atom:000"]

    batches = _problem_mining_job_batches(_atoms_for_problem_mining_prompt(atoms))
    batch_by_atom = {
        str(atom["atom_id"]): batch_index
        for batch_index, batch in enumerate(batches)
        for atom in batch
    }

    assert batch_by_atom["atom:000"] == batch_by_atom["atom:179"]


def test_assignment_requires_one_decision_for_every_atom(tmp_path: Path) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
        append=True,
    )

    with pytest.raises(ValueError, match="decision_partition_mismatch"):
        build_live_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            eligible_atom_ids=["atom:one"],
            records=[],
            decisions=[],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_cited_atom_requires_matching_support_decision(tmp_path: Path) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
        append=True,
    )

    with pytest.raises(ValueError, match="citation_without_support_decision"):
        build_live_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            eligible_atom_ids=["atom:one"],
            records=[_problem()],
            decisions=[
                {
                    "atom_id": "atom:one",
                    "disposition": "expected_noise",
                    "problem_ids": [],
                    "rationale": "The atom was incorrectly dismissed.",
                    "revisit_when": None,
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_live_deferred_decision_requires_concrete_revisit_trigger(tmp_path: Path) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
        append=True,
    )

    with pytest.raises(ValueError, match="deferred_revisit_missing"):
        build_live_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            eligible_atom_ids=["atom:one"],
            records=[],
            decisions=[
                {
                    "atom_id": "atom:one",
                    "disposition": "deferred",
                    "problem_ids": [],
                    "rationale": "A referenced runtime artifact is not available yet.",
                    "revisit_when": None,
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_independent_review_can_recover_primary_missed_problem() -> None:
    records, decisions = _reconcile_problem_mining_reviews(
        primary_records=[],
        primary_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "expected_noise",
                "problem_ids": [],
                "rationale": "The primary pass treated the output as routine.",
                "revisit_when": None,
            }
        ],
        review_records=[_problem()],
        review_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
                "rationale": "The second pass recognized the workflow-blocking failure.",
                "revisit_when": None,
            }
        ],
    )

    assert [record["problem_id"] for record in records] == ["problem:one"]
    assert decisions[0]["disposition"] == "supports_case"


def test_independent_review_must_confirm_primary_support_claim_verbatim() -> None:
    primary_problem = _problem()
    records, decisions = _reconcile_problem_mining_reviews(
        primary_records=[primary_problem],
        primary_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
                "rationale": "The primary pass found a workflow-blocking command failure.",
                "revisit_when": None,
            }
        ],
        review_records=[dict(primary_problem)],
        review_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
                "rationale": "The complete atom independently confirms the exact claim.",
                "revisit_when": None,
            }
        ],
    )

    assert [record["problem_id"] for record in records] == ["problem:one"]
    assert decisions[0]["disposition"] == "supports_case"
    assert decisions[0]["problem_ids"] == ["problem:one"]
    assert "independently confirmed" in decisions[0]["rationale"]


def test_unconfirmed_primary_support_claim_becomes_unresolved() -> None:
    records, decisions = _reconcile_problem_mining_reviews(
        primary_records=[_problem()],
        primary_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
                "rationale": "The primary pass inferred a problem from the atom.",
                "revisit_when": None,
            }
        ],
        review_records=[],
        review_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "unresolved",
                "problem_ids": [],
                "rationale": "The evidence does not directly establish that claim.",
                "revisit_when": None,
            }
        ],
    )

    assert records == []
    assert len(decisions) == 1
    assert decisions[0]["atom_id"] == "atom:one"
    assert decisions[0]["disposition"] == "unresolved"
    assert decisions[0]["problem_ids"] == []
    assert decisions[0]["revisit_when"] is None
    assert "did not confirm" in decisions[0]["rationale"]


def test_coverage_review_failure_preserves_verified_primary_work() -> None:
    primary = {
        "tag": "problem_mining_001",
        "status": "verified",
        "assigned_atom_ids": ["atom:one"],
        "atom_decisions": [
            {
                "atom_id": "atom:one",
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
            }
        ],
    }
    failed_review = {
        "tag": "problem_mining_001_coverage_depth_review",
        "status": "failed_unresolved",
        "assigned_atom_ids": ["atom:one"],
    }

    preserved = _preserve_primary_after_coverage_review_failure(
        primary_receipt=primary,
        review_receipt=failed_review,
        review_failure="RuntimeError: reviewer unavailable",
    )

    assert preserved["status"] == "review_failed_primary_preserved"
    assert preserved["atom_decisions"] == primary["atom_decisions"]
    assert preserved["primary_pass"]["status"] == "verified"
    assert preserved["non_support_review"] == failed_review
    assert primary["status"] == "verified"


def test_all_support_job_still_runs_exactly_one_independent_full_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = tmp_path / "problem_miner_default.md"
    template.write_text(
        "{{STAGE_GUIDANCE}}\nEvidence: {{ATOMS_JSON}}\n",
        encoding="utf-8",
    )
    problem = _problem()
    problem.pop("case_id", None)
    decision = {
        "atom_id": "atom:one",
        "disposition": "supports_case",
        "problem_ids": ["problem:one"],
        "rationale": "The complete atom records a workflow-blocking command failure.",
        "revisit_when": None,
    }
    response = json.dumps(
        {
            "problem_records": [problem],
            "atom_decisions": [decision],
        }
    )
    prompts: list[str] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        prompts.append(str(kwargs["prompt"]))
        return _write_fake_codex_attempt_artifacts(
            kwargs=dict(kwargs),
            response=response,
        )

    def _fake_build_live_miner_receipt(**kwargs: object) -> dict[str, object]:
        return {
            "tag": kwargs["tag"],
            "status": "verified",
            "assigned_atom_ids": list(kwargs["assigned_atom_ids"]),
            "atom_decisions": [dict(item) for item in kwargs["decisions"]],
            "read_attestations": [],
        }

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.normalize_problem_mining_events",
        lambda **_: [],
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.build_live_miner_receipt",
        _fake_build_live_miner_receipt,
    )

    stage_doc = _run_problem_mining_stage(
        repo_root=tmp_path,
        atoms=[_atom()],
        pipeline_manifest=type(
            "Manifest",
            (),
            {"problem_miner_templates": (template,)},
        )(),
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "problem_records.json",
        out_md=tmp_path / "problem_records.md",
        agent="codex",
        model=None,
        cfg=object(),
        dry_run=False,
        stage_guidance_text="Mine observed problems without proposing fixes.",
        case_registry={"cases": {}, "aliases": {}},
    )

    assert len(prompts) == 2
    assert "INDEPENDENT FULL COVERAGE AND DEPTH REVIEW" not in prompts[0]
    assert "INDEPENDENT FULL COVERAGE AND DEPTH REVIEW" in prompts[1]
    assert "including atoms that the primary pass attached to a problem" in prompts[1]
    miner_result = stage_doc["input_meta"]["miner_results"][0]
    assert miner_result["positive_review_atom_count"] == 1
    assert miner_result["non_support_review_atom_count"] == 0
    assert miner_result["coverage_depth_review_atom_count"] == 1
    receipt = stage_doc["input_meta"]["problem_mining_evidence_draft"]["miners"][0]
    assert receipt["review_scope"] == "all_assigned_atoms_positive_and_non_support"
    assert receipt["non_support_review"]["status"] == "verified"


def test_coverage_review_response_retry_reruns_full_review_and_verifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = tmp_path / "problem_miner_default.md"
    template.write_text(
        "{{STAGE_GUIDANCE}}\nEvidence: {{ATOMS_JSON}}\n",
        encoding="utf-8",
    )
    valid_response = _valid_problem_mining_response()
    invalid_response = (
        r'{"problem_records":[{"problem_id":"problem:one"}],'
        r'"atom_decisions":[{"rationale":"Path .\scripts\run.ps1"}]}'
    )
    calls: list[dict[str, object]] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        calls.append(dict(kwargs))
        tag = str(kwargs["tag"])
        response = (
            invalid_response
            if tag == "problem_mining_001_coverage_depth_review"
            else valid_response
        )
        return _write_fake_codex_attempt_artifacts(
            kwargs=dict(kwargs),
            response=response,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )

    stage_doc = _run_problem_mining_stage(
        repo_root=tmp_path,
        atoms=[_atom()],
        pipeline_manifest=type(
            "Manifest",
            (),
            {"problem_miner_templates": (template,)},
        )(),
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "problem_records.json",
        out_md=tmp_path / "problem_records.md",
        agent="codex",
        model=None,
        cfg=object(),
        dry_run=False,
        stage_guidance_text="Mine observed problems without proposing fixes.",
        case_registry={"cases": {}, "aliases": {}},
    )

    assert [str(call["tag"]) for call in calls] == [
        "problem_mining_001",
        "problem_mining_001_coverage_depth_review",
        "problem_mining_001_coverage_depth_review_format_retry_001",
    ]
    miner_result = stage_doc["input_meta"]["miner_results"][0]
    assert miner_result["status"] == "ok"
    assert miner_result["coverage_depth_review_format_retry_count"] == 1
    review_attempts = miner_result["coverage_depth_review_attempt_history"]
    assert [attempt["status"] for attempt in review_attempts] == [
        "response_contract_failed",
        "verified",
    ]
    assert review_attempts[0]["workspace_dir"] != review_attempts[1]["workspace_dir"]
    assert (
        review_attempts[0]["workspace_manifest_sha256"]
        == review_attempts[1]["workspace_manifest_sha256"]
    )
    receipt = stage_doc["input_meta"]["problem_mining_evidence_draft"]["miners"][0]
    assert receipt["status"] == "verified"
    assert receipt["non_support_review"]["status"] == "verified"
    assert receipt["non_support_review"]["successful_attempt_tag"] == (
        "problem_mining_001_coverage_depth_review_format_retry_001"
    )
    assert len(receipt["non_support_review"]["read_attestations"]) == 1


def test_relation_payload_omits_unrelated_global_case_index_entries() -> None:
    relation_items = [
        {
            "problem_id": f"problem:{index}",
            "case_id": f"case:{index}",
            "title": f"Problem {index}",
            "evidence_atom_ids": [f"atom:{index}"],
        }
        for index in range(5)
    ]
    neighborhoods = [
        {
            "focus_id": "problem:0",
            "most_related_by_semantic": [{"index": 4, "score": 0.8}],
            "most_related_by_evidence_overlap": [],
            "most_related_by_metadata": [],
            "most_related_by_path_anchor": [],
        },
        {
            "focus_id": "problem:1",
            "most_related_by_semantic": [{"index": 2, "score": 0.7}],
            "most_related_by_evidence_overlap": [],
            "most_related_by_metadata": [],
            "most_related_by_path_anchor": [],
        },
    ]

    payload = _relation_review_payload(
        relation_items=relation_items,
        neighborhoods=neighborhoods,
        focus_problem_ids={"problem:0"},
    )

    assert payload["focus_count"] == 1
    assert payload["full_case_index_count"] == 5
    assert payload["case_index_count"] == 2
    assert {item["problem_id"] for item in payload["case_index"]} == {
        "problem:0",
        "problem:4",
    }


def test_failed_relation_batch_keeps_only_that_batch_provisionally_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relation_items = [
        {
            "problem_id": f"problem:{index}",
            "case_id": f"case:{index}",
            "title": f"Problem {index}",
            "evidence_atom_ids": [f"atom:{index}"],
        }
        for index in range(3)
    ]
    neighborhoods = [
        {
            "focus_id": f"problem:{index}",
            "most_related_by_semantic": [],
            "most_related_by_evidence_overlap": [],
            "most_related_by_metadata": [],
            "most_related_by_path_anchor": [],
        }
        for index in range(3)
    ]

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        if str(kwargs["tag"]).endswith("batch_001"):
            raise RuntimeError("reviewer unavailable")
        return json.dumps(
            [
                {
                    "focus_id": "problem:2",
                    "action": "keep_separate",
                    "rationale": "No objective identity edge exists.",
                    "review_confidence": 0.9,
                }
            ]
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )
    review_dir = tmp_path / "relation_review"
    review_dir.mkdir()
    decisions, batches = _run_relation_review_batches(
        relation_items=relation_items,
        neighborhoods=neighborhoods,
        focus_problem_ids=["problem:0", "problem:1", "problem:2"],
        template=("{{STAGE_GUIDANCE}}\n{{ALLOWED_ACTIONS}}\n{{NEIGHBORHOODS_JSON}}"),
        allowed_actions=["merge", "alias", "split", "same_cause_group", "keep_separate"],
        stage_guidance_text="Keep uncertain cases independent.",
        review_dir=review_dir,
        tag="problem_mining_relation_review_001",
        agent="codex",
        model=None,
        cfg=object(),
        max_foci=2,
    )

    assert len(decisions) == 3
    assert [batch["status"] for batch in batches] == [
        "failed_provisional_keep_separate",
        "completed",
    ]
    for decision in decisions[:2]:
        assert decision["action"] == "keep_separate"
        assert decision["provisional_relation_suggestion"]["kind"] == (
            "relation_review_batch_failure"
        )
    assert decisions[2]["review_confidence"] == 0.9
    checkpoint = json.loads(
        (review_dir / "problem_mining_relation_review_001.response.txt").read_text(encoding="utf-8")
    )
    assert checkpoint == decisions


def test_verified_relation_edges_require_hash_bound_runner_receipt(tmp_path: Path) -> None:
    response_path = tmp_path / "relation.response.txt"
    response_path.write_text("[]\n", encoding="utf-8")
    _receipt, refs = write_case_relation_receipt(
        tmp_path / "relations.json",
        stage="problem_mining",
        relation_review_response_path=response_path,
        relations=[
            {
                "source_case_id": "case:source",
                "target_case_id": "case:target",
                "direction": "source_to_canonical",
                "relation_kind": "canonical_absorption",
                "decision_actions": ["alias"],
            }
        ],
    )
    registry = {
        "cases": {
            "case:source": {
                "case_id": "case:source",
                "relation_receipt": refs["case:source"],
            },
            "case:target": {
                "case_id": "case:target",
                "incoming_relation_receipts": [refs["case:source"]],
            },
        }
    }

    assert _verified_relation_edges_from_case_registry(registry) == {("case:source", "case:target")}

    registry["cases"]["case:source"]["relation_receipt"] = {
        **refs["case:source"],
        "receipt_sha256": "0" * 64,
    }
    registry["cases"]["case:target"]["incoming_relation_receipts"] = []
    assert _verified_relation_edges_from_case_registry(registry) == set()


def test_disagreeing_non_support_reviews_remain_unresolved() -> None:
    records, decisions = _reconcile_problem_mining_reviews(
        primary_records=[],
        primary_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "duplicate",
                "problem_ids": [],
                "rationale": "The primary pass believed another atom covered it.",
                "revisit_when": None,
            }
        ],
        review_records=[],
        review_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "expected_noise",
                "problem_ids": [],
                "rationale": "The second pass believed it was routine output.",
                "revisit_when": None,
            }
        ],
    )

    assert records == []
    assert decisions[0]["disposition"] == "unresolved"


def test_receipt_revalidation_detects_retained_read_tampering(tmp_path: Path) -> None:
    stage1, atoms = _verified_stage1(tmp_path)
    receipt_path = Path(stage1["artifacts"]["problem_mining_evidence_receipt"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    atom_file = (
        Path(receipt["miners"][0]["workspace_dir"])
        / receipt["miners"][0]["read_attestations"][0]["atom_file"]
    )
    atom_file.write_text("tampered\n", encoding="utf-8")

    errors = verify_problem_mining_evidence_receipt(
        stage1=stage1,
        atoms=atoms,
        require_live=True,
    )

    assert any(error.startswith("problem_mining_read_attestation_changed") for error in errors)


def test_receipt_revalidation_detects_required_index_tampering(tmp_path: Path) -> None:
    stage1, atoms = _verified_stage1(tmp_path)
    receipt_path = Path(stage1["artifacts"]["problem_mining_evidence_receipt"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    workspace = Path(receipt["miners"][0]["workspace_dir"])
    (workspace / "atoms_index.md").write_text("tampered index\n", encoding="utf-8")

    errors = verify_problem_mining_evidence_receipt(
        stage1=stage1,
        atoms=atoms,
        require_live=True,
    )

    assert any(
        error.startswith("problem_mining_required_read_attestation_changed") for error in errors
    )


def test_derived_evidence_is_never_counted_as_source_coverage() -> None:
    source = _atom("atom:source")
    derived = _atom("atom:derived", role="research")

    draft = build_problem_mining_evidence_draft(
        atoms=[source, derived],
        eligible_atoms=[source, derived],
        mode="live",
    )

    assert draft["eligible_source_atom_ids"] == ["atom:source"]
    assert draft["eligible_derived_atom_ids"] == ["atom:derived"]


def test_dry_run_receipt_never_claims_full_reads_or_export_eligibility(tmp_path: Path) -> None:
    atom = _atom()
    draft = build_problem_mining_evidence_draft(atoms=[atom], eligible_atoms=[atom], mode="dry_run")
    draft["miners"] = [
        build_dry_run_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            records=[],
        )
    ]
    partitioned = apply_problem_mining_decision_partition(
        atoms=[atom], canonical_records=[], draft=draft
    )
    receipt_path = tmp_path / "dry.json"
    receipt = finalize_problem_mining_evidence_receipt(
        draft=draft,
        atoms=partitioned,
        receipt_path=receipt_path,
    )
    stage1 = {
        "items": [],
        "input_meta": {
            "problem_mining_evidence_receipt": problem_mining_evidence_receipt_ref(
                receipt=receipt,
                receipt_path=receipt_path,
            )
        },
        "artifacts": {"problem_mining_evidence_receipt": str(receipt_path)},
    }

    errors = verify_problem_mining_evidence_receipt(
        stage1=stage1,
        atoms=partitioned,
        require_live=True,
    )

    assert receipt["eligible_for_shadow_export"] is False
    assert receipt["miners"][0]["read_attestations"] == []
    assert "problem_mining_evidence_receipt_not_live_verified" in errors


def test_failed_mining_job_preserves_partition_but_keeps_shadow_closed(tmp_path: Path) -> None:
    atom = _atom()
    draft = build_problem_mining_evidence_draft(atoms=[atom], eligible_atoms=[atom], mode="live")
    workspace = tmp_path / "failed_workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    draft["miners"] = [
        build_failed_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            workspace_dir=workspace,
            workspace_manifest=manifest,
            error="ValueError: malformed model response",
        )
    ]
    partitioned = apply_problem_mining_decision_partition(
        atoms=[atom], canonical_records=[], draft=draft
    )
    receipt_path = tmp_path / "partial.json"
    receipt = finalize_problem_mining_evidence_receipt(
        draft=draft,
        atoms=partitioned,
        receipt_path=receipt_path,
    )
    stage1 = {
        "items": [],
        "input_meta": {
            "problem_mining_evidence_receipt": problem_mining_evidence_receipt_ref(
                receipt=receipt,
                receipt_path=receipt_path,
            )
        },
        "artifacts": {"problem_mining_evidence_receipt": str(receipt_path)},
    }

    errors = verify_problem_mining_evidence_receipt(
        stage1=stage1,
        atoms=partitioned,
        require_live=True,
    )

    assert partitioned[0]["disposition"] == "unresolved"
    assert partitioned[0]["disposition_status"] == "decided"
    assert receipt["status"] == "partial_failed_jobs"
    assert receipt["eligible_for_shadow_export"] is False
    assert any("problem_mining_miner_not_verified" in error for error in errors)
