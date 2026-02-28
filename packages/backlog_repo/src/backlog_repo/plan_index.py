from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from backlog_repo.actions import (
    canonicalize_failure_atom_id,
    promote_atom_status,
    sorted_unique_strings,
)

PLAN_BUCKET_TO_ATOM_STATUS: dict[str, str] = {
    "0.5 - to_triage": "queued",
    "1 - ideas": "queued",
    "1.5 - to_plan": "queued",
    "2 - ready": "queued",
    "3 - in_progress": "actioned",
    "4 - for_review": "actioned",
    "5 - complete": "actioned",
    "6 - archived": "actioned",
    "0.1 - deferred": "actioned",
}

ACTIONED_PLAN_BUCKET_PRIORITY: list[str] = [
    "6 - archived",
    "5 - complete",
    "4 - for_review",
    "3 - in_progress",
    "0.1 - deferred",
]
_ACTIONED_BUCKET_RANK: dict[str, int] = {
    bucket: rank for rank, bucket in enumerate(reversed(ACTIONED_PLAN_BUCKET_PRIORITY), start=1)
}
PLAN_TICKET_FILENAME_RE = re.compile(
    r"^(?P<date>[0-9]{8})_(?P<ticket_id>BLG-[0-9]{3})_(?P<fingerprint>[0-9a-f]{16})_.+\.md$"
)
ATOM_ID_RE = re.compile(
    r"^[A-Za-z0-9_.-]+/[0-9]{8}T[0-9]{6}Z/[A-Za-z0-9_.-]+/[0-9]+:[A-Za-z0-9_.-]+:[0-9]+$"
)
DEQUEUED_PLAN_DIRNAMES: tuple[str, ...] = ("_dequeued", "_archive")


def _extract_atom_ids_from_ticket_markdown(markdown: str) -> list[str]:
    """Extract atom identifiers from backtick-wrapped markdown tokens.

    Parameters
    ----------
    markdown:
        Ticket markdown content.

    Returns
    -------
    list[str]
        Sorted unique atom IDs matching the repository atom-ID pattern.
    """

    candidates = re.findall(r"`([^`]+)`", markdown)
    atom_ids: set[str] = set()
    for candidate in candidates:
        cleaned = candidate.strip()
        if ATOM_ID_RE.match(cleaned):
            atom_ids.add(cleaned)
    return sorted(atom_ids)


