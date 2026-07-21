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

from backlog_core.case_lineage import source_evidence_atom_projection

ORIGIN_ATTACHMENT_EVIDENCE_SCHEMA_VERSION = 2
DEFAULT_ATTACHMENT_CHUNK_MAX_BYTES = 16 * 1024
DEFAULT_ATTACHMENT_CHUNK_OVERLAP_BYTES = 512
DEFAULT_BINARY_SUMMARY_MAX_BYTES = 12 * 1024
DEFAULT_BINARY_PRINTABLE_ENTRIES = 96
DEFAULT_BINARY_ARCHIVE_ENTRIES = 64
RUN_CONTEXT_SCHEMA_VERSION = 1
ASSIGNED_EVIDENCE_SCHEMA_VERSION = 1
RUN_CONTEXT_SOURCE_MAX_BYTES = 1024 * 1024
RUN_CONTEXT_INDEX_MAX_BYTES = 256 * 1024
ASSIGNED_EVIDENCE_SYMPTOM_EXCERPT_CHARS = 600
RESEARCH_RUN_CONTEXT_FILES: dict[str, str] = {
    "preflight.json": "preflight",
    "agent_shell_probe/raw_events.jsonl": "agent_shell_probe_events",
    "raw_events.jsonl": "agent_events",
    "agent_attempts.json": "agent_attempts",
    "metrics.json": "metrics",
    "settings_ref.json": "settings",
    "effective_run_spec.json": "effective_run_spec",
    "error.json": "error",
    "workspace_ref.json": "workspace",
    "target_ref.json": "target",
    "run_meta.json": "run_meta",
    "report.json": "report",
    "normalized_events.jsonl": "normalized_events",
}

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

_CONTEXT_DIAGNOSTIC_FIELDS = (
    "status",
    "present",
    "usable",
    "reason",
    "reason_code",
    "reason_type",
    "remediation",
    "resolved_path",
    "probe_exit_code",
    "probe_stdout_excerpt",
    "probe_stderr_excerpt",
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|credential|authorization|cookie|session)"
    r"\s*[:=]\s*[^\s,;]+"
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


def source_observation_classification(atom: Mapping[str, Any]) -> dict[str, Any]:
    """Hash a runner-owned source/derived classification for a full evidence atom."""

    evidence_role = _text(atom.get("evidence_role"))
    origin_stage = _text(atom.get("origin_stage"))
    excluded_stages = {
        "repro_research",
        "research",
        "implementation",
        "verification",
    }
    proposal_kinds = {
        "idea",
        "proposal",
        "recommendation",
        "suggested_change",
        "suggestion",
    }
    classification: dict[str, Any] = {
        "schema_version": 1,
        "evidence_role": evidence_role,
        "origin_stage": origin_stage,
        "is_source_observation": (
            evidence_role == "observation"
            and (origin_stage or "").casefold() not in excluded_stages
            and not any(
                str(atom.get(field) or "").strip().casefold() in proposal_kinds
                for field in ("category", "kind", "source", "surface_kind")
            )
        ),
    }
    classification["classification_sha256"] = _canonical_sha256(classification)
    return classification


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
    if candidate.is_symlink():
        return None
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
    if candidate.is_symlink():
        return None, "attachment_artifact_symlink_rejected"
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


def _safe_context_text(value: Any, *, limit: int = 1000) -> str | None:
    text = _text(value)
    if text is None:
        return None
    redacted = _SENSITIVE_TEXT.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return redacted[:limit]


def _safe_context_path(value: Any, *, suffix_parts: int = 4) -> str | None:
    """Retain a useful path suffix without exposing a host workspace location."""

    text = _safe_context_text(value, limit=2_000)
    if text is None:
        return None
    normalized = text.replace("\\", "/")
    absolute = normalized.startswith("/") or bool(
        re.match(r"^[A-Za-z]:/", normalized)
    )
    if not absolute:
        return normalized
    parts = [part for part in normalized.split("/") if part and not part.endswith(":")]
    suffix = "/".join(parts[-max(1, suffix_parts) :])
    return f"<absolute>/{suffix}"


def _project_agent_events(raw: bytes) -> dict[str, Any]:
    """Project provider tool order while excluding unbounded tool-result content.

    The sequence is material evidence when a model reads a repository source and then
    executes a command copied from it.  Raw provider streams can also contain prompts,
    file contents, credentials, and long model messages, so only a small set of
    redacted tool inputs and command-execution fields are retained.
    """

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return {
            "projection_status": "invalid_jsonl",
            "error_type": type(exc).__name__,
        }

    projected_events: list[dict[str, Any]] = []
    invalid_line_count = 0
    event_count = 0
    claude_tool_use_count = 0
    command_execution_count = 0
    file_read_count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid_line_count += 1
            continue
        if not isinstance(event, Mapping):
            invalid_line_count += 1
            continue
        event_count += 1
        event_type = _safe_context_text(event.get("type"), limit=128)

        # Claude emits assistant message blocks with typed tool_use records.
        message_raw = event.get("message")
        message = message_raw if isinstance(message_raw, Mapping) else {}
        content_raw = message.get("content")
        content = content_raw if isinstance(content_raw, list) else []
        for block_raw in content:
            block = block_raw if isinstance(block_raw, Mapping) else {}
            if block.get("type") != "tool_use":
                continue
            tool_name = _safe_context_text(block.get("name"), limit=128)
            tool_input_raw = block.get("input")
            tool_input = tool_input_raw if isinstance(tool_input_raw, Mapping) else {}
            projected: dict[str, Any] = {
                "event_ordinal": event_count,
                "provider_event_type": event_type,
                "tool_use_id": _safe_context_text(block.get("id"), limit=128),
                "tool_name": tool_name,
            }
            file_path = _safe_context_path(tool_input.get("file_path"))
            path = _safe_context_path(tool_input.get("path"))
            command = _safe_context_text(tool_input.get("command"), limit=2_000)
            pattern = _safe_context_text(tool_input.get("pattern"), limit=500)
            if file_path is not None:
                projected["file_path"] = file_path
            if path is not None:
                projected["path"] = path
            if command is not None:
                projected["command"] = command
            if pattern is not None:
                projected["pattern"] = pattern
            projected_events.append(
                {key: value for key, value in projected.items() if value is not None}
            )
            claude_tool_use_count += 1
            if str(tool_name or "").casefold() == "read" and (
                file_path is not None or path is not None
            ):
                file_read_count += 1

        # Codex emits command_execution items. Keep both lifecycle state and the
        # sanitized command so ordering remains inspectable without model prose.
        item_raw = event.get("item")
        item = item_raw if isinstance(item_raw, Mapping) else {}
        if item.get("type") == "command_execution":
            command = _safe_context_text(item.get("command"), limit=2_000)
            projected_events.append(
                {
                    key: value
                    for key, value in {
                        "event_ordinal": event_count,
                        "provider_event_type": event_type,
                        "item_id": _safe_context_text(item.get("id"), limit=128),
                        "item_type": "command_execution",
                        "command": command,
                        "status": _safe_context_text(item.get("status"), limit=128),
                        "exit_code": (
                            item.get("exit_code")
                            if item.get("exit_code") is None
                            or isinstance(item.get("exit_code"), int)
                            else None
                        ),
                    }.items()
                    if value is not None
                }
            )
            command_execution_count += 1

    retained_events = projected_events
    events_truncated = len(projected_events) > 128
    if events_truncated:
        retained_events = [*projected_events[:32], *projected_events[-96:]]
    return {
        "projection_status": "projected",
        "content": {
            "event_count": event_count,
            "invalid_line_count": invalid_line_count,
            "claude_tool_use_count": claude_tool_use_count,
            "command_execution_count": command_execution_count,
            "file_read_count": file_read_count,
            "projected_tool_event_count": len(projected_events),
            "retained_tool_event_count": len(retained_events),
            "events_truncated": events_truncated,
            "tool_events": retained_events,
        },
    }


