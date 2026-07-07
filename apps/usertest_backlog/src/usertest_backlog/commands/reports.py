# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.shared import *


def _render_intent_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    """
    Render a human-readable markdown view of an intent snapshot JSON.

    Parameters
    ----------
    snapshot:
        Snapshot object as written to `.intent_snapshot.json`.

    Returns
    -------
    str
        Markdown content.
    """

    generated_at = _coerce_string(snapshot.get("generated_at")) or "unknown"
    scope = snapshot.get("scope") if isinstance(snapshot.get("scope"), dict) else {}
    target = _coerce_string(scope.get("target")) or "all"
    repo_input = _coerce_string(scope.get("repo_input"))

    lines: list[str] = []
    lines.append("# Repo Intent Snapshot")
    lines.append("")
    lines.append(f"- Generated at: `{generated_at}`")
    lines.append(f"- Scope target: `{target}`")
    if repo_input is not None:
        lines.append(f"- Scope repo_input: `{repo_input}`")
    lines.append("")

    repo_intent = _coerce_string(snapshot.get("repo_intent_excerpt"))
    if repo_intent:
        lines.append("## Human-Owned Intent (excerpt)")
        lines.append("")
        lines.append(repo_intent.strip())
        lines.append("")

    lines.append("## Command Surface")
    lines.append("")
    cmds = snapshot.get("commands")
    cmd_list = [item for item in cmds if isinstance(item, dict)] if isinstance(cmds, list) else []
    if not cmd_list:
        lines.append("- (no commands extracted)")
        lines.append("")
    else:
        for cmd in cmd_list[:120]:
            command = _coerce_string(cmd.get("command")) or "unknown"
            help_text = _coerce_string(cmd.get("help")) or ""
            suffix = f": {help_text}" if help_text else ""
            lines.append(f"- `{command}`{suffix}")
        lines.append("")

    lines.append("## Docs Index")
    lines.append("")
    docs = snapshot.get("docs_index")
    docs_list = [item for item in docs if isinstance(item, dict)] if isinstance(docs, list) else []
    if not docs_list:
        lines.append("- (no docs indexed)")
        lines.append("")
    else:
        for item in docs_list[:120]:
            path = _coerce_string(item.get("path")) or "unknown"
            title = _coerce_string(item.get("title"))
            if title:
                lines.append(f"- `{path}`: {title}")
            else:
                lines.append(f"- `{path}`")
        lines.append("")

    llm_meta = snapshot.get("llm_summary_meta")
    if isinstance(llm_meta, dict):
        status = _coerce_string(llm_meta.get("status")) or "unknown"
        lines.append("## Optional Summary Pass")
        lines.append("")
        lines.append(f"- Status: `{status}`")
        prompt_hash = _coerce_string(llm_meta.get("prompt_hash"))
        if prompt_hash:
            lines.append(f"- Prompt hash: `{prompt_hash}`")
        agent = _coerce_string(llm_meta.get("agent"))
        if agent:
            lines.append(f"- Agent: `{agent}`")
        model = _coerce_string(llm_meta.get("model"))
        if model:
            lines.append(f"- Model: `{model}`")
        lines.append("")

    return "\n".join(lines) + "\n"


def _cmd_reports_compile(args: argparse.Namespace) -> int:
    """Execute the `reports compile` command handler.

    Parameters
    ----------
    args:
        Parsed command-line arguments namespace.

    Returns
    -------
    int
        Process exit code.
    """
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    runs_dir = args.runs_dir.resolve() if args.runs_dir is not None else cfg.runs_dir
    cfg = replace(cfg, runs_dir=runs_dir)
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
    """Execute the `reports analyze` command handler.

    Parameters
    ----------
    args:
        Parsed command-line arguments namespace.

    Returns
    -------
    int
        Process exit code.
    """
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