def sync_atom_actions_from_dequeued_plan_folders(
    *,
    atom_actions: dict[str, dict[str, Any]],
    owner_roots: list[Path],
    generated_at: str,
) -> dict[str, Any]:
    """Demote queued/ticketed atom ledger entries based on `_dequeued` plan files.

    Plans moved under `.agents/plans/_dequeued/**` (or `.agents/plans/_archive/**`) are
    treated as explicitly removed from the active queue. Any referenced atoms are
    demoted back to `new` so they become eligible for re-mining, while `actioned`
    atoms are never demoted.

    This is intended to run *before* `sync_atom_actions_from_plan_folders()` so that
    any atoms still referenced by active queued/actioned plan buckets are promoted
    back immediately.
    """

    roots_scanned = 0
    dequeued_dirs_scanned = 0
    ticket_files_scanned = 0
    tickets_without_evidence = 0
    atom_ids_seen = 0
    atoms_missing = 0
    atoms_skipped_actioned = 0
    atoms_demoted = 0

    for owner_root in owner_roots:
        plans_dir = owner_root / ".agents" / "plans"
        if not plans_dir.exists() or not plans_dir.is_dir():
            continue
        roots_scanned += 1

        dequeued_dirs: list[Path] = []
        for dirname in DEQUEUED_PLAN_DIRNAMES:
            candidate = plans_dir / dirname
            if candidate.exists() and candidate.is_dir():
                dequeued_dirs.append(candidate)
        if not dequeued_dirs:
            continue
        dequeued_dirs_scanned += len(dequeued_dirs)

        for dequeued_dir in dequeued_dirs:
            for md_path in sorted(dequeued_dir.rglob("*.md"), key=lambda p: str(p)):
                ticket_files_scanned += 1

                try:
                    markdown = md_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                atom_ids = _extract_atom_ids_from_ticket_markdown(markdown)
                if not atom_ids:
                    tickets_without_evidence += 1
                    continue
                atom_ids_seen += len(atom_ids)

                for atom_id in atom_ids:
                    canonical_atom_id = canonicalize_failure_atom_id(atom_id)
                    derived_from_atom_id: str | None = None
                    if canonical_atom_id is not None and canonical_atom_id != atom_id:
                        derived_from_atom_id = atom_id
                        atom_id = canonical_atom_id

                    existing = atom_actions.get(atom_id)
                    if existing is None:
                        atoms_missing += 1
                        continue

                    old_status_raw = existing.get("status")
                    old_status = str(old_status_raw) if isinstance(old_status_raw, str) else None
                    old_status_n = old_status.strip().lower() if old_status else "new"
                    if old_status_n == "actioned":
                        atoms_skipped_actioned += 1
                        continue

                    if old_status_n != "new":
                        atoms_demoted += 1
                    existing["status"] = "new"
                    existing["last_dequeued_at"] = generated_at

                    dequeued_paths = [
                        item for item in existing.get("dequeued_paths", []) if isinstance(item, str)
                    ]
                    dequeued_paths.append(str(md_path))
                    existing["dequeued_paths"] = sorted_unique_strings(dequeued_paths)

                    dequeued_roots = [
                        item
                        for item in existing.get("dequeued_owner_roots", [])
                        if isinstance(item, str)
                    ]
                    dequeued_roots.append(str(owner_root))
                    existing["dequeued_owner_roots"] = sorted_unique_strings(dequeued_roots)

                    if derived_from_atom_id is not None:
                        derived = [
                            item
                            for item in existing.get("derived_from_atom_ids", [])
                            if isinstance(item, str)
                        ]
                        derived.append(derived_from_atom_id)
                        existing["derived_from_atom_ids"] = sorted_unique_strings(derived)

                    atom_actions[atom_id] = existing

    return {
        "roots_scanned": roots_scanned,
        "dequeued_dirs_scanned": dequeued_dirs_scanned,
        "ticket_files_scanned": ticket_files_scanned,
        "tickets_without_evidence": tickets_without_evidence,
        "atom_ids_seen": atom_ids_seen,
        "atoms_missing": atoms_missing,
        "atoms_skipped_actioned": atoms_skipped_actioned,
        "atoms_demoted": atoms_demoted,
    }


def scan_plan_ticket_index(*, owner_root: Path) -> dict[str, dict[str, Any]]:
    """Build fingerprint-to-plan index from `.agents/plans` folders.

    Parameters
    ----------
    owner_root:
        Repository root containing `.agents/plans`.

    Returns
    -------
    dict[str, dict[str, Any]]
        Mapping keyed by fingerprint with merged status, paths, bucket names, and ticket IDs.
    """

    plans_dir = owner_root / ".agents" / "plans"
    if not plans_dir.exists() or not plans_dir.is_dir():
        return {}

    index: dict[str, dict[str, Any]] = {}
    for bucket_dir in sorted([p for p in plans_dir.iterdir() if p.is_dir()], key=lambda p: p.name):
        desired_status = PLAN_BUCKET_TO_ATOM_STATUS.get(bucket_dir.name)
        if desired_status is None:
            continue

        for md_path in sorted(bucket_dir.glob("*.md"), key=lambda p: p.name):
            match = PLAN_TICKET_FILENAME_RE.match(md_path.name)
            if match is None:
                continue
            fingerprint = match.group("fingerprint")
            ticket_id = match.group("ticket_id")

            meta = index.get(fingerprint)
            if meta is None:
                meta = {"status": desired_status, "paths": [], "buckets": [], "ticket_ids": []}
                index[fingerprint] = meta

            status_value = meta.get("status")
            status_current = str(status_value) if isinstance(status_value, str) else None
            meta["status"] = promote_atom_status(status_current, desired_status)

            paths = [item for item in meta.get("paths", []) if isinstance(item, str)]
            paths.append(str(md_path))
            meta["paths"] = sorted_unique_strings(paths)

            buckets = [item for item in meta.get("buckets", []) if isinstance(item, str)]
            buckets.append(bucket_dir.name)
            meta["buckets"] = sorted_unique_strings(buckets)

            ticket_ids = [item for item in meta.get("ticket_ids", []) if isinstance(item, str)]
            ticket_ids.append(ticket_id)
            meta["ticket_ids"] = sorted_unique_strings(ticket_ids)

            index[fingerprint] = meta

    return index


