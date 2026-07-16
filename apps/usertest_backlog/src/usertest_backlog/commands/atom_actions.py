# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.shared import *


def _backfill_failure_event_atoms_from_legacy_entries(
    *,
    atom_actions: dict[str, dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    """
    Ensure canonical `:run_failure_event:1` atoms exist and inherit lifecycle state.

    This is intentionally idempotent and only promotes (never demotes).
    """

    mapped = 0
    created = 0
    promoted = 0

    # Iterate over a snapshot since we may insert new canonical keys.
    for legacy_atom_id, legacy_entry in list(atom_actions.items()):
        canonical = _canonicalize_failure_atom_id(legacy_atom_id)
        if canonical is None or canonical == legacy_atom_id:
            continue
        mapped += 1

        legacy_status = _normalize_atom_status(_coerce_string(legacy_entry.get("status")))

        existing = atom_actions.get(canonical)
        if existing is None:
            existing = {
                "atom_id": canonical,
                "status": legacy_status,
                "first_seen_at": _coerce_string(legacy_entry.get("first_seen_at")) or generated_at,
            }
            atom_actions[canonical] = existing
            created += 1

        old_status = _normalize_atom_status(_coerce_string(existing.get("status")))
        new_status = _promote_atom_status(old_status, legacy_status)
        if _ATOM_STATUS_ORDER[new_status] > _ATOM_STATUS_ORDER[old_status]:
            promoted += 1
        existing["status"] = new_status
        existing["last_seen_at"] = generated_at

        for list_key in ("queue_paths", "queue_owner_roots", "fingerprints"):
            values: list[str] = []
            values.extend([item for item in existing.get(list_key, []) if isinstance(item, str)])
            values.extend(
                [item for item in legacy_entry.get(list_key, []) if isinstance(item, str)]
            )
            existing[list_key] = _sorted_unique_strings(values)

        derived = [
            item for item in existing.get("derived_from_atom_ids", []) if isinstance(item, str)
        ]
        derived.append(legacy_atom_id)
        existing["derived_from_atom_ids"] = _sorted_unique_strings(derived)

        atom_actions[canonical] = existing

    return {
        "legacy_atoms_mapped": mapped,
        "canonical_atoms_created": created,
        "canonical_atoms_promoted": promoted,
    }


def _update_atom_actions_from_backlog(
    *,
    atom_actions: dict[str, dict[str, Any]],
    atoms: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    generated_at: str,
    backlog_json_path: Path,
) -> dict[str, Any]:
    """
    Update atom lifecycle status during backlog generation.

    - atom in an exportable research/implementation ticket -> at least `ticketed`
    - atom cited only by blocked or triage output -> at least `new`
    """

    fingerprints_by_atom: dict[str, set[str]] = {}
    for ticket in tickets:
        stage = (_coerce_string(ticket.get("stage")) or "triage").strip().lower()
        if stage not in {"ready_for_ticket", "research_required"}:
            # Blocked and triage records are not ticket outcomes. Their evidence
            # must remain eligible so later runs can accumulate enough proof.
            continue
        fingerprint = ticket_export_fingerprint(ticket)
        for atom_id in _coerce_string_list(ticket.get("evidence_atom_ids")):
            bucket = fingerprints_by_atom.setdefault(atom_id, set())
            bucket.add(fingerprint)

    created = 0
    promoted = 0
    observed = 0
    ticketed_now = 0
    new_now = 0

    for atom in atoms:
        atom_id = _coerce_string(atom.get("atom_id"))
        if atom_id is None:
            continue
        if atom_id.startswith("__aggregate__/"):
            # Synthetic aggregates are regenerated every time and should not be tracked
            # in the lifecycle ledger.
            continue
        observed += 1
        desired = "ticketed" if atom_id in fingerprints_by_atom else "new"
        if desired == "ticketed":
            ticketed_now += 1
        else:
            new_now += 1

        existing = atom_actions.get(atom_id)
        if existing is None:
            existing = {"atom_id": atom_id, "status": desired, "first_seen_at": generated_at}
            atom_actions[atom_id] = existing
            created += 1
        old_status = _normalize_atom_status(_coerce_string(existing.get("status")))
        new_status = _promote_atom_status(old_status, desired)
        if _ATOM_STATUS_ORDER[new_status] > _ATOM_STATUS_ORDER[old_status]:
            promoted += 1
        existing["status"] = new_status
        existing["last_backlog_status"] = desired
        existing["last_seen_at"] = generated_at
        existing["last_backlog_generated_at"] = generated_at
        existing["last_backlog_json"] = str(backlog_json_path)
        existing["source"] = _coerce_string(atom.get("source")) or existing.get("source")
        existing["severity_hint"] = _coerce_string(atom.get("severity_hint")) or existing.get(
            "severity_hint"
        )
        existing["run_rel"] = _coerce_string(atom.get("run_rel")) or existing.get("run_rel")
        existing["agent"] = _coerce_string(atom.get("agent")) or existing.get("agent")
        existing["mission_id"] = _coerce_string(atom.get("mission_id")) or existing.get(
            "mission_id"
        )
        existing["persona_id"] = _coerce_string(atom.get("persona_id")) or existing.get(
            "persona_id"
        )
        existing["target_slug"] = _coerce_string(atom.get("target_slug")) or existing.get(
            "target_slug"
        )
        existing["repo_input"] = _coerce_string(atom.get("repo_input")) or existing.get(
            "repo_input"
        )
        fingerprints_existing = [
            item for item in existing.get("fingerprints", []) if isinstance(item, str)
        ]
        if atom_id in fingerprints_by_atom:
            fingerprints_existing.extend(sorted(fingerprints_by_atom[atom_id]))
        existing["fingerprints"] = _sorted_unique_strings(fingerprints_existing)
        existing.pop("ticket_ids", None)
        atom_actions[atom_id] = existing

    status_counts: dict[str, int] = {}
    for entry in atom_actions.values():
        status = _normalize_atom_status(_coerce_string(entry.get("status")))
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "observed_atoms": observed,
        "current_new_atoms": new_now,
        "current_ticketed_atoms": ticketed_now,
        "created_entries": created,
        "promoted_entries": promoted,
        "ledger_atoms_total": len(atom_actions),
        "status_counts": status_counts,
    }


def _update_atom_actions_from_exports(
    *,
    atom_actions: dict[str, dict[str, Any]],
    queued_refs: list[dict[str, str]],
    generated_at: str,
    export_json_path: Path,
) -> dict[str, Any]:
    """
    Update atom lifecycle status during ticket export.

    - atom referenced by an exported ticket -> at least `queued`
    - atom referenced by a deduped existing plan ticket -> `queued` or `actioned` (from plan bucket)
    """

    touched_atoms: set[str] = set()
    promoted = 0
    created = 0

    for ref in queued_refs:
        atom_id_raw = _coerce_string(ref.get("atom_id"))
        if atom_id_raw is None:
            continue
        if atom_id_raw.startswith("__aggregate__/"):
            continue
        derived_from_atom_id: str | None = None
        atom_id = atom_id_raw
        canonical_atom_id = _canonicalize_failure_atom_id(atom_id_raw)
        if canonical_atom_id is not None and canonical_atom_id != atom_id_raw:
            derived_from_atom_id = atom_id_raw
            atom_id = canonical_atom_id
        desired_status = _normalize_atom_status(_coerce_string(ref.get("desired_status")))
        if desired_status not in ("queued", "actioned"):
            desired_status = "queued"
        touched_atoms.add(atom_id)
        existing = atom_actions.get(atom_id)
        if existing is None:
            existing = {"atom_id": atom_id, "status": desired_status, "first_seen_at": generated_at}
            atom_actions[atom_id] = existing
            created += 1

        old_status = _normalize_atom_status(_coerce_string(existing.get("status")))
        new_status = _promote_atom_status(old_status, desired_status)
        if _ATOM_STATUS_ORDER[new_status] > _ATOM_STATUS_ORDER[old_status]:
            promoted += 1
        existing["status"] = new_status
        existing["last_queue_status"] = desired_status
        existing["last_seen_at"] = generated_at
        existing["last_queue_at"] = generated_at
        existing["last_export_json"] = str(export_json_path)

        fingerprints = [item for item in existing.get("fingerprints", []) if isinstance(item, str)]
        fingerprint = _coerce_string(ref.get("fingerprint"))
        if fingerprint is not None:
            fingerprints.append(fingerprint)
        existing["fingerprints"] = _sorted_unique_strings(fingerprints)
        existing.pop("ticket_ids", None)

        queue_paths = [item for item in existing.get("queue_paths", []) if isinstance(item, str)]
        idea_path = _coerce_string(ref.get("idea_path"))
        if idea_path is not None:
            queue_paths.append(idea_path)
        existing["queue_paths"] = _sorted_unique_strings(queue_paths)

        queue_roots = [
            item for item in existing.get("queue_owner_roots", []) if isinstance(item, str)
        ]
        owner_root = _coerce_string(ref.get("owner_root"))
        if owner_root is not None:
            queue_roots.append(owner_root)
        existing["queue_owner_roots"] = _sorted_unique_strings(queue_roots)

        if derived_from_atom_id is not None:
            derived = [
                item for item in existing.get("derived_from_atom_ids", []) if isinstance(item, str)
            ]
            derived.append(derived_from_atom_id)
            existing["derived_from_atom_ids"] = _sorted_unique_strings(derived)

        atom_actions[atom_id] = existing

    status_counts: dict[str, int] = {}
    for entry in atom_actions.values():
        status = _normalize_atom_status(_coerce_string(entry.get("status")))
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "queued_atoms_touched": len(touched_atoms),
        "created_entries": created,
        "promoted_entries": promoted,
        "ledger_atoms_total": len(atom_actions),
        "status_counts": status_counts,
    }


