"""Pipeline prompt-manifest loading and generic stage-prompt execution.

This module owns:
- ``PipelinePromptManifest``: the version-2 manifest dataclass.
- ``load_pipeline_prompt_manifest``: loads and validates the manifest from disk.
- ``run_stage_prompt_json``: generic helper that runs a single stage prompt through
  the agent backend and returns the raw text response.

All file references in the manifest are validated at load time.  Missing files raise
``FileNotFoundError`` loudly so no silent fallback to embedded defaults occurs.

Stage-specific orchestration lives in the CLI (``_run_problem_mining_stage``, etc.)
and calls this module's generic helpers.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Manifest dataclass
# ---------------------------------------------------------------------------

_MANIFEST_VERSION = 2
_MODEL_INVOCATION_SUFFIX = ".model_invocation.json"
_STAGE_INVOCATION_NAMES: dict[str, frozenset[str]] = {
    "solution_selection": frozenset(
        {
            "solution_selection",
            "solution_falsification",
            "solution_optioning",
            "selected_solution_labeler",
        }
    ),
}


@dataclass(frozen=True)
class StagePromptRun:
    """Structured stage invocation used by exact-session correction loops."""

    response: str
    agent_session_id: str | None
    resumed_from_session_id: str | None
    workspace_dir: Path | None
    invocation_manifest_path: Path
    prompt_path: Path
    response_path: Path
    raw_events_path: Path
    last_message_path: Path
    stderr_path: Path
    elapsed_seconds: float


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _file_receipt(path: Path) -> dict[str, Any]:
    exists = path.is_file()
    payload = path.read_bytes() if exists else b""
    return {
        "path": str(path.resolve()),
        "exists": exists,
        "size_bytes": len(payload) if exists else 0,
        "sha256": sha256(payload).hexdigest() if exists else None,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def model_invocation_manifest_path(*, out_dir: Path, tag: str) -> Path:
    return out_dir / f"{tag}{_MODEL_INVOCATION_SUFFIX}"


def _write_model_invocation_manifest(
    *,
    stage: str,
    tag: str,
    agent: str,
    out_dir: Path,
    prompt: str,
    response: str | None,
    error_kind: str | None,
    agent_session_id: str | None = None,
    resumed_from_session_id: str | None = None,
    workspace_dir: Path | None = None,
    model: str | None = None,
    invocation_started_at: str | None = None,
    invocation_ended_at: str | None = None,
    elapsed_seconds: float | None = None,
) -> Path:
    """Persist one prompt invocation, including a verified Codex auth proof."""

    from backlog_miner.ensemble import (
        _codex_auth_receipt_path,
        verify_codex_auth_receipt,
    )

    prompt_path = out_dir / f"{tag}.prompt.txt"
    response_path = out_dir / f"{tag}.response.txt"
    raw_events_path = out_dir / f"{tag}.raw_events.jsonl"
    last_message_path = out_dir / f"{tag}.last_message.txt"
    stderr_path = out_dir / f"{tag}.stderr.txt"
    auth_required = agent.strip().lower() == "codex"
    auth_receipt_path = _codex_auth_receipt_path(raw_events_path)
    auth_errors: list[str] = []
    if auth_required:
        auth_errors = verify_codex_auth_receipt(
            receipt_path=auth_receipt_path,
            prompt=prompt,
            raw_events_path=raw_events_path,
            last_message_path=last_message_path,
            stderr_path=stderr_path,
        )
    artifacts = {
        "prompt": _file_receipt(prompt_path),
        "response": _file_receipt(response_path),
        "raw_events": _file_receipt(raw_events_path),
        "last_message": _file_receipt(last_message_path),
        "stderr": _file_receipt(stderr_path),
    }
    response_matches = bool(
        response is not None
        and artifacts["response"]["exists"] is True
        and artifacts["response"]["sha256"] == sha256(response.encode("utf-8")).hexdigest()
    )
    verified = bool(
        error_kind is None
        and response_matches
        and artifacts["prompt"]["sha256"] == sha256(prompt.encode("utf-8")).hexdigest()
        and (not auth_required or not auth_errors)
    )
    manifest: dict[str, Any] = {
        "schema_version": 3,
        "invocation_id": uuid4().hex,
        "stage": stage,
        "tag": tag,
        "agent": agent.strip().lower(),
        "model": model,
        "invocation_started_at": invocation_started_at,
        "invocation_ended_at": invocation_ended_at,
        "elapsed_seconds": elapsed_seconds,
        "agent_session_id": agent_session_id,
        "resumed_from_session_id": resumed_from_session_id,
        "workspace_dir": str(workspace_dir.resolve()) if workspace_dir is not None else None,
        "status": "verified" if verified else "failed",
        "error_kind": error_kind,
        "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
        "response_sha256": (
            sha256(response.encode("utf-8")).hexdigest() if response is not None else None
        ),
        "artifacts": artifacts,
        "codex_subscription": {
            "required": auth_required,
            "verified": auth_required and not auth_errors,
            "receipt": _file_receipt(auth_receipt_path) if auth_required else None,
            "verification_errors": auth_errors,
        },
    }
    manifest["manifest_sha256"] = _canonical_sha256(manifest)
    path = model_invocation_manifest_path(out_dir=out_dir, tag=tag)
    _write_json_atomic(path, manifest)
    try:
        from backlog_miner.invocation_telemetry import (
            write_stage_invocation_telemetry,
        )

        write_stage_invocation_telemetry(manifest_path=path)
    except Exception as exc:  # noqa: BLE001 - metrics must not gate stage output
        _write_json_atomic(
            out_dir / f"{tag}.telemetry_error.json",
            {
                "schema_version": 1,
                "non_fatal": True,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )
    return path


def verify_model_invocation_manifest(
    path: Path,
    *,
    require_verified: bool = True,
) -> list[str]:
    """Re-open a prompt invocation and verify all retained bytes and auth proof."""

    from backlog_miner.ensemble import verify_codex_auth_receipt

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"model_invocation_manifest_unreadable:{type(exc).__name__}"]
    if not isinstance(raw, dict):
        return ["model_invocation_manifest_not_object"]
    errors: list[str] = []
    expected_hash = _canonical_sha256(
        {key: value for key, value in raw.items() if key != "manifest_sha256"}
    )
    if raw.get("manifest_sha256") != expected_hash:
        errors.append("model_invocation_manifest_hash_changed")
    schema_version = raw.get("schema_version")
    if schema_version not in {1, 2, 3}:
        errors.append("model_invocation_manifest_schema_invalid")
    if require_verified and raw.get("status") != "verified":
        errors.append("model_invocation_manifest_not_verified")
    for field in ("stage", "tag", "agent", "prompt_sha256"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"model_invocation_manifest_{field}_missing")
    artifacts_raw = raw.get("artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, dict) else {}
    resolved_paths: dict[str, Path] = {}
    for kind in ("prompt", "response", "raw_events", "last_message", "stderr"):
        receipt_raw = artifacts.get(kind)
        receipt = receipt_raw if isinstance(receipt_raw, dict) else {}
        path_raw = receipt.get("path")
        artifact_path = Path(path_raw) if isinstance(path_raw, str) else None
        current_receipt = _file_receipt(artifact_path) if artifact_path is not None else None
        absent_failed_response = bool(
            kind == "response"
            and raw.get("status") == "failed"
            and raw.get("response_sha256") is None
            and receipt.get("exists") is False
        )
        if artifact_path is None or (
            receipt != current_receipt and not absent_failed_response
        ):
            errors.append(f"model_invocation_artifact_changed:{kind}")
            continue
        if artifact_path.is_file() and not absent_failed_response:
            resolved_paths[kind] = artifact_path
    prompt_path = resolved_paths.get("prompt")
    response_path = resolved_paths.get("response")
    if prompt_path is not None:
        prompt_bytes = prompt_path.read_bytes()
        if raw.get("prompt_sha256") != sha256(prompt_bytes).hexdigest():
            errors.append("model_invocation_prompt_hash_changed")
    if response_path is not None:
        response_bytes = response_path.read_bytes()
        if raw.get("response_sha256") != sha256(response_bytes).hexdigest():
            errors.append("model_invocation_response_hash_changed")
    elif require_verified:
        errors.append("model_invocation_response_missing")

    auth_raw = raw.get("codex_subscription")
    auth = auth_raw if isinstance(auth_raw, dict) else {}
    agent = str(raw.get("agent") or "").strip().lower()
    if agent == "codex":
        session_raw = raw.get("agent_session_id")
        resumed_raw = raw.get("resumed_from_session_id")
        session_id: str | None = None
        if schema_version in {2, 3}:
            # A verified author turn must bind a UUID. A failed fresh invocation may
            # terminate before Codex creates an author session; retaining that failed
            # receipt is necessary for session acquisition telemetry and replay.
            session_required = bool(
                raw.get("status") == "verified"
                or session_raw is not None
                or resumed_raw is not None
            )
            if session_required:
                try:
                    session_id = str(UUID(str(session_raw)))
                except (ValueError, AttributeError, TypeError):
                    session_id = None
                if session_id is None or session_raw != session_id:
                    errors.append("model_invocation_agent_session_id_invalid")
            if resumed_raw is not None:
                try:
                    resumed_id = str(UUID(str(resumed_raw)))
                except (ValueError, AttributeError, TypeError):
                    resumed_id = None
                if resumed_id is None or resumed_raw != resumed_id or resumed_id != session_id:
                    errors.append("model_invocation_resumed_session_mismatch")
        if auth.get("required") is not True:
            errors.append("model_invocation_codex_subscription_not_required")
        if require_verified and auth.get("verified") is not True:
            errors.append("model_invocation_codex_subscription_not_verified")
        receipt_ref_raw = auth.get("receipt")
        receipt_ref = receipt_ref_raw if isinstance(receipt_ref_raw, dict) else {}
        receipt_path_raw = receipt_ref.get("path")
        receipt_path = Path(receipt_path_raw) if isinstance(receipt_path_raw, str) else None
        if receipt_path is None or receipt_ref != _file_receipt(receipt_path):
            errors.append("model_invocation_codex_receipt_changed")
        elif require_verified and all(
            kind in resolved_paths for kind in ("prompt", "raw_events", "last_message", "stderr")
        ):
            prompt_text = resolved_paths["prompt"].read_text(encoding="utf-8")
            errors.extend(
                verify_codex_auth_receipt(
                    receipt_path=receipt_path,
                    prompt=prompt_text,
                    raw_events_path=resolved_paths["raw_events"],
                    last_message_path=resolved_paths["last_message"],
                    stderr_path=resolved_paths["stderr"],
                )
            )
            try:
                receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                receipt_payload = {}
            activation = (
                receipt_payload.get("model_activation")
                if isinstance(receipt_payload, dict)
                else None
            )
            if schema_version in {2, 3} and (
                not isinstance(activation, dict)
                or activation.get("agent_session_id") != session_raw
                or activation.get("resumed_from_session_id") != resumed_raw
            ):
                errors.append("model_invocation_session_receipt_mismatch")
    elif auth.get("required") is not False:
        errors.append("model_invocation_unexpected_subscription_requirement")
    return list(dict.fromkeys(errors))


def model_invocation_manifest_ref(
    path: Path,
    *,
    require_verified: bool = True,
) -> dict[str, Any]:
    errors = verify_model_invocation_manifest(path, require_verified=require_verified)
    if errors:
        raise ValueError("model_invocation_manifest_invalid:" + ",".join(errors))
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "stage": payload["stage"],
        "tag": payload["tag"],
        "agent": payload["agent"],
        "status": payload["status"],
        "agent_session_id": payload.get("agent_session_id"),
        "resumed_from_session_id": payload.get("resumed_from_session_id"),
        "manifest_sha256": payload["manifest_sha256"],
    }


class ModelInvocationTracker:
    """Collect only invocation manifests created or replaced during one stage run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._before = {
            str(path.resolve()): sha256(path.read_bytes()).hexdigest()
            for path in root.rglob(f"*{_MODEL_INVOCATION_SUFFIX}")
            if path.is_file()
        }

    def collect(self) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for path in sorted(
            self.root.rglob(f"*{_MODEL_INVOCATION_SUFFIX}"),
            key=lambda item: item.as_posix(),
        ):
            if not path.is_file():
                continue
            digest = sha256(path.read_bytes()).hexdigest()
            if self._before.get(str(path.resolve())) == digest:
                continue
            refs.append(model_invocation_manifest_ref(path, require_verified=False))
        return refs


