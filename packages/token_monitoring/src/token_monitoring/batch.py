from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
    return out


def analyze_batch_context(batch_dir: Path) -> dict[str, Any]:
    batch_dir = batch_dir.resolve()
    exceptions: list[dict[str, Any]] = []
    summary_path = batch_dir / "batch_summary.json"
    state_path = batch_dir / "batch_state.json"
    blockers_path = batch_dir / "global_blockers.json"
    outcomes_path = batch_dir / "ticket_outcomes.jsonl"

    summary: dict[str, Any] = {}
    state: dict[str, Any] = {}
    blockers: dict[str, Any] = {}
    for path, label in (
        (summary_path, "batch_summary"),
        (state_path, "batch_state"),
        (blockers_path, "global_blockers"),
    ):
        if not path.exists():
            exceptions.append({"code": f"missing_{label}", "path": str(path)})
            continue
        try:
            loaded = _load_json(path)
        except Exception as exc:  # noqa: BLE001
            exceptions.append({"code": f"invalid_{label}", "path": str(path), "message": str(exc)})
            continue
        if not isinstance(loaded, dict):
            exceptions.append({"code": f"non_object_{label}", "path": str(path)})
            continue
        if label == "batch_summary":
            summary = loaded
        elif label == "batch_state":
            state = loaded
        else:
            blockers = loaded

    outcomes = _iter_jsonl(outcomes_path)
    if not outcomes_path.exists():
        exceptions.append({"code": "missing_ticket_outcomes", "path": str(outcomes_path)})

    completed_count = summary.get("completed_count")
    if not isinstance(completed_count, int):
        completed_count = (
            len(state.get("completed")) if isinstance(state.get("completed"), list) else 0
        )
    run_dir_null_outcomes = [item for item in outcomes if item.get("run_dir") is None]
    blocked_zero_completed = (
        summary.get("status") == "blocked"
        and completed_count == 0
        and bool(blockers.get("global_blockers") or run_dir_null_outcomes)
    )
    signals: list[dict[str, Any]] = []
    if blocked_zero_completed:
        signals.append(
            {
                "signal_id": "control_plane_spin",
                "confidence": "attributable",
                "causal_mechanism": (
                    "Batch/control-plane work ended blocked with zero completed runs; "
                    "this is workflow "
                    "context, not a completed run token attribution."
                ),
                "token_dimensions_affected": {
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "uncached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                },
                "evidence_path": str(summary_path),
                "evidence": {
                    "completed_count": completed_count,
                    "failed_count": summary.get("failed_count"),
                    "run_dir_null_outcomes": len(run_dir_null_outcomes),
                    "global_blocker_count": summary.get("global_blocker_count"),
                },
                "mitigation_lever": (
                    "Monitor control-plane terminal artifact contracts separately "
                    "from run-level token drivers."
                ),
                "false_positive_risk": "Low when completed_count is zero and run_dir is null.",
                "confirmed_by_counters": False,
            }
        )

    return {
        "schema_version": 1,
        "generated_at_utc": _utc_now_z(),
        "batch_dir": str(batch_dir),
        "status": summary.get("status") or state.get("status"),
        "phase": summary.get("phase") or state.get("phase"),
        "completed_count": completed_count,
        "failed_count": summary.get("failed_count"),
        "run_dir_null_outcome_count": len(run_dir_null_outcomes),
        "signals": signals,
        "exceptions": exceptions,
        "privacy": {
            "contains_raw_prompt": False,
            "contains_raw_source": False,
            "contains_raw_command_output": False,
        },
    }


def write_batch_context(batch_dir: Path, *, output_dir: Path | None = None) -> dict[str, Any]:
    analysis = analyze_batch_context(batch_dir)
    destination = (output_dir or batch_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "token_batch_context.json").write_text(
        json.dumps(analysis, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    lines = [
        "# Token batch context",
        "",
        f"- Batch: `{analysis['batch_dir']}`",
        f"- Status: `{analysis.get('status')}`",
        f"- Completed count: `{analysis.get('completed_count')}`",
        "",
        "## Signals",
        "",
    ]
    if analysis["signals"]:
        for signal in analysis["signals"]:
            lines.append(f"- `{signal['signal_id']}`: {signal['causal_mechanism']}")
    else:
        lines.append("No batch/control-plane signal met the v1 evidence rules.")
    lines.append("")
    (destination / "token_batch_context.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
        newline="\n",
    )
    return analysis
