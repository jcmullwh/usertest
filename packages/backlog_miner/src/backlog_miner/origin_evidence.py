"""Hash-verified, workspace-confined origin attachment materialization.

Evidence atoms retain host-side ``attachments[*].artifact_ref`` objects so the
pipeline can audit where an observation came from.  Agent workspaces cannot be
assumed to see those host paths, however.  This module copies the exact bytes
into an agent workspace only after validating the declared SHA-256, and splits
large text into bounded, overlapping files that provider read tools can consume
without losing a signature at a chunk boundary.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from hashlib import sha256
from heapq import heappush, heapreplace
from io import BytesIO
from pathlib import Path
from typing import Any

ORIGIN_ATTACHMENT_EVIDENCE_SCHEMA_VERSION = 2
DEFAULT_ATTACHMENT_CHUNK_MAX_BYTES = 16 * 1024
DEFAULT_ATTACHMENT_CHUNK_OVERLAP_BYTES = 512
DEFAULT_BINARY_SUMMARY_MAX_BYTES = 12 * 1024
DEFAULT_BINARY_PRINTABLE_ENTRIES = 96
DEFAULT_BINARY_ARCHIVE_ENTRIES = 64

_PRINTABLE_BYTES = re.compile(rb"[\x20-\x7e]{4,}")
_DIAGNOSTIC_TERMS = (
    "abort",
    "crash",
    "denied",
    "error",
    "exception",
    "fail",
    "fatal",
    "invalid",
    "missing",
    "panic",
    "root cause",
    "timeout",
    "traceback",
    "warning",
)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolved_run_dir(atom: Mapping[str, Any], *, source_root: Path | None) -> Path | None:
    raw = _text(atom.get("run_dir"))
    if raw is None:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        if source_root is None:
            return None
        candidate = source_root / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _resolve_attachment_source(
    *,
    atom: Mapping[str, Any],
    artifact_ref: Mapping[str, Any],
    source_root: Path | None,
) -> tuple[Path | None, str | None]:
    raw_path = _text(artifact_ref.get("path"))
    if raw_path is None:
        return None, "attachment_artifact_path_missing"
    run_dir = _resolved_run_dir(atom, source_root=source_root)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        if run_dir is not None:
            candidate = run_dir / candidate
        elif source_root is not None:
            candidate = source_root / candidate
        else:
            return None, "attachment_artifact_base_missing"
    try:
        resolved = candidate.resolve()
    except OSError:
        return None, "attachment_artifact_path_unresolvable"
    boundary = run_dir or (source_root.resolve() if source_root is not None else None)
    if boundary is None or not _path_within(resolved, boundary):
        return None, "attachment_artifact_outside_source_boundary"
    if not resolved.is_file():
        return None, "attachment_artifact_file_missing"
    return resolved, None


def _utf8_boundary_before(raw: bytes, offset: int) -> int:
    offset = min(max(0, offset), len(raw))
    while offset > 0 and offset < len(raw) and raw[offset] & 0xC0 == 0x80:
        offset -= 1
    return offset


def _text_chunks(
    raw: bytes,
    *,
    chunk_max_bytes: int,
    overlap_bytes: int,
) -> list[tuple[int, int, int, int, bytes]]:
    if not raw:
        return [(0, 0, 0, 0, b"")]
    # Validate the representation once.  Per-chunk boundaries below are then
    # moved to UTF-8 code-point boundaries before bytes are written.
    raw.decode("utf-8")
    core_bytes = chunk_max_bytes - (2 * overlap_bytes)
    if core_bytes <= 0:
        raise ValueError("attachment_chunk_overlap_must_leave_positive_core")
    chunks: list[tuple[int, int, int, int, bytes]] = []
    core_start = 0
    while core_start < len(raw):
        core_start = _utf8_boundary_before(raw, core_start)
        core_end = _utf8_boundary_before(raw, min(len(raw), core_start + core_bytes))
        if core_end <= core_start:
            core_end = min(len(raw), core_start + core_bytes)
            while core_end < len(raw) and raw[core_end] & 0xC0 == 0x80:
                core_end += 1
        start = _utf8_boundary_before(raw, max(0, core_start - overlap_bytes))
        end = _utf8_boundary_before(raw, min(len(raw), core_end + overlap_bytes))
        if end < len(raw) and end <= core_end:
            end = core_end
        payload = raw[start:end]
        if len(payload) > chunk_max_bytes:
            # UTF-8 boundary adjustment can add at most three bytes.  Trim only
            # overlap, never the non-overlapping core.
            end = _utf8_boundary_before(raw, start + chunk_max_bytes)
            if end < core_end:
                raise ValueError("attachment_chunk_core_exceeds_max_bytes")
            payload = raw[start:end]
        chunks.append((start, end, core_start, core_end, payload))
        core_start = core_end
    return chunks


def _binary_kind(raw: bytes) -> str:
    """Return a bounded, magic-based content type without executing the artifact."""

    signatures = (
        (b"PK\x03\x04", "zip"),
        (b"\x1f\x8b", "gzip"),
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"\xff\xd8\xff", "jpeg"),
        (b"%PDF-", "pdf"),
        (b"SQLite format 3\x00", "sqlite3"),
        (b"\x7fELF", "elf"),
        (b"\x00asm", "webassembly"),
        (b"MZ", "windows_pe"),
    )
    return next((kind for signature, kind in signatures if raw.startswith(signature)), "binary")


def _printable_binary_candidates(raw: bytes) -> tuple[int, list[dict[str, Any]]]:
    """Select bounded, useful printable spans from the complete binary payload.

    The scan covers all bytes.  Selection favors diagnostic terms and longer spans,
    while retaining a small early sample for orientation.  No replacement-decoded
    rendering of the complete binary is produced.
    """

    total = 0
    early: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    ranked: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    for match in _PRINTABLE_BYTES.finditer(raw):
        total += 1
        start, end = match.span()
        length = end - start
        preview_raw = raw[start : min(end, start + 512)]
        preview = preview_raw.decode("ascii")
        folded = preview.casefold()
        signal_hits = sum(term in folded for term in _DIAGNOSTIC_TERMS)
        rank = (signal_hits, min(length, 4096), -start)
        entry = {
            "offset": start,
            "length": length,
            "text": preview,
            "text_truncated": length > len(preview_raw),
            "diagnostic_term_hits": signal_hits,
        }
        if len(early) < 12:
            early.append((rank, entry))
        candidate = (rank, entry)
        if len(ranked) < DEFAULT_BINARY_PRINTABLE_ENTRIES:
            heappush(ranked, candidate)
        elif rank > ranked[0][0]:
            heapreplace(ranked, candidate)

    # The heap remains bounded throughout the full scan and stores compact previews,
    # not full spans. A diagnostic in the middle or tail can still outrank boilerplate.
    ranked.sort(key=lambda item: item[0], reverse=True)
    selected_by_offset: dict[int, tuple[tuple[int, int, int], dict[str, Any]]] = {
        int(entry["offset"]): (rank, entry)
        for rank, entry in [*early, *ranked[:DEFAULT_BINARY_PRINTABLE_ENTRIES]]
    }
    selected = sorted(
        selected_by_offset.values(),
        key=lambda item: item[0],
        reverse=True,
    )[:DEFAULT_BINARY_PRINTABLE_ENTRIES]
    return total, [entry for _, entry in selected]


def _zip_archive_listing(raw: bytes) -> dict[str, Any] | None:
    """Read bounded ZIP central-directory metadata without extracting any member."""

    if _binary_kind(raw) != "zip":
        return None
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            infos = archive.infolist()
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        return {
            "format": "zip",
            "status": "invalid",
            "error_type": type(exc).__name__,
            "entries": [],
        }
    entries: list[dict[str, Any]] = []
    for info in infos[:DEFAULT_BINARY_ARCHIVE_ENTRIES]:
        printable_name = "".join(
            character if character.isprintable() else "?"
            for character in info.filename
        )[:256]
        entries.append(
            {
                "name": printable_name,
                "name_truncated": len(info.filename) > 256,
                "is_directory": info.is_dir(),
                "compressed_size": info.compress_size,
                "uncompressed_size": info.file_size,
                "compression_method": info.compress_type,
                "crc32": f"{info.CRC:08x}",
            }
        )
    return {
        "format": "zip",
        "status": "listed_without_extraction",
        "entry_count": len(infos),
        "listed_entry_count": len(entries),
        "listing_truncated": len(infos) > len(entries),
        "entries": entries,
    }


def _binary_summary(
    raw: bytes,
    *,
    source_name: str | None,
    max_bytes: int,
) -> bytes:
    """Build a bounded typed summary while retaining raw bytes separately."""

    printable_total, ranked_printable = _printable_binary_candidates(raw)
    archive_listing = _zip_archive_listing(raw)

    def _render(
        printable: list[dict[str, Any]],
        archive: dict[str, Any] | None,
    ) -> bytes:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "representation": "bounded_binary_evidence_summary",
            "binary_kind": _binary_kind(raw),
            "source_name": source_name,
            "artifact_sha256": sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "magic_prefix_hex": raw[:32].hex(),
            "printable_extraction": {
                "minimum_sequence_length": 4,
                "total_sequence_count": printable_total,
                "selected_sequence_count": len(printable),
                "selection_truncated": printable_total > len(printable),
                "sequences": sorted(printable, key=lambda item: int(item["offset"])),
            },
            "archive_listing": archive,
            "raw_bytes_note": (
                "Exact hash-verified bytes are retained in raw_file but are not a "
                "mandatory agent read."
            ),
        }
        return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    selected = list(ranked_printable)
    listing = dict(archive_listing) if archive_listing is not None else None
    rendered = _render(selected, listing)
    while len(rendered) > max_bytes and selected:
        selected.pop()
        rendered = _render(selected, listing)
    while len(rendered) > max_bytes and isinstance(listing, dict) and listing.get("entries"):
        entries = list(listing["entries"])
        entries.pop()
        listing["entries"] = entries
        listing["listed_entry_count"] = len(entries)
        listing["listing_truncated"] = True
        rendered = _render(selected, listing)
    if len(rendered) > max_bytes:
        raise ValueError("attachment_binary_summary_exceeds_chunk_max_bytes")
    return rendered


def _attachment_entries(atom: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]]:
    raw = atom.get("attachments")
    if not isinstance(raw, list):
        return []
    result: list[tuple[int, Mapping[str, Any]]] = []
    for index, attachment in enumerate(raw):
        if not isinstance(attachment, Mapping):
            continue
        ref = attachment.get("artifact_ref")
        if isinstance(ref, Mapping):
            result.append((index, ref))
    return result


def materialize_origin_attachments(
    *,
    atoms: Sequence[Mapping[str, Any]],
    workspace_dir: Path,
    source_root: Path | None,
    relative_root: Path = Path("origin_evidence"),
    chunk_max_bytes: int = DEFAULT_ATTACHMENT_CHUNK_MAX_BYTES,
    overlap_bytes: int = DEFAULT_ATTACHMENT_CHUNK_OVERLAP_BYTES,
) -> dict[str, Any]:
    """Materialize verified attachment content and return its retained manifest.

    Every destination is derived from the declared content hash, never from a
    host path.  Invalid, missing, out-of-boundary, or hash-mismatched references
    are retained as explicit errors and are never copied.
    """

    if chunk_max_bytes <= 0 or chunk_max_bytes >= 24 * 1024:
        raise ValueError("attachment_chunk_max_bytes_must_be_between_1_and_24575")
    if overlap_bytes < 0 or overlap_bytes * 2 >= chunk_max_bytes:
        raise ValueError("attachment_chunk_overlap_invalid")
    workspace = workspace_dir.resolve()
    destination_root = (workspace / relative_root).resolve()
    if not _path_within(destination_root, workspace):
        raise ValueError("attachment_destination_outside_workspace")
    destination_root.mkdir(parents=True, exist_ok=True)

    artifacts_by_sha: dict[str, dict[str, Any]] = {}
    atom_refs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for atom in atoms:
        atom_id = _text(atom.get("atom_id"))
        if atom_id is None:
            continue
        for attachment_index, artifact_ref in _attachment_entries(atom):
            expected_sha_raw = artifact_ref.get("sha256")
            expected_sha = (
                str(expected_sha_raw).casefold() if _valid_sha256(expected_sha_raw) else None
            )
            if expected_sha is None:
                errors.append(
                    {
                        "atom_id": atom_id,
                        "attachment_index": attachment_index,
                        "error": "attachment_artifact_sha256_missing_or_invalid",
                    }
                )
                continue
            source, source_error = _resolve_attachment_source(
                atom=atom,
                artifact_ref=artifact_ref,
                source_root=source_root,
            )
            if source is None:
                errors.append(
                    {
                        "atom_id": atom_id,
                        "attachment_index": attachment_index,
                        "artifact_sha256": expected_sha,
                        "error": source_error,
                    }
                )
                continue
            try:
                raw = source.read_bytes()
            except OSError:
                errors.append(
                    {
                        "atom_id": atom_id,
                        "attachment_index": attachment_index,
                        "artifact_sha256": expected_sha,
                        "error": "attachment_artifact_read_failed",
                    }
                )
                continue
            actual_sha = sha256(raw).hexdigest()
            if actual_sha != expected_sha:
                errors.append(
                    {
                        "atom_id": atom_id,
                        "attachment_index": attachment_index,
                        "artifact_sha256": expected_sha,
                        "observed_sha256": actual_sha,
                        "error": "attachment_artifact_sha256_mismatch",
                    }
                )
                continue
            expected_size = artifact_ref.get("size_bytes")
            if isinstance(expected_size, int) and not isinstance(expected_size, bool):
                if expected_size != len(raw):
                    errors.append(
                        {
                            "atom_id": atom_id,
                            "attachment_index": attachment_index,
                            "artifact_sha256": expected_sha,
                            "expected_size_bytes": expected_size,
                            "observed_size_bytes": len(raw),
                            "error": "attachment_artifact_size_mismatch",
                        }
                    )
                    continue

            artifact = artifacts_by_sha.get(expected_sha)
            if artifact is None:
                artifact_rel = relative_root / "artifacts" / expected_sha
                artifact_dir = (workspace / artifact_rel).resolve()
                if not _path_within(artifact_dir, workspace):
                    raise ValueError("attachment_artifact_destination_outside_workspace")
                chunks_dir = artifact_dir / "chunks"
                chunks_dir.mkdir(parents=True, exist_ok=True)
                raw_file_fields: dict[str, Any] = {}
                chunk_entries: list[dict[str, Any]] = []
                try:
                    chunk_payloads = _text_chunks(
                        raw,
                        chunk_max_bytes=chunk_max_bytes,
                        overlap_bytes=overlap_bytes,
                    )
                    representation = "utf-8"
                    extension = "txt"
                    for chunk_index, (
                        start,
                        end,
                        core_start,
                        core_end,
                        payload,
                    ) in enumerate(chunk_payloads, start=1):
                        chunk_rel = (
                            artifact_rel / "chunks" / f"chunk_{chunk_index:04d}.{extension}"
                        )
                        chunk_path = (workspace / chunk_rel).resolve()
                        if not _path_within(chunk_path, workspace):
                            raise ValueError("attachment_chunk_destination_outside_workspace")
                        chunk_path.write_bytes(payload)
                        chunk_entries.append(
                            {
                                "file": chunk_rel.as_posix(),
                                "sha256": sha256(payload).hexdigest(),
                                "size_bytes": len(payload),
                                "content_role": "full_utf8_text",
                                "source_start_byte": start,
                                "source_end_byte": end,
                                "core_start_byte": core_start,
                                "core_end_byte": core_end,
                            }
                        )
                except UnicodeDecodeError:
                    representation = "bounded_binary_summary"
                    raw_rel = artifact_rel / "raw.bin"
                    raw_path = (workspace / raw_rel).resolve()
                    if not _path_within(raw_path, workspace):
                        raise ValueError(
                            "attachment_raw_destination_outside_workspace"
                        ) from None
                    raw_path.write_bytes(raw)
                    source_path = _text(artifact_ref.get("path"))
                    source_name = Path(source_path).name if source_path is not None else None
                    summary_payload = _binary_summary(
                        raw,
                        source_name=source_name,
                        max_bytes=min(chunk_max_bytes, DEFAULT_BINARY_SUMMARY_MAX_BYTES),
                    )
                    summary_rel = artifact_rel / "chunks" / "binary_summary.json"
                    summary_path = (workspace / summary_rel).resolve()
                    if not _path_within(summary_path, workspace):
                        raise ValueError(
                            "attachment_chunk_destination_outside_workspace"
                        ) from None
                    summary_path.write_bytes(summary_payload)
                    chunk_entries.append(
                        {
                            "file": summary_rel.as_posix(),
                            "sha256": sha256(summary_payload).hexdigest(),
                            "size_bytes": len(summary_payload),
                            "content_role": "bounded_binary_summary",
                        }
                    )
                    raw_file_fields = {
                        "raw_file": raw_rel.as_posix(),
                        "raw_file_sha256": actual_sha,
                        "raw_file_size_bytes": len(raw),
                        "binary_kind": _binary_kind(raw),
                    }
                artifact_manifest: dict[str, Any] = {
                    "schema_version": ORIGIN_ATTACHMENT_EVIDENCE_SCHEMA_VERSION,
                    "artifact_sha256": expected_sha,
                    "size_bytes": len(raw),
                    "representation": representation,
                    "chunk_max_bytes": chunk_max_bytes,
                    "chunk_overlap_bytes": overlap_bytes if representation == "utf-8" else 0,
                    "chunk_count": len(chunk_entries),
                    "chunks": chunk_entries,
                    **raw_file_fields,
                }
                artifact_manifest["materialization_sha256"] = _canonical_sha256(
                    artifact_manifest
                )
                artifact_manifest_rel = artifact_rel / "manifest.json"
                artifact_manifest_path = (workspace / artifact_manifest_rel).resolve()
                artifact_manifest_path.write_text(
                    json.dumps(artifact_manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                artifact = {
                    **artifact_manifest,
                    "manifest_file": artifact_manifest_rel.as_posix(),
                    "manifest_file_sha256": sha256(
                        artifact_manifest_path.read_bytes()
                    ).hexdigest(),
                }
                artifacts_by_sha[expected_sha] = artifact
            atom_refs.append(
                {
                    "atom_id": atom_id,
                    "attachment_index": attachment_index,
                    "artifact_sha256": expected_sha,
                    "size_bytes": len(raw),
                    "manifest_file": artifact["manifest_file"],
                }
            )

    manifest: dict[str, Any] = {
        "schema_version": ORIGIN_ATTACHMENT_EVIDENCE_SCHEMA_VERSION,
        "format": "hash_verified_origin_attachments_v2",
        "workspace_root": relative_root.as_posix(),
        "chunk_max_bytes": chunk_max_bytes,
        "chunk_overlap_bytes": overlap_bytes,
        "artifacts": sorted(artifacts_by_sha.values(), key=lambda item: item["artifact_sha256"]),
        "atom_refs": sorted(
            atom_refs,
            key=lambda item: (item["atom_id"], item["attachment_index"]),
        ),
        "errors": sorted(
            errors,
            key=lambda item: (str(item.get("atom_id")), int(item.get("attachment_index", -1))),
        ),
    }
    manifest["materialization_sha256"] = _canonical_sha256(manifest)
    manifest_path = destination_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest["manifest_file"] = (relative_root / "manifest.json").as_posix()
    manifest["manifest_file_sha256"] = sha256(manifest_path.read_bytes()).hexdigest()
    return manifest


def origin_attachment_requirements(
    manifest: Mapping[str, Any],
    *,
    atom_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the exact bounded chunk files that assigned atoms must observe."""

    selected = set(atom_ids or [])
    refs_raw = manifest.get("atom_refs")
    refs = refs_raw if isinstance(refs_raw, list) else []
    selected_shas = {
        str(ref.get("artifact_sha256"))
        for ref in refs
        if isinstance(ref, Mapping)
        and (not selected or _text(ref.get("atom_id")) in selected)
        and _valid_sha256(ref.get("artifact_sha256"))
    }
    artifacts_raw = manifest.get("artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, list) else []
    requirements: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            continue
        artifact_sha = _text(artifact.get("artifact_sha256"))
        if artifact_sha not in selected_shas:
            continue
        chunks_raw = artifact.get("chunks")
        chunks = chunks_raw if isinstance(chunks_raw, list) else []
        for chunk in chunks:
            if not isinstance(chunk, Mapping):
                continue
            path = _text(chunk.get("file"))
            chunk_sha = _text(chunk.get("sha256"))
            if path is None or not _valid_sha256(chunk_sha):
                continue
            requirements.append(
                {
                    "artifact_sha256": artifact_sha,
                    "file": path,
                    "sha256": chunk_sha,
                    "size_bytes": chunk.get("size_bytes"),
                    "representation": artifact.get("representation"),
                    "content_role": chunk.get("content_role"),
                }
            )
    return sorted(requirements, key=lambda item: (item["artifact_sha256"], item["file"]))


def verify_materialized_origin_attachments(
    *,
    workspace_dir: Path,
    manifest: Mapping[str, Any],
) -> list[str]:
    """Revalidate retained materialization files without consulting host paths."""

    workspace = workspace_dir.resolve()
    errors: list[str] = []
    materialization_projection = {
        key: value
        for key, value in manifest.items()
        if key not in {"materialization_sha256", "manifest_file", "manifest_file_sha256"}
    }
    if manifest.get("materialization_sha256") != _canonical_sha256(
        materialization_projection
    ):
        errors.append("origin_attachment_manifest_hash_changed")
    manifest_rel = _text(manifest.get("manifest_file"))
    manifest_path = (workspace / Path(manifest_rel)).resolve() if manifest_rel else None
    if (
        manifest_path is None
        or not _path_within(manifest_path, workspace)
        or not manifest_path.is_file()
        or sha256(manifest_path.read_bytes()).hexdigest()
        != manifest.get("manifest_file_sha256")
    ):
        errors.append("origin_attachment_manifest_file_changed")
    artifacts_raw = manifest.get("artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, list) else []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            errors.append("origin_attachment_artifact_manifest_invalid")
            continue
        artifact_projection = {
            key: value
            for key, value in artifact.items()
            if key not in {"materialization_sha256", "manifest_file", "manifest_file_sha256"}
        }
        if artifact.get("materialization_sha256") != _canonical_sha256(
            artifact_projection
        ):
            errors.append(
                f"origin_attachment_artifact_manifest_hash_changed:{artifact.get('artifact_sha256')}"
            )
        artifact_manifest_rel = _text(artifact.get("manifest_file"))
        artifact_manifest_path = (
            (workspace / Path(artifact_manifest_rel)).resolve()
            if artifact_manifest_rel
            else None
        )
        if (
            artifact_manifest_path is None
            or not _path_within(artifact_manifest_path, workspace)
            or not artifact_manifest_path.is_file()
            or sha256(artifact_manifest_path.read_bytes()).hexdigest()
            != artifact.get("manifest_file_sha256")
        ):
            errors.append(
                f"origin_attachment_artifact_manifest_file_changed:{artifact.get('artifact_sha256')}"
            )
        if artifact.get("representation") == "bounded_binary_summary":
            raw_rel = _text(artifact.get("raw_file"))
            raw_path = (workspace / Path(raw_rel)).resolve() if raw_rel else None
            if raw_path is None or not _path_within(raw_path, workspace):
                errors.append(
                    f"origin_attachment_raw_outside_workspace:{artifact.get('artifact_sha256')}"
                )
            elif not raw_path.is_file():
                errors.append(
                    f"origin_attachment_raw_missing:{artifact.get('artifact_sha256')}"
                )
            else:
                retained_raw = raw_path.read_bytes()
                if (
                    sha256(retained_raw).hexdigest() != artifact.get("artifact_sha256")
                    or sha256(retained_raw).hexdigest() != artifact.get("raw_file_sha256")
                    or len(retained_raw) != artifact.get("size_bytes")
                    or len(retained_raw) != artifact.get("raw_file_size_bytes")
                ):
                    errors.append(
                        f"origin_attachment_raw_changed:{artifact.get('artifact_sha256')}"
                    )
    for requirement in origin_attachment_requirements(manifest):
        rel = Path(str(requirement["file"]))
        path = (workspace / rel).resolve()
        if not _path_within(path, workspace):
            errors.append(f"origin_attachment_chunk_outside_workspace:{rel.as_posix()}")
            continue
        if not path.is_file():
            errors.append(f"origin_attachment_chunk_missing:{rel.as_posix()}")
            continue
        raw = path.read_bytes()
        if (
            sha256(raw).hexdigest() != requirement.get("sha256")
            or len(raw) != requirement.get("size_bytes")
        ):
            errors.append(f"origin_attachment_chunk_changed:{rel.as_posix()}")
    return errors


__all__ = [
    "DEFAULT_ATTACHMENT_CHUNK_MAX_BYTES",
    "DEFAULT_ATTACHMENT_CHUNK_OVERLAP_BYTES",
    "ORIGIN_ATTACHMENT_EVIDENCE_SCHEMA_VERSION",
    "materialize_origin_attachments",
    "origin_attachment_requirements",
    "verify_materialized_origin_attachments",
]
