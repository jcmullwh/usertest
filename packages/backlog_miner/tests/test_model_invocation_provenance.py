from __future__ import annotations

from pathlib import Path

import backlog_miner.pipeline as mod


def _write_prompt_artifacts(
    root: Path,
    *,
    tag: str,
    prompt: str,
    response: str | None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
    (root / f"{tag}.raw_events.jsonl").write_text("{}\n", encoding="utf-8")
    (root / f"{tag}.last_message.txt").write_text(
        response or "",
        encoding="utf-8",
    )
    (root / f"{tag}.stderr.txt").write_text("", encoding="utf-8")
    if response is not None:
        (root / f"{tag}.response.txt").write_text(response, encoding="utf-8")


def _stage_doc(stage: str = "solution_optioning") -> dict[str, object]:
    return {"stage": stage, "input_meta": {}, "artifacts": {}, "items": []}


def test_mixed_failed_and_successful_invocations_are_both_retained(
    tmp_path: Path,
) -> None:
    tracker = mod.ModelInvocationTracker(tmp_path)
    success_dir = tmp_path / "success"
    failed_dir = tmp_path / "failed"
    _write_prompt_artifacts(
        success_dir,
        tag="solution_optioning_001",
        prompt="success prompt",
        response="[]",
    )
    success_manifest = mod._write_model_invocation_manifest(
        stage="solution_optioning",
        tag="solution_optioning_001",
        agent="claude",
        out_dir=success_dir,
        prompt="success prompt",
        response="[]",
        error_kind=None,
    )
    _write_prompt_artifacts(
        failed_dir,
        tag="solution_optioning_002",
        prompt="failed prompt",
        response=None,
    )
    failed_manifest = mod._write_model_invocation_manifest(
        stage="solution_optioning",
        tag="solution_optioning_002",
        agent="claude",
        out_dir=failed_dir,
        prompt="failed prompt",
        response=None,
        error_kind="RuntimeError",
    )

    refs = tracker.collect()
    assert [ref["status"] for ref in refs] == ["failed", "verified"]
    attached = mod.attach_stage_model_invocation_contract(
        _stage_doc(),
        agent="claude",
        dry_run=False,
        manifest_refs=refs,
        invocation_expected=True,
    )

    assert success_manifest.is_file()
    assert failed_manifest.is_file()
    errors = mod.verify_stage_model_invocation_contract(attached)
    assert any("model_invocation_manifest_not_verified" in error for error in errors)


def test_codex_stage_contract_cannot_downgrade_subscription_requirement(
    tmp_path: Path,
) -> None:
    attached = mod.attach_stage_model_invocation_contract(
        _stage_doc(),
        agent="codex",
        dry_run=False,
        manifest_refs=[],
        invocation_expected=False,
    )
    contract = attached["input_meta"]["model_invocation_contract"]
    contract["subscription_required"] = False
    contract["contract_sha256"] = mod._canonical_sha256(
        {key: value for key, value in contract.items() if key != "contract_sha256"}
    )

    assert "stage_model_invocation_subscription_requirement_invalid" in (
        mod.verify_stage_model_invocation_contract(attached)
    )


def test_stage_contract_rejects_deleted_manifest(tmp_path: Path) -> None:
    tracker = mod.ModelInvocationTracker(tmp_path)
    out_dir = tmp_path / "one"
    _write_prompt_artifacts(
        out_dir,
        tag="solution_optioning_001",
        prompt="prompt",
        response="[]",
    )
    manifest = mod._write_model_invocation_manifest(
        stage="solution_optioning",
        tag="solution_optioning_001",
        agent="claude",
        out_dir=out_dir,
        prompt="prompt",
        response="[]",
        error_kind=None,
    )
    attached = mod.attach_stage_model_invocation_contract(
        _stage_doc(),
        agent="claude",
        dry_run=False,
        manifest_refs=tracker.collect(),
        invocation_expected=True,
    )

    manifest.unlink()
    assert "stage_model_invocation_ref_changed:0" in (
        mod.verify_stage_model_invocation_contract(attached)
    )