def _selected_fields(value: Any, fields: Sequence[str]) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    selected: dict[str, Any] = {}
    for field in fields:
        raw = source.get(field)
        if isinstance(raw, str):
            cleaned = _safe_context_text(raw)
            if cleaned is not None:
                selected[field] = cleaned
        elif raw is None or isinstance(raw, (bool, int, float)):
            selected[field] = raw
    return selected


def _diagnostic_projection(value: Any) -> dict[str, dict[str, Any]]:
    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, dict[str, Any]] = {}
    for name, raw in sorted(source.items(), key=lambda item: str(item[0])):
        if not isinstance(raw, Mapping):
            continue
        fields = _selected_fields(raw, _CONTEXT_DIAGNOSTIC_FIELDS)
        status = str(fields.get("status") or "").casefold()
        if (
            fields.get("usable") is True
            and not fields.get("reason")
            and status in {"", "available", "ok", "ready", "usable"}
        ):
            continue
        projected[str(name)[:128]] = fields
        if len(projected) >= 48:
            break
    return projected


def _scalar_tree(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, Mapping) or depth >= 2:
        return None
    result: dict[str, Any] = {}
    for key, child in sorted(value.items(), key=lambda item: str(item[0])):
        key_text = str(key)
        if re.search(r"(?i)(key|token|secret|password|credential|authorization|cookie)", key_text):
            continue
        projected = _scalar_tree(child, depth=depth + 1)
        if projected is not None:
            result[key_text[:128]] = projected
        if len(result) >= 48:
            break
    return result


