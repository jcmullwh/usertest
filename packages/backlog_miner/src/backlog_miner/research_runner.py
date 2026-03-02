"""Stage-3 reproduce-plus-research runner.

This module implements the stage-3 orchestration described in
`.agents/ops/backlog-six-stage-pipeline/backlog-six-stage-pipeline.execplan.md`.

Stage 3 is operationally distinct from the prompt-only stages: it runs a dedicated
mission inside an isolated writable workspace (via ``runner_core.run_once``) and
extracts a strict research-dossier extension block from the resulting report.

Offline testability
-------------------
Tests must run offline. In ``dry_run`` mode, this module does not invoke any agent;
it instead writes request artifacts and returns deterministic placeholder dossiers
that satisfy the stage contract without claiming reproduction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Sequence

from backlog_core.stage_contracts import build_stage_document, parse_research_dossier_list
from runner_core import RunRequest, RunnerConfig, run_once

_LOG = logging.getLogger(__name__)

_MISSION_ID = "backlog_repro_research"
_PERSONA_ID = "repo_backlog_investigator"
_POLICY = "write"

_STAGE = "repro_research"

_GUIDANCE_PATH = Path("configs") / "backlog_stage_guidance" / "repro_research.md"
_REPO_INTENT_PATH = Path("configs") / "repo_intent.md"

_EXTENSION_KEY = "backlog_repro_research"


def _coerce_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _stable_seed(problem_id: str) -> int:
    """Derive a stable integer seed for a problem ID."""
    digest = sha256(problem_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _load_json_object(path: Path) -> dict[str, Any]:
    """Load JSON from *path* and require it is an object."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in {path}, got {type(raw).__name__}")
    return raw


