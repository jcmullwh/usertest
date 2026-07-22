from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from run_artifacts.lifecycle_events import (
    LifecycleContext,
    ModelUsageReceipt,
    append_lifecycle_event,
    canonical_sha256,
    load_context_from_env,
    make_lifecycle_event,
    write_content_addressed_model_usage_receipt,
)
from token_monitoring import TokenUsage, parse_codex_invocation_usage


def _stable_id(prefix: str, *parts: object) -> str:
    encoded = "\x00".join(str(part) for part in parts).encode("utf-8", errors="replace")
    return f"{prefix}:{sha256(encoded).hexdigest()}"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _context(
    *,
    out_dir: Path,
    stage: str,
    invocation_id: str,
    session_id: str | None,
    invocation_fingerprint: Mapping[str, str],
) -> tuple[LifecycleContext, bool]:
    inherited = load_context_from_env(required=False)
    automatic = bool(
        inherited is not None
        and inherited.system_fingerprint.get("controller_context_verified") == "true"
    )
    cycle_id = (
        inherited.cycle_id
        if inherited is not None and inherited.cycle_id is not None
        else _stable_id("backlog-invocation-cycle", str(out_dir.resolve()))
    )
    return (
        LifecycleContext(
            case_lifecycle_id=(inherited.case_lifecycle_id if inherited is not None else None),
            case_id=inherited.case_id if inherited is not None else None,
            cycle_id=cycle_id,
            stage=stage,
            work_unit_id=_stable_id("model-work", invocation_id),
            invocation_id=invocation_id,
            session_id=session_id,
            shared_work_id=inherited.shared_work_id if inherited is not None else None,
            parent_action_id=(inherited.parent_action_id if inherited is not None else None),
            system_fingerprint=(
                dict(inherited.system_fingerprint)
                if inherited is not None
                else dict(invocation_fingerprint)
            ),
        ),
        automatic,
    )


def _actor_fields(*, automatic: bool) -> dict[str, str]:
    return {
        "actor_type": "model",
        "initiator_type": "controller" if automatic else "unknown",
        "root_initiator_type": "controller" if automatic else "unknown",
        "origin": "automatic" if automatic else "unknown_external",
        "provenance_quality": "authoritative" if automatic else "unknown",
    }


def _source_sha256(path: Path) -> str | None:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _lifecycle_token_map(usage: TokenUsage | None) -> dict[str, int]:
    if usage is None:
        return {}
    return {
        "total_tokens": usage.total_tokens,
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "uncached_input_tokens": usage.uncached_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_output_tokens,
    }


def _manifest_raw_events_path(
    manifest: Mapping[str, Any],
    *,
    out_dir: Path,
) -> Path:
    artifacts = manifest.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    raw_ref = artifacts.get("raw_events")
    raw_ref = raw_ref if isinstance(raw_ref, Mapping) else {}
    raw_path_value = raw_ref.get("path")
    if not isinstance(raw_path_value, str):
        return out_dir / f"{manifest.get('tag')}.raw_events.jsonl"
    raw_path = Path(raw_path_value)
    return raw_path if raw_path.is_absolute() else out_dir / raw_path


def _raw_events_match_manifest(manifest: Mapping[str, Any], raw_path: Path) -> bool:
    artifacts = manifest.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    raw_ref = artifacts.get("raw_events")
    raw_ref = raw_ref if isinstance(raw_ref, Mapping) else {}
    expected_digest = raw_ref.get("sha256")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        return False
    try:
        return sha256(raw_path.read_bytes()).hexdigest() == expected_digest
    except OSError:
        return False