def _bounded_context_tree(value: Any, *, depth: int = 0) -> Any:
    """Project mixed report data without copying secrets or unbounded payloads."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_context_text(value, limit=2_000)
    if depth >= 4:
        return None
    if isinstance(value, list):
        projected = [
            item
            for child in value[:16]
            if (item := _bounded_context_tree(child, depth=depth + 1)) is not None
        ]
        return projected
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key, child in sorted(value.items(), key=lambda item: str(item[0])):
        key_text = str(key)
        if re.search(r"(?i)(key|token|secret|password|credential|authorization|cookie)", key_text):
            continue
        projected = _bounded_context_tree(child, depth=depth + 1)
        if projected is not None:
            result[key_text[:128]] = projected
        if len(result) >= 32:
            break
    return result


def _project_context_json(role: str, value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    if role == "preflight":
        meta = source.get("meta") if isinstance(source.get("meta"), Mapping) else {}
        return {
            "mission_requirements": _scalar_tree(source.get("mission_requirements")),
            "capabilities": _scalar_tree(source.get("capabilities")),
            "required_agent_binary": _safe_context_text(source.get("required_agent_binary")),
            "required_agent_binary_present": source.get("required_agent_binary_present"),
            "warnings": [
                text for raw in (source.get("warnings") or [])[:16]
                if (text := _safe_context_text(raw, limit=500)) is not None
            ],
            "command_diagnostics": _diagnostic_projection(source.get("command_diagnostics")),
            "command_probe_diagnostics": _diagnostic_projection(
                meta.get("command_probe_details") if isinstance(meta, Mapping) else None
            ),
        }
    if role == "agent_attempts":
        attempts_raw = source.get("attempts")
        attempts = attempts_raw if isinstance(attempts_raw, list) else []
        projected = {
            "attempts": [{
                **_selected_fields(attempt, (
                    "attempt", "attempt_wall_seconds", "agent_exec_wall_seconds",
                    "exit_code", "failure_subtype",
                )),
                "verification": _selected_fields(attempt.get("verification"), (
                    "status", "passed", "source", "failure_reason", "terminal_reason",
                )),
            } for attempt in attempts[:16] if isinstance(attempt, Mapping)],
            "followup_attempts_used": source.get("followup_attempts_used"),
            "rate_limit_retries_used": source.get("rate_limit_retries_used"),
        }
        return projected
    if role == "settings":
        settings = source.get("settings") if isinstance(source.get("settings"), Mapping) else {}
        applied = settings.get("applied") if isinstance(settings.get("applied"), Mapping) else {}
        return {
            "profile": _safe_context_text(settings.get("profile")),
            "auto_loaded": settings.get("auto_loaded"),
            "applied": _selected_fields(
                applied,
                (
                    "exec_backend",
                    "exec_docker_profile",
                    "exec_keep_container",
                    "exec_use_host_agent_login",
                    "exec_use_target_sandbox_cli_install",
                    "keep_workspace",
                    "policy",
                    "mission_id",
                    "persona_id",
                    "model",
                    "dry_run",
                    "skip_verify",
                    "verification_profile",
                ),
            ),
        }
    if role == "effective_run_spec":
        return _selected_fields(source, (
            "execution_mode", "mission_id", "mission_name", "persona_id", "persona_name",
        ))
    if role == "error":
        return _selected_fields(source, ("type", "subtype", "exit_code", "stderr_synthesized"))
    if role == "workspace":
        workspace_dir = _safe_context_text(source.get("workspace_dir"))
        return {
            **_selected_fields(
                source,
                (
                    "schema_version",
                    "workspace_id",
                    "keep_workspace_requested",
                    "will_cleanup_workspace",
                ),
            ),
            "workspace_volume": Path(workspace_dir).anchor if workspace_dir is not None else None,
        }
    if role == "target":
        return _selected_fields(source, (
            "commit_sha", "acquire_mode", "agent", "policy", "seed", "model",
            "model_source", "persona_id", "mission_id",
        ))
    if role == "run_meta":
        return {
            **_selected_fields(
                source,
                ("schema_version", "run_started_utc", "run_finished_utc", "run_wall_seconds"),
            ),
            "phases": _scalar_tree(source.get("phases")),
        }
    if role == "metrics":
        projected = _scalar_tree(source)
        return projected if isinstance(projected, dict) else {}
    if role == "report":
        selected: dict[str, Any] = _selected_fields(
            source,
            ("schema_version", "kind", "status", "confidence", "summary", "failure_point"),
        )
        for field in (
            "baseline",
            "final_result",
            "adoption_decision",
            "issues",
            "confidence_signals",
            "verification",
        ):
            if field not in source:
                continue
            projected = _bounded_context_tree(source.get(field))
            if projected is not None:
                selected[field] = projected
        return selected
    return {}


def _context_projection(role: str, raw: bytes) -> dict[str, Any]:
    if role == "agent_events":
        return _project_agent_events(raw)
    if role == "agent_shell_probe_events":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            return {
                "projection_status": "invalid_jsonl",
                "error_type": type(exc).__name__,
            }

        projected_events: list[dict[str, Any]] = []
        invalid_line_count = 0
        event_count = 0
        command_execution_count = 0
        completed_command_count = 0
        successful_command_count = 0
        nonempty_command_output_count = 0
        turn_completed_count = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid_line_count += 1
                continue
            if not isinstance(event, Mapping):
                invalid_line_count += 1
                continue
            event_count += 1
            event_type = _safe_context_text(event.get("type"), limit=128)
            item_raw = event.get("item")
            item = item_raw if isinstance(item_raw, Mapping) else {}
            item_type = _safe_context_text(item.get("type"), limit=128)
            if item_type == "command_execution":
                command_execution_count += 1
                status = _safe_context_text(item.get("status"), limit=128)
                exit_code = item.get("exit_code")
                if event_type == "item.completed" or status == "completed":
                    completed_command_count += 1
                if exit_code == 0 and status == "completed":
                    successful_command_count += 1
                outputs = {
                    field: cleaned
                    for field in ("aggregated_output", "stdout", "output", "stderr")
                    if (cleaned := _safe_context_text(item.get(field), limit=2000))
                    is not None
                }
                if outputs:
                    nonempty_command_output_count += 1
                projected_events.append(
                    {
                        "event_type": event_type,
                        "item_id": _safe_context_text(item.get("id"), limit=128),
                        "item_type": item_type,
                        "command": _safe_context_text(item.get("command"), limit=1000),
                        "status": status,
                        "exit_code": exit_code
                        if exit_code is None or isinstance(exit_code, int)
                        else None,
                        **outputs,
                    }
                )
            elif item_type == "agent_message":
                projected_events.append(
                    {
                        "event_type": event_type,
                        "item_id": _safe_context_text(item.get("id"), limit=128),
                        "item_type": item_type,
                        "text": _safe_context_text(item.get("text"), limit=1000),
                    }
                )
            elif event_type == "turn.completed":
                turn_completed_count += 1
                projected_events.append({"event_type": event_type})

        retained_events = projected_events
        events_truncated = len(projected_events) > 64
        if events_truncated:
            retained_events = [*projected_events[:16], *projected_events[-48:]]
        return {
            "projection_status": "projected",
            "content": {
                "event_count": event_count,
                "invalid_line_count": invalid_line_count,
                "command_execution_count": command_execution_count,
                "completed_command_count": completed_command_count,
                "successful_command_count": successful_command_count,
                "nonempty_command_output_count": nonempty_command_output_count,
                "turn_completed_count": turn_completed_count,
                "projected_event_count": len(projected_events),
                "retained_event_count": len(retained_events),
                "events_truncated": events_truncated,
                "events": retained_events,
            },
        }
    if role == "normalized_events":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            return {
                "projection_status": "invalid_jsonl",
                "error_type": type(exc).__name__,
            }

        commands: list[dict[str, Any]] = []
        invalid_line_count = 0
        event_count = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid_line_count += 1
                continue
            if not isinstance(event, Mapping):
                invalid_line_count += 1
                continue
            event_count += 1
            if _safe_context_text(event.get("type"), limit=128) != "run_command":
                continue
            data_raw = event.get("data")
            data = data_raw if isinstance(data_raw, Mapping) else {}
            command = _safe_context_text(data.get("command"), limit=2_000)
            if command is None:
                argv = data.get("argv")
                if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
                    command = _safe_context_text(" ".join(argv), limit=2_000)
            exit_code = data.get("exit_code")
            if command is None or not isinstance(exit_code, int):
                continue
            projected: dict[str, Any] = {
                "command_ordinal": len(commands) + 1,
                "timestamp_utc": _safe_context_text(event.get("ts"), limit=128),
                "command": command,
                "exit_code": exit_code,
            }
            output_excerpt = _safe_context_text(data.get("output_excerpt"), limit=2_000)
            if output_excerpt is not None:
                projected["output_excerpt"] = output_excerpt
            commands.append(projected)

        retained_commands = commands
        commands_truncated = len(commands) > 64
        if commands_truncated:
            retained_commands = [*commands[:16], *commands[-48:]]
        return {
            "projection_status": "projected",
            "content": {
                "event_count": event_count,
                "invalid_line_count": invalid_line_count,
                "command_count": len(commands),
                "failed_command_count": sum(item["exit_code"] != 0 for item in commands),
                "successful_command_count": sum(item["exit_code"] == 0 for item in commands),
                "retained_command_count": len(retained_commands),
                "commands_truncated": commands_truncated,
                "commands": retained_commands,
            },
        }
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"projection_status": "invalid_json", "error_type": type(exc).__name__}
    return {
        "projection_status": "projected",
        "content": _project_context_json(role, value),
    }


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


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip())
    )


def _assigned_atom_prompt_projection(
    atom: Mapping[str, Any],
    *,
    atom_sha256: str,
    decision_atom: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return bounded orientation metadata while retaining the complete atom on disk."""

    text = _text(atom.get("text"))
    error_raw = atom.get("error")
    error = error_raw if isinstance(error_raw, Mapping) else {}
    error_message = _text(error.get("message"))
    symptom: dict[str, Any] = {
        "text_excerpt": (
            text[:ASSIGNED_EVIDENCE_SYMPTOM_EXCERPT_CHARS] if text is not None else None
        ),
        "text_sha256": sha256(text.encode("utf-8")).hexdigest() if text is not None else None,
        "text_truncated": (
            len(text) > ASSIGNED_EVIDENCE_SYMPTOM_EXCERPT_CHARS
            if text is not None
            else False
        ),
        "failure_kind": _text(atom.get("failure_kind")),
        "status": _text(atom.get("status")),
        "error_type": _text(error.get("type")),
        "error_subtype": _text(error.get("subtype")),
        "error_code": _text(error.get("code")),
        "error_message_excerpt": (
            error_message[:ASSIGNED_EVIDENCE_SYMPTOM_EXCERPT_CHARS]
            if error_message is not None
            else None
        ),
        "error_message_sha256": (
            sha256(error_message.encode("utf-8")).hexdigest()
            if error_message is not None
            else None
        ),
    }
    symptom = {key: value for key, value in symptom.items() if value is not None}
    decision = decision_atom if isinstance(decision_atom, Mapping) else atom
    lineage = {
        "evidence_class": _text(atom.get("evidence_class")),
        "evidence_role": _text(atom.get("evidence_role")),
        "origin_stage": _text(atom.get("origin_stage")),
        "origin_run_id": _text(atom.get("origin_run_id")),
        "parent_case_id": _text(atom.get("parent_case_id")),
        "parent_problem_id": _text(atom.get("parent_problem_id")),
        "derived_from_atom_ids": _string_list(atom.get("derived_from_atom_ids")),
        "disposition": _text(decision.get("disposition")),
        "disposition_status": _text(decision.get("disposition_status")),
    }
    return {
        "atom_id": _text(atom.get("atom_id")),
        "atom_sha256": atom_sha256,
        "symptom": symptom,
        "lineage": {
            key: value
            for key, value in lineage.items()
            if value is not None and value != []
        },
    }