def _cmd_reports_window(args: argparse.Namespace) -> int:
    """Execute the `reports window` command handler.

    Parameters
    ----------
    args:
        Parsed command-line arguments namespace.

    Returns
    -------
    int
        Process exit code.
    """
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

    window_size = int(args.last)
    if window_size <= 0:
        print("--last must be > 0", file=sys.stderr)
        return 2

    baseline_size = window_size if args.baseline is None else int(args.baseline)
    if baseline_size < 0:
        print("--baseline must be >= 0", file=sys.stderr)
        return 2

    default_name = slugify(repo_input) if repo_input is not None else (target_slug or "all")

    if args.out_json is not None:
        out_json = _resolve_optional_path(repo_root, args.out_json) or args.out_json.resolve()
    else:
        if target_slug is not None:
            out_json = runs_dir / target_slug / "_compiled" / f"{default_name}.window_summary.json"
        else:
            out_json = runs_dir / "_compiled" / f"{default_name}.window_summary.json"

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

    limit = window_size + baseline_size
    run_dirs = select_recent_run_dirs(
        runs_dir,
        target_slug=target_slug,
        repo_input=repo_input,
        limit=limit,
    )
    if not run_dirs:
        print(
            f"No runs found under {runs_dir} "
            f"(target={target_slug or 'all'}, repo_input={repo_input or 'any'}).",
            file=sys.stderr,
        )
        return 1

    records: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        record = load_run_record(run_dir, runs_dir=runs_dir)
        if record is None:
            continue
        records.append(record)

    if not records:
        print("No readable run records found.", file=sys.stderr)
        return 1

    if baseline_size <= 0 or window_size >= len(records):
        baseline_records: list[dict[str, Any]] = []
        current_records = records
    else:
        current_records = records[-window_size:]
        baseline_records = records[: len(records) - window_size]
        if len(baseline_records) > baseline_size:
            baseline_records = baseline_records[-baseline_size:]

    summary = build_window_summary(
        current_records=current_records,
        baseline_records=baseline_records,
        repo_root=repo_root,
        issue_actions_path=actions_path,
        window_size=window_size,
        baseline_size=baseline_size,
    )

    scope_bits = []
    if target_slug is not None:
        scope_bits.append(f"target={target_slug}")
    if repo_input is not None:
        scope_bits.append(f"repo_input={repo_input}")
    title_suffix = f" ({', '.join(scope_bits)})" if scope_bits else ""
    title = f"Usertest Window Summary (last={window_size}, baseline={baseline_size}){title_suffix}"
    write_window_summary(
        summary,
        out_json_path=out_json,
        out_md_path=out_md,
        title=title,
    )

    print(str(out_json))
    print(str(out_md))
    current_summary: dict[str, Any] = {}
    summary_obj = summary.get("summary")
    if isinstance(summary_obj, dict):
        cur = summary_obj.get("current")
        if isinstance(cur, dict):
            for key in (
                "runs",
                "ok_rate",
                "timing_coverage_runs",
                "median_run_wall_seconds",
                "median_attempts_per_run",
            ):
                value = cur.get(key)
                if value is not None:
                    current_summary[key] = value
    print(json.dumps(current_summary, indent=2, ensure_ascii=False))
    return 0


