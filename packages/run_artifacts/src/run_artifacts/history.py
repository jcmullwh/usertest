from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TIMESTAMP_DIR_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


def _parse_timestamp_dirname(name: str) -> str | None:
    if not _TIMESTAMP_DIR_RE.match(name):
        return None
    dt = datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _normalize_repo_input(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" in value:
        return value.lower()
    # Treat as filesystem path (Windows + POSIX).
    return os.path.normcase(os.path.normpath(value))


def iter_run_dirs(runs_dir: Path, *, target_slug: str | None = None) -> Iterator[Path]:
    """
    Yield run directories under a `runs/usertest` directory.

    Expected layout:
      <runs_dir>/<target_slug>/<timestamp>/<agent>/<seed>/target_ref.json
    """

    if target_slug is not None:
        target_dirs = [runs_dir / target_slug]
    else:
        try:
            target_dirs = [
                p for p in runs_dir.iterdir() if p.is_dir() and not p.name.startswith("_")
            ]
        except OSError:
            return

    for target_dir in sorted(target_dirs, key=lambda p: p.name):
        if not target_dir.exists() or not target_dir.is_dir():
            continue

        try:
            ts_dirs = [p for p in target_dir.iterdir() if p.is_dir() and not p.name.startswith("_")]
        except OSError:
            continue

        for ts_dir in sorted(ts_dirs, key=lambda p: p.name):
            try:
                agent_dirs = [
                    p for p in ts_dir.iterdir() if p.is_dir() and not p.name.startswith("_")
                ]
            except OSError:
                continue

            for agent_dir in sorted(agent_dirs, key=lambda p: p.name):
                try:
                    seed_dirs = [
                        p for p in agent_dir.iterdir() if p.is_dir() and not p.name.startswith("_")
                    ]
                except OSError:
                    continue

                for seed_dir in sorted(seed_dirs, key=lambda p: p.name):
                    if (seed_dir / "target_ref.json").exists():
                        yield seed_dir


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _read_text(path: Path, *, max_bytes: int) -> str | None:
    try:
        if path.stat().st_size > max_bytes:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError:
        return None


def iter_report_history(
    runs_dir: Path,
    *,
    target_slug: str | None = None,
    repo_input: str | None = None,
    embed: str = "definitions",
    max_embed_bytes: int = 200_000,
) -> Iterator[dict[str, Any]]:
    """
    Iterate run records suitable for longitudinal analysis.

    Each yielded item includes:
    - path-derived identifiers (target_slug/timestamp/agent/seed)
    - parsed JSON artifacts (target_ref, effective_run_spec, report, metrics, errors)
    - optional embedded text artifacts (persona/mission/prompt/users) depending on `embed`.
    """

    embed_rank = {"none": 0, "definitions": 1, "prompt": 2, "all": 3}.get(embed)
    if embed_rank is None:
        raise ValueError("embed must be one of: none, definitions, prompt, all")
    if max_embed_bytes <= 0:
        raise ValueError("max_embed_bytes must be > 0")

    normalized_repo_input: str | None = None
    if isinstance(repo_input, str) and repo_input.strip():
        normalized_repo_input = _normalize_repo_input(repo_input)

    for run_dir in iter_run_dirs(runs_dir, target_slug=target_slug):
        run_rel = None
        target = None
        ts_dir = None
        agent = None
        seed = None

        try:
            run_rel = str(run_dir.relative_to(runs_dir)).replace("\\", "/")
            parts = run_dir.relative_to(runs_dir).parts
            if len(parts) >= 4:
                target, ts_dir, agent, seed = parts[0], parts[1], parts[2], parts[3]
        except Exception:  # noqa: BLE001
            run_rel = None

        target_ref = _read_json(run_dir / "target_ref.json")
        if normalized_repo_input is not None:
            candidate = None
            if isinstance(target_ref, dict):
                raw = target_ref.get("repo_input")
                candidate = raw if isinstance(raw, str) else None
            if candidate is None or _normalize_repo_input(candidate) != normalized_repo_input:
                continue

        effective_run_spec = _read_json(run_dir / "effective_run_spec.json")
        report = _read_json(run_dir / "report.json")
        metrics = _read_json(run_dir / "metrics.json")
        preflight = _read_json(run_dir / "preflight.json")
        error = _read_json(run_dir / "error.json")
        report_validation_errors = _read_json(run_dir / "report_validation_errors.json")

        agent_exit_code: int | None = None
        if isinstance(error, dict):
            exit_code_raw = error.get("exit_code")
            agent_exit_code = exit_code_raw if isinstance(exit_code_raw, int) else None

        if isinstance(error, dict):
            status = "error"
        elif report_validation_errors is not None:
            status = "report_validation_error"
        elif report is None:
            status = "missing_report"
        else:
            status = "ok"

        embedded: dict[str, Any] = {}
        if embed_rank >= 1:
            embedded["persona_source_md"] = _read_text(
                run_dir / "persona.source.md", max_bytes=max_embed_bytes
            )
            embedded["persona_resolved_md"] = _read_text(
                run_dir / "persona.resolved.md", max_bytes=max_embed_bytes
            )
            embedded["mission_source_md"] = _read_text(
                run_dir / "mission.source.md", max_bytes=max_embed_bytes
            )
            embedded["mission_resolved_md"] = _read_text(
                run_dir / "mission.resolved.md", max_bytes=max_embed_bytes
            )
            embedded["prompt_template_md"] = _read_text(
                run_dir / "prompt.template.md", max_bytes=max_embed_bytes
            )
            embedded["report_schema_json"] = _read_json(run_dir / "report.schema.json")
        if embed_rank >= 2:
            embedded["prompt_txt"] = _read_text(run_dir / "prompt.txt", max_bytes=max_embed_bytes)
        if embed_rank >= 3:
            embedded["users_md"] = _read_text(run_dir / "users.md", max_bytes=max_embed_bytes)

        ts_utc = _parse_timestamp_dirname(ts_dir) if isinstance(ts_dir, str) else None

        yield {
            "run_dir": str(run_dir),
            "run_rel": run_rel,
            "target_slug": target,
            "timestamp_dir": ts_dir,
            "timestamp_utc": ts_utc,
            "agent": agent,
            "seed": int(seed) if isinstance(seed, str) and seed.isdigit() else seed,
            "status": status,
            "agent_exit_code": agent_exit_code,
            "target_ref": target_ref,
            "effective_run_spec": effective_run_spec,
            "report": report,
            "metrics": metrics,
            "preflight": preflight,
            "error": error,
            "report_validation_errors": report_validation_errors,
            "embedded": embedded,
        }


def write_report_history_jsonl(
    runs_dir: Path,
    *,
    out_path: Path,
    target_slug: str | None = None,
    repo_input: str | None = None,
    embed: str = "definitions",
    max_embed_bytes: int = 200_000,
) -> dict[str, int]:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    counts: dict[str, int] = {
        "ok": 0,
        "missing_report": 0,
        "report_validation_error": 0,
        "error": 0,
    }
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        for item in iter_report_history(
            runs_dir,
            target_slug=target_slug,
            repo_input=repo_input,
            embed=embed,
            max_embed_bytes=max_embed_bytes,
        ):
            total += 1
            status = item.get("status")
            if isinstance(status, str):
                counts[status] = counts.get(status, 0) + 1
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    counts["total"] = total
    return counts