def _atom_status_counts(atom_actions: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in atom_actions.values():
        status = _normalize_atom_status(_coerce_string(entry.get("status")))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _cmd_reports_sync_atom_actions(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    atom_actions_arg: Path | None = args.atom_actions_yaml
    if atom_actions_arg is not None:
        atom_actions_path = (
            _resolve_optional_path(repo_root, atom_actions_arg) or atom_actions_arg.resolve()
        )
    else:
        atom_actions_path = repo_root / "configs" / "backlog_atom_actions.yaml"

    try:
        atom_actions = _load_atom_actions_yaml(atom_actions_path)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    owner_roots_raw = list(args.owner_root or [Path.cwd()])
    owner_roots: list[Path] = []
    for owner_root_raw in owner_roots_raw:
        resolved = _resolve_optional_path(repo_root, owner_root_raw) or owner_root_raw.resolve()
        owner_roots.append(resolved)
    owner_roots = sorted({path.resolve() for path in owner_roots}, key=lambda p: str(p))

    working = copy.deepcopy(atom_actions)
    before_counts = _atom_status_counts(working)
    sync_meta = _reconcile_atom_actions_from_plan_folders(
        atom_actions=working,
        owner_roots=owner_roots,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    after_counts = _atom_status_counts(working)

    if not bool(args.dry_run):
        _write_atom_actions_yaml(atom_actions_path, working)

    payload = {
        "schema_version": 1,
        "dry_run": bool(args.dry_run),
        "atom_actions_yaml": str(atom_actions_path),
        "owner_roots": [str(path) for path in owner_roots],
        "before_status_counts": before_counts,
        "after_status_counts": after_counts,
        "sync": sync_meta,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