def _cmd_reports_intent_snapshot(args: argparse.Namespace) -> int:
    """Execute the `reports intent snapshot` command handler.

    Parameters
    ----------
    args:
        Parsed command-line arguments namespace.

    Returns
    -------
    int
        Process exit code.
    """
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

    default_name = slugify(repo_input) if repo_input is not None else (target_slug or "all")

    if args.out_json is not None:
        out_json = _resolve_optional_path(repo_root, args.out_json) or args.out_json.resolve()
    else:
        if target_slug is not None:
            out_json = runs_dir / target_slug / "_compiled" / f"{default_name}.intent_snapshot.json"
        else:
            out_json = runs_dir / "_compiled" / f"{default_name}.intent_snapshot.json"

    if args.out_md is not None:
        out_md = _resolve_optional_path(repo_root, args.out_md) or args.out_md.resolve()
    else:
        out_md = out_json.with_suffix(".md")

    repo_intent_arg: Path | None = args.repo_intent_md
    if repo_intent_arg is not None:
        repo_intent_path = (
            _resolve_optional_path(repo_root, repo_intent_arg) or repo_intent_arg.resolve()
        )
    else:
        repo_intent_path = repo_root / "configs" / "repo_intent.md"
    if not repo_intent_path.exists():
        print(f"Missing repo intent doc: {repo_intent_path}", file=sys.stderr)
        return 2

    readme_arg: Path | None = args.readme_md
    if readme_arg is not None:
        readme_path = _resolve_optional_path(repo_root, readme_arg) or readme_arg.resolve()
    else:
        readme_path = repo_root / "README.md"
    if not readme_path.exists():
        print(f"Missing README: {readme_path}", file=sys.stderr)
        return 2

    docs_dir_arg: Path | None = args.docs_dir
    if docs_dir_arg is not None:
        docs_dir = _resolve_optional_path(repo_root, docs_dir_arg) or docs_dir_arg.resolve()
    else:
        docs_dir = repo_root / "docs"

    max_readme_bytes = max(1, int(args.max_readme_bytes))
    max_doc_bytes = max(1, int(args.max_doc_bytes))

    try:
        repo_intent_excerpt = repo_intent_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"Failed reading repo intent doc: {repo_intent_path}: {e}", file=sys.stderr)
        return 2

    try:
        readme_excerpt = _read_text_excerpt(readme_path, max_bytes=max_readme_bytes)
    except OSError as e:
        print(f"Failed reading README: {readme_path}: {e}", file=sys.stderr)
        return 2

    from usertest_backlog.parser import build_parser
    commands = _extract_cli_commands(build_parser())
    docs_index = _index_docs(repo_root=repo_root, docs_dir=docs_dir, max_doc_bytes=max_doc_bytes)

    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": {
            "target": target_slug,
            "repo_input": repo_input,
        },
        "inputs": {
            "repo_intent_path": _safe_relpath(repo_intent_path, repo_root),
            "readme_path": _safe_relpath(readme_path, repo_root),
            "docs_dir": _safe_relpath(docs_dir, repo_root),
        },
        "repo_intent_excerpt": repo_intent_excerpt,
        "readme_excerpt": readme_excerpt,
        "docs_index": docs_index,
        "commands": commands,
        "llm_summary": None,
        "llm_summary_meta": {"status": "not_requested"},
    }

    prompts_dir_arg: Path | None = args.prompts_dir
    if prompts_dir_arg is not None:
        prompts_dir = (
            _resolve_optional_path(repo_root, prompts_dir_arg) or prompts_dir_arg.resolve()
        )
    else:
        prompts_dir = repo_root / "configs" / "backlog_prompts"

    with_summary = bool(args.with_summary)
    resume = bool(args.resume)
    force = bool(args.force)
    dry_run = bool(args.dry_run)
    agent = str(args.agent)
    model = str(args.model) if isinstance(args.model, str) and args.model.strip() else None

    if with_summary:
        template_path = prompts_dir / "intent_snapshot.md"
        if not template_path.exists():
            print(f"Missing intent snapshot prompt template: {template_path}", file=sys.stderr)
            return 2

        template = template_path.read_text(encoding="utf-8")
        prompt = _render_template(
            template,
            {
                "REPO_INTENT_MD": repo_intent_excerpt,
                "README_MD": readme_excerpt,
                "DOCS_INDEX_JSON": json.dumps(docs_index, indent=2, ensure_ascii=False),
                "COMMANDS_JSON": json.dumps(commands, indent=2, ensure_ascii=False),
            },
        )

        prompt_hash = sha256(prompt.encode("utf-8")).hexdigest()[:16]
        artifacts_dir = out_json.parent / f"{default_name}.intent_snapshot_artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        tag = f"intent_snapshot_{prompt_hash}"
        cached_path = artifacts_dir / f"{tag}.summary.json"

        summary_obj: dict[str, Any] | None = None
        status = "ok"
        used_cached = False

        if resume and not force and cached_path.exists():
            try:
                cached = json.loads(cached_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                warnings.warn(
                    f"Failed to parse cached intent summary at {cached_path}: {e}; rerunning summary.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                cached = None
            except OSError as e:
                warnings.warn(
                    f"Failed reading cached intent summary at {cached_path}: {e}; rerunning summary.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                cached = None
            if isinstance(cached, dict):
                summary_obj = cached
                status = "cached"
                used_cached = True
            elif cached is not None:
                warnings.warn(
                    "Ignoring cached intent summary with unexpected payload type "
                    f"{type(cached).__name__} at {cached_path}; expected object.",
                    RuntimeWarning,
                    stacklevel=2,
                )

        if summary_obj is None:
            if dry_run:
                (artifacts_dir / f"{tag}.dry_run.prompt.txt").write_text(prompt, encoding="utf-8")
                status = "dry_run"
            else:
                raw_text = run_backlog_prompt(
                    agent=agent,
                    prompt=prompt,
                    out_dir=artifacts_dir,
                    tag=tag,
                    model=model,
                    cfg=cfg,
                )
                parsed = _parse_first_json_object(raw_text)
                if not isinstance(parsed, dict):
                    (artifacts_dir / f"{tag}.parse_error.txt").write_text(
                        raw_text.strip() + "\n",
                        encoding="utf-8",
                    )
                    print(
                        "Failed to parse JSON from summary output "
                        f"(see artifacts under {artifacts_dir})",
                        file=sys.stderr,
                    )
                    return 2
                summary_obj = parsed
                cached_path.write_text(
                    json.dumps(summary_obj, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

        snapshot["llm_summary"] = summary_obj
        snapshot["llm_summary_meta"] = {
            "status": status,
            "prompt_hash": prompt_hash,
            "agent": agent,
            "model": model,
            "cached": used_cached,
            "template_path": _safe_relpath(template_path, repo_root),
        }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out_md.write_text(_render_intent_snapshot_markdown(snapshot), encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    if not with_summary:
        print(
            "Summary pass not requested (use --with-summary to generate an optional cached "
            "LLM summary)."
        )
    else:
        meta = snapshot.get("llm_summary_meta")
        status = meta.get("status") if isinstance(meta, dict) else None
        print(f"Summary status: {status}")
    return 0




__all__ = [name for name in globals() if not name.startswith("__")]