def _materialize_assigned_evidence(
    *,
    atoms: Sequence[Mapping[str, Any]],
    evidence_assignment: Mapping[str, Any] | None,
    workspace: Path,
    relative_root: Path,
) -> dict[str, Any]:
    """Persist complete assigned evidence plus a bounded, hash-bound prompt index."""

    assignment = dict(evidence_assignment) if isinstance(evidence_assignment, Mapping) else {}
    # The attachment manifest is composed after this source assignment is written.
    # Excluding a pre-existing copy also prevents recursive assignment/manifest hashes.
    assignment.pop("origin_attachment_evidence", None)
    destination = (workspace / relative_root / "assigned").resolve()
    if not _path_within(destination, workspace):
        raise ValueError("assigned_evidence_destination_outside_workspace")
    atoms_dir = destination / "atoms"
    receipts_dir = destination / "receipts"
    atoms_dir.mkdir(parents=True, exist_ok=True)
    receipts_dir.mkdir(parents=True, exist_ok=True)

    assignment_rel = relative_root / "assigned" / "assignment.json"
    assignment_bytes = _json_bytes(assignment)
    (workspace / assignment_rel).write_bytes(assignment_bytes)

    receipts_raw = assignment.get("atom_receipts")
    receipts = receipts_raw if isinstance(receipts_raw, list) else []
    receipts_by_id = {
        atom_id: receipt
        for receipt in receipts
        if isinstance(receipt, Mapping)
        for atom_id in [_text(receipt.get("atom_id"))]
        if atom_id is not None
    }
    case_ids = set(_string_list(assignment.get("case_evidence_atom_ids")))
    occurrence_ids = set(_string_list(assignment.get("occurrence_evidence_atom_ids")))
    entries: list[dict[str, Any]] = []
    seen_atom_ids: set[str] = set()
    for atom in atoms:
        atom_id = _text(atom.get("atom_id"))
        if atom_id is None or atom_id in seen_atom_ids:
            continue
        seen_atom_ids.add(atom_id)
        context_atom = dict(atom)
        receipt = receipts_by_id.get(atom_id)
        authoritative_atom = source_evidence_atom_projection(context_atom)
        if isinstance(receipt, Mapping):
            source_classification = receipt.get("source_classification")
            if (
                source_classification is not None
                and source_classification != source_observation_classification(context_atom)
            ):
                raise ValueError(
                    f"assigned_evidence_source_classification_mismatch:{atom_id}"
                )
            snapshot_raw = receipt.get("atom_snapshot")
            if not isinstance(snapshot_raw, Mapping):
                raise ValueError(f"assigned_evidence_atom_snapshot_missing:{atom_id}")
            authoritative_atom = dict(snapshot_raw)
            assigned_atom_sha = _text(receipt.get("atom_sha256"))
            if (
                authoritative_atom.get("atom_id") != atom_id
                or assigned_atom_sha != _canonical_sha256(authoritative_atom)
                or source_evidence_atom_projection(context_atom) != authoritative_atom
            ):
                raise ValueError(f"assigned_evidence_atom_snapshot_mismatch:{atom_id}")

        atom_digest = _canonical_sha256(authoritative_atom)
        atom_rel = relative_root / "assigned" / "atoms" / f"{atom_digest}.json"
        atom_bytes = _json_bytes(authoritative_atom)
        (workspace / atom_rel).write_bytes(atom_bytes)

        context_digest = _canonical_sha256(context_atom)
        if context_digest == atom_digest:
            context_rel = atom_rel
            context_bytes = atom_bytes
        else:
            context_rel = (
                relative_root / "assigned" / "atoms" / f"{context_digest}.json"
            )
            context_bytes = _json_bytes(context_atom)
            (workspace / context_rel).write_bytes(context_bytes)

        receipt_fields: dict[str, Any] = {}
        if isinstance(receipt, Mapping):
            receipt_projection = dict(receipt)
            receipt_digest = _canonical_sha256(receipt_projection)
            receipt_rel = (
                relative_root / "assigned" / "receipts" / f"{receipt_digest}.json"
            )
            receipt_bytes = _json_bytes(receipt_projection)
            (workspace / receipt_rel).write_bytes(receipt_bytes)
            receipt_fields = {
                "receipt_file": receipt_rel.as_posix(),
                "receipt_file_sha256": sha256(receipt_bytes).hexdigest(),
                "receipt_file_size_bytes": len(receipt_bytes),
                "assigned_atom_sha256": atom_digest,
                "origin_evidence_mode": _text(receipt.get("origin_evidence_mode")),
                "source_classification": receipt.get("source_classification"),
                "artifact_receipt_count": len(
                    receipt.get("artifact_receipts")
                    if isinstance(receipt.get("artifact_receipts"), list)
                    else []
                ),
            }
        entry = {
            **_assigned_atom_prompt_projection(
                authoritative_atom,
                atom_sha256=atom_digest,
                decision_atom=context_atom,
            ),
            "assignment_role": (
                "case_evidence"
                if atom_id in case_ids
                else "occurrence_evidence"
                if atom_id in occurrence_ids
                else "supplemental_evidence"
            ),
            "atom_file": atom_rel.as_posix(),
            "atom_file_sha256": sha256(atom_bytes).hexdigest(),
            "atom_file_size_bytes": len(atom_bytes),
            "context_atom_sha256": context_digest,
            "context_atom_file": context_rel.as_posix(),
            "context_atom_file_sha256": sha256(context_bytes).hexdigest(),
            "context_atom_file_size_bytes": len(context_bytes),
            **receipt_fields,
        }
        entries.append(entry)

    entries.sort(key=lambda item: str(item.get("atom_id")))
    index: dict[str, Any] = {
        "schema_version": ASSIGNED_EVIDENCE_SCHEMA_VERSION,
        "format": "hash_bound_assigned_evidence_index_v1",
        "case_id": _text(assignment.get("case_id")),
        "problem_id": _text(assignment.get("problem_id")),
        "assignment_status": _text(assignment.get("status")),
        "assignment_errors": assignment.get("errors")
        if isinstance(assignment.get("errors"), list)
        else [],
        "assignment_sha256": _text(assignment.get("assignment_sha256")),
        "assignment_file": assignment_rel.as_posix(),
        "assignment_file_sha256": sha256(assignment_bytes).hexdigest(),
        "assignment_file_size_bytes": len(assignment_bytes),
        "expected_atom_ids": _string_list(assignment.get("expected_atom_ids")),
        "expected_atom_count": len(_string_list(assignment.get("expected_atom_ids"))),
        "case_evidence_atom_ids": sorted(case_ids),
        "case_evidence_atom_count": len(case_ids),
        "occurrence_evidence_atom_ids": sorted(occurrence_ids),
        "occurrence_evidence_atom_count": len(occurrence_ids),
        "provisional_same_cause_member_evidence_atom_ids": sorted(
            _string_list(
                assignment.get("provisional_same_cause_member_evidence_atom_ids")
            )
        ),
        "materialized_atom_count": len(entries),
        "materialized_receipt_count": sum("receipt_file" in entry for entry in entries),
        "atoms": entries,
    }
    index["materialization_sha256"] = _canonical_sha256(index)
    index_rel = relative_root / "assigned" / "index.json"
    index_bytes = _json_bytes(index)
    (workspace / index_rel).write_bytes(index_bytes)
    return {
        **index,
        "index_file": index_rel.as_posix(),
        "index_file_sha256": sha256(index_bytes).hexdigest(),
        "index_file_size_bytes": len(index_bytes),
    }


