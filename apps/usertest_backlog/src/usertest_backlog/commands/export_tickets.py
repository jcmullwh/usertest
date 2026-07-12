# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256

from backlog_core import assess_ticket_readiness
from backlog_miner.research_evidence import verify_persisted_research_evidence
from backlog_repo import verify_outcome_record_provenance
from backlog_repo.plan_scope import (
    parse_plan_target_contract_markdown,
    render_plan_target_contract_markdown,
)
from backlog_repo.ticket_provenance import (
    canonical_plan_sha256,
    canonical_ticket_body_sha256,
    parse_verification_contract_markdown,
    render_verification_contract_markdown,
)

from usertest_backlog.commands.atom_actions import _update_atom_actions_from_exports
from usertest_backlog.commands.plan_cleanup import (
    _cleanup_stale_generated_scope_ticket_files,
    _cleanup_stale_ticket_idea_files,
    _refresh_generated_ticket_idea_file,
    _ticket_queue_dirs,
    _ticket_requires_live_verification,
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
from usertest_backlog.workflows.pipeline_provenance import (
    pipeline_source_config_bindings as _pipeline_source_config_bindings,
)
from usertest_backlog.workflows.qualification_transaction import (
    load_qualification_input_bundle,
)
from usertest_backlog.workflows.shadow_validation import (
    normalize_shadow_gate_config,
    shadow_state_path,
    validate_shadow_export_state,
)

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


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _read_snapshot(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    return data, sha256(data).hexdigest()


def _sealed_live_atom_actions_snapshot(
    *,
    qualification_input_bundle_path: Path | None,
    live_atom_actions_path: Path,
) -> tuple[bytes, str] | None:
    """Bind export mutations to the exact ledger used for qualification.

    Qualification intentionally operates on an external custody copy. Export writes the
    live repository ledger, so a concurrent or intervening ledger change must invalidate
    the shadow streak rather than silently applying an old decision to new evidence.
    """

    if qualification_input_bundle_path is None:
        return None
    bundle = load_qualification_input_bundle(
        qualification_input_bundle_path,
        verify_files=True,
    )
    source_inputs_raw = bundle.get("source_inputs")
    source_inputs = source_inputs_raw if isinstance(source_inputs_raw, Mapping) else {}
    receipt_raw = source_inputs.get("atom_actions")
    receipt = receipt_raw if isinstance(receipt_raw, Mapping) else {}
    expected_sha256 = _coerce_string(receipt.get("sha256"))
    expected_size = receipt.get("size_bytes")
    if expected_sha256 is None or isinstance(expected_size, bool) or not isinstance(
        expected_size, int
    ):
        raise ValueError("qualification_atom_actions_receipt_invalid")
    if not live_atom_actions_path.is_file():
        raise ValueError(
            f"qualification_live_atom_actions_missing:{live_atom_actions_path}"
        )
    snapshot, observed_sha256 = _read_snapshot(live_atom_actions_path)
    if observed_sha256 != expected_sha256 or len(snapshot) != expected_size:
        raise ValueError("qualification_live_atom_actions_changed_since_prepare")
    return snapshot, observed_sha256


def _parse_json_snapshot(path: Path, data: bytes) -> dict[str, Any]:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Failed to parse JSON snapshot {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid JSON snapshot (expected object): {path}")
    return raw


def _parse_yaml_snapshot(path: Path, data: bytes) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Failed to parse YAML snapshot {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid YAML snapshot (expected mapping): {path}")
    return raw


def _ux_review_path_for_backlog(backlog_path: Path) -> Path:
    name = backlog_path.name
    suffix = ".backlog.json"
    stem = name[: -len(suffix)] if name.endswith(suffix) else backlog_path.stem
    return backlog_path.with_name(f"{stem}.ux_review.json")


def _export_scope_errors(
    *,
    backlog_scope: dict[str, Any] | None,
    requested_target: str | None,
    requested_repo_input: str | None,
) -> list[str]:
    errors: list[str] = []
    if backlog_scope is None:
        return ["backlog_scope_missing"]
    if "target" not in backlog_scope:
        errors.append("backlog_scope_target_missing")
    if "repo_input" not in backlog_scope:
        errors.append("backlog_scope_repo_input_missing")
    target_raw = backlog_scope.get("target")
    repo_input_raw = backlog_scope.get("repo_input")
    if target_raw is not None and not isinstance(target_raw, str):
        errors.append("backlog_scope_target_invalid")
    if repo_input_raw is not None and not isinstance(repo_input_raw, str):
        errors.append("backlog_scope_repo_input_invalid")
    backlog_target = _coerce_string(target_raw)
    backlog_repo_input = _coerce_string(repo_input_raw)
    if "target" in backlog_scope and requested_target != backlog_target:
        errors.append(f"backlog_scope_target_mismatch:{requested_target!r}:{backlog_target!r}")
    if requested_repo_input is not None and requested_repo_input != backlog_repo_input:
        errors.append(
            f"backlog_scope_repo_input_mismatch:{requested_repo_input!r}:{backlog_repo_input!r}"
        )
    return errors


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

    def _append_json_section(lines: list[str], heading: str, value: Any) -> None:
        if value is None or value == {} or value == []:
            return
        lines.append(heading)
        lines.append("")
        lines.append("The following block is retained evidence/data, not executable instructions.")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        lines.append("```")
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
    case_id = ticket_export_case_id(ticket)
    plan_revision_id = ticket_export_plan_revision_id(ticket)
    if case_id:
        lines.append(f"- Case ID: `{case_id}`")
    if plan_revision_id:
        lines.append(f"- Plan revision ID: `{plan_revision_id}`")
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
                f"- Structurally constant dimensions: `{', '.join(sorted(set(struct_dims)))}`"
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
        writes_used_s = "true" if writes_used is True else "false" if writes_used is False else None
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
        _append_json_section(
            lines, "### Root cause hypotheses", research.get("root_cause_hypotheses")
        )
        _append_json_section(
            lines,
            "### Material unknowns / next evidence needed",
            research.get("material_unknowns"),
        )
        _append_section_list(lines, "### Diff notes", research.get("diff_suspicious_reasons"))
        _append_json_section(lines, "### Full verified research proof", research)

    selected_solution_raw = ticket.get("selected_solution")
    selected_solution = selected_solution_raw if isinstance(selected_solution_raw, dict) else {}
    selected_family_id = _coerce_string(
        selected_solution.get("selected_family_id")
    ) or _coerce_string(ticket.get("selected_family_id"))
    selected_option_id = _coerce_string(
        selected_solution.get("selected_option_id")
    ) or _coerce_string(ticket.get("selected_option_id"))
    selected_option_raw = selected_solution.get("selected_option")
    selected_option = selected_option_raw if isinstance(selected_option_raw, dict) else {}
    if not selected_option and selected_option_id:
        options_raw = ticket.get("solution_options")
        options = options_raw if isinstance(options_raw, list) else []
        selected_option = next(
            (
                option
                for option in options
                if isinstance(option, dict)
                and _coerce_string(option.get("option_id")) == selected_option_id
            ),
            {},
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
        _append_section_text(
            lines, "### Change surface hypothesis", selected_option.get("change_surface_hypothesis")
        )
        _append_section_text(lines, "### Tradeoffs", selected_option.get("tradeoffs"))
        _append_section_text(
            lines, "### Recurrence prevention", selected_option.get("recurrence_prevention")
        )
        _append_section_text(
            lines, "### Test implications", selected_option.get("test_implications")
        )
        _append_section_text(lines, "### Option rationale", selected_option.get("rationale"))
        _append_json_section(
            lines, "### Selected causal coverage", selected_option.get("causal_coverage")
        )
        _append_json_section(
            lines, "### Selected scope evidence", selected_option.get("scope_evidence")
        )
        _append_json_section(
            lines,
            "### Causal coverage evaluation",
            selected_solution.get("causal_coverage_evaluation"),
        )
        _append_json_section(
            lines,
            "### Adversarial falsification review",
            selected_solution.get("falsification_review"),
        )

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
        repo_revision = _coerce_string(change_plan.get("repo_revision"))
        if repo_revision:
            lines.append(f"- Repository revision: `{repo_revision}`")
        plan_revision = _coerce_string(change_plan.get("plan_revision_id"))
        if plan_revision:
            lines.append(f"- Plan revision: `{plan_revision}`")
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
        verification_commands = _coerce_string_list(
            ticket.get("verification_commands")
        ) or _coerce_string_list(change_plan.get("verification_commands"))
        if verification_commands:
            lines.append("### Verification command contract")
            lines.append("")
            outcome_roles = ticket.get("outcome_verification_roles")
            if outcome_roles is None:
                outcome_roles = change_plan.get("outcome_verification_roles")
            lines.append(
                render_verification_contract_markdown(
                    verification_commands,
                    outcome_roles=outcome_roles,
                )
            )
            lines.append("")
        _append_json_section(lines, "### Exact change targets", change_plan.get("change_targets"))
        target_contract = change_plan.get("target_contract")
        if target_contract is not None:
            lines.append("### Machine-verifiable implementation scope contract")
            lines.append("")
            lines.append(render_plan_target_contract_markdown(target_contract))
            lines.append("")
        _append_json_section(
            lines,
            "### Original-scenario before / after proof",
            change_plan.get("before_after_reproduction"),
        )
        _append_json_section(
            lines,
            "### Compatibility and failure modes",
            change_plan.get("compatibility_and_failure_modes"),
        )
        _append_json_section(lines, "### Plan causal coverage", change_plan.get("causal_coverage"))
        _append_json_section(lines, "### Plan scope evidence", change_plan.get("scope_evidence"))
        requires_live = change_plan.get("requires_live_verification")
        if isinstance(requires_live, bool):
            lines.append("### Outcome verification requirement")
            lines.append("")
            lines.append(f"- Requires live verification: `{str(requires_live).lower()}`")
            live_rationale = _coerce_string(change_plan.get("live_verification_rationale"))
            if live_rationale:
                lines.append(f"- Rationale: {live_rationale}")
            lines.append("")
        _append_section_text(
            lines,
            "### Rollback notes",
            _coerce_string(ticket.get("rollback_notes"))
            or _coerce_string(change_plan.get("rollback_notes")),
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


def _contract_path(value: Any, *, repo_root: Path) -> Path | None:
    text = _coerce_string(value)
    if text is None:
        return None
    path = Path(text)
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def _ticket_export_decision(
    *,
    ticket: dict[str, Any],
    surface_area_high: set[str],
    scope_repo_input: str | None,
    cli_repo_input: str | None,
    repo_root: Path,
) -> dict[str, Any]:
    stage = (_coerce_string(ticket.get("stage")) or "triage").strip()
    readiness_ready, readiness_reasons = assess_ticket_readiness(ticket)
    research_raw = ticket.get("research")
    retained_ready, retained_reasons = verify_persisted_research_evidence(
        research_raw if isinstance(research_raw, dict) else {}
    )
    if not retained_ready:
        readiness_ready = False
        readiness_reasons = [
            *readiness_reasons,
            "retained_research_evidence_invalid",
            *[f"retained:{reason}" for reason in retained_reasons],
        ]
    if stage == "ready_for_ticket" and not readiness_ready:
        stage = "research_required"

    change_surface_raw = ticket.get("change_surface")
    change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
    kinds = set(_coerce_string_list(change_surface.get("kinds")))
    user_visible = bool(change_surface.get("user_visible"))
    # Implementation is a positive authoritative state, never the default for
    # an unresearched triage record. Triage and research-required records remain
    # research/annotation output even after the shadow gate opens.
    export_kind = "implementation" if stage == "ready_for_ticket" else "research"
    if user_visible and bool(kinds & surface_area_high):
        export_kind = "research"

    fingerprint = ticket_export_fingerprint(ticket)
    severity = (_coerce_string(ticket.get("severity")) or "medium").strip().lower()
    title = _coerce_string(ticket.get("title")) or "Untitled"
    issue_title = f"[Research] {title}" if export_kind == "research" else title
    ticket_for_body = dict(ticket)
    ticket_for_body["stage"] = stage
    ticket_for_body["ticket_readiness"] = {
        "ready": readiness_ready,
        "reasons": readiness_reasons,
    }
    base_body = _render_export_issue_body(
        ticket=ticket_for_body,
        fingerprint=fingerprint,
        export_kind=export_kind,
        surface_area_high=surface_area_high,
    )
    verification_contract = parse_verification_contract_markdown(base_body)
    target_contract = parse_plan_target_contract_markdown(base_body)
    labels = [f"stage:{stage}", f"severity:{severity}"]
    labels.append("type:research" if export_kind == "research" else "type:implementation")
    owner = _coerce_string(ticket.get("suggested_owner")) or _coerce_string(ticket.get("component"))
    if owner:
        labels.append(f"owner:{owner}")
    labels.extend(f"surface:{kind}" for kind in sorted(kinds))
    owner_root, owner_input, owner_resolution = _resolve_owner_repo_root(
        ticket=ticket,
        scope_repo_input=scope_repo_input,
        cli_repo_input=cli_repo_input,
        repo_root=repo_root,
    )
    return {
        "fingerprint": fingerprint,
        "case_id": ticket_export_case_id(ticket),
        "plan_revision_id": ticket_export_plan_revision_id(ticket),
        "stage": stage,
        "export_kind": export_kind,
        "title": issue_title,
        "severity": severity,
        "labels": labels,
        "body_sha256": sha256(base_body.encode("utf-8")).hexdigest(),
        "verification_contract_sha256": (
            verification_contract["contract_sha256"] if verification_contract is not None else None
        ),
        "target_contract_sha256": (
            target_contract["contract_sha256"] if target_contract is not None else None
        ),
        "owner": {
            "root": str(owner_root.resolve()),
            "repo_input": owner_input,
            "resolution": owner_resolution,
            "remote_urls": sorted(
                {
                    _normalize_remote_repo_input_for_match(url)
                    for url in _git_remote_urls(owner_root)
                }
            ),
        },
        "ticket_readiness": ticket_for_body["ticket_readiness"],
    }


def _build_export_projection(
    *,
    backlog: dict[str, Any],
    surface_area_high: set[str],
    cli_repo_input: str | None,
    repo_root: Path,
) -> dict[str, Any]:
    scope_raw = backlog.get("scope")
    scope = scope_raw if isinstance(scope_raw, dict) else {}
    scope_repo_input = _coerce_string(scope.get("repo_input"))
    tickets_raw = backlog.get("tickets")
    tickets = (
        [item for item in tickets_raw if isinstance(item, dict)]
        if isinstance(tickets_raw, list)
        else []
    )
    decisions = [
        _ticket_export_decision(
            ticket=ticket,
            surface_area_high=surface_area_high,
            scope_repo_input=scope_repo_input,
            cli_repo_input=cli_repo_input,
            repo_root=repo_root,
        )
        for ticket in tickets
    ]
    fingerprints = [str(item["fingerprint"]) for item in decisions]
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError("Backlog export projection contains duplicate fingerprints")
    decisions.sort(key=lambda item: str(item["fingerprint"]))
    projection = {
        "schema_version": 1,
        "scope": {
            "target": _coerce_string(scope.get("target")),
            "repo_input": scope_repo_input,
        },
        "tickets": decisions,
    }
    projection["sha256"] = _canonical_sha256(projection)
    return projection


def _shadow_projection_binding_errors(
    *,
    backlog: dict[str, Any],
    gate_state: dict[str, Any] | None,
    projection_sha256: str,
    exact_snapshot_hashes: dict[str, str],
) -> list[str]:
    cycles_raw = gate_state.get("cycles") if isinstance(gate_state, dict) else None
    latest_cycle = (
        cycles_raw[-1]
        if isinstance(cycles_raw, list) and cycles_raw and isinstance(cycles_raw[-1], dict)
        else {}
    )
    artifacts_raw = backlog.get("artifacts")
    backlog_artifacts = artifacts_raw if isinstance(artifacts_raw, dict) else {}
    contract_raw = backlog_artifacts.get("export_contract")
    contract = contract_raw if isinstance(contract_raw, dict) else {}
    errors: list[str] = []
    if _coerce_string(latest_cycle.get("export_projection_sha256")) != projection_sha256:
        errors.append("shadow_export_projection_changed")
    if _coerce_string(contract.get("projection_sha256")) != projection_sha256:
        errors.append("backlog_export_projection_changed")

    receipts_raw = latest_cycle.get("artifact_receipts")
    receipts = receipts_raw if isinstance(receipts_raw, list) else []
    receipts_by_name = {
        str(item.get("name")): item
        for item in receipts
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for name, snapshot_hash in exact_snapshot_hashes.items():
        receipt = receipts_by_name.get(name)
        if not isinstance(receipt, dict) or receipt.get("sha256") != snapshot_hash:
            errors.append(f"export_input_snapshot_unbound:{name}")
    return errors


def _shadow_state_path_for_backlog(
    backlog: Mapping[str, Any],
    backlog_path: Path,
) -> Path:
    """Resolve the state ledger bound by the shadow transaction.

    Legacy backlogs keep the colocated default. Sealed qualification cycles use
    different cycle-local backlog paths but intentionally share one explicit
    state ledger so consecutive independent cycles can form a release streak.
    """

    artifacts = backlog.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    export_contract = artifacts.get("export_contract")
    export_contract = export_contract if isinstance(export_contract, Mapping) else {}
    configured = _coerce_string(export_contract.get("shadow_state_path"))
    return Path(configured).expanduser().resolve() if configured else shadow_state_path(backlog_path)


def _export_artifact_paths(
    *,
    backlog: dict[str, Any],
    backlog_path: Path,
    repo_root: Path,
    policy_config_path: Path,
    export_gate_config_path: Path,
    cli_repo_input: str | None,
) -> dict[str, Path | None]:
    artifacts_raw = backlog.get("artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, dict) else {}
    pipeline_raw = artifacts.get("six_stage_pipeline")
    pipeline = pipeline_raw if isinstance(pipeline_raw, dict) else {}
    qualification_raw = artifacts.get("shadow_qualification")
    qualification = qualification_raw if isinstance(qualification_raw, dict) else {}
    operational_raw = artifacts.get("operational_shadow")
    operational = operational_raw if isinstance(operational_raw, dict) else {}
    bindings: dict[str, Path | None] = {
        "atoms": _contract_path(artifacts.get("atoms_jsonl"), repo_root=repo_root),
        "problem_records": _contract_path(
            pipeline.get("problem_records_json"), repo_root=repo_root
        ),
        "problem_mining_evidence": _contract_path(
            pipeline.get("problem_mining_evidence_json"), repo_root=repo_root
        ),
        "prioritized_problems": _contract_path(
            pipeline.get("prioritized_problems_json"), repo_root=repo_root
        ),
        "research": _contract_path(pipeline.get("research_json"), repo_root=repo_root),
        "solution_options": _contract_path(
            pipeline.get("solution_options_json"), repo_root=repo_root
        ),
        "solution_selection": _contract_path(
            pipeline.get("solution_selection_json"), repo_root=repo_root
        ),
        "change_plans": _contract_path(pipeline.get("change_plans_json"), repo_root=repo_root),
        "case_registry": _contract_path(
            pipeline.get("case_registry_json") or artifacts.get("case_registry_json"),
            repo_root=repo_root,
        ),
        "config.policy": policy_config_path.resolve(),
        "config.research": (repo_root / "configs" / "backlog_research.yaml").resolve(),
        "config.export_gate": export_gate_config_path.resolve(),
        "ux.review_json": _ux_review_path_for_backlog(backlog_path).resolve(),
        "qualification.corpus_manifest": _contract_path(
            qualification.get("qualification_corpus_manifest_path"),
            repo_root=repo_root,
        ),
        "qualification.input_bundle": _contract_path(
            qualification.get("qualification_input_bundle_path"),
            repo_root=repo_root,
        ),
        "qualification.raw_first_pass_report": _contract_path(
            qualification.get("raw_first_pass_report_path"),
            repo_root=repo_root,
        ),
        "qualification.repaired_child_contract": _contract_path(
            qualification.get("pending_repaired_run_receipt_path"),
            repo_root=repo_root,
        ),
        "qualification.repair_bundle_manifest": _contract_path(
            artifacts.get("qualification_repair_bundle_manifest"),
            repo_root=repo_root,
        ),
        "qualification.output_adjudication": _contract_path(
            qualification.get("qualification_output_adjudication_path"),
            repo_root=repo_root,
        ),
        "qualification.no_actionable_receipt": _contract_path(
            qualification.get("no_actionable_evidence_receipt_path"),
            repo_root=repo_root,
        ),
        "qualification.pending_run_receipt": _contract_path(
            qualification.get("pending_run_receipt_path"),
            repo_root=repo_root,
        ),
        "operational.pending_run_receipt": _contract_path(
            operational.get("pending_run_receipt_path"),
            repo_root=repo_root,
        ),
    }
    correction_history_raw = qualification.get("correction_history")
    correction_history = (
        correction_history_raw if isinstance(correction_history_raw, list) else []
    )
    for index, item in enumerate(correction_history):
        if not isinstance(item, Mapping):
            continue
        bindings[f"qualification.correction_history:{index}:failed_report"] = (
            _contract_path(item.get("failed_report_path"), repo_root=repo_root)
        )
        bindings[f"qualification.correction_history:{index}:consumption"] = (
            _contract_path(
                item.get("correction_consumption_path"),
                repo_root=repo_root,
            )
        )
        bindings[f"qualification.correction_history:{index}:failure"] = (
            _contract_path(
                item.get("correction_failure_path"),
                repo_root=repo_root,
            )
        )

    input_raw = backlog.get("input")
    input_meta = input_raw if isinstance(input_raw, dict) else {}
    pipeline_manifest_path = _contract_path(
        input_meta.get("pipeline_manifest_path"), repo_root=repo_root
    )
    prompts_dir = _contract_path(artifacts.get("prompts_dir"), repo_root=repo_root)
    if pipeline_manifest_path is None and prompts_dir is not None:
        pipeline_manifest_path = prompts_dir / "pipeline_manifest.json"
    bindings["pipeline.manifest"] = pipeline_manifest_path
    if prompts_dir is not None and prompts_dir.is_dir():
        for path in sorted(prompts_dir.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_file():
                rel = path.relative_to(prompts_dir).as_posix()
                bindings[f"pipeline.prompt:{rel}"] = path.resolve()

    if pipeline_manifest_path is not None and pipeline_manifest_path.is_file():
        try:
            manifest = json.loads(pipeline_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        if isinstance(manifest, dict):
            for key, artifact_key in (
                ("taxonomy_file", "pipeline.taxonomy"),
                ("relation_review_config", "pipeline.relation_review_config"),
                ("stage_guidance_manifest", "pipeline.stage_guidance_manifest"),
            ):
                bindings[artifact_key] = _contract_path(manifest.get(key), repo_root=repo_root)
            guidance_manifest = bindings.get("pipeline.stage_guidance_manifest")
            if guidance_manifest is not None and guidance_manifest.parent.is_dir():
                for path in sorted(
                    guidance_manifest.parent.rglob("*"), key=lambda item: item.as_posix()
                ):
                    if path.is_file():
                        rel = path.relative_to(guidance_manifest.parent).as_posix()
                        bindings[f"pipeline.guidance:{rel}"] = path.resolve()

    imported_source_root = Path(__file__).resolve().parents[5]
    source_root = (
        repo_root.resolve()
        if (repo_root / "apps" / "usertest_backlog" / "src").is_dir()
        else imported_source_root
    )
    bindings.update(
        _pipeline_source_config_bindings(
            source_root=source_root,
            config_root=repo_root / "configs",
        )
    )

    projection = _build_export_projection(
        backlog=backlog,
        surface_area_high=set(),
        cli_repo_input=cli_repo_input,
        repo_root=repo_root,
    )
    for decision in projection["tickets"]:
        owner_raw = decision.get("owner")
        owner = owner_raw if isinstance(owner_raw, dict) else {}
        owner_root = _contract_path(owner.get("root"), repo_root=repo_root)
        if owner_root is not None:
            owner_key = sha256(str(owner_root).encode("utf-8")).hexdigest()[:16]
            bindings[f"owner.{owner_key}.git_config"] = owner_root / ".git" / "config"
    return bindings


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


def _validate_export_change_plan(
    *,
    tickets: list[dict[str, Any]],
    projection_by_fingerprint: dict[str, dict[str, Any]],
    ux_recommendations_by_fingerprint: dict[str, list[dict[str, Any]]],
    ux_review_doc: dict[str, Any] | None,
    ux_review_json_path: Path,
    ux_review_md_path: Path,
    actions: dict[str, dict[str, Any]],
    stages: list[str],
    min_severity: str,
    include_actioned: bool,
    include_discarded: bool,
    skip_plan_folder_dedupe: bool,
    surface_area_high: set[str],
    backlog_scope_repo_input: str | None,
    repo_input: str | None,
    repo_root: Path,
    trusted_runs_roots: list[Path],
    case_registry: dict[str, Any],
) -> tuple[
    dict[Path, dict[str, dict[str, Any]]],
    dict[str, dict[str, Any]],
]:
    """Validate every export decision and filesystem read before mutation begins."""

    plan_indexes: dict[Path, dict[str, dict[str, Any]]] = {}
    validated_action_outcomes: dict[str, dict[str, Any]] = {}
    for ticket in tickets:
        fingerprint = ticket_export_fingerprint(ticket)
        decision = projection_by_fingerprint.get(fingerprint)
        if decision is None:
            raise ValueError(
                f"Ticket is not present in the shadowed export projection: {fingerprint}"
            )
        stage = str(decision["stage"])
        readiness_raw = decision.get("ticket_readiness")
        readiness = readiness_raw if isinstance(readiness_raw, dict) else {}
        ticket_for_body = dict(ticket)
        ticket_for_body["ticket_readiness"] = {
            "ready": readiness.get("ready") is True,
            "reasons": [
                reason for reason in readiness.get("reasons", []) if isinstance(reason, str)
            ],
        }
        ticket_for_body["stage"] = stage

        owner_repo_root, owner_repo_input, owner_repo_resolution = _resolve_owner_repo_root(
            ticket=ticket,
            scope_repo_input=backlog_scope_repo_input,
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )
        actual_owner = {
            "root": str(owner_repo_root.resolve()),
            "repo_input": owner_repo_input,
            "resolution": owner_repo_resolution,
            "remote_urls": sorted(
                {
                    _normalize_remote_repo_input_for_match(url)
                    for url in _git_remote_urls(owner_repo_root)
                }
            ),
        }
        if actual_owner != decision.get("owner"):
            raise ValueError(
                f"Ticket owner routing changed outside the shadowed projection: {fingerprint}"
            )

        owner_key = owner_repo_root.resolve()
        if not include_actioned and not skip_plan_folder_dedupe:
            if owner_key not in plan_indexes:
                plan_indexes[owner_key] = _scan_plan_ticket_index(
                    owner_root=owner_key,
                    include_discarded=False,
                )

        ux_recs = ux_recommendations_by_fingerprint.get(fingerprint) or []
        ux_section: str | None = None
        if ux_recs and ux_review_doc is not None:
            ux_section = _render_ux_review_section_for_ticket(
                ux_review_doc=ux_review_doc,
                ux_review_json_path=ux_review_json_path,
                ux_review_md_path=ux_review_md_path,
                fingerprint=fingerprint,
                recs=ux_recs,
            )

        if stage not in stages:
            continue
        severity = (_coerce_string(ticket.get("severity")) or "medium").strip().lower()
        if _severity_rank(severity) < _severity_rank(min_severity):
            continue

        action_entry = actions.get(fingerprint)
        if action_entry is not None:
            action_status = _coerce_string(action_entry.get("status"))
            if action_status == "discarded" and not include_discarded:
                continue
            if action_status != "discarded" and not include_actioned:
                case_id = ticket_export_case_id(ticket)
                outcome_raw = action_entry.get("outcome")
                if isinstance(outcome_raw, dict):
                    outcome = _validate_outcome_record(outcome_raw)
                    validated_action_outcomes[fingerprint] = outcome
                    if _outcome_suppresses_new_case_discovery(outcome):
                        suppresses, provenance = _verified_outcome_suppresses_export(
                            outcome,
                            trusted_runs_roots=trusted_runs_roots,
                            owner_roots=[owner_key],
                            case_registry=case_registry,
                        )
                        if not suppresses:
                            outcome["_export_suppression_provenance"] = provenance
                        else:
                            outcome["_export_suppression_provenance_verified"] = True
                            existing = plan_indexes.get(owner_key, {}).get(fingerprint)
                            known_states = {
                                item.strip().lower()
                                for field in (
                                    "outcome_states",
                                    "relationship_outcome_states",
                                )
                                for item in (
                                    existing.get(field, []) if isinstance(existing, dict) else []
                                )
                                if isinstance(item, str) and item.strip()
                            }
                            known_states.add(str(outcome["state"]).strip().lower())
                            terminal_states = {"resolved", "duplicate", "superseded"}
                            if not known_states.issubset(terminal_states):
                                raise ValueError(
                                    "Conflicting terminal and non-terminal case outcomes; "
                                    "refusing action suppression: "
                                    f"fingerprint={fingerprint!r} "
                                    f"states={sorted(known_states)!r}"
                                )
                            continue
                elif outcome_raw is not None:
                    raise ValueError(
                        f"Action outcome must be an object for fingerprint {fingerprint!r}"
                    )
                elif case_id is None or action_status == "deferred":
                    continue

        case_id = ticket_export_case_id(ticket)
        if (
            not include_actioned
            and not skip_plan_folder_dedupe
            and case_id is not None
            and case_id in _case_ids_awaiting_outcome_verification(plan_indexes.get(owner_key, {}))
        ):
            continue

        export_kind = str(decision["export_kind"])
        base_body = _render_export_issue_body(
            ticket=ticket_for_body,
            fingerprint=fingerprint,
            export_kind=export_kind,
            surface_area_high=surface_area_high,
        )
        if sha256(base_body.encode("utf-8")).hexdigest() != decision.get("body_sha256"):
            raise ValueError(f"Ticket body changed outside the shadowed projection: {fingerprint}")
        body = (
            ux_section.strip() + "\n\n" + base_body.strip() + "\n"
            if ux_section is not None
            else base_body
        )
        parse_verification_contract_markdown(body)
        canonical_ticket_body_sha256(body)

        existing = plan_indexes.get(owner_key, {}).get(fingerprint)
        if not isinstance(existing, dict):
            continue
        integrity_records = [
            item for item in existing.get("integrity_unknown_records", []) if isinstance(item, dict)
        ]
        if integrity_records:
            continue
        desired_status = _normalize_atom_status(_coerce_string(existing.get("status")))
        if (
            desired_status == "actioned"
            and ticket_export_case_id(ticket) is not None
            and not {
                item.strip().lower()
                for item in existing.get("outcome_states", [])
                if isinstance(item, str) and item.strip()
            }
        ):
            raise ValueError(
                "Case-aware actioned ticket has no durable outcome; refusing to "
                f"treat folder state as resolution: fingerprint={fingerprint!r}"
            )
    return plan_indexes, validated_action_outcomes


_OUTCOME_STATES_AWAITING_PROOF = frozenset(
    {
        "planned",
        "implemented",
        "tests_verified",
        "original_scenario_verified",
        "live_verified",
    }
)


def _case_ids_awaiting_outcome_verification(
    plan_index: dict[str, dict[str, Any]],
) -> set[str]:
    """Return cases that should wait without blocking unrelated backlog work.

    A merged plan that is still moving through its promised outcome proof must not be
    exported again under wording drift.  A predicate failure is different: it is evidence
    that the selected solution did not solve the problem, so that case is allowed back into
    research/planning while the failed outcome remains durable provenance.
    """

    awaiting: set[str] = set()
    for metadata in plan_index.values():
        if not isinstance(metadata, dict):
            continue
        records = metadata.get("active_outcome_records")
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            case_id = _coerce_string(record.get("case_id"))
            state = (_coerce_string(record.get("state")) or "").casefold()
            if case_id is None:
                continue
            if state in _OUTCOME_STATES_AWAITING_PROOF:
                awaiting.add(case_id)
                continue
            if state != "unverified":
                continue
            risks = "\n".join(_coerce_string_list(record.get("remaining_risks"))).casefold()
            if "did not satisfy its causal predicates" not in risks:
                awaiting.add(case_id)
    return awaiting


def _verified_outcome_suppresses_export(
    outcome: dict[str, Any],
    *,
    trusted_runs_roots: list[Path],
    owner_roots: list[Path],
    case_registry: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Require retained provenance before a terminal outcome can hide a case."""

    if not _outcome_suppresses_new_case_discovery(outcome):
        return False, {
            "verified": True,
            "provenance_status": "not_required_non_suppressing",
            "errors": [],
        }
    provenance = verify_outcome_record_provenance(
        outcome,
        trusted_runs_roots=trusted_runs_roots,
        owner_roots=owner_roots,
        case_registry=case_registry,
    )
    return provenance.get("verified") is True, provenance


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
        actions = _load_backlog_actions_yaml(actions_path) if actions_path.exists() else {}
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        backlog_snapshot, backlog_snapshot_sha256 = _read_snapshot(backlog_path)
        backlog_doc = _parse_json_snapshot(backlog_path, backlog_snapshot)
    except (OSError, ValueError) as e:
        print(f"Failed to parse backlog JSON: {backlog_path}: {e}", file=sys.stderr)
        return 2
    backlog_content_projection = dict(backlog_doc)
    backlog_content_projection.pop("generated_at_utc", None)
    backlog_snapshot_content_sha256 = _canonical_sha256(backlog_content_projection)
    if (
        backlog_doc.get("pipeline_kind") == "legacy_one_pass_analysis"
        or backlog_doc.get("analysis_only") is True
        or backlog_doc.get("export_eligible") is False
    ):
        print(
            "Legacy one-pass backlog output is analysis-only and cannot be exported; "
            "materialize the authoritative six-stage pipeline instead.",
            file=sys.stderr,
        )
        return 2
    backlog_scope_raw = backlog_doc.get("scope")
    backlog_scope = backlog_scope_raw if isinstance(backlog_scope_raw, dict) else {}
    backlog_scope_repo_input = _coerce_string(backlog_scope.get("repo_input"))
    scope_errors = _export_scope_errors(
        backlog_scope=(backlog_scope if isinstance(backlog_scope_raw, dict) else None),
        requested_target=target_slug,
        requested_repo_input=repo_input,
    )
    if scope_errors:
        print(
            "Export scope does not match the shadowed backlog scope: " + ", ".join(scope_errors),
            file=sys.stderr,
        )
        return 2

    shadow_gate_config_path = repo_root / "configs" / "backlog_export_gate.yaml"
    if not shadow_gate_config_path.is_file():
        print(
            f"Missing backlog export gate config: {shadow_gate_config_path}",
            file=sys.stderr,
        )
        return 2
    try:
        shadow_gate_snapshot, _shadow_gate_snapshot_sha256 = _read_snapshot(shadow_gate_config_path)
        shadow_gate_root = _parse_yaml_snapshot(shadow_gate_config_path, shadow_gate_snapshot)
        shadow_gate_config = normalize_shadow_gate_config(
            shadow_gate_root.get("backlog_export_gate", {})
        )
    except (OSError, ValueError) as exc:
        print(f"Invalid backlog export gate: {shadow_gate_config_path}: {exc}", file=sys.stderr)
        return 2
    if shadow_gate_config["enabled"] is not True:
        print(
            f"Backlog export gate is disabled: {shadow_gate_config_path}",
            file=sys.stderr,
        )
        return 2
    required_consecutive_cycles = shadow_gate_config["required_consecutive_shadow_cycles"]
    require_exact_export_projection = shadow_gate_config["require_exact_export_projection"]
    bound_shadow_state_path = _shadow_state_path_for_backlog(backlog_doc, backlog_path)
    shadow_gate_meta: dict[str, Any] = {
        "enabled": True,
        "config_path": str(shadow_gate_config_path),
        "state_path": str(bound_shadow_state_path),
        "required_consecutive_shadow_cycles": required_consecutive_cycles,
        "require_exact_export_projection": require_exact_export_projection,
    }
    unsafe_overrides = [
        name
        for name, enabled in (
            ("--include-actioned", bool(args.include_actioned)),
            ("--include-discarded", bool(getattr(args, "include_discarded", False))),
            (
                "--skip-plan-folder-dedupe",
                bool(getattr(args, "skip_plan_folder_dedupe", False)),
            ),
        )
        if enabled
    ]
    if unsafe_overrides:
        print(
            "Automated export forbids unshadowed additive overrides: "
            + ", ".join(unsafe_overrides),
            file=sys.stderr,
        )
        return 2

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

    ux_review_json_path = _ux_review_path_for_backlog(backlog_path)
    ux_review_md_path = ux_review_json_path.with_suffix(".md")
    ux_review_doc: dict[str, Any] | None = None
    if ux_review_json_path.exists():
        try:
            ux_review_snapshot, _ux_review_snapshot_sha256 = _read_snapshot(ux_review_json_path)
            ux_review_doc = _parse_json_snapshot(ux_review_json_path, ux_review_snapshot)
        except (OSError, ValueError) as exc:
            print(f"Invalid UX review input: {ux_review_json_path}: {exc}", file=sys.stderr)
            return 2
    ux_recommendations_by_fingerprint = (
        _index_ux_recommendations(ux_review_doc) if ux_review_doc is not None else {}
    )
    backlog_input_raw = backlog_doc.get("input")
    backlog_input = backlog_input_raw if isinstance(backlog_input_raw, dict) else {}
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
        policy_snapshot, _policy_snapshot_sha256 = _read_snapshot(policy_config_path)
        policy_root = _parse_yaml_snapshot(policy_config_path, policy_snapshot)
        policy_raw = policy_root.get("backlog_policy", {})
        if not isinstance(policy_raw, dict):
            raise ValueError("backlog_policy config must be a mapping")
        policy_cfg = BacklogPolicyConfig.from_dict(policy_raw)
    except (OSError, TypeError, ValueError) as e:
        print(f"Invalid backlog policy config: {policy_config_path}: {e}", file=sys.stderr)
        return 2

    surface_area_high = set(policy_cfg.surface_area_high)

    try:
        export_artifact_paths = _export_artifact_paths(
            backlog=backlog_doc,
            backlog_path=backlog_path,
            repo_root=repo_root,
            policy_config_path=policy_config_path,
            export_gate_config_path=shadow_gate_config_path,
            cli_repo_input=repo_input,
        )
        current_projection = _build_export_projection(
            backlog=backlog_doc,
            surface_area_high=surface_area_high,
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"Failed to build export contract: {exc}", file=sys.stderr)
        return 2

    gate_ready, gate_reasons, gate_state = validate_shadow_export_state(
        state_path=bound_shadow_state_path,
        backlog_path=backlog_path,
        backlog_snapshot_sha256=backlog_snapshot_sha256,
        backlog_snapshot_content_sha256=backlog_snapshot_content_sha256,
        artifact_paths=export_artifact_paths,
        required_consecutive_cycles=required_consecutive_cycles,
        require_exact_export_projection=require_exact_export_projection,
    )
    shadow_gate_meta.update(
        {
            "ready": gate_ready,
            "reasons": gate_reasons,
            "consecutive_stable_passes": (
                gate_state.get("consecutive_stable_passes") if isinstance(gate_state, dict) else 0
            ),
        }
    )
    if not gate_ready:
        print(
            "Automated ticket export is locked until "
            f"{required_consecutive_cycles} consecutive full shadow cycles pass"
            + (" with an exact export projection: " if require_exact_export_projection else ": ")
            + ", ".join(gate_reasons),
            file=sys.stderr,
        )
        return 2

    current_projection_sha256 = str(current_projection["sha256"])
    exact_snapshots = {
        "config.export_gate": _shadow_gate_snapshot_sha256,
        "config.policy": _policy_snapshot_sha256,
    }
    if ux_review_doc is not None:
        exact_snapshots["ux.review_json"] = _ux_review_snapshot_sha256
    projection_binding_errors = _shadow_projection_binding_errors(
        backlog=backlog_doc,
        gate_state=gate_state,
        projection_sha256=current_projection_sha256,
        exact_snapshot_hashes=exact_snapshots,
    )
    if projection_binding_errors:
        print(
            "Export projection or exact decision inputs changed since shadow validation: "
            + ", ".join(projection_binding_errors),
            file=sys.stderr,
        )
        return 2

    qualification_bundle_path = export_artifact_paths.get(
        "qualification.input_bundle"
    )
    if isinstance(qualification_bundle_path, Path):
        scope_owner_candidate = (
            Path(backlog_scope_repo_input).expanduser()
            if backlog_scope_repo_input is not None
            else repo_root
        )
        scope_owner_root = (
            scope_owner_candidate.resolve()
            if scope_owner_candidate.is_dir()
            else repo_root.resolve()
        )
        canonical_actions_path = (
            scope_owner_root / "configs" / "backlog_actions.yaml"
        ).resolve()
        canonical_atom_actions_path = (
            scope_owner_root / "configs" / "backlog_atom_actions.yaml"
        ).resolve()
        if actions_path.resolve() != canonical_actions_path:
            print(
                "Sealed export requires the canonical live ticket-action ledger: "
                f"{canonical_actions_path}",
                file=sys.stderr,
            )
            return 2
        if atom_actions_path.resolve() != canonical_atom_actions_path:
            print(
                "Sealed export requires the canonical live atom-action ledger: "
                f"{canonical_atom_actions_path}",
                file=sys.stderr,
            )
            return 2
    try:
        sealed_atom_actions_snapshot = _sealed_live_atom_actions_snapshot(
            qualification_input_bundle_path=(
                qualification_bundle_path
                if isinstance(qualification_bundle_path, Path)
                else None
            ),
            live_atom_actions_path=atom_actions_path,
        )
    except (OSError, ValueError) as exc:
        print(
            "Live atom ledger no longer matches the sealed qualification corpus: "
            f"{exc}",
            file=sys.stderr,
        )
        return 2

    projection_by_fingerprint = {
        str(item["fingerprint"]): item
        for item in current_projection["tickets"]
        if isinstance(item, dict)
    }
    case_registry: dict[str, Any] = {}
    case_registry_path = export_artifact_paths.get("case_registry")
    if isinstance(case_registry_path, Path) and case_registry_path.is_file():
        try:
            case_registry_bytes, _case_registry_sha256 = _read_snapshot(case_registry_path)
            case_registry = _parse_json_snapshot(
                case_registry_path,
                case_registry_bytes,
            )
        except (OSError, ValueError) as exc:
            print(f"Invalid case registry for outcome provenance: {exc}", file=sys.stderr)
            return 2

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
    skipped_legacy_actioned = 0
    skipped_discarded = 0
    skipped_existing_plan = 0
    skipped_awaiting_outcome = 0
    skipped_integrity_unknown = 0
    skipped_stage = 0
    skipped_severity = 0
    integrity_unknown_skips: list[dict[str, Any]] = []
    idea_files_written: list[str] = []
    plan_index_cache: dict[Path, dict[str, dict[str, Any]]] = {}
    skip_plan_folder_dedupe = bool(getattr(args, "skip_plan_folder_dedupe", False))
    ux_plan_tickets_updated = 0
    ux_idea_files_updated = 0
    ux_tickets_deferred = 0
    swept_actioned_queue_dupes_archived = 0
    swept_actioned_bucket_dupes_archived = 0
    swept_scope_stale_generated_archived = 0
    swept_scope_stale_generated_unresolved = 0
    generated_queue_files_refreshed = 0
    actions_mutated = False
    keep_fingerprints_by_owner_root: dict[Path, set[str]] = {}
    keep_fingerprint_by_case_by_owner_root: dict[Path, dict[str, str | None]] = {}
    owner_roots_for_reconciliation: set[Path] = set()
    pre_export_atom_sync_meta: dict[str, Any] | None = None

    for ticket in tickets:
        owner_repo_root, _owner_repo_input, _owner_repo_resolution = _resolve_owner_repo_root(
            ticket=ticket,
            scope_repo_input=backlog_scope_repo_input,
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )
        owner_roots_for_reconciliation.add(owner_repo_root.resolve())

    def _register_current_replacement(
        *, ticket: dict[str, Any], fingerprint: str, owner_repo_root: Path
    ) -> None:
        owner_key = owner_repo_root.resolve()
        keep_fingerprints_by_owner_root.setdefault(owner_key, set()).add(fingerprint)
        case_id = ticket_export_case_id(ticket)
        if case_id is not None:
            by_case = keep_fingerprint_by_case_by_owner_root.setdefault(owner_key, {})
            if case_id not in by_case:
                by_case[case_id] = fingerprint
            elif by_case[case_id] != fingerprint:
                # Explicitly split/coordinated plans make automatic one-to-one
                # supersession ambiguous. Preserve stale records for relation
                # review rather than guessing which revision replaced them.
                by_case[case_id] = None

    try:
        plan_index_cache, validated_action_outcomes = _validate_export_change_plan(
            tickets=tickets,
            projection_by_fingerprint=projection_by_fingerprint,
            ux_recommendations_by_fingerprint=ux_recommendations_by_fingerprint,
            ux_review_doc=ux_review_doc,
            ux_review_json_path=ux_review_json_path,
            ux_review_md_path=ux_review_md_path,
            actions=actions,
            stages=stages,
            min_severity=min_severity,
            include_actioned=include_actioned,
            include_discarded=include_discarded,
            skip_plan_folder_dedupe=skip_plan_folder_dedupe,
            surface_area_high=surface_area_high,
            backlog_scope_repo_input=backlog_scope_repo_input,
            repo_input=repo_input,
            repo_root=repo_root,
            trusted_runs_roots=[runs_dir.resolve()],
            case_registry=case_registry,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"Export change-plan validation failed: {exc}", file=sys.stderr)
        return 2

    try:
        final_sealed_atom_actions_snapshot = _sealed_live_atom_actions_snapshot(
            qualification_input_bundle_path=(
                qualification_bundle_path
                if isinstance(qualification_bundle_path, Path)
                else None
            ),
            live_atom_actions_path=atom_actions_path,
        )
        if final_sealed_atom_actions_snapshot != sealed_atom_actions_snapshot:
            raise ValueError("qualification_live_atom_actions_changed_during_export")
        atom_actions = _load_atom_actions_yaml(atom_actions_path)
    except (OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    pre_export_atom_sync_meta = _reconcile_atom_actions_from_plan_folders(
        atom_actions=atom_actions,
        owner_roots=sorted(owner_roots_for_reconciliation, key=lambda p: str(p)),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    try:
        current_backlog_snapshot, current_backlog_sha256 = _read_snapshot(backlog_path)
        final_projection = _build_export_projection(
            backlog=backlog_doc,
            surface_area_high=surface_area_high,
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"Final export preflight failed: {exc}", file=sys.stderr)
        return 2
    if (
        current_backlog_sha256 != backlog_snapshot_sha256
        or current_backlog_snapshot != backlog_snapshot
    ):
        print(
            "Backlog changed after export validation; refusing all mutations.",
            file=sys.stderr,
        )
        return 2
    if final_projection.get("sha256") != current_projection.get("sha256"):
        print(
            "Export projection changed during preflight; refusing all mutations.",
            file=sys.stderr,
        )
        return 2
    final_gate_ready, final_gate_reasons, _ = validate_shadow_export_state(
        state_path=bound_shadow_state_path,
        backlog_path=backlog_path,
        backlog_snapshot_sha256=backlog_snapshot_sha256,
        backlog_snapshot_content_sha256=backlog_snapshot_content_sha256,
        artifact_paths=export_artifact_paths,
        required_consecutive_cycles=required_consecutive_cycles,
        require_exact_export_projection=require_exact_export_projection,
    )
    if not final_gate_ready:
        print(
            "Export inputs changed during preflight; refusing all mutations: "
            + ", ".join(final_gate_reasons),
            file=sys.stderr,
        )
        return 2
    for ticket in tickets:
        fingerprint = ticket_export_fingerprint(ticket)
        decision = projection_by_fingerprint[fingerprint]
        stage = str(decision["stage"])
        ux_recs = ux_recommendations_by_fingerprint.get(fingerprint) or []
        readiness_raw = decision.get("ticket_readiness")
        readiness = readiness_raw if isinstance(readiness_raw, dict) else {}
        readiness_ready = readiness.get("ready") is True
        readiness_reasons = [
            reason for reason in readiness.get("reasons", []) if isinstance(reason, str)
        ]
        ticket["ticket_readiness"] = {
            "ready": readiness_ready,
            "reasons": readiness_reasons,
        }

        owner_repo_root, _owner_repo_input, _owner_repo_resolution = _resolve_owner_repo_root(
            ticket=ticket,
            scope_repo_input=backlog_scope_repo_input,
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )

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
        if ux_recs and ux_review_doc is not None:
            ux_section = _render_ux_review_section_for_ticket(
                ux_review_doc=ux_review_doc,
                ux_review_json_path=ux_review_json_path,
                ux_review_md_path=ux_review_md_path,
                fingerprint=fingerprint,
                recs=ux_recs,
            )
            if stage == "research_required" and ux_approach == "defer":
                defer_to_bucket = "0.1 - deferred"
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
                case_id = ticket_export_case_id(ticket)
                outcome_raw = action_entry.get("outcome")
                if isinstance(outcome_raw, dict):
                    outcome = validated_action_outcomes[fingerprint]
                    if outcome.get(
                        "_export_suppression_provenance_verified"
                    ) is True and _outcome_suppresses_new_case_discovery(outcome):
                        skipped_actioned += 1
                        continue
                elif case_id is None or action_status == "deferred":
                    # Preserve explicit legacy/deferred behavior, but do not let
                    # a case-aware ticket inherit "resolved" from folder/action
                    # state alone.
                    skipped_actioned += 1
                    skipped_legacy_actioned += 1
                    continue

        export_kind = str(decision["export_kind"])

        issue_title = str(decision["title"])

        ticket_for_body = dict(ticket)
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

        labels = [str(label) for label in decision.get("labels", [])]
        if ux_approach:
            labels.append(f"ux:{ux_approach}")

        owner_repo_root, owner_repo_input, owner_repo_resolution = _resolve_owner_repo_root(
            ticket=ticket,
            scope_repo_input=backlog_scope_repo_input,
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )

        if not include_actioned and not skip_plan_folder_dedupe:
            owner_key = owner_repo_root.resolve()
            if owner_key not in plan_index_cache:
                plan_index_cache[owner_key] = _scan_plan_ticket_index(
                    owner_root=owner_key,
                    include_discarded=False,
                )
            case_id = ticket_export_case_id(ticket)
            if case_id is not None and case_id in _case_ids_awaiting_outcome_verification(
                plan_index_cache[owner_key]
            ):
                prior_fingerprints = {
                    prior_fingerprint
                    for prior_fingerprint, metadata in plan_index_cache[owner_key].items()
                    if isinstance(metadata, dict)
                    and any(
                        isinstance(record, dict)
                        and _coerce_string(record.get("case_id")) == case_id
                        for record in (
                            metadata.get("active_outcome_records")
                            if isinstance(metadata.get("active_outcome_records"), list)
                            else []
                        )
                    )
                }
                keep_fingerprints_by_owner_root.setdefault(owner_key, set()).update(
                    prior_fingerprints
                )
                by_case = keep_fingerprint_by_case_by_owner_root.setdefault(owner_key, {})
                by_case[case_id] = (
                    next(iter(prior_fingerprints)) if len(prior_fingerprints) == 1 else None
                )
                skipped_awaiting_outcome += 1
                continue
            existing = plan_index_cache[owner_key].get(fingerprint)
            if isinstance(existing, dict):
                integrity_records = [
                    item
                    for item in existing.get("integrity_unknown_records", [])
                    if isinstance(item, dict)
                ]
                if integrity_records:
                    reasons = sorted(
                        {
                            str(item.get("reason")).strip()
                            for item in integrity_records
                            if isinstance(item.get("reason"), str)
                            and str(item.get("reason")).strip()
                        }
                    )
                    paths = sorted(
                        {
                            str(item.get("path")).strip()
                            for item in integrity_records
                            if isinstance(item.get("path"), str) and str(item.get("path")).strip()
                        }
                    )
                    if not reasons:
                        reasons = ["plan_copy_integrity_unknown"]
                    skipped_integrity_unknown += 1
                    integrity_unknown_skips.append(
                        {
                            "fingerprint": fingerprint,
                            "owner_root": str(owner_repo_root),
                            "reasons": reasons,
                            "paths": paths,
                        }
                    )
                    print(
                        "Skipping ticket because a matching plan copy has unknown integrity: "
                        f"fingerprint={fingerprint!r} reasons={reasons!r}",
                        file=sys.stderr,
                    )
                    continue

                _register_current_replacement(
                    ticket=ticket,
                    fingerprint=fingerprint,
                    owner_repo_root=owner_repo_root,
                )
                desired_status = _normalize_atom_status(_coerce_string(existing.get("status")))
                if desired_status not in ("queued", "actioned"):
                    desired_status = "queued"

                # The read-only change-plan pass already proved that a case-aware
                # actioned copy has a durable outcome. A nonterminal outcome keeps
                # this exact plan revision current for verification follow-up.

                skipped_existing_plan += 1

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
                        execution_conflict_keys=_coerce_string_list(
                            ticket.get("execution_conflict_keys")
                        ),
                        ux_review_section=ux_section,
                        case_id=ticket_export_case_id(ticket),
                        plan_revision_id=ticket_export_plan_revision_id(ticket),
                        requires_live_verification=_ticket_requires_live_verification(ticket),
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
        _register_current_replacement(
            ticket=ticket,
            fingerprint=fingerprint,
            owner_repo_root=owner_repo_root,
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

        exported_verification_contract = parse_verification_contract_markdown(body)
        exported_target_contract = parse_plan_target_contract_markdown(body)
        local_plan_markdown = idea_path.read_text(encoding="utf-8")
        exports.append(
            {
                "fingerprint": fingerprint,
                "case_id": ticket_export_case_id(ticket),
                "plan_revision_id": ticket_export_plan_revision_id(ticket),
                "export_kind": export_kind,
                "title": issue_title,
                "labels": labels,
                "body_markdown": body,
                "body_sha256": canonical_ticket_body_sha256(body),
                "local_plan_sha256": canonical_plan_sha256(local_plan_markdown),
                "verification_contract_sha256": (
                    exported_verification_contract["contract_sha256"]
                    if exported_verification_contract is not None
                    else None
                ),
                "target_contract_sha256": (
                    exported_target_contract["contract_sha256"]
                    if exported_target_contract is not None
                    else None
                ),
                "source_ticket": {
                    "fingerprint": fingerprint,
                    "case_id": ticket_export_case_id(ticket),
                    "plan_revision_id": ticket_export_plan_revision_id(ticket),
                    "stage": stage_effective,
                    "severity": severity,
                    "ticket_readiness": ticket.get("ticket_readiness"),
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
        stale_cleanup = _cleanup_stale_generated_scope_ticket_files(
            owner_repo_root=owner_root,
            target_slug=target_slug,
            keep_fingerprints=keep_fingerprints,
            keep_fingerprint_by_case_id=keep_fingerprint_by_case_by_owner_root.get(owner_root),
        )
        swept_scope_stale_generated_archived += stale_cleanup["archived"]
        swept_scope_stale_generated_unresolved += stale_cleanup["unresolved"]

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
            "shadow_export_gate": shadow_gate_meta,
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
            "skipped_legacy_actioned": skipped_legacy_actioned,
            "skipped_discarded": skipped_discarded,
            "skipped_existing_plan": skipped_existing_plan,
            "skipped_awaiting_outcome_verification": skipped_awaiting_outcome,
            "skipped_integrity_unknown": skipped_integrity_unknown,
            "skipped_stage": skipped_stage,
            "skipped_severity": skipped_severity,
            "actioned_total": len(actions),
            "idea_files_written": len(idea_files_written),
            "generated_queue_files_refreshed": generated_queue_files_refreshed,
            # Destructive cleanup was removed. Retain the old counters as
            # explicit zeroes so consumers cannot mistake archival for deletion.
            "swept_actioned_queue_dupes_removed": 0,
            "swept_actioned_bucket_dupes_removed": 0,
            "swept_scope_stale_generated_removed": 0,
            "swept_actioned_queue_dupes_archived": swept_actioned_queue_dupes_archived,
            "swept_actioned_bucket_dupes_archived": swept_actioned_bucket_dupes_archived,
            "swept_scope_stale_generated_archived": swept_scope_stale_generated_archived,
            "swept_scope_stale_generated_unresolved": swept_scope_stale_generated_unresolved,
            "ux_recommendations_loaded": len(ux_recommendations_by_fingerprint),
            "ux_plan_tickets_updated": ux_plan_tickets_updated,
            "ux_idea_files_updated": ux_idea_files_updated,
            "ux_tickets_deferred": ux_tickets_deferred,
            "pre_export_atom_sync": pre_export_atom_sync_meta,
            "atom_status_updates": atom_status_meta,
        },
        "integrity_unknown_skips": integrity_unknown_skips,
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
