from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backlog_repo.actions import (
    canonicalize_failure_atom_id,
    promote_atom_status,
    sorted_unique_strings,
)
from backlog_repo.outcomes import (
    extract_outcome_markdown,
    upsert_outcome_markdown,
    validate_outcome_record,
)
from backlog_repo.ticket_provenance import is_generated_backlog_ticket

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
DISCARDED_PLAN_BUCKET = "0.2 - discarded"
DISCARDED_PLAN_BUCKETS: tuple[str, ...] = (DISCARDED_PLAN_BUCKET,)
PLAN_BUCKET_TO_TICKET_STATUS: dict[str, str] = {
    **PLAN_BUCKET_TO_ATOM_STATUS,
    DISCARDED_PLAN_BUCKET: "discarded",
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
    r"^(?P<date>[0-9]{8})_"
    r"(?:(?P<legacy_ticket_id>BLG-[0-9]{3}|TKT-[0-9a-f]{12})_)?"
    r"(?P<fingerprint>[0-9a-f]{16})_(?P<slug>.+\.md)$"
)
_ATOM_ID_PATH_COMPONENT_RE = re.compile(r"[A-Za-z0-9_.-]+")
_ATOM_ID_SOURCE_RE = re.compile(r"[A-Za-z0-9_.-]+")
_EVIDENCE_ATOM_IDS_HEADING_RE = re.compile(
    r"^#{1,6}\s+Evidence\s+atom\s+ids\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_EVIDENCE_ATOM_IDS_LABEL_RE = re.compile(
    r"^(?:-\s*)?(?:Evidence\s+atom\s+ids(?:\s+from\s+source\s+ticket)?|Atom\s+ids)"
    r"\s*:\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+", flags=re.MULTILINE)
DEQUEUED_PLAN_DIRNAMES: tuple[str, ...] = ("_dequeued",)
REMOVED_PLAN_DIRNAMES: tuple[str, ...] = (*DISCARDED_PLAN_BUCKETS, *DEQUEUED_PLAN_DIRNAMES)


def _merge_ticket_status(current: str | None, desired: str) -> str:
    """Merge plan-index statuses while treating discarded as non-actioned."""

    if current == "actioned" or desired == "actioned":
        return "actioned"
    if current == "queued" or desired == "queued":
        return "queued"
    if current == "discarded" or desired == "discarded":
        return "discarded"
    return desired


def _strip_legacy_source_ticket_lines(markdown: str) -> str:
    """Remove legacy ``Source ticket`` lines from plan markdown."""

    return re.sub(
        r"(?m)^-\s*Source ticket:\s*`[^`]*`\s*$\n?",
        "",
        markdown,
    )


def _decode_plan_markdown(path: Path) -> str | None:
    """Return readable Markdown, treating embedded NUL as integrity corruption."""
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in payload:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _plan_integrity_reason(path: Path) -> str | None:
    """Return a stable reason when a plan cannot be trusted as Markdown."""

    try:
        payload = path.read_bytes()
    except OSError:
        return "plan_copy_read_failed"
    if b"\x00" in payload:
        return "plan_copy_contains_nul_byte"
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return "plan_copy_invalid_utf8"
    return None


def _outcome_sidecar_path(path: Path) -> Path:
    """Return the durable outcome sidecar path for a plan copy."""

    return path.with_suffix(f"{path.suffix}.outcome.json")


def _read_outcome_sidecar(path: Path) -> dict[str, Any] | None:
    """Return a validated plan outcome sidecar without trusting plan contents."""

    sidecar = _outcome_sidecar_path(path)
    if not sidecar.is_file():
        return None
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        return validate_outcome_record(raw) if isinstance(raw, dict) else None
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None


def _normalize_plan_ticket_file(path: Path) -> Path:
    """Normalize legacy plan filenames/content to fingerprint-only form."""

    original = _decode_plan_markdown(path)
    if original is None:
        # Integrity-unknown records are byte-preserved until an explicit
        # archival operation can attach a sidecar disposition.
        return path

    match = PLAN_TICKET_FILENAME_RE.match(path.name)
    if match is None:
        return path

    normalized_path = path
    legacy_ticket_id = match.group("legacy_ticket_id")
    if legacy_ticket_id:
        date = match.group("date")
        fingerprint = match.group("fingerprint")
        slug = match.group("slug")
        normalized_name = f"{date}_{fingerprint}_{slug}"
        candidate = path.with_name(normalized_name)
        if candidate != path:
            if candidate.exists():
                # Preserve the legacy file. Destructive dedupe during a scan
                # loses lifecycle evidence; explicit cleanup archives it with a
                # relationship record instead.
                normalized_path = candidate
            else:
                normalized_path = path.replace(candidate)

    if normalized_path != path:
        original = _decode_plan_markdown(normalized_path)
    if original is None:
        return normalized_path
    updated = _strip_legacy_source_ticket_lines(original)
    if updated != original:
        try:
            normalized_path.write_text(updated, encoding="utf-8")
        except OSError:
            return normalized_path
    return normalized_path


def normalize_legacy_plan_ticket_files(
    *, owner_root: Path, include_discarded: bool = True
) -> dict[str, int]:
    """Explicitly normalize legacy plan filenames and Markdown metadata.

    Normalization is intentionally separate from plan indexing so callers can
    perform read-only validation without surprising filesystem mutations.
    """

    plans_dir = owner_root / ".agents" / "plans"
    if not plans_dir.is_dir():
        return {"files_scanned": 0, "files_changed": 0}
    files_scanned = 0
    files_changed = 0
    for bucket_dir in sorted(
        [path for path in plans_dir.iterdir() if path.is_dir()], key=lambda path: path.name
    ):
        if not include_discarded and bucket_dir.name in DISCARDED_PLAN_BUCKETS:
            continue
        if bucket_dir.name not in PLAN_BUCKET_TO_TICKET_STATUS:
            continue
        for path in sorted(bucket_dir.glob("*.md"), key=lambda item: item.name):
            files_scanned += 1
            before_path = path
            try:
                before_bytes = path.read_bytes()
            except OSError:
                before_bytes = None
            normalized_path = _normalize_plan_ticket_file(path)
            try:
                after_bytes = normalized_path.read_bytes()
            except OSError:
                after_bytes = None
            if normalized_path != before_path or after_bytes != before_bytes:
                files_changed += 1
    return {"files_scanned": files_scanned, "files_changed": files_changed}


