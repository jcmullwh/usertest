# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.commands.atom_actions import _update_atom_actions_from_exports
from usertest_backlog.commands.plan_cleanup import (
    _cleanup_actioned_plan_queue_duplicates,
    _cleanup_stale_generated_scope_ticket_files,
    _cleanup_stale_ticket_idea_files,
    _refresh_generated_ticket_idea_file,
    _ticket_queue_dirs,
    _write_ticket_idea_file,
)
from usertest_backlog.commands.review_ux import (
    _apply_ux_review_to_plan_ticket,
    _index_ux_recommendations,
    _load_optional_json_object,
    _move_plan_ticket_to_bucket,
    _pick_ux_recommended_approach,
    _render_ux_review_section_for_ticket,
)
from usertest_backlog.shared import *

_RESEARCH_TICKET_TEMPLATE_MD = """## Research / ADR Template

### Intent check
- What does `configs/repo_intent.md` say about this proposal?
- Does this solve a repo-wide problem or a single mission-local preference?

### Surface consolidation checklist
- Can an existing command be parameterized instead of adding a new command?
- Can docs/examples remove the friction without any new surface area?
- If a new surface is required, what is the minimal addition?

### Alternatives considered
- Parameterize existing command(s)
- Improve docs/examples
- Defer / do nothing

### Decision outcome
- Outcome: (approved | rejected | deferred)
- Notes:
"""


