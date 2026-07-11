# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from backlog_core import assess_research_readiness, build_operational_failure_candidates
from backlog_miner.research_evidence import BlockedReplayExecutor
from backlog_repo import verify_outcome_record_provenance

from usertest_backlog.commands.atom_actions import (
    _backfill_failure_event_atoms_from_legacy_entries,
    _update_atom_actions_from_backlog,
)
from usertest_backlog.commands.export_tickets import (
    _build_export_projection,
    _export_artifact_paths,
    _ux_review_path_for_backlog,
)
from usertest_backlog.shared import *
from usertest_backlog.workflows.derived_evidence import (
    annotate_operational_failure_candidates,
    annotate_primary_derived_evidence,
    filter_derived_history_records,
    inferred_implementation_runs_root,
    ingest_derived_evidence_records,
    with_operational_candidate_metadata,
)
from usertest_backlog.workflows.implementation_planning import _run_implementation_planning_stage
from usertest_backlog.workflows.orphan_implementation_history import (
    recover_orphan_implementation_history,
)
from usertest_backlog.workflows.post_research_relations import (
    collapse_post_research_verified_mechanisms,
)
from usertest_backlog.workflows.prioritization import _run_problem_prioritization_stage
from usertest_backlog.workflows.problem_mining import (
    _persist_canonical_relation_receipts,
    _run_problem_case_relation_review,
    _run_problem_mining_stage,
)
from usertest_backlog.workflows.reproduction_research import (
    _configured_replay_executor,
    _run_repro_research_stage,
)
from usertest_backlog.workflows.shadow_validation import (
    evaluate_shadow_invariants,
    normalize_shadow_gate_config,
    record_shadow_cycle,
    shadow_state_path,
)
from usertest_backlog.workflows.solution_options import _run_solution_optioning_stage
from usertest_backlog.workflows.solution_selection import _run_solution_selection_stage


def _require_stage_model_invocation_provenance(stage_doc: dict[str, Any]) -> None:
    errors = verify_stage_model_invocation_contract(stage_doc)
    if errors:
        raise ValueError(
            "stage_model_invocation_provenance_invalid:"
            + str(stage_doc.get("stage") or "unknown")
            + ":"
            + ",".join(errors)
        )