def attach_stage_model_invocation_contract(
    stage_doc: dict[str, Any],
    *,
    agent: str,
    dry_run: bool,
    manifest_refs: list[dict[str, Any]],
    invocation_expected: bool,
) -> dict[str, Any]:
    """Bind all prompt invocations into a standard stage-document contract."""

    updated = dict(stage_doc)
    input_meta_raw = updated.get("input_meta")
    input_meta = dict(input_meta_raw) if isinstance(input_meta_raw, dict) else {}
    refs = sorted(
        [dict(ref) for ref in manifest_refs],
        key=lambda ref: (str(ref.get("stage") or ""), str(ref.get("tag") or "")),
    )
    contract: dict[str, Any] = {
        "schema_version": 1,
        "agent": agent.strip().lower(),
        "dry_run": bool(dry_run),
        "subscription_required": agent.strip().lower() == "codex" and not dry_run,
        "invocation_expected": bool(invocation_expected and not dry_run),
        "manifests": refs,
    }
    contract["contract_sha256"] = _canonical_sha256(contract)
    input_meta["model_invocation_contract"] = contract
    updated["input_meta"] = input_meta
    errors = verify_stage_model_invocation_contract(updated, require_verified=False)
    if errors:
        raise ValueError("stage_model_invocation_contract_invalid:" + ",".join(errors))
    return updated


