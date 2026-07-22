# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.shared import *


def _render_ux_review_markdown(doc: dict[str, Any]) -> str:
    """
    Render a human-readable markdown view of a UX review JSON artifact.

    Parameters
    ----------
    doc:
        UX review document as written to `.ux_review.json`.

    Returns
    -------
    str
        Markdown content.
    """

    generated_at = _coerce_string(doc.get("generated_at")) or "unknown"
    status = _coerce_string(doc.get("status")) or "unknown"
    scope = doc.get("scope") if isinstance(doc.get("scope"), dict) else {}
    target = _coerce_string(scope.get("target")) or "all"
    repo_input = _coerce_string(scope.get("repo_input"))

    lines: list[str] = []
    lines.append("# UX / Intent Review")
    lines.append("")
    lines.append(f"- Generated at: `{generated_at}`")
    lines.append(f"- Status: `{status}`")
    lines.append(f"- Scope target: `{target}`")
    if repo_input is not None:
        lines.append(f"- Scope repo_input: `{repo_input}`")
    lines.append("")

    review = doc.get("review")
    review_obj = review if isinstance(review, dict) else None
    if review_obj is None:
        lines.append("## Output")
        lines.append("")
        lines.append("- No reviewer output was generated.")
        artifacts_dir = _coerce_string(doc.get("artifacts_dir"))
        if artifacts_dir:
            lines.append(f"- Artifacts dir: `{artifacts_dir}`")
        lines.append("")
        return "\n".join(lines) + "\n"

    budget = review_obj.get("command_surface_budget")
    if isinstance(budget, dict):
        max_new = budget.get("max_new_commands_per_quarter")
        notes = _coerce_string(budget.get("notes")) or ""
        lines.append("## Command Surface Budget")
        lines.append("")
        if isinstance(max_new, int):
            lines.append(f"- Max new commands/quarter: `{max_new}`")
        elif isinstance(max_new, (float, str)):
            lines.append(f"- Max new commands/quarter: `{max_new}`")
        if notes:
            lines.append(f"- Notes: {notes}")
        lines.append("")

    recs = review_obj.get("recommendations")
    rec_list = [item for item in recs if isinstance(item, dict)] if isinstance(recs, list) else []
    lines.append("## Recommendations")
    lines.append("")
    if not rec_list:
        lines.append("- (no recommendations)")
        lines.append("")
    else:
        for rec in rec_list[:80]:
            rec_id = _coerce_string(rec.get("recommendation_id")) or "UX-???"
            approach = _coerce_string(rec.get("recommended_approach")) or "unknown"
            fingerprints = rec.get("fingerprints")
            tickets_s = (
                ", ".join([fp for fp in fingerprints if isinstance(fp, str) and fp.strip()])
                if isinstance(fingerprints, list)
                else ""
            )
            title_bits = f" ({tickets_s})" if tickets_s else ""
            lines.append(f"### {rec_id}: {approach}{title_bits}")
            rationale = _coerce_string(rec.get("rationale"))
            if rationale:
                lines.append("")
                lines.append(rationale.strip())
                lines.append("")
            review_domain = _coerce_string(rec.get("review_domain"))
            if review_domain:
                lines.append(f"- Review domain: `{review_domain}`")
            breadth_profile = _coerce_string(rec.get("breadth_profile"))
            if breadth_profile:
                lines.append(f"- Breadth profile: `{breadth_profile}`")
            decision_basis_raw = rec.get("decision_basis")
            if isinstance(decision_basis_raw, dict):
                context_breadth = decision_basis_raw.get("context_breadth")
                observation_breadth = decision_basis_raw.get("observation_breadth")
                struct_dims = decision_basis_raw.get("structurally_constant_dimensions")
                if isinstance(context_breadth, dict):
                    bits = [
                        f"{dim}={int(_coerce_int(context_breadth.get(dim), default=0))}"
                        for dim in _BREADTH_CONTEXT_DIMENSIONS
                    ]
                    lines.append(f"- Context breadth: `{', '.join(bits)}`")
                if isinstance(observation_breadth, dict):
                    bits = [
                        f"{dim}={int(_coerce_int(observation_breadth.get(dim), default=0))}"
                        for dim in _BREADTH_OBSERVATION_DIMENSIONS
                    ]
                    lines.append(f"- Observation breadth: `{', '.join(bits)}`")
                if isinstance(struct_dims, list):
                    dims = [dim for dim in struct_dims if isinstance(dim, str) and dim.strip()]
                    if dims:
                        lines.append(f"- Structurally constant dimensions: `{', '.join(dims)}`")
            next_steps = rec.get("next_steps")
            if isinstance(next_steps, list):
                steps = [s for s in next_steps if isinstance(s, str) and s.strip()]
                if steps:
                    lines.append("- Next steps:")
                    for step in steps[:10]:
                        lines.append(f"  - {step}")
                    lines.append("")

        lines.append("")

    notes = _coerce_string(review_obj.get("notes"))
    if notes:
        lines.append("## Notes")
        lines.append("")
        lines.append(notes.strip())
        lines.append("")

    return "\n".join(lines) + "\n"