def dedupe_actioned_plan_ticket_files(*, owner_root: Path) -> int:
    """Remove stale duplicates across actioned plan buckets.

    When the same fingerprint exists in multiple actioned buckets (e.g.
    `3 - in_progress` and `5 - complete`), keep only the most-advanced bucket's
    files and delete the lower-bucket copies.

    Returns
    -------
    int
        Number of files removed.
    """

    plans_dir = owner_root / ".agents" / "plans"
    if not plans_dir.exists() or not plans_dir.is_dir():
        return 0

    removed = 0
    index = scan_plan_ticket_index(owner_root=owner_root)
    for meta in index.values():
        if not isinstance(meta, dict):
            continue
        if meta.get("status") != "actioned":
            continue
        paths_raw = meta.get("paths", [])
        paths = [Path(p) for p in paths_raw if isinstance(p, str) and p]

        actioned_buckets: set[str] = set()
        by_bucket: dict[str, list[Path]] = {}
        for path in paths:
            bucket = path.parent.name
            if bucket not in _ACTIONED_BUCKET_RANK:
                continue
            actioned_buckets.add(bucket)
            by_bucket.setdefault(bucket, []).append(path)

        if len(actioned_buckets) <= 1:
            continue

        keep_bucket = max(actioned_buckets, key=lambda b: _ACTIONED_BUCKET_RANK.get(b, 0))
        for bucket, bucket_paths in by_bucket.items():
            if bucket == keep_bucket:
                continue
            for path in bucket_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                removed += 1
    return removed


def dedupe_queued_plan_ticket_files_when_actioned_exists(*, owner_root: Path) -> int:
    """Remove queued-bucket plan files for fingerprints already marked actioned.

    This is a best-effort hygiene sweep to eliminate stale duplicates that can
    linger across runs even when the current backlog no longer contains that
    fingerprint.

    Returns
    -------
    int
        Number of files removed.
    """

    plans_dir = owner_root / ".agents" / "plans"
    if not plans_dir.exists() or not plans_dir.is_dir():
        return 0

    queued_buckets = {
        bucket
        for bucket, desired_status in PLAN_BUCKET_TO_ATOM_STATUS.items()
        if desired_status == "queued"
    }
    if not queued_buckets:
        return 0

    removed = 0
    index = scan_plan_ticket_index(owner_root=owner_root)
    for meta in index.values():
        if not isinstance(meta, dict):
            continue
        if meta.get("status") != "actioned":
            continue

        paths_raw = meta.get("paths", [])
        paths = [Path(p) for p in paths_raw if isinstance(p, str) and p]
        for path in paths:
            if path.parent.name not in queued_buckets:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            removed += 1
    return removed