def _markdown_metadata_value(markdown: str, label: str) -> str | None:
    """Extract a backtick-wrapped generated-ticket metadata value."""

    match = re.search(
        rf"^-\s*{re.escape(label)}:\s*`([^`]+)`\s*$",
        markdown,
        flags=re.MULTILINE,
    )
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


def _integrity_sidecar_matches(
    outcome: dict[str, Any] | None,
    *,
    reason: str,
    archive_reason: str,
    content_sha256: str,
    related_fingerprint: str | None,
    related_case_id: str | None,
    related_plan_revision_id: str | None,
) -> bool:
    """Return whether an existing sidecar is the requested integrity receipt."""

    return bool(
        isinstance(outcome, dict)
        and outcome.get("state") == "integrity_unknown"
        and outcome.get("outcome_scope") == "plan_copy"
        and outcome.get("intended_disposition") == "unverified"
        and outcome.get("integrity_reason") == reason
        and outcome.get("archive_reason") == archive_reason
        and outcome.get("original_sha256") == content_sha256
        and outcome.get("archive_sha256") == content_sha256
        and outcome.get("related_fingerprint") == related_fingerprint
        and outcome.get("related_case_id") == related_case_id
        and outcome.get("related_plan_revision_id") == related_plan_revision_id
    )


def archive_integrity_unknown_plan_ticket_file(
    *,
    owner_root: Path,
    path: Path,
    reason: str,
    expected_source_sha256: str,
    archive_reason: str | None = None,
    related_fingerprint: str | None = None,
    related_case_id: str | None = None,
    related_plan_revision_id: str | None = None,
) -> Path:
    """Archive an explicitly untrusted plan copy without parsing or rewriting it.

    This is the opt-in path for both byte-level corruption and valid UTF-8 that
    is known not to be plan Markdown. The caller must bind the operation to the
    bytes it inspected. The source is removed only after the archive copy and
    its sidecar have both been written, read back, hashed, and validated.
    """

    normalized_reason = reason.strip() if isinstance(reason, str) else ""
    if not normalized_reason:
        raise ValueError("integrity_archive_reason_required")
    normalized_archive_reason = (
        archive_reason.strip()
        if isinstance(archive_reason, str) and archive_reason.strip()
        else normalized_reason
    )
    normalized_expected_sha = (
        expected_source_sha256.strip().lower() if isinstance(expected_source_sha256, str) else ""
    )
    if re.fullmatch(r"[0-9a-f]{64}", normalized_expected_sha) is None:
        raise ValueError("integrity_archive_expected_source_sha256_invalid")
    normalized_related_fingerprint = (
        related_fingerprint.strip().lower()
        if isinstance(related_fingerprint, str) and related_fingerprint.strip()
        else None
    )
    if (
        normalized_related_fingerprint is not None
        and re.fullmatch(r"[0-9a-f]{16}", normalized_related_fingerprint) is None
    ):
        raise ValueError("integrity_archive_related_fingerprint_invalid")
    normalized_related_case_id = (
        related_case_id.strip()
        if isinstance(related_case_id, str) and related_case_id.strip()
        else None
    )
    normalized_related_plan_revision_id = (
        related_plan_revision_id.strip()
        if isinstance(related_plan_revision_id, str) and related_plan_revision_id.strip()
        else None
    )

    owner_root_resolved = owner_root.resolve()
    plans_dir = (owner_root_resolved / ".agents" / "plans").resolve()
    source_input = path.expanduser()
    if source_input.is_symlink():
        raise ValueError(f"Refusing to archive a symlinked plan path: {source_input}")
    source = source_input.resolve()
    if not source.is_relative_to(plans_dir):
        raise ValueError(f"Refusing to archive path outside plan root: {source}")
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(source)

    fingerprint = _fingerprint_from_plan_path(source)
    if fingerprint is None:
        raise ValueError(f"Cannot archive plan without fingerprint: {source}")
    original_bytes = source.read_bytes()
    original_sha256 = hashlib.sha256(original_bytes).hexdigest()
    if original_sha256 != normalized_expected_sha:
        raise ValueError(
            "integrity_archive_source_sha256_mismatch: "
            f"expected={normalized_expected_sha} actual={original_sha256}"
        )

    archive_dir = plans_dir / "6 - archived"
    if archive_dir.exists() and not archive_dir.resolve().is_relative_to(plans_dir):
        raise ValueError(f"Refusing to archive outside plan root: {archive_dir.resolve()}")
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = archive_dir.resolve()
    if not archive_dir.is_relative_to(plans_dir):
        raise ValueError(f"Refusing to archive outside plan root: {archive_dir}")

    def reusable(candidate: Path) -> bool:
        if not candidate.exists():
            return True
        if not candidate.is_file() or candidate.is_symlink():
            return False
        try:
            candidate_bytes = candidate.read_bytes()
        except OSError:
            return False
        if candidate_bytes != original_bytes:
            return False
        existing_outcome = _read_outcome_sidecar(candidate)
        return existing_outcome is None or _integrity_sidecar_matches(
            existing_outcome,
            reason=normalized_reason,
            archive_reason=normalized_archive_reason,
            content_sha256=original_sha256,
            related_fingerprint=normalized_related_fingerprint,
            related_case_id=normalized_related_case_id,
            related_plan_revision_id=normalized_related_plan_revision_id,
        )

    destination = archive_dir / source.name
    if destination.resolve() != source and not reusable(destination):
        destination = archive_dir / (
            f"{source.stem}__integrity_unknown_{original_sha256[:16]}{source.suffix}"
        )
        counter = 2
        while not reusable(destination):
            destination = archive_dir / (
                f"{source.stem}__integrity_unknown_{original_sha256}_{counter}{source.suffix}"
            )
            counter += 1
    if destination.parent.resolve() != archive_dir:
        raise ValueError(f"Refusing to archive outside archive directory: {destination}")

    if destination.resolve() != source and not destination.exists():
        destination.write_bytes(original_bytes)
    archived_bytes = destination.read_bytes()
    archive_sha256 = hashlib.sha256(archived_bytes).hexdigest()
    if archived_bytes != original_bytes or archive_sha256 != original_sha256:
        raise OSError(f"Archived byte verification failed: {destination}")

    sidecar = _outcome_sidecar_path(destination)
    existing_outcome = _read_outcome_sidecar(destination)
    if sidecar.exists():
        if not _integrity_sidecar_matches(
            existing_outcome,
            reason=normalized_reason,
            archive_reason=normalized_archive_reason,
            content_sha256=original_sha256,
            related_fingerprint=normalized_related_fingerprint,
            related_case_id=normalized_related_case_id,
            related_plan_revision_id=normalized_related_plan_revision_id,
        ):
            raise ValueError(f"Conflicting integrity archive sidecar: {sidecar}")
    else:
        integrity_outcome = validate_outcome_record(
            {
                "schema_version": 1,
                "case_id": f"legacy-case:{fingerprint}",
                "plan_revision_id": f"legacy-plan:{fingerprint}",
                "state": "integrity_unknown",
                "outcome_scope": "plan_copy",
                "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "requires_live_verification": False,
                "target_branch": None,
                "merged_commit": None,
                "test_evidence": [],
                "original_scenario_evidence": [],
                "live_evidence": [],
                "remaining_risks": [
                    *dict.fromkeys(
                        [
                            normalized_reason,
                            normalized_archive_reason,
                            "Plan contents were not parsed or rewritten; "
                            "original bytes were preserved unchanged",
                        ]
                    ),
                ],
                "recurrence_check": {"status": "not_run"},
                "archive_reason": normalized_archive_reason,
                "integrity_reason": normalized_reason,
                "intended_disposition": "unverified",
                "previous_path": str(source),
                "archive_path": str(destination),
                "original_sha256": original_sha256,
                "archive_sha256": archive_sha256,
                "legacy_identity": True,
                **(
                    {"related_fingerprint": normalized_related_fingerprint}
                    if normalized_related_fingerprint is not None
                    else {}
                ),
                **(
                    {"related_case_id": normalized_related_case_id}
                    if normalized_related_case_id is not None
                    else {}
                ),
                **(
                    {"related_plan_revision_id": normalized_related_plan_revision_id}
                    if normalized_related_plan_revision_id is not None
                    else {}
                ),
            }
        )
        sidecar_payload = (
            json.dumps(integrity_outcome, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        )
        temporary_sidecar = sidecar.with_name(f"{sidecar.name}.tmp")
        temporary_sidecar.write_text(sidecar_payload, encoding="utf-8")
        temporary_sidecar.replace(sidecar)
        persisted_outcome = _read_outcome_sidecar(destination)
        if not _integrity_sidecar_matches(
            persisted_outcome,
            reason=normalized_reason,
            archive_reason=normalized_archive_reason,
            content_sha256=original_sha256,
            related_fingerprint=normalized_related_fingerprint,
            related_case_id=normalized_related_case_id,
            related_plan_revision_id=normalized_related_plan_revision_id,
        ):
            raise OSError(f"Archived integrity sidecar verification failed: {sidecar}")

    if destination.resolve() != source:
        source.unlink()
    return destination


def archive_plan_ticket_file(
    *,
    owner_root: Path,
    path: Path,
    disposition: str,
    reason: str,
    related_fingerprint: str | None = None,
    related_case_id: str | None = None,
    related_plan_revision_id: str | None = None,
) -> Path:
    """Archive a generated plan without discarding its contents or lineage.

    The destination receives a validated embedded outcome record. The source is
    removed only after the destination has been written and read back exactly,
    so a partial failure can leave a harmless duplicate but cannot lose the only
    copy of a plan.
    """

    if disposition not in {"duplicate", "superseded", "unverified"}:
        raise ValueError(f"Unsupported archive disposition: {disposition!r}")
    owner_root_resolved = owner_root.resolve()
    plans_dir = (owner_root_resolved / ".agents" / "plans").resolve()
    source = path.resolve()
    if not source.is_relative_to(plans_dir):
        raise ValueError(f"Refusing to archive path outside plan root: {source}")
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(source)

    original_bytes = source.read_bytes()
    markdown = None if b"\x00" in original_bytes else _decode_plan_markdown(source)
    fingerprint = _fingerprint_from_plan_path(source) or (
        _markdown_metadata_value(markdown, "Fingerprint") if markdown is not None else None
    )
    if fingerprint is None:
        raise ValueError(f"Cannot archive plan without fingerprint: {source}")
    if disposition in {"duplicate", "superseded"} and not any(
        (related_fingerprint, related_case_id, related_plan_revision_id)
    ):
        raise ValueError(f"{disposition} archive requires a related identity")

    archive_dir = plans_dir / "6 - archived"
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / source.name
    if destination.resolve() != source and destination.exists():
        suffix = f"__{disposition}_of_{(related_fingerprint or fingerprint)[:16]}"
        destination = destination.with_name(f"{destination.stem}{suffix}{destination.suffix}")
        counter = 2
        while destination.exists():
            destination = destination.with_name(
                f"{destination.stem.rsplit('__copy_', 1)[0]}__copy_{counter}{destination.suffix}"
            )
            counter += 1

    if markdown is None:
        integrity_reason = _plan_integrity_reason(source) or "plan_copy_integrity_unknown"
        return archive_integrity_unknown_plan_ticket_file(
            owner_root=owner_root,
            path=source,
            reason=integrity_reason,
            expected_source_sha256=hashlib.sha256(original_bytes).hexdigest(),
            archive_reason=reason,
            related_fingerprint=related_fingerprint,
            related_case_id=related_case_id,
            related_plan_revision_id=related_plan_revision_id,
        )

    case_id = _markdown_metadata_value(markdown, "Case ID") or f"legacy-case:{fingerprint}"
    plan_revision_id = (
        _markdown_metadata_value(markdown, "Plan revision ID") or f"legacy-plan:{fingerprint}"
    )
    recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    outcome: dict[str, Any] = {
        "schema_version": 1,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "state": disposition,
        "outcome_scope": "plan_copy",
        "recorded_at": recorded_at,
        "requires_live_verification": False,
        "target_branch": None,
        "merged_commit": None,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "remaining_risks": [reason],
        "recurrence_check": {"status": "not_run"},
        "archive_reason": reason,
        "previous_path": str(source),
        "legacy_identity": case_id.startswith("legacy-case:"),
    }
    if disposition in {"duplicate", "superseded"}:
        outcome["related_case_id"] = related_case_id or case_id
    if related_fingerprint is not None:
        outcome["related_fingerprint"] = related_fingerprint
    if related_plan_revision_id is not None:
        outcome["related_plan_revision_id"] = related_plan_revision_id
    updated = upsert_outcome_markdown(markdown, outcome)
    updated_bytes = updated.encode("utf-8")

    if destination.resolve() == source:
        source.write_bytes(updated_bytes)
        return source

    destination.write_bytes(updated_bytes)
    if destination.read_bytes() != updated_bytes:
        raise OSError(f"Archived plan verification failed: {destination}")
    source.unlink()
    return destination


def _valid_plan_atom_id(value: str) -> bool:
    """Return whether ``value`` has the structural atom-ID contract.

    A run identifier is a slash-delimited path and may contain arbitrary namespace
    depth.  Splitting from the right preserves that namespace while still binding the
    source kind and one-based atom index.
    """

    parts = value.rsplit(":", 2)
    if len(parts) != 3:
        return False
    run_id, source, raw_index = parts
    run_parts = run_id.split("/")
    return bool(
        len(run_parts) >= 2
        and all(_ATOM_ID_PATH_COMPONENT_RE.fullmatch(part) for part in run_parts)
        and _ATOM_ID_SOURCE_RE.fullmatch(source)
        and raw_index.isdigit()
        and int(raw_index) > 0
    )


def _evidence_atom_id_section_bodies(markdown: str) -> list[str]:
    """Return bodies of current and recognized historical provenance blocks."""

    bodies: list[str] = []
    for heading in _EVIDENCE_ATOM_IDS_HEADING_RE.finditer(markdown):
        body_start = heading.end()
        next_heading = _MARKDOWN_HEADING_RE.search(markdown, body_start)
        body_end = next_heading.start() if next_heading is not None else len(markdown)
        bodies.append(markdown[body_start:body_end])
    for label in _EVIDENCE_ATOM_IDS_LABEL_RE.finditer(markdown):
        body_start = label.end()
        body_end = body_start
        saw_atom_line = False
        for line in markdown[body_start:].splitlines(keepends=True):
            stripped = line.strip()
            if not stripped:
                if saw_atom_line:
                    break
                body_end += len(line)
                continue
            match = re.fullmatch(r"-\s*`([^`]+)`\s*", stripped)
            if match is None or not _valid_plan_atom_id(match.group(1).strip()):
                break
            saw_atom_line = True
            body_end += len(line)
        if saw_atom_line:
            bodies.append(markdown[body_start:body_end])
    return bodies


def _extract_atom_ids_from_ticket_markdown(markdown: str) -> list[str]:
    """Extract atom identifiers from the ticket's evidence section.

    Parameters
    ----------
    markdown:
        Ticket markdown content.

    Returns
    -------
    list[str]
        Sorted unique atom IDs matching the repository atom-ID structure.
    """

    atom_ids: set[str] = set()
    for body in _evidence_atom_id_section_bodies(markdown):
        for candidate in re.findall(r"`([^`]+)`", body):
            cleaned = candidate.strip()
            if _valid_plan_atom_id(cleaned):
                atom_ids.add(cleaned)
    return sorted(atom_ids)


def _fingerprint_from_plan_path(path: Path) -> str | None:
    match = PLAN_TICKET_FILENAME_RE.match(path.name)
    if match is None:
        return None
    return match.group("fingerprint")


def _atom_ids_for_plan(
    *,
    atom_actions: dict[str, dict[str, Any]],
    markdown: str,
    fingerprint: str | None,
) -> list[str]:
    atom_ids = set(_extract_atom_ids_from_ticket_markdown(markdown))
    if fingerprint is not None:
        for atom_id, entry in atom_actions.items():
            fingerprints = entry.get("fingerprints", [])
            if not isinstance(fingerprints, list):
                continue
            if fingerprint in {item for item in fingerprints if isinstance(item, str)}:
                atom_ids.add(atom_id)
    return sorted(atom_ids)


def _canonicalize_atom_id_for_update(atom_id: str) -> tuple[str, str | None]:
    canonical_atom_id = canonicalize_failure_atom_id(atom_id)
    if canonical_atom_id is not None and canonical_atom_id != atom_id:
        return canonical_atom_id, atom_id
    return atom_id, None


def sync_atom_actions_from_dequeued_plan_folders(
    *,
    atom_actions: dict[str, dict[str, Any]],
    owner_roots: list[Path],
    generated_at: str,
) -> dict[str, Any]:
    """Demote atom ledger entries based on discarded/dequeued plan files.

    Plans moved under `.agents/plans/0.2 - discarded` or `_dequeued/**`
    are treated as explicitly removed from the active queue. Any referenced atoms are
    demoted back to `new` so they become eligible for re-mining.

    This is intended to run *before* `sync_atom_actions_from_plan_folders()` so that
    any atoms still referenced by active queued/actioned plan buckets are promoted
    back immediately.
    """

    roots_scanned = 0
    dequeued_dirs_scanned = 0
    ticket_files_scanned = 0
    integrity_unknown_ticket_files = 0
    tickets_without_evidence = 0
    atom_ids_seen = 0
    atoms_missing = 0
    atoms_created = 0
    atoms_demoted = 0
    discarded_ticket_files_scanned = 0

    for owner_root in owner_roots:
        plans_dir = owner_root / ".agents" / "plans"
        if not plans_dir.exists() or not plans_dir.is_dir():
            continue
        roots_scanned += 1

        dequeued_dirs: list[Path] = []
        for dirname in REMOVED_PLAN_DIRNAMES:
            candidate = plans_dir / dirname
            if candidate.exists() and candidate.is_dir():
                dequeued_dirs.append(candidate)
        if not dequeued_dirs:
            continue
        dequeued_dirs_scanned += len(dequeued_dirs)

        for dequeued_dir in dequeued_dirs:
            for md_path in sorted(dequeued_dir.rglob("*.md"), key=lambda p: str(p)):
                ticket_files_scanned += 1
                if dequeued_dir.name in DISCARDED_PLAN_BUCKETS:
                    discarded_ticket_files_scanned += 1

                markdown = _decode_plan_markdown(md_path)
                if markdown is None:
                    # Corrupt historical records must not mutate case lifecycle
                    # state. ``scan_plan_ticket_index`` exposes them as
                    # ``integrity_unknown`` while retaining the original bytes.
                    integrity_unknown_ticket_files += 1
                    continue
                fingerprint = _fingerprint_from_plan_path(md_path)
                atom_ids = _atom_ids_for_plan(
                    atom_actions=atom_actions,
                    markdown=markdown,
                    fingerprint=fingerprint,
                )
                if not atom_ids:
                    tickets_without_evidence += 1
                    continue
                atom_ids_seen += len(atom_ids)

                for atom_id in atom_ids:
                    atom_id, derived_from_atom_id = _canonicalize_atom_id_for_update(atom_id)

                    existing = atom_actions.get(atom_id)
                    if existing is None:
                        existing = {
                            "atom_id": atom_id,
                            "status": "new",
                            "first_seen_at": generated_at,
                        }
                        atom_actions[atom_id] = existing
                        atoms_created += 1

                    old_status_raw = existing.get("status")
                    old_status = str(old_status_raw) if isinstance(old_status_raw, str) else None
                    old_status_n = old_status.strip().lower() if old_status else "new"

                    if old_status_n != "new":
                        atoms_demoted += 1
                    existing["status"] = "new"
                    existing["last_dequeued_at"] = generated_at
                    existing["last_removed_plan_bucket"] = dequeued_dir.name
                    if dequeued_dir.name in DISCARDED_PLAN_BUCKETS:
                        existing["last_discarded_at"] = generated_at

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

                    if dequeued_dir.name in DISCARDED_PLAN_BUCKETS:
                        discarded_paths = [
                            item
                            for item in existing.get("discarded_paths", [])
                            if isinstance(item, str)
                        ]
                        discarded_paths.append(str(md_path))
                        existing["discarded_paths"] = sorted_unique_strings(discarded_paths)

                        discarded_roots = [
                            item
                            for item in existing.get("discarded_owner_roots", [])
                            if isinstance(item, str)
                        ]
                        discarded_roots.append(str(owner_root))
                        existing["discarded_owner_roots"] = sorted_unique_strings(discarded_roots)

                        if fingerprint is not None:
                            discarded_fingerprints = [
                                item
                                for item in existing.get("discarded_fingerprints", [])
                                if isinstance(item, str)
                            ]
                            discarded_fingerprints.append(fingerprint)
                            existing["discarded_fingerprints"] = sorted_unique_strings(
                                discarded_fingerprints
                            )

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
        "integrity_unknown_ticket_files": integrity_unknown_ticket_files,
        "tickets_without_evidence": tickets_without_evidence,
        "atom_ids_seen": atom_ids_seen,
        "atoms_missing": atoms_missing,
        "atoms_created": atoms_created,
        "atoms_demoted": atoms_demoted,
        "discarded_ticket_files_scanned": discarded_ticket_files_scanned,
    }