def merge_stage_model_invocation_contract(
    stage_doc: dict[str, Any],
    *,
    manifest_refs: list[dict[str, Any]],
    invocation_expected: bool,
) -> dict[str, Any]:
    """Append later same-stage invocations, such as canonical relation review."""

    input_meta_raw = stage_doc.get("input_meta")
    input_meta = input_meta_raw if isinstance(input_meta_raw, dict) else {}
    contract_raw = input_meta.get("model_invocation_contract")
    contract = contract_raw if isinstance(contract_raw, dict) else {}
    existing_raw = contract.get("manifests")
    existing = existing_raw if isinstance(existing_raw, list) else []
    return attach_stage_model_invocation_contract(
        stage_doc,
        agent=str(contract.get("agent") or ""),
        dry_run=contract.get("dry_run") is True,
        manifest_refs=[
            *[dict(ref) for ref in existing if isinstance(ref, dict)],
            *manifest_refs,
        ],
        invocation_expected=(contract.get("invocation_expected") is True or invocation_expected),
    )


def verify_stage_model_invocation_contract(
    stage_doc: dict[str, Any],
    *,
    require_verified: bool = True,
) -> list[str]:
    """Verify the durable prompt/auth provenance attached to one stage document."""

    input_meta_raw = stage_doc.get("input_meta")
    input_meta = input_meta_raw if isinstance(input_meta_raw, dict) else {}
    contract_raw = input_meta.get("model_invocation_contract")
    if not isinstance(contract_raw, dict):
        return ["stage_model_invocation_contract_missing"]
    contract = contract_raw
    errors: list[str] = []
    if contract.get("schema_version") != 1:
        errors.append("stage_model_invocation_contract_schema_invalid")
    agent_raw = contract.get("agent")
    if not isinstance(agent_raw, str) or not agent_raw.strip():
        errors.append("stage_model_invocation_contract_agent_missing")
    if not isinstance(contract.get("dry_run"), bool):
        errors.append("stage_model_invocation_contract_dry_run_invalid")
    if not isinstance(contract.get("invocation_expected"), bool):
        errors.append("stage_model_invocation_contract_expectation_invalid")
    expected_hash = _canonical_sha256(
        {key: value for key, value in contract.items() if key != "contract_sha256"}
    )
    if contract.get("contract_sha256") != expected_hash:
        errors.append("stage_model_invocation_contract_hash_changed")
    refs_raw = contract.get("manifests")
    refs = refs_raw if isinstance(refs_raw, list) else []
    if not isinstance(refs_raw, list):
        errors.append("stage_model_invocation_contract_manifests_invalid")
    if contract.get("invocation_expected") is True and not refs:
        errors.append("stage_model_invocation_manifest_missing")
    stage = str(stage_doc.get("stage") or "")
    allowed_invocation_stages = _STAGE_INVOCATION_NAMES.get(stage, frozenset({stage}))
    agent = str(contract.get("agent") or "").strip().lower()
    subscription_expected = agent == "codex" and contract.get("dry_run") is False
    if contract.get("subscription_required") is not subscription_expected:
        errors.append("stage_model_invocation_subscription_requirement_invalid")
    verified_manifest_count = 0
    for index, ref_raw in enumerate(refs):
        ref = ref_raw if isinstance(ref_raw, dict) else {}
        path_raw = ref.get("path")
        path = Path(path_raw) if isinstance(path_raw, str) else None
        if (
            path is None
            or not path.is_file()
            or ref.get("sha256") != sha256(path.read_bytes()).hexdigest()
        ):
            errors.append(f"stage_model_invocation_ref_changed:{index}")
            continue
        # Failed attempts are retained telemetry, not a reason to discard a later
        # verified result.  Their bytes and auth receipts still have to be intact.
        # Stage-specific validators decide whether the resulting work is usable.
        manifest_errors = verify_model_invocation_manifest(path, require_verified=False)
        errors.extend(f"stage_model_invocation:{error}" for error in manifest_errors)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        if manifest.get("status") == "verified":
            verified_manifest_count += 1
        if manifest.get("stage") not in allowed_invocation_stages:
            errors.append(f"stage_model_invocation_stage_mismatch:{index}")
        if manifest.get("agent") != agent:
            errors.append(f"stage_model_invocation_agent_mismatch:{index}")
        if ref.get("manifest_sha256") != manifest.get("manifest_sha256"):
            errors.append(f"stage_model_invocation_manifest_identity_changed:{index}")
    if (
        require_verified
        and contract.get("invocation_expected") is True
        and verified_manifest_count == 0
    ):
        errors.append("stage_model_invocation_verified_manifest_missing")
    return list(dict.fromkeys(errors))