def _prior_session_high_water(
    *,
    manifest: Mapping[str, Any],
    out_dir: Path,
    session_id: str | None,
) -> tuple[TokenUsage | None, Path | None]:
    if session_id is None or manifest.get("resumed_from_session_id") != session_id:
        return None, None
    current_id = manifest.get("invocation_id")
    current_started = _timestamp(manifest.get("invocation_started_at"))
    candidates: list[tuple[datetime, TokenUsage, Path]] = []
    for prior_path in out_dir.glob("*.model_invocation.json"):
        prior = _read_json(prior_path)
        if (
            prior.get("invocation_id") == current_id
            or prior.get("agent") != "codex"
            or prior.get("agent_session_id") != session_id
        ):
            continue
        prior_ended = _timestamp(prior.get("invocation_ended_at"))
        if prior_ended is None or (current_started is not None and prior_ended > current_started):
            continue
        raw_path = _manifest_raw_events_path(prior, out_dir=out_dir)
        if not _raw_events_match_manifest(prior, raw_path):
            continue
        parsed = parse_codex_invocation_usage(
            raw_path,
            invocation_id=str(prior.get("invocation_id") or prior_path.stem),
            session_id=session_id,
        )
        if parsed.observed_high_water is not None:
            candidates.append((prior_ended, parsed.observed_high_water, raw_path))
    if not candidates:
        return None, None
    _, high_water, evidence_path = max(candidates, key=lambda item: item[0])
    return high_water, evidence_path


def _usage_receipt(
    *,
    manifest: Mapping[str, Any],
    context: LifecycleContext,
    out_dir: Path,
) -> tuple[Path, ModelUsageReceipt, Path | None, str | None]:
    raw_path = _manifest_raw_events_path(manifest, out_dir=out_dir)
    agent = str(manifest.get("agent") or "unknown")
    session_id = (
        str(manifest["agent_session_id"])
        if isinstance(manifest.get("agent_session_id"), str)
        else None
    )
    if agent == "codex":
        baseline, baseline_evidence = _prior_session_high_water(
            manifest=manifest,
            out_dir=out_dir,
            session_id=session_id,
        )
        result = parse_codex_invocation_usage(
            raw_path,
            invocation_id=str(manifest["invocation_id"]),
            baseline_high_water=baseline,
            session_id=session_id,
        )
        usage = result.usage
        provider = result.provider
        semantics = result.semantics
        baseline = result.baseline_high_water
        observed = result.observed_high_water
        unknown_reason = None
        if manifest.get("resumed_from_session_id") == session_id and baseline is None:
            usage = None
            semantics = "unattributable"
            unknown_reason = "continued_session_missing_prior_high_water"
    else:
        usage = None
        provider = agent
        semantics = "unattributable"
        baseline = None
        observed = None
        baseline_evidence = None
        unknown_reason = "provider_usage_unsupported"
    source_digest = _source_sha256(raw_path)
    receipt = ModelUsageReceipt(
        receipt_id=_stable_id("usage", manifest["invocation_id"], source_digest or "missing"),
        context=context,
        provider=provider,
        model=str(manifest.get("model") or agent),
        usage_semantics=semantics,
        recorded_at=str(
            manifest.get("invocation_ended_at")
            or manifest.get("invocation_started_at")
            or "1970-01-01T00:00:00Z"
        ),
        invocation_started_at=(
            str(manifest["invocation_started_at"])
            if isinstance(manifest.get("invocation_started_at"), str)
            else None
        ),
        invocation_ended_at=(
            str(manifest["invocation_ended_at"])
            if isinstance(manifest.get("invocation_ended_at"), str)
            else None
        ),
        input_tokens=usage.input_tokens if usage is not None else None,
        cached_input_tokens=usage.cached_input_tokens if usage is not None else None,
        uncached_input_tokens=(usage.uncached_input_tokens if usage is not None else None),
        output_tokens=usage.output_tokens if usage is not None else None,
        reasoning_tokens=(usage.reasoning_output_tokens if usage is not None else None),
        total_tokens=usage.total_tokens if usage is not None else None,
        baseline_usage=_lifecycle_token_map(baseline),
        observed_usage=_lifecycle_token_map(observed),
        source_artifact_path=str(raw_path) if source_digest is not None else None,
        source_artifact_sha256=source_digest,
        provenance_quality=("authoritative" if semantics != "unattributable" else "unknown"),
    )
    path = write_content_addressed_model_usage_receipt(out_dir / "model_usage_receipts", receipt)
    return path, receipt, baseline_evidence, unknown_reason