def scan_plan_ticket_index(
    *,
    owner_root: Path,
    include_discarded: bool = True,
) -> dict[str, dict[str, Any]]:
    """Build a read-only fingerprint-to-plan index from `.agents/plans` folders.

    Parameters
    ----------
    owner_root:
        Repository root containing `.agents/plans`.
    include_discarded:
        Include the non-actioned discarded bucket in the returned index. Disable
        this for export dedupe so rejected generated tickets do not block future
        ticket generation.

    Returns
    -------
    dict[str, dict[str, Any]]
        Mapping keyed by fingerprint with merged status, paths, and bucket names.
    """

    plans_dir = owner_root / ".agents" / "plans"
    if not plans_dir.exists() or not plans_dir.is_dir():
        return {}

    index: dict[str, dict[str, Any]] = {}
    for bucket_dir in sorted([p for p in plans_dir.iterdir() if p.is_dir()], key=lambda p: p.name):
        if not include_discarded and bucket_dir.name in DISCARDED_PLAN_BUCKETS:
            continue
        desired_status = PLAN_BUCKET_TO_TICKET_STATUS.get(bucket_dir.name)
        if desired_status is None:
            continue

        for md_path in sorted(bucket_dir.glob("*.md"), key=lambda p: p.name):
            match = PLAN_TICKET_FILENAME_RE.match(md_path.name)
            if match is None:
                continue
            fingerprint = match.group("fingerprint")

            try:
                markdown = _decode_plan_markdown(md_path)
                outcome = extract_outcome_markdown(markdown) if markdown is not None else None
            except (OSError, ValueError):
                markdown = None
                outcome = None
            integrity_reason = _plan_integrity_reason(md_path) if markdown is None else None
            if markdown is None and integrity_reason is None:
                integrity_reason = "plan_copy_outcome_metadata_invalid"
            if outcome is None:
                outcome = _read_outcome_sidecar(md_path)

            semantic_integrity_unknown = bool(
                isinstance(outcome, dict)
                and outcome.get("outcome_scope") == "plan_copy"
                and outcome.get("state") == "integrity_unknown"
            )
            if semantic_integrity_unknown and integrity_reason is None:
                outcome_reason = outcome.get("integrity_reason")
                integrity_reason = (
                    outcome_reason.strip()
                    if isinstance(outcome_reason, str) and outcome_reason.strip()
                    else "plan_copy_integrity_unknown"
                )

            if markdown is None or semantic_integrity_unknown:
                meta = index.get(fingerprint)
                if meta is None:
                    meta = {"status": "integrity_unknown", "paths": [], "buckets": []}
                integrity_paths = [
                    item
                    for item in meta.get("integrity_unknown_paths", [])
                    if isinstance(item, str)
                ]
                integrity_paths.append(str(md_path))
                meta["integrity_unknown_paths"] = sorted_unique_strings(integrity_paths)
                integrity_buckets = [
                    item
                    for item in meta.get("integrity_unknown_buckets", [])
                    if isinstance(item, str)
                ]
                integrity_buckets.append(bucket_dir.name)
                meta["integrity_unknown_buckets"] = sorted_unique_strings(integrity_buckets)
                integrity_reasons = [
                    item
                    for item in meta.get("integrity_unknown_reasons", [])
                    if isinstance(item, str)
                ]
                assert integrity_reason is not None
                integrity_reasons.append(integrity_reason)
                meta["integrity_unknown_reasons"] = sorted_unique_strings(integrity_reasons)
                integrity_records = [
                    item
                    for item in meta.get("integrity_unknown_records", [])
                    if isinstance(item, dict)
                ]
                integrity_record: dict[str, Any] = {
                    "path": str(md_path),
                    "bucket": bucket_dir.name,
                    "state": "integrity_unknown",
                    "outcome_scope": "plan_copy",
                    "reason": integrity_reason,
                    "canonical": False,
                }
                if isinstance(outcome, dict):
                    integrity_record["sidecar_state"] = str(outcome.get("state") or "")
                    for relation_field in (
                        "related_fingerprint",
                        "related_case_id",
                        "related_plan_revision_id",
                    ):
                        relation_value = outcome.get(relation_field)
                        if isinstance(relation_value, str) and relation_value:
                            integrity_record[relation_field] = relation_value
                integrity_records.append(integrity_record)
                meta["integrity_unknown_records"] = integrity_records
                index[fingerprint] = meta
                continue

            if outcome is not None and outcome.get("outcome_scope") == "plan_copy":
                meta = index.get(fingerprint)
                if meta is None:
                    meta = {"status": "actioned", "paths": [], "buckets": []}
                relationship_paths = [
                    item for item in meta.get("relationship_paths", []) if isinstance(item, str)
                ]
                relationship_paths.append(str(md_path))
                meta["relationship_paths"] = sorted_unique_strings(relationship_paths)
                states = [
                    item
                    for item in meta.get("relationship_outcome_states", [])
                    if isinstance(item, str)
                ]
                states.append(str(outcome["state"]))
                meta["relationship_outcome_states"] = sorted_unique_strings(states)
                case_ids = [item for item in meta.get("case_ids", []) if isinstance(item, str)]
                case_ids.append(str(outcome["case_id"]))
                meta["case_ids"] = sorted_unique_strings(case_ids)
                revision_ids = [
                    item for item in meta.get("plan_revision_ids", []) if isinstance(item, str)
                ]
                revision_ids.append(str(outcome["plan_revision_id"]))
                meta["plan_revision_ids"] = sorted_unique_strings(revision_ids)
                index[fingerprint] = meta
                continue

            meta = index.get(fingerprint)
            if meta is None:
                meta = {"status": desired_status, "paths": [], "buckets": []}
                index[fingerprint] = meta

            status_value = meta.get("status")
            status_current = str(status_value) if isinstance(status_value, str) else None
            meta["status"] = _merge_ticket_status(status_current, desired_status)

            paths = [item for item in meta.get("paths", []) if isinstance(item, str)]
            paths.append(str(md_path))
            meta["paths"] = sorted_unique_strings(paths)

            buckets = [item for item in meta.get("buckets", []) if isinstance(item, str)]
            buckets.append(bucket_dir.name)
            meta["buckets"] = sorted_unique_strings(buckets)

            assert markdown is not None
            case_id = _markdown_metadata_value(markdown, "Case ID")
            if case_id is not None:
                case_ids = [item for item in meta.get("case_ids", []) if isinstance(item, str)]
                case_ids.append(case_id)
                meta["case_ids"] = sorted_unique_strings(case_ids)

            plan_revision_id = _markdown_metadata_value(markdown, "Plan revision ID")
            if plan_revision_id is not None:
                revision_ids = [
                    item for item in meta.get("plan_revision_ids", []) if isinstance(item, str)
                ]
                revision_ids.append(plan_revision_id)
                meta["plan_revision_ids"] = sorted_unique_strings(revision_ids)

            if outcome is not None and desired_status == "actioned":
                states = [item for item in meta.get("outcome_states", []) if isinstance(item, str)]
                states.append(str(outcome["state"]))
                meta["outcome_states"] = sorted_unique_strings(states)
                active_outcomes = [
                    item
                    for item in meta.get("active_outcome_records", [])
                    if isinstance(item, dict)
                ]
                active_outcomes.append(
                    {
                        "path": str(md_path),
                        "bucket": bucket_dir.name,
                        "state": str(outcome["state"]),
                        "case_id": str(outcome["case_id"]),
                        "plan_revision_id": str(outcome["plan_revision_id"]),
                        "remaining_risks": list(outcome.get("remaining_risks", [])),
                    }
                )
                meta["active_outcome_records"] = active_outcomes

            index[fingerprint] = meta

    for fingerprint, meta in index.items():
        if not isinstance(meta, dict) or meta.get("status") != "actioned":
            continue
        paths = [Path(item) for item in meta.get("paths", []) if isinstance(item, str)]
        active_by_bucket: dict[str, list[str]] = {}
        for path in paths:
            if PLAN_BUCKET_TO_TICKET_STATUS.get(path.parent.name) != "actioned":
                continue
            active_by_bucket.setdefault(path.parent.name, []).append(str(path))
        ambiguous_buckets = {
            bucket: sorted(bucket_paths)
            for bucket, bucket_paths in active_by_bucket.items()
            if len(set(bucket_paths)) > 1
        }
        if ambiguous_buckets:
            raise ValueError(
                "plan_index_multiple_canonical_copies: "
                f"fingerprint={fingerprint!r} buckets={ambiguous_buckets!r}"
            )

        active_outcomes = [
            item for item in meta.get("active_outcome_records", []) if isinstance(item, dict)
        ]
        states = sorted(
            {
                str(item.get("state")).strip().lower()
                for item in active_outcomes
                if isinstance(item.get("state"), str) and str(item.get("state")).strip()
            }
        )
        identities = sorted(
            {
                (str(item.get("case_id")), str(item.get("plan_revision_id")))
                for item in active_outcomes
            }
        )
        if len(states) > 1 or len(identities) > 1:
            raise ValueError(
                "plan_index_conflicting_active_outcomes: "
                f"fingerprint={fingerprint!r} states={states!r} identities={identities!r}"
            )

    return index