# Stage template key names (used for validation and lookup).
_STAGE_TEMPLATE_KEYS: tuple[str, ...] = (
    "problem_miner_templates",
    "relation_reviewer_template",
    "prioritizer_template",
    "solution_optioner_template",
    "solution_selector_template",
    "solution_falsifier_template",
    "change_planner_template",
    "selected_solution_labeler_template",
    "ux_reviewer_template",
)


@dataclass(frozen=True)
class PipelinePromptManifest:
    """Validated pipeline prompt manifest (version 2).

    All path attributes are absolute ``Path`` objects resolved relative to the
    prompts directory.  Missing files are caught at construction time.

    Attributes
    ----------
    prompts_dir:
        Directory from which this manifest was loaded.
    problem_miner_templates:
        Ordered list of problem-miner prompt template paths (one per pass type:
        default, onboarding, harness, schema).
    relation_reviewer_template:
        Generic relation-reviewer prompt path.
    prioritizer_template:
        Stage-2 prioritizer prompt path.
    solution_optioner_template:
        Stage-4 solution-optioner prompt path.
    solution_selector_template:
        Stage-5 solution-selector prompt path.
    solution_falsifier_template:
        Independent stage-5 falsification-review prompt path.
    change_planner_template:
        Stage-6 change-planner prompt path.
    selected_solution_labeler_template:
        Post-selection labeler prompt path (replaces old early labeler).
    ux_reviewer_template:
        UX reviewer prompt path.
    stage_guidance_manifest_path:
        Path to ``configs/backlog_stage_guidance/manifest.json``.
    taxonomy_path:
        Path to ``configs/backlog_taxonomy.json``.
    relation_review_config_path:
        Path to ``configs/backlog_relation_review.yaml``.
    """

    prompts_dir: Path
    problem_miner_templates: tuple[Path, ...]
    relation_reviewer_template: Path | None
    prioritizer_template: Path | None
    solution_optioner_template: Path | None
    solution_selector_template: Path | None
    solution_falsifier_template: Path | None
    change_planner_template: Path | None
    selected_solution_labeler_template: Path | None
    ux_reviewer_template: Path | None
    stage_guidance_manifest_path: Path
    taxonomy_path: Path
    relation_review_config_path: Path

    def template_text(self, path: Path | None) -> str:
        """Return the text content of an optional template path.

        Parameters
        ----------
        path:
            Template path, or ``None`` if the template was not configured.

        Returns
        -------
        str
            Template text.

        Raises
        ------
        FileNotFoundError
            When *path* is not ``None`` but does not exist.
        ValueError
            When *path* is ``None``.
        """
        if path is None:
            raise ValueError("template_text: template path is None (not yet configured)")
        if not path.exists():
            raise FileNotFoundError(f"Missing pipeline prompt template: {path}")
        return path.read_text(encoding="utf-8")

    def load_taxonomy(self) -> dict[str, Any]:
        """Load and return the taxonomy JSON.

        Returns
        -------
        dict[str, Any]
            Parsed taxonomy document.

        Raises
        ------
        FileNotFoundError
            When the taxonomy file is missing.
        """
        if not self.taxonomy_path.exists():
            raise FileNotFoundError(f"Missing taxonomy config: {self.taxonomy_path}")
        return json.loads(self.taxonomy_path.read_text(encoding="utf-8"))

    def load_stage_guidance(self, stage: str) -> str:
        """Load stage guidance text for *stage*.

        Reads the per-stage guidance Markdown file referenced in the stage-guidance
        manifest.

        Parameters
        ----------
        stage:
            Stage identifier string (e.g. ``"problem_mining"``).

        Returns
        -------
        str
            Guidance text for the stage.

        Raises
        ------
        FileNotFoundError
            When the stage-guidance manifest or the referenced stage file is missing.
        KeyError
            When *stage* is not listed in the stage-guidance manifest.
        """
        if not self.stage_guidance_manifest_path.exists():
            raise FileNotFoundError(
                f"Missing stage-guidance manifest: {self.stage_guidance_manifest_path}"
            )
        manifest_doc = json.loads(self.stage_guidance_manifest_path.read_text(encoding="utf-8"))
        stages_map: dict[str, str] = manifest_doc.get("stages") or {}
        if stage not in stages_map:
            raise KeyError(
                f"Stage {stage!r} not found in stage-guidance manifest "
                f"({self.stage_guidance_manifest_path}); "
                f"available stages: {sorted(stages_map)}"
            )
        guidance_filename = stages_map[stage]
        guidance_path = self.stage_guidance_manifest_path.parent / guidance_filename
        if not guidance_path.exists():
            raise FileNotFoundError(f"Missing stage guidance file: {guidance_path}")
        return guidance_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Manifest loader