def sync_atom_actions_from_plan_folders(
    *,
    atom_actions: dict[str, dict[str, Any]],
    owner_roots: list[Path],
    generated_at: str,
) -> dict[str, Any]:
    """Synchronize atom action ledger entries from queued/completed plan files.

    Parameters
    ----------
    atom_actions:
        Mutable atom-action map keyed by atom ID.
    owner_roots:
        Candidate repository roots to scan for `.agents/plans`.
    generated_at:
        Timestamp persisted as `last_seen_at` metadata.

    Returns
    -------
    dict[str, Any]
        Summary counters describing scan coverage and mutation counts.
    """

    roots_scanned = 0
    buckets_scanned = 0
    ticket_files_scanned = 0
    tickets_without_evidence = 0
    atom_ids_seen = 0
    atoms_created = 0
    atoms_promoted = 0

    for owner_root in owner_roots:
        plans_dir = owner_root / ".agents" / "plans"
        if not plans_dir.exists() or not plans_dir.is_dir():
            continue
        roots_scanned += 1

        bucket_dirs = sorted(
            [p for p in plans_dir.iterdir() if p.is_dir()],
            key=lambda p: p.name,
        )
        for bucket_dir in bucket_dirs:
            desired_status = PLAN_BUCKET_TO_ATOM_STATUS.get(bucket_dir.name)
            if desired_status is None:
                continue
            buckets_scanned += 1

            for md_path in sorted(bucket_dir.glob("*.md"), key=lambda p: p.name):
                match = PLAN_TICKET_FILENAME_RE.match(md_path.name)
                if match is None:
                    continue

                ticket_files_scanned += 1
                ticket_id = match.group("ticket_id")
                fingerprint = match.group("fingerprint")

                try:
                    markdown = md_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                atom_ids = _extract_atom_ids_from_ticket_markdown(markdown)
                if not atom_ids:
                    tickets_without_evidence += 1
                    continue
                atom_ids_seen += len(atom_ids)

                for atom_id in atom_ids:
                    derived_from_atom_id: str | None = None
                    canonical_atom_id = canonicalize_failure_atom_id(atom_id)
                    if canonical_atom_id is not None and canonical_atom_id != atom_id:
                        derived_from_atom_id = atom_id
                        atom_id = canonical_atom_id

                    existing = atom_actions.get(atom_id)
                    if existing is None:
                        existing = {
                            "atom_id": atom_id,
                            "status": desired_status,
                            "first_seen_at": generated_at,
                        }
                        atom_actions[atom_id] = existing
                        atoms_created += 1

                    old_status_raw = existing.get("status")
                    old_status = str(old_status_raw) if isinstance(old_status_raw, str) else None
                    new_status = promote_atom_status(old_status, desired_status)
                    if old_status != new_status:
                        atoms_promoted += 1
                    existing["status"] = new_status

                    existing["last_seen_at"] = generated_at
                    existing["last_plan_bucket"] = bucket_dir.name
                    existing["last_plan_seen_at"] = generated_at

                    ticket_ids = [
                        item for item in existing.get("ticket_ids", []) if isinstance(item, str)
                    ]
                    ticket_ids.append(ticket_id)
                    existing["ticket_ids"] = sorted_unique_strings(ticket_ids)

                    queue_paths = [
                        item for item in existing.get("queue_paths", []) if isinstance(item, str)
                    ]
                    queue_paths.append(str(md_path))
                    existing["queue_paths"] = sorted_unique_strings(queue_paths)

                    queue_roots = [
                        item
                        for item in existing.get("queue_owner_roots", [])
                        if isinstance(item, str)
                    ]
                    queue_roots.append(str(owner_root))
                    existing["queue_owner_roots"] = sorted_unique_strings(queue_roots)

                    fingerprints = [
                        item for item in existing.get("fingerprints", []) if isinstance(item, str)
                    ]
                    fingerprints.append(fingerprint)
                    existing["fingerprints"] = sorted_unique_strings(fingerprints)

                    if derived_from_atom_id is not None:
                        derived = [
                            item
                            for item in existing.get("derived_from_atom_ids", [])
                            if isinstance(item, str)
                        ]
                        derived.append(derived_from_atom_id)
                        existing["derived_from_atom_ids"] = sorted_unique_strings(derived)

                    atom_actions[atom_id] = existing

    return {
        "roots_scanned": roots_scanned,
        "buckets_scanned": buckets_scanned,
        "ticket_files_scanned": ticket_files_scanned,
        "tickets_without_evidence": tickets_without_evidence,
        "atom_ids_seen": atom_ids_seen,
        "atoms_created": atoms_created,
        "atoms_promoted": atoms_promoted,
    }