def _event_token_usage(receipt: ModelUsageReceipt) -> dict[str, int] | None:
    if receipt.usage_semantics == "unattributable":
        return None
    values = {
        "total_tokens": receipt.total_tokens,
        "input_tokens": receipt.input_tokens,
        "cached_input_tokens": receipt.cached_input_tokens,
        "uncached_input_tokens": receipt.uncached_input_tokens,
        "output_tokens": receipt.output_tokens,
        "reasoning_output_tokens": receipt.reasoning_tokens,
    }
    return (
        {key: int(value) for key, value in values.items() if value is not None}
        if all(value is not None for value in values.values())
        else None
    )


def _prior_failed_manifests(out_dir: Path, *, stage: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob("*.model_invocation.json")):
        value = _read_json(path)
        if value.get("stage") == stage and value.get("status") == "failed":
            results.append(value)
    return results


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _provider_wait_predecessor(
    manifest: Mapping[str, Any],
    failed_manifests: list[dict[str, Any]],
) -> tuple[dict[str, Any], float] | None:
    """Return the verified provider-wait interval immediately preceding a resume."""

    resumed_session = manifest.get("resumed_from_session_id")
    current_started = _timestamp(manifest.get("invocation_started_at"))
    if not isinstance(resumed_session, str) or current_started is None:
        return None
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for failed in failed_manifests:
        if failed.get("error_kind") != "BacklogProviderExternalWait":
            continue
        if failed.get("agent_session_id") != resumed_session:
            continue
        failed_ended = _timestamp(failed.get("invocation_ended_at"))
        if failed_ended is not None and failed_ended <= current_started:
            candidates.append((failed_ended, failed))
    if not candidates:
        return None
    failed_ended, predecessor = max(candidates, key=lambda item: item[0])
    return predecessor, max(0.0, (current_started - failed_ended).total_seconds())


