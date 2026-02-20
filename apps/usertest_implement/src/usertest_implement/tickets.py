from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backlog_repo.plan_index import scan_plan_ticket_index


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


def move_ticket_file(
    *,
    owner_root: Path,
    fingerprint: str,
    to_bucket: str,
    dry_run: bool,
) -> Path:
    index = build_ticket_index(owner_root=owner_root)
    entry = index.get(fingerprint)
    if entry is None:
        raise ValueError(f"Unknown fingerprint: {fingerprint}")
    if len(entry.paths) != 1:
        raise ValueError(
            f"Expected exactly 1 ticket file for fingerprint {fingerprint}, got {len(entry.paths)}"
        )
    src_path = entry.paths[0]
    plans_dir = owner_root / ".agents" / "plans"
    dest_dir = plans_dir / to_bucket
    if not dest_dir.exists() or not dest_dir.is_dir():
        raise ValueError(f"Bucket directory does not exist: {dest_dir}")
    dest_path = dest_dir / src_path.name
    if dry_run:
        return dest_path
    dest_dir.mkdir(parents=True, exist_ok=True)
    src_path.replace(dest_path)
    return dest_path


def parse_ticket_markdown_metadata(markdown: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for key, label in (("fingerprint", "Fingerprint"), ("ticket_id", "Source ticket")):
        match = re.search(
            rf"^-\\s*{re.escape(label)}:\\s*`([^`]+)`\\s*$",
            markdown,
            flags=re.MULTILINE,
        )
        if match is not None:
            meta[key] = match.group(1).strip()
    title_match = re.search(r"^#\\s+(.+)$", markdown, flags=re.MULTILINE)
    if title_match is not None:
        meta["title"] = title_match.group(1).strip()
    meta["parsed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return meta