# ---------------------------------------------------------------------------


def _resolve_optional_template(
    prompts_dir: Path, raw_manifest: dict[str, Any], key: str
) -> Path | None:
    """Resolve an optional template path from the manifest dict.

    Parameters
    ----------
    prompts_dir:
        Prompts directory.
    raw_manifest:
        Parsed manifest JSON.
    key:
        Key in *raw_manifest* to look up.

    Returns
    -------
    Path | None
        Resolved path if the key is present and non-empty, else ``None``.

    Raises
    ------
    FileNotFoundError
        When the key is present and points to a file that does not exist.
    """
    value = raw_manifest.get(key)
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError(
            "load_pipeline_prompt_manifest: expected string "
            f"for {key!r}, got {type(value).__name__}"
        )
    path = prompts_dir / value
    if not path.exists():
        raise FileNotFoundError(f"Missing pipeline prompt template: {path} (key={key!r})")
    return path


def load_pipeline_prompt_manifest(prompts_dir: Path) -> PipelinePromptManifest:
    """Load and validate the pipeline prompt manifest from *prompts_dir*.

    The manifest file must be named ``pipeline_manifest.json`` and must declare
    ``"version": 2``.  All referenced files are validated at load time.  Missing
    files raise ``FileNotFoundError``; there is no silent fallback.

    Parameters
    ----------
    prompts_dir:
        Directory containing ``pipeline_manifest.json`` and referenced templates.

    Returns
    -------
    PipelinePromptManifest
        Validated manifest.

    Raises
    ------
    FileNotFoundError
        When the manifest file or any referenced file is missing.
    ValueError
        When the manifest has the wrong version or invalid structure.
    """
    manifest_path = prompts_dir / "pipeline_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Missing pipeline prompt manifest: {manifest_path}\n"
            "Create configs/backlog_prompts/pipeline_manifest.json with version=2."
        )

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"load_pipeline_prompt_manifest: invalid JSON in {manifest_path}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ValueError(
            f"load_pipeline_prompt_manifest: expected object in {manifest_path}, "
            f"got {type(raw).__name__}"
        )

    version = raw.get("version")
    if version != _MANIFEST_VERSION:
        raise ValueError(
            f"load_pipeline_prompt_manifest: expected version={_MANIFEST_VERSION} "
            f"in {manifest_path}, got {version!r}"
        )

    # Problem-miner templates (list).
    raw_miner_templates = raw.get("problem_miner_templates") or []
    if not isinstance(raw_miner_templates, list):
        raise ValueError(
            f"load_pipeline_prompt_manifest: 'problem_miner_templates' must be a list "
            f"in {manifest_path}"
        )
    miner_paths: list[Path] = []
    for item in raw_miner_templates:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"load_pipeline_prompt_manifest: invalid item in 'problem_miner_templates': "
                f"{item!r} in {manifest_path}"
            )
        p = prompts_dir / item
        if not p.exists():
            raise FileNotFoundError(
                f"Missing pipeline prompt template: {p} (problem_miner_templates)"
            )
        miner_paths.append(p)

    if not miner_paths:
        raise ValueError(
            f"load_pipeline_prompt_manifest: 'problem_miner_templates' is empty in {manifest_path}"
        )

    # Optional stage templates.
    relation_reviewer = _resolve_optional_template(prompts_dir, raw, "relation_reviewer_template")
    prioritizer = _resolve_optional_template(prompts_dir, raw, "prioritizer_template")
    solution_optioner = _resolve_optional_template(prompts_dir, raw, "solution_optioner_template")
    solution_selector = _resolve_optional_template(prompts_dir, raw, "solution_selector_template")
    solution_falsifier = _resolve_optional_template(prompts_dir, raw, "solution_falsifier_template")
    change_planner = _resolve_optional_template(prompts_dir, raw, "change_planner_template")
    sel_labeler = _resolve_optional_template(prompts_dir, raw, "selected_solution_labeler_template")
    ux_reviewer = _resolve_optional_template(prompts_dir, raw, "ux_reviewer_template")

    # Required config references.
    # These use repo-relative paths defined in the manifest.
    repo_root = prompts_dir.parent.parent  # configs/ → repo root
    stage_guidance_manifest_rel = raw.get("stage_guidance_manifest")
    if stage_guidance_manifest_rel:
        stage_guidance_manifest_path = repo_root / stage_guidance_manifest_rel
    else:
        stage_guidance_manifest_path = (
            repo_root / "configs" / "backlog_stage_guidance" / "manifest.json"
        )
    if not stage_guidance_manifest_path.exists():
        raise FileNotFoundError(f"Missing stage-guidance manifest: {stage_guidance_manifest_path}")

    taxonomy_rel = raw.get("taxonomy_file")
    if taxonomy_rel:
        taxonomy_path = repo_root / taxonomy_rel
    else:
        taxonomy_path = repo_root / "configs" / "backlog_taxonomy.json"
    if not taxonomy_path.exists():
        raise FileNotFoundError(f"Missing taxonomy config: {taxonomy_path}")

    relation_config_rel = raw.get("relation_review_config")
    if relation_config_rel:
        relation_review_config_path = repo_root / relation_config_rel
    else:
        relation_review_config_path = repo_root / "configs" / "backlog_relation_review.yaml"
    if not relation_review_config_path.exists():
        raise FileNotFoundError(f"Missing relation-review config: {relation_review_config_path}")

    _LOG.info(
        "load_pipeline_prompt_manifest: loaded version=%d from %s "
        "(miner_templates=%d, taxonomy=%s)",
        _MANIFEST_VERSION,
        manifest_path,
        len(miner_paths),
        taxonomy_path,
    )

    return PipelinePromptManifest(
        prompts_dir=prompts_dir,
        problem_miner_templates=tuple(miner_paths),
        relation_reviewer_template=relation_reviewer,
        prioritizer_template=prioritizer,
        solution_optioner_template=solution_optioner,
        solution_selector_template=solution_selector,
        solution_falsifier_template=solution_falsifier,
        change_planner_template=change_planner,
        selected_solution_labeler_template=sel_labeler,
        ux_reviewer_template=ux_reviewer,
        stage_guidance_manifest_path=stage_guidance_manifest_path,
        taxonomy_path=taxonomy_path,
        relation_review_config_path=relation_review_config_path,
    )