def dedupe_actioned_plan_ticket_files(*, owner_root: Path) -> int:
    """Losslessly repair duplicate canonical copies before strict indexing.

    Historical corpora can contain two files with the same fingerprint in the
    *same* actioned bucket.  A strict index correctly rejects that ambiguity, so
    cleanup cannot depend on the strict index it is intended to repair.  This
    pass groups raw filenames, retains a protected copy or one deterministic
    generated copy, and archives only the remaining generated copies with
    plan-copy lineage.  Corrupt/unknown bytes are never moved automatically.

    Only records carrying the exact automated-export marker may be archived.
    Manual, IDEA-originated, and unreadable records are protected.  When one
    protected copy shares a fingerprint with generated copies, it is retained
    and only the generated copies are archived.  More than one protected copy
    is an ambiguity that requires manual handling.

    Returns
    -------
    int
        Number of duplicate files archived or marked as relationship copies.
    """

    plans_dir = owner_root / ".agents" / "plans"
    if not plans_dir.exists() or not plans_dir.is_dir():
        return 0

    def _generated(markdown: str | None) -> bool:
        return markdown is not None and is_generated_backlog_ticket(markdown)

    def _plan_copy_relationship(path: Path, markdown: str | None) -> bool:
        if markdown is not None:
            try:
                outcome = extract_outcome_markdown(markdown)
            except ValueError:
                outcome = None
            if isinstance(outcome, dict) and outcome.get("outcome_scope") == "plan_copy":
                return True
        outcome = _read_outcome_sidecar(path)
        return isinstance(outcome, dict) and outcome.get("outcome_scope") == "plan_copy"

    def _candidate_score(path: Path, markdown: str | None) -> tuple[Any, ...]:
        readable = markdown is not None
        outcome_valid = False
        case_aware = False
        generated = False
        if markdown is not None:
            try:
                outcome_valid = extract_outcome_markdown(markdown) is not None
            except ValueError:
                outcome_valid = False
            case_aware = bool(
                _markdown_metadata_value(markdown, "Case ID")
                and _markdown_metadata_value(markdown, "Plan revision ID")
            )
            generated = (
                "Generated by `python -m usertest_backlog.cli reports export-tickets`" in markdown
            )
        match = PLAN_TICKET_FILENAME_RE.match(path.name)
        date = int(match.group("date")) if match is not None else 0
        return (
            readable,
            outcome_valid,
            case_aware,
            generated,
            _ACTIONED_BUCKET_RANK.get(path.parent.name, 0),
            date,
            path.name.casefold(),
            str(path).casefold(),
        )

    grouped: dict[str, list[tuple[Path, str | None]]] = {}
    for bucket in ACTIONED_PLAN_BUCKET_PRIORITY:
        bucket_dir = plans_dir / bucket
        if not bucket_dir.is_dir():
            continue
        for path in sorted(bucket_dir.glob("*.md"), key=lambda item: item.name):
            match = PLAN_TICKET_FILENAME_RE.match(path.name)
            if match is None:
                continue
            markdown = _decode_plan_markdown(path)
            if _plan_copy_relationship(path, markdown):
                continue
            grouped.setdefault(match.group("fingerprint"), []).append((path, markdown))

    repairs: list[tuple[str, Path, str | None, list[tuple[Path, str | None]]]] = []
    for fingerprint, candidates in sorted(grouped.items()):
        if len(candidates) <= 1:
            continue
        protected = [item for item in candidates if not _generated(item[1])]
        if len(protected) > 1:
            raise ValueError(
                "Refusing automated repair of duplicate non-generated or unknown plans: "
                f"fingerprint={fingerprint!r}"
            )
        canonical_path, canonical_markdown = (
            protected[0]
            if protected
            else max(
                candidates,
                key=lambda item: _candidate_score(item[0], item[1]),
            )
        )
        generated_duplicates = [
            item for item in candidates if item[0] != canonical_path and _generated(item[1])
        ]
        repairs.append(
            (
                fingerprint,
                canonical_path,
                canonical_markdown,
                generated_duplicates,
            )
        )

    # All ambiguity checks happen before the first write so a protected group can
    # never be partially repaired because a later group is unsafe.
    archived = 0
    for fingerprint, canonical_path, canonical_markdown, duplicates in repairs:
        related_case_id = (
            _markdown_metadata_value(canonical_markdown, "Case ID")
            if canonical_markdown is not None
            else None
        )
        related_plan_revision_id = (
            _markdown_metadata_value(canonical_markdown, "Plan revision ID")
            if canonical_markdown is not None
            else None
        )
        for path, _markdown in duplicates:
            archive_plan_ticket_file(
                owner_root=owner_root,
                path=path,
                disposition="duplicate",
                related_fingerprint=fingerprint,
                related_case_id=related_case_id,
                related_plan_revision_id=related_plan_revision_id,
                reason=f"Duplicate plan copy; canonical copy retained at {canonical_path}",
            )
            archived += 1

    # Re-run the strict read-only index as the repair's postcondition.  A second
    # invocation sees only canonical and relationship copies and is a no-op.
    scan_plan_ticket_index(owner_root=owner_root)
    return archived