def write_stage_invocation_telemetry(
    *,
    manifest_path: Path,
) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    invocation_id = str(manifest["invocation_id"])
    stage = str(manifest["stage"])
    out_dir = manifest_path.parent
    session_id = (
        str(manifest["agent_session_id"])
        if isinstance(manifest.get("agent_session_id"), str)
        else None
    )
    context, automatic = _context(
        out_dir=out_dir,
        stage=stage,
        invocation_id=invocation_id,
        session_id=session_id,
        invocation_fingerprint={
            key: str(value)
            for key, value in {
                "model": manifest.get("model"),
                "agent": manifest.get("agent"),
            }.items()
            if isinstance(value, str) and value.strip()
        },
    )
    events_path = out_dir / "lifecycle_events.jsonl"
    actor_fields = _actor_fields(automatic=automatic)
    started_at = manifest.get("invocation_started_at")
    ended_at = manifest.get("invocation_ended_at")
    receipt_path, receipt, baseline_evidence, usage_unknown_reason = _usage_receipt(
        manifest=manifest,
        context=context,
        out_dir=out_dir,
    )
    failed_manifests = _prior_failed_manifests(out_dir, stage=stage)
    provider_wait = _provider_wait_predecessor(manifest, failed_manifests)
    if provider_wait is not None:
        predecessor, wait_seconds = provider_wait
        wait_started_at = str(predecessor["invocation_ended_at"])
        wait_ended_at = str(manifest["invocation_started_at"])
        predecessor_id = str(predecessor["invocation_id"])
        wait_context = LifecycleContext.from_dict(
            {
                **context.to_dict(),
                "invocation_id": None,
                "work_unit_id": _stable_id("provider-wait-work", predecessor_id, invocation_id),
            }
        )
        predecessor_path = out_dir / (f"{predecessor.get('tag')}.model_invocation.json")
        append_lifecycle_event(
            events_path,
            make_lifecycle_event(
                "work.completed",
                wait_context,
                idempotency_key=(f"provider-wait:{predecessor_id}:{invocation_id}:completed"),
                occurred_at=wait_ended_at,
                started_at=wait_started_at,
                ended_at=wait_ended_at,
                external_wait_seconds=wait_seconds,
                actor_type="external_service",
                initiator_type="controller" if automatic else "unknown",
                root_initiator_type="controller" if automatic else "unknown",
                origin="external_service",
                provenance_quality="artifact_derived",
                evidence_paths=tuple(
                    str(path) for path in (predecessor_path, manifest_path) if path.is_file()
                ),
                attributes={
                    "wait_category": "provider",
                    "wait_seconds_by_category": {"provider": wait_seconds},
                    "provider": predecessor.get("agent"),
                    "prior_invocation_id": predecessor_id,
                    "resumed_invocation_id": invocation_id,
                    "token_scope": "qualification",
                    "cost_scope": "direct" if context.case_id is not None else "shared",
                },
            ),
        )
    relative_receipt = str(receipt_path.relative_to(out_dir)).replace("\\", "/")
    append_lifecycle_event(
        events_path,
        make_lifecycle_event(
            "model.invocation.completed",
            context,
            idempotency_key=f"{invocation_id}:completed",
            occurred_at=str(ended_at or started_at or receipt.recorded_at),
            started_at=str(started_at) if isinstance(started_at, str) else None,
            ended_at=str(ended_at) if isinstance(ended_at, str) else None,
            active_seconds=(
                float(manifest["elapsed_seconds"])
                if isinstance(manifest.get("elapsed_seconds"), (int, float))
                else None
            ),
            evidence_paths=tuple(
                str(path)
                for path in (manifest_path, receipt_path, baseline_evidence)
                if path is not None
            ),
            artifact_hashes={
                "model_invocation_manifest": sha256(manifest_path.read_bytes()).hexdigest(),
                "model_usage_receipt": canonical_sha256(receipt.to_dict()),
            },
            attributes={
                "status": manifest.get("status"),
                "error_kind": manifest.get("error_kind"),
                "usage_receipt_path": relative_receipt,
                "usage_semantics": receipt.usage_semantics,
                "usage_unknown_reason": usage_unknown_reason,
                "token_usage": _event_token_usage(receipt),
                "token_scope": "qualification",
                "cost_scope": "direct" if context.case_id is not None else "shared",
                "model": manifest.get("model"),
                "agent": manifest.get("agent"),
                "prompt_sha256": manifest.get("prompt_sha256"),
            },
            **actor_fields,
        ),
    )

    error_kind = str(manifest.get("error_kind") or "model_invocation_failed")
    cluster_id = _stable_id("error", str(out_dir.resolve()), stage, error_kind)
    if manifest.get("status") == "failed":
        append_lifecycle_event(
            events_path,
            make_lifecycle_event(
                "error.occurred",
                context,
                idempotency_key=f"{cluster_id}:occurrence:{invocation_id}",
                occurred_at=str(ended_at or started_at or receipt.recorded_at),
                error_cluster_id=cluster_id,
                attributes={"error_kind": error_kind, "terminal": False},
                **actor_fields,
            ),
        )
    else:
        for failed in failed_manifests:
            prior_kind = str(failed.get("error_kind") or "model_invocation_failed")
            prior_cluster = _stable_id("error", str(out_dir.resolve()), stage, prior_kind)
            same_author = bool(
                session_id is not None
                and failed.get("agent_session_id") == session_id
                and manifest.get("resumed_from_session_id") == session_id
            )
            append_lifecycle_event(
                events_path,
                make_lifecycle_event(
                    "error.resolved",
                    context,
                    idempotency_key=f"{prior_cluster}:resolved",
                    occurred_at=str(ended_at or started_at or receipt.recorded_at),
                    error_cluster_id=prior_cluster,
                    attributes={
                        "error_kind": prior_kind,
                        "resolution_mode": (
                            "self_healed_same_author" if same_author else "self_healed_controller"
                        ),
                    },
                    **actor_fields,
                ),
            )
    return {
        "events_path": str(events_path),
        "usage_receipt_path": str(receipt_path),
        "usage_semantics": receipt.usage_semantics,
    }


__all__ = ["write_stage_invocation_telemetry"]
