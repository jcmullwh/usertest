from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_adapters import (
    CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS,
    CodexLoginStatusResult,
)
from backlog_core import build_operational_failure_candidates, extract_backlog_atoms
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
from backlog_miner.pipeline import StagePromptRun, _write_model_invocation_manifest
from backlog_repo import write_case_relation_receipt

from usertest_backlog.workflows.problem_mining import (
    _atoms_for_problem_mining_prompt,
    _cross_job_leaf_routing_nodes,
    _failed_relation_review_batch_count,
    _partition_problem_mining_chunks,
    _preserve_primary_after_coverage_review_failure,
    _problem_mining_attempt_manifest_sha256,
    _problem_mining_job_batches,
    _problem_mining_jobs_with_terminal_context,
    _problem_mining_routing_decision_errors,
    _recall_bearing_cross_job_groups,
    _reconcile_problem_mining_reviews,
    _relation_decision_item_errors,
    _relation_review_payload,
    _run_cross_job_problem_synthesis,
    _run_independently_reviewed_problem_pass,
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
    _miner_receipt_errors,
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
    include_origin_attachments: bool = True,
) -> None:
    relative_paths = [
        "atoms.json",
        str(manifest["index_file"]),
    ]
    if include_chunks:
        relative_paths.extend(
            str(chunk["text_file"]) for chunk in manifest["chunks"] if isinstance(chunk, dict)
        )
    origin_manifest = manifest.get("origin_attachment_evidence")
    if include_origin_attachments and isinstance(origin_manifest, dict):
        workspace_atom_ids = [
            str(atom_id)
            for key in ("assigned_atom_ids", "context_atom_ids")
            for atom_id in manifest.get(key, [])
            if isinstance(atom_id, str)
        ]
        relative_paths.extend(
            str(requirement["file"])
            for requirement in origin_attachment_requirements(
                origin_manifest,
                atom_ids=workspace_atom_ids,
            )
        )
    relative_paths = list(dict.fromkeys(relative_paths))
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
    session_available: bool = True,
) -> str | StagePromptRun:
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
    origin_summary = manifest.get("origin_attachment_evidence")
    if isinstance(origin_summary, dict):
        origin_manifest_file = origin_summary.get("manifest_file")
        if isinstance(origin_manifest_file, str):
            origin_manifest = json.loads(
                (workspace / origin_manifest_file).read_text(encoding="utf-8")
            )
            assigned_atom_ids = [
                str(atom_id)
                for key in ("assigned_atom_ids", "context_atom_ids")
                for atom_id in manifest.get(key, [])
                if isinstance(atom_id, str)
            ]
            relative_paths.extend(
                str(requirement["file"])
                for requirement in origin_attachment_requirements(
                    origin_manifest,
                    atom_ids=assigned_atom_ids,
                )
            )
    relative_paths = list(dict.fromkeys(relative_paths))
    session_id = (
        str(
            kwargs.get("resume_session_id")
            or "019f2cca-9011-7e32-88ae-6c25af578b49"
        )
        if session_available
        else None
    )
    events = (
        [{"type": "thread.started", "thread_id": session_id}]
        if session_id is not None
        else []
    )
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
        agent_session_id=session_id,
        resumed_from_session_id=(
            str(kwargs["resume_session_id"])
            if kwargs.get("resume_session_id") is not None
            else None
        ),
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
        error_kind=None if session_id is not None else "NoAuthorSession",
        agent_session_id=session_id,
        resumed_from_session_id=(
            str(kwargs["resume_session_id"])
            if kwargs.get("resume_session_id") is not None
            else None
        ),
        workspace_dir=workspace,
    )
    if kwargs.get("structured") is True:
        return StagePromptRun(
            response=response,
            agent_session_id=session_id,
            resumed_from_session_id=(
                str(kwargs["resume_session_id"])
                if kwargs.get("resume_session_id") is not None
                else None
            ),
            workspace_dir=workspace,
            invocation_manifest_path=out_dir / f"{tag}.model_invocation.json",
            prompt_path=out_dir / f"{tag}.prompt.txt",
            response_path=out_dir / f"{tag}.response.txt",
            raw_events_path=raw_events_path,
            last_message_path=last_message_path,
            stderr_path=stderr_path,
            elapsed_seconds=0.0,
        )
    return response