# ---------------------------------------------------------------------------
# Generic stage prompt runner
# ---------------------------------------------------------------------------


def run_stage_prompt_json_result(
    *,
    stage: str,
    prompt: str,
    out_dir: Path,
    tag: str,
    agent: str,
    model: str | None,
    cfg: Any,
    workspace_dir: Path | None = None,
    allowed_tools: list[str] | None = None,
    include_directories: list[str] | None = None,
    resume_session_id: str | None = None,
    allow_empty: bool = False,
) -> StagePromptRun:
    """Run one stage prompt and retain exact session, workspace, auth, and bytes.

    This is a generic helper used by each stage helper in the CLI.  Stage-specific
    orchestration passes the fully-rendered prompt and receives the raw LLM response
    string.  The caller is responsible for parsing the response with the appropriate
    ``stage_contracts`` parser.

    The prompt and raw response are written to *out_dir* / *tag* for auditability.

    Parameters
    ----------
    stage:
        Stage identifier string (for logging and file naming).
    prompt:
        Fully-rendered prompt text.
    out_dir:
        Directory where prompt and response artifacts are written.
    tag:
        Short tag for file naming (e.g. ``"problem_mining_001"``).
    agent:
        Agent identifier (e.g. ``"claude"``, ``"codex"``, ``"gemini"``).
    model:
        Optional model override.
    cfg:
        ``RunnerConfig`` instance.
    workspace_dir:
        Optional workspace directory passed to the agent backend.  When ``None``,
        the default workspace is used.
    allowed_tools:
        Optional list of tool names to allow for this stage prompt (agent-specific).
        When ``None``, the agent backend uses its default tool configuration.
    include_directories:
        Optional list of directories that tools are allowed to access (agent-specific).
        When ``None``, the agent backend uses its default directory policy.

    Returns
    -------
    str
        Raw text response from the agent.

    Raises
    ------
    RuntimeError
        When the agent backend returns an empty response.
    """
    from backlog_miner.agent import BacklogProviderExternalWait, run_backlog_prompt_result

    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / f"{tag}.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8", newline="\n")
    invocation_path = model_invocation_manifest_path(out_dir=out_dir, tag=tag)
    invocation_path.unlink(missing_ok=True)
    _LOG.info("run_stage_prompt_json: stage=%s tag=%s agent=%s", stage, tag, agent)

    response: str | None = None
    prompt_result: Any | None = None
    invocation_started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    invocation_started_monotonic = time.monotonic()
    try:
        prompt_result = run_backlog_prompt_result(
            prompt=prompt,
            agent=agent,
            model=model,
            cfg=cfg,
            out_dir=out_dir,
            tag=tag,
            workspace_dir=workspace_dir,
            allowed_tools=allowed_tools,
            include_directories=include_directories,
            resume_session_id=resume_session_id,
        )
        response = prompt_result.response
        if (not response or not response.strip()) and not allow_empty:
            raise RuntimeError(
                f"run_stage_prompt_json: empty response from agent for stage={stage} tag={tag}"
            )
        response_path = out_dir / f"{tag}.response.txt"
        response_path.write_text(response, encoding="utf-8", newline="\n")
        _write_model_invocation_manifest(
            stage=stage,
            tag=tag,
            agent=agent,
            out_dir=out_dir,
            prompt=prompt,
            response=response,
            error_kind=None,
            agent_session_id=prompt_result.agent_session_id,
            resumed_from_session_id=resume_session_id,
            workspace_dir=prompt_result.workspace_dir,
            model=model,
            invocation_started_at=invocation_started_at,
            invocation_ended_at=datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            elapsed_seconds=prompt_result.elapsed_seconds,
        )
        invocation_errors = verify_model_invocation_manifest(invocation_path)
        if invocation_errors:
            raise RuntimeError(
                "run_stage_prompt_json: model invocation provenance invalid: "
                + ", ".join(invocation_errors)
            )
    except BacklogProviderExternalWait as exc:
        _write_model_invocation_manifest(
            stage=stage,
            tag=tag,
            agent=agent,
            out_dir=out_dir,
            prompt=prompt,
            response=response,
            error_kind="BacklogProviderExternalWait",
            agent_session_id=(
                str(exc.external_wait.get("agent_session_id") or "").strip() or None
            ),
            resumed_from_session_id=resume_session_id,
            workspace_dir=(
                Path(str(exc.external_wait["workspace_dir"]))
                if exc.external_wait.get("workspace_dir")
                else workspace_dir
            ),
            model=model,
            invocation_started_at=invocation_started_at,
            invocation_ended_at=datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            elapsed_seconds=max(0.0, time.monotonic() - invocation_started_monotonic),
        )
        raise
    except Exception as exc:
        _write_model_invocation_manifest(
            stage=stage,
            tag=tag,
            agent=agent,
            out_dir=out_dir,
            prompt=prompt,
            response=response,
            error_kind=type(exc).__name__,
            agent_session_id=(
                prompt_result.agent_session_id if prompt_result is not None else None
            ),
            resumed_from_session_id=resume_session_id,
            workspace_dir=(
                prompt_result.workspace_dir if prompt_result is not None else workspace_dir
            ),
            model=model,
            invocation_started_at=invocation_started_at,
            invocation_ended_at=datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            elapsed_seconds=max(0.0, time.monotonic() - invocation_started_monotonic),
        )
        raise

    assert response is not None and prompt_result is not None
    _LOG.info("run_stage_prompt_json: stage=%s tag=%s response_len=%d", stage, tag, len(response))
    return StagePromptRun(
        response=response,
        agent_session_id=prompt_result.agent_session_id,
        resumed_from_session_id=resume_session_id,
        workspace_dir=prompt_result.workspace_dir,
        invocation_manifest_path=invocation_path,
        prompt_path=prompt_result.prompt_path,
        response_path=prompt_result.response_path,
        raw_events_path=prompt_result.raw_events_path,
        last_message_path=prompt_result.last_message_path,
        stderr_path=prompt_result.stderr_path,
        elapsed_seconds=prompt_result.elapsed_seconds,
    )


def run_stage_prompt_json(
    *,
    stage: str,
    prompt: str,
    out_dir: Path,
    tag: str,
    agent: str,
    model: str | None,
    cfg: Any,
    workspace_dir: Path | None = None,
    allowed_tools: list[str] | None = None,
    include_directories: list[str] | None = None,
    resume_session_id: str | None = None,
    allow_empty: bool = False,
    structured: bool = False,
) -> str | StagePromptRun:
    """Backward-compatible text-only wrapper around :func:`run_stage_prompt_json_result`."""

    result = run_stage_prompt_json_result(
        stage=stage,
        prompt=prompt,
        out_dir=out_dir,
        tag=tag,
        agent=agent,
        model=model,
        cfg=cfg,
        workspace_dir=workspace_dir,
        allowed_tools=allowed_tools,
        include_directories=include_directories,
        resume_session_id=resume_session_id,
        allow_empty=allow_empty,
    )
    return result if structured else result.response
