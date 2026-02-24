from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backlog_repo.plan_index import (
    dedupe_actioned_plan_ticket_files,
    dedupe_queued_plan_ticket_files_when_actioned_exists,
    scan_plan_ticket_index,
)


@dataclass(frozen=True)
class TicketIndexEntry:
    fingerprint: str
    ticket_id: str | None
    paths: list[Path]
    buckets: list[str]
    status: str | None


def build_ticket_index(*, owner_root: Path) -> dict[str, TicketIndexEntry]:
    raw = scan_plan_ticket_index(owner_root=owner_root)
    out: dict[str, TicketIndexEntry] = {}
    for fingerprint, meta in raw.items():
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            continue
        paths_raw = meta.get("paths", [])
        paths = [Path(p) for p in paths_raw if isinstance(p, str) and p.strip()]
        buckets_raw = meta.get("buckets", [])
        buckets = [b for b in buckets_raw if isinstance(b, str) and b.strip()]
        status_raw = meta.get("status")
        status = status_raw if isinstance(status_raw, str) else None
        ticket_ids_raw = meta.get("ticket_ids", [])
        ticket_ids = [t for t in ticket_ids_raw if isinstance(t, str) and t.strip()]
        ticket_id = ticket_ids[0] if ticket_ids else None
        out[fingerprint] = TicketIndexEntry(
            fingerprint=fingerprint,
            ticket_id=ticket_id,
            paths=paths,
            buckets=buckets,
            status=status,
        )
    return out


def select_next_ticket(
    index: dict[str, TicketIndexEntry],
    *,
    bucket_priority: list[str],
) -> TicketIndexEntry | None:
    for bucket in bucket_priority:
        candidates: list[TicketIndexEntry] = []
        for entry in index.values():
            if entry.status == "actioned":
                continue
            if bucket not in entry.buckets:
                continue
            if not entry.paths:
                continue
            candidates.append(entry)
        if not candidates:
            continue
        candidates.sort(key=lambda e: sorted(str(p) for p in e.paths)[0])
        return candidates[0]
    return None


def select_next_ticket_path(
    index: dict[str, TicketIndexEntry],
    *,
    bucket_priority: list[str],
    kind_priority: list[str],
) -> tuple[TicketIndexEntry, Path] | None:
    kind_priority_clean = [
        kind.strip().lower() for kind in kind_priority if isinstance(kind, str) and kind.strip()
    ]
    kind_rank = {kind: idx for idx, kind in enumerate(kind_priority_clean)}
    unknown_rank = len(kind_rank)

    for bucket in bucket_priority:
        candidates: list[tuple[int, str, TicketIndexEntry, Path]] = []
        for entry in index.values():
            if entry.status == "actioned":
                continue
            if bucket not in entry.buckets:
                continue
            bucket_paths = [path for path in entry.paths if path.parent.name == bucket]
            if not bucket_paths:
                continue
            path = sorted(bucket_paths, key=lambda p: str(p))[0]
            try:
                markdown = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                kind = None
            else:
                meta = parse_ticket_markdown_metadata(markdown)
                export_kind_raw = meta.get("export_kind")
                kind = export_kind_raw.strip().lower() if export_kind_raw else None
            rank = kind_rank.get(kind or "", unknown_rank)
            candidates.append((rank, path.name, entry, path))
        if not candidates:
            continue
        candidates.sort(key=lambda item: (item[0], item[1]))
        _, _, entry, path = candidates[0]
        return entry, path
    return None