def _load_diff_numstat(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Expected JSON list in {path}, got {type(raw).__name__}")
    return [item for item in raw if isinstance(item, dict)]


def _classify_diff(
    modified_paths: Sequence[str],
    *,
    writes_purpose: Sequence[str] | None = None,
) -> tuple[str, list[str]]:
    """Classify a stage-3 diff as allowed research edits vs suspicious implementation.

    The classification is intentionally conservative: any writes outside common
    research-only locations are considered suspicious.
    """
    normalized = [p.replace("\\", "/") for p in modified_paths if isinstance(p, str) and p.strip()]
    if not normalized:
        return "no_changes", []

    allowed_prefixes = (
        ".usertest/",
        "tests/",
        "test/",
        "scripts/",
        "tools/",
        "fixtures/",
        "fixture/",
        "configs/",
    )
    allowed_markers = (
        "/tests/",
        "/test/",
        "/fixtures/",
        "/fixture/",
        "/scripts/",
    )
    allowed_suffixes = (
        "_test.py",
        ".spec.ts",
        ".spec.js",
        ".test.ts",
        ".test.js",
    )

    suspicious: list[str] = []
    for path in normalized:
        if path.startswith(allowed_prefixes):
            continue
        if any(marker in path for marker in allowed_markers):
            continue
        if path.endswith(allowed_suffixes):
            continue
        suspicious.append(path)

    if not suspicious:
        return "allowed_research_edits", []

    reasons = [f"suspicious_path: {p}" for p in suspicious]
    if writes_purpose:
        reasons.append("writes_purpose_claimed: " + ", ".join(sorted(set(writes_purpose))))
    return "suspicious_implementation", reasons


def _append_prompt_for_problem(
    *,
    repo_root: Path,
    problem_payload: dict[str, Any],
) -> str:
    """Build a system-prompt append string containing stage guidance + problem context."""
    guidance_path = repo_root / _GUIDANCE_PATH
    if not guidance_path.exists():
        raise FileNotFoundError(f"Missing stage guidance: {guidance_path}")
    guidance_text = guidance_path.read_text(encoding="utf-8")

    repo_intent_path = repo_root / _REPO_INTENT_PATH
    repo_intent_text = (
        repo_intent_path.read_text(encoding="utf-8") if repo_intent_path.exists() else ""
    )

    payload = json.dumps(problem_payload, ensure_ascii=False, indent=2)

    parts: list[str] = []
    parts.append("# Backlog reproduce-plus-research: context")
    parts.append("")
    parts.append("## Stage guidance (repo-owned)")
    parts.append(guidance_text.strip())
    parts.append("")
    if repo_intent_text.strip():
        parts.append("## Repo intent (repo-owned)")
        parts.append(repo_intent_text.strip())
        parts.append("")
    parts.append("## Assigned problem payload (JSON)")
    parts.append(payload)
    parts.append("")
    parts.append(
        "Reminder: stage-3 success is reproduction/bounding with evidence, NOT implementation."
    )
    return "\n".join(parts).strip() + "\n"


def run_repro_research_stage(
    *,
    repo_root: Path,
    repo_input: str | None,
    target_slug: str | None,
    selected_problems: Sequence[dict[str, Any]],
    artifacts_dir: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    dry_run: bool,
) -> dict[str, Any]:
    """Run stage 3 repro-plus-research for selected problems.

    Parameters
    ----------
    repo_root:
        Runner repo root (the monorepo that contains `.usertest/`).
    repo_input:
        Target repo input for acquisition (path, git URL, pip:/pdm: spec, etc.).
    target_slug:
        Optional runs target slug (for metadata only).
    selected_problems:
        Selected problems from stage 2. Each entry must contain ``problem_id``.
        The dict is passed through to the mission via an appended system prompt.
    artifacts_dir:
        Base compiled artifact directory (``*.backlog_artifacts``). Stage 3 writes
        request metadata under ``artifacts_dir / "repro_research"``.
    agent:
        Agent backend to run (``codex``, ``claude``, ``gemini``).
    model:
        Optional model override.
    cfg:
        Runner configuration.
    dry_run:
        When ``True``, do not invoke any agent; return deterministic placeholder dossiers.

    Returns
    -------
    dict[str, Any]
        Stage document dict (see ``backlog_core.stage_contracts.build_stage_document``).

    Raises
    ------
    ValueError
        When a run report omits the required extension block or reports
        ``implementation_performed=true``.
    FileNotFoundError
        When expected run artifacts are missing.
    """
    stage_artifacts_dir = artifacts_dir / _STAGE
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)

    requests: list[dict[str, Any]] = []
    dossiers: list[dict[str, Any]] = []

    if (
        not dry_run
        and selected_problems
        and (repo_input is None or not str(repo_input).strip())
    ):
        raise ValueError(
            "run_repro_research_stage: repo_input is required when dry_run=false. "
            "Provide --repo-input or ensure the caller inferred a single repo_input."
        )

    for idx, problem in enumerate(selected_problems, start=1):
        pid = _coerce_str(problem.get("problem_id"))
        if pid is None:
            raise ValueError(
                f"run_repro_research_stage: selected_problems[{idx}] missing problem_id"
            )

        seed = _stable_seed(pid)
        req_meta = {
            "problem_id": pid,
            "agent": agent,
            "model": model,
            "policy": _POLICY,
            "persona_id": _PERSONA_ID,
            "mission_id": _MISSION_ID,
            "seed": seed,
            "repo_input": repo_input,
        }
        requests.append(req_meta)

        if dry_run:
            placeholder = {
                "problem_id": pid,
                "reproduction_status": "reproduction_failed",
                "writes_used": False,
                "writes_purpose": ["none"],
                "implementation_performed": False,
                "diff_classification": "no_changes",
                "root_cause_hypotheses": [],
                "broader_class_assessment": "unknown",
                "unknowns": [
                    "dry_run: repro+research not executed (offline mode); rerun without --dry-run"
                ],
                "research_status": "researched",
                "run_dir": None,
            }
            validated, _ = parse_research_dossier_list(json.dumps([placeholder]))
            dossiers.append(validated[0])
            continue

        append_prompt = _append_prompt_for_problem(repo_root=repo_root, problem_payload=problem)
        request = RunRequest(
            repo=str(repo_input),
            ref=None,
            agent=str(agent),
            policy=_POLICY,
            persona_id=_PERSONA_ID,
            mission_id=_MISSION_ID,
            seed=seed,
            model=model,
            agent_append_system_prompt=append_prompt,
        )

        _LOG.info(
            "stage3: run_once problem_id=%s agent=%s policy=%s mission=%s persona=%s seed=%d",
            pid,
            agent,
            _POLICY,
            _MISSION_ID,
            _PERSONA_ID,
            seed,
        )
        result = run_once(config=cfg, request=request)
        run_dir = result.run_dir

        report_path = run_dir / "report.json"
        if not report_path.exists():
            raise FileNotFoundError(f"stage3: missing report.json for problem_id={pid}: {report_path}")

        report_obj = _load_json_object(report_path)
        ext_raw = report_obj.get("extensions")
        ext_map = ext_raw if isinstance(ext_raw, dict) else {}
        ext_block_raw = ext_map.get(_EXTENSION_KEY)
        if not isinstance(ext_block_raw, dict):
            raise ValueError(
                f"stage3: report missing required extensions.{_EXTENSION_KEY} "
                f"for problem_id={pid}: {report_path}"
            )

        ext_pid = _coerce_str(ext_block_raw.get("problem_id"))
        if ext_pid is not None and ext_pid != pid:
            _LOG.warning(
                "stage3: extension problem_id mismatch expected=%s got=%s (using expected)",
                pid,
                ext_pid,
            )

        if ext_block_raw.get("implementation_performed") is True:
            raise ValueError(
                f"stage3: extensions.{_EXTENSION_KEY}.implementation_performed=true "
                f"is forbidden (problem_id={pid}); stage 3 is reproduction+research, not implementation. "
                f"See {report_path}."
            )

        writes_purpose_raw = ext_block_raw.get("writes_purpose")
        writes_purpose = (
            [str(x) for x in writes_purpose_raw if isinstance(x, str) and x.strip()]
            if isinstance(writes_purpose_raw, list)
            else []
        )

        diff_numstat_path = run_dir / "diff_numstat.json"
        diff_numstat = _load_diff_numstat(diff_numstat_path)
        modified_paths: list[str] = []
        for entry in diff_numstat:
            p = _coerce_str(entry.get("path"))
            if p is not None:
                modified_paths.append(p)

        diff_class, diff_reasons = _classify_diff(modified_paths, writes_purpose=writes_purpose)

        dossier: dict[str, Any] = dict(ext_block_raw)
        dossier["problem_id"] = pid
        dossier["diff_classification"] = diff_class
        if diff_reasons:
            dossier["diff_suspicious_reasons"] = diff_reasons
        dossier["run_dir"] = str(run_dir)
        dossier["runner_exit_code"] = int(result.exit_code)
        dossier["runner_report_validation_errors"] = list(result.report_validation_errors)
        dossier["artifacts"] = {
            "report_json": str(report_path),
            "report_md": str(run_dir / "report.md"),
            "patch_diff": str(run_dir / "patch.diff"),
            "diff_numstat_json": str(diff_numstat_path),
            "normalized_events_jsonl": str(run_dir / "normalized_events.jsonl"),
            "agent_stderr_txt": str(run_dir / "agent_stderr.txt"),
        }

        validated, warnings = parse_research_dossier_list(json.dumps([dossier]))
        normalized = validated[0]
        if warnings:
            normalized["_parse_warning"] = "; ".join(warnings)
        dossiers.append(normalized)

    requests_path = stage_artifacts_dir / "repro_research_requests.json"
    requests_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stage": _STAGE,
                "dry_run": bool(dry_run),
                "repo_input": repo_input,
                "target_slug": target_slug,
                "requests": requests,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    # Ensure the stage doc records the exact runs_dir used so a reader can find the run directories.
    cfg_effective = replace(cfg, runs_dir=Path(cfg.runs_dir))
    stage_doc = build_stage_document(
        _STAGE,
        dossiers,
        input_meta={
            "selected_problem_count": len(selected_problems),
            "dry_run": bool(dry_run),
            "repo_input": repo_input,
            "target_slug": target_slug,
            "agent": agent,
            "model": model,
            "runner_runs_dir": str(cfg_effective.runs_dir),
            "mission_id": _MISSION_ID,
            "persona_id": _PERSONA_ID,
            "policy": _POLICY,
        },
        artifacts={"requests_json": str(requests_path)},
    )
    return stage_doc