def _render_export_issue_body(
    *,
    ticket: dict[str, Any],
    fingerprint: str,
    export_kind: str,
    surface_area_high: set[str],
) -> str:
    """Render export issue body output text.

    Parameters
    ----------
    ticket:
        Ticket payload mapping.
    fingerprint:
        Input parameter.
    export_kind:
        Input parameter.
    surface_area_high:
        Input parameter.

    Returns
    -------
    str
        Normalized string result.
    """
    def _append_section_text(lines: list[str], heading: str, value: Any) -> None:
        text = _coerce_string(value)
        if not text:
            return
        lines.append(heading)
        lines.append("")
        lines.append(text)
        lines.append("")

    def _append_section_list(lines: list[str], heading: str, values: Any) -> None:
        items = _coerce_string_list(values)
        if not items:
            return
        lines.append(heading)
        lines.append("")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")

    title = _coerce_string(ticket.get("title")) or ""
    problem = _coerce_string(ticket.get("problem")) or ""
    user_impact = _coerce_string(ticket.get("user_impact")) or ""
    proposed_fix = _coerce_string(ticket.get("proposed_fix")) or ""

    change_surface_raw = ticket.get("change_surface")
    change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
    kinds = sorted(set(_coerce_string_list(change_surface.get("kinds"))))
    user_visible = bool(change_surface.get("user_visible"))
    breadth_raw = ticket.get("breadth")
    breadth = breadth_raw if isinstance(breadth_raw, dict) else {}
    breadth_profile = _coerce_string(ticket.get("breadth_profile"))
    decision_basis_raw = ticket.get("decision_basis")
    decision_basis = decision_basis_raw if isinstance(decision_basis_raw, dict) else {}
    review_domain = _coerce_string(ticket.get("review_domain"))

    lines: list[str] = []
    lines.append(f"- Fingerprint: `{fingerprint}`")
    lines.append(f"- Export kind: `{export_kind}`")
    stage = _coerce_string(ticket.get("stage"))
    if stage:
        lines.append(f"- Stage: `{stage}`")
    severity = _coerce_string(ticket.get("severity"))
    if severity:
        lines.append(f"- Severity: `{severity}`")
    if kinds:
        lines.append(f"- Change surface kinds: `{', '.join(kinds)}`")
    if user_visible:
        gated = bool(set(kinds) & surface_area_high)
        lines.append(f"- User-visible: `true` (high-surface gated: `{str(gated).lower()}`)")
    lines.append("")

    if title:
        lines.append("## Title")
        lines.append("")
        lines.append(title)
        lines.append("")

    if problem:
        lines.append("## Problem")
        lines.append("")
        lines.append(problem)
        lines.append("")

    if user_impact:
        lines.append("## User impact")
        lines.append("")
        lines.append(user_impact)
        lines.append("")

    problem_record_raw = ticket.get("problem_record")
    problem_record = problem_record_raw if isinstance(problem_record_raw, dict) else {}
    evidence_summary = _coerce_string(problem_record.get("evidence_summary")) or _coerce_string(
        ticket.get("evidence_summary")
    )
    if evidence_summary:
        lines.append("## Evidence summary")
        lines.append("")
        lines.append(evidence_summary)
        lines.append("")

    if proposed_fix:
        lines.append("## Proposed fix")
        lines.append("")
        lines.append(proposed_fix)
        lines.append("")

    context_breadth = (
        decision_basis.get("context_breadth")
        if isinstance(decision_basis.get("context_breadth"), dict)
        else {}
    )
    observation_breadth = (
        decision_basis.get("observation_breadth")
        if isinstance(decision_basis.get("observation_breadth"), dict)
        else {}
    )
    struct_dims = [
        item
        for item in decision_basis.get("structurally_constant_dimensions", [])
        if isinstance(item, str) and item.strip()
    ]
    if breadth_profile or context_breadth or observation_breadth or review_domain:
        lines.append("## Evidence breadth context")
        lines.append("")
        if breadth_profile:
            lines.append(f"- Breadth profile: `{breadth_profile}`")
        if review_domain:
            lines.append(f"- Review domain: `{review_domain}`")
        if context_breadth:
            context_bits = [
                f"{dim}={int(val)}"
                for dim in ("missions", "targets", "repo_inputs")
                for val in [context_breadth.get(dim)]
                if isinstance(val, (int, float))
            ]
            if context_bits:
                lines.append(f"- Context breadth: `{', '.join(context_bits)}`")
        if observation_breadth:
            observation_bits = [
                f"{dim}={int(val)}"
                for dim in ("runs", "agents", "personas")
                for val in [observation_breadth.get(dim)]
                if isinstance(val, (int, float))
            ]
            if observation_bits:
                lines.append(f"- Observation breadth: `{', '.join(observation_bits)}`")
        if struct_dims:
            lines.append(
                "- Structurally constant dimensions: "
                f"`{', '.join(sorted(set(struct_dims)))}`"
            )
        if breadth_profile == "internal_maintenance" and struct_dims:
            lines.append(
                "- Note: in internal-maintenance mode, structurally constant context "
                "dimensions are not negative evidence for existing-surface hardening."
            )
        lines.append("")

    inv_steps = _coerce_string_list(ticket.get("investigation_steps"))
    if inv_steps:
        lines.append("## Investigation steps")
        lines.append("")
        for step in inv_steps:
            lines.append(f"- {step}")
        lines.append("")

    research_raw = ticket.get("research")
    research = research_raw if isinstance(research_raw, dict) else {}
    if research:
        lines.append("## Research context")
        lines.append("")
        repro = _coerce_string(research.get("reproduction_status"))
        diff_cls = _coerce_string(research.get("diff_classification"))
        broader = _coerce_string(research.get("broader_class_assessment"))
        writes_used = research.get("writes_used")
        writes_used_s = (
            "true" if writes_used is True else "false" if writes_used is False else None
        )
        if repro:
            lines.append(f"- Reproduction status: `{repro}`")
        if diff_cls:
            lines.append(f"- Diff classification: `{diff_cls}`")
        if broader:
            lines.append(f"- Broader class assessment: `{broader}`")
        if writes_used_s is not None:
            lines.append(f"- Writes used during research: `{writes_used_s}`")
        writes_purpose = _coerce_string_list(research.get("writes_purpose"))
        if writes_purpose:
            lines.append("- Writes purpose:")
            for item in writes_purpose:
                lines.append(f"  - {item}")
        lines.append("")
        _append_section_list(lines, "### Root cause hypotheses", research.get("root_cause_hypotheses"))
        _append_section_list(lines, "### Unknowns / next evidence needed", research.get("unknowns"))
        _append_section_list(lines, "### Diff notes", research.get("diff_suspicious_reasons"))

    selected_solution_raw = ticket.get("selected_solution")
    selected_solution = selected_solution_raw if isinstance(selected_solution_raw, dict) else {}
    selected_option_raw = selected_solution.get("selected_option")
    selected_option = selected_option_raw if isinstance(selected_option_raw, dict) else {}
    selected_family_id = _coerce_string(selected_solution.get("selected_family_id")) or _coerce_string(
        ticket.get("selected_family_id")
    )
    selected_option_id = _coerce_string(selected_solution.get("selected_option_id")) or _coerce_string(
        ticket.get("selected_option_id")
    )
    selected_option_summary = _coerce_string(selected_option.get("summary"))
    selection_rationale = _coerce_string(selected_solution.get("selection_rationale"))
    repo_intent_alignment = _coerce_string(selected_solution.get("repo_intent_alignment"))
    why_not_others = _coerce_string(selected_solution.get("why_other_options_were_not_selected"))
    if (
        selected_solution
        or selected_family_id
        or selected_option_id
        or selected_option_summary
        or selection_rationale
    ):
        lines.append("## Selected solution context")
        lines.append("")
        if selected_family_id:
            lines.append(f"- Selected family: `{selected_family_id}`")
        if selected_option_id:
            lines.append(f"- Selected option: `{selected_option_id}`")
        component = _coerce_string(selected_solution.get("component")) or _coerce_string(
            ticket.get("component")
        )
        if component:
            lines.append(f"- Component: `{component}`")
        intent_risk = _coerce_string(selected_solution.get("intent_risk")) or _coerce_string(
            ticket.get("intent_risk")
        )
        if intent_risk:
            lines.append(f"- Intent risk: `{intent_risk}`")
        lines.append("")
        _append_section_text(lines, "### Selected option summary", selected_option_summary)
        _append_section_text(lines, "### Selection rationale", selection_rationale)
        _append_section_text(lines, "### Repo intent alignment", repo_intent_alignment)
        _append_section_text(lines, "### Why other options were not selected", why_not_others)
        _append_section_text(lines, "### Change surface hypothesis", selected_option.get("change_surface_hypothesis"))
        _append_section_text(lines, "### Tradeoffs", selected_option.get("tradeoffs"))
        _append_section_text(lines, "### Recurrence prevention", selected_option.get("recurrence_prevention"))
        _append_section_text(lines, "### Test implications", selected_option.get("test_implications"))
        _append_section_text(lines, "### Option rationale", selected_option.get("rationale"))

    solution_options_raw = ticket.get("solution_options")
    solution_options = (
        [item for item in solution_options_raw if isinstance(item, dict)]
        if isinstance(solution_options_raw, list)
        else []
    )
    if solution_options:
        lines.append("## Solution options considered")
        lines.append("")
        for opt in solution_options:
            option_id = _coerce_string(opt.get("option_id")) or "(no option_id)"
            family_id = _coerce_string(opt.get("family_id")) or "unknown"
            label = f"### `{option_id}`"
            if selected_option_id and option_id == selected_option_id:
                label += " (selected)"
            lines.append(label)
            lines.append("")
            lines.append(f"- Family: `{family_id}`")
            summary = _coerce_string(opt.get("summary"))
            if summary:
                lines.append(f"- Summary: {summary}")
            change_surface_hypothesis = _coerce_string(opt.get("change_surface_hypothesis"))
            if change_surface_hypothesis:
                lines.append(f"- Change surface hypothesis: `{change_surface_hypothesis}`")
            tradeoffs = _coerce_string(opt.get("tradeoffs"))
            if tradeoffs:
                lines.append(f"- Tradeoffs: {tradeoffs}")
            recurrence = _coerce_string(opt.get("recurrence_prevention"))
            if recurrence:
                lines.append(f"- Recurrence prevention: {recurrence}")
            tests = _coerce_string(opt.get("test_implications"))
            if tests:
                lines.append(f"- Test implications: {tests}")
            rationale = _coerce_string(opt.get("rationale"))
            if rationale:
                lines.append(f"- Rationale: {rationale}")
            lines.append("")

    success = _coerce_string_list(ticket.get("success_criteria"))
    if success:
        lines.append("## Success criteria")
        lines.append("")
        for criterion in success:
            lines.append(f"- {criterion}")
        lines.append("")

    change_plan_raw = ticket.get("change_plan")
    change_plan = change_plan_raw if isinstance(change_plan_raw, dict) else {}
    if change_plan:
        lines.append("## Implementation plan")
        lines.append("")
        change_plan_id = _coerce_string(change_plan.get("change_plan_id")) or _coerce_string(
            ticket.get("change_plan_id")
        )
        if change_plan_id:
            lines.append(f"- Change plan ID: `{change_plan_id}`")
        plan_status = _coerce_string(change_plan.get("change_plan_status"))
        if plan_status:
            lines.append(f"- Plan status: `{plan_status}`")
        suggested_owner = _coerce_string(change_plan.get("suggested_owner")) or _coerce_string(
            ticket.get("suggested_owner")
        )
        if suggested_owner:
            lines.append(f"- Suggested owner: `{suggested_owner}`")
        related_change_plans = _coerce_string_list(change_plan.get("related_change_plan_ids"))
        if related_change_plans:
            lines.append("- Related change plans:")
            for rel in related_change_plans:
                lines.append(f"  - `{rel}`")
        lines.append("")
        _append_section_list(
            lines,
            "### Implementation steps",
            _coerce_string_list(ticket.get("implementation_steps"))
            or _coerce_string_list(change_plan.get("implementation_steps")),
        )
        _append_section_list(
            lines,
            "### Verification steps",
            _coerce_string_list(ticket.get("verification_steps"))
            or _coerce_string_list(change_plan.get("verification_steps")),
        )
        _append_section_text(
            lines,
            "### Rollback notes",
            _coerce_string(ticket.get("rollback_notes")) or _coerce_string(change_plan.get("rollback_notes")),
        )

    if breadth:
        lines.append("## Problem-local evidence breadth (counts)")
        lines.append("")
        for dim in ("missions", "targets", "repo_inputs", "agents", "personas", "runs"):
            val = breadth.get(dim)
            if isinstance(val, (int, float)):
                lines.append(f"- {dim}: {int(val)}")
        lines.append("")

    evidence_ids = _coerce_string_list(ticket.get("evidence_atom_ids"))
    if evidence_ids:
        lines.append("## Evidence atom ids")
        lines.append("")
        for atom_id in evidence_ids[:40]:
            lines.append(f"- `{atom_id}`")
        lines.append("")

    if export_kind == "research":
        lines.append(_RESEARCH_TICKET_TEMPLATE_MD.rstrip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_ticket_export_markdown(doc: dict[str, Any]) -> str:
    """Render ticket export markdown output text.

    Parameters
    ----------
    doc:
        Structured document payload.

    Returns
    -------
    str
        Normalized string result.
    """
    generated_at = _coerce_string(doc.get("generated_at")) or "unknown"
    scope = doc.get("scope") if isinstance(doc.get("scope"), dict) else {}
    target = _coerce_string(scope.get("target")) or "all"
    repo_input = _coerce_string(scope.get("repo_input"))

    lines: list[str] = []
    lines.append("# Ticket Export")
    lines.append("")
    lines.append(f"- Generated at: `{generated_at}`")
    lines.append(f"- Scope target: `{target}`")
    if repo_input is not None:
        lines.append(f"- Scope repo_input: `{repo_input}`")

    filters = doc.get("filters") if isinstance(doc.get("filters"), dict) else {}
    stages = filters.get("stages")
    if isinstance(stages, list) and stages:
        lines.append(f"- Stages: `{', '.join([s for s in stages if isinstance(s, str)])}`")
    min_sev = _coerce_string(filters.get("min_severity"))
    if min_sev:
        lines.append(f"- Min severity: `{min_sev}`")
    include_actioned = filters.get("include_actioned")
    if isinstance(include_actioned, bool):
        lines.append(f"- Include actioned: `{str(include_actioned).lower()}`")
    include_discarded = filters.get("include_discarded")
    if isinstance(include_discarded, bool):
        lines.append(f"- Include discarded: `{str(include_discarded).lower()}`")
    lines.append("")

    exports_raw = doc.get("exports")
    exports = (
        [item for item in exports_raw if isinstance(item, dict)]
        if isinstance(exports_raw, list)
        else []
    )

    research = [e for e in exports if _coerce_string(e.get("export_kind")) == "research"]
    impl = [e for e in exports if _coerce_string(e.get("export_kind")) == "implementation"]

    def _render_section(title: str, items: list[dict[str, Any]]) -> None:
        """Render section output text.

        Parameters
        ----------
        title:
            Title text input.
        items:
            Collection items to process.

        Returns
        -------
        None
            None.
        """
        lines.append(f"## {title}")
        lines.append("")
        if not items:
            lines.append("- (none)")
            lines.append("")
            return
        for item in items:
            issue_title = _coerce_string(item.get("title")) or "Untitled"
            fingerprint = _coerce_string(item.get("fingerprint")) or "unknown"
            lines.append(f"### {issue_title}")
            lines.append("")
            lines.append(f"- Fingerprint: `{fingerprint}`")
            owner_repo_raw = item.get("owner_repo")
            owner_repo = owner_repo_raw if isinstance(owner_repo_raw, dict) else {}
            idea_path = _coerce_string(owner_repo.get("idea_path"))
            if idea_path:
                lines.append(f"- Idea file: `{idea_path}`")
            owner_root = _coerce_string(owner_repo.get("root"))
            if owner_root:
                lines.append(f"- Owner repo root: `{owner_root}`")
            body = _coerce_string(item.get("body_markdown")) or ""
            if body:
                lines.append("")
                lines.append(body.rstrip())
            lines.append("")

    _render_section("Research / Design", research)
    _render_section("Implementation", impl)
    return "\n".join(lines).rstrip() + "\n"


def _cmd_reports_export_tickets(args: argparse.Namespace) -> int:
    """Execute the `reports export tickets` command handler.

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

    actions_arg: Path | None = args.actions_yaml
    if actions_arg is not None:
        actions_path = _resolve_optional_path(repo_root, actions_arg) or actions_arg.resolve()
    else:
        actions_path = repo_root / "configs" / "backlog_actions.yaml"

    atom_actions_arg: Path | None = args.atom_actions_yaml
    if atom_actions_arg is not None:
        atom_actions_path = (
            _resolve_optional_path(repo_root, atom_actions_arg) or atom_actions_arg.resolve()
        )
    else:
        atom_actions_path = repo_root / "configs" / "backlog_atom_actions.yaml"

    try:
        actions = _load_backlog_actions_yaml(actions_path)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        backlog_doc = json.loads(backlog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Failed to parse backlog JSON: {backlog_path}: {e}", file=sys.stderr)
        return 2
    if not isinstance(backlog_doc, dict):
        print(f"Invalid backlog JSON (expected object): {backlog_path}", file=sys.stderr)
        return 2
    backlog_scope_raw = backlog_doc.get("scope")
    backlog_scope = backlog_scope_raw if isinstance(backlog_scope_raw, dict) else {}
    backlog_scope_repo_input = _coerce_string(backlog_scope.get("repo_input"))

    tickets_raw = backlog_doc.get("tickets")
    tickets = (
        [item for item in tickets_raw if isinstance(item, dict)]
        if isinstance(tickets_raw, list)
        else []
    )
    policy_cfg: BacklogPolicyConfig | None = None
    policy_config_path: Path | None
    if args.out_json is not None:
        out_json = _resolve_optional_path(repo_root, args.out_json) or args.out_json.resolve()
    else:
        out_json = compiled_dir / f"{default_name}.tickets_export.json"

    if args.out_md is not None:
        out_md = _resolve_optional_path(repo_root, args.out_md) or args.out_md.resolve()
    else:
        out_md = out_json.with_suffix(".md")

    ux_review_json_path = compiled_dir / f"{default_name}.ux_review.json"
    ux_review_md_path = ux_review_json_path.with_suffix(".md")
    ux_review_doc = _load_optional_json_object(ux_review_json_path)
    ux_recommendations_by_fingerprint = (
        _index_ux_recommendations(ux_review_doc) if ux_review_doc is not None else {}
    )
    backlog_input_raw = backlog_doc.get("input")
    backlog_input = backlog_input_raw if isinstance(backlog_input_raw, dict) else {}
    ux_review_inputs_raw = ux_review_doc.get("inputs") if isinstance(ux_review_doc, dict) else None
    ux_review_inputs = ux_review_inputs_raw if isinstance(ux_review_inputs_raw, dict) else {}
    export_breadth_profile = _normalize_breadth_profile(
        _coerce_string(backlog_input.get("breadth_profile"))
        or next(
            (
                _coerce_string(ticket.get("breadth_profile"))
                for ticket in tickets
                if _coerce_string(ticket.get("breadth_profile"))
            ),
            None,
        )
        or _coerce_string(ux_review_inputs.get("breadth_profile"))
        or next(
            (
                _coerce_string(rec.get("breadth_profile"))
                for recs in ux_recommendations_by_fingerprint.values()
                for rec in recs
                if _coerce_string(rec.get("breadth_profile"))
            ),
            None,
        )
    )
    if args.policy_config is not None:
        policy_config_path = (
            _resolve_optional_path(repo_root, args.policy_config) or args.policy_config.resolve()
        )
    else:
        default_policy = _default_breadth_profile_policy_path(
            repo_root,
            export_breadth_profile,
        )
        policy_config_path = default_policy if default_policy.exists() else None
    if policy_config_path is None or not policy_config_path.exists():
        print(
            "Missing backlog policy config (needed for high-surface gating). "
            "Provide --policy-config or add the matching configs/backlog_policy*.yaml.",
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

    stage_filters = [s.strip() for s in args.stage if isinstance(s, str) and s.strip()]
    stages = stage_filters if stage_filters else ["triage", "ready_for_ticket", "research_required"]
    min_severity = str(args.min_severity)
    include_actioned = bool(args.include_actioned)
    include_discarded = bool(getattr(args, "include_discarded", False))

    print(
        "Export filters:",
        f"stages={stages}",
        f"min_severity={min_severity}",
        f"include_actioned={include_actioned}",
        f"include_discarded={include_discarded}",
        sep=" ",
    )

    exports: list[dict[str, Any]] = []
    queued_refs: list[dict[str, str]] = []
    skipped_actioned = 0
    skipped_discarded = 0
    skipped_existing_plan = 0
    skipped_stage = 0
    skipped_severity = 0
    idea_files_written: list[str] = []
    plan_index_cache: dict[Path, dict[str, dict[str, Any]]] = {}
    skip_plan_folder_dedupe = bool(getattr(args, "skip_plan_folder_dedupe", False))
    ux_plan_tickets_updated = 0
    ux_idea_files_updated = 0
    ux_tickets_deferred = 0
    swept_actioned_queue_dupes_removed = 0
    swept_actioned_bucket_dupes_removed = 0
    swept_scope_stale_generated_removed = 0
    generated_queue_files_refreshed = 0
    actions_mutated = False
    keep_fingerprints_by_owner_root: dict[Path, set[str]] = {}
    pre_export_atom_sync_meta: dict[str, Any] | None = None

    for ticket in tickets:
        fingerprint = ticket_export_fingerprint(ticket)
        owner_repo_root, _owner_repo_input, _owner_repo_resolution = _resolve_owner_repo_root(
            ticket=ticket,
            scope_repo_input=backlog_scope_repo_input,
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )
        keep_fingerprints_by_owner_root.setdefault(owner_repo_root.resolve(), set()).add(
            fingerprint
        )

    try:
        atom_actions = _load_atom_actions_yaml(atom_actions_path)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    pre_export_atom_sync_meta = _reconcile_atom_actions_from_plan_folders(
        atom_actions=atom_actions,
        owner_roots=sorted(keep_fingerprints_by_owner_root.keys(), key=lambda p: str(p)),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    _write_atom_actions_yaml(atom_actions_path, atom_actions)

    for ticket in tickets:
        stage = (_coerce_string(ticket.get("stage")) or "triage").strip()
        fingerprint = ticket_export_fingerprint(ticket)
        ux_recs = ux_recommendations_by_fingerprint.get(fingerprint) or []

        change_surface_raw = ticket.get("change_surface")
        change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
        kinds = set(_coerce_string_list(change_surface.get("kinds")))
        user_visible = bool(change_surface.get("user_visible"))
        high_surface_ready = bool(
            stage == "ready_for_ticket" and user_visible and bool(kinds & surface_area_high)
        )

        stage_override: str | None = None
        export_kind_override: str | None = None
        defer_to_bucket: str | None = None
        ux_section: str | None = None
        ux_approach = _pick_ux_recommended_approach(ux_recs) if ux_recs else None
        selected_ux_rec: dict[str, Any] | None = None
        if ux_recs and ux_review_doc is not None:
            selected_ux_rec = next(
                (
                    rec
                    for rec in ux_recs
                    if (_coerce_string(rec.get("recommended_approach")) or "") == ux_approach
                ),
                ux_recs[0] if ux_recs else None,
            )
            ux_section = _render_ux_review_section_for_ticket(
                ux_review_doc=ux_review_doc,
                ux_review_json_path=ux_review_json_path,
                ux_review_md_path=ux_review_md_path,
                fingerprint=fingerprint,
                recs=ux_recs,
            )
            if stage == "research_required" and ux_approach in (
                "docs",
                "parameterize_existing",
                "accept_existing_surface",
            ):
                stage_override = "ready_for_ticket"
                export_kind_override = "implementation"
            elif stage == "research_required" and ux_approach == "defer":
                defer_to_bucket = "0.1 - deferred"
            elif high_surface_ready and ux_approach in (
                "docs",
                "parameterize_existing",
                "accept_existing_surface",
            ):
                export_kind_override = "implementation"
            elif high_surface_ready and ux_approach == "defer":
                defer_to_bucket = "0.1 - deferred"

        stage_effective = stage_override or stage

        if stage_effective not in stages:
            skipped_stage += 1
            continue
        severity = (_coerce_string(ticket.get("severity")) or "medium").strip().lower()
        if _severity_rank(severity) < _severity_rank(min_severity):
            skipped_severity += 1
            continue

        action_entry = actions.get(fingerprint)
        if action_entry is not None:
            action_status = _coerce_string(action_entry.get("status"))
            if action_status == "discarded" and not include_discarded:
                skipped_discarded += 1
                continue
            if action_status != "discarded" and not include_actioned:
                skipped_actioned += 1
                continue

        export_kind = "implementation"
        if stage_effective == "research_required":
            export_kind = "research"
        elif user_visible and bool(kinds & surface_area_high):
            export_kind = "research"
        if export_kind_override is not None:
            export_kind = export_kind_override

        title = _coerce_string(ticket.get("title")) or "Untitled"
        issue_title = f"[Research] {title}" if export_kind == "research" else title

        ticket_for_body = dict(ticket)
        if isinstance(selected_ux_rec, dict):
            for key in ("breadth_profile", "decision_basis", "review_domain"):
                if key in selected_ux_rec:
                    ticket_for_body[key] = selected_ux_rec.get(key)
        ticket_for_body["stage"] = stage_effective
        base_body = _render_export_issue_body(
            ticket=ticket_for_body,
            fingerprint=fingerprint,
            export_kind=export_kind,
            surface_area_high=surface_area_high,
        )
        body = base_body
        if ux_section is not None:
            body = ux_section.strip() + "\n\n" + base_body.strip() + "\n"

        labels: list[str] = []
        labels.append(f"stage:{stage_effective}")
        labels.append(f"severity:{severity}")
        if export_kind == "research":
            labels.append("type:research")
        else:
            labels.append("type:implementation")
        if ux_approach:
            labels.append(f"ux:{ux_approach}")
        owner = _coerce_string(ticket.get("suggested_owner")) or _coerce_string(
            ticket.get("component")
        )
        if owner:
            labels.append(f"owner:{owner}")
        for kind in sorted(kinds):
            labels.append(f"surface:{kind}")

        owner_repo_root, owner_repo_input, owner_repo_resolution = _resolve_owner_repo_root(
            ticket=ticket,
            scope_repo_input=backlog_scope_repo_input,
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )

        if not include_actioned and not skip_plan_folder_dedupe:
            owner_key = owner_repo_root.resolve()
            if owner_key not in plan_index_cache:
                swept_actioned_queue_dupes_removed += _cleanup_actioned_plan_queue_duplicates(
                    owner_repo_root=owner_key,
                )
                swept_actioned_bucket_dupes_removed += _dedupe_actioned_plan_ticket_files(
                    owner_root=owner_key,
                )
                plan_index_cache[owner_key] = _scan_plan_ticket_index(
                    owner_root=owner_key,
                    include_discarded=False,
                )
            existing = plan_index_cache[owner_key].get(fingerprint)
            if isinstance(existing, dict):
                skipped_existing_plan += 1
                desired_status = _normalize_atom_status(_coerce_string(existing.get("status")))
                if desired_status not in ("queued", "actioned"):
                    desired_status = "queued"

                if desired_status == "actioned":
                    _cleanup_stale_ticket_idea_files(
                        ticket=ticket,
                        fingerprint=fingerprint,
                        owner_repo_root=owner_repo_root,
                        repo_root=repo_root,
                        scope_repo_input=backlog_scope_repo_input,
                        cli_repo_input=repo_input,
                    )

                queue_paths = [
                    item for item in existing.get("paths", []) if isinstance(item, str) and item
                ]
                queue_dir_roots = {path.resolve() for path in _ticket_queue_dirs(owner_repo_root)}
                for path_s in queue_paths:
                    path_obj = Path(path_s)
                    try:
                        parent_resolved = path_obj.parent.resolve()
                    except OSError:
                        continue
                    if parent_resolved not in queue_dir_roots:
                        continue
                    if _refresh_generated_ticket_idea_file(
                        path=path_obj,
                        issue_title=issue_title,
                        fingerprint=fingerprint,
                        body_markdown=base_body,
                        scope_target=target_slug,
                        scope_repo_input=backlog_scope_repo_input or repo_input,
                        execution_domain=_coerce_string(ticket.get("execution_domain")),
                        execution_conflict_keys=_coerce_string_list(ticket.get("execution_conflict_keys")),
                        ux_review_section=ux_section,
                    ):
                        generated_queue_files_refreshed += 1
                        if ux_section is not None:
                            ux_plan_tickets_updated += 1
                if ux_section is not None and queue_paths:
                    for path_s in queue_paths:
                        if _apply_ux_review_to_plan_ticket(
                            path=Path(path_s),
                            ux_section=ux_section,
                            stage_override=stage_override,
                            export_kind_override=export_kind_override,
                        ):
                            ux_plan_tickets_updated += 1
                    if defer_to_bucket is not None:
                        primary_path = Path(queue_paths[0])
                        moved = _move_plan_ticket_to_bucket(
                            path=primary_path,
                            owner_repo_root=owner_repo_root,
                            bucket=defer_to_bucket,
                        )
                        if moved is not None:
                            queue_paths[0] = str(moved)
                            desired_status = "actioned"
                            ux_tickets_deferred += 1
                            if fingerprint not in actions:
                                actions[fingerprint] = {
                                    "fingerprint": fingerprint,
                                    "status": "deferred",
                                    "notes": "Deferred by UX review recommendation.",
                                }
                                actions_mutated = True
                elif (
                    desired_status != "actioned"
                    and stage_effective == "ready_for_ticket"
                    and queue_paths
                ):
                    moved = _move_plan_ticket_to_bucket(
                        path=Path(queue_paths[0]),
                        owner_repo_root=owner_repo_root,
                        bucket="2 - ready",
                    )
                    if moved is not None:
                        queue_paths[0] = str(moved)
                for atom_id in _coerce_string_list(ticket.get("evidence_atom_ids")):
                    ref: dict[str, str] = {
                        "atom_id": atom_id,
                        "fingerprint": fingerprint,
                        "owner_root": str(owner_repo_root),
                        "desired_status": desired_status,
                    }
                    if queue_paths:
                        ref["idea_path"] = queue_paths[0]
                    queued_refs.append(ref)
                continue

        idea_path = _write_ticket_idea_file(
            ticket=ticket_for_body,
            issue_title=issue_title,
            fingerprint=fingerprint,
            body_markdown=base_body,
            owner_repo_root=owner_repo_root,
            scope_target=target_slug,
            scope_repo_input=backlog_scope_repo_input or repo_input,
            ux_review_section=ux_section,
        )
        _cleanup_stale_ticket_idea_files(
            ticket=ticket,
            fingerprint=fingerprint,
            owner_repo_root=owner_repo_root,
            repo_root=repo_root,
            scope_repo_input=backlog_scope_repo_input,
            cli_repo_input=repo_input,
            keep_path=idea_path,
        )
        if ux_section is not None:
            ux_idea_files_updated += 1
        deferred_moved = False
        if defer_to_bucket is not None:
            moved = _move_plan_ticket_to_bucket(
                path=idea_path,
                owner_repo_root=owner_repo_root,
                bucket=defer_to_bucket,
            )
            if moved is not None:
                idea_path = moved
                deferred_moved = True
                ux_tickets_deferred += 1

                if fingerprint not in actions:
                    actions[fingerprint] = {
                        "fingerprint": fingerprint,
                        "status": "deferred",
                        "notes": "Deferred by UX review recommendation.",
                    }
                    actions_mutated = True

        idea_files_written.append(str(idea_path))
        for atom_id in _coerce_string_list(ticket.get("evidence_atom_ids")):
            queued_refs.append(
                {
                    "atom_id": atom_id,
                    "fingerprint": fingerprint,
                    "idea_path": str(idea_path),
                    "owner_root": str(owner_repo_root),
                    "desired_status": "actioned" if deferred_moved else "queued",
                }
            )

        if deferred_moved:
            continue

        exports.append(
            {
                "fingerprint": fingerprint,
                "export_kind": export_kind,
                "title": issue_title,
                "labels": labels,
                "body_markdown": body,
                "source_ticket": {
                    "fingerprint": fingerprint,
                    "stage": stage_effective,
                    "severity": severity,
                },
                "owner_repo": {
                    "repo_input": owner_repo_input,
                    "root": str(owner_repo_root),
                    "resolution": owner_repo_resolution,
                    "idea_path": str(idea_path),
                },
                "action_ledger": actions.get(fingerprint),
            }
        )

    for owner_root, keep_fingerprints in keep_fingerprints_by_owner_root.items():
        swept_scope_stale_generated_removed += _cleanup_stale_generated_scope_ticket_files(
            owner_repo_root=owner_root,
            target_slug=target_slug,
            keep_fingerprints=keep_fingerprints,
        )

    try:
        atom_actions = _load_atom_actions_yaml(atom_actions_path)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    atom_status_meta = _update_atom_actions_from_exports(
        atom_actions=atom_actions,
        queued_refs=queued_refs,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        export_json_path=out_json,
    )
    _write_atom_actions_yaml(atom_actions_path, atom_actions)

    if actions_mutated:
        _write_backlog_actions_yaml(actions_path, actions)

    export_doc: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": {"target": target_slug, "repo_input": repo_input},
        "inputs": {
            "backlog_json": str(backlog_path),
            "actions_yaml": str(actions_path),
            "atom_actions_yaml": str(atom_actions_path),
            "policy_config": str(policy_config_path),
            "breadth_profile": export_breadth_profile,
            "ux_review_json": str(ux_review_json_path) if ux_review_json_path.exists() else None,
            "ux_review_md": str(ux_review_md_path) if ux_review_md_path.exists() else None,
        },
        "filters": {
            "stages": stages,
            "min_severity": min_severity,
            "include_actioned": include_actioned,
            "include_discarded": include_discarded,
        },
        "policy": {
            "surface_area_high": sorted(surface_area_high),
        },
        "stats": {
            "tickets_total": len(tickets),
            "exports_total": len(exports),
            "skipped_actioned": skipped_actioned,
            "skipped_discarded": skipped_discarded,
            "skipped_existing_plan": skipped_existing_plan,
            "skipped_stage": skipped_stage,
            "skipped_severity": skipped_severity,
            "actioned_total": len(actions),
            "idea_files_written": len(idea_files_written),
            "generated_queue_files_refreshed": generated_queue_files_refreshed,
            "swept_actioned_queue_dupes_removed": swept_actioned_queue_dupes_removed,
            "swept_actioned_bucket_dupes_removed": swept_actioned_bucket_dupes_removed,
            "swept_scope_stale_generated_removed": swept_scope_stale_generated_removed,
            "ux_recommendations_loaded": len(ux_recommendations_by_fingerprint),
            "ux_plan_tickets_updated": ux_plan_tickets_updated,
            "ux_idea_files_updated": ux_idea_files_updated,
            "ux_tickets_deferred": ux_tickets_deferred,
            "pre_export_atom_sync": pre_export_atom_sync_meta,
            "atom_status_updates": atom_status_meta,
        },
        "idea_files": idea_files_written,
        "exports": exports,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(export_doc, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out_md.write_text(_render_ticket_export_markdown(export_doc), encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    for path in idea_files_written:
        print(path)
    print(json.dumps(export_doc["stats"], indent=2, ensure_ascii=False))
    return 0




__all__ = [name for name in globals() if not name.startswith("__")]