_UX_REVIEW_SECTION_START = "<!-- usertest:ux_review:start -->"
_UX_REVIEW_SECTION_END = "<!-- usertest:ux_review:end -->"


def _load_optional_json_object(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _index_ux_recommendations(doc: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    review_raw = doc.get("review")
    review = review_raw if isinstance(review_raw, dict) else {}
    recs_raw = review.get("recommendations")
    recs = (
        [item for item in recs_raw if isinstance(item, dict)] if isinstance(recs_raw, list) else []
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for rec in recs:
        fingerprints_raw = rec.get("fingerprints")
        fingerprints = (
            [fp for fp in fingerprints_raw if isinstance(fp, str) and fp.strip()]
            if isinstance(fingerprints_raw, list)
            else []
        )
        for fingerprint in fingerprints:
            out.setdefault(fingerprint.strip(), []).append(rec)
    return out


def _pick_ux_recommended_approach(recs: list[dict[str, Any]]) -> str | None:
    approaches = [
        _coerce_string(rec.get("recommended_approach")) or ""
        for rec in recs
        if isinstance(rec, dict)
    ]
    normalized = {a.strip() for a in approaches if a.strip()}
    for choice in (
        "defer",
        "new_surface",
        "accept_existing_surface",
        "parameterize_existing",
        "docs",
    ):
        if choice in normalized:
            return choice
    return next(iter(sorted(normalized)), None)


def _render_ux_review_section_for_ticket(
    *,
    ux_review_doc: dict[str, Any],
    ux_review_json_path: Path,
    ux_review_md_path: Path,
    fingerprint: str,
    recs: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append(_UX_REVIEW_SECTION_START)
    lines.append("## UX review (authoritative)")
    lines.append("")
    lines.append(
        "**Authoritative**: This UX review is the decision source of truth for implementation. "
        "If it conflicts with the original ticket text below, follow the UX review."
    )
    lines.append("")
    lines.append(f"- ux_review.json: `{ux_review_json_path}`")
    lines.append(f"- ux_review.md: `{ux_review_md_path}`")

    status = _coerce_string(ux_review_doc.get("status"))
    if status:
        lines.append(f"- reviewer_status: `{status}`")
    generated_at = _coerce_string(ux_review_doc.get("generated_at"))
    if generated_at:
        lines.append(f"- reviewer_generated_at: `{generated_at}`")
    prompt_hash = _coerce_string(ux_review_doc.get("prompt_hash"))
    if prompt_hash:
        lines.append(f"- reviewer_prompt_hash: `{prompt_hash}`")

    review_raw = ux_review_doc.get("review")
    review = review_raw if isinstance(review_raw, dict) else {}
    conf_raw = review.get("confidence")
    if isinstance(conf_raw, (int, float)):
        lines.append(f"- reviewer_confidence: `{float(conf_raw):.2f}`")

    lines.append("")

    for rec in recs[:5]:
        rec_id = _coerce_string(rec.get("recommendation_id")) or "UX-???"
        approach = _coerce_string(rec.get("recommended_approach")) or "unknown"
        lines.append(f"### {rec_id}: {approach} ({fingerprint})")
        lines.append("")

        rationale = _coerce_string(rec.get("rationale"))
        if rationale:
            lines.append(rationale.strip())
            lines.append("")
        review_domain = _coerce_string(rec.get("review_domain"))
        if review_domain:
            lines.append(f"- Review domain: `{review_domain}`")
        breadth_profile = _coerce_string(rec.get("breadth_profile"))
        if breadth_profile:
            lines.append(f"- Breadth profile: `{breadth_profile}`")
        decision_basis_raw = rec.get("decision_basis")
        if isinstance(decision_basis_raw, dict):
            context_breadth = decision_basis_raw.get("context_breadth")
            observation_breadth = decision_basis_raw.get("observation_breadth")
            struct_dims = decision_basis_raw.get("structurally_constant_dimensions")
            if isinstance(context_breadth, dict):
                bits = [
                    f"{dim}={int(_coerce_int(context_breadth.get(dim), default=0))}"
                    for dim in _BREADTH_CONTEXT_DIMENSIONS
                ]
                lines.append(f"- Context breadth: `{', '.join(bits)}`")
            if isinstance(observation_breadth, dict):
                bits = [
                    f"{dim}={int(_coerce_int(observation_breadth.get(dim), default=0))}"
                    for dim in _BREADTH_OBSERVATION_DIMENSIONS
                ]
                lines.append(f"- Observation breadth: `{', '.join(bits)}`")
            if isinstance(struct_dims, list):
                dims = [dim for dim in struct_dims if isinstance(dim, str) and dim.strip()]
                if dims:
                    lines.append(f"- Structurally constant dimensions: `{', '.join(dims)}`")
            lines.append("")

        next_steps_raw = rec.get("next_steps")
        next_steps = (
            [step for step in next_steps_raw if isinstance(step, str) and step.strip()]
            if isinstance(next_steps_raw, list)
            else []
        )
        if next_steps:
            lines.append("Next steps:")
            for step in next_steps[:10]:
                lines.append(f"- {step}")
            lines.append("")

        breadth_raw = rec.get("evidence_breadth_summary")
        breadth = breadth_raw if isinstance(breadth_raw, dict) else {}
        breadth_bits: list[str] = []
        for key in ("missions", "targets", "repo_inputs", "agents", "runs"):
            val = breadth.get(key)
            if isinstance(val, (int, float)):
                breadth_bits.append(f"{key}={int(val)}")
        if breadth_bits:
            lines.append(f"Evidence breadth: `{', '.join(breadth_bits)}`")
            lines.append("")

        lines.append("Raw recommendation JSON:")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(rec, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")

    lines.append(_UX_REVIEW_SECTION_END)
    lines.append("")
    return "\n".join(lines)


def _replace_markdown_ticket_field(markdown: str, *, label: str, value: str) -> str:
    pattern = rf"(?m)^-\s*{re.escape(label)}:\s*`[^`]*`\s*$"
    replacement = f"- {label}: `{value}`"
    if re.search(pattern, markdown) is None:
        return markdown
    return re.sub(pattern, replacement, markdown, count=1)


def _upsert_ux_review_section(markdown: str, *, section: str) -> str:
    start = markdown.find(_UX_REVIEW_SECTION_START)
    end = markdown.find(_UX_REVIEW_SECTION_END)
    if start != -1 and end != -1 and end > start:
        end_idx = end + len(_UX_REVIEW_SECTION_END)
        prefix = markdown[:start]
        suffix = markdown[end_idx:]
        remainder = (prefix.rstrip() + "\n\n" + suffix.lstrip("\n")).strip()
        out = section.strip()
        if remainder:
            out += "\n\n" + remainder
        return out.rstrip() + "\n"
    remainder = markdown.strip()
    out = section.strip()
    if remainder:
        out += "\n\n" + remainder
    return out.rstrip() + "\n"


def _apply_ux_review_to_plan_ticket(
    *,
    path: Path,
    ux_section: str,
    stage_override: str | None,
    export_kind_override: str | None,
) -> bool:
    try:
        original = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    updated = original
    if export_kind_override:
        updated = _replace_markdown_ticket_field(
            updated,
            label="Export kind",
            value=export_kind_override,
        )
    if stage_override:
        updated = _replace_markdown_ticket_field(updated, label="Stage", value=stage_override)
    updated = _upsert_ux_review_section(updated, section=ux_section)

    if updated == original:
        return False
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError:
        return False
    return True


def _move_plan_ticket_to_bucket(*, path: Path, owner_repo_root: Path, bucket: str) -> Path | None:
    plans_dir = owner_repo_root / ".agents" / "plans"
    dest_dir = plans_dir / bucket
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    dest_path = dest_dir / path.name
    try:
        path.replace(dest_path)
    except OSError:
        return None
    return dest_path


def _cmd_reports_review_ux(args: argparse.Namespace) -> int:
    """Execute the `reports review ux` command handler.

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
    if target_slug is not None:
        compiled_dir = runs_dir / target_slug / "_compiled"
    else:
        compiled_dir = runs_dir / "_compiled"

    backlog_arg: Path | None = args.backlog_json
    if backlog_arg is not None:
        backlog_path = _resolve_optional_path(repo_root, backlog_arg) or backlog_arg.resolve()
    else:
        backlog_path = compiled_dir / f"{default_name}.backlog.json"
    if not backlog_path.exists():
        print(f"Missing backlog JSON: {backlog_path}", file=sys.stderr)
        return 2

    intent_snapshot_arg: Path | None = args.intent_snapshot_json
    if intent_snapshot_arg is not None:
        intent_snapshot_path = (
            _resolve_optional_path(repo_root, intent_snapshot_arg) or intent_snapshot_arg.resolve()
        )
    else:
        intent_snapshot_path = compiled_dir / f"{default_name}.intent_snapshot.json"

    allow_missing_snapshot = bool(args.allow_missing_intent_snapshot)
    intent_snapshot_obj: dict[str, Any] | None = None
    if intent_snapshot_path.exists():
        try:
            raw_snapshot = json.loads(intent_snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"Failed to parse intent snapshot JSON: {intent_snapshot_path}: {e}",
                file=sys.stderr,
            )
            return 2
        if isinstance(raw_snapshot, dict):
            intent_snapshot_obj = raw_snapshot
        else:
            intent_snapshot_obj = {"raw": raw_snapshot}
    elif not allow_missing_snapshot:
        print(
            "Missing intent snapshot JSON: "
            f"{intent_snapshot_path} (run `usertest-backlog reports intent-snapshot`)",
            file=sys.stderr,
        )
        return 2

    intent_snapshot_json_path = (
        str(intent_snapshot_path) if intent_snapshot_obj is not None else None
    )

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

    if args.out_json is not None:
        out_json = _resolve_optional_path(repo_root, args.out_json) or args.out_json.resolve()
    else:
        out_json = compiled_dir / f"{default_name}.ux_review.json"

    if args.out_md is not None:
        out_md = _resolve_optional_path(repo_root, args.out_md) or args.out_md.resolve()
    else:
        out_md = out_json.with_suffix(".md")

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
        print(f"[review-ux] NOTE: {warning_text}", file=sys.stderr)

    template_path = prompts_dir / "ux_reviewer.md"
    if not template_path.exists():
        print(f"Missing UX reviewer prompt template: {template_path}", file=sys.stderr)
        return 2

    try:
        backlog_doc = json.loads(backlog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Failed to parse backlog JSON: {backlog_path}: {e}", file=sys.stderr)
        return 2
    if not isinstance(backlog_doc, dict):
        print(f"Invalid backlog JSON (expected object): {backlog_path}", file=sys.stderr)
        return 2

    tickets_raw = backlog_doc.get("tickets")
    tickets = (
        [item for item in tickets_raw if isinstance(item, dict)]
        if isinstance(tickets_raw, list)
        else []
    )

    # Prefer the staged solution-selection artifact when present (milestone 5).
    # This ensures UX review is driven by selected solutions rather than early miner guesses.
    tickets_source = "backlog"
    solution_selection_path = compiled_dir / f"{default_name}.solution_selection.json"
    solution_selection_doc = _load_optional_json_object(solution_selection_path)
    selection_input_meta = (
        solution_selection_doc.get("input_meta")
        if isinstance(solution_selection_doc, dict)
        and isinstance(solution_selection_doc.get("input_meta"), dict)
        else {}
    )
    if isinstance(solution_selection_doc, dict):
        sel_items_raw = solution_selection_doc.get("items")
        sel_items = (
            [item for item in sel_items_raw if isinstance(item, dict)]
            if isinstance(sel_items_raw, list)
            else []
        )
        if sel_items:
            tickets = sel_items
            tickets_source = "solution_selection"
    batch_breadth = _coerce_batch_breadth(selection_input_meta.get("batch_breadth"))
    selection_profile = _coerce_string(selection_input_meta.get("breadth_profile"))
    if selection_profile is not None and selection_profile != breadth_profile:
        mismatch_warning = (
            "breadth-profile mismatch between review-ux CLI and solution-selection artifact: "
            f"review_ux={breadth_profile} solution_selection={selection_profile}"
        )
        breadth_profile_warnings.append(mismatch_warning)
        print(f"[review-ux] NOTE: {mismatch_warning}", file=sys.stderr)

    policy_cfg: BacklogPolicyConfig | None = None
    policy_config_path: Path | None = policy_config_default_path
    if policy_config_path is None or not policy_config_path.exists():
        print(
            "Missing backlog policy config (needed for high-surface gating). "
            f"Provide --policy-config or add {policy_config_default_path}.",
            file=sys.stderr,
        )
        return 2

    try:
        policy_raw = _load_yaml(policy_config_path).get("backlog_policy", {})
        if not isinstance(policy_raw, dict):
            raise ValueError("backlog_policy config must be a mapping")
        policy_cfg = BacklogPolicyConfig.from_dict(policy_raw)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as e:
        print(f"Invalid backlog policy config: {policy_config_path}: {e}", file=sys.stderr)
        return 2

    surface_area_high = set(policy_cfg.surface_area_high)

    review_tickets: list[dict[str, Any]] = []
    research_required_total = 0
    high_surface_ready_total = 0
    needs_ux_review_total = 0
    for ticket in tickets:
        stage = (_coerce_string(ticket.get("stage")) or "triage").strip()
        needs_ux_review = ticket.get("needs_ux_review") is True
        change_surface_raw = ticket.get("change_surface")
        change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
        kinds = set(_coerce_string_list(change_surface.get("kinds")))
        user_visible = bool(change_surface.get("user_visible"))
        high_surface_gated = bool(user_visible and bool(kinds & surface_area_high))

        if tickets_source == "solution_selection":
            if needs_ux_review or high_surface_gated:
                review_tickets.append(ticket)
            if needs_ux_review:
                needs_ux_review_total += 1
            if stage == "research_required":
                research_required_total += 1
            if stage == "ready_for_ticket" and high_surface_gated:
                high_surface_ready_total += 1
            continue

        # Legacy mode: UX review is driven by ticket stage + high-surface gating.
        if stage == "research_required":
            review_tickets.append(ticket)
            research_required_total += 1
            continue
        if stage == "ready_for_ticket" and high_surface_gated:
            review_tickets.append(ticket)
            high_surface_ready_total += 1

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not review_tickets:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema_version": 1,
            "generated_at": generated_at,
            "scope": {"target": target_slug, "repo_input": repo_input},
            "status": "no_research_required_tickets",
            "inputs": {
                "backlog_json": str(backlog_path),
                "solution_selection_json": str(solution_selection_path)
                if tickets_source == "solution_selection"
                else None,
                "intent_snapshot_json": intent_snapshot_json_path,
                "repo_intent_md": str(repo_intent_path),
                "policy_config": str(policy_config_path),
                "breadth_profile": breadth_profile,
                "breadth_profile_warnings": breadth_profile_warnings,
            },
            "policy": {"surface_area_high": sorted(surface_area_high)},
            "tickets_meta": {
                "tickets_total": len(tickets),
                "research_required_total": 0,
                "high_surface_ready_total": 0,
                "needs_ux_review_total": 0,
                "review_total": 0,
                "tickets_source": tickets_source,
            },
            "review_meta": {
                "breadth_profile": breadth_profile,
                "batch_breadth": batch_breadth,
                "structurally_constant_batch_dimensions": batch_breadth.get(
                    "structurally_constant_dimensions", []
                ),
            },
            "review": {"recommendations": [], "confidence": 1.0},
            "artifacts_dir": None,
        }
        out_json.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        out_md.write_text(_render_ux_review_markdown(doc), encoding="utf-8")
        print(str(out_json))
        print(str(out_md))
        return 0

    try:
        repo_intent_text = repo_intent_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"Failed reading repo intent doc: {repo_intent_path}: {e}", file=sys.stderr)
        return 2

    repo_head_sha: str | None = None
    repo_dirty = False
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            sha = proc.stdout.strip()
            if sha:
                repo_head_sha = sha
    except OSError:
        repo_head_sha = None
    if repo_head_sha is not None:
        try:
            status_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            if status_proc.returncode == 0 and status_proc.stdout.strip():
                repo_dirty = True
        except OSError:
            repo_dirty = False

    template = template_path.read_text(encoding="utf-8")
    tickets_payload: list[dict[str, Any]] = []
    for ticket in review_tickets:
        payload: dict[str, Any] = {}
        payload["fingerprint"] = ticket_export_fingerprint(ticket)
        for key in (
            "title",
            "problem",
            "user_impact",
            "severity",
            "confidence",
            "change_surface",
            "breadth",
            "problem_breadth",
            "stage",
            "risks",
            "selected_option_id",
            "selected_family_id",
            "selection_rationale",
            "repo_intent_alignment",
            "why_other_options_were_not_selected",
            "needs_ux_review",
            "selected_option",
            "proposed_fix",
            "investigation_steps",
            "success_criteria",
            "suggested_owner",
            "breadth_profile",
            "decision_basis",
            "review_domain",
        ):
            if key in ticket:
                payload[key] = ticket.get(key)
        stage = (_coerce_string(ticket.get("stage")) or "triage").strip()
        change_surface_raw = ticket.get("change_surface")
        change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
        kinds = set(_coerce_string_list(change_surface.get("kinds")))
        user_visible = bool(change_surface.get("user_visible"))
        high_surface_gated = bool(user_visible and bool(kinds & surface_area_high))
        problem_breadth = _coerce_breadth_counts(
            ticket.get("problem_breadth") if "problem_breadth" in ticket else ticket.get("breadth")
        )
        decision_basis = ticket.get("decision_basis")
        if not isinstance(decision_basis, dict):
            decision_basis = _build_decision_basis(
                problem_breadth=problem_breadth,
                batch_breadth=batch_breadth,
            )
        review_domain = _coerce_string(ticket.get("review_domain")) or _infer_review_domain(
            change_surface=change_surface,
            needs_ux_review=bool(ticket.get("needs_ux_review") is True),
        )
        payload["breadth"] = _coerce_breadth_counts(ticket.get("breadth"))
        payload["problem_breadth"] = problem_breadth
        payload["batch_breadth"] = batch_breadth
        payload["breadth_profile"] = (
            _coerce_string(ticket.get("breadth_profile")) or breadth_profile
        )
        payload["decision_basis"] = decision_basis
        payload["review_domain"] = review_domain
        payload["structurally_constant_batch_dimensions"] = batch_breadth.get(
            "structurally_constant_dimensions",
            [],
        )
        payload["high_surface_gated"] = high_surface_gated
        if tickets_source == "solution_selection":
            if ticket.get("needs_ux_review") is True:
                payload["ux_review_reason"] = "needs_ux_review"
            elif high_surface_gated:
                payload["ux_review_reason"] = "high_surface_gated"
            else:
                payload["ux_review_reason"] = "unknown"
        else:
            if stage == "research_required":
                payload["ux_review_reason"] = "research_required"
            elif stage == "ready_for_ticket" and high_surface_gated:
                payload["ux_review_reason"] = "high_surface_ready"
            else:
                payload["ux_review_reason"] = "unknown"
        tickets_payload.append(payload)

    prompt = _render_template(
        template,
        {
            "REPO_INTENT_MD": repo_intent_text,
            "REPO_HEAD_SHA": repo_head_sha or "unknown",
            "REPO_DIRTY": "true" if repo_dirty else "false",
            "INTENT_SNAPSHOT_JSON": json.dumps(intent_snapshot_obj, indent=2, ensure_ascii=False)
            if intent_snapshot_obj is not None
            else "null",
            "SURFACE_AREA_HIGH_JSON": json.dumps(
                sorted(surface_area_high), indent=2, ensure_ascii=False
            ),
            "TICKETS_JSON": json.dumps(tickets_payload, indent=2, ensure_ascii=False),
        },
    )
    prompt_hash = sha256(prompt.encode("utf-8")).hexdigest()[:16]

    artifacts_dir = out_json.parent / f"{default_name}.ux_review_artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    agent = str(args.agent)
    model = str(args.model) if isinstance(args.model, str) and args.model.strip() else None
    resume = bool(args.resume)
    force = bool(args.force)
    dry_run = bool(args.dry_run)

    tag = f"ux_review_{prompt_hash}"
    cached_path = artifacts_dir / f"{tag}.review.json"

    review_obj: dict[str, Any] | None = None
    status = "ok"
    used_cached = False
    workspace_meta: dict[str, Any] = {
        "repo_root": str(repo_root),
        "repo_head_sha": repo_head_sha,
        "repo_dirty": repo_dirty,
        "acquired_mode": None,
        "acquired_commit_sha": None,
        "provided": False,
        "error": None,
    }

    if resume and not force and cached_path.exists():
        try:
            cached = json.loads(cached_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            warnings.warn(
                f"Failed to parse cached UX review at {cached_path}: {e}; rerunning review.",
                RuntimeWarning,
                stacklevel=2,
            )
            cached = None
        except OSError as e:
            warnings.warn(
                f"Failed reading cached UX review at {cached_path}: {e}; rerunning review.",
                RuntimeWarning,
                stacklevel=2,
            )
            cached = None
        if isinstance(cached, dict):
            review_obj = cached
            status = "cached"
            used_cached = True
        elif cached is not None:
            warnings.warn(
                "Ignoring cached UX review with unexpected payload type "
                f"{type(cached).__name__} at {cached_path}; expected object.",
                RuntimeWarning,
                stacklevel=2,
            )

    if review_obj is None:
        if dry_run:
            (artifacts_dir / f"{tag}.dry_run.prompt.txt").write_text(prompt, encoding="utf-8")
            status = "dry_run"
        else:
            with tempfile.TemporaryDirectory(prefix="usertest_ux_review_") as temp_dir:
                dest_dir = Path(temp_dir) / "repo"
                workspace_dir = Path(temp_dir)
                try:
                    acquired = acquire_target(repo=str(repo_root), dest_dir=dest_dir, ref=None)
                except Exception as e:
                    workspace_meta["error"] = str(e)
                else:
                    workspace_meta["provided"] = True
                    workspace_meta["acquired_mode"] = acquired.mode
                    workspace_meta["acquired_commit_sha"] = acquired.commit_sha
                    workspace_dir = acquired.workspace_dir

                raw_text = run_backlog_prompt(
                    agent=agent,
                    prompt=prompt,
                    out_dir=artifacts_dir,
                    tag=tag,
                    model=model,
                    cfg=cfg,
                    workspace_dir=workspace_dir,
                )
            parsed = _parse_first_json_object(raw_text)
            if not isinstance(parsed, dict):
                (artifacts_dir / f"{tag}.parse_error.txt").write_text(
                    raw_text.strip() + "\n",
                    encoding="utf-8",
                )
                print(
                    "Failed to parse JSON from reviewer output "
                    f"(see artifacts under {artifacts_dir})",
                    file=sys.stderr,
                )
                return 2
            review_obj = parsed
            cached_path.write_text(
                json.dumps(review_obj, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    doc: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": generated_at,
        "scope": {"target": target_slug, "repo_input": repo_input},
        "status": status,
        "prompt_hash": prompt_hash,
        "inputs": {
            "backlog_json": str(backlog_path),
            "solution_selection_json": str(solution_selection_path)
            if tickets_source == "solution_selection"
            else None,
            "intent_snapshot_json": intent_snapshot_json_path,
            "repo_intent_md": str(repo_intent_path),
            "allow_missing_intent_snapshot": allow_missing_snapshot,
            "policy_config": str(policy_config_path),
            "breadth_profile": breadth_profile,
            "breadth_profile_warnings": breadth_profile_warnings,
        },
        "artifacts_dir": str(artifacts_dir),
        "policy": {"surface_area_high": sorted(surface_area_high)},
        "review_meta": {
            "agent": agent,
            "model": model,
            "cached": used_cached,
            "template_path": _safe_relpath(template_path, repo_root),
            "breadth_profile": breadth_profile,
            "batch_breadth": batch_breadth,
            "structurally_constant_batch_dimensions": batch_breadth.get(
                "structurally_constant_dimensions", []
            ),
            "workspace": workspace_meta,
        },
        "tickets_meta": {
            "tickets_total": len(tickets),
            "research_required_total": research_required_total,
            "high_surface_ready_total": high_surface_ready_total,
            "needs_ux_review_total": needs_ux_review_total,
            "review_total": len(review_tickets),
            "tickets_source": tickets_source,
        },
        "review": review_obj,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(_render_ux_review_markdown(doc), encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    print(f"Reviewer status: {status}")
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
