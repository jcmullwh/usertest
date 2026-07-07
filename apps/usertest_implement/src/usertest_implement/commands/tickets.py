# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_implement.commands.run import (
    _default_backlog_runs_dir,
    _refresh_backlog_for_ticket_implementation,
    _resolve_backlog_target,
    _run_selected_ticket,
)
from usertest_implement.selection import _select_ticket_from_owner_root, _select_ticket_from_path
from usertest_implement.shared import *


def _cmd_tickets_list(args: argparse.Namespace) -> int:
    owner_root = args.owner_root.resolve()
    index = build_ticket_index(owner_root=owner_root)
    payload = {
        "schema_version": 1,
        "owner_root": str(owner_root),
        "tickets_total": len(index),
        "tickets": [
            {
                "fingerprint": e.fingerprint,
                "paths": [str(p) for p in e.paths],
                "buckets": e.buckets,
                "status": e.status,
            }
            for e in sorted(index.values(), key=lambda x: x.fingerprint)
        ],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_tickets_next(args: argparse.Namespace) -> int:
    owner_root = args.owner_root.resolve()
    index = build_ticket_index(owner_root=owner_root)
    bucket_priority = list(args.bucket_priority or [])
    if not bucket_priority:
        bucket_priority = ["2 - ready", "1.5 - to_plan", "1 - ideas", "0.5 - to_triage"]
    entry = select_next_ticket(index, bucket_priority=bucket_priority)
    if entry is None:
        print("No tickets found.")
        return 0
    payload = {
        "schema_version": 1,
        "owner_root": str(owner_root),
        "fingerprint": entry.fingerprint,
        "paths": [str(p) for p in entry.paths],
        "buckets": entry.buckets,
        "status": entry.status,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_tickets_run_next(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    owner_root = args.owner_root.resolve()
    if bool(args.refresh_backlog):
        _refresh_backlog_for_ticket_implementation(args=args, repo_root=repo_root)

    index = build_ticket_index(owner_root=owner_root)
    bucket_priority = list(args.bucket_priority or [])
    if not bucket_priority:
        bucket_priority = ["2 - ready", "1.5 - to_plan", "1 - ideas", "0.5 - to_triage"]

    kind_priority = list(args.kind_priority or [])
    if not kind_priority:
        kind_priority = ["implementation"]

    selected = select_next_ticket_path(
        index,
        bucket_priority=bucket_priority,
        kind_priority=kind_priority,
    )
    if selected is None:
        print("No tickets found.")
        return 0

    _, ticket_path = selected
    ticket = _select_ticket_from_path(ticket_path)
    return _run_selected_ticket(args=args, repo_root=repo_root, cfg=cfg, selected=ticket)


def _cmd_tickets_move(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    owner_root = args.owner_root.resolve()
    if str(args.to_bucket) == DISCARDED_PLAN_BUCKET:
        (owner_root / ".agents" / "plans" / DISCARDED_PLAN_BUCKET).mkdir(
            parents=True,
            exist_ok=True,
        )
    dest = move_ticket_file(
        owner_root=owner_root,
        fingerprint=str(args.fingerprint),
        to_bucket=str(args.to_bucket),
        dry_run=bool(args.dry_run),
    )
    if not bool(args.dry_run):
        _sync_ticket_atom_actions(repo_root=repo_root, owner_root=owner_root)
    print(str(dest))
    return 0


def _cmd_tickets_discard(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    owner_root = args.owner_root.resolve()
    fingerprint = str(args.fingerprint).strip().lower()
    discarded_at = _utc_now_z()
    discard_dir = owner_root / ".agents" / "plans" / DISCARDED_PLAN_BUCKET
    discard_dir.mkdir(parents=True, exist_ok=True)

    dest = move_ticket_file(
        owner_root=owner_root,
        fingerprint=fingerprint,
        to_bucket=DISCARDED_PLAN_BUCKET,
        dry_run=False,
    )

    actions_path = args.actions_yaml or _default_backlog_actions_path(repo_root)
    actions = load_backlog_actions_yaml(actions_path)
    entry = dict(actions.get(fingerprint) or {})
    entry.update(
        {
            "fingerprint": fingerprint,
            "status": "discarded",
            "discard_reason": str(args.reason),
            "discarded_at": discarded_at,
            "discarded_path": str(dest),
            "owner_root": str(owner_root),
        }
    )
    if args.note:
        entry["discard_note"] = str(args.note)
    actions[fingerprint] = entry
    write_backlog_actions_yaml(actions_path, actions)

    atom_sync = _sync_ticket_atom_actions(
        repo_root=repo_root,
        owner_root=owner_root,
        atom_actions_path=args.atom_actions_yaml,
        discard_fingerprint=fingerprint,
        discard_reason=str(args.reason),
        discard_note=str(args.note) if args.note else None,
        discarded_path=dest,
        discarded_at=discarded_at,
    )
    payload = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "discard_reason": str(args.reason),
        "discarded_path": str(dest),
        "actions_yaml": str(actions_path),
        "atom_actions_yaml": str(args.atom_actions_yaml or _default_atom_actions_path(repo_root)),
        "atom_sync": atom_sync,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0




__all__ = [name for name in globals() if not name.startswith("__")]
