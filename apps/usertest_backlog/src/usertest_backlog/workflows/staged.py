# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.commands.atom_actions import (
    _backfill_failure_event_atoms_from_legacy_entries,
    _update_atom_actions_from_backlog,
)
from usertest_backlog.shared import *
from usertest_backlog.workflows.implementation_planning import _run_implementation_planning_stage
from usertest_backlog.workflows.prioritization import _run_problem_prioritization_stage
from usertest_backlog.workflows.problem_mining import _run_problem_mining_stage
from usertest_backlog.workflows.reproduction_research import _run_repro_research_stage
from usertest_backlog.workflows.solution_options import _run_solution_optioning_stage
from usertest_backlog.workflows.solution_selection import _run_solution_selection_stage


def _cmd_reports_backlog(args: argparse.Namespace) -> int:
    """Execute the `reports backlog` command handler.

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
            out_json = runs_dir / target_slug / "_compiled" / f"{default_name}.backlog.json"
        else:
            out_json = runs_dir / "_compiled" / f"{default_name}.backlog.json"

    if args.out_md is not None:
        out_md = _resolve_optional_path(repo_root, args.out_md) or args.out_md.resolve()
    else:
        out_md = out_json.with_suffix(".md")

    atom_actions_arg: Path | None = args.atom_actions_yaml
    if atom_actions_arg is not None:
        atom_actions_path = (
            _resolve_optional_path(repo_root, atom_actions_arg) or atom_actions_arg.resolve()
        )
    else:
        atom_actions_path = repo_root / "configs" / "backlog_atom_actions.yaml"

    breadth_profile = _normalize_breadth_profile(getattr(args, "breadth_profile", None))
    prompts_dir, policy_config_default_path, breadth_profile_warnings = (
        _resolve_breadth_profile_paths(
            repo_root=repo_root,
            breadth_profile=breadth_profile,
            prompts_dir_arg=args.prompts_dir,
            policy_config_arg=args.policy_config,
        )
    )
    for warning_text in breadth_profile_warnings:
        print(f"[backlog] NOTE: {warning_text}", file=sys.stderr)

    atoms_jsonl = out_json.parent / f"{default_name}.backlog.atoms.jsonl"
    agent_last_message_atoms_jsonl = (
        out_json.parent / f"{default_name}.backlog.atoms.agent_last_message_artifact.jsonl"
    )
    artifacts_dir = out_json.parent / f"{default_name}.backlog_artifacts"

    records = list(
        iter_report_history(
            runs_dir,
            target_slug=target_slug,
            repo_input=repo_input,
            embed="none",
        )
    )
    atoms_doc_raw = extract_backlog_atoms(records, repo_root=repo_root)
    atoms_raw = atoms_doc_raw.get("atoms")
    raw_atoms = (
        [item for item in atoms_raw if isinstance(item, dict)]
        if isinstance(atoms_raw, list)
        else []
    )

    try:
        atom_actions = _load_atom_actions_yaml(atom_actions_path)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    plan_sync_meta: dict[str, Any] | None = None
    plan_sync_at: str | None = None
    if not bool(getattr(args, "skip_plan_folder_sync", False)):
        candidate_roots: list[Path] = [repo_root]

        for record in records:
            target_ref = record.get("target_ref")
            if not isinstance(target_ref, dict):
                continue
            repo_input_from_record = _coerce_string(target_ref.get("repo_input"))
            if repo_input_from_record is None:
                continue
            if not _looks_like_local_repo_input(repo_input_from_record):
                continue
            resolved = _resolve_local_repo_root(repo_root, repo_input_from_record)
            if resolved is None:
                continue
            candidate_roots.append(resolved)

        for entry in atom_actions.values():
            roots_raw = entry.get("queue_owner_roots")
            roots = (
                [item for item in roots_raw if isinstance(item, str) and item.strip()]
                if isinstance(roots_raw, list)
                else []
            )
            for root_s in roots:
                if not _looks_like_local_repo_input(root_s):
                    continue
                resolved = _resolve_local_repo_root(repo_root, root_s)
                if resolved is None:
                    continue
                candidate_roots.append(resolved)

        owner_roots = sorted({p.resolve() for p in candidate_roots}, key=lambda p: str(p))
        sync_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        plan_sync_at = sync_at
        plan_sync_meta = _reconcile_atom_actions_from_plan_folders(
            atom_actions=atom_actions,
            owner_roots=owner_roots,
            generated_at=sync_at,
        )

    backfill_at = plan_sync_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    backfill_meta = _backfill_failure_event_atoms_from_legacy_entries(
        atom_actions=atom_actions,
        generated_at=backfill_at,
    )
    if plan_sync_meta is not None:
        plan_sync_meta["failure_event_backfill"] = backfill_meta
        _write_atom_actions_yaml(atom_actions_path, atom_actions)

    carryover_meta: dict[str, Any] | None = None
    if bool(getattr(args, "carryover_actioned_only", False)):
        if args.exclude_atom_status:
            print(
                "Cannot combine --carryover-actioned-only with --exclude-atom-status.",
                file=sys.stderr,
            )
            return 2
        carryover_at = plan_sync_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        demoted_atoms = 0
        demoted_status_counts: dict[str, int] = {}
        for entry in atom_actions.values():
            status = _normalize_atom_status(_coerce_string(entry.get("status")))
            if status in ("new", "actioned"):
                continue
            entry["status"] = "new"
            entry["carryover_reset_at"] = carryover_at
            demoted_atoms += 1
            demoted_status_counts[status] = demoted_status_counts.get(status, 0) + 1
        carryover_meta = {
            "mode": "actioned_only",
            "reset_at": carryover_at,
            "demoted_atoms": demoted_atoms,
            "demoted_status_counts": demoted_status_counts,
        }

    # By default, do not re-mine atoms that already produced any ticket outcome.
    exclude_atom_statuses = args.exclude_atom_status or ["ticketed", "queued", "actioned"]
    exclude_atom_status_set = {
        _normalize_atom_status(_coerce_string(status))
        for status in exclude_atom_statuses
        if _coerce_string(status) is not None
    }
    excluded_atoms: list[dict[str, Any]] = []
    atoms: list[dict[str, Any]] = []
    agent_last_message_atoms: list[dict[str, Any]] = []
    excluded_status_counts: dict[str, int] = {}
    for atom in raw_atoms:
        atom_id = _coerce_string(atom.get("atom_id"))
        atom_status = "new"
        if atom_id is not None:
            existing = atom_actions.get(atom_id)
            if isinstance(existing, dict):
                atom_status = _normalize_atom_status(_coerce_string(existing.get("status")))
        if atom_status in exclude_atom_status_set:
            excluded_atoms.append(atom)
            excluded_status_counts[atom_status] = excluded_status_counts.get(atom_status, 0) + 1
            continue
        if _coerce_string(atom.get("source")) == "agent_last_message_artifact":
            agent_last_message_atoms.append(atom)
            continue
        atoms.append(atom)

    eligible_atoms_trackable = len(atoms)
    pipeline_batch_breadth = compute_batch_breadth(atoms)
    eligible_run_rels = {
        run_rel
        for atom in atoms
        for run_rel in [_coerce_string(atom.get("run_rel"))]
        if run_rel is not None
    }
    aggregate_run_id_prefix = (
        "__aggregate__/"
        + (target_slug or "all")
        + "/"
        + (slugify(repo_input) if repo_input is not None else "all")
    )
    aggregate_atoms = build_aggregate_metrics_atoms(
        records,
        eligible_run_rels,
        run_id_prefix=aggregate_run_id_prefix,
    )
    atoms.extend(aggregate_atoms)
    atoms = add_atom_links(atoms)
    agent_last_message_atoms = add_atom_links(agent_last_message_atoms)

    atom_totals = _summarize_atoms_for_totals(atoms)
    atoms_doc = dict(atoms_doc_raw)
    atoms_doc["atoms"] = atoms
    totals_raw = atoms_doc_raw.get("totals")
    totals_dict = dict(totals_raw) if isinstance(totals_raw, dict) else {}
    totals_dict.update(atom_totals)
    atoms_doc["totals"] = totals_dict
    atoms_doc["atom_filter"] = {
        "exclude_statuses": sorted(exclude_atom_status_set),
        "carryover": carryover_meta,
        "eligible_atoms": len(atoms),
        "eligible_atoms_trackable": eligible_atoms_trackable,
        "excluded_sources": ["agent_last_message_artifact"],
        "excluded_source_counts": {"agent_last_message_artifact": len(agent_last_message_atoms)},
        "excluded_source_atoms_jsonl": str(agent_last_message_atoms_jsonl),
        "synthetic_atoms_added": len(aggregate_atoms),
        "excluded_atoms": len(excluded_atoms),
        "excluded_status_counts": excluded_status_counts,
        "plan_folder_sync": plan_sync_meta,
        "excluded_atom_ids_preview": [
            atom_id
            for atom in excluded_atoms[:200]
            for atom_id in [_coerce_string(atom.get("atom_id"))]
            if atom_id is not None
        ],
    }
    write_backlog_atoms(atoms_doc, atoms_jsonl)
    write_backlog_atoms({"atoms": agent_last_message_atoms}, agent_last_message_atoms_jsonl)

    sample_size = int(args.sample_size)
    if sample_size < 0:
        raise ValueError("--sample-size must be >= 0")
    sample_size_semantics = "all_atoms" if sample_size == 0 else "fixed_sample"
    seed = int(args.seed)
    resume = bool(args.resume)
    force = bool(args.force)
    dry_run = bool(args.dry_run)
    agent = str(args.agent)
    model = str(args.model) if isinstance(args.model, str) and args.model.strip() else None

    legacy_one_pass_flags: list[str] = []
    if int(args.miners) != 10:
        legacy_one_pass_flags.append(f"--miners={int(args.miners)}")
    if int(args.sample_size) != 120:
        legacy_one_pass_flags.append(f"--sample-size={int(args.sample_size)}")
    if int(args.coverage_miners) != 3:
        legacy_one_pass_flags.append(f"--coverage-miners={int(args.coverage_miners)}")
    if args.bagging_miners is not None:
        legacy_one_pass_flags.append(f"--bagging-miners={int(args.bagging_miners)}")
    if int(args.max_tickets_per_miner) != 12:
        legacy_one_pass_flags.append(f"--max-tickets-per-miner={int(args.max_tickets_per_miner)}")
    if int(args.orphan_pass) != 1:
        legacy_one_pass_flags.append(f"--orphan-pass={int(args.orphan_pass)}")
    if seed != 0:
        legacy_one_pass_flags.append(f"--seed={seed}")
    if not resume:
        legacy_one_pass_flags.append("--no-resume")
    if force:
        legacy_one_pass_flags.append("--force")
    if bool(args.no_merge):
        legacy_one_pass_flags.append("--no-merge")
    merge_candidate_threshold = float(args.merge_candidate_threshold)
    if not (0.0 <= merge_candidate_threshold <= 1.0):
        raise ValueError("--merge-candidate-threshold must be in [0, 1]")
    if merge_candidate_threshold != 0.65:
        legacy_one_pass_flags.append(f"--merge-candidate-threshold={merge_candidate_threshold:g}")
    if bool(args.merge_keep_anchor_pairs):
        legacy_one_pass_flags.append("--merge-keep-anchor-pairs")
    if int(args.labelers) != 3:
        legacy_one_pass_flags.append(f"--labelers={int(args.labelers)}")

    if legacy_one_pass_flags:
        print(
            "[backlog] NOTE: legacy one-pass knobs are ignored by the six-stage pipeline: "
            + " ".join(legacy_one_pass_flags),
            file=sys.stderr,
        )

    policy_cfg: BacklogPolicyConfig | None = None
    policy_config_path: Path | None = policy_config_default_path
    if not bool(args.no_policy) and policy_config_path is not None and policy_config_path.exists():
        policy_root = _load_yaml(policy_config_path).get("backlog_policy")
        if policy_root is None:
            raise ValueError(f"Expected backlog_policy key in {policy_config_path}")
        if not isinstance(policy_root, dict):
            raise ValueError(
                f"Expected mapping at backlog_policy in {policy_config_path}, got "
                f"{type(policy_root).__name__}"
            )
        policy_cfg = BacklogPolicyConfig.from_dict(policy_root)

    # ---------------------------------------------------------------------------
    # Six-stage backlog pipeline (canonical, milestone 6).
    # ---------------------------------------------------------------------------
    pipeline_manifest_path = prompts_dir / "pipeline_manifest.json"
    if not pipeline_manifest_path.exists():
        print(
            f"Missing six-stage pipeline manifest: {pipeline_manifest_path} "
            "(expected under --prompts-dir).",
            file=sys.stderr,
        )
        return 2

    try:
        pipeline_manifest = load_pipeline_prompt_manifest(prompts_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    problem_records_json = out_json.parent / f"{default_name}.problem_records.json"
    problem_records_md = out_json.parent / f"{default_name}.problem_records.md"
    prioritized_json = out_json.parent / f"{default_name}.prioritized_problems.json"
    prioritized_md = out_json.parent / f"{default_name}.prioritized_problems.md"
    research_json = out_json.parent / f"{default_name}.research.json"
    research_md = out_json.parent / f"{default_name}.research.md"
    solution_options_json = out_json.parent / f"{default_name}.solution_options.json"
    solution_options_md = out_json.parent / f"{default_name}.solution_options.md"
    solution_selection_json = out_json.parent / f"{default_name}.solution_selection.json"
    solution_selection_md = out_json.parent / f"{default_name}.solution_selection.md"
    change_plans_json = out_json.parent / f"{default_name}.change_plans.json"
    change_plans_md = out_json.parent / f"{default_name}.change_plans.md"

    try:
        stage1_guidance = pipeline_manifest.load_stage_guidance("problem_mining")
        stage1_doc = _run_problem_mining_stage(
            atoms=atoms,
            pipeline_manifest=pipeline_manifest,
            artifacts_dir=artifacts_dir,
            out_json=problem_records_json,
            out_md=problem_records_md,
            agent=agent,
            model=model,
            cfg=cfg,
            dry_run=dry_run,
            stage_guidance_text=stage1_guidance,
        )

        items1_raw = stage1_doc.get("items") if isinstance(stage1_doc, dict) else None
        problem_records = (
            [item for item in items1_raw if isinstance(item, dict)]
            if isinstance(items1_raw, list)
            else []
        )

        stage2_guidance = pipeline_manifest.load_stage_guidance("problem_prioritization")
        stage2_doc = _run_problem_prioritization_stage(
            atoms=atoms,
            problem_records=problem_records,
            pipeline_manifest=pipeline_manifest,
            artifacts_dir=artifacts_dir,
            out_json=prioritized_json,
            out_md=prioritized_md,
            agent=agent,
            model=model,
            cfg=cfg,
            dry_run=dry_run,
            stage_guidance_text=stage2_guidance,
        )

        items2_raw = stage2_doc.get("items") if isinstance(stage2_doc, dict) else None
        priority_decisions = (
            [item for item in items2_raw if isinstance(item, dict)]
            if isinstance(items2_raw, list)
            else []
        )
        selected_priority = [dec for dec in priority_decisions if dec.get("selected_for_research") is True]

        resolved_repo_input = repo_input
        if resolved_repo_input is None:
            raw_repo_inputs: list[str] = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                target_ref = record.get("target_ref")
                if not isinstance(target_ref, dict):
                    continue
                candidate = _coerce_string(target_ref.get("repo_input"))
                if candidate is not None:
                    raw_repo_inputs.append(candidate)

            normalized_repo_inputs: dict[str, str] = {}
            for candidate in sorted(set(raw_repo_inputs)):
                resolved = candidate.strip()
                norm_key = resolved
                if _looks_like_local_repo_input(resolved):
                    resolved_path = _resolve_local_repo_root(repo_root, resolved) or Path(resolved).expanduser()
                    try:
                        resolved_path = resolved_path.resolve()
                    except OSError:
                        pass
                    resolved = str(resolved_path)
                    norm_key = os.path.normcase(os.path.normpath(resolved))
                if norm_key not in normalized_repo_inputs:
                    normalized_repo_inputs[norm_key] = resolved

            if len(normalized_repo_inputs) == 1:
                resolved_repo_input = next(iter(normalized_repo_inputs.values()))
                print(
                    f"[stage3] inferred repo_input from run history: {resolved_repo_input}",
                    file=sys.stderr,
                )
            elif len(normalized_repo_inputs) > 1:
                preview = ", ".join(list(normalized_repo_inputs.values())[:4])
                suffix = " …" if len(normalized_repo_inputs) > 4 else ""
                print(
                    "[stage3] WARNING: multiple repo_inputs found in run history; "
                    "provide --repo-input to enable stage 3 repro+research. "
                    f"(unique_after_normalization={len(normalized_repo_inputs)} preview={preview}{suffix})",
                    file=sys.stderr,
                )

        if selected_priority and not resolved_repo_input and not dry_run:
            print(
                "[stage3] Missing repo_input for repro+research. Provide --repo-input "
                "or ensure run history contains exactly one local repo_input.",
                file=sys.stderr,
            )
            return 2

        stage3_doc = _run_repro_research_stage(
            repo_root=repo_root,
            repo_input=resolved_repo_input,
            target_slug=target_slug,
            selected_priority_decisions=selected_priority,
            problem_records=problem_records,
            artifacts_dir=artifacts_dir,
            out_json=research_json,
            out_md=research_md,
            agent=agent,
            model=model,
            cfg=cfg,
            dry_run=dry_run,
        )

        items3_raw = stage3_doc.get("items") if isinstance(stage3_doc, dict) else None
        research_dossiers = (
            [item for item in items3_raw if isinstance(item, dict)]
            if isinstance(items3_raw, list)
            else []
        )

        stage4_guidance = pipeline_manifest.load_stage_guidance("solution_optioning")
        stage4_doc = _run_solution_optioning_stage(
            repo_root=repo_root,
            atoms=atoms,
            problem_records=problem_records,
            priority_decisions=priority_decisions,
            research_dossiers=research_dossiers,
            pipeline_manifest=pipeline_manifest,
            artifacts_dir=artifacts_dir,
            out_json=solution_options_json,
            out_md=solution_options_md,
            agent=agent,
            model=model,
            cfg=cfg,
            dry_run=dry_run,
            breadth_profile=breadth_profile,
            stage_guidance_text=stage4_guidance,
        )

        items4_raw = stage4_doc.get("items") if isinstance(stage4_doc, dict) else None
        solution_options = (
            [item for item in items4_raw if isinstance(item, dict)]
            if isinstance(items4_raw, list)
            else []
        )

        stage5_guidance = pipeline_manifest.load_stage_guidance("solution_selection")
        stage5_doc = _run_solution_selection_stage(
            repo_root=repo_root,
            atoms=atoms,
            problem_records=problem_records,
            research_dossiers=research_dossiers,
            solution_options=solution_options,
            pipeline_manifest=pipeline_manifest,
            artifacts_dir=artifacts_dir,
            out_json=solution_selection_json,
            out_md=solution_selection_md,
            agent=agent,
            model=model,
            cfg=cfg,
            dry_run=dry_run,
            breadth_profile=breadth_profile,
            stage_guidance_text=stage5_guidance,
        )

        items5_raw = stage5_doc.get("items") if isinstance(stage5_doc, dict) else None
        selection_decisions = (
            [item for item in items5_raw if isinstance(item, dict)]
            if isinstance(items5_raw, list)
            else []
        )

        stage6_guidance = pipeline_manifest.load_stage_guidance("implementation_planning")
        stage6_doc = _run_implementation_planning_stage(
            repo_root=repo_root,
            problem_records=problem_records,
            research_dossiers=research_dossiers,
            solution_options=solution_options,
            selection_decisions=selection_decisions,
            pipeline_manifest=pipeline_manifest,
            artifacts_dir=artifacts_dir,
            out_json=change_plans_json,
            out_md=change_plans_md,
            agent=agent,
            model=model,
            cfg=cfg,
            dry_run=dry_run,
            stage_guidance_text=stage6_guidance,
        )

        items6_raw = stage6_doc.get("items") if isinstance(stage6_doc, dict) else None
        change_plans = (
            [item for item in items6_raw if isinstance(item, dict)]
            if isinstance(items6_raw, list)
            else []
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[backlog] ERROR: six-stage backlog pipeline failed: {exc}", file=sys.stderr)
        return 2

    try:
        tickets = assemble_backlog_tickets(
            problem_records=problem_records,
            priority_decisions=priority_decisions,
            research_dossiers=research_dossiers,
            solution_option_sets=solution_options,
            selection_decisions=selection_decisions,
            change_plans=change_plans,
        )
    except ValueError as exc:
        print(f"[backlog] ERROR: ticket assembly failed: {exc}", file=sys.stderr)
        return 2

    eligible_atom_ids = {
        atom_id
        for atom in atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        if atom_id is not None
    }
    dropped_tickets_excluded_atoms = 0
    filtered_tickets: list[dict[str, Any]] = []
    for ticket in tickets:
        evidence_ids = _coerce_string_list(ticket.get("evidence_atom_ids"))
        filtered_ids = [atom_id for atom_id in evidence_ids if atom_id in eligible_atom_ids]
        if not filtered_ids:
            dropped_tickets_excluded_atoms += 1
            continue
        updated = dict(ticket)
        updated["evidence_atom_ids"] = filtered_ids
        filtered_tickets.append(updated)
    tickets = filtered_tickets

    summary = build_backlog_document(
        atoms_doc=atoms_doc,
        tickets=tickets,
        input_meta={
            "runs_dir": str(runs_dir),
            "target": target_slug,
            "repo_input": repo_input,
            "agent": agent,
            "model": model,
            "breadth_profile": breadth_profile,
            "dry_run": dry_run,
            "resume": resume,
            "force": force,
            "seed": seed,
            "sample_size": sample_size,
            "sample_size_semantics": sample_size_semantics,
            "exclude_atom_statuses": sorted(exclude_atom_status_set),
            "batch_breadth": pipeline_batch_breadth,
            "pipeline_manifest_path": str(pipeline_manifest_path),
            "pipeline_manifest_version": int(getattr(pipeline_manifest, "version", 2)),
            "breadth_profile_warnings": breadth_profile_warnings,
        },
        artifacts={
            "atoms_jsonl": str(atoms_jsonl),
            "atoms_agent_last_message_artifact_jsonl": str(agent_last_message_atoms_jsonl),
            "artifacts_dir": str(artifacts_dir),
            "prompts_dir": str(prompts_dir),
            "breadth_profile": breadth_profile,
            "batch_breadth": pipeline_batch_breadth,
            "atom_filter": {
                **(atoms_doc.get("atom_filter") or {}),
                "dropped_tickets_excluded_atoms": dropped_tickets_excluded_atoms,
            },
            "six_stage_pipeline": {
                "problem_records_json": str(problem_records_json),
                "prioritized_problems_json": str(prioritized_json),
                "research_json": str(research_json),
                "solution_options_json": str(solution_options_json),
                "solution_selection_json": str(solution_selection_json),
                "change_plans_json": str(change_plans_json),
            },
        },
        miners_meta={},
    )

    if policy_cfg is not None:
        tickets_raw = summary.get("tickets")
        tickets_list = (
            [item for item in tickets_raw if isinstance(item, dict)]
            if isinstance(tickets_raw, list)
            else []
        )
        if tickets_list:
            updated_tickets, policy_meta = apply_backlog_policy(tickets_list, config=policy_cfg)
            summary["tickets"] = updated_tickets
            artifacts = summary.get("artifacts")
            artifacts_dict = artifacts if isinstance(artifacts, dict) else {}
            artifacts_dict["policy"] = {
                "config_path": str(policy_config_path) if policy_config_path is not None else None,
                "breadth_profile": breadth_profile,
                "warnings": breadth_profile_warnings,
                "meta": policy_meta,
            }
            summary["artifacts"] = artifacts_dict

    generated_at = _coerce_string(summary.get("generated_at_utc")) or datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    tickets_for_atoms_raw = summary.get("tickets")
    tickets_for_atoms = (
        [item for item in tickets_for_atoms_raw if isinstance(item, dict)]
        if isinstance(tickets_for_atoms_raw, list)
        else []
    )
    atom_status_meta = _update_atom_actions_from_backlog(
        atom_actions=atom_actions,
        atoms=atoms,
        tickets=tickets_for_atoms,
        generated_at=generated_at,
        backlog_json_path=out_json,
    )
    _write_atom_actions_yaml(atom_actions_path, atom_actions)

    artifacts = summary.get("artifacts")
    artifacts_dict = artifacts if isinstance(artifacts, dict) else {}
    artifacts_dict["atom_actions"] = {
        "path": str(atom_actions_path),
        "meta": atom_status_meta,
    }
    summary["artifacts"] = artifacts_dict

    scope_bits = []
    if target_slug is not None:
        scope_bits.append(f"target={target_slug}")
    if repo_input is not None:
        scope_bits.append(f"repo_input={repo_input}")
    title_suffix = f" ({', '.join(scope_bits)})" if scope_bits else ""

    write_backlog(
        summary,
        out_json_path=out_json,
        out_md_path=out_md,
        title=f"Usertest Backlog{title_suffix}",
    )

    print(str(out_json))
    print(str(out_md))
    print(str(atoms_jsonl))
    print(str(agent_last_message_atoms_jsonl))
    print(json.dumps(summary.get("totals", {}), indent=2, ensure_ascii=False))
    print(json.dumps(summary.get("coverage", {}), indent=2, ensure_ascii=False))

    return 0




__all__ = [name for name in globals() if not name.startswith("__")]