def _persist_downstream_case_lineage(
    *,
    stage_doc: dict[str, Any],
    out_json: Path,
    problem_cases: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Attach canonical case identity to a stage document and persist it.

    Stage parsers retain legacy wire compatibility, so lineage is enforced at this
    orchestration boundary for every newly written stage artifact.
    """

    items_raw = stage_doc.get("items")
    items = (
        [item for item in items_raw if isinstance(item, dict)]
        if isinstance(items_raw, list)
        else []
    )
    propagated = propagate_case_lineage(
        items,
        problem_cases,
        strict_new_output=True,
    )
    updated_doc = dict(stage_doc)
    updated_doc["items"] = propagated
    input_meta_raw = updated_doc.get("input_meta")
    input_meta = dict(input_meta_raw) if isinstance(input_meta_raw, dict) else {}
    input_meta["case_lineage_propagated"] = True
    input_meta["canonical_case_count"] = len(problem_cases)
    updated_doc["input_meta"] = input_meta
    out_json.write_text(
        json.dumps(updated_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return updated_doc, propagated


def _persist_case_registry_stage_lineage(
    *,
    case_registry: dict[str, Any],
    case_registry_path: Path,
    stage_doc: dict[str, Any],
) -> dict[str, Any]:
    """Persist one completed stage into the cumulative case graph."""

    updated = update_case_registry_stage_lineage(
        case_registry,
        stage_doc=stage_doc,
        strict=True,
    )
    write_case_registry(case_registry_path, updated)
    return updated


def _ticket_lineage_stage_document(
    *,
    tickets: list[dict[str, Any]],
    problem_cases: list[dict[str, Any]],
    generated_at: str,
    backlog_json_path: Path,
    backlog_md_path: Path,
) -> dict[str, Any]:
    """Build a compact ticket-assembly artifact for the persistent case graph."""

    records: list[dict[str, Any]] = []
    represented_case_ids: set[str] = set()
    for ticket in tickets:
        case_id = ticket_export_case_id(ticket)
        if case_id is None:
            raise ValueError("ticket_assembly_lineage_missing_case_id")
        represented_case_ids.add(case_id)
        problem_raw = ticket.get("problem_record")
        problem = problem_raw if isinstance(problem_raw, dict) else {}
        records.append(
            {
                "case_id": case_id,
                "problem_id": _coerce_string(ticket.get("problem_id"))
                or _coerce_string(problem.get("problem_id")),
                "plan_revision_id": ticket_export_plan_revision_id(ticket),
                "ticket_fingerprint": ticket_export_fingerprint(ticket),
                "ticket_stage": _coerce_string(ticket.get("stage")) or "triage",
            }
        )

    # A no-ticket result is still a ticket-assembly outcome worth retaining.  It has
    # no fingerprint and therefore cannot be confused with an exported ticket identity.
    for problem_case in problem_cases:
        case_id = _coerce_string(problem_case.get("case_id"))
        if case_id is None or case_id in represented_case_ids:
            continue
        records.append(
            {
                "case_id": case_id,
                "problem_id": _coerce_string(problem_case.get("problem_id")),
                "ticket_stage": "not_emitted",
            }
        )

    stage_doc = build_stage_document(
        "ticket_assembly",
        records,
        input_meta={
            "ticket_count": len(tickets),
            "case_count": len(problem_cases),
        },
        artifacts={
            "backlog_json": str(backlog_json_path),
            "backlog_md": str(backlog_md_path),
        },
    )
    stage_doc["generated_at"] = generated_at
    return stage_doc


def _sync_case_registry_outcomes(
    *,
    case_registry: dict[str, Any],
    atom_actions: dict[str, dict[str, Any]],
    trusted_runs_roots: tuple[Path, ...] = (),
    owner_roots: tuple[Path, ...] = (),
) -> dict[str, int]:
    """Apply durable atom-ledger outcomes to persistent case lifecycle state.

    Plan-folder reconciliation validates embedded outcome records before copying their
    state and case identity into the atom ledger.  The backlog pipeline consumes that
    durable projection here instead of treating queue/action status itself as resolution.
    """

    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, dict) else {}
    plans_by_case: dict[str, dict[str, dict[str, Any]]] = {}
    legacy_latest_by_case: dict[str, tuple[str, str]] = {}
    invalid_outcome_records = 0
    provenance_failed_outcome_records = 0
    for entry in atom_actions.values():
        case_id = _coerce_string(entry.get("case_id"))
        if case_id is None:
            continue
        plan_outcomes_raw = entry.get("plan_outcomes")
        if isinstance(plan_outcomes_raw, dict):
            case_plans = plans_by_case.setdefault(case_id, {})
            for raw_revision_id, raw_plan_outcome in plan_outcomes_raw.items():
                revision_id = _coerce_string(raw_revision_id)
                if revision_id is None or not isinstance(raw_plan_outcome, dict):
                    continue
                required = raw_plan_outcome.get("required") is not False
                outcome_record_raw = raw_plan_outcome.get("outcome_record")
                normalized_outcome: dict[str, Any] | None = None
                outcome_verification: dict[str, Any] | None = None
                if isinstance(outcome_record_raw, dict):
                    outcome_verification = verify_outcome_record_provenance(
                        outcome_record_raw,
                        trusted_runs_roots=trusted_runs_roots,
                        owner_roots=owner_roots,
                        case_registry=case_registry,
                    )
                    structural_record = outcome_verification.get("outcome_record")
                    if outcome_verification.get("structural_status") != "valid":
                        invalid_outcome_records += 1
                    elif outcome_verification.get("verified") is not True:
                        provenance_failed_outcome_records += 1
                    elif isinstance(structural_record, dict):
                        normalized_outcome = structural_record
                        if normalized_outcome.get("outcome_scope") == "plan_copy":
                            # Copy disposition is archival lineage, never case lifecycle.
                            continue
                        if (
                            normalized_outcome.get("case_id") != case_id
                            or normalized_outcome.get("plan_revision_id") != revision_id
                        ):
                            invalid_outcome_records += 1
                            normalized_outcome = None

                if normalized_outcome is not None:
                    state = str(normalized_outcome["state"])
                    recorded_at = str(normalized_outcome["recorded_at"])
                else:
                    raw_state = (_coerce_string(raw_plan_outcome.get("state")) or "").lower()
                    # Only a planned sentinel is trusted without a complete validated
                    # OutcomeRecord. Any projected terminal state fails open.
                    state = "planned" if raw_state == "planned" else "unverified"
                    recorded_at = _coerce_string(raw_plan_outcome.get("recorded_at")) or ""
                candidate = {
                    "state": state,
                    "recorded_at": recorded_at,
                    "path": _coerce_string(raw_plan_outcome.get("path")) or "",
                    "fingerprint": _coerce_string(raw_plan_outcome.get("fingerprint")) or "",
                    "required": required,
                }
                if outcome_verification is not None:
                    candidate["outcome_verification"] = outcome_verification
                    if (
                        outcome_verification.get("structural_status") == "valid"
                        and outcome_verification.get("verified") is not True
                    ):
                        candidate["structural_outcome_record"] = outcome_verification.get(
                            "outcome_record"
                        )
                if normalized_outcome is not None:
                    candidate["outcome_record"] = normalized_outcome
                previous = case_plans.get(revision_id)
                if previous is None:
                    case_plans[revision_id] = candidate
                else:
                    previous_state = previous.get("state", "planned")
                    previous_terminal = previous_state in TERMINAL_CASE_STATES
                    candidate_terminal = state in TERMINAL_CASE_STATES
                    if previous_terminal and not candidate_terminal:
                        case_plans[revision_id] = candidate
                    elif previous_terminal == candidate_terminal and recorded_at > previous.get(
                        "recorded_at", ""
                    ):
                        case_plans[revision_id] = candidate
                    case_plans[revision_id]["required"] = (
                        previous.get("required") is not False or required
                    )
            continue

        outcome_record_raw = entry.get("last_outcome_record")
        normalized_outcome = None
        outcome_verification = None
        if isinstance(outcome_record_raw, dict):
            outcome_verification = verify_outcome_record_provenance(
                outcome_record_raw,
                trusted_runs_roots=trusted_runs_roots,
                owner_roots=owner_roots,
                case_registry=case_registry,
            )
            structural_record = outcome_verification.get("outcome_record")
            if outcome_verification.get("structural_status") != "valid":
                invalid_outcome_records += 1
            elif outcome_verification.get("verified") is not True:
                provenance_failed_outcome_records += 1
            elif isinstance(structural_record, dict):
                normalized_outcome = structural_record
                if (
                    normalized_outcome.get("outcome_scope") != "case"
                    or normalized_outcome.get("case_id") != case_id
                ):
                    invalid_outcome_records += 1
                    normalized_outcome = None
        raw_outcome_state = (_coerce_string(entry.get("last_outcome_state")) or "").lower()
        if normalized_outcome is not None:
            outcome_state = str(normalized_outcome["state"])
            recorded_at = str(normalized_outcome["recorded_at"])
        elif raw_outcome_state:
            # A bare legacy label proves neither implementation nor verification.
            # Preserve the harmless planned sentinel; every more advanced state must
            # carry a complete validated OutcomeRecord or fail open to unverified.
            outcome_state = "planned" if raw_outcome_state == "planned" else "unverified"
            recorded_at = _coerce_string(entry.get("last_outcome_recorded_at")) or ""
        else:
            continue
        previous = legacy_latest_by_case.get(case_id)
        if previous is None:
            legacy_latest_by_case[case_id] = (recorded_at, outcome_state)
        else:
            previous_state = previous[1]
            previous_terminal = previous_state in TERMINAL_CASE_STATES
            candidate_terminal = outcome_state in TERMINAL_CASE_STATES
            if previous_terminal and not candidate_terminal:
                legacy_latest_by_case[case_id] = (recorded_at, outcome_state)
            elif previous_terminal == candidate_terminal and recorded_at > previous[0]:
                legacy_latest_by_case[case_id] = (recorded_at, outcome_state)

    updated = 0
    terminal = 0
    nonterminal = 0
    all_case_ids = set(plans_by_case) | set(legacy_latest_by_case)
    terminal_priority = {"superseded": 1, "duplicate": 2, "resolved": 3}
    for case_id in sorted(all_case_ids):
        raw_case = cases.get(case_id)
        if not isinstance(raw_case, dict):
            continue
        case_plans = plans_by_case.get(case_id, {})
        selected_revision_id: str | None = None
        selected_outcome: dict[str, Any] | None = None
        legacy_recorded_at: str | None = None
        if case_plans:
            required_plans = [
                (revision_id, outcome)
                for revision_id, outcome in case_plans.items()
                if outcome.get("required") is not False
            ]
            if not required_plans:
                case = dict(raw_case)
                case["plan_outcomes"] = case_plans
                cases[case_id] = case
                continue
            open_plans = [
                (revision_id, outcome)
                for revision_id, outcome in required_plans
                if outcome.get("state") not in TERMINAL_CASE_STATES
            ]
            if open_plans:
                selected_revision_id, selected = max(
                    open_plans,
                    key=lambda item: (
                        item[1].get("recorded_at", ""),
                        item[0],
                    ),
                )
                selected_outcome = selected
                outcome_state = selected.get("state", "planned")
            else:
                selected_revision_id, selected = max(
                    required_plans,
                    key=lambda item: (
                        terminal_priority.get(item[1].get("state", ""), 0),
                        item[1].get("recorded_at", ""),
                        item[0],
                    ),
                )
                selected_outcome = selected
                outcome_state = selected.get("state", "resolved")
        else:
            if case_id not in legacy_latest_by_case:
                continue
            legacy_recorded_at, outcome_state = legacy_latest_by_case[case_id]
        case = dict(raw_case)
        selected_recorded_state = outcome_state
        recurrence_reopen_raw = case.get("recurrence_reopen")
        recurrence_reopen = (
            recurrence_reopen_raw if isinstance(recurrence_reopen_raw, dict) else None
        )
        recurrence_reopened = bool(
            recurrence_reopen is not None
            and selected_revision_id is not None
            and outcome_state in TERMINAL_CASE_STATES
            and recurrence_reopen.get("against_plan_revision_id") == selected_revision_id
        )
        if recurrence_reopened:
            # A previously terminal outcome cannot suppress newer evidence that relation
            # review attached to the same canonical case. Keep the old outcome as
            # provenance, but reopen lifecycle state until a different plan revision earns
            # a new terminal outcome.
            outcome_state = "unverified"
        elif (
            recurrence_reopen is not None
            and selected_revision_id is not None
            and outcome_state in TERMINAL_CASE_STATES
            and recurrence_reopen.get("against_plan_revision_id") != selected_revision_id
        ):
            case.pop("recurrence_reopen", None)
        if _coerce_string(case.get("state")) != outcome_state:
            updated += 1
        case["state"] = outcome_state
        case["last_outcome_state"] = selected_recorded_state
        current_lifecycle: dict[str, Any] = {"state": outcome_state}
        if selected_revision_id is not None and selected_outcome is not None:
            verification_raw = selected_outcome.get("outcome_verification")
            verification = verification_raw if isinstance(verification_raw, dict) else None
            provenance_status = (
                str(verification.get("provenance_status"))
                if verification is not None
                else "fail_open_projection"
            )
            has_accepted_record = isinstance(selected_outcome.get("outcome_record"), dict)
            if has_accepted_record:
                outcome_source = (
                    "provenance_verified_plan_outcome"
                    if provenance_status == "verified"
                    else "structurally_valid_nonterminal_plan_outcome"
                )
            elif verification is not None and verification.get("structural_status") == "valid":
                outcome_source = "structurally_valid_unverified_plan_outcome"
            else:
                outcome_source = "atom_action_projection"
            outcome_reference = {
                "source": outcome_source,
                "validation_status": provenance_status,
                "plan_revision_id": selected_revision_id,
                "recorded_at": selected_outcome.get("recorded_at", ""),
                "path": selected_outcome.get("path", ""),
                "fingerprint": selected_outcome.get("fingerprint", ""),
            }
            if recurrence_reopened:
                outcome_reference["source"] = "same_class_recurrence_reopen"
                outcome_reference["recurrence_reopen"] = recurrence_reopen
            if verification is not None and verification.get("errors"):
                outcome_reference["verification_errors"] = verification.get("errors")
            current_lifecycle["outcome_reference"] = outcome_reference
            case["last_outcome_recorded_at"] = selected_outcome.get("recorded_at", "")
        elif legacy_recorded_at is not None:
            current_lifecycle["outcome_reference"] = {
                "source": "legacy_atom_action_projection",
                "validation_status": "projected",
                "recorded_at": legacy_recorded_at,
            }
            case["last_outcome_recorded_at"] = legacy_recorded_at
        case["current_lifecycle"] = current_lifecycle
        if case_plans:
            case["plan_outcomes"] = case_plans
        cases[case_id] = case
        if outcome_state in TERMINAL_CASE_STATES:
            terminal += 1
        else:
            nonterminal += 1
    case_registry["cases"] = cases
    return {
        "cases_updated": updated,
        "terminal_cases": terminal,
        "nonterminal_cases": nonterminal,
        "invalid_outcome_records": invalid_outcome_records,
        "provenance_failed_outcome_records": provenance_failed_outcome_records,
    }


def _outcome_trusted_runs_roots(
    *,
    primary_runs_dir: Path,
    configured_runs_dir: Path,
    implementation_runs_root: Path,
) -> tuple[Path, ...]:
    """Return the complete retained-evidence boundary for outcome verification."""

    return tuple(
        sorted(
            {
                primary_runs_dir.resolve(),
                configured_runs_dir.resolve(),
                implementation_runs_root.resolve(),
            },
            key=lambda path: str(path),
        )
    )


def _case_state_from_registry(case_registry: dict[str, Any], case_id: str | None) -> str | None:
    """Return a case lifecycle state without treating aliases as resolution."""

    if case_id is None:
        return None
    cases = case_registry.get("cases")
    if not isinstance(cases, dict):
        return None
    entry = cases.get(case_id)
    if not isinstance(entry, dict):
        return None
    return _coerce_string(entry.get("state")) or "active"


def _case_has_proven_terminal_outcome(
    case_registry: dict[str, Any],
    case_id: str | None,
) -> bool:
    """Return whether a terminal case is backed by provenance-verified evidence."""

    if case_id is None:
        return False
    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, dict) else {}
    entry_raw = cases.get(case_id)
    entry = entry_raw if isinstance(entry_raw, dict) else {}
    state = _coerce_string(entry.get("state"))
    lifecycle_raw = entry.get("current_lifecycle")
    lifecycle = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
    reference_raw = lifecycle.get("outcome_reference")
    reference = reference_raw if isinstance(reference_raw, dict) else {}
    return bool(
        state in TERMINAL_CASE_STATES
        and _coerce_string(lifecycle.get("state")) in {None, state}
        and _coerce_string(reference.get("validation_status")) == "verified"
    )


def _registry_case_id_for_atom(
    case_registry: dict[str, Any],
    atom_id: str | None,
) -> str | None:
    """Resolve the canonical case mapping retained by the runner-owned registry."""

    if atom_id is None:
        return None
    mapping_raw = case_registry.get("atom_id_to_case_id")
    mapping = mapping_raw if isinstance(mapping_raw, dict) else {}
    return _coerce_string(mapping.get(atom_id))


def _reset_stale_unproven_actioned_atoms(
    *,
    atom_actions: dict[str, dict[str, Any]],
    case_registry: dict[str, Any],
    current_plan_sync_at: str | None,
    generated_at: str,
) -> dict[str, int]:
    """Fail open legacy ``actioned`` rows whose plan and outcome disappeared.

    A historical status label is not resolution evidence. When a complete plan-folder
    scan finds no surviving plan and no provenance-verified terminal case, the atom is
    returned to ``new`` so its observed evidence can be researched again. IDEA intake
    records remain outside this automated remediation boundary.
    """

    if current_plan_sync_at is None:
        return {"examined": 0, "reset_to_new": 0, "idea_excluded": 0}
    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, dict) else {}
    examined = 0
    reset = 0
    idea_excluded = 0
    for entry in atom_actions.values():
        if _normalize_atom_status(_coerce_string(entry.get("status"))) != "actioned":
            continue
        examined += 1
        if atom_is_idea_originated(entry):
            idea_excluded += 1
            continue
        if _coerce_string(entry.get("last_plan_seen_at")) == current_plan_sync_at:
            continue
        case_id = _coerce_string(entry.get("case_id"))
        case_raw = cases.get(case_id) if case_id is not None else None
        case = case_raw if isinstance(case_raw, dict) else {}
        lifecycle_raw = case.get("current_lifecycle")
        lifecycle = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
        reference_raw = lifecycle.get("outcome_reference")
        reference = reference_raw if isinstance(reference_raw, dict) else {}
        case_state = _coerce_string(case.get("state"))
        if case and case_state not in TERMINAL_CASE_STATES:
            # A live canonical work unit remains the durable owner of this evidence.
            # Queue/action state must not detach or relabel it as a new problem.
            continue
        if (
            case_state in TERMINAL_CASE_STATES
            and _coerce_string(reference.get("validation_status")) == "verified"
        ):
            continue

        entry["stale_actioned_previous_status"] = "actioned"
        entry["stale_actioned_previous_case_id"] = case_id
        entry["stale_actioned_previous_disposition"] = _coerce_string(entry.get("disposition"))
        entry["stale_actioned_previous_supporting_case_ids"] = (
            list(entry.get("supporting_case_ids"))
            if isinstance(entry.get("supporting_case_ids"), list)
            else []
        )
        entry["status"] = "new"
        entry["stale_actioned_reset_at"] = generated_at
        entry["stale_actioned_reset_reason"] = (
            "no_surviving_plan_or_provenance_verified_terminal_outcome"
        )
        entry["disposition"] = "unresolved"
        entry["disposition_status"] = "pending"
        entry["disposition_rationale"] = (
            "The historical action label lacks a surviving plan or a "
            "provenance-verified terminal outcome and must be reconsidered."
        )
        entry.pop("disposition_receipt", None)
        entry.pop("supporting_case_ids", None)
        if not case:
            entry.pop("case_id", None)
            entry.pop("novel_case_rationale", None)
        reset += 1
    return {"examined": examined, "reset_to_new": reset, "idea_excluded": idea_excluded}


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
    shadow = bool(getattr(args, "shadow", False))
    if shadow and bool(args.dry_run):
        print("Cannot combine --shadow with --dry-run.", file=sys.stderr)
        return 2

    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)
    research_config_path = repo_root / "configs" / "backlog_research.yaml"
    research_config: dict[str, Any] = {}
    if research_config_path.exists():
        research_config_raw = _load_yaml(research_config_path).get("backlog_research", {})
        if not isinstance(research_config_raw, dict):
            print(f"Invalid backlog research config: {research_config_path}", file=sys.stderr)
            return 2
        research_config = research_config_raw
    research_ref = _coerce_string(getattr(args, "research_ref", None)) or _coerce_string(
        research_config.get("source_ref")
    )
    replay_timeout_raw = research_config.get("clean_replay_timeout_seconds")
    replay_timeout_seconds = (
        float(replay_timeout_raw)
        if isinstance(replay_timeout_raw, (int, float))
        and not isinstance(replay_timeout_raw, bool)
        and float(replay_timeout_raw) > 0
        else 10800.0
    )
    shadow_gate_config = normalize_shadow_gate_config(None)
    if shadow:
        shadow_gate_config_path = repo_root / "configs" / "backlog_export_gate.yaml"
        if not shadow_gate_config_path.is_file():
            print(
                f"Missing backlog export gate config: {shadow_gate_config_path}",
                file=sys.stderr,
            )
            return 2
        try:
            shadow_gate_raw = _load_yaml(shadow_gate_config_path).get("backlog_export_gate", {})
            shadow_gate_config = normalize_shadow_gate_config(shadow_gate_raw)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            print(
                f"Invalid backlog export gate: {shadow_gate_config_path}: {exc}",
                file=sys.stderr,
            )
            return 2
        if shadow_gate_config["enabled"] is not True:
            print(
                f"Backlog export gate is disabled: {shadow_gate_config_path}",
                file=sys.stderr,
            )
            return 2

    runs_dir = args.runs_dir.resolve() if args.runs_dir is not None else cfg.runs_dir
    # The CLI-selected history root is also the append-only destination for stage
    # runners.  Without this replacement, an isolated checkout writes new research
    # beside itself while the next backlog cycle scans the owner's explicit root.
    cfg = replace(cfg, runs_dir=runs_dir)
    implementation_runs_root = inferred_implementation_runs_root(runs_dir)
    outcome_trusted_runs_roots = _outcome_trusted_runs_roots(
        primary_runs_dir=runs_dir,
        configured_runs_dir=cfg.runs_dir,
        implementation_runs_root=implementation_runs_root,
    )
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
    case_registry_json = out_json.parent / f"{default_name}.case_registry.json"
    try:
        case_registry = load_case_registry(case_registry_json)
    except ValueError as exc:
        print(f"[backlog] ERROR: {exc}", file=sys.stderr)
        return 2

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
    extracted_atoms = (
        [item for item in atoms_raw if isinstance(item, dict)]
        if isinstance(atoms_raw, list)
        else []
    )
    primary_raw_atoms = normalize_atom_lineage(
        extracted_atoms,
        case_registry=case_registry,
        strict_new_output=True,
    )
    primary_derived_evidence = annotate_primary_derived_evidence(
        records,
        primary_raw_atoms,
        source_root=runs_dir,
        case_registry=case_registry,
    )
    primary_raw_atoms = primary_derived_evidence.atoms
    primary_atom_ids = {
        atom_id
        for atom in primary_raw_atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        if atom_id is not None
    }

    try:
        atom_actions = _load_atom_actions_yaml(atom_actions_path)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    plan_sync_meta: dict[str, Any] | None = None
    plan_sync_at: str | None = None
    candidate_roots: list[Path] = [repo_root]
    if repo_input is not None and _looks_like_local_repo_input(repo_input):
        resolved_repo_input = _resolve_local_repo_root(repo_root, repo_input)
        if resolved_repo_input is not None:
            candidate_roots.append(resolved_repo_input)
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
        if resolved is not None:
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
            if resolved is not None:
                candidate_roots.append(resolved)
    owner_roots = sorted({p.resolve() for p in candidate_roots}, key=lambda p: str(p))

    if not bool(getattr(args, "skip_plan_folder_sync", False)):
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
        if not shadow:
            _write_atom_actions_yaml(atom_actions_path, atom_actions)

    case_outcome_sync = _sync_case_registry_outcomes(
        case_registry=case_registry,
        atom_actions=atom_actions,
        trusted_runs_roots=outcome_trusted_runs_roots,
        owner_roots=tuple(owner_roots),
    )
    stale_actioned_reset = _reset_stale_unproven_actioned_atoms(
        atom_actions=atom_actions,
        case_registry=case_registry,
        current_plan_sync_at=plan_sync_at,
        generated_at=backfill_at,
    )
    if not shadow and stale_actioned_reset["reset_to_new"]:
        _write_atom_actions_yaml(atom_actions_path, atom_actions)
    try:
        # Lifecycle evidence is durable independently of whether a later mining stage
        # succeeds; do not wait for relation review to persist validated outcomes.
        write_case_registry(case_registry_json, case_registry)
    except (OSError, ValueError) as exc:
        print(f"[backlog] ERROR: failed to persist case outcomes: {exc}", file=sys.stderr)
        return 2

    derived_scope_repo_root = repo_root
    if repo_input is not None and _looks_like_local_repo_input(repo_input):
        resolved_scope_repo_root = _resolve_local_repo_root(repo_root, repo_input)
        if resolved_scope_repo_root is not None:
            derived_scope_repo_root = resolved_scope_repo_root
    orphan_history_records, orphan_history_recovery_meta = recover_orphan_implementation_history(
        runs_dir,
        target_slug=target_slug,
        scoped_repo_root=derived_scope_repo_root,
    )
    derived_history_records_unfiltered = [
        *iter_report_history(
            implementation_runs_root,
            target_slug=target_slug,
            repo_input=None,
            embed="none",
        ),
        *orphan_history_records,
    ]
    derived_history_records, derived_scope_filter_meta = filter_derived_history_records(
        derived_history_records_unfiltered,
        target_slug=target_slug,
        repo_input=repo_input,
        repo_root=derived_scope_repo_root,
        git_remote_urls=sorted(_git_remote_urls(derived_scope_repo_root)),
    )
    derived_ingestion = ingest_derived_evidence_records(
        derived_history_records,
        source_root=implementation_runs_root,
        repo_root=repo_root,
        atom_actions=atom_actions,
        case_registry=case_registry,
    )
    operational_failure_candidates = build_operational_failure_candidates(
        [*records, *derived_ingestion.records],
        [*primary_raw_atoms, *derived_ingestion.atoms],
        parent_bindings_by_run={
            **primary_derived_evidence.parent_bindings_by_run,
            **derived_ingestion.parent_bindings_by_run,
        },
    )
    operational_failure_candidates = annotate_operational_failure_candidates(
        operational_failure_candidates,
        records=[*records, *derived_ingestion.records],
        source_atoms=[*primary_raw_atoms, *derived_ingestion.atoms],
        primary_source_root=runs_dir,
    )
    derived_evidence_meta = with_operational_candidate_metadata(
        derived_ingestion.metadata,
        operational_failure_candidates,
    )
    derived_evidence_meta["scope_filter"] = derived_scope_filter_meta
    derived_evidence_meta["orphan_history_recovery"] = orphan_history_recovery_meta
    derived_evidence_meta["primary_derived_evidence"] = primary_derived_evidence.metadata
    derived_evidence_meta["source_roots"] = [
        {
            "kind": "usertest",
            "path": str(runs_dir.resolve()),
            "records_seen": len(records),
            "derived_records": primary_derived_evidence.metadata["derived_records"],
        },
        *derived_evidence_meta["source_roots"],
    ]
    raw_atoms = [
        *primary_raw_atoms,
        *derived_ingestion.atoms,
        *operational_failure_candidates,
    ]

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

    # A queue/ticket/action label records workflow movement, not resolution.  The
    # default filter suppresses these atoms only when a provenance-verified terminal
    # outcome exists. Active canonical cases remain attached to their work unit, while
    # unmapped or unproven evidence fails open for another complete mining pass. An
    # explicit --exclude-atom-status remains an operator override.
    default_status_filter = not bool(args.exclude_atom_status)
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
    reopened_atoms: list[dict[str, Any]] = []
    reopened_status_counts: dict[str, int] = {}
    reopened_reason_counts: dict[str, int] = {}
    preserved_open_case_status_counts: dict[str, int] = {}
    reopened_case_identity: dict[str, str | None] = {}
    for atom in raw_atoms:
        atom_id = _coerce_string(atom.get("atom_id"))
        atom_status = "new"
        if atom_id is not None:
            existing = atom_actions.get(atom_id)
            if isinstance(existing, dict):
                atom_status = _normalize_atom_status(_coerce_string(existing.get("status")))
        action_entry = atom_actions.get(atom_id) if atom_id is not None else None
        stale_reset_status = (
            _normalize_atom_status(
                _coerce_string(action_entry.get("stale_actioned_previous_status"))
            )
            if isinstance(action_entry, dict)
            and _coerce_string(action_entry.get("stale_actioned_reset_at")) == backfill_at
            else None
        )
        historical_status = stale_reset_status or atom_status
        action_case_id = (
            _coerce_string(action_entry.get("case_id")) if isinstance(action_entry, dict) else None
        )
        if isinstance(action_entry, dict):
            explicit_disposition = _coerce_string(action_entry.get("disposition"))
            if explicit_disposition in ATOM_DISPOSITIONS:
                atom = dict(atom)
                novel_rationale = _coerce_string(action_entry.get("novel_case_rationale"))
                if novel_rationale is not None:
                    atom["novel_case_rationale"] = novel_rationale
                if explicit_disposition == "supports_case" and action_case_id is not None:
                    atom["case_id"] = action_case_id
                    atom["supporting_case_ids"] = [action_case_id]
                    if _coerce_string(atom.get("evidence_role")) in {
                        "research",
                        "implementation",
                        "verification",
                    }:
                        atom["parent_case_id"] = action_case_id
                decision_rationale = _coerce_string(action_entry.get("disposition_rationale")) or (
                    novel_rationale if explicit_disposition == "novel_case" else None
                )
                if (
                    decision_rationale is None
                    and explicit_disposition == "supports_case"
                    and action_case_id is not None
                ):
                    decision_rationale = (
                        "The durable atom action ledger explicitly attaches this atom to "
                        f"{action_case_id}."
                    )
                derived_role = _coerce_string(atom.get("evidence_role")) in {
                    "research",
                    "implementation",
                    "verification",
                }
                decision_error = None
                if decision_rationale is None:
                    decision_error = "atom_action_disposition_rationale_missing"
                elif explicit_disposition == "supports_case" and action_case_id is None:
                    decision_error = "atom_action_supports_case_id_missing"
                elif (
                    explicit_disposition == "novel_case"
                    and derived_role
                    and _coerce_string(atom.get("parent_case_id")) is None
                ):
                    decision_error = "atom_action_novel_parent_case_id_missing"
                if decision_error is None:
                    atom = apply_atom_disposition_decision(
                        atom,
                        disposition=explicit_disposition,
                        source="atom_action_ledger",
                        rationale=decision_rationale,
                    )
                else:
                    atom["disposition_decision_error"] = decision_error
        registry_case_id = _registry_case_id_for_atom(case_registry, atom_id)
        atom_case_id = _coerce_string(atom.get("case_id")) or action_case_id or registry_case_id
        case_state = _case_state_from_registry(case_registry, atom_case_id)
        keep_for_open_case = case_state is not None and case_state not in TERMINAL_CASE_STATES
        proven_terminal_outcome = _case_has_proven_terminal_outcome(
            case_registry,
            atom_case_id,
        )
        idea_originated = atom_is_idea_originated(atom) or (
            isinstance(action_entry, dict) and atom_is_idea_originated(action_entry)
        )
        reopen_unproven = bool(
            default_status_filter
            and historical_status in exclude_atom_status_set
            and not idea_originated
            and not keep_for_open_case
            and not proven_terminal_outcome
        )
        if keep_for_open_case and atom_status in exclude_atom_status_set:
            preserved_open_case_status_counts[atom_status] = (
                preserved_open_case_status_counts.get(atom_status, 0) + 1
            )
        if reopen_unproven:
            reason = (
                "canonical_case_missing" if case_state is None else "terminal_outcome_not_proven"
            )
            stale_previous_disposition = (
                _coerce_string(action_entry.get("stale_actioned_previous_disposition"))
                if isinstance(action_entry, dict)
                else None
            )
            prior_disposition = stale_previous_disposition or _coerce_string(
                atom.get("disposition")
            )
            prior_supporting = (
                list(atom.get("supporting_case_ids"))
                if isinstance(atom.get("supporting_case_ids"), list)
                else []
            )
            reopen_audit = {
                "previous_status": historical_status,
                "previous_case_id": atom_case_id,
                "previous_disposition": prior_disposition,
                "previous_supporting_case_ids": prior_supporting,
                "reason": reason,
                "reopened_at": backfill_at,
            }
            atom = dict(atom)
            atom["status_reopen_audit"] = reopen_audit
            atom["disposition"] = "unresolved"
            atom["disposition_status"] = "pending"
            atom["disposition_receipt"] = None
            atom.pop("disposition_decision_error", None)
            atom["supporting_case_ids"] = []
            if case_state is None:
                atom["case_id"] = None
            else:
                # Retain identity so canonicalization updates/reopens this case rather
                # than minting a wording-derived replacement.
                atom["case_id"] = atom_case_id
            if isinstance(action_entry, dict):
                action_entry["reopened_previous_status"] = historical_status
                action_entry["reopened_previous_case_id"] = atom_case_id
                action_entry["reopened_previous_disposition"] = (
                    stale_previous_disposition or _coerce_string(action_entry.get("disposition"))
                )
                action_entry["reopened_previous_supporting_case_ids"] = list(prior_supporting)
                action_entry["reopened_at"] = backfill_at
                action_entry["reopened_reason"] = reason
                action_entry["status"] = "new"
                action_entry["disposition"] = "unresolved"
                action_entry["disposition_status"] = "pending"
                action_entry["disposition_rationale"] = (
                    "A queue/ticket/action label lacked a live canonical case or a "
                    "provenance-verified terminal outcome, so the evidence was reopened."
                )
                action_entry.pop("disposition_receipt", None)
                action_entry.pop("supporting_case_ids", None)
                if case_state is None:
                    action_entry.pop("case_id", None)
                else:
                    action_entry["case_id"] = atom_case_id
            if atom_id is not None:
                reopened_case_identity[atom_id] = atom_case_id if case_state is not None else None
            reopened_atoms.append(atom)
            reopened_status_counts[historical_status] = (
                reopened_status_counts.get(historical_status, 0) + 1
            )
            reopened_reason_counts[reason] = reopened_reason_counts.get(reason, 0) + 1
        if (
            atom_status in exclude_atom_status_set
            and not keep_for_open_case
            and not reopen_unproven
        ):
            excluded_atoms.append(atom)
            excluded_status_counts[atom_status] = excluded_status_counts.get(atom_status, 0) + 1
            continue
        if _coerce_string(atom.get("source")) == "agent_last_message_artifact":
            # Retain the diagnostic mirror, but do not remove this available observation
            # from problem mining. Complete bounded stage-1 review now handles the noise
            # explicitly instead of silently discarding potentially unique caveats.
            agent_last_message_atoms.append(atom)
        atoms.append(atom)

    if reopened_atoms and not shadow:
        # Persist immediately so a later stage failure cannot let the monotonic action
        # updater reapply a stale supports_case/ticketed row on the next cycle.
        _write_atom_actions_yaml(atom_actions_path, atom_actions)

    eligible_atoms_trackable = len(atoms)
    pipeline_batch_breadth = compute_batch_breadth(atoms)
    # Aggregate metrics may originate a problem, so their source population must be
    # constrained by runner-authored lineage rather than by mere retention.  Derived
    # research/implementation/verification runs remain useful on their parent cases,
    # but must not be recycled into fresh observation aggregates.
    eligible_run_rels = {
        run_rel
        for atom in atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        for run_rel in [_coerce_string(atom.get("run_rel"))]
        if atom_id in primary_atom_ids
        and run_rel is not None
        and _coerce_string(atom.get("evidence_role")) == "observation"
        and _coerce_string(atom.get("lineage_mining_blocker")) is None
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
    atoms = normalize_atom_lineage(
        atoms,
        case_registry=case_registry,
        strict_new_output=True,
    )
    if reopened_case_identity:
        reopened_normalized: list[dict[str, Any]] = []
        for atom in atoms:
            atom_id = _coerce_string(atom.get("atom_id"))
            if atom_id not in reopened_case_identity:
                reopened_normalized.append(atom)
                continue
            reopened = dict(atom)
            retained_case_id = reopened_case_identity[atom_id]
            reopened["disposition"] = "unresolved"
            reopened["disposition_status"] = "pending"
            reopened["disposition_receipt"] = None
            reopened["case_id"] = retained_case_id
            reopened["supporting_case_ids"] = []
            reopened_normalized.append(reopened)
        atoms = reopened_normalized
    agent_last_message_atoms = normalize_atom_lineage(
        agent_last_message_atoms,
        case_registry=case_registry,
        strict_new_output=True,
    )

    atom_totals = _summarize_atoms_for_totals(atoms)
    atoms_doc = dict(atoms_doc_raw)
    atoms_doc["atoms"] = atoms
    atoms_doc["derived_evidence_ingestion"] = derived_evidence_meta
    totals_raw = atoms_doc_raw.get("totals")
    totals_dict = dict(totals_raw) if isinstance(totals_raw, dict) else {}
    totals_dict.update(atom_totals)
    atoms_doc["totals"] = totals_dict
    atoms_doc["atom_filter"] = {
        "exclude_statuses": sorted(exclude_atom_status_set),
        "carryover": carryover_meta,
        "eligible_atoms": len(atoms),
        "eligible_atoms_trackable": eligible_atoms_trackable,
        "excluded_sources": [],
        "excluded_source_counts": {},
        "source_roots": {
            "primary": str(runs_dir.resolve()),
            "derived": [str(implementation_runs_root.resolve())],
        },
        "primary_records": len(records),
        "derived_records": derived_evidence_meta["records_ingested"],
        "derived_atoms": derived_evidence_meta["atoms_ingested"],
        "derived_binding_status_counts": derived_evidence_meta["binding_atom_status_counts"],
        "operational_failure_candidates": derived_evidence_meta["operational_failure_candidates"],
        "mirrored_diagnostic_sources": ["agent_last_message_artifact"],
        "mirrored_source_counts": {"agent_last_message_artifact": len(agent_last_message_atoms)},
        "mirrored_source_atoms_jsonl": str(agent_last_message_atoms_jsonl),
        "synthetic_atoms_added": len(aggregate_atoms),
        "excluded_atoms": len(excluded_atoms),
        "excluded_status_counts": excluded_status_counts,
        "default_status_filter": default_status_filter,
        "reopened_unproven_atoms": len(reopened_atoms),
        "reopened_status_counts": reopened_status_counts,
        "reopened_reason_counts": reopened_reason_counts,
        "reopened_atom_ids_preview": [
            atom_id
            for atom in reopened_atoms[:200]
            for atom_id in [_coerce_string(atom.get("atom_id"))]
            if atom_id is not None
        ],
        "preserved_open_case_status_counts": preserved_open_case_status_counts,
        "plan_folder_sync": plan_sync_meta,
        "case_outcome_sync": case_outcome_sync,
        "stale_actioned_reset": stale_actioned_reset,
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
    if shadow and (policy_cfg is None or policy_config_path is None):
        print(
            "Shadow backlog cycles require an enabled, explicit backlog policy config.",
            file=sys.stderr,
        )
        return 2

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
    problem_mining_evidence_json = problem_records_json.with_name(
        f"{problem_records_json.stem}.evidence_receipt.json"
    )
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
            repo_root=repo_root,
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
            case_registry=case_registry,
        )

        items1_raw = stage1_doc.get("items") if isinstance(stage1_doc, dict) else None
        newly_mined_problem_records = (
            [item for item in items1_raw if isinstance(item, dict)]
            if isinstance(items1_raw, list)
            else []
        )

        current_case_ids = {
            case_id
            for item in newly_mined_problem_records
            for case_id in [_coerce_string(item.get("case_id"))]
            if case_id is not None
        }
        carried_problem_records: list[dict[str, Any]] = []
        for historical_case in problem_case_records_from_registry(case_registry):
            historical_case_id = _coerce_string(historical_case.get("case_id"))
            state = _coerce_string(historical_case.get("case_state")) or "active"
            if historical_case_id in current_case_ids:
                continue
            if state in TERMINAL_CASE_STATES:
                continue
            carried = dict(historical_case)
            carried["_carried_forward_case"] = True
            carried_problem_records.append(carried)
        problem_records = [*newly_mined_problem_records, *carried_problem_records]
        stage1_doc = dict(stage1_doc)
        stage1_doc["items"] = problem_records
        stage1_meta_raw = stage1_doc.get("input_meta")
        stage1_meta = dict(stage1_meta_raw) if isinstance(stage1_meta_raw, dict) else {}
        stage1_meta.update(
            {
                "newly_mined_case_count": len(newly_mined_problem_records),
                "carried_forward_active_case_count": len(carried_problem_records),
            }
        )
        stage1_doc["input_meta"] = stage1_meta

        stage1_doc, problem_records, atoms, case_registry = _run_problem_case_relation_review(
            stage_doc=stage1_doc,
            problem_records=problem_records,
            atoms=atoms,
            pipeline_manifest=pipeline_manifest,
            artifacts_dir=artifacts_dir,
            out_json=problem_records_json,
            out_md=problem_records_md,
            case_registry_path=case_registry_json,
            previous_case_registry=case_registry,
            agent=agent,
            model=model,
            cfg=cfg,
            dry_run=dry_run,
            stage_guidance_text=stage1_guidance,
        )
        _require_stage_model_invocation_provenance(stage1_doc)
        atoms_doc["atoms"] = atoms
        atoms_doc["atom_dispositions"] = atom_disposition_summary(atoms)
        write_backlog_atoms(atoms_doc, atoms_jsonl)
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=stage1_doc,
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
        _require_stage_model_invocation_provenance(stage2_doc)

        items2_raw = stage2_doc.get("items") if isinstance(stage2_doc, dict) else None
        priority_decisions = (
            [item for item in items2_raw if isinstance(item, dict)]
            if isinstance(items2_raw, list)
            else []
        )
        stage2_doc, priority_decisions = _persist_downstream_case_lineage(
            stage_doc=stage2_doc,
            out_json=prioritized_json,
            problem_cases=problem_records,
        )
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=stage2_doc,
        )
        selected_priority = [
            dec for dec in priority_decisions if dec.get("selected_for_research") is True
        ]

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
                    resolved_path = (
                        _resolve_local_repo_root(repo_root, resolved) or Path(resolved).expanduser()
                    )
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
        if selected_priority and not research_ref and not dry_run:
            print(
                "[stage3] Missing source-of-truth research ref. Pass --research-ref or "
                "configure backlog_research.source_ref.",
                file=sys.stderr,
            )
            return 2

        if dry_run or not selected_priority:
            # Stages 1-2 and empty/dry-run stage 3 do not execute experiments.
            # Do not reject those useful mining runs merely because a
            # repository-backed replay boundary is not needed yet.  A real
            # selected research case still fails above without a repository.
            replay_executor = BlockedReplayExecutor(reason="stage3_repository_not_required")
            replay_executor_metadata = {
                "executor": "blocked",
                "reason": "stage3_repository_not_required",
            }
        else:
            try:
                replay_executor, replay_executor_metadata = _configured_replay_executor(
                    research_config=research_config,
                    repo_root=repo_root,
                    repo_input=resolved_repo_input,
                )
            except ValueError as exc:
                print(
                    f"Invalid backlog research replay config: {exc}",
                    file=sys.stderr,
                )
                return 2

        stage3_doc = _run_repro_research_stage(
            repo_root=repo_root,
            repo_input=resolved_repo_input,
            repo_ref=research_ref,
            target_slug=target_slug,
            selected_priority_decisions=selected_priority,
            problem_records=problem_records,
            atoms=atoms,
            artifacts_dir=artifacts_dir,
            out_json=research_json,
            out_md=research_md,
            agent=agent,
            model=model,
            cfg=cfg,
            dry_run=dry_run,
            replay_timeout_seconds=replay_timeout_seconds,
            replay_executor=replay_executor,
            replay_executor_metadata=replay_executor_metadata,
        )

        items3_raw = stage3_doc.get("items") if isinstance(stage3_doc, dict) else None
        research_dossiers = (
            [item for item in items3_raw if isinstance(item, dict)]
            if isinstance(items3_raw, list)
            else []
        )
        stage3_doc, research_dossiers = _persist_downstream_case_lineage(
            stage_doc=stage3_doc,
            out_json=research_json,
            problem_cases=problem_records,
        )
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=stage3_doc,
        )

        post_research_relations = collapse_post_research_verified_mechanisms(
            problem_records=problem_records,
            priority_decisions=priority_decisions,
            research_dossiers=research_dossiers,
            case_registry=case_registry,
        )
        post_research_groups = post_research_relations["groups"]
        if post_research_groups:
            problem_records = post_research_relations["problem_records"]
            priority_decisions = post_research_relations["priority_decisions"]
            research_dossiers = post_research_relations["research_dossiers"]
            case_registry = build_case_registry(
                problem_records,
                previous=case_registry,
                supporting_atoms=atoms,
            )
            relation_dir = (
                artifacts_dir / "repro_research" / "post_research_verified_mechanism_relations_001"
            )
            relation_dir.mkdir(parents=True, exist_ok=True)
            response_path = relation_dir / (
                "post_research_verified_mechanism_relations_001.response.txt"
            )
            response_path.write_text(
                json.dumps(post_research_groups, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            post_research_canonical_case_ids = {
                str(group["canonical_case_id"])
                for group in post_research_groups
                if isinstance(group, dict) and group.get("canonical_case_id")
            }
            _, relation_receipt_path = _persist_canonical_relation_receipts(
                canonical_records=[
                    record
                    for record in problem_records
                    if record.get("case_id") in post_research_canonical_case_ids
                ],
                registry=case_registry,
                review_response_path=response_path,
                receipt_path=relation_dir
                / "post_research_verified_mechanism_relations_001.relations.json",
                stage="repro_research",
            )
            write_case_registry(case_registry_json, case_registry)
            stage3_doc = dict(stage3_doc)
            stage3_doc["items"] = research_dossiers
            stage3_meta_raw = stage3_doc.get("input_meta")
            stage3_meta = dict(stage3_meta_raw) if isinstance(stage3_meta_raw, dict) else {}
            stage3_meta.update(
                {
                    "post_research_relation_review": ("runner_verified_mechanism_identity_v2"),
                    "post_research_relation_groups": post_research_groups,
                    "post_research_case_aliases": post_research_relations["case_aliases"],
                    "post_research_canonical_case_count": len(problem_records),
                    "post_research_canonical_research_count": len(research_dossiers),
                }
            )
            stage3_doc["input_meta"] = stage3_meta
            stage3_artifacts_raw = stage3_doc.get("artifacts")
            stage3_artifacts = (
                dict(stage3_artifacts_raw) if isinstance(stage3_artifacts_raw, dict) else {}
            )
            stage3_artifacts.update(
                {
                    "post_research_relation_response": str(response_path),
                    "post_research_relation_receipt": str(relation_receipt_path),
                }
            )
            stage3_doc["artifacts"] = stage3_artifacts
            research_json.write_text(
                json.dumps(stage3_doc, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            case_registry = _persist_case_registry_stage_lineage(
                case_registry=case_registry,
                case_registry_path=case_registry_json,
                stage_doc=stage3_doc,
            )

        target_repo_roots_by_problem: dict[str, Path] = {}
        for dossier in research_dossiers:
            research_ready, _research_blockers = assess_research_readiness(dossier)
            if not research_ready:
                continue
            receipt_ready, receipt_blockers = verify_persisted_research_evidence(dossier)
            if not receipt_ready:
                raise ValueError(
                    "planning_research_receipt_invalid: "
                    f"problem_id={dossier.get('problem_id')!r} "
                    f"reasons={','.join(receipt_blockers)}"
                )
            pid = _coerce_string(dossier.get("problem_id"))
            workspace_raw = _coerce_string(dossier.get("repo_workspace"))
            research_revision = _coerce_string(dossier.get("repo_revision"))
            verification_raw = dossier.get("evidence_verification")
            verification = verification_raw if isinstance(verification_raw, dict) else {}
            attested_workspace = _coerce_string(verification.get("planning_workspace_dir"))
            attested_head = _coerce_string(verification.get("planning_workspace_head"))
            if (
                pid is None
                or workspace_raw is None
                or research_revision is None
                or verification.get("status") != "verified"
                or verification.get("planning_workspace_clean") is not True
                or attested_workspace != workspace_raw
                or attested_head != research_revision
            ):
                raise ValueError(
                    "planning_target_workspace_unverified: "
                    f"problem_id={pid!r} workspace={workspace_raw!r} "
                    f"revision={research_revision!r}"
                )
            workspace = Path(workspace_raw).expanduser().resolve()
            if not workspace.is_dir():
                raise ValueError(
                    "planning_target_workspace_missing: "
                    f"problem_id={pid!r} workspace={str(workspace)!r}"
                )
            target_repo_roots_by_problem[pid] = workspace

        stage4_guidance = pipeline_manifest.load_stage_guidance("solution_optioning")
        stage4_doc = _run_solution_optioning_stage(
            repo_root=repo_root,
            target_repo_roots_by_problem=target_repo_roots_by_problem,
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
        _require_stage_model_invocation_provenance(stage4_doc)

        items4_raw = stage4_doc.get("items") if isinstance(stage4_doc, dict) else None
        solution_options = (
            [item for item in items4_raw if isinstance(item, dict)]
            if isinstance(items4_raw, list)
            else []
        )
        stage4_doc, solution_options = _persist_downstream_case_lineage(
            stage_doc=stage4_doc,
            out_json=solution_options_json,
            problem_cases=problem_records,
        )
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=stage4_doc,
        )

        stage5_guidance = pipeline_manifest.load_stage_guidance("solution_selection")
        stage5_doc = _run_solution_selection_stage(
            repo_root=repo_root,
            target_repo_roots_by_problem=target_repo_roots_by_problem,
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
        _require_stage_model_invocation_provenance(stage5_doc)

        items5_raw = stage5_doc.get("items") if isinstance(stage5_doc, dict) else None
        selection_decisions = (
            [item for item in items5_raw if isinstance(item, dict)]
            if isinstance(items5_raw, list)
            else []
        )
        stage5_doc, selection_decisions = _persist_downstream_case_lineage(
            stage_doc=stage5_doc,
            out_json=solution_selection_json,
            problem_cases=problem_records,
        )
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=stage5_doc,
        )

        stage6_guidance = pipeline_manifest.load_stage_guidance("implementation_planning")
        stage6_doc = _run_implementation_planning_stage(
            repo_root=repo_root,
            target_repo_roots_by_problem=target_repo_roots_by_problem,
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
        _require_stage_model_invocation_provenance(stage6_doc)

        items6_raw = stage6_doc.get("items") if isinstance(stage6_doc, dict) else None
        change_plans = (
            [item for item in items6_raw if isinstance(item, dict)]
            if isinstance(items6_raw, list)
            else []
        )
        stage6_doc, change_plans = _persist_downstream_case_lineage(
            stage_doc=stage6_doc,
            out_json=change_plans_json,
            problem_cases=problem_records,
        )
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=stage6_doc,
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
            "implementation_runs_root": str(implementation_runs_root),
            "primary_record_count": len(records),
            "derived_record_count": derived_evidence_meta["records_ingested"],
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
            "derived_evidence_ingestion": derived_evidence_meta,
            "pipeline_manifest_path": str(pipeline_manifest_path),
            "pipeline_manifest_version": int(getattr(pipeline_manifest, "version", 2)),
            "breadth_profile_warnings": breadth_profile_warnings,
        },
        artifacts={
            "atoms_jsonl": str(atoms_jsonl),
            "atoms_agent_last_message_artifact_jsonl": str(agent_last_message_atoms_jsonl),
            "artifacts_dir": str(artifacts_dir),
            "case_registry_json": str(case_registry_json),
            "prompts_dir": str(prompts_dir),
            "breadth_profile": breadth_profile,
            "batch_breadth": pipeline_batch_breadth,
            "atom_filter": {
                **(atoms_doc.get("atom_filter") or {}),
                "dropped_tickets_excluded_atoms": dropped_tickets_excluded_atoms,
            },
            "six_stage_pipeline": {
                "problem_records_json": str(problem_records_json),
                "problem_mining_evidence_json": str(problem_mining_evidence_json),
                "prioritized_problems_json": str(prioritized_json),
                "research_json": str(research_json),
                "solution_options_json": str(solution_options_json),
                "solution_selection_json": str(solution_selection_json),
                "change_plans_json": str(change_plans_json),
                "case_registry_json": str(case_registry_json),
            },
        },
        miners_meta={},
    )
    summary["scope"] = {
        "target": target_slug,
        "repo_input": repo_input,
    }

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
    try:
        ticket_lineage_doc = _ticket_lineage_stage_document(
            tickets=tickets_for_atoms,
            problem_cases=problem_records,
            generated_at=generated_at,
            backlog_json_path=out_json,
            backlog_md_path=out_md,
        )
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=ticket_lineage_doc,
        )
    except (OSError, ValueError) as exc:
        print(
            f"[backlog] ERROR: failed to persist ticket case lineage: {exc}",
            file=sys.stderr,
        )
        return 2
    atom_status_meta = _update_atom_actions_from_backlog(
        atom_actions=atom_actions,
        atoms=atoms,
        tickets=tickets_for_atoms,
        generated_at=generated_at,
        backlog_json_path=out_json,
    )
    if not shadow:
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

    export_projection: dict[str, Any] | None = None
    if shadow:
        assert policy_cfg is not None
        assert policy_config_path is not None
        export_projection = _build_export_projection(
            backlog=summary,
            surface_area_high=set(policy_cfg.surface_area_high),
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )
        export_contract_raw = artifacts_dict.get("export_contract")
        export_contract = dict(export_contract_raw) if isinstance(export_contract_raw, dict) else {}
        export_contract.update(
            {
                "schema_version": 1,
                "projection_sha256": export_projection["sha256"],
                "policy_config_path": str(policy_config_path.resolve()),
                "ux_review_json_path": str(_ux_review_path_for_backlog(out_json).resolve()),
            }
        )
        artifacts_dict["export_contract"] = export_contract
        summary["artifacts"] = artifacts_dict

    write_backlog(
        summary,
        out_json_path=out_json,
        out_md_path=out_md,
        title=f"Usertest Backlog{title_suffix}",
    )

    shadow_state: dict[str, Any] | None = None
    if shadow:
        invariant_report = evaluate_shadow_invariants(
            backlog=summary,
            atoms=atoms,
            stage1=stage1_doc,
            stage2=stage2_doc,
            stage3=stage3_doc,
            stage4=stage4_doc,
            stage5=stage5_doc,
            stage6=stage6_doc,
            case_registry=case_registry,
            trusted_runs_roots=outcome_trusted_runs_roots,
            owner_roots=tuple(owner_roots),
            qualification_contract=shadow_gate_config,
        )
        assert export_projection is not None
        assert policy_config_path is not None
        invariant_report["export_projection_sha256"] = export_projection["sha256"]
        export_artifact_paths = _export_artifact_paths(
            backlog=summary,
            backlog_path=out_json,
            repo_root=repo_root,
            policy_config_path=policy_config_path,
            export_gate_config_path=repo_root / "configs" / "backlog_export_gate.yaml",
            cli_repo_input=repo_input,
        )
        shadow_state = record_shadow_cycle(
            state_path=shadow_state_path(out_json),
            backlog_path=out_json,
            invariant_report=invariant_report,
            artifact_paths=export_artifact_paths,
            generated_at=generated_at,
            required_consecutive_cycles=shadow_gate_config["required_consecutive_shadow_cycles"],
            require_exact_export_projection=shadow_gate_config["require_exact_export_projection"],
        )
        print(str(shadow_state_path(out_json)))
        print(
            json.dumps(
                {
                    "shadow_invariants_passed": invariant_report["passed"],
                    "ready_for_export": shadow_state["ready_for_export"],
                    "failures": invariant_report["failures"],
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    print(str(out_json))
    print(str(out_md))
    print(str(atoms_jsonl))
    print(str(agent_last_message_atoms_jsonl))
    print(json.dumps(summary.get("totals", {}), indent=2, ensure_ascii=False))
    print(json.dumps(summary.get("coverage", {}), indent=2, ensure_ascii=False))

    if shadow_state is not None and not shadow_state["cycles"][-1]["passed"]:
        return 3
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