def _project_run_context(
    *,
    evidence_assignment: Mapping[str, Any] | None,
    source_root: Path | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(evidence_assignment, Mapping):
        return None, []
    receipts_raw = evidence_assignment.get("atom_receipts")
    receipts = receipts_raw if isinstance(receipts_raw, list) else []
    grouped: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []

    def reject(atom_id: str, error: str, role: str | None = None) -> None:
        item: dict[str, Any] = {"atom_id": atom_id, "attachment_index": -1, "error": error}
        if role is not None:
            item["context_role"] = role
        errors.append(item)

    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        atom_id = _text(receipt.get("atom_id"))
        snapshot = receipt.get("atom_snapshot")
        run_dir = (
            _resolved_run_dir(snapshot, source_root=source_root)
            if isinstance(snapshot, Mapping)
            else None
        )
        artifacts_raw = receipt.get("artifact_receipts")
        artifacts = artifacts_raw if isinstance(artifacts_raw, list) else []
        context_artifacts = [
            artifact
            for artifact in artifacts
            if isinstance(artifact, Mapping)
            and _text(artifact.get("research_context_role"))
            in set(RESEARCH_RUN_CONTEXT_FILES.values())
        ]
        if not context_artifacts:
            continue
        if atom_id is None or run_dir is None:
            reject(atom_id or "unknown", "run_context_source_boundary_missing")
            continue
        group = grouped.setdefault(
            str(run_dir),
            {"atom_ids": set(), "sources": {}},
        )
        group["atom_ids"].add(atom_id)
        for artifact in context_artifacts:
            role = str(artifact["research_context_role"])
            rel_raw = _text(artifact.get("source_relpath"))
            path_raw = _text(artifact.get("path"))
            rel = Path(rel_raw) if rel_raw is not None else None
            candidate = Path(path_raw).expanduser() if path_raw is not None else None
            if candidate is not None and not candidate.is_absolute():
                candidate = run_dir / candidate
            try:
                if candidate is None or candidate.is_symlink():
                    raise ValueError
                source = candidate.resolve() if candidate is not None else None
                if (
                    source is None
                    or rel is None
                    or rel.is_absolute()
                    or RESEARCH_RUN_CONTEXT_FILES.get(rel.as_posix()) != role
                    or not _path_within(source, run_dir)
                    or not source.is_file()
                    or source.relative_to(run_dir) != rel
                ):
                    raise ValueError
            except (OSError, ValueError):
                reject(atom_id, "run_context_source_invalid", role)
                continue
            size = source.stat().st_size
            digest = _file_sha256(source)
            if digest != artifact.get("sha256") or size != artifact.get("size_bytes"):
                reject(atom_id, "run_context_source_hash_mismatch", role)
                continue
            projection = (
                _context_projection(role, source.read_bytes())
                if size <= RUN_CONTEXT_SOURCE_MAX_BYTES
                else {
                    "projection_status": "source_too_large",
                    "source_max_bytes": RUN_CONTEXT_SOURCE_MAX_BYTES,
                }
            )
            source_record = {
                "source_name": rel.as_posix(),
                "role": role,
                "source_sha256": digest,
                "source_size_bytes": size,
                "projection_version": RUN_CONTEXT_SCHEMA_VERSION,
                "projection": projection,
            }
            source_record["projection_sha256"] = _canonical_sha256(projection)
            group["sources"][(role, digest)] = source_record

    runs: list[dict[str, Any]] = []
    for group in grouped.values():
        sources = sorted(
            group["sources"].values(),
            key=lambda item: (item["role"], item["source_sha256"]),
        )
        atom_ids = sorted(group["atom_ids"])
        run = {"atom_ids": atom_ids, "sources": sources}
        run["context_id"] = _canonical_sha256(run)
        runs.append(run)
    runs.sort(key=lambda item: item["context_id"])
    if not runs:
        return None, errors
    payload = {
        "schema_version": RUN_CONTEXT_SCHEMA_VERSION,
        "format": "bounded_source_run_context_v1",
        "source_run_count": len(runs),
        "source_artifact_count": sum(len(run["sources"]) for run in runs),
        "runs": runs,
    }
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > RUN_CONTEXT_INDEX_MAX_BYTES:
        # Preserve one mandatory context index rather than dropping all source-run
        # evidence. The original projection hashes remain bound to the raw receipts;
        # only verbose projected bodies are reduced to a structural inventory.
        source_set_hashes = {
            str(run["context_id"]): _canonical_sha256(run["sources"])
            for run in payload["runs"]
        }
        for run in payload["runs"]:
            for source in run["sources"]:
                projection = source.get("projection")
                content = projection.get("content") if isinstance(projection, Mapping) else None
                source["projection"] = {
                    "projection_status": "compacted_for_index_budget",
                    "full_projection_sha256": source.get("projection_sha256"),
                    "retained_content_fields": sorted(map(str, content))[:64]
                    if isinstance(content, Mapping)
                    else [],
                }
        payload["index_compacted"] = True
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > RUN_CONTEXT_INDEX_MAX_BYTES:
            # A large assignment can contain dozens of independent source runs. Even
            # the field-level projection inventory above then repeats enough
            # per-source structure to exceed the bounded model-facing index. Keep a
            # deterministic run inventory instead. The context and source-set hashes
            # still bind every omitted source record, while the main attachment
            # manifest retains the exact source receipts and raw artifact hashes.
            payload["runs"] = [
                {
                    "atom_ids": list(run["atom_ids"]),
                    "context_id": run["context_id"],
                    "source_artifact_count": len(run["sources"]),
                    "source_roles": sorted(
                        {
                            str(source["role"])
                            for source in run["sources"]
                            if isinstance(source, Mapping) and source.get("role")
                        }
                    ),
                    "source_set_sha256": source_set_hashes[str(run["context_id"])],
                }
                for run in payload["runs"]
            ]
            payload["index_compaction"] = "run_inventory_v1"
            encoded = (
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
    if len(encoded) > RUN_CONTEXT_INDEX_MAX_BYTES:
        reject("multiple", "run_context_compact_index_exceeds_max_bytes")
        return None, errors
    return payload, errors


def _materialize_run_context(
    *,
    evidence_assignment: Mapping[str, Any] | None,
    workspace: Path,
    relative_root: Path,
    source_root: Path | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    payload, errors = _project_run_context(
        evidence_assignment=evidence_assignment,
        source_root=source_root,
    )
    if payload is None:
        return None, errors
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    rel = relative_root / "run_context" / "index.json"
    path = (workspace / rel).resolve()
    if not _path_within(path, workspace):
        raise ValueError("run_context_destination_outside_workspace")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return {
        **payload,
        "materialization_sha256": _canonical_sha256(payload),
        "index_file": rel.as_posix(),
        "index_file_sha256": sha256(encoded).hexdigest(),
        "index_file_size_bytes": len(encoded),
    }, errors


def materialize_origin_attachments(
    *,
    atoms: Sequence[Mapping[str, Any]],
    workspace_dir: Path,
    source_root: Path | None,
    evidence_assignment: Mapping[str, Any] | None = None,
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

    run_context, run_context_errors = _materialize_run_context(
        evidence_assignment=evidence_assignment,
        workspace=workspace,
        relative_root=relative_root,
        source_root=source_root,
    )
    errors.extend(run_context_errors)
    assigned_evidence = _materialize_assigned_evidence(
        atoms=atoms,
        evidence_assignment=evidence_assignment,
        workspace=workspace,
        relative_root=relative_root,
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
        "run_context": run_context,
        "assigned_evidence": assigned_evidence,
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
    context_raw = manifest.get("run_context")
    context = context_raw if isinstance(context_raw, Mapping) else {}
    runs_raw = context.get("runs")
    runs = runs_raw if isinstance(runs_raw, list) else []
    context_selected = not selected or any(
        isinstance(run, Mapping)
        and any(atom_id in selected for atom_id in (run.get("atom_ids") or []))
        for run in runs
    )
    context_path = _text(context.get("index_file"))
    context_sha = _text(context.get("index_file_sha256"))
    if context_selected and context_path is not None and _valid_sha256(context_sha):
        requirements.append(
            {
                "artifact_sha256": context_sha,
                "file": context_path,
                "sha256": context_sha,
                "size_bytes": context.get("index_file_size_bytes"),
                "representation": "json",
                "content_role": "source_run_context_index",
            }
        )
    assigned_raw = manifest.get("assigned_evidence")
    assigned = assigned_raw if isinstance(assigned_raw, Mapping) else {}
    assigned_path = _text(assigned.get("index_file"))
    assigned_sha = _text(assigned.get("index_file_sha256"))
    if assigned_path is not None and _valid_sha256(assigned_sha):
        requirements.append(
            {
                "artifact_sha256": assigned_sha,
                "file": assigned_path,
                "sha256": assigned_sha,
                "size_bytes": assigned.get("index_file_size_bytes"),
                "representation": "json",
                "content_role": "assigned_evidence_index",
            }
        )
    role_order = {
        "source_run_context_index": 1,
        "assigned_evidence_index": 2,
    }
    return sorted(
        requirements,
        key=lambda item: (
            role_order.get(str(item.get("content_role")), 0),
            item["artifact_sha256"],
            item["file"],
        ),
    )


def origin_attachment_read_scope(
    manifest: Mapping[str, Any],
    *,
    dossier: Mapping[str, Any],
    verification: Mapping[str, Any] | None = None,
    observed_files: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the bounded Stage-3 read contract for retained origin evidence.

    Materialization integrity still covers every retained byte.  Model observation is
    selective: the compact assigned-evidence and source-run indexes are mandatory, while raw
    chunks become mandatory only when the authored dossier explicitly declares their
    workspace path (or artifact digest) in ``artifact_refs``.  This keeps unclaimed evidence
    available without making an agent reread an entire historical corpus for every case.
    """

    requirements = origin_attachment_requirements(manifest)
    by_file = {str(requirement["file"]): requirement for requirement in requirements}
    mandatory_files = {
        path
        for path, requirement in by_file.items()
        if requirement.get("content_role")
        in {"assigned_evidence_index", "source_run_context_index"}
    }

    declared_paths: set[str] = set()
    declared_digests: set[str] = set()
    artifact_refs_raw = dossier.get("artifact_refs")
    for artifact_ref in (
        artifact_refs_raw if isinstance(artifact_refs_raw, list) else []
    ):
        if not isinstance(artifact_ref, Mapping):
            continue
        path = _text(artifact_ref.get("path"))
        if path is not None:
            declared_paths.add(path.replace("\\", "/").casefold())
        for field in ("artifact_sha256", "sha256"):
            digest = _text(artifact_ref.get(field))
            if _valid_sha256(digest):
                declared_digests.add(str(digest))

    claim_bound_files: set[str] = set()
    for path, requirement in by_file.items():
        normalized = path.replace("\\", "/").casefold()
        if any(
            declared == normalized or declared.endswith("/" + normalized)
            for declared in declared_paths
        ) or str(requirement.get("artifact_sha256")) in declared_digests:
            claim_bound_files.add(path)

    selection_errors: list[str] = []
    verification_map = verification if isinstance(verification, Mapping) else {}
    bindings_raw = verification_map.get("atom_bindings")
    artifact_only_bindings = [
        binding
        for binding in (bindings_raw if isinstance(bindings_raw, list) else [])
        if isinstance(binding, Mapping)
        and binding.get("match_kind")
        in {
            "command_and_artifact_symptom_text",
            "faithful_artifact_symptom_text",
        }
        and _text(binding.get("origin_atom_field_path")) is None
    ]
    for binding in artifact_only_bindings:
        artifact_sha = _text(binding.get("origin_artifact_sha256"))
        declared_for_artifact = {
            path
            for path in claim_bound_files
            if by_file[path].get("artifact_sha256") == artifact_sha
        }
        if not declared_for_artifact:
            selection_errors.append(
                "origin_attachment_artifact_only_binding_missing_declared_chunk:"
                + str(binding.get("atom_id") or "unknown")
                + ":"
                + str(binding.get("experiment_id") or "unknown")
            )

    required_files = mandatory_files | claim_bound_files
    observed = {str(path) for path in observed_files if str(path) in by_file}
    missing_required = required_files - observed
    unread_optional = set(by_file) - required_files - observed
    if missing_required:
        coverage_status = "required_reads_missing"
    elif unread_optional:
        coverage_status = "required_reads_complete_with_unread_optional_evidence"
    else:
        coverage_status = "all_available_evidence_read"
    scope: dict[str, Any] = {
        "schema_version": 1,
        "policy": "mandatory_indexes_plus_claim_bound_chunks_v1",
        "available_file_count": len(by_file),
        "mandatory_files": sorted(mandatory_files),
        "claim_bound_files": sorted(claim_bound_files),
        "required_files": sorted(required_files),
        "observed_files": sorted(observed),
        "missing_required_files": sorted(missing_required),
        "unread_optional_file_count": len(unread_optional),
        "unread_optional_files_sha256": _canonical_sha256(sorted(unread_optional)),
        "coverage_status": coverage_status,
        "selection_errors": list(dict.fromkeys(selection_errors)),
    }
    scope["scope_sha256"] = _canonical_sha256(scope)
    return scope


def _verify_materialized_assigned_evidence(
    *,
    workspace: Path,
    assigned: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    index_projection = {
        key: value
        for key, value in assigned.items()
        if key not in {"index_file", "index_file_sha256", "index_file_size_bytes"}
    }
    materialization_projection = {
        key: value
        for key, value in index_projection.items()
        if key != "materialization_sha256"
    }
    if assigned.get("materialization_sha256") != _canonical_sha256(
        materialization_projection
    ):
        errors.append("assigned_evidence_materialization_hash_changed")

    index_rel = _text(assigned.get("index_file"))
    index_path = (workspace / Path(index_rel)).resolve() if index_rel else None
    if (
        index_path is None
        or not _path_within(index_path, workspace)
        or not index_path.is_file()
    ):
        errors.append("assigned_evidence_index_missing")
    else:
        index_bytes = index_path.read_bytes()
        if (
            sha256(index_bytes).hexdigest() != assigned.get("index_file_sha256")
            or len(index_bytes) != assigned.get("index_file_size_bytes")
        ):
            errors.append("assigned_evidence_index_changed")
        else:
            try:
                parsed_index = json.loads(index_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                errors.append("assigned_evidence_index_unreadable")
            else:
                if parsed_index != index_projection:
                    errors.append("assigned_evidence_index_projection_changed")

    assignment_rel = _text(assigned.get("assignment_file"))
    assignment_path = (
        (workspace / Path(assignment_rel)).resolve() if assignment_rel else None
    )
    assignment: dict[str, Any] | None = None
    if (
        assignment_path is None
        or not _path_within(assignment_path, workspace)
        or not assignment_path.is_file()
    ):
        errors.append("assigned_evidence_assignment_missing")
    else:
        assignment_bytes = assignment_path.read_bytes()
        if (
            sha256(assignment_bytes).hexdigest()
            != assigned.get("assignment_file_sha256")
            or len(assignment_bytes) != assigned.get("assignment_file_size_bytes")
        ):
            errors.append("assigned_evidence_assignment_changed")
        else:
            try:
                parsed_assignment = json.loads(assignment_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                errors.append("assigned_evidence_assignment_unreadable")
            else:
                if isinstance(parsed_assignment, dict):
                    assignment = parsed_assignment
                else:
                    errors.append("assigned_evidence_assignment_invalid")
    if assignment is not None:
        assignment_sha = _text(assignment.get("assignment_sha256"))
        if assignment_sha is not None and assignment_sha != _canonical_sha256(
            {key: value for key, value in assignment.items() if key != "assignment_sha256"}
        ):
            errors.append("assigned_evidence_assignment_self_hash_changed")
        if assignment_sha != assigned.get("assignment_sha256"):
            errors.append("assigned_evidence_assignment_hash_changed")
        assignment_projection = {
            "case_id": _text(assignment.get("case_id")),
            "problem_id": _text(assignment.get("problem_id")),
            "assignment_status": _text(assignment.get("status")),
            "assignment_errors": (
                assignment.get("errors") if isinstance(assignment.get("errors"), list) else []
            ),
            "expected_atom_ids": _string_list(assignment.get("expected_atom_ids")),
            "case_evidence_atom_ids": sorted(
                _string_list(assignment.get("case_evidence_atom_ids"))
            ),
            "occurrence_evidence_atom_ids": sorted(
                _string_list(assignment.get("occurrence_evidence_atom_ids"))
            ),
            "provisional_same_cause_member_evidence_atom_ids": sorted(
                _string_list(
                    assignment.get("provisional_same_cause_member_evidence_atom_ids")
                )
            ),
        }
        assigned_projection = {
            key: assigned.get(key) for key in assignment_projection
        }
        if assigned_projection != assignment_projection:
            errors.append("assigned_evidence_assignment_projection_changed")
        if assigned.get("expected_atom_count") != len(
            assignment_projection["expected_atom_ids"]
        ):
            errors.append("assigned_evidence_expected_atom_count_changed")
        if assigned.get("case_evidence_atom_count") != len(
            assignment_projection["case_evidence_atom_ids"]
        ):
            errors.append("assigned_evidence_case_atom_count_changed")
        if assigned.get("occurrence_evidence_atom_count") != len(
            assignment_projection["occurrence_evidence_atom_ids"]
        ):
            errors.append("assigned_evidence_occurrence_atom_count_changed")

    entries_raw = assigned.get("atoms")
    entries = entries_raw if isinstance(entries_raw, list) else []
    observed_atom_ids: list[str] = []
    observed_receipts = 0
    for entry_raw in entries:
        if not isinstance(entry_raw, Mapping):
            errors.append("assigned_evidence_atom_entry_invalid")
            continue
        entry = dict(entry_raw)
        atom_id = _text(entry.get("atom_id"))
        if atom_id is None:
            errors.append("assigned_evidence_atom_id_missing")
            continue
        observed_atom_ids.append(atom_id)
        atom_rel = _text(entry.get("atom_file"))
        atom_path = (workspace / Path(atom_rel)).resolve() if atom_rel else None
        atom: dict[str, Any] | None = None
        if (
            atom_path is None
            or not _path_within(atom_path, workspace)
            or not atom_path.is_file()
        ):
            errors.append(f"assigned_evidence_atom_missing:{atom_id}")
        else:
            atom_bytes = atom_path.read_bytes()
            if (
                sha256(atom_bytes).hexdigest() != entry.get("atom_file_sha256")
                or len(atom_bytes) != entry.get("atom_file_size_bytes")
            ):
                errors.append(f"assigned_evidence_atom_changed:{atom_id}")
            else:
                try:
                    parsed_atom = json.loads(atom_bytes)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    errors.append(f"assigned_evidence_atom_unreadable:{atom_id}")
                else:
                    if isinstance(parsed_atom, dict):
                        atom = parsed_atom
                    else:
                        errors.append(f"assigned_evidence_atom_invalid:{atom_id}")
        context_rel = _text(entry.get("context_atom_file"))
        context_path = (
            (workspace / Path(context_rel)).resolve() if context_rel else None
        )
        context_atom: dict[str, Any] | None = None
        if (
            context_path is None
            or not _path_within(context_path, workspace)
            or not context_path.is_file()
        ):
            errors.append(f"assigned_evidence_context_atom_missing:{atom_id}")
        else:
            context_bytes = context_path.read_bytes()
            if (
                sha256(context_bytes).hexdigest()
                != entry.get("context_atom_file_sha256")
                or len(context_bytes) != entry.get("context_atom_file_size_bytes")
            ):
                errors.append(f"assigned_evidence_context_atom_changed:{atom_id}")
            else:
                try:
                    parsed_context = json.loads(context_bytes)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    errors.append(f"assigned_evidence_context_atom_unreadable:{atom_id}")
                else:
                    if isinstance(parsed_context, dict):
                        context_atom = parsed_context
                    else:
                        errors.append(f"assigned_evidence_context_atom_invalid:{atom_id}")
        if (
            atom is not None
            and (
                atom.get("atom_id") != atom_id
                or _canonical_sha256(atom) != entry.get("atom_sha256")
                or context_atom is None
                or context_atom.get("atom_id") != atom_id
                or _canonical_sha256(context_atom) != entry.get("context_atom_sha256")
                or source_evidence_atom_projection(context_atom) != atom
                or _assigned_atom_prompt_projection(
                    atom,
                    atom_sha256=str(entry.get("atom_sha256")),
                    decision_atom=context_atom,
                )
                != {
                    key: entry.get(key)
                    for key in ("atom_id", "atom_sha256", "symptom", "lineage")
                }
            )
        ):
            errors.append(f"assigned_evidence_atom_projection_changed:{atom_id}")

        receipt_rel = _text(entry.get("receipt_file"))
        if receipt_rel is None:
            continue
        observed_receipts += 1
        receipt_path = (workspace / Path(receipt_rel)).resolve()
        if not _path_within(receipt_path, workspace) or not receipt_path.is_file():
            errors.append(f"assigned_evidence_receipt_missing:{atom_id}")
            continue
        receipt_bytes = receipt_path.read_bytes()
        if (
            sha256(receipt_bytes).hexdigest() != entry.get("receipt_file_sha256")
            or len(receipt_bytes) != entry.get("receipt_file_size_bytes")
        ):
            errors.append(f"assigned_evidence_receipt_changed:{atom_id}")
            continue
        try:
            receipt = json.loads(receipt_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append(f"assigned_evidence_receipt_unreadable:{atom_id}")
            continue
        if (
            not isinstance(receipt, dict)
            or receipt.get("atom_id") != atom_id
            or receipt.get("atom_sha256") != entry.get("assigned_atom_sha256")
            or receipt.get("atom_sha256") != entry.get("atom_sha256")
            or receipt.get("atom_snapshot") != atom
                or entry.get("origin_evidence_mode")
                != _text(receipt.get("origin_evidence_mode"))
                or entry.get("source_classification")
                != receipt.get("source_classification")
                or (
                    receipt.get("source_classification") is not None
                    and receipt.get("source_classification")
                    != source_observation_classification(context_atom or {})
                )
                or entry.get("artifact_receipt_count")
            != len(
                receipt.get("artifact_receipts")
                if isinstance(receipt.get("artifact_receipts"), list)
                else []
            )
        ):
            errors.append(f"assigned_evidence_receipt_projection_changed:{atom_id}")

    if len(observed_atom_ids) != len(set(observed_atom_ids)):
        errors.append("assigned_evidence_atom_ids_duplicate")
    if len(entries) != assigned.get("materialized_atom_count"):
        errors.append("assigned_evidence_atom_count_changed")
    if observed_receipts != assigned.get("materialized_receipt_count"):
        errors.append("assigned_evidence_receipt_count_changed")
    expected_ids = _string_list(assigned.get("expected_atom_ids"))
    if not set(expected_ids).issubset(observed_atom_ids):
        errors.append("assigned_evidence_expected_atom_coverage_changed")
    return errors


def verify_materialized_origin_attachments(
    *,
    workspace_dir: Path,
    manifest: Mapping[str, Any],
    evidence_assignment: Mapping[str, Any] | None = None,
    source_root: Path | None = None,
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
    assigned_raw = manifest.get("assigned_evidence")
    if assigned_raw is not None and not isinstance(assigned_raw, Mapping):
        errors.append("assigned_evidence_manifest_invalid")
    elif isinstance(assigned_raw, Mapping):
        errors.extend(
            _verify_materialized_assigned_evidence(
                workspace=workspace,
                assigned=assigned_raw,
            )
        )
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
    context_raw = manifest.get("run_context")
    if context_raw is not None:
        if not isinstance(context_raw, Mapping):
            errors.append("origin_run_context_manifest_invalid")
        else:
            context_payload = {
                key: value
                for key, value in context_raw.items()
                if key
                not in {
                    "materialization_sha256",
                    "index_file",
                    "index_file_sha256",
                    "index_file_size_bytes",
                }
            }
            if context_raw.get("materialization_sha256") != _canonical_sha256(context_payload):
                errors.append("origin_run_context_manifest_hash_changed")
            if evidence_assignment is not None:
                projected, projection_errors = _project_run_context(
                    evidence_assignment=evidence_assignment,
                    source_root=source_root,
                )
                errors.extend(
                    f"origin_run_context_source_projection_failed:{item.get('error', 'unknown')}"
                    for item in projection_errors
                )
                if projected != context_payload:
                    errors.append("origin_run_context_source_projection_mismatch")
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
    "RESEARCH_RUN_CONTEXT_FILES",
    "RUN_CONTEXT_SCHEMA_VERSION",
    "materialize_origin_attachments",
    "origin_attachment_requirements",
    "verify_materialized_origin_attachments",
]
