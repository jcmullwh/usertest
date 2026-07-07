# ruff: noqa: E501
from __future__ import annotations

import argparse
import itertools
import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from agent_adapters import normalize_claude_events, normalize_codex_events, normalize_gemini_events
from reporter import (
    analyze_report_history,
    compute_metrics,
    iter_events_jsonl,
    make_event,
    render_report_markdown,
    validate_report,
    write_issue_analysis,
)
from run_artifacts.history import iter_report_history, write_report_history_jsonl
from runner_core.catalog import discover_missions, discover_personas, load_catalog_config
from runner_core.pathing import slugify
from runner_core.target_acquire import acquire_target

from usertest.commands.shared import (
    _load_runner_config,
    _resolve_local_repo_root,
    _resolve_optional_path,
    _resolve_repo_root,
    _warn_legacy_runs_layout,
)


def add_report_commands(sub: argparse._SubParsersAction) -> None:
    report_p = sub.add_parser("report", help="(Re)render report.md for an existing run dir.")
    report_p.add_argument("--run-dir", required=True, type=Path, help="Run directory to render.")
    report_p.add_argument(
        "--recompute-metrics",
        action="store_true",
        help=(
            "Overwrite normalized_events.jsonl and regenerate metrics.json from raw_events.jsonl. "
            "For reproducibility, an existing normalized_events.jsonl timestamp stream is "
            "reused when available."
        ),
    )
    report_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    init_p = sub.add_parser(
        "init-usertest",
        help="Initialize target .usertest/ scaffold (catalog.yaml + missions/personas dirs).",
    )
    init_p.add_argument("--repo", required=True, type=Path, help="Path to local target repo.")
    init_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite .usertest scaffold files if they already exist.",
    )
    init_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    personas_p = sub.add_parser("personas", help="Persona catalog commands.")
    personas_sub = personas_p.add_subparsers(dest="personas_cmd", required=True)
    personas_list_p = personas_sub.add_parser(
        "list",
        help="List discovered personas.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Repo selection (--repo):\n"
            "  - Local path: read in-place.\n"
            "  - Git URL: cloned to a temp dir.\n"
            "\n"
            "Examples:\n"
            "  usertest personas list --repo C:\\path\\to\\repo\n"
            "  usertest personas list --repo https://github.com/org/repo\n"
            "  usertest personas list --repo-root .\n"
        ),
    )
    personas_list_p.add_argument(
        "--repo",
        help=(
            "Optional target repo (local path or git URL) to load .usertest/catalog.yaml from "
            "(if present)."
        ),
    )
    personas_list_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    missions_p = sub.add_parser("missions", help="Mission catalog commands.")
    missions_sub = missions_p.add_subparsers(dest="missions_cmd", required=True)
    missions_list_p = missions_sub.add_parser(
        "list",
        help="List discovered missions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Repo selection (--repo):\n"
            "  - Local path: read in-place.\n"
            "  - Git URL: cloned to a temp dir.\n"
            "\n"
            "Examples:\n"
            "  usertest missions list --repo C:\\path\\to\\repo\n"
            "  usertest missions list --repo https://github.com/org/repo\n"
            "  usertest missions list --repo-root .\n"
        ),
    )
    missions_list_p.add_argument(
        "--repo",
        help=(
            "Optional target repo (local path or git URL) to load .usertest/catalog.yaml from "
            "(if present)."
        ),
    )
    missions_list_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_p = sub.add_parser("reports", help="Report history commands.")
    reports_sub = reports_p.add_subparsers(dest="reports_cmd", required=True)
    reports_compile_p = reports_sub.add_parser(
        "compile",
        help="Compile report.json + metadata across runs into a JSONL history file.",
    )
    reports_compile_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (e.g. tiktok_vids).",
    )
    reports_compile_p.add_argument(
        "--repo-input",
        help="Optional match for target_ref.repo_input (path or git URL).",
    )
    reports_compile_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_compile_p.add_argument(
        "--out",
        type=Path,
        help=(
            "Output JSONL path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_compile_p.add_argument(
        "--embed",
        choices=["none", "definitions", "prompt", "all"],
        default="definitions",
        help=(
            "How much extra run context to embed (beyond JSON artifacts). "
            "none: only JSON; definitions: persona/mission/schema/template; "
            "prompt: + prompt.txt; all: + users.md."
        ),
    )
    reports_compile_p.add_argument(
        "--max-embed-bytes",
        type=int,
        default=200_000,
        help="Skip embedding any single text file larger than this many bytes.",
    )
    reports_compile_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_analyze_p = reports_sub.add_parser(
        "analyze",
        help="Analyze run outcomes and cluster recurring issues from batch/historical runs.",
    )
    reports_analyze_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (e.g. tiktok_vids).",
    )
    reports_analyze_p.add_argument(
        "--repo-input",
        help="Optional match for target_ref.repo_input (path or git URL).",
    )
    reports_analyze_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_analyze_p.add_argument(
        "--history",
        type=Path,
        help="Path to a compiled report history JSONL (from `reports compile`).",
    )
    reports_analyze_p.add_argument(
        "--out-json",
        type=Path,
        help=(
            "Output analysis JSON path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_analyze_p.add_argument(
        "--out-md",
        type=Path,
        help=("Output markdown summary path (defaults next to --out-json with .md extension)."),
    )
    reports_analyze_p.add_argument(
        "--actions",
        type=Path,
        help=(
            "Optional JSON action registry for addressed comments (date/plan metadata). "
            "Defaults to configs/issue_actions.json when present."
        ),
    )
    reports_analyze_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    report_p.set_defaults(func=_cmd_report)
    init_p.set_defaults(func=_cmd_init_users)
    personas_list_p.set_defaults(func=_cmd_personas_list)
    missions_list_p.set_defaults(func=_cmd_missions_list)
    reports_compile_p.set_defaults(func=_cmd_reports_compile)
    reports_analyze_p.set_defaults(func=_cmd_reports_analyze)

def _run_report_requires_shell_capability(run_dir: Path) -> bool:
    preflight_path = run_dir / "preflight.json"
    if not preflight_path.exists():
        return False
    try:
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(preflight, dict):
        return False
    requirements = preflight.get("mission_requirements")
    requirements_dict = requirements if isinstance(requirements, dict) else {}
    return bool(requirements_dict.get("requires_shell") is True)


def _cmd_report(args: argparse.Namespace) -> int:
    """Execute the report subcommand for a run directory."""
    repo_root = _resolve_repo_root(args.repo_root)
    _warn_legacy_runs_layout(repo_root)

    run_dir: Path = args.run_dir
    if not run_dir.is_absolute() and not run_dir.exists():
        run_dir = repo_root / run_dir
    run_dir = run_dir.resolve()

    if args.recompute_metrics:

        def _parse_ts(ts: str) -> datetime | None:
            ts = ts.strip()
            if not ts:
                return None
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        def _reproducible_ts_iter(existing: list[str]) -> Iterator[str] | None:
            cleaned = [ts.strip() for ts in existing if isinstance(ts, str) and ts.strip()]
            if not cleaned:
                return None

            last_raw = cleaned[-1]
            last_dt = _parse_ts(last_raw)

            def _iter() -> Iterator[str]:
                yield from cleaned
                if last_dt is None:
                    yield from itertools.repeat(last_raw)
                else:
                    base = last_dt.replace(microsecond=0)
                    for i in itertools.count(1):
                        yield (base + timedelta(seconds=i)).isoformat()

            return _iter()

        def _read_last_ts_from_jsonl(path: Path) -> str | None:
            last: str | None = None
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = obj.get("ts")
                        if isinstance(ts, str) and ts.strip():
                            last = ts.strip()
            except OSError:
                return None
            return last

        raw_events_path = run_dir / "raw_events.jsonl"
        if not raw_events_path.exists():
            raise FileNotFoundError(f"Missing {raw_events_path}")

        agent_name: str | None = None
        target_ref_path = run_dir / "target_ref.json"
        if target_ref_path.exists():
            target_ref_raw = json.loads(target_ref_path.read_text(encoding="utf-8"))
            if isinstance(target_ref_raw, dict):
                agent_name_raw = target_ref_raw.get("agent")
                agent_name = agent_name_raw if isinstance(agent_name_raw, str) else None

        workspace_root: Path | None = None
        if agent_name == "codex":
            try:
                with raw_events_path.open("r", encoding="utf-8") as f:
                    for _ in range(20):
                        line = f.readline()
                        if not line:
                            break
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        workdir = obj.get("workdir")
                        if isinstance(workdir, str) and workdir:
                            wd = workdir[4:] if workdir.startswith("\\\\?\\") else workdir
                            wd_path = Path(wd)
                            workspace_root = wd_path if wd_path.exists() else None
                            break
            except OSError:
                workspace_root = None

        normalized_events_path = run_dir / "normalized_events.jsonl"
        ts_iter: Iterator[str] | None = None
        raw_ts_f = None
        raw_ts_iter: Iterator[str] | None = None

        # Recompute is an overwrite of normalized_events.jsonl. For reproducibility, prefer
        # reusing the existing normalized event timestamps (when present) so that re-running
        # `--recompute-metrics` on unchanged inputs produces minimal diffs.
        if normalized_events_path.exists():
            try:
                ts_values: list[str] = []
                for event in iter_events_jsonl(normalized_events_path):
                    ts = event.get("ts")
                    if isinstance(ts, str) and ts.strip():
                        ts_values.append(ts.strip())
                ts_iter = _reproducible_ts_iter(ts_values)
            except Exception:  # noqa: BLE001
                ts_iter = None

        raw_events_ts_path = raw_events_path.with_suffix(".ts.jsonl")
        if ts_iter is None and raw_events_ts_path.exists():
            try:
                raw_ts_f = raw_events_ts_path.open("r", encoding="utf-8")
                raw_ts_iter = (line.strip() for line in raw_ts_f if line.strip())
            except OSError:
                raw_ts_f = None
                raw_ts_iter = None
        try:
            if agent_name == "codex":
                normalize_codex_events(
                    raw_events_path=raw_events_path,
                    normalized_events_path=normalized_events_path,
                    ts_iter=ts_iter,
                    raw_ts_iter=raw_ts_iter,
                    workspace_root=workspace_root,
                )
            elif agent_name == "claude":
                normalize_claude_events(
                    raw_events_path=raw_events_path,
                    normalized_events_path=normalized_events_path,
                    ts_iter=ts_iter,
                    raw_ts_iter=raw_ts_iter,
                    workspace_root=workspace_root,
                )
            elif agent_name == "gemini":
                normalize_gemini_events(
                    raw_events_path=raw_events_path,
                    normalized_events_path=normalized_events_path,
                    ts_iter=ts_iter,
                    raw_ts_iter=raw_ts_iter,
                    workspace_root=workspace_root,
                )
            else:
                raise ValueError(
                    "Cannot recompute metrics: could not determine agent type from target_ref.json."
                )
        finally:
            if raw_ts_f is not None:
                raw_ts_f.close()

        write_file_ts_iter: Iterator[str] | None = ts_iter
        if write_file_ts_iter is None:
            last_ts = _read_last_ts_from_jsonl(normalized_events_path)
            if isinstance(last_ts, str) and last_ts.strip():
                last_dt = _parse_ts(last_ts)

                def _iter_write_file_ts() -> Iterator[str]:
                    if last_dt is None:
                        yield from itertools.repeat(last_ts.strip())
                    else:
                        base = last_dt.replace(microsecond=0)
                        for i in itertools.count(1):
                            yield (base + timedelta(seconds=i)).isoformat()

                write_file_ts_iter = _iter_write_file_ts()

        diff_numstat: list[dict[str, Any]] = []
        diff_numstat_path = run_dir / "diff_numstat.json"
        if diff_numstat_path.exists():
            try:
                diff_raw = json.loads(diff_numstat_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                diff_raw = None

            if isinstance(diff_raw, list):
                diff_numstat = [x for x in diff_raw if isinstance(x, dict)]
                if diff_numstat:
                    with normalized_events_path.open("a", encoding="utf-8", newline="\n") as out_f:
                        for item in diff_numstat:
                            path = item.get("path")
                            lines_added = item.get("lines_added")
                            lines_removed = item.get("lines_removed")
                            if not isinstance(path, str):
                                continue
                            if not isinstance(lines_added, int) or not isinstance(
                                lines_removed, int
                            ):
                                continue
                            out_f.write(
                                json.dumps(
                                    make_event(
                                        "write_file",
                                        {
                                            "path": path,
                                            "lines_added": lines_added,
                                            "lines_removed": lines_removed,
                                        },
                                        ts=next(write_file_ts_iter)
                                        if write_file_ts_iter is not None
                                        else None,
                                    ),
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )

        recomputed_metrics = compute_metrics(iter_events_jsonl(normalized_events_path))
        if diff_numstat:
            recomputed_metrics["diff_numstat"] = diff_numstat
        metrics_path = run_dir / "metrics.json"
        metrics_path.write_text(
            json.dumps(recomputed_metrics, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    report_path = run_dir / "report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"Missing {report_path}. Did the run succeed?")

    report_raw = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report_raw, dict):
        raise ValueError(f"{report_path} must contain a JSON object.")

    schema: dict[str, Any] | None = None
    schema_path = run_dir / "report.schema.json"
    if schema_path.exists():
        schema_raw = json.loads(schema_path.read_text(encoding="utf-8"))
        schema = schema_raw if isinstance(schema_raw, dict) else None
        if schema is None:
            print(
                f"WARNING: {schema_path} is not a JSON object; skipping validation.",
                file=sys.stderr,
            )
    else:
        fallback = repo_root / "configs" / "report.schema.json"
        if fallback.exists():
            print(f"WARNING: Missing {schema_path}; falling back to {fallback}.", file=sys.stderr)
            schema_raw = json.loads(fallback.read_text(encoding="utf-8"))
            schema = schema_raw if isinstance(schema_raw, dict) else None

    errors = (
        validate_report(
            report_raw,
            schema,
            require_shell_capability=_run_report_requires_shell_capability(run_dir),
        )
        if schema is not None
        else []
    )

    metrics: dict[str, Any] | None = None
    metrics_path = run_dir / "metrics.json"
    if metrics_path.exists():
        metrics_raw = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics = cast(dict[str, Any], metrics_raw) if isinstance(metrics_raw, dict) else None

    target_ref: dict[str, Any] | None = None
    target_ref_path = run_dir / "target_ref.json"
    if target_ref_path.exists():
        target_ref_raw = json.loads(target_ref_path.read_text(encoding="utf-8"))
        target_ref = target_ref_raw if isinstance(target_ref_raw, dict) else None

    md = render_report_markdown(report=report_raw, metrics=metrics, target_ref=target_ref)
    (run_dir / "report.md").write_text(md, encoding="utf-8", newline="\n")

    if errors:
        (run_dir / "report_validation_errors.json").write_text(
            json.dumps(errors, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    print(str(run_dir / "report.md"))
    if errors:
        print("Report validation errors:")
        for e in errors:
            print(f"- {e}")
    return 0 if not errors else 2


def _render_target_catalog_yaml(*, persona_id: str | None, mission_id: str | None) -> str:
    """Render target-level catalog override YAML content."""
    resolved_persona = persona_id or "representative_workflow_evaluator"
    resolved_mission = mission_id or "verify_install_to_result"
    return "\n".join(
        [
            "version: 1",
            "",
            "# Target-local usertest overrides for this repo.",
            "#",
            "# How this file is used:",
            "# - The runner always loads the base catalog at <repo_root>/configs/catalog.yaml.",
            "# - If this target repo contains `.usertest/catalog.yaml`, it is merged in.",
            "#",
            "# Path semantics (important):",
            "# - Relative paths in `personas_dirs`, `missions_dirs`, `prompt_templates_dir`, and",
            "#   `report_schemas_dir` are resolved relative to the *target repo root* (the directory passed",
            "#   to `init-usertest --repo`). This is repo-root style, not relative to this file.",
            "# - `personas_dirs` / `missions_dirs` are additive: they are appended to the base catalog.",
            "# - `prompt_templates_dir` / `report_schemas_dir` override the base catalog when set.",
            "# - Duplicate persona/mission IDs across all directories are an error; use unique IDs or `extends`.",
            "#",
            "# Directory scanning:",
            "# - `personas_dirs` are searched recursively for `*.persona.md`.",
            "# - `missions_dirs` are searched recursively for `*.mission.md`.",
            "",
            "defaults:",
            f"  persona_id: {resolved_persona}",
            f"  mission_id: {resolved_mission}",
            "",
            "# You can add multiple directories if you want to separate team/person/project definitions.",
            "personas_dirs:",
            "  - .usertest/personas",
            "",
            "missions_dirs:",
            "  - .usertest/missions",
            "",
            "# Optional: override prompt templates / report schemas for this target repo.",
            "# prompt_templates_dir: .usertest/prompt_templates",
            "# report_schemas_dir: .usertest/report_schemas",
            "",
            "meta:",
            "  note: Put local `*.persona.md` and `*.mission.md` files under the directories above.",
            "",
        ]
    )


def _cmd_init_users(args: argparse.Namespace) -> int:
    """Execute init-users to scaffold target user directories."""
    repo_root = _resolve_repo_root(args.repo_root)

    target_dir: Path = args.repo
    if not target_dir.is_absolute():
        target_dir = target_dir.resolve()

    if not target_dir.exists() or not target_dir.is_dir():
        raise FileNotFoundError(f"Target repo directory not found: {target_dir}")

    base_catalog = load_catalog_config(repo_root, None)

    usertest_dir = target_dir / ".usertest"
    catalog_dest = usertest_dir / "catalog.yaml"
    install_manifest_dest = usertest_dir / "sandbox_cli_install.yaml"

    existing_paths = [p for p in (catalog_dest, install_manifest_dest) if p.exists()]
    if existing_paths and not args.force:
        first = existing_paths[0]
        print(f"{first} already exists; use --force to overwrite .usertest scaffold.")
        return 2

    usertest_dir.mkdir(parents=True, exist_ok=True)
    personas_dir = usertest_dir / "personas"
    missions_dir = usertest_dir / "missions"
    personas_dir.mkdir(parents=True, exist_ok=True)
    missions_dir.mkdir(parents=True, exist_ok=True)

    # Ensure empty directories survive a git commit when the scaffold is checked in.
    (personas_dir / ".gitkeep").write_text("", encoding="utf-8", newline="\n")
    (missions_dir / ".gitkeep").write_text("", encoding="utf-8", newline="\n")

    catalog_dest.write_text(
        _render_target_catalog_yaml(
            persona_id=base_catalog.defaults_persona_id,
            mission_id=base_catalog.defaults_mission_id,
        ),
        encoding="utf-8",
    )

    install_manifest_dest.write_text(
        "\n".join(
            [
                "version: 1",
                "sandbox_cli_install:",
                "  apt: []",
                "  pip: []",
                "  npm_global: []",
                "",
                "meta:",
                "  note: Optional sandbox package/tooling installs for docker sandbox runs.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(str(usertest_dir))
    return 0


def _cmd_personas_list(args: argparse.Namespace) -> int:
    """Execute personas list and print discovered personas."""
    repo_root = _resolve_repo_root(args.repo_root)
    repo_arg = args.repo if isinstance(args.repo, str) and args.repo.strip() else None

    try:
        if repo_arg is not None:
            local = _resolve_local_repo_root(repo_root, repo_arg)
            if local is not None:
                catalog_cfg = load_catalog_config(repo_root, local)
                personas = discover_personas(catalog_cfg)
            else:
                with tempfile.TemporaryDirectory(prefix="usertest_catalog_") as tmp_dir:
                    dest_dir = Path(tmp_dir) / "workspace"
                    acquired = acquire_target(repo=repo_arg, dest_dir=dest_dir, ref=None)
                    try:
                        catalog_cfg = load_catalog_config(repo_root, acquired.workspace_dir)
                        personas = discover_personas(catalog_cfg)
                    finally:
                        shutil.rmtree(acquired.workspace_dir, ignore_errors=True)
        else:
            catalog_cfg = load_catalog_config(repo_root, None)
            personas = discover_personas(catalog_cfg)

        for persona_id, spec in sorted(personas.items(), key=lambda kv: kv[0]):
            print(f"{persona_id}\t{spec.name}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(str(e), file=sys.stderr)
        return 2


def _cmd_missions_list(args: argparse.Namespace) -> int:
    """Execute missions list and print discovered missions."""
    repo_root = _resolve_repo_root(args.repo_root)
    repo_arg = args.repo if isinstance(args.repo, str) and args.repo.strip() else None

    try:
        if repo_arg is not None:
            local = _resolve_local_repo_root(repo_root, repo_arg)
            if local is not None:
                catalog_cfg = load_catalog_config(repo_root, local)
                missions = discover_missions(catalog_cfg)
            else:
                with tempfile.TemporaryDirectory(prefix="usertest_catalog_") as tmp_dir:
                    dest_dir = Path(tmp_dir) / "workspace"
                    acquired = acquire_target(repo=repo_arg, dest_dir=dest_dir, ref=None)
                    try:
                        catalog_cfg = load_catalog_config(repo_root, acquired.workspace_dir)
                        missions = discover_missions(catalog_cfg)
                    finally:
                        shutil.rmtree(acquired.workspace_dir, ignore_errors=True)
        else:
            catalog_cfg = load_catalog_config(repo_root, None)
            missions = discover_missions(catalog_cfg)

        for mission_id, spec in sorted(missions.items(), key=lambda kv: kv[0]):
            print(f"{mission_id}\t{spec.name}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(str(e), file=sys.stderr)
        return 2


def _cmd_reports_compile(args: argparse.Namespace) -> int:
    """Execute reports compile to build report history artifacts."""
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    runs_dir = args.runs_dir.resolve() if args.runs_dir is not None else cfg.runs_dir
    target_slug: str | None = None
    if isinstance(args.target, str) and args.target.strip():
        target_slug = str(args.target).strip()
    repo_input = (
        str(args.repo_input).strip()
        if isinstance(args.repo_input, str) and args.repo_input.strip()
        else None
    )

    out_path: Path
    if args.out is not None:
        out_path = _resolve_optional_path(repo_root, args.out) or args.out.resolve()
    else:
        default_name = slugify(repo_input) if repo_input is not None else (target_slug or "all")
        if target_slug is not None:
            out_path = runs_dir / target_slug / "_compiled" / f"{default_name}.report_history.jsonl"
        else:
            out_path = runs_dir / "_compiled" / f"{default_name}.report_history.jsonl"

    counts = write_report_history_jsonl(
        runs_dir,
        out_path=out_path,
        target_slug=target_slug,
        repo_input=repo_input,
        embed=str(args.embed),
        max_embed_bytes=int(args.max_embed_bytes),
    )

    print(str(out_path))
    print(json.dumps(counts, indent=2, ensure_ascii=False))
    return 0


def _cmd_reports_analyze(args: argparse.Namespace) -> int:
    """Execute reports analyze to generate issue analysis outputs."""
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    runs_dir = args.runs_dir.resolve() if args.runs_dir is not None else cfg.runs_dir
    history_path: Path | None
    if args.history is not None:
        history_path = _resolve_optional_path(repo_root, args.history) or args.history.resolve()
    else:
        history_path = None

    target_slug: str | None = None
    if isinstance(args.target, str) and args.target.strip():
        target_slug = str(args.target).strip()
    repo_input = (
        str(args.repo_input).strip()
        if isinstance(args.repo_input, str) and args.repo_input.strip()
        else None
    )

    default_name = slugify(repo_input) if repo_input is not None else (target_slug or "all")

    if args.out_json is not None:
        out_json = _resolve_optional_path(repo_root, args.out_json) or args.out_json.resolve()
    else:
        if history_path is not None:
            out_json = history_path.with_name(f"{history_path.stem}.issue_analysis.json")
        elif target_slug is not None:
            out_json = runs_dir / target_slug / "_compiled" / f"{default_name}.issue_analysis.json"
        else:
            out_json = runs_dir / "_compiled" / f"{default_name}.issue_analysis.json"

    if args.out_md is not None:
        out_md = _resolve_optional_path(repo_root, args.out_md) or args.out_md.resolve()
    else:
        out_md = out_json.with_suffix(".md")

    actions_path: Path | None
    if args.actions is not None:
        actions_path = _resolve_optional_path(repo_root, args.actions) or args.actions.resolve()
    else:
        default_actions = repo_root / "configs" / "issue_actions.json"
        actions_path = default_actions if default_actions.exists() else None

    history_source = history_path if history_path is not None else runs_dir
    records = list(
        iter_report_history(
            history_source,
            target_slug=target_slug,
            repo_input=repo_input,
            embed="none",
        )
    )
    summary = analyze_report_history(
        records,
        repo_root=repo_root,
        issue_actions_path=actions_path,
    )

    scope_bits = []
    if target_slug is not None:
        scope_bits.append(f"target={target_slug}")
    if repo_input is not None:
        scope_bits.append(f"repo_input={repo_input}")
    title_suffix = f" ({', '.join(scope_bits)})" if scope_bits else ""
    write_issue_analysis(
        summary,
        out_json_path=out_json,
        out_md_path=out_md,
        title=f"Usertest Issue Analysis{title_suffix}",
    )

    print(str(out_json))
    print(str(out_md))
    print(json.dumps(summary.get("totals", {}), indent=2, ensure_ascii=False))
    return 0

__all__ = ['add_report_commands', '_cmd_init_users', '_cmd_missions_list', '_cmd_personas_list', '_cmd_report', '_cmd_reports_analyze', '_cmd_reports_compile']