def dedupe_queued_plan_ticket_files_when_actioned_exists(*, owner_root: Path) -> int:
    """Archive queued plan files when the same fingerprint is already actioned.

    This is a best-effort hygiene sweep to eliminate stale *generated*
    duplicates that can linger across runs even when the current backlog no
    longer contains that fingerprint. Manual, IDEA, corrupt, and otherwise
    unclassified queue files are never archived by automated hygiene.

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
            markdown = _decode_plan_markdown(path)
            if markdown is None or not is_generated_backlog_ticket(markdown):
                continue
            try:
                archive_plan_ticket_file(
                    owner_root=owner_root,
                    path=path,
                    disposition="duplicate",
                    related_fingerprint=_fingerprint_from_plan_path(path),
                    reason="Same fingerprint already exists in an actioned plan bucket",
                )
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
    integrity_unknown_ticket_files = 0
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
                fingerprint = match.group("fingerprint")

                markdown = _decode_plan_markdown(md_path)
                if markdown is None:
                    # Do not promote atoms from a plan whose bytes cannot be
                    # trusted. The plan index preserves its identity with an
                    # ``integrity_unknown`` projection instead.
                    integrity_unknown_ticket_files += 1
                    continue
                outcome = (
                    extract_outcome_markdown(markdown)
                    if "<!-- backlog-outcome:start -->" in markdown
                    else None
                )
                if outcome is None:
                    outcome = _read_outcome_sidecar(md_path)
                if (
                    isinstance(outcome, dict)
                    and outcome.get("outcome_scope") == "plan_copy"
                    and outcome.get("state") == "integrity_unknown"
                ):
                    # Explicit semantic-integrity receipts are authoritative even
                    # when the untrusted bytes happen to decode as UTF-8.
                    integrity_unknown_ticket_files += 1
                    continue
                plan_case_id = _markdown_metadata_value(markdown, "Case ID")
                plan_revision_id = _markdown_metadata_value(markdown, "Plan revision ID")
                if outcome is not None:
                    plan_case_id = str(outcome["case_id"])
                    plan_revision_id = str(outcome["plan_revision_id"])
                if plan_revision_id is None:
                    plan_revision_id = f"legacy-plan:{fingerprint}"

                atom_ids = _atom_ids_for_plan(
                    atom_actions=atom_actions,
                    markdown=markdown,
                    fingerprint=fingerprint,
                )
                if not atom_ids:
                    tickets_without_evidence += 1
                    continue
                atom_ids_seen += len(atom_ids)

                for atom_id in atom_ids:
                    atom_id, derived_from_atom_id = _canonicalize_atom_id_for_update(atom_id)

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
                    if plan_case_id is not None:
                        existing["case_id"] = plan_case_id
                    if outcome is not None and outcome.get("outcome_scope") == "case":
                        existing["last_outcome_state"] = outcome["state"]
                        existing["last_outcome_recorded_at"] = outcome["recorded_at"]
                        existing["last_outcome_path"] = str(md_path)
                        existing["last_outcome_record"] = dict(outcome)
                    elif outcome is not None:
                        existing["last_plan_copy_disposition"] = outcome["state"]
                        existing["last_plan_copy_disposition_at"] = outcome["recorded_at"]
                        existing["last_plan_copy_path"] = str(md_path)

                    if plan_case_id is not None:
                        plan_outcomes_raw = existing.get("plan_outcomes")
                        plan_outcomes = (
                            dict(plan_outcomes_raw) if isinstance(plan_outcomes_raw, dict) else {}
                        )
                        plan_outcomes_changed = False
                        if outcome is not None and outcome.get("outcome_scope") == "case":
                            plan_outcomes[plan_revision_id] = {
                                "state": outcome["state"],
                                "recorded_at": outcome["recorded_at"],
                                "path": str(md_path),
                                "fingerprint": fingerprint,
                                "required": True,
                                "outcome_record": dict(outcome),
                            }
                            plan_outcomes_changed = True
                        elif outcome is None and plan_revision_id not in plan_outcomes:
                            plan_outcomes[plan_revision_id] = {
                                "state": "planned",
                                "recorded_at": generated_at,
                                "path": str(md_path),
                                "fingerprint": fingerprint,
                                "required": True,
                            }
                            plan_outcomes_changed = True
                        if plan_outcomes_changed:
                            existing["plan_outcomes"] = plan_outcomes

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
        "integrity_unknown_ticket_files": integrity_unknown_ticket_files,
        "tickets_without_evidence": tickets_without_evidence,
        "atom_ids_seen": atom_ids_seen,
        "atoms_created": atoms_created,
        "atoms_promoted": atoms_promoted,
    }


def _live_plan_fingerprints(*, owner_roots: list[Path]) -> set[str]:
    live: set[str] = set()
    for owner_root in owner_roots:
        plans_dir = owner_root / ".agents" / "plans"
        if not plans_dir.exists() or not plans_dir.is_dir():
            continue
        bucket_dirs = sorted(
            [path for path in plans_dir.iterdir() if path.is_dir()],
            key=lambda path: path.name,
        )
        for bucket_dir in bucket_dirs:
            if PLAN_BUCKET_TO_TICKET_STATUS.get(bucket_dir.name) is None:
                continue
            for md_path in sorted(bucket_dir.glob("*.md"), key=lambda p: p.name):
                if _decode_plan_markdown(md_path) is None:
                    continue
                fingerprint = _fingerprint_from_plan_path(md_path)
                if fingerprint is not None:
                    live.add(fingerprint)
    return live


def reconcile_missing_plan_atoms(
    *,
    atom_actions: dict[str, dict[str, Any]],
    owner_roots: list[Path],
    generated_at: str,
) -> dict[str, Any]:
    """Demote stale queued/ticketed atoms whose recorded plans no longer exist."""

    live_fingerprints = _live_plan_fingerprints(owner_roots=owner_roots)
    atoms_demoted = 0
    atoms_checked = 0
    atoms_with_live_fingerprint = 0
    atoms_with_existing_queue_path = 0

    for atom_id, existing in list(atom_actions.items()):
        old_status_raw = existing.get("status")
        old_status = (
            str(old_status_raw).strip().lower() if isinstance(old_status_raw, str) else "new"
        )
        if old_status not in ("queued", "ticketed"):
            continue
        atoms_checked += 1

        fingerprints = [
            item for item in existing.get("fingerprints", []) if isinstance(item, str) and item
        ]
        if any(fingerprint in live_fingerprints for fingerprint in fingerprints):
            atoms_with_live_fingerprint += 1
            continue

        queue_paths = [
            item for item in existing.get("queue_paths", []) if isinstance(item, str) and item
        ]
        existing_queue_paths = [
            item
            for item in queue_paths
            if Path(item).is_file() and _decode_plan_markdown(Path(item)) is not None
        ]
        if existing_queue_paths:
            atoms_with_existing_queue_path += 1
            continue

        existing["status"] = "new"
        existing["last_reconciled_missing_plan_at"] = generated_at
        existing["last_reconciled_missing_plan_reason"] = "no_live_fingerprint_or_queue_path"
        if queue_paths:
            missing_paths = [
                item
                for item in existing.get("reconciled_missing_queue_paths", [])
                if isinstance(item, str)
            ]
            missing_paths.extend(queue_paths)
            existing["reconciled_missing_queue_paths"] = sorted_unique_strings(missing_paths)
        atom_actions[atom_id] = existing
        atoms_demoted += 1

    return {
        "atoms_checked": atoms_checked,
        "atoms_demoted": atoms_demoted,
        "atoms_with_live_fingerprint": atoms_with_live_fingerprint,
        "atoms_with_existing_queue_path": atoms_with_existing_queue_path,
        "live_fingerprints": len(live_fingerprints),
    }


def reconcile_atom_actions_from_plan_folders(
    *,
    atom_actions: dict[str, dict[str, Any]],
    owner_roots: list[Path],
    generated_at: str,
) -> dict[str, Any]:
    """Synchronize atom lifecycle state from all plan-folder evidence."""

    removal_sync = sync_atom_actions_from_dequeued_plan_folders(
        atom_actions=atom_actions,
        owner_roots=owner_roots,
        generated_at=generated_at,
    )
    plan_sync = sync_atom_actions_from_plan_folders(
        atom_actions=atom_actions,
        owner_roots=owner_roots,
        generated_at=generated_at,
    )
    missing_plan_sync = reconcile_missing_plan_atoms(
        atom_actions=atom_actions,
        owner_roots=owner_roots,
        generated_at=generated_at,
    )
    status_counts: dict[str, int] = {}
    for entry in atom_actions.values():
        status = entry.get("status")
        if isinstance(status, str) and status:
            status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "removal_sync": removal_sync,
        "plan_sync": plan_sync,
        "missing_plan_sync": missing_plan_sync,
        "status_counts": status_counts,
        "ledger_atoms_total": len(atom_actions),
    }
