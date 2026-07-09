from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_batch_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def batch_root(repo_root: Path) -> Path:
    return repo_root / "runs" / "_batch" / "usertest_implement"


def batch_dir(repo_root: Path, batch_id: str) -> Path:
    return batch_root(repo_root) / batch_id


def latest_batch_dir(repo_root: Path) -> Path | None:
    root = batch_root(repo_root)
    if not root.exists():
        return None
    dirs = sorted(
        [path for path in root.iterdir() if path.is_dir()],
        key=lambda path: path.name,
        reverse=True,
    )
    return dirs[0] if dirs else None


def state_path(batch_dir_path: Path) -> Path:
    return batch_dir_path / "batch_state.json"


def outcomes_path(batch_dir_path: Path) -> Path:
    return batch_dir_path / "ticket_outcomes.jsonl"


def blockers_path(batch_dir_path: Path) -> Path:
    return batch_dir_path / "global_blockers.json"


def summary_path(batch_dir_path: Path) -> Path:
    return batch_dir_path / "batch_summary.json"


def docker_resource_plan_path(batch_dir_path: Path) -> Path:
    return batch_dir_path / "docker_resource_plan.json"


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def build_initial_state(
    *,
    batch_id: str,
    batch_commit: str,
    batch_branch: str,
    base_ci_run_url: str | None,
    workers: list[dict[str, Any]],
    docker_resource_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = {
        "schema_version": 1,
        "batch_id": batch_id,
        "batch_commit": batch_commit,
        "batch_branch": batch_branch,
        "base_ci_run_url": base_ci_run_url,
        "phase": None,
        "status": "running",
        "created_utc": utc_now_z(),
        "updated_utc": utc_now_z(),
        "global_blockers": [],
        "workers": workers,
        "in_flight": [],
        "completed": [],
        "failed": [],
    }
    if docker_resource_plan is not None:
        state["docker_resource_plan"] = docker_resource_plan
    return state


def persist_state(batch_dir_path: Path, state: dict[str, Any]) -> None:
    state["updated_utc"] = utc_now_z()
    write_json(state_path(batch_dir_path), state)
    docker_resource_plan = state.get("docker_resource_plan")
    if isinstance(docker_resource_plan, dict):
        write_json(docker_resource_plan_path(batch_dir_path), docker_resource_plan)
    write_json(
        blockers_path(batch_dir_path),
        {
            "schema_version": 1,
            "batch_id": state.get("batch_id"),
            "generated_at": utc_now_z(),
            "global_blockers": state.get("global_blockers", []),
        },
    )
    summary = {
        "schema_version": 1,
        "batch_id": state.get("batch_id"),
        "status": state.get("status"),
        "phase": state.get("phase"),
        "completed_count": len(state.get("completed", [])),
        "failed_count": len(state.get("failed", [])),
        "global_blocker_count": len(state.get("global_blockers", [])),
        "generated_at": utc_now_z(),
    }
    if isinstance(docker_resource_plan, dict):
        summary["docker_resource_plan"] = docker_resource_plan
    write_json(summary_path(batch_dir_path), summary)