def _write_fake_relation_stage_run(
    *,
    kwargs: dict[str, object],
    response: str,
    session_available: bool = True,
) -> StagePromptRun:
    out_dir = Path(str(kwargs["out_dir"]))
    tag = str(kwargs["tag"])
    workspace = Path(str(kwargs["workspace_dir"]))
    session_id = (
        str(
            kwargs.get("resume_session_id")
            or "019f2cca-9011-7e32-88ae-6c25af578b49"
        )
        if session_available
        else None
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / f"{tag}.prompt.txt"
    response_path = out_dir / f"{tag}.response.txt"
    raw_events_path = out_dir / f"{tag}.raw_events.jsonl"
    last_message_path = out_dir / f"{tag}.last_message.txt"
    stderr_path = out_dir / f"{tag}.stderr.txt"
    invocation_path = out_dir / f"{tag}.model_invocation.json"
    prompt_path.write_text(str(kwargs["prompt"]), encoding="utf-8")
    response_path.write_text(response, encoding="utf-8")
    raw_events_path.write_text(
        (
            json.dumps({"type": "thread.started", "thread_id": session_id}) + "\n"
            if session_id is not None
            else ""
        ),
        encoding="utf-8",
    )
    last_message_path.write_text(response, encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    invocation_path.write_text("{}\n", encoding="utf-8")
    return StagePromptRun(
        response=response,
        agent_session_id=session_id,
        resumed_from_session_id=(
            str(kwargs["resume_session_id"])
            if kwargs.get("resume_session_id") is not None
            else None
        ),
        workspace_dir=workspace,
        invocation_manifest_path=invocation_path,
        prompt_path=prompt_path,
        response_path=response_path,
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
        elapsed_seconds=0.0,
    )


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


def test_problem_miner_prompt_requires_context_attachment_reads() -> None:
    prompt_path = (
        Path(__file__).resolve().parents[3]
        / "configs"
        / "backlog_prompts"
        / "problem_miner_default.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")

    assert "assigned or context" in prompt
    assert "Context attachments are required interpretation evidence" in prompt
    assert "cannot receive decisions or citations" in prompt


def test_primary_response_correction_resumes_same_session_and_retains_first_attempt(
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
        "problem_mining_001_correction_001",
    ]
    attempts = result["attempt_history"]
    assert [attempt["status"] for attempt in attempts] == [
        "response_contract_failed",
        "verified",
    ]
    assert attempts[0]["workspace_dir"] == attempts[1]["workspace_dir"]
    assert (
        attempts[0]["workspace_manifest_sha256"]
        == attempts[1]["workspace_manifest_sha256"]
        == _problem_mining_attempt_manifest_sha256(initial_manifest)
    )
    assert (
        attempts[0]["artifacts"]["response"]["sha256"]
        == sha256(invalid_response.encode("utf-8")).hexdigest()
    )
    assert result["receipt"]["successful_attempt_tag"] == ("problem_mining_001_correction_001")
    assert result["receipt"]["workspace_dir"] == attempts[1]["workspace_dir"]
    assert len(result["receipt"]["read_attestations"]) == 1
    retry_prompt = Path(attempts[1]["artifacts"]["prompt"]["path"]).read_text(encoding="utf-8")
    assert "SAME-AUTHOR RESPONSE CORRECTION" in retry_prompt
    assert "immediately prior complete response" in retry_prompt
    assert "Original assignment prompt SHA-256" in retry_prompt
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


def test_primary_response_correction_reuses_content_bound_first_attempt_reads(
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
    assert result["failure"] is None
    assert [attempt["status"] for attempt in result["attempt_history"]] == [
        "response_contract_failed",
        "verified",
    ]
    assert result["receipt"]["read_attestations"]
    assert calls[1]["workspace_dir"] == calls[0]["workspace_dir"]
    assert calls[1]["resume_session_id"] == result["agent_session_id"]


def test_primary_mining_retries_fresh_until_codex_author_session_exists(
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

    def fake_run(**kwargs: object) -> str | StagePromptRun:
        calls.append(dict(kwargs))
        return _write_fake_codex_attempt_artifacts(
            kwargs=dict(kwargs),
            response="" if len(calls) == 1 else _valid_problem_mining_response(),
            session_available=len(calls) > 1,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        fake_run,
    )
    result = _run_problem_mining_job_with_response_retry(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="problem_mining_001",
        prompt="Read all assigned evidence and return the strict response.",
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
    assert len(calls) == 2
    assert calls[0]["resume_session_id"] is None
    assert calls[1]["resume_session_id"] is None
    assert calls[1]["prompt"] == calls[0]["prompt"]
    assert result["correction_status"] == "accepted"
    assert result["correction_metrics"]["attempt_count"] == 2
    assert result["correction_metrics"]["session_acquisition_retry_count"] == 1
    assert [attempt["attempt_tag"] for attempt in result["attempt_history"]] == [
        "problem_mining_001",
        "problem_mining_001_session_acquisition_001",
    ]
    assert result["attempt_history"][0]["agent_session_id"] is None
    assert result["attempt_history"][1]["agent_session_id"] is not None


def test_primary_mining_transient_exact_session_exception_retries_same_author(
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
    invalid = (
        r'{"problem_records":[{"problem_id":"problem:one"}],'
        r'"atom_decisions":[{"rationale":"Path .\scripts\run.ps1"}]}'
    )
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> str | StagePromptRun:
        calls.append(dict(kwargs))
        if len(calls) == 2:
            raise RuntimeError("transient resumed transport loss")
        return _write_fake_codex_attempt_artifacts(
            kwargs=dict(kwargs),
            response=invalid if len(calls) == 1 else _valid_problem_mining_response(),
            read_chunks=len(calls) == 1,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        fake_run,
    )
    result = _run_problem_mining_job_with_response_retry(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="problem_mining_001",
        prompt="Read all assigned evidence and return the strict response.",
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

    session_id = result["agent_session_id"]
    assert result["failure"] is None
    assert len(calls) == 3
    assert calls[1]["resume_session_id"] == session_id
    assert calls[2]["resume_session_id"] == session_id
    assert result["correction_metrics"]["correction_invocation_failure_count"] == 1
    assert [attempt["status"] for attempt in result["attempt_history"]] == [
        "response_contract_failed",
        "invocation_failed",
        "verified",
    ]


def test_response_contract_correction_stalls_only_after_repeated_exact_noop(
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

    assert len(calls) == 3
    assert isinstance(result["failure"], ProblemMiningResponseContractError)
    assert len(result["attempt_history"]) == 3
    assert all(
        attempt["status"] == "response_contract_failed" for attempt in result["attempt_history"]
    )
    assert result["correction_status"] == "stalled:same_state_repeated_after_feedback"
    second_feedback_prompt = Path(
        result["attempt_history"][2]["artifacts"]["prompt"]["path"]
    ).read_text(encoding="utf-8")
    assert "first_noop_receives_feedback" in second_feedback_prompt


def test_problem_mining_correction_counts_two_old_errors_to_one_new_as_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import usertest_backlog.workflows.problem_mining as problem_mining_module

    atom = _atom()
    prompt_atoms = _atoms_for_problem_mining_prompt([atom])
    initial_workspace = tmp_path / "artifacts" / "problem_mining" / "job" / "workspace"
    initial_manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=initial_workspace,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
        source_root=tmp_path,
    )
    original_parser = problem_mining_module.parse_problem_record_list

    def fake_parser(text: str, *args: object, **kwargs: object):
        raw = json.loads(text)
        title = raw[0].get("title") if isinstance(raw, list) and raw else None
        if title == "phase-two-errors":
            return [], ["old:a", "old:b"]
        if title == "phase-one-new-error":
            return [], ["new:c"]
        return original_parser(text, *args, **kwargs)

    monkeypatch.setattr(problem_mining_module, "parse_problem_record_list", fake_parser)
    phase_responses: list[str] = []
    for title in ("phase-two-errors", "phase-one-new-error"):
        record = _problem()
        record.pop("case_id", None)
        record["title"] = title
        phase_responses.append(
            json.dumps(
                {
                    "problem_records": [record],
                    "atom_decisions": [
                        {
                            "atom_id": "atom:one",
                            "disposition": "supports_case",
                            "problem_ids": ["problem:one"],
                            "rationale": "The atom records the failure.",
                            "revisit_when": None,
                        }
                    ],
                }
            )
        )
    phase_responses.append(_valid_problem_mining_response())
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> str | StagePromptRun:
        calls.append(dict(kwargs))
        return _write_fake_codex_attempt_artifacts(
            kwargs=dict(kwargs),
            response=phase_responses[len(calls) - 1],
        )

    monkeypatch.setattr(problem_mining_module, "run_stage_prompt_json", fake_run)
    result = _run_problem_mining_job_with_response_retry(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="problem_mining_001",
        prompt="Read the assigned evidence and return the strict envelope.",
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=["atom:one"],
        max_records_per_miner=20,
        eligible_atom_ids=["atom:one"],
        template_name="problem_miner_default.md",
        record_contract_error_prefix="problem_record_invalid",
        agent="codex",
        model=None,
        cfg=object(),
        initial_workspace_dir=initial_workspace,
        initial_manifest=initial_manifest,
    )

    assert result["failure"] is None
    assert len(calls) == 3
    first_progress = result["attempt_history"][1]["correction_progress"]
    assert first_progress["before_error_count"] == 2
    assert first_progress["after_error_count"] == 1
    assert first_progress["reason"] == "error_count_decreased"
    assert first_progress["introduced_error_identities"] == ["problem_record_invalid:new:c"]
    assert all("response" in attempt["artifacts"] for attempt in result["attempt_history"])


def test_cross_job_routing_repairs_same_author_without_promoting_carriers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atoms = [_atom("routing:one"), _atom("routing:two")]
    prompt_atoms = _atoms_for_problem_mining_prompt(atoms)
    initial_workspace = tmp_path / "artifacts" / "problem_mining" / "routing" / "workspace"
    initial_manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=initial_workspace,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=0,
        assigned_atom_ids=["routing:one", "routing:two"],
        source_root=tmp_path,
    )
    invalid_record = _problem("routing:one")
    invalid_record.pop("case_id", None)
    invalid_response = json.dumps(
        {
            "problem_records": [invalid_record],
            "atom_decisions": [
                {
                    "atom_id": "routing:one",
                    "disposition": "supports_case",
                    "problem_ids": ["problem:one"],
                    "rationale": "The old contract incorrectly promoted a routing carrier.",
                    "revisit_when": None,
                    "routing_keys": ["command-failure", "execution-boundary"],
                },
                {
                    "atom_id": "routing:two",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "This carrier remains neutral but omitted its keys.",
                    "revisit_when": None,
                },
            ],
        }
    )
    corrected_response = json.dumps(
        {
            "problem_records": [],
            "atom_decisions": [
                {
                    "atom_id": atom_id,
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "Neutral carrier; exact evidence decides problem support.",
                    "revisit_when": None,
                    "routing_keys": ["command-failure", f"route-{atom_id[-3:]}"],
                }
                for atom_id in ("routing:one", "routing:two")
            ],
        }
    )
    calls: list[dict[str, object]] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str | StagePromptRun:
        calls.append(dict(kwargs))
        return _write_fake_codex_attempt_artifacts(
            kwargs=dict(kwargs),
            response=invalid_response if len(calls) == 1 else corrected_response,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )
    result = _run_problem_mining_job_with_response_retry(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="cross_job_routing_l01_b001",
        prompt="Assign neutral semantic routing keys.",
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=["routing:one", "routing:two"],
        max_records_per_miner=0,
        eligible_atom_ids=["routing:one", "routing:two"],
        template_name="cross_job_routing",
        record_contract_error_prefix="cross_job_problem_record_contract_invalid",
        agent="codex",
        model=None,
        cfg=object(),
        initial_workspace_dir=initial_workspace,
        initial_manifest=initial_manifest,
    )

    assert result["failure"] is None
    assert result["correction_status"] == "corrected"
    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == "019f2cca-9011-7e32-88ae-6c25af578b49"
    correction_prompt = str(calls[1]["prompt"])
    assert "problem_mining_routing_records_not_empty" in correction_prompt
    assert "problem_mining_routing_decision_not_neutral" in correction_prompt
    assert "problem_mining_routing_decision_keys_invalid" in correction_prompt
    assert result["attempt_history"][0]["valid_item_keys"] == []
    assert result["records"] == []
    assert {decision["disposition"] for decision in result["receipt"]["atom_decisions"]} == {
        "unresolved"
    }
    assert all(
        2 <= len(decision["routing_keys"]) <= 5 for decision in result["receipt"]["atom_decisions"]
    )


def test_proposal_only_problem_returns_to_same_author_for_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _atom("proposal:one")
    proposal["source"] = "suggested_change"
    proposal["evidence_class"] = "proposal"
    prompt_atoms = _atoms_for_problem_mining_prompt([proposal])
    workspace = tmp_path / "artifacts" / "problem_mining" / "proposal" / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=0,
        assigned_atom_ids=["proposal:one"],
        source_root=tmp_path,
    )
    proposed_record = _problem("proposal:one")
    proposed_record.pop("case_id", None)
    initial_response = json.dumps(
        {
            "problem_records": [proposed_record],
            "atom_decisions": [
                {
                    "atom_id": "proposal:one",
                    "disposition": "supports_case",
                    "problem_ids": ["problem:one"],
                    "rationale": "The requested change implies a problem.",
                    "revisit_when": None,
                }
            ],
        }
    )
    corrected_response = json.dumps(
        {
            "problem_records": [],
            "atom_decisions": [
                {
                    "atom_id": "proposal:one",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "A proposal alone does not establish observed harm.",
                    "revisit_when": None,
                }
            ],
        }
    )
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> str | StagePromptRun:
        calls.append(dict(kwargs))
        return _write_fake_codex_attempt_artifacts(
            kwargs=dict(kwargs),
            response=initial_response if len(calls) == 1 else corrected_response,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        fake_run,
    )
    result = _run_problem_mining_job_with_response_retry(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="problem_mining_001",
        prompt="Mine observed problems from the assigned evidence.",
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=["proposal:one"],
        max_records_per_miner=0,
        eligible_atom_ids=["proposal:one"],
        template_name="problem_miner_default.md",
        record_contract_error_prefix="problem_record_invalid",
        agent="codex",
        model=None,
        cfg=object(),
        initial_workspace_dir=workspace,
        initial_manifest=manifest,
    )

    assert result["failure"] is None
    assert result["correction_status"] == "corrected"
    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == "019f2cca-9011-7e32-88ae-6c25af578b49"
    assert "problem_mining_proposal_only_record:problem:one" in str(calls[1]["prompt"])
    assert "problem_record:problem:one" not in result["attempt_history"][0][
        "valid_item_keys"
    ]
    assert result["records"] == []


def test_cross_partition_problem_id_conflict_repairs_author_then_reviewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_atoms = _atoms_for_problem_mining_prompt([_atom("atom:one")])
    canonical = _problem("atom:other")
    canonical.pop("case_id", None)
    canonical.update(
        {
            "title": "Canonical command execution failure",
            "problem": "The command executor aborts before the requested workflow starts.",
            "user_impact": "Users cannot begin the requested workflow.",
            "severity": "high",
        }
    )
    candidate = _problem("atom:one")
    candidate.pop("case_id", None)
    candidate.update(
        {
            "title": "Command startup is confusing",
            "problem": "The startup path appears unclear.",
            "user_impact": "Users may hesitate before starting.",
            "severity": "low",
        }
    )
    corrected = dict(candidate)
    for field in ("title", "problem", "user_impact", "severity"):
        corrected[field] = canonical[field]
    partially_corrected = dict(corrected)
    partially_corrected["severity"] = candidate["severity"]

    def response(record: dict[str, object]) -> str:
        return json.dumps(
            {
                "problem_records": [record],
                "atom_decisions": [
                    {
                        "atom_id": "atom:one",
                        "disposition": "supports_case",
                        "problem_ids": ["problem:one"],
                        "rationale": "The exact assigned atom establishes this problem.",
                        "revisit_when": None,
                    }
                ],
            }
        )

    calls: list[dict[str, object]] = []
    correction_round = 0
    current_author_record = candidate

    def fake_run(**kwargs: object) -> str | StagePromptRun:
        nonlocal correction_round, current_author_record
        calls.append(dict(kwargs))
        prompt = str(kwargs["prompt"])
        if "SAME-AUTHOR RESPONSE CORRECTION" in prompt:
            correction_round += 1
            current_author_record = (
                partially_corrected if correction_round == 1 else corrected
            )
        elif "INDEPENDENT CROSS-JOB REVIEW" not in prompt:
            current_author_record = candidate
        return _write_fake_codex_attempt_artifacts(
            kwargs=dict(kwargs),
            response=response(current_author_record),
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        fake_run,
    )
    result = _run_independently_reviewed_problem_pass(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="problem_mining_002",
        prompt="Read the complete evidence and identify observed problems.",
        prompt_atoms=prompt_atoms,
        agent="codex",
        model=None,
        cfg=object(),
        template_name="problem_miner_default.md",
        canonical_records=[canonical],
    )

    assert len(calls) == 4
    assert "SAME-AUTHOR RESPONSE CORRECTION" in str(calls[1]["prompt"])
    assert "problem_mining_conflicting_problem_id" in str(calls[1]["prompt"])
    assert calls[1]["resume_session_id"] == "019f2cca-9011-7e32-88ae-6c25af578b49"
    assert calls[2]["resume_session_id"] == "019f2cca-9011-7e32-88ae-6c25af578b49"
    assert calls[3]["resume_session_id"] is None
    assert result["records"][0]["title"] == canonical["title"]
    assert result["records"][0]["evidence_atom_ids"] == ["atom:one"]
    attempts = result["receipt"]["primary_pass"]["attempt_history"]
    assert attempts[1]["correction_progress"]["after_error_count"] == 1
    assert attempts[1]["correction_progress"]["reason"] == "error_count_decreased"
    assert attempts[2]["correction_progress"]["after_error_count"] == 0


def test_cross_job_routing_repairs_exact_independent_reviewer_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atom_ids = ("routing:one", "routing:two")
    prompt_atoms = _atoms_for_problem_mining_prompt([_atom(atom_id) for atom_id in atom_ids])
    primary_session = "019f2cca-9011-7e32-88ae-6c25af578b49"
    reviewer_session = "019f2cca-9011-7e32-88ae-6c25af578b50"

    def _neutral_response(*, key_prefix: str, include_keys: bool) -> str:
        return json.dumps(
            {
                "problem_records": [],
                "atom_decisions": [
                    {
                        "atom_id": atom_id,
                        "disposition": "unresolved",
                        "problem_ids": [],
                        "rationale": "Neutral carrier; exact evidence decides problem support.",
                        "revisit_when": None,
                        **(
                            {
                                "routing_keys": [
                                    f"{key_prefix}-mechanism",
                                    f"route-{atom_id[-3:]}",
                                ]
                            }
                            if include_keys
                            else {}
                        ),
                    }
                    for atom_id in atom_ids
                ],
            }
        )

    calls: list[dict[str, object]] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str | StagePromptRun:
        calls.append(dict(kwargs))
        tag = str(kwargs["tag"])
        resumed = kwargs.get("resume_session_id")
        if "independent_review" not in tag:
            response = _neutral_response(key_prefix="primary", include_keys=True)
            session_id = primary_session
        elif resumed is None:
            response = _neutral_response(key_prefix="review", include_keys=False)
            session_id = reviewer_session
        else:
            response = _neutral_response(key_prefix="review", include_keys=True)
            session_id = reviewer_session
        helper_kwargs = dict(kwargs)
        helper_kwargs["resume_session_id"] = session_id
        return _write_fake_codex_attempt_artifacts(
            kwargs=helper_kwargs,
            response=response,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )
    result = _run_independently_reviewed_problem_pass(
        repo_root=tmp_path,
        stage_artifacts_dir=tmp_path / "artifacts" / "problem_mining",
        base_tag="cross_job_routing_l01_b001",
        prompt="Assign neutral semantic routing keys.",
        prompt_atoms=prompt_atoms,
        agent="codex",
        model=None,
        cfg=object(),
        template_name="cross_job_routing",
    )

    assert len(calls) == 3
    assert calls[1].get("resume_session_id") is None
    assert calls[2]["resume_session_id"] == reviewer_session
    assert result["records"] == []
    assert result["receipt"]["non_support_review"]["correction_status"] == "corrected"
    assert all(
        decision["routing_keys"][0] == "review-mechanism" for decision in result["decisions"]
    )


def _empty_problem_mining_response(kwargs: dict[str, object]) -> str | StagePromptRun:
    """Model a transport-valid empty author turn that remains same-session repairable."""

    return _write_fake_codex_attempt_artifacts(kwargs=kwargs, response="")


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
            return _empty_problem_mining_response(dict(kwargs))
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


def test_empty_response_correction_retains_attempts_until_exact_noop_stall(
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
        return _empty_problem_mining_response(dict(kwargs))

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

    assert len(calls) == 3
    assert isinstance(result["failure"], ProblemMiningResponseContractError)
    attempts = result["attempt_history"]
    assert [attempt["status"] for attempt in attempts] == [
        "response_contract_failed",
        "response_contract_failed",
        "response_contract_failed",
    ]
    assert all(attempt["artifacts"]["response"]["bytes"] == 0 for attempt in attempts)
    assert result["correction_status"] == "stalled:same_state_repeated_after_feedback"
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
            return _empty_problem_mining_response(dict(kwargs))
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
    required_paths.extend(
        str(requirement["file"])
        for requirement in origin_attachment_requirements(
            manifest["origin_attachment_evidence"],
            atom_ids=["atom:one", "atom:two"],
        )
    )
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
        routing_keys_by_atom: dict[str, list[str]] = {}
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
                routing_keys_by_atom[route_id] = routing_keys
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
                **(
                    {"routing_keys": routing_keys_by_atom[atom_id]}
                    if atom_id in routing_keys_by_atom
                    else {}
                ),
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
        if expected_related_ids <= set(synthesis["candidate_atom_ids"])
    )
    assert exact_assignments
    assert all(len(assignment) <= 100 for assignment in exact_assignments)
    assert all(total_bytes <= 150_000 for total_bytes in exact_assignment_bytes)
    exact_workspace = Path(exact["receipt"]["workspace_dir"])
    exact_manifest = json.loads((exact_workspace / "atoms.json").read_text(encoding="utf-8"))
    assert exact_manifest["assigned_atom_count"] == len(exact["candidate_atom_ids"])
    assert any(
        expected_related_ids <= set(group)
        for group in exact["source_candidate_groups"]
    )
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
            records = []
            decisions = [
                {
                    "atom_id": atom_id,
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": (
                        "This leaf alone remains inconclusive while its semantic keys permit "
                        "bounded exact cross-job comparison."
                    ),
                    "revisit_when": None,
                    "routing_keys": [
                        "lifecycle-completion-classification",
                        "terminal-report-absence",
                    ],
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
        ["duplicate", "duplicate"],
        ["one", "two", "three", "four", "five", "six"],
        ["a" * 81, "valid-key"],
    ],
)
def test_routing_decision_key_contract_reports_noncanonical_bounds(
    routing_keys: list[str],
) -> None:
    assert _problem_mining_routing_decision_errors(
        {
            "problem_records": [],
            "atom_decisions": [
                {
                    "atom_id": "routing:one",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "Neutral routing carrier.",
                    "revisit_when": None,
                    "routing_keys": routing_keys,
                }
            ],
        },
        assigned_atom_ids=["routing:one"],
        tag="routing_test",
    ) == ["problem_mining_routing_decision_keys_invalid:routing_test:routing:one"]


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
            response = json.dumps(
                {
                    "problem_records": [],
                    "atom_decisions": [
                        {
                            "atom_id": atom_id,
                            "disposition": "unresolved",
                            "problem_ids": [],
                            "rationale": "The neutral carrier receives deliberately broad keys.",
                            "revisit_when": None,
                            "routing_keys": [
                                "shared-test-mechanism",
                                "shared-test-boundary",
                            ],
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


def test_overlapping_bounded_keys_pack_reads_but_preserve_independent_cases(
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
            records = [
                {
                    "problem_id": "problem:ab",
                    "title": "A and B establish one exact shared mechanism",
                    "problem": "The exact pair establishes a shared causal failure.",
                    "user_impact": "The causal failure can recur across source jobs.",
                    "severity": "high",
                    "confidence": 0.95,
                    "evidence_atom_ids": ["atom:a", "atom:b"],
                    "evidence_summary": "Both full atoms directly establish this mechanism.",
                    "problem_status": "identified",
                },
                {
                    "problem_id": "problem:ac",
                    "title": "A and C establish a different exact shared mechanism",
                    "problem": "The exact pair establishes a shared causal failure.",
                    "user_impact": "The causal failure can recur across source jobs.",
                    "severity": "high",
                    "confidence": 0.95,
                    "evidence_atom_ids": ["atom:a", "atom:c"],
                    "evidence_summary": "Both full atoms directly establish this mechanism.",
                    "problem_status": "identified",
                },
            ]
            decisions = [
                {
                    "atom_id": atom_id,
                    "disposition": "supports_case",
                    "problem_ids": (
                        ["problem:ab", "problem:ac"]
                        if atom_id == "atom:a"
                        else ["problem:ab"]
                        if atom_id == "atom:b"
                        else ["problem:ac"]
                    ),
                    "rationale": "The packed exact evidence establishes the retained cases.",
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
            records = []
            decisions = [
                {
                    "atom_id": route_id,
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "The neutral routing carrier received semantic keys.",
                    "revisit_when": None,
                    "routing_keys": routing_keys_by_origin[origin_by_route[route_id]],
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
    assert result["recall_candidate_groups"] == expected_groups
    assert result["candidate_groups"] == [["atom:a", "atom:b", "atom:c"]]
    assert set(exact_assignments) == {frozenset({"atom:a", "atom:b", "atom:c"})}
    assert {record["problem_id"] for record in result["records"]} == {
        "problem:ab",
        "problem:ac",
    }
    overrides = {override["atom_id"]: override for override in result["decision_overrides"]}
    assert overrides["atom:a"]["problem_ids"] == ["problem:ab", "problem:ac"]
    assert [
        provenance["problem_ids"]
        for provenance in overrides["atom:a"]["exact_synthesis_provenance"]
    ] == [["problem:ab", "problem:ac"]]
    assert overrides["atom:b"]["problem_ids"] == ["problem:ab"]
    assert overrides["atom:c"]["problem_ids"] == ["problem:ac"]
    assert all(len(exact["decision_overrides"]) == 3 for exact in result["exact_syntheses"])

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


def test_cross_job_exact_groups_only_cover_recall_bearing_signals() -> None:
    atoms = {
        atom_id: _atoms_for_problem_mining_prompt([_atom(atom_id)])[0]
        for atom_id in ("atom:a", "atom:b", "atom:c", "atom:d")
    }
    signals = [
        {
            "disposition": "candidate",
            "member_atom_ids": ["atom:a", "atom:b"],
        },
        {
            "disposition": "candidate",
            "member_atom_ids": ["atom:b", "atom:c"],
        },
        {
            "disposition": "candidate",
            "member_atom_ids": ["atom:c", "atom:d"],
        },
    ]
    recall, packed = _recall_bearing_cross_job_groups(
        routing_signals=signals,
        original_disposition_by_atom={
            "atom:a": "supports_case",
            "atom:b": "supports_case",
            "atom:c": "unresolved",
            "atom:d": "supports_case",
        },
        exact_atoms_by_id=atoms,
    )

    assert recall == [["atom:b", "atom:c"], ["atom:c", "atom:d"]]
    assert packed == [["atom:b", "atom:c", "atom:d"]]
    assert "atom:a" not in packed[0]


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


def test_stage1_job_includes_same_run_terminal_context_without_assigning_it(
    tmp_path: Path,
) -> None:
    assigned = _atom("atom:failed-probe")
    assigned["run_id"] = "run:success"
    assigned["run_rel"] = "runs/success"
    assigned["origin_run_id"] = "run:success"
    terminal = _atom("atom:terminal-success")
    terminal.update(
        {
            "run_id": "run:success",
            "run_rel": "runs/success",
            "origin_run_id": "run:success",
            "source": "agent_last_message_artifact",
            "text": json.dumps(
                {
                    "kind": "task_run_v1",
                    "status": "success",
                    "summary": "The intended workflow recovered and verification passed.",
                    "verification": [{"check": "original scenario", "result": "passed"}],
                    "issues": [],
                }
            ),
        }
    )
    unrelated = _atom("atom:other-terminal")
    unrelated.update(
        {
            "run_id": "run:other",
            "run_rel": "runs/other",
            "origin_run_id": "run:other",
            "source": "run_failure_event",
            "text": "A different run failed.",
        }
    )

    jobs = _problem_mining_jobs_with_terminal_context(
        eligible_prompt_atoms=_atoms_for_problem_mining_prompt([assigned]),
        all_prompt_atoms=_atoms_for_problem_mining_prompt([assigned, terminal, unrelated]),
    )

    assert len(jobs) == 1
    assert jobs[0]["assigned_atom_ids"] == ["atom:failed-probe"]
    assert jobs[0]["context_atom_ids"] == ["atom:terminal-success"]
    context = jobs[0]["context_atoms"][0]
    assert context["problem_mining_context_role"] == "origin_run_terminal"
    assert context["decision_eligible"] is False
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=tmp_path / "context-workspace",
        prompt_atoms=jobs[0]["prompt_atoms"],
        max_records_per_miner=20,
        assigned_atom_ids=jobs[0]["assigned_atom_ids"],
    )
    assert manifest["decision_eligible_atom_ids"] == ["atom:failed-probe"]
    assert manifest["context_atom_ids"] == ["atom:terminal-success"]
    assert manifest["context_atom_count"] == 1
    assert {
        entry["atom_id"]: (entry["assigned"], entry["context_only"])
        for entry in manifest["atom_files"]
    } == {
        "atom:failed-probe": (True, False),
        "atom:terminal-success": (False, True),
    }


def test_extracted_success_report_is_terminal_context_when_agent_message_is_absent(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target" / "run" / "codex" / "0"
    run_dir.mkdir(parents=True)
    atoms = extract_backlog_atoms(
        [
            {
                "run_dir": str(run_dir),
                "run_rel": "target/run/codex/0",
                "agent": "codex",
                "status": "ok",
                "metrics": {
                    "commands_failed": 1,
                    "failed_commands": [
                        {"command": "python -m package doctor", "exit_code": 1}
                    ],
                },
                "report": {
                    "schema_version": 1,
                    "kind": "task_run_v1",
                    "status": "success",
                    "goal": "Complete the intended workflow",
                    "summary": "Dependencies were installed and final verification passed.",
                    "steps": [
                        {
                            "name": "workflow",
                            "attempts": [{"action": "run"}],
                            "outcome": "complete",
                        }
                    ],
                    "outputs": [],
                    "verification": [{"check": "scenario", "result": "passed"}],
                    "next_actions": ["None."],
                },
            }
        ],
        repo_root=tmp_path,
    )["atoms"]
    eligible = eligible_problem_mining_atoms(atoms)
    jobs = _problem_mining_jobs_with_terminal_context(
        eligible_prompt_atoms=_atoms_for_problem_mining_prompt(eligible),
        all_prompt_atoms=_atoms_for_problem_mining_prompt(atoms),
    )

    command_id = next(
        str(atom["atom_id"]) for atom in atoms if atom.get("source") == "command_failure"
    )
    job = next(job for job in jobs if command_id in job["assigned_atom_ids"])
    context = next(
        atom for atom in job["context_atoms"] if atom.get("source") == "run_outcome_context"
    )
    assert context["report_status"] == "success"
    assert context["verification_result_values"] == ["passed"]
    assert context["decision_eligible"] is False


def test_terminal_context_counts_against_job_budget_without_losing_assignments() -> None:
    assigned_atoms = [_atom(f"atom:assigned-{index}") for index in range(6)]
    terminal_atoms: list[dict[str, object]] = []
    for index, atom in enumerate(assigned_atoms):
        run_id = f"run:{index}"
        atom.update(
            {
                "run_id": run_id,
                "run_rel": f"runs/{index}",
                "origin_run_id": run_id,
                "text": "A diagnostic command failed before recovery. " * 8,
            }
        )
        terminal = _atom(f"atom:terminal-{index}")
        terminal.update(
            {
                "run_id": run_id,
                "run_rel": f"runs/{index}",
                "origin_run_id": run_id,
                "source": "agent_last_message_artifact",
                "text": "Terminal outcome and exact verification evidence. " * 18,
            }
        )
        terminal_atoms.append(terminal)
    eligible = _atoms_for_problem_mining_prompt(assigned_atoms)
    all_atoms = _atoms_for_problem_mining_prompt([*assigned_atoms, *terminal_atoms])
    max_bytes = 8_000

    jobs = _problem_mining_jobs_with_terminal_context(
        eligible_prompt_atoms=eligible,
        all_prompt_atoms=all_atoms,
        chunk_max_bytes=55_000,
        max_chunks=3,
        max_atoms=100,
        max_bytes=max_bytes,
    )

    assert len(jobs) > 1
    assert [
        atom_id for job in jobs for atom_id in job["assigned_atom_ids"]
    ] == [str(atom["atom_id"]) for atom in assigned_atoms]
    for job in jobs:
        assert set(job["assigned_atom_ids"]).isdisjoint(job["context_atom_ids"])
        assert len(
            _problem_mining_job_batches(
                job["prompt_atoms"],
                chunk_max_bytes=55_000,
                max_chunks=3,
                max_atoms=100,
                max_bytes=max_bytes,
            )
        ) == 1


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


def test_problem_mining_projection_preserves_and_attests_every_linked_atom(
    tmp_path: Path,
) -> None:
    atom = _atom()
    expected_links = [f"atom:linked:{index}" for index in range(7)]
    atom["linked_atom_ids"] = [
        *expected_links,
        " ",
        expected_links[2],
        f" {expected_links[6]} ",
    ]

    projection = _atoms_for_problem_mining_prompt([atom])[0]

    assert projection["linked_atom_ids"] == expected_links
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=[projection],
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    chunk = manifest["chunks"][0]
    chunk_path = workspace / str(chunk["text_file"])
    chunk_text = chunk_path.read_text(encoding="utf-8")
    assert all(link in chunk_text for link in expected_links)

    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(chunk["text_file"]),
        file_path=chunk_path,
    )
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
        append=True,
        include_chunks=False,
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
                "disposition": "unresolved",
                "problem_ids": [],
                "rationale": "The available evidence does not establish an actionable case.",
                "revisit_when": None,
            }
        ],
        response_text="{}",
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )
    assert len(receipt["read_attestations"]) == 1
    assert receipt["read_attestations"][0]["atom_id"] == "atom:one"


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
        include_origin_attachments=False,
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
        "problem_mining_001_coverage_depth_review_correction_001",
    ]
    miner_result = stage_doc["input_meta"]["miner_results"][0]
    assert miner_result["status"] == "ok"
    assert miner_result["coverage_depth_review_format_retry_count"] == 1
    review_attempts = miner_result["coverage_depth_review_attempt_history"]
    assert [attempt["status"] for attempt in review_attempts] == [
        "response_contract_failed",
        "verified",
    ]
    assert review_attempts[0]["workspace_dir"] == review_attempts[1]["workspace_dir"]
    assert (
        review_attempts[0]["workspace_manifest_sha256"]
        == review_attempts[1]["workspace_manifest_sha256"]
    )
    receipt = stage_doc["input_meta"]["problem_mining_evidence_draft"]["miners"][0]
    assert receipt["status"] == "verified"
    assert receipt["non_support_review"]["status"] == "verified"
    assert receipt["non_support_review"]["successful_attempt_tag"] == (
        "problem_mining_001_coverage_depth_review_correction_001"
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


def test_relation_review_repairs_structural_errors_in_exact_reviewer_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relation_items = [
        {
            "problem_id": "problem:0",
            "case_id": "case:0",
            "title": "Problem 0",
            "evidence_atom_ids": ["atom:0"],
        }
    ]
    neighborhoods = [
        {
            "focus_id": "problem:0",
            "most_related_by_semantic": [],
            "most_related_by_evidence_overlap": [],
            "most_related_by_metadata": [],
            "most_related_by_path_anchor": [],
        }
    ]
    invalid = json.dumps(
        [
            {
                "focus_id": "problem:0",
                "action": "merge",
                "rationale": "These may match.",
                "review_confidence": 0.9,
            }
        ]
    )
    valid = json.dumps(
        [
            {
                "focus_id": "problem:0",
                "action": "keep_separate",
                "rationale": "No objective identity edge exists.",
                "review_confidence": 0.9,
            }
        ]
    )
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> StagePromptRun:
        calls.append(dict(kwargs))
        return _write_fake_relation_stage_run(
            kwargs=dict(kwargs),
            response=invalid if len(calls) == 1 else valid,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        fake_run,
    )
    review_dir = tmp_path / "relation_review"
    review_dir.mkdir()
    decisions, batches = _run_relation_review_batches(
        relation_items=relation_items,
        neighborhoods=neighborhoods,
        focus_problem_ids=["problem:0"],
        template="{{STAGE_GUIDANCE}}\n{{ALLOWED_ACTIONS}}\n{{NEIGHBORHOODS_JSON}}",
        allowed_actions=["merge", "alias", "split", "same_cause_group", "keep_separate"],
        stage_guidance_text="Use objective evidence.",
        review_dir=review_dir,
        tag="relation_review",
        agent="codex",
        model=None,
        cfg=object(),
    )

    assert [decision["action"] for decision in decisions] == ["keep_separate"]
    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == "019f2cca-9011-7e32-88ae-6c25af578b49"
    assert calls[0]["workspace_dir"] == calls[1]["workspace_dir"]
    assert batches[0]["status"] == "completed"
    assert batches[0]["correction_status"] == "corrected"
    correction_prompt = str(calls[1]["prompt"])
    assert "relation_decision_merge_targets_invalid" in correction_prompt
    assert invalid not in correction_prompt


def test_relation_review_retries_fresh_until_codex_author_session_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relation_items = [
        {
            "problem_id": "problem:0",
            "case_id": "case:0",
            "title": "Problem 0",
            "evidence_atom_ids": ["atom:0"],
        }
    ]
    neighborhoods = [
        {
            "focus_id": "problem:0",
            "most_related_by_semantic": [],
            "most_related_by_evidence_overlap": [],
            "most_related_by_metadata": [],
            "most_related_by_path_anchor": [],
        }
    ]
    valid = json.dumps(
        [
            {
                "focus_id": "problem:0",
                "action": "keep_separate",
                "rationale": "No objective identity edge exists.",
                "review_confidence": 0.9,
            }
        ]
    )
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> StagePromptRun:
        calls.append(dict(kwargs))
        return _write_fake_relation_stage_run(
            kwargs=dict(kwargs),
            response="" if len(calls) == 1 else valid,
            session_available=len(calls) > 1,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        fake_run,
    )
    review_dir = tmp_path / "relation_review"
    review_dir.mkdir()
    decisions, batches = _run_relation_review_batches(
        relation_items=relation_items,
        neighborhoods=neighborhoods,
        focus_problem_ids=["problem:0"],
        template="{{STAGE_GUIDANCE}}\n{{ALLOWED_ACTIONS}}\n{{NEIGHBORHOODS_JSON}}",
        allowed_actions=["merge", "alias", "split", "same_cause_group", "keep_separate"],
        stage_guidance_text="Use objective evidence.",
        review_dir=review_dir,
        tag="relation_review",
        agent="codex",
        model=None,
        cfg=object(),
    )

    assert [decision["action"] for decision in decisions] == ["keep_separate"]
    assert len(calls) == 2
    assert calls[0]["resume_session_id"] is None
    assert calls[1]["resume_session_id"] is None
    assert calls[1]["prompt"] == calls[0]["prompt"]
    assert batches[0]["status"] == "completed"
    assert batches[0]["correction_status"] == "accepted"
    assert batches[0]["correction_metrics"]["attempt_count"] == 2
    assert batches[0]["correction_metrics"]["session_acquisition_retry_count"] == 1
    assert len(batches[0]["attempt_history"]) == 2
    assert batches[0]["attempt_history"][0]["agent_session_id"] is None
    assert batches[0]["attempt_history"][1]["agent_session_id"] is not None


def test_relation_review_transient_exact_session_exception_retries_same_author(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relation_items = [
        {
            "problem_id": "problem:0",
            "case_id": "case:0",
            "title": "Problem 0",
            "evidence_atom_ids": ["atom:0"],
        }
    ]
    neighborhoods = [
        {
            "focus_id": "problem:0",
            "most_related_by_semantic": [],
            "most_related_by_evidence_overlap": [],
            "most_related_by_metadata": [],
            "most_related_by_path_anchor": [],
        }
    ]
    invalid = json.dumps(
        [
            {
                "focus_id": "problem:0",
                "action": "merge",
                "rationale": "The target is missing.",
                "review_confidence": 0.9,
            }
        ]
    )
    valid = json.dumps(
        [
            {
                "focus_id": "problem:0",
                "action": "keep_separate",
                "rationale": "No objective identity edge exists.",
                "review_confidence": 0.9,
            }
        ]
    )
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> StagePromptRun:
        calls.append(dict(kwargs))
        if len(calls) == 2:
            raise RuntimeError("transient resumed transport loss")
        return _write_fake_relation_stage_run(
            kwargs=dict(kwargs),
            response=invalid if len(calls) == 1 else valid,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        fake_run,
    )
    review_dir = tmp_path / "relation_review"
    review_dir.mkdir()
    decisions, batches = _run_relation_review_batches(
        relation_items=relation_items,
        neighborhoods=neighborhoods,
        focus_problem_ids=["problem:0"],
        template="{{STAGE_GUIDANCE}}\n{{ALLOWED_ACTIONS}}\n{{NEIGHBORHOODS_JSON}}",
        allowed_actions=["merge", "alias", "split", "same_cause_group", "keep_separate"],
        stage_guidance_text="Use objective evidence.",
        review_dir=review_dir,
        tag="relation_review",
        agent="codex",
        model=None,
        cfg=object(),
    )

    assert [decision["action"] for decision in decisions] == ["keep_separate"]
    assert len(calls) == 3
    assert calls[1]["resume_session_id"] == calls[2]["resume_session_id"]
    assert calls[1]["resume_session_id"] is not None
    assert batches[0]["correction_status"] == "corrected"
    assert batches[0]["correction_metrics"]["correction_invocation_failure_count"] == 1
    assert [attempt["status"] for attempt in batches[0]["attempt_history"]] == [
        "invalid",
        "invocation_failed",
        "verified",
    ]


def test_relation_review_confidence_is_telemetry_not_a_collapse_gate() -> None:
    errors = _relation_decision_item_errors(
        {
            "focus_id": "problem:0",
            "action": "merge",
            "target_ids": ["problem:1"],
            "evidence_atom_ids": ["atom:shared"],
            "rationale": "Both records cite the exact same observed failure.",
            "review_confidence": 0.05,
        },
        focus_problem_ids={"problem:0"},
        known_problem_ids={"problem:0", "problem:1"},
        known_evidence_atom_ids={"atom:shared"},
        allowed_actions={"merge", "alias", "split", "same_cause_group", "keep_separate"},
    )

    assert errors == []


def test_relation_review_stall_preserves_valid_focus_and_falls_back_only_missing_focus(
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
        for index in range(2)
    ]
    neighborhoods = [
        {
            "focus_id": f"problem:{index}",
            "most_related_by_semantic": [],
            "most_related_by_evidence_overlap": [],
            "most_related_by_metadata": [],
            "most_related_by_path_anchor": [],
        }
        for index in range(2)
    ]
    partial = json.dumps(
        [
            {
                "focus_id": "problem:0",
                "action": "keep_separate",
                "rationale": "Independent evidence keeps this case distinct.",
                "review_confidence": 0.9,
            }
        ]
    )
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> StagePromptRun:
        calls.append(dict(kwargs))
        return _write_fake_relation_stage_run(kwargs=dict(kwargs), response=partial)

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        fake_run,
    )
    review_dir = tmp_path / "relation_review"
    review_dir.mkdir()
    decisions, batches = _run_relation_review_batches(
        relation_items=relation_items,
        neighborhoods=neighborhoods,
        focus_problem_ids=["problem:0", "problem:1"],
        template="{{STAGE_GUIDANCE}}\n{{ALLOWED_ACTIONS}}\n{{NEIGHBORHOODS_JSON}}",
        allowed_actions=["merge", "alias", "split", "same_cause_group", "keep_separate"],
        stage_guidance_text="Use objective evidence.",
        review_dir=review_dir,
        tag="relation_review",
        agent="codex",
        model=None,
        cfg=object(),
        max_foci=2,
    )

    assert len(calls) == 3
    by_focus = {decision["focus_id"]: decision for decision in decisions}
    assert by_focus["problem:0"]["rationale"] == (
        "Independent evidence keeps this case distinct."
    )
    assert by_focus["problem:1"]["provisional_relation_suggestion"]["kind"] == (
        "relation_review_batch_failure"
    )
    assert batches[0]["status"] == "failed_partial_provisional_keep_separate"
    assert batches[0]["retained_valid_decision_count"] == 1
    assert "first_noop_receives_feedback" in str(calls[2]["prompt"])


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

    def _fake_run_stage_prompt_json(**kwargs: object) -> StagePromptRun:
        if "batch_001" in str(kwargs["tag"]):
            raise RuntimeError("reviewer unavailable")
        return _write_fake_relation_stage_run(
            kwargs=dict(kwargs),
            response=json.dumps(
                [
                    {
                        "focus_id": "problem:2",
                        "action": "keep_separate",
                        "rationale": "No objective identity edge exists.",
                        "review_confidence": 0.9,
                    }
                ]
            ),
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


def test_failed_relation_batch_count_includes_partial_failures() -> None:
    assert (
        _failed_relation_review_batch_count(
            [
                {"status": "completed"},
                {"status": "failed_provisional_keep_separate"},
                {"status": "failed_partial_provisional_keep_separate"},
            ]
        )
        == 2
    )


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


def test_independent_stage1_feedback_resumes_exact_reviewer_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import usertest_backlog.workflows.problem_mining as problem_mining

    session_id = "11111111-1111-4111-8111-111111111111"
    workspace = tmp_path / "review-workspace"
    workspace.mkdir()
    manifest = {
        "schema_version": 1,
        "assigned_atom_ids": ["atom:one"],
        "chunks": [],
    }
    (workspace / "atoms.json").write_text(json.dumps(manifest), encoding="utf-8")
    manifest_sha = problem_mining._problem_mining_attempt_manifest_sha256(manifest)
    primary_workspace = tmp_path / "primary-workspace"
    primary_workspace.mkdir()
    (primary_workspace / "atoms.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    primary_events = primary_workspace / "primary.normalized.jsonl"
    primary_events.write_text("", encoding="utf-8")
    receipt_path = tmp_path / "problem_mining.evidence.json"
    finalized_receipt = {
        "schema_version": 1,
        "receipt_kind": "problem_mining_evidence",
        "mode": "live",
        "eligible_atom_ids": ["atom:one"],
        "eligible_source_atom_ids": ["atom:one"],
        "eligible_derived_atom_ids": [],
        "eligible_corpus_sha256": "a" * 64,
        "atom_evidence": [],
        "miners": [
            {
                "tag": "problem_mining_001",
                "status": "verified",
                "primary_pass": {
                    "tag": "problem_mining_001",
                    "status": "verified",
                    "workspace_dir": str(primary_workspace),
                    "normalized_events_path": str(primary_events),
                    "atom_decisions": [
                        {
                            "atom_id": "atom:one",
                            "disposition": "unresolved",
                            "problem_ids": [],
                            "rationale": "Primary evidence remained unresolved.",
                            "revisit_when": None,
                        }
                    ],
                },
                "non_support_review": {"status": "verified"},
                "review_scope": "all_assigned_atoms_positive_and_non_support",
                "primary_problem_records": [],
                "primary_atom_decisions": [
                    {
                        "atom_id": "atom:one",
                        "disposition": "unresolved",
                        "problem_ids": [],
                        "rationale": "Primary evidence remained unresolved.",
                        "revisit_when": None,
                    }
                ],
            }
        ],
        "decision_partition": [],
    }
    receipt_path.write_text(json.dumps(finalized_receipt), encoding="utf-8")
    stage_doc = {
        "stage": "problem_mining",
        "items": [],
        "input_meta": {
            "miner_results": [
                {
                    "tag": "problem_mining_001",
                    "template": "problem_miner_default.md",
                    "assigned_atom_ids": ["atom:one"],
                    "coverage_depth_review_attempt_history": [
                        {
                            "status": "verified",
                            "agent_session_id": session_id,
                            "workspace_dir": str(workspace),
                            "workspace_manifest_sha256": manifest_sha,
                        }
                    ],
                }
            ],
            "problem_mining_evidence_receipt": {"path": str(receipt_path)},
        },
    }
    atoms = [{"atom_id": "atom:one", "evidence_role": "observation"}]
    feedback = {
        "content_sha256": "b" * 64,
        "rationale": "The source observation was falsely rejected.",
    }
    calls: list[dict[str, object]] = []

    def attempt(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "failure": None,
            "envelope": {
                "atom_decisions": [
                    {
                        "atom_id": "atom:one",
                        "disposition": "supports_case",
                        "problem_ids": ["problem:one"],
                        "rationale": "Observed failure establishes the case.",
                    }
                ]
            },
            "records": [
                {
                    "problem_id": "problem:one",
                    "evidence_atom_ids": ["atom:one"],
                }
            ],
            "receipt": {
                "tag": "problem_mining_001",
                "status": "verified",
                "atom_decisions": [
                    {
                        "atom_id": "atom:one",
                        "disposition": "supports_case",
                        "problem_ids": ["problem:one"],
                        "rationale": "Observed failure establishes the case.",
                        "revisit_when": None,
                    }
                ],
            },
            "attempt_record": {"status": "verified", "artifacts": {}},
        }

    monkeypatch.setattr(problem_mining, "_run_problem_mining_attempt", attempt)
    monkeypatch.setattr(
        problem_mining,
        "_build_composite_miner_receipt",
        lambda **kwargs: (
            {
                "tag": "problem_mining_001",
                "status": "verified",
                "primary_pass": kwargs["primary_receipt"],
                "non_support_review": kwargs["review_receipt"],
                "review_scope": "all_assigned_atoms_positive_and_non_support",
                "primary_problem_records": kwargs["primary_records"],
                "primary_atom_decisions": kwargs["primary_decisions"],
                "coverage_depth_review_problem_records": kwargs["review_records"],
                "coverage_depth_review_atom_decisions": kwargs["review_decisions"],
            },
            list(kwargs["review_records"]),
            list(kwargs["review_decisions"]),
        ),
    )
    monkeypatch.setattr(
        problem_mining,
        "assign_problem_case_ids",
        lambda records, *_args, **_kwargs: (
            [{**records[0], "case_id": "case:one"}] if records else []
        ),
    )
    relation_calls: list[dict[str, object]] = []

    def relation(**kwargs: object) -> tuple[dict, list, list, dict]:
        relation_calls.append(kwargs)
        updated = dict(kwargs["stage_doc"])
        updated["items"] = kwargs["problem_records"]
        return (
            updated,
            kwargs["problem_records"],
            kwargs["atoms"],
            {"cases": {}},
        )

    monkeypatch.setattr(problem_mining, "_run_problem_case_relation_review", relation)
    result = problem_mining.continue_problem_mining_from_independent_feedback(
        stage_doc=stage_doc,
        atoms=atoms,
        actionable_atom_ids=["atom:one"],
        feedback=feedback,
        pipeline_manifest=object(),
        stage_guidance_text="guidance",
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "repaired.problem_records.json",
        out_md=tmp_path / "repaired.problem_records.md",
        case_registry_path=tmp_path / "case_registry.json",
        previous_case_registry={"cases": {}},
        repo_root=tmp_path,
        agent="codex",
        model=None,
        cfg=object(),
    )

    assert result["status"] == "corrected"
    assert calls[0]["resume_session_id"] == session_id
    assert calls[0]["initial_workspace_dir"] == workspace.resolve()
    assert calls[0]["expected_manifest_sha256"] == manifest_sha
    assert "BOUND FEEDBACK" in str(calls[0]["prompt"])
    draft = relation_calls[0]["stage_doc"]["input_meta"][
        "problem_mining_evidence_draft"
    ]
    assert (
        draft["miners"][0]["non_support_review"]["attempt_history"][-1]["status"]
        == "verified"
    )
    assert draft["miners"][0]["primary_problem_records"] == []

    def retract_attempt(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "failure": None,
            "envelope": {
                "atom_decisions": [
                    {
                        "atom_id": "atom:one",
                        "disposition": "unresolved",
                        "problem_ids": [],
                        "rationale": "The retained evidence does not establish a problem.",
                        "revisit_when": None,
                    }
                ]
            },
            "records": [],
            "receipt": {
                "tag": "problem_mining_001",
                "status": "verified",
                "atom_decisions": [
                    {
                        "atom_id": "atom:one",
                        "disposition": "unresolved",
                        "problem_ids": [],
                        "rationale": "The retained evidence does not establish a problem.",
                        "revisit_when": None,
                    }
                ],
            },
            "attempt_record": {"status": "verified", "artifacts": {}},
        }

    monkeypatch.setattr(problem_mining, "_run_problem_mining_attempt", retract_attempt)
    bad_stage_doc = json.loads(json.dumps(stage_doc))
    bad_stage_doc["items"] = [
        {
            "problem_id": "problem:shallow",
            "case_id": "case:shallow",
            "evidence_atom_ids": ["atom:one"],
        }
    ]
    retracted = problem_mining.continue_problem_mining_from_independent_feedback(
        stage_doc=bad_stage_doc,
        atoms=atoms,
        actionable_atom_ids=["atom:one"],
        feedback={
            "content_sha256": "c" * 64,
            "feedback_kind": "accepted_output_quality",
            "rationale": "The mined problem is not established by the source evidence.",
        },
        pipeline_manifest=object(),
        stage_guidance_text="guidance",
        artifacts_dir=tmp_path / "retraction-artifacts",
        out_json=tmp_path / "retracted.problem_records.json",
        out_md=tmp_path / "retracted.problem_records.md",
        case_registry_path=tmp_path / "retracted.case_registry.json",
        previous_case_registry={"cases": {}},
        repo_root=tmp_path,
        agent="codex",
        model=None,
        cfg=object(),
    )

    assert retracted["status"] == "corrected"
    assert retracted["stage_doc"]["items"] == []
    assert relation_calls[-1]["problem_records"] == []
    assert "retract the bad case" in str(calls[-1]["prompt"])


@pytest.mark.parametrize(
    ("mismatch", "expected_error"),
    [
        ("miner_tag", "stage1_author_miner_tag_binding_mismatch"),
        ("session", "stage1_author_session_binding_mismatch"),
        ("workspace", "stage1_author_workspace_binding_mismatch"),
    ],
)
def test_stage1_correction_rejects_wrong_retained_author_before_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
    expected_error: str,
) -> None:
    import usertest_backlog.workflows.problem_mining as problem_mining

    retained_workspace = tmp_path / "retained"
    expected_workspace = (
        tmp_path / "different" if mismatch == "workspace" else retained_workspace
    )
    retained_session = "11111111-1111-4111-8111-111111111111"
    expected_session = (
        "22222222-2222-4222-8222-222222222222"
        if mismatch == "session"
        else retained_session
    )
    expected_tag = "missing-miner" if mismatch == "miner_tag" else "target-miner"
    stage_doc = {
        "stage": "problem_mining",
        "items": [],
        "input_meta": {
            "problem_mining_evidence_draft": {
                "eligible_atom_ids": ["atom:one"],
                "miners": [{"tag": "target-miner"}],
            },
            "miner_results": [
                {
                    "tag": "other-miner",
                    "assigned_atom_ids": ["atom:one"],
                    "coverage_depth_review_attempt_history": [
                        {
                            "status": "verified",
                            "agent_session_id": "other-session",
                            "workspace_dir": str(tmp_path / "other"),
                        }
                    ],
                },
                {
                    "tag": "target-miner",
                    "assigned_atom_ids": ["atom:one"],
                    "coverage_depth_review_attempt_history": [
                        {
                            "status": "verified",
                            "agent_session_id": retained_session,
                            "workspace_dir": str(retained_workspace),
                        }
                    ],
                },
            ],
        },
    }

    monkeypatch.setattr(
        problem_mining,
        "_run_problem_mining_attempt",
        lambda **_kwargs: pytest.fail("mismatched author must not be invoked"),
    )
    result = problem_mining.continue_problem_mining_from_independent_feedback(
        stage_doc=stage_doc,
        atoms=[{"atom_id": "atom:one", "evidence_role": "observation"}],
        actionable_atom_ids=["atom:one"],
        feedback={"content_sha256": "a" * 64, "rationale": "Correct this miss."},
        pipeline_manifest=object(),
        stage_guidance_text="guidance",
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "problem.json",
        out_md=tmp_path / "problem.md",
        case_registry_path=tmp_path / "cases.json",
        previous_case_registry={"cases": {}},
        repo_root=tmp_path,
        agent="codex",
        model=None,
        cfg=object(),
        author_component="coverage_review",
        author_provenance={
            "miner_tag": expected_tag,
            "agent_session_id": expected_session,
            "workspace_dir": str(expected_workspace),
        },
    )

    assert result["status"] == "repairable_paused:stage1_author_binding_mismatch"
    assert result["validation_errors"] == [expected_error]


def test_independent_relation_feedback_resumes_relation_author_not_miner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import usertest_backlog.workflows.problem_mining as problem_mining

    session_id = "22222222-2222-4222-8222-222222222222"
    workspace = tmp_path / "relation-workspace"
    workspace.mkdir()
    receipt_path = tmp_path / "evidence.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "eligible_atom_ids": ["atom:one", "atom:two"],
                "miners": [],
            }
        ),
        encoding="utf-8",
    )
    pre_relation = [
        {
            "problem_id": "problem:one",
            "case_id": "case:one",
            "evidence_atom_ids": ["atom:one"],
        },
        {
            "problem_id": "problem:two",
            "case_id": "case:two",
            "evidence_atom_ids": ["atom:two"],
        },
    ]
    stage_doc = {
        "stage": "problem_mining",
        "items": [
            {
                "problem_id": "problem:one",
                "case_id": "case:one",
                "case_member_problem_ids": ["problem:one", "problem:two"],
                "evidence_atom_ids": ["atom:one", "atom:two"],
            }
        ],
        "input_meta": {
            "pre_relation_problem_records": pre_relation,
            "relation_review_decisions": [
                {
                    "focus_id": "problem:one",
                    "action": "merge",
                    "target_id": "problem:two",
                    "rationale": "Initially treated as one case.",
                    "review_confidence": 0.8,
                },
                {
                    "focus_id": "problem:two",
                    "action": "merge",
                    "target_id": "problem:one",
                    "rationale": "Initially treated as one case.",
                    "review_confidence": 0.8,
                },
            ],
            "relation_review_batches": [
                {
                    "tag": "problem_mining_relation_review_001_batch_001",
                    "focus_ids": ["problem:one", "problem:two"],
                    "attempt_history": [
                        {
                            "attempt_number": 1,
                            "status": "verified",
                            "agent_session_id": session_id,
                            "workspace_dir": str(workspace.resolve()),
                            "elapsed_seconds": 4.0,
                        }
                    ],
                }
            ],
            "problem_mining_evidence_receipt": {"path": str(receipt_path)},
        },
        "artifacts": {},
    }
    corrected_decisions = [
        {
            "focus_id": "problem:one",
            "action": "keep_separate",
            "rationale": "The mechanisms differ.",
            "review_confidence": 0.9,
        },
        {
            "focus_id": "problem:two",
            "action": "keep_separate",
            "rationale": "The mechanisms differ.",
            "review_confidence": 0.9,
        },
    ]
    invocation_path = tmp_path / "relation.model_invocation.json"
    invocation_path.write_text("{}", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def run_prompt(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            response=json.dumps(corrected_decisions),
            agent_session_id=session_id,
            workspace_dir=workspace,
            elapsed_seconds=2.0,
            invocation_manifest_path=invocation_path,
        )

    monkeypatch.setattr(problem_mining, "run_stage_prompt_json", run_prompt)
    monkeypatch.setattr(
        problem_mining,
        "model_invocation_manifest_ref",
        lambda path: {"path": str(path), "sha256": "b" * 64},
    )
    relation_calls: list[dict[str, object]] = []

    def rematerialize(**kwargs: object) -> tuple[dict, list, list, dict]:
        relation_calls.append(kwargs)
        updated = dict(kwargs["stage_doc"])
        updated["items"] = pre_relation
        return updated, pre_relation, kwargs["atoms"], {"cases": {}}

    monkeypatch.setattr(
        problem_mining,
        "_run_problem_case_relation_review",
        rematerialize,
    )
    result = (
        problem_mining.continue_problem_relation_review_from_independent_feedback(
            stage_doc=stage_doc,
            atoms=[{"atom_id": "atom:one"}, {"atom_id": "atom:two"}],
            feedback={"content_sha256": "a" * 64, "rationale": "Bad merge."},
            author_provenance={
                "agent_session_id": session_id,
                "workspace_dir": str(workspace.resolve()),
                "relation_review_batch_tag": (
                    "problem_mining_relation_review_001_batch_001"
                ),
            },
            pipeline_manifest=object(),
            stage_guidance_text="guidance",
            artifacts_dir=tmp_path / "repair",
            out_json=tmp_path / "repaired.json",
            out_md=tmp_path / "repaired.md",
            case_registry_path=tmp_path / "cases.json",
            previous_case_registry={"cases": {}},
            repo_root=tmp_path,
            agent="codex",
            model=None,
            cfg=object(),
        )
    )

    assert result["status"] == "corrected"
    assert calls[0]["resume_session_id"] == session_id
    assert calls[0]["workspace_dir"] == workspace.resolve()
    assert relation_calls[0]["relation_decisions_override"] == corrected_decisions
    assert relation_calls[0]["relation_manifest_refs"]


def test_primary_miner_correction_requires_retained_independent_rereview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import usertest_backlog.workflows.problem_mining as problem_mining

    primary_session = "33333333-3333-4333-8333-333333333333"
    review_session = "44444444-4444-4444-8444-444444444444"
    manifest = {
        "schema_version": 1,
        "assigned_atom_ids": ["atom:one"],
        "chunks": [],
    }
    primary_workspace = tmp_path / "primary"
    review_workspace = tmp_path / "review"
    primary_workspace.mkdir()
    review_workspace.mkdir()
    for workspace in (primary_workspace, review_workspace):
        (workspace / "atoms.json").write_text(json.dumps(manifest), encoding="utf-8")
    primary_events = primary_workspace / "events.jsonl"
    primary_events.write_text("", encoding="utf-8")
    old_primary_record = {
        "problem_id": "problem:old",
        "evidence_atom_ids": ["atom:one"],
    }
    old_decision = {
        "atom_id": "atom:one",
        "disposition": "supports_case",
        "problem_ids": ["problem:old"],
        "rationale": "The old shallow claim was retained.",
        "revisit_when": None,
    }
    primary_receipt = {
        "tag": "problem_mining_001",
        "status": "verified",
        "workspace_dir": str(primary_workspace),
        "normalized_events_path": str(primary_events),
        "atom_decisions": [old_decision],
        "response_sha256": "a" * 64,
    }
    review_receipt = {
        "tag": "problem_mining_001_coverage_depth_review",
        "status": "verified",
        "workspace_dir": str(review_workspace),
        "atom_decisions": [old_decision],
        "response_sha256": "b" * 64,
    }
    finalized_receipt = {
        "schema_version": 1,
        "receipt_kind": "problem_mining_evidence",
        "mode": "live",
        "eligible_atom_ids": ["atom:one"],
        "eligible_source_atom_ids": ["atom:one"],
        "eligible_derived_atom_ids": [],
        "eligible_corpus_sha256": "c" * 64,
        "atom_evidence": [],
        "miners": [
            {
                "tag": "problem_mining_001",
                "status": "verified",
                "primary_pass": primary_receipt,
                "non_support_review": review_receipt,
                "review_scope": "all_assigned_atoms_positive_and_non_support",
                "primary_problem_records": [old_primary_record],
                "primary_atom_decisions": [old_decision],
                "coverage_depth_review_problem_records": [old_primary_record],
                "coverage_depth_review_atom_decisions": [old_decision],
            }
        ],
        "decision_partition": [],
    }
    receipt_path = tmp_path / "evidence.json"
    receipt_path.write_text(json.dumps(finalized_receipt), encoding="utf-8")
    manifest_sha = problem_mining._problem_mining_attempt_manifest_sha256(manifest)
    stage_doc = {
        "stage": "problem_mining",
        "items": [{**old_primary_record, "case_id": "case:old"}],
        "input_meta": {
            "miner_results": [
                {
                    "tag": "problem_mining_001",
                    "template": "problem_miner_default.md",
                    "assigned_atom_ids": ["atom:one"],
                    "attempt_history": [
                        {
                            "status": "verified",
                            "agent_session_id": primary_session,
                            "workspace_dir": str(primary_workspace),
                            "workspace_manifest_sha256": manifest_sha,
                        }
                    ],
                    "coverage_depth_review_attempt_history": [
                        {
                            "status": "verified",
                            "agent_session_id": review_session,
                            "workspace_dir": str(review_workspace),
                            "workspace_manifest_sha256": manifest_sha,
                        }
                    ],
                }
            ],
            "problem_mining_evidence_receipt": {"path": str(receipt_path)},
        },
    }
    corrected_record = {
        "problem_id": "problem:root",
        "evidence_atom_ids": ["atom:one"],
    }
    corrected_decision = {
        "atom_id": "atom:one",
        "disposition": "supports_case",
        "problem_ids": ["problem:root"],
        "rationale": "The corrected claim describes the observed mechanism.",
        "revisit_when": None,
    }
    direct_calls: list[dict[str, object]] = []

    def direct_attempt(**kwargs: object) -> dict[str, object]:
        direct_calls.append(kwargs)
        return {
            "failure": None,
            "agent_session_id": primary_session,
            "envelope": {"atom_decisions": [corrected_decision]},
            "records": [corrected_record],
            "receipt": {
                **primary_receipt,
                "atom_decisions": [corrected_decision],
                "normalized_events_path": str(primary_events),
            },
            "attempt_record": {
                "status": "verified",
                "agent_session_id": primary_session,
                "workspace_dir": str(primary_workspace),
                "artifacts": {},
            },
        }

    rereview_calls: list[dict[str, object]] = []

    def rereview(**kwargs: object) -> dict[str, object]:
        rereview_calls.append(kwargs)
        assert kwargs["resume_session_id"] == review_session
        assert "problem:root" in str(kwargs["prompt"])
        return {
            "failure": None,
            "agent_session_id": review_session,
            "workspace_dir": review_workspace,
            "manifest": manifest,
            "records": [corrected_record],
            "receipt": {
                **review_receipt,
                "atom_decisions": [corrected_decision],
            },
            "attempt_history": [
                {
                    "status": "verified",
                    "agent_session_id": review_session,
                    "workspace_dir": str(review_workspace),
                    "attempt_elapsed_seconds": 2.0,
                }
            ],
        }

    composite_calls: list[dict[str, object]] = []

    def composite(**kwargs: object) -> tuple[dict, list, list]:
        composite_calls.append(kwargs)
        return (
            {
                "tag": "problem_mining_001",
                "status": "verified",
                "primary_pass": kwargs["primary_receipt"],
                "non_support_review": kwargs["review_receipt"],
                "review_scope": "all_assigned_atoms_positive_and_non_support",
                "primary_problem_records": kwargs["primary_records"],
                "primary_atom_decisions": kwargs["primary_decisions"],
                "coverage_depth_review_problem_records": kwargs["review_records"],
                "coverage_depth_review_atom_decisions": kwargs["review_decisions"],
            },
            [corrected_record],
            [corrected_decision],
        )

    monkeypatch.setattr(problem_mining, "_run_problem_mining_attempt", direct_attempt)
    monkeypatch.setattr(
        problem_mining,
        "_run_problem_mining_job_with_response_retry",
        rereview,
    )
    monkeypatch.setattr(problem_mining, "_build_composite_miner_receipt", composite)
    monkeypatch.setattr(
        problem_mining,
        "assign_problem_case_ids",
        lambda records, *_args, **_kwargs: [
            {**record, "case_id": "case:root"} for record in records
        ],
    )
    monkeypatch.setattr(
        problem_mining,
        "_run_problem_case_relation_review",
        lambda **kwargs: (
            {**kwargs["stage_doc"], "items": kwargs["problem_records"]},
            kwargs["problem_records"],
            kwargs["atoms"],
            {"cases": {}},
        ),
    )
    result = problem_mining.continue_problem_mining_from_independent_feedback(
        stage_doc=stage_doc,
        atoms=[{"atom_id": "atom:one", "evidence_role": "observation"}],
        actionable_atom_ids=["atom:one"],
        feedback={
            "content_sha256": "d" * 64,
            "feedback_kind": "accepted_output_quality",
            "rationale": "The primary claim was surface-level.",
        },
        pipeline_manifest=object(),
        stage_guidance_text="guidance",
        artifacts_dir=tmp_path / "repair",
        out_json=tmp_path / "repaired.json",
        out_md=tmp_path / "repaired.md",
        case_registry_path=tmp_path / "cases.json",
        previous_case_registry={"cases": {}},
        repo_root=tmp_path,
        agent="codex",
        model=None,
        cfg=object(),
        author_component="problem_miner",
    )

    assert result["status"] == "corrected"
    assert direct_calls[0]["resume_session_id"] == primary_session
    assert len(rereview_calls) == 1
    assert composite_calls[0]["primary_records"] == [corrected_record]
    assert composite_calls[0]["review_records"] == [corrected_record]
    assert result["attempt_record"]["dependent_coverage_review_attempt_history"]
    repaired_miner = result["stage_doc"]["input_meta"][
        "problem_mining_evidence_draft"
    ]["miners"][0]
    assert repaired_miner["primary_pass"]["attempt_history"][-1]["status"] == "verified"
    assert repaired_miner["non_support_review"]["attempt_history"][-1]["status"] == "verified"


# Context-only receipt validation


def _verified_context_only_miner_receipt(tmp_path: Path) -> dict[str, object]:
    assigned = _atom("atom:assigned")
    context = _atom("atom:terminal-context")
    context.update(
        {
            "source": "agent_last_message_artifact",
            "text": "The run completed successfully and original-scenario verification passed.",
            "problem_mining_context_role": "origin_run_terminal",
            "decision_eligible": False,
        }
    )
    workspace = tmp_path / "context-receipt-workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([assigned, context]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:assigned"],
    )
    normalized = tmp_path / "context-receipt-events.jsonl"
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
    )
    problem = _problem("atom:assigned")
    return build_live_miner_receipt(
        tag="problem_mining_context_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=["atom:assigned"],
        eligible_atom_ids=["atom:assigned"],
        records=[problem],
        decisions=[
            {
                "atom_id": "atom:assigned",
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
                "rationale": "The assigned atom records the observed failure.",
            }
        ],
        response_text="{}",
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )


def test_live_receipt_attests_context_without_making_it_decision_eligible(
    tmp_path: Path,
) -> None:
    receipt = _verified_context_only_miner_receipt(tmp_path)

    assert receipt["assigned_atom_ids"] == ["atom:assigned"]
    assert receipt["context_atom_ids"] == ["atom:terminal-context"]
    assert [row["atom_id"] for row in receipt["read_attestations"]] == ["atom:assigned"]
    assert [row["atom_id"] for row in receipt["context_read_attestations"]] == [
        "atom:terminal-context"
    ]
    assert [decision["atom_id"] for decision in receipt["atom_decisions"]] == [
        "atom:assigned"
    ]
    assert (
        _miner_receipt_errors(
            receipt,
            eligible_ids={"atom:assigned"},
            require_live=True,
        )
        == []
    )


def test_context_read_attestation_is_required_during_receipt_revalidation(
    tmp_path: Path,
) -> None:
    receipt = _verified_context_only_miner_receipt(tmp_path)
    receipt["context_read_attestations"] = []

    assert "problem_mining_context_full_read_coverage_mismatch:problem_mining_context_001" in (
        _miner_receipt_errors(
            receipt,
            eligible_ids={"atom:assigned"},
            require_live=True,
        )
    )


def test_receipt_cannot_omit_context_retained_by_workspace_manifest(tmp_path: Path) -> None:
    receipt = _verified_context_only_miner_receipt(tmp_path)
    receipt["context_atom_ids"] = []
    receipt["context_read_attestations"] = []

    assert "problem_mining_workspace_context_mismatch:problem_mining_context_001" in (
        _miner_receipt_errors(
            receipt,
            eligible_ids={"atom:assigned"},
            require_live=True,
        )
    )


def test_context_atom_cannot_be_used_as_problem_citation(tmp_path: Path) -> None:
    assigned = _atom("atom:assigned")
    context = _atom("atom:terminal-context")
    workspace = tmp_path / "context-citation-workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([assigned, context]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:assigned"],
    )
    normalized = tmp_path / "context-citation-events.jsonl"
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
    )

    with pytest.raises(ValueError, match="problem_mining_citation_outside_eligible_corpus"):
        build_live_miner_receipt(
            tag="problem_mining_context_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:assigned"],
            eligible_atom_ids=["atom:assigned"],
            records=[_problem("atom:terminal-context")],
            decisions=[
                {
                    "atom_id": "atom:assigned",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "The assigned evidence is insufficient by itself.",
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_assigned_and_context_manifest_ids_must_be_disjoint(tmp_path: Path) -> None:
    workspace = tmp_path / "overlap-workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([_atom("atom:assigned")]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:assigned"],
    )
    manifest["context_atom_ids"] = ["atom:assigned"]
    normalized = tmp_path / "overlap-events.jsonl"
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
    )

    with pytest.raises(ValueError, match="problem_mining_workspace_assignment_context_overlap"):
        build_live_miner_receipt(
            tag="problem_mining_context_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:assigned"],
            eligible_atom_ids=["atom:assigned"],
            records=[],
            decisions=[
                {
                    "atom_id": "atom:assigned",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "The evidence is inconclusive.",
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_context_origin_attachment_requires_a_full_read(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "context"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "terminal_report.json"
    artifact.write_text('{"status":"success","verification":"passed"}\n', encoding="utf-8")
    assigned = _atom("atom:assigned")
    context = _atom("atom:terminal-context")
    context.update(
        {
            "run_dir": str(run_dir),
            "source": "agent_last_message_artifact",
            "attachments": [
                {
                    "kind": "terminal_report",
                    "artifact_ref": {
                        "path": artifact.name,
                        "sha256": sha256(artifact.read_bytes()).hexdigest(),
                        "size_bytes": artifact.stat().st_size,
                    },
                }
            ],
        }
    )
    workspace = tmp_path / "context-attachment-workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([assigned, context]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:assigned"],
        source_root=tmp_path,
    )
    assert origin_attachment_requirements(
        manifest["origin_attachment_evidence"],
        atom_ids=["atom:terminal-context"],
    )
    normalized = tmp_path / "context-attachment-events.jsonl"
    _write_required_workspace_read_events(
        normalized,
        workspace=workspace,
        manifest=manifest,
        include_origin_attachments=False,
    )

    with pytest.raises(ValueError, match="problem_mining_origin_attachment_not_read_in_full"):
        build_live_miner_receipt(
            tag="problem_mining_context_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:assigned"],
            eligible_atom_ids=["atom:assigned"],
            records=[],
            decisions=[
                {
                    "atom_id": "atom:assigned",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "The assigned evidence is inconclusive.",
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )
