from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_artifacts.capture import TextCapturePolicy, TextExcerpt, capture_text_artifact

_TIMESTAMP_DIR_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
_EMBED_DEFINITION_KEYS = {
    "persona_source_md",
    "persona_resolved_md",
    "mission_source_md",
    "mission_resolved_md",
    "prompt_template_md",
    "report_schema_json",
}


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


def _history_text_policy(max_embed_bytes: int) -> TextCapturePolicy:
    head_bytes = max_embed_bytes // 2
    tail_bytes = max_embed_bytes - head_bytes
    return TextCapturePolicy(
        max_excerpt_bytes=max_embed_bytes,
        head_bytes=head_bytes,
        tail_bytes=tail_bytes,
        binary_detection_bytes=2_048,
    )


def _compose_history_excerpt(excerpt: TextExcerpt) -> str:
    if not excerpt.truncated:
        return excerpt.head
    marker = "\n...[truncated; see embedded_capture_manifest]...\n"
    return f"{excerpt.head}{marker}{excerpt.tail}"


def _capture_embedded_text(
    run_dir: Path,
    rel_path: str,
    *,
    policy: TextCapturePolicy,
) -> tuple[str | None, dict[str, Any]]:
    result = capture_text_artifact(run_dir / rel_path, policy=policy, root=run_dir)
    manifest: dict[str, Any] = {
        "path": result.artifact.path,
        "exists": result.artifact.exists,
        "size_bytes": result.artifact.size_bytes,
        "sha256": result.artifact.sha256,
        "truncated": bool(result.excerpt.truncated) if result.excerpt is not None else False,
        "error": result.error,
    }

    if not result.artifact.exists:
        return None, manifest
    if result.excerpt is not None:
        return _compose_history_excerpt(result.excerpt), manifest
    if isinstance(result.error, str) and result.error.strip():
        return f"[capture_error] {result.error}", manifest
    return "[capture_error] capture_unavailable", manifest


def _embed_allowed_keys(embed_rank: int) -> set[str]:
    if embed_rank <= 0:
        return set()
    keys = set(_EMBED_DEFINITION_KEYS)
    if embed_rank >= 2:
        keys.add("prompt_txt")
    if embed_rank >= 3:
        keys.add("users_md")
    return keys


def _prune_embedded_map(raw: Any, *, allowed_keys: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str) and key in allowed_keys
    }


def _iter_report_history_jsonl(
    source_path: Path,
    *,
    target_slug: str | None,
    normalized_repo_input: str | None,
    embed_rank: int,
) -> Iterator[dict[str, Any]]:
    allowed_embed_keys = _embed_allowed_keys(embed_rank)

    try:
        with source_path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                item_target_slug = item.get("target_slug")
                if target_slug is not None and item_target_slug != target_slug:
                    continue

                if normalized_repo_input is not None:
                    target_ref = item.get("target_ref")
                    candidate: str | None = None
                    if isinstance(target_ref, dict):
                        repo_raw = target_ref.get("repo_input")
                        candidate = repo_raw if isinstance(repo_raw, str) else None
                    if (
                        candidate is None
                        or _normalize_repo_input(candidate) != normalized_repo_input
                    ):
                        continue

                item["embedded"] = _prune_embedded_map(
                    item.get("embedded"),
                    allowed_keys=allowed_embed_keys,
                )
                item["embedded_capture_manifest"] = _prune_embedded_map(
                    item.get("embedded_capture_manifest"),
                    allowed_keys=allowed_embed_keys,
                )
                yield item
    except OSError:
        return


def iter_report_history(
    source: Path | str,
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

    source_path = Path(source)

    if source_path.is_file():
        yield from _iter_report_history_jsonl(
            source_path,
            target_slug=target_slug,
            normalized_repo_input=normalized_repo_input,
            embed_rank=embed_rank,
        )
        return

    policy = _history_text_policy(max_embed_bytes)
    for run_dir in iter_run_dirs(source_path, target_slug=target_slug):
        run_rel = None
        target = None
        ts_dir = None
        agent = None
        seed = None

        try:
            run_rel = str(run_dir.relative_to(source_path)).replace("\\", "/")
            parts = run_dir.relative_to(source_path).parts
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
        embedded_capture_manifest: dict[str, Any] = {}
        if embed_rank >= 1:
            embedded["persona_source_md"], embedded_capture_manifest["persona_source_md"] = (
                _capture_embedded_text(
                    run_dir,
                    "persona.source.md",
                    policy=policy,
                )
            )
            embedded["persona_resolved_md"], embedded_capture_manifest["persona_resolved_md"] = (
                _capture_embedded_text(
                    run_dir,
                    "persona.resolved.md",
                    policy=policy,
                )
            )
            embedded["mission_source_md"], embedded_capture_manifest["mission_source_md"] = (
                _capture_embedded_text(
                    run_dir,
                    "mission.source.md",
                    policy=policy,
                )
            )
            embedded["mission_resolved_md"], embedded_capture_manifest["mission_resolved_md"] = (
                _capture_embedded_text(
                    run_dir,
                    "mission.resolved.md",
                    policy=policy,
                )
            )
            embedded["prompt_template_md"], embedded_capture_manifest["prompt_template_md"] = (
                _capture_embedded_text(
                    run_dir,
                    "prompt.template.md",
                    policy=policy,
                )
            )
            embedded["report_schema_json"] = _read_json(run_dir / "report.schema.json")
        if embed_rank >= 2:
            embedded["prompt_txt"], embedded_capture_manifest["prompt_txt"] = (
                _capture_embedded_text(
                    run_dir,
                    "prompt.txt",
                    policy=policy,
                )
            )
        if embed_rank >= 3:
            embedded["users_md"], embedded_capture_manifest["users_md"] = _capture_embedded_text(
                run_dir,
                "users.md",
                policy=policy,
            )

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
            "embedded_capture_manifest": embedded_capture_manifest,
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