def move_ticket_file(
    *,
    owner_root: Path,
    fingerprint: str,
    to_bucket: str,
    dry_run: bool,
) -> Path:
    if not dry_run:
        dedupe_actioned_plan_ticket_files(owner_root=owner_root)
        dedupe_queued_plan_ticket_files_when_actioned_exists(owner_root=owner_root)
    index = build_ticket_index(owner_root=owner_root)
    entry = index.get(fingerprint)
    if entry is None:
        raise ValueError(f"Unknown fingerprint: {fingerprint}")

    # Prefer not to move a ticket "backwards" once it's in a more-advanced actioned bucket.
    actioned_rank = {
        "0.1 - deferred": 0,
        "3 - in_progress": 1,
        "4 - for_review": 2,
        "5 - complete": 3,
    }
    to_rank = actioned_rank.get(to_bucket)
    bucket_to_paths: dict[str, list[Path]] = {}
    for path in entry.paths:
        bucket_to_paths.setdefault(path.parent.name, []).append(path)

    best_existing_bucket: str | None = None
    best_existing_rank: int | None = None
    for bucket in bucket_to_paths:
        rank = actioned_rank.get(bucket)
        if rank is None:
            continue
        if best_existing_rank is None or rank > best_existing_rank:
            best_existing_rank = rank
            best_existing_bucket = bucket

    if to_rank is not None and best_existing_rank is not None and best_existing_rank > to_rank:
        existing_paths = sorted(
            bucket_to_paths.get(best_existing_bucket or "", []),
            key=lambda path: str(path),
        )
        if existing_paths:
            return existing_paths[0]

    # If it's already in the destination bucket, treat as a no-op.
    already_paths = sorted(bucket_to_paths.get(to_bucket, []), key=lambda p: str(p))
    if already_paths:
        return already_paths[0]

    # Choose a sensible source bucket based on intended promotion direction.
    if to_bucket == "4 - for_review":
        source_priority = [
            "3 - in_progress",
            "2 - ready",
            "1.5 - to_plan",
            "1 - ideas",
            "0.5 - to_triage",
            "5 - complete",
            "0.1 - deferred",
        ]
    elif to_bucket == "5 - complete":
        source_priority = [
            "4 - for_review",
            "3 - in_progress",
            "2 - ready",
            "1.5 - to_plan",
            "1 - ideas",
            "0.5 - to_triage",
            "0.1 - deferred",
        ]
    else:
        source_priority = [
            "2 - ready",
            "1.5 - to_plan",
            "1 - ideas",
            "0.5 - to_triage",
            "3 - in_progress",
            "4 - for_review",
            "5 - complete",
            "0.1 - deferred",
        ]

    src_path: Path | None = None
    for bucket in source_priority:
        candidates = sorted(bucket_to_paths.get(bucket, []), key=lambda p: str(p))
        if candidates:
            src_path = candidates[0]
            break
    if src_path is None:
        # Fall back to any known path.
        candidates = sorted(entry.paths, key=lambda p: str(p))
        if not candidates:
            raise ValueError(f"Missing ticket files for fingerprint: {fingerprint}")
        src_path = candidates[0]
    plans_dir = owner_root / ".agents" / "plans"
    dest_dir = plans_dir / to_bucket
    if not dest_dir.exists() or not dest_dir.is_dir():
        raise ValueError(f"Bucket directory does not exist: {dest_dir}")
    dest_path = dest_dir / src_path.name
    if dry_run:
        return dest_path
    dest_dir.mkdir(parents=True, exist_ok=True)
    src_path.replace(dest_path)
    dedupe_actioned_plan_ticket_files(owner_root=owner_root)
    dedupe_queued_plan_ticket_files_when_actioned_exists(owner_root=owner_root)
    return dest_path


def parse_ticket_markdown_metadata(markdown: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for key, label in (
        ("fingerprint", "Fingerprint"),
        ("ticket_id", "Source ticket"),
        ("export_kind", "Export kind"),
        ("stage", "Stage"),
    ):
        match = re.search(
            rf"^-\s*{re.escape(label)}:\s*`([^`]+)`\s*$",
            markdown,
            flags=re.MULTILINE,
        )
        if match is not None:
            meta[key] = match.group(1).strip()
    title_match = re.search(r"^#\s+(.+)$", markdown, flags=re.MULTILINE)
    if title_match is not None:
        meta["title"] = title_match.group(1).strip()
    meta["parsed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return meta
