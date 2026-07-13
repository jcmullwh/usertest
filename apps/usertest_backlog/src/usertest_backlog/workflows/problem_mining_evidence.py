"""Runner-owned evidence and decision receipts for problem mining.

Stage 1 is only complete when every atom that was eligible to originate a case has
been read in full and receives exactly one final disposition.  Model prose is not an
attestation: this module derives read evidence from normalized tool events, binds it
to retained files, and revalidates the resulting receipt before shadow activation or
ticket export.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID

from agent_adapters import (
    normalize_claude_events,
    normalize_codex_events,
    normalize_gemini_events,
)
from backlog_core.case_lineage import (
    apply_atom_disposition_decision,
    atom_disposition_receipt_errors,
    atom_is_idea_originated,
)
from backlog_miner.origin_evidence import (
    origin_attachment_requirements,
    verify_materialized_origin_attachments,
)

PROBLEM_MINING_EVIDENCE_SCHEMA_VERSION = 1
_DERIVED_EVIDENCE_ROLES = frozenset({"research", "implementation", "verification"})
_FINAL_DISPOSITIONS = frozenset(
    {"supports_case", "duplicate", "expected_noise", "deferred", "unresolved"}
)
_NON_SUPPORT_DISPOSITIONS = _FINAL_DISPOSITIONS - {"supports_case"}
_CROSS_JOB_ROUTING_KEY_MIN = 2
_CROSS_JOB_ROUTING_KEY_MAX = 5
_CROSS_JOB_ROUTING_KEY_CHARS = 80
STAGE1_ATOM_DECISION_FIELDS = frozenset(
    {
        "case_id",
        "disposition_decision_error",
        "supporting_case_ids",
        "disposition",
        "disposition_status",
        "disposition_receipt",
        "disposition_rationale",
        "disposition_proof",
        "disposition_revisit_when",
        "novel_case_rationale",
        # These fields describe workflow/lifecycle processing, not the observed
        # evidence. Including their wall-clock values made a separately prepared
        # qualification corpus differ from the live run despite identical source
        # bytes and also made nondeterministic correction history reset a streak.
        "qualification_repair_history",
        "status_reopen_audit",
    }
)


class ProblemMiningEvidenceReceipt(TypedDict):
    """Persisted internal contract for one complete stage-1 evidence pass."""

    schema_version: int
    receipt_kind: str
    mode: str
    status: str
    eligible_for_shadow_export: bool
    eligible_atom_ids: list[str]
    eligible_source_atom_ids: list[str]
    eligible_derived_atom_ids: list[str]
    eligible_corpus_sha256: str
    atom_evidence: list[dict[str, Any]]
    miners: list[dict[str, Any]]
    decision_partition: list[dict[str, Any]]
    receipt_sha256: str


class ProblemMiningResponseContractError(ValueError):
    """The miner returned a response that cannot satisfy the stage-1 contract."""


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip())
    )


def _routing_decision_keys(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else []
    return list(
        dict.fromkeys(
            "-".join(item.casefold().split())
            for item in raw
            if isinstance(item, str)
            and item.strip()
            and len(item.strip()) <= _CROSS_JOB_ROUTING_KEY_CHARS
        )
    )


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _receipt_hash(receipt: Mapping[str, Any]) -> str:
    return _canonical_hash(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )


def immutable_atom_evidence_projection(atom: Mapping[str, Any]) -> dict[str, Any]:
    """Return immutable evidence content, excluding stage-1 decisions."""

    projection = {
        key: value
        for key, value in sorted(atom.items(), key=lambda item: str(item[0]))
        if key not in STAGE1_ATOM_DECISION_FIELDS
    }
    if _text(projection.get("evidence_class")) is None:
        projection["evidence_class"] = (
            "proposal" if _text(projection.get("source")) == "suggested_change" else "observed"
        )
    return projection


def _atom_evidence_row(atom: Mapping[str, Any]) -> dict[str, Any]:
    atom_id = _text(atom.get("atom_id"))
    if atom_id is None:
        raise ValueError("problem_mining_evidence_atom_id_missing")
    role = _text(atom.get("evidence_role")) or "observation"
    return {
        "atom_id": atom_id,
        "evidence_role": role,
        "is_source_observation": role not in _DERIVED_EVIDENCE_ROLES,
        "evidence_sha256": _canonical_hash(immutable_atom_evidence_projection(atom)),
    }


def build_problem_mining_evidence_draft(
    *,
    atoms: Sequence[Mapping[str, Any]],
    eligible_atoms: Sequence[Mapping[str, Any]],
    mode: str,
) -> dict[str, Any]:
    """Bind the exact runner-selected eligible corpus before any model call."""

    if mode not in {"live", "dry_run"}:
        raise ValueError(f"problem_mining_evidence_mode_invalid:{mode}")
    all_by_id: dict[str, Mapping[str, Any]] = {}
    for atom in atoms:
        atom_id = _text(atom.get("atom_id"))
        if atom_id is None:
            raise ValueError("problem_mining_evidence_atom_id_missing")
        if atom_id in all_by_id:
            raise ValueError(f"problem_mining_evidence_atom_id_duplicate:{atom_id}")
        all_by_id[atom_id] = atom

    rows: list[dict[str, Any]] = []
    eligible_ids: list[str] = []
    source_ids: list[str] = []
    derived_ids: list[str] = []
    for atom in eligible_atoms:
        atom_id = _text(atom.get("atom_id"))
        if atom_id is None or atom_id not in all_by_id:
            raise ValueError(f"problem_mining_evidence_eligible_atom_unknown:{atom_id}")
        if atom_id in eligible_ids:
            raise ValueError(f"problem_mining_evidence_eligible_atom_duplicate:{atom_id}")
        row = _atom_evidence_row(atom)
        rows.append(row)
        eligible_ids.append(atom_id)
        if row["is_source_observation"]:
            source_ids.append(atom_id)
        else:
            derived_ids.append(atom_id)

    rows.sort(key=lambda item: str(item["atom_id"]))
    eligible_ids.sort()
    source_ids.sort()
    derived_ids.sort()
    return {
        "schema_version": PROBLEM_MINING_EVIDENCE_SCHEMA_VERSION,
        "receipt_kind": "problem_mining_evidence",
        "mode": mode,
        "status": "corpus_bound",
        "eligible_for_shadow_export": False,
        "eligible_atom_ids": eligible_ids,
        "eligible_source_atom_ids": source_ids,
        "eligible_derived_atom_ids": derived_ids,
        "eligible_corpus_sha256": _canonical_hash(rows),
        "atom_evidence": rows,
        "miners": [],
        "decision_partition": [],
    }


def normalize_problem_mining_events(
    *,
    agent: str,
    raw_events_path: Path,
    normalized_events_path: Path,
    workspace_dir: Path,
) -> None:
    """Normalize one miner transcript with workspace-aware read attestations."""

    if agent == "codex":
        normalize_codex_events(
            raw_events_path=raw_events_path,
            normalized_events_path=normalized_events_path,
            workspace_root=workspace_dir,
        )
    elif agent == "claude":
        normalize_claude_events(
            raw_events_path=raw_events_path,
            normalized_events_path=normalized_events_path,
            workspace_root=workspace_dir,
        )
    elif agent == "gemini":
        normalize_gemini_events(
            raw_events_path=raw_events_path,
            normalized_events_path=normalized_events_path,
            workspace_root=workspace_dir,
        )
    else:
        raise ValueError(f"problem_mining_evidence_agent_unsupported:{agent}")


def parse_problem_mining_response_envelope(text: str) -> dict[str, Any]:
    """Parse the complete strict stage-1 response object from model output.

    Stage 1 promises JSON-only output. Salvaging a nested object from a malformed
    envelope can turn the real syntax error into a misleading schema failure and can
    discard otherwise useful diagnostics. Parse the whole response once and retain
    the exact location of any JSON error instead.
    """

    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProblemMiningResponseContractError(
            "problem_mining_response_json_invalid:"
            f"{exc.msg}:line={exc.lineno}:column={exc.colno}:char={exc.pos}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ProblemMiningResponseContractError("problem_mining_response_envelope_required")
    if set(parsed) != {"problem_records", "atom_decisions"}:
        raise ProblemMiningResponseContractError("problem_mining_response_envelope_fields_invalid")
    if not isinstance(parsed.get("problem_records"), list) or not isinstance(
        parsed.get("atom_decisions"), list
    ):
        raise ProblemMiningResponseContractError("problem_mining_response_envelope_lists_required")
    return dict(parsed)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"problem_mining_normalized_event_invalid:{line_number}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"problem_mining_normalized_event_not_object:{line_number}")
            events.append(raw)
    return events


def _event_read_attestations(
    *,
    events: Sequence[Mapping[str, Any]],
    atom_files: Mapping[str, Mapping[str, Any]],
    chunks: Sequence[Mapping[str, Any]],
    workspace_dir: Path,
) -> dict[str, dict[str, Any]]:
    evidence_files: list[tuple[str, str, str, list[str]]] = []
    for atom_id, atom_file in atom_files.items():
        rel_path = _text(atom_file.get("file"))
        expected_sha = _text(atom_file.get("sha256"))
        if rel_path is not None and expected_sha is not None:
            evidence_files.append(("atom", rel_path, expected_sha, [atom_id]))
    for raw_chunk in chunks:
        atom_ids = _string_list(raw_chunk.get("atom_ids"))
        for file_kind, path_key, hash_key in (
            ("chunk_json", "file", "sha256"),
            ("chunk_markdown", "text_file", "text_sha256"),
        ):
            rel_path = _text(raw_chunk.get(path_key))
            expected_sha = _text(raw_chunk.get(hash_key))
            if rel_path is not None and expected_sha is not None and atom_ids:
                evidence_files.append((file_kind, rel_path, expected_sha, atom_ids))

    by_atom: dict[str, dict[str, Any]] = {}
    for event_index, raw_event in enumerate(events):
        if raw_event.get("type") != "read_file":
            continue
        data_raw = raw_event.get("data")
        data = data_raw if isinstance(data_raw, Mapping) else {}
        event_path = _text(data.get("path"))
        if (
            event_path is None
            or data.get("content_observed") is not True
            or data.get("whole_file_observed") is not True
            or data.get("source_exit_code") != 0
        ):
            continue
        normalized_event_path = event_path.replace("\\", "/").casefold()
        for file_kind, rel_path, expected_sha, atom_ids in evidence_files:
            normalized_rel = rel_path.replace("\\", "/").casefold()
            if not (
                normalized_event_path == normalized_rel
                or normalized_event_path.endswith("/" + normalized_rel)
            ):
                continue
            path = workspace_dir / Path(rel_path)
            if (
                not path.is_file()
                or _file_sha256(path) != expected_sha
                or data.get("file_sha256") != expected_sha
            ):
                continue
            for atom_id in atom_ids:
                by_atom.setdefault(
                    atom_id,
                    {
                        "atom_id": atom_id,
                        "atom_file": rel_path,
                        "atom_file_sha256": expected_sha,
                        "evidence_file_kind": file_kind,
                        "event_index": event_index,
                        "event_sha256": _canonical_hash(dict(raw_event)),
                        "observed_content_sha256": data.get("observed_content_sha256"),
                        "observed_bytes": data.get("observed_bytes"),
                    },
                )
    return by_atom


def _event_required_workspace_read_attestations(
    *,
    events: Sequence[Mapping[str, Any]],
    workspace_manifest: Mapping[str, Any],
    workspace_dir: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Bind mandatory manifest, index, and full chunk reads to exact events/bytes."""

    required: list[tuple[str, str]] = [("manifest", "atoms.json")]
    index_file = _text(workspace_manifest.get("index_file"))
    if index_file is not None:
        required.append(("index", index_file))
    chunks_raw = workspace_manifest.get("chunks")
    if isinstance(chunks_raw, list):
        for raw_chunk in chunks_raw:
            if not isinstance(raw_chunk, Mapping):
                continue
            text_file = _text(raw_chunk.get("text_file"))
            if text_file is not None:
                required.append(("chunk_markdown", text_file))

    attestations: list[dict[str, Any]] = []
    missing: list[str] = []
    workspace_resolved = workspace_dir.resolve()
    for file_kind, rel_path in required:
        path = (workspace_dir / Path(rel_path)).resolve()
        try:
            path.relative_to(workspace_resolved)
        except ValueError:
            missing.append(rel_path)
            continue
        if not path.is_file():
            missing.append(rel_path)
            continue
        expected_sha = _file_sha256(path)
        expected_size = path.stat().st_size
        matched: dict[str, Any] | None = None
        normalized_rel = rel_path.replace("\\", "/").casefold()
        for event_index, raw_event in enumerate(events):
            if raw_event.get("type") != "read_file":
                continue
            data_raw = raw_event.get("data")
            data = data_raw if isinstance(data_raw, Mapping) else {}
            event_path = _text(data.get("path"))
            normalized_event_path = (event_path or "").replace("\\", "/").casefold()
            if not (
                normalized_event_path == normalized_rel
                or normalized_event_path.endswith("/" + normalized_rel)
            ):
                continue
            if (
                data.get("content_observed") is not True
                or data.get("whole_file_observed") is not True
                or data.get("source_exit_code") != 0
                or data.get("file_sha256") != expected_sha
            ):
                continue
            event_size = data.get("file_size_bytes", data.get("observed_bytes"))
            if event_size is not None and event_size != expected_size:
                continue
            matched = {
                "file_kind": file_kind,
                "file": rel_path,
                "file_sha256": expected_sha,
                "file_size_bytes": expected_size,
                "event_index": event_index,
                "event_sha256": _canonical_hash(dict(raw_event)),
            }
            break
        if matched is None:
            missing.append(rel_path)
        else:
            attestations.append(matched)
    return attestations, missing


def _event_origin_attachment_attestations(
    *,
    events: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    workspace_dir: Path,
    assigned_atom_ids: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    requirements = origin_attachment_requirements(
        manifest,
        atom_ids=assigned_atom_ids,
    )
    refs_raw = manifest.get("atom_refs")
    refs = refs_raw if isinstance(refs_raw, list) else []
    atoms_by_artifact: dict[str, set[str]] = {}
    assigned = set(assigned_atom_ids)
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        atom_id = _text(ref.get("atom_id"))
        artifact_sha = _text(ref.get("artifact_sha256"))
        if atom_id in assigned and artifact_sha is not None:
            atoms_by_artifact.setdefault(artifact_sha, set()).add(atom_id)

    attestations: list[dict[str, Any]] = []
    observed_files: set[str] = set()
    for requirement in requirements:
        rel_path = str(requirement["file"])
        expected_sha = str(requirement["sha256"])
        path = (workspace_dir / Path(rel_path)).resolve()
        for event_index, raw_event in enumerate(events):
            if raw_event.get("type") != "read_file":
                continue
            data_raw = raw_event.get("data")
            data = data_raw if isinstance(data_raw, Mapping) else {}
            event_path = _text(data.get("path"))
            if event_path is None:
                continue
            normalized_event_path = event_path.replace("\\", "/").casefold()
            normalized_rel = rel_path.replace("\\", "/").casefold()
            if not (
                normalized_event_path == normalized_rel
                or normalized_event_path.endswith("/" + normalized_rel)
            ):
                continue
            if (
                not path.is_file()
                or _file_sha256(path) != expected_sha
                or data.get("content_observed") is not True
                or data.get("whole_file_observed") is not True
                or data.get("source_exit_code") != 0
                or data.get("file_sha256") != expected_sha
                or data.get("file_size_bytes") != requirement.get("size_bytes")
            ):
                continue
            attestations.append(
                {
                    "artifact_sha256": requirement["artifact_sha256"],
                    "atom_ids": sorted(
                        atoms_by_artifact.get(str(requirement["artifact_sha256"]), set())
                    ),
                    "file": rel_path,
                    "file_sha256": expected_sha,
                    "file_size_bytes": requirement.get("size_bytes"),
                    "event_index": event_index,
                    "event_sha256": _canonical_hash(dict(raw_event)),
                    "observed_content_sha256": data.get("observed_content_sha256"),
                    "observed_bytes": data.get("observed_bytes"),
                }
            )
            observed_files.add(rel_path)
            break
    missing = sorted(
        str(requirement["file"])
        for requirement in requirements
        if str(requirement["file"]) not in observed_files
    )
    return attestations, missing


def _workspace_atoms_by_id(
    *,
    workspace_manifest: Mapping[str, Any],
    workspace_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Load the exact hash-bound atom payloads used by one mining job."""

    atoms: dict[str, dict[str, Any]] = {}
    chunks_raw = workspace_manifest.get("chunks")
    chunks = chunks_raw if isinstance(chunks_raw, list) else []
    for index, raw_chunk in enumerate(chunks):
        if not isinstance(raw_chunk, Mapping):
            raise ValueError(f"problem_mining_workspace_chunk_invalid:{index}")
        rel_path = _text(raw_chunk.get("file"))
        expected_sha = _text(raw_chunk.get("sha256"))
        path = workspace_dir / Path(rel_path or "")
        if (
            rel_path is None
            or expected_sha is None
            or not path.is_file()
            or _file_sha256(path) != expected_sha
        ):
            raise ValueError(f"problem_mining_workspace_chunk_changed:{index}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"problem_mining_workspace_chunk_unreadable:{index}") from exc
        if not isinstance(payload, list):
            raise ValueError(f"problem_mining_workspace_chunk_not_list:{index}")
        for raw_atom in payload:
            if not isinstance(raw_atom, Mapping):
                raise ValueError(f"problem_mining_workspace_atom_invalid:{index}")
            atom_id = _text(raw_atom.get("atom_id"))
            if atom_id is None or atom_id in atoms:
                raise ValueError(f"problem_mining_workspace_atom_identity_invalid:{atom_id}")
            atoms[atom_id] = dict(raw_atom)
    return atoms


def _runner_expected_noise_proof(atom: Mapping[str, Any]) -> dict[str, Any] | None:
    """Mint the only currently supported permanent noise proof.

    Proposal evidence is not an observed problem. Everything else remains reconsiderable;
    model agreement alone is not authority to suppress an observation forever.
    """

    atom_id = _text(atom.get("atom_id"))
    explicit_class = _text(atom.get("evidence_class"))
    if atom_id is None:
        return None
    if explicit_class == "proposal":
        support: dict[str, Any] = {"field": "$.evidence_class", "value": "proposal"}
    elif _text(atom.get("source")) == "suggested_change":
        support = {"field": "$.source", "value": "suggested_change"}
    else:
        return None
    support["value_sha256"] = _canonical_hash(support["value"])
    proof: dict[str, Any] = {
        "schema_version": 1,
        "producer": "usertest_backlog.problem_mining",
        "proof_kind": "runner_expected_noise_rule_v1",
        "rule_id": "proposal_evidence_class_v1",
        "rule_version": 1,
        "atom_id": atom_id,
        "support": support,
    }
    proof["proof_sha256"] = _canonical_hash(proof)
    return proof


def build_live_miner_receipt(
    *,
    tag: str,
    template_name: str,
    assigned_atom_ids: Sequence[str],
    eligible_atom_ids: Sequence[str],
    records: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    response_text: str,
    normalized_events_path: Path,
    workspace_dir: Path,
    workspace_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one miner's exact assignment, citations, and full-read evidence."""

    assigned = sorted(set(assigned_atom_ids))
    if assigned != sorted(assigned_atom_ids):
        raise ValueError(f"problem_mining_assignment_invalid:{tag}")
    manifest_assigned_raw = workspace_manifest.get("assigned_atom_ids")
    manifest_assigned = _string_list(manifest_assigned_raw)
    if (
        not isinstance(manifest_assigned_raw, list)
        or len(manifest_assigned) != len(manifest_assigned_raw)
        or manifest_assigned != assigned
    ):
        raise ValueError(f"problem_mining_workspace_assignment_mismatch:{tag}")
    context_raw = workspace_manifest.get("context_atom_ids", [])
    context_atom_ids = _string_list(context_raw)
    if (
        not isinstance(context_raw, list)
        or len(context_atom_ids) != len(context_raw)
        or context_atom_ids != sorted(context_atom_ids)
    ):
        raise ValueError(f"problem_mining_workspace_context_invalid:{tag}")
    if set(assigned) & set(context_atom_ids):
        raise ValueError(f"problem_mining_workspace_assignment_context_overlap:{tag}")
    workspace_atom_ids = set(assigned) | set(context_atom_ids)
    eligible = set(eligible_atom_ids)
    if not set(assigned).issubset(eligible):
        raise ValueError(f"problem_mining_assignment_outside_eligible_corpus:{tag}")
    atom_files_raw = workspace_manifest.get("atom_files")
    atom_files_list = atom_files_raw if isinstance(atom_files_raw, list) else []
    atom_files: dict[str, dict[str, Any]] = {}
    for raw in atom_files_list:
        if not isinstance(raw, Mapping):
            continue
        atom_id = _text(raw.get("atom_id"))
        if atom_id is not None:
            atom_files[atom_id] = dict(raw)
    if len(atom_files) != len(atom_files_list) or set(atom_files) != workspace_atom_ids:
        raise ValueError(f"problem_mining_workspace_atom_partition_mismatch:{tag}")
    chunks_raw = workspace_manifest.get("chunks")
    chunks = (
        [dict(raw) for raw in chunks_raw if isinstance(raw, Mapping)]
        if isinstance(chunks_raw, list)
        else []
    )
    chunk_atom_ids = [
        atom_id for chunk in chunks for atom_id in _string_list(chunk.get("atom_ids"))
    ]
    if set(chunk_atom_ids) != workspace_atom_ids or len(chunk_atom_ids) != len(
        set(chunk_atom_ids)
    ):
        raise ValueError(f"problem_mining_workspace_chunk_partition_mismatch:{tag}")
    workspace_atoms = _workspace_atoms_by_id(
        workspace_manifest=workspace_manifest,
        workspace_dir=workspace_dir,
    )
    if set(workspace_atoms) != workspace_atom_ids:
        raise ValueError(f"problem_mining_workspace_payload_partition_mismatch:{tag}")
    if not normalized_events_path.is_file():
        raise ValueError(f"problem_mining_normalized_events_missing:{tag}")
    events = _load_jsonl(normalized_events_path)
    reads = _event_read_attestations(
        events=events,
        atom_files=atom_files,
        chunks=chunks,
        workspace_dir=workspace_dir,
    )
    required_workspace_reads, missing_workspace_reads = _event_required_workspace_read_attestations(
        events=events,
        workspace_manifest=workspace_manifest,
        workspace_dir=workspace_dir,
    )
    if missing_workspace_reads:
        raise ValueError(
            f"problem_mining_required_evidence_file_not_read_in_full:{tag}:"
            + ",".join(missing_workspace_reads)
        )
    origin_manifest_raw = workspace_manifest.get("origin_attachment_evidence")
    origin_manifest = dict(origin_manifest_raw) if isinstance(origin_manifest_raw, Mapping) else {}
    materialization_errors = [
        dict(error)
        for error in origin_manifest.get("errors", [])
        if isinstance(error, Mapping) and _text(error.get("atom_id")) in set(assigned)
    ]
    attachment_reads, missing_attachment_reads = _event_origin_attachment_attestations(
        events=events,
        manifest=origin_manifest,
        workspace_dir=workspace_dir,
        assigned_atom_ids=sorted(workspace_atom_ids),
    )
    if missing_attachment_reads:
        raise ValueError(
            f"problem_mining_origin_attachment_not_read_in_full:{tag}:"
            + ",".join(missing_attachment_reads)
        )

    routing_only = template_name.endswith("cross_job_routing")
    if routing_only and records:
        raise ValueError(f"problem_mining_routing_records_not_empty:{tag}")

    record_ids: set[str] = set()
    record_evidence: dict[str, set[str]] = {}
    cited_ids: set[str] = set()
    for index, raw_record in enumerate(records):
        problem_id = _text(raw_record.get("problem_id"))
        if problem_id is None or problem_id in record_ids:
            raise ValueError(f"problem_mining_problem_id_invalid:{tag}:{index}")
        record_ids.add(problem_id)
        evidence_ids = set(_string_list(raw_record.get("evidence_atom_ids")))
        if not evidence_ids or not evidence_ids.issubset(set(assigned)):
            raise ValueError(f"problem_mining_citation_outside_eligible_corpus:{tag}:{problem_id}")
        preview_only = sorted(evidence_ids - set(reads))
        if preview_only:
            raise ValueError(
                f"problem_mining_preview_only_citation:{tag}:{problem_id}:" + ",".join(preview_only)
            )
        record_evidence[problem_id] = evidence_ids
        cited_ids.update(evidence_ids)

    normalized_decisions: list[dict[str, Any]] = []
    decision_ids: list[str] = []
    decisions_by_atom: dict[str, dict[str, Any]] = {}
    for index, raw_decision in enumerate(decisions):
        if not isinstance(raw_decision, Mapping):
            raise ValueError(f"problem_mining_atom_decision_invalid:{tag}:{index}")
        expected_decision_fields = {
            "atom_id",
            "disposition",
            "problem_ids",
            "rationale",
            "revisit_when",
        }
        if routing_only:
            expected_decision_fields.add("routing_keys")
        if set(raw_decision) - expected_decision_fields:
            raise ValueError(f"problem_mining_atom_decision_fields_invalid:{tag}:{index}")
        atom_id = _text(raw_decision.get("atom_id"))
        disposition = _text(raw_decision.get("disposition"))
        rationale = _text(raw_decision.get("rationale"))
        revisit_when = _text(raw_decision.get("revisit_when"))
        problem_ids = sorted(_string_list(raw_decision.get("problem_ids")))
        if atom_id is None or disposition not in _FINAL_DISPOSITIONS or rationale is None:
            raise ValueError(f"problem_mining_atom_decision_invalid:{tag}:{index}")
        if atom_id not in assigned:
            raise ValueError(f"problem_mining_atom_decision_outside_assignment:{tag}:{atom_id}")
        routing_keys = _routing_decision_keys(raw_decision.get("routing_keys"))
        if routing_only and not (
            _CROSS_JOB_ROUTING_KEY_MIN <= len(routing_keys) <= _CROSS_JOB_ROUTING_KEY_MAX
        ):
            raise ValueError(f"problem_mining_routing_decision_keys_invalid:{tag}:{atom_id}")
        if routing_only and (
            disposition != "unresolved" or problem_ids or revisit_when is not None
        ):
            raise ValueError(f"problem_mining_routing_decision_not_neutral:{tag}:{atom_id}")
        disposition_proof: dict[str, Any] | None = None
        if not routing_only and disposition == "duplicate":
            disposition = "deferred"
            rationale = (
                "A model-only duplicate label cannot permanently suppress observed evidence. "
                "Mine a case and let canonical relation review bind an exact target. "
                f"Original assessment: {rationale}"
            )
            revisit_when = "A canonical duplicate relation receipt names the target case."
            problem_ids = []
        elif not routing_only and disposition == "expected_noise":
            disposition_proof = _runner_expected_noise_proof(workspace_atoms[atom_id])
            if disposition_proof is None:
                disposition = "deferred"
                rationale = (
                    "No runner-owned expected-noise rule supports permanent suppression. "
                    f"Original assessment: {rationale}"
                )
                revisit_when = (
                    "New evidence establishes a case or a versioned runner noise rule applies."
                )
                problem_ids = []
        if disposition == "supports_case":
            if not problem_ids or any(problem_id not in record_ids for problem_id in problem_ids):
                raise ValueError(f"problem_mining_support_problem_invalid:{tag}:{atom_id}")
            if any(atom_id not in record_evidence[problem_id] for problem_id in problem_ids):
                raise ValueError(f"problem_mining_support_citation_missing:{tag}:{atom_id}")
        elif problem_ids:
            raise ValueError(f"problem_mining_non_support_has_problem_ids:{tag}:{atom_id}")
        if disposition == "deferred" and revisit_when is None:
            raise ValueError(f"problem_mining_deferred_revisit_missing:{tag}:{atom_id}")
        if disposition != "deferred" and revisit_when is not None:
            raise ValueError(f"problem_mining_revisit_on_non_deferred:{tag}:{atom_id}")
        decision_ids.append(atom_id)
        normalized = {
            "atom_id": atom_id,
            "disposition": disposition,
            "problem_ids": problem_ids,
            "rationale": rationale,
            "revisit_when": revisit_when,
        }
        if routing_only:
            normalized["routing_keys"] = routing_keys
        if disposition_proof is not None:
            normalized["disposition_proof"] = disposition_proof
        normalized_decisions.append(normalized)
        decisions_by_atom[atom_id] = normalized
    if sorted(decision_ids) != assigned or len(decision_ids) != len(set(decision_ids)):
        raise ValueError(f"problem_mining_assignment_decision_partition_mismatch:{tag}")
    for error in materialization_errors:
        atom_id = _text(error.get("atom_id"))
        decision = decisions_by_atom.get(atom_id or "") or {}
        if decision.get("disposition") != "unresolved":
            raise ValueError(
                f"problem_mining_unavailable_attachment_must_remain_unresolved:{tag}:{atom_id}"
            )
    for problem_id, evidence_ids in record_evidence.items():
        for atom_id in evidence_ids:
            decision = decisions_by_atom.get(atom_id) or {}
            if decision.get("disposition") != "supports_case" or problem_id not in _string_list(
                decision.get("problem_ids")
            ):
                raise ValueError(
                    f"problem_mining_citation_without_support_decision:{tag}:{problem_id}:{atom_id}"
                )
    unread_assigned = sorted(set(assigned) - set(reads))
    if unread_assigned:
        raise ValueError(
            f"problem_mining_assigned_atom_not_read_in_full:{tag}:" + ",".join(unread_assigned)
        )
    unread_context = sorted(set(context_atom_ids) - set(reads))
    if unread_context:
        raise ValueError(
            f"problem_mining_context_atom_not_read_in_full:{tag}:" + ",".join(unread_context)
        )

    required_read_ids = sorted(set(assigned) | cited_ids)
    return {
        "tag": tag,
        "template": template_name,
        "status": "verified",
        "assigned_atom_ids": assigned,
        "context_atom_ids": context_atom_ids,
        "cited_atom_ids": sorted(cited_ids),
        "response_sha256": sha256(response_text.encode("utf-8")).hexdigest(),
        "workspace_dir": str(workspace_dir.resolve()),
        "workspace_manifest_sha256": _canonical_hash(dict(workspace_manifest)),
        "normalized_events_path": str(normalized_events_path.resolve()),
        "normalized_events_sha256": _file_sha256(normalized_events_path),
        "required_workspace_read_attestations": required_workspace_reads,
        "read_attestations": [reads[atom_id] for atom_id in required_read_ids],
        "context_read_attestations": [reads[atom_id] for atom_id in context_atom_ids],
        "origin_attachment_evidence": origin_manifest,
        "origin_attachment_read_attestations": attachment_reads,
        "atom_decisions": sorted(normalized_decisions, key=lambda item: item["atom_id"]),
    }


def build_dry_run_miner_receipt(
    *,
    tag: str,
    template_name: str,
    assigned_atom_ids: Sequence[str],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Record deterministic dry-run decisions without claiming model reads."""

    cited_by: dict[str, list[str]] = {}
    for record in records:
        problem_id = _text(record.get("problem_id"))
        if problem_id is None:
            continue
        for atom_id in _string_list(record.get("evidence_atom_ids")):
            cited_by.setdefault(atom_id, []).append(problem_id)
    decisions = []
    for atom_id in sorted(assigned_atom_ids):
        problem_ids = sorted(set(cited_by.get(atom_id, [])))
        if problem_ids:
            disposition = "supports_case"
            rationale = "Dry-run synthesis grouped this atom into a deterministic case fixture."
        else:
            disposition = "deferred"
            rationale = (
                "Dry-run synthesis did not classify this atom; "
                "a live full-evidence pass is required."
            )
        decisions.append(
            {
                "atom_id": atom_id,
                "disposition": disposition,
                "problem_ids": problem_ids,
                "rationale": rationale,
                "revisit_when": (
                    "Run the required live full-evidence mining pass."
                    if disposition == "deferred"
                    else None
                ),
            }
        )
    return {
        "tag": tag,
        "template": template_name,
        "status": "dry_run_not_attested",
        "assigned_atom_ids": sorted(assigned_atom_ids),
        "context_atom_ids": [],
        "cited_atom_ids": sorted(cited_by),
        "response_sha256": None,
        "workspace_dir": None,
        "workspace_manifest_sha256": None,
        "normalized_events_path": None,
        "normalized_events_sha256": None,
        "read_attestations": [],
        "context_read_attestations": [],
        "origin_attachment_evidence": {},
        "origin_attachment_read_attestations": [],
        "atom_decisions": decisions,
    }


def build_failed_miner_receipt(
    *,
    tag: str,
    template_name: str,
    assigned_atom_ids: Sequence[str],
    workspace_dir: Path,
    workspace_manifest: Mapping[str, Any],
    error: str,
) -> dict[str, Any]:
    """Retain a failed job and explicitly return its atoms for a later retry.

    This receipt never claims an agent read. It exists so successful disjoint jobs and
    their cases survive the cycle while shadow/export remains closed and only these
    unresolved atoms are eligible on the next full mining pass.
    """

    assigned = sorted(set(assigned_atom_ids))
    context_atom_ids = _string_list(workspace_manifest.get("context_atom_ids", []))
    decisions = [
        {
            "atom_id": atom_id,
            "disposition": "unresolved",
            "problem_ids": [],
            "rationale": f"The assigned mining job failed before a verified decision: {error}",
            "revisit_when": None,
        }
        for atom_id in assigned
    ]
    return {
        "tag": tag,
        "template": template_name,
        "status": "failed_unresolved",
        "error": error,
        "assigned_atom_ids": assigned,
        "context_atom_ids": context_atom_ids,
        "cited_atom_ids": [],
        "response_sha256": None,
        "workspace_dir": str(workspace_dir.resolve()),
        "workspace_manifest_sha256": _canonical_hash(dict(workspace_manifest)),
        "normalized_events_path": None,
        "normalized_events_sha256": None,
        "read_attestations": [],
        "context_read_attestations": [],
        "origin_attachment_evidence": workspace_manifest.get("origin_attachment_evidence", {}),
        "origin_attachment_read_attestations": [],
        "atom_decisions": decisions,
    }


def apply_problem_mining_decision_partition(
    *,
    atoms: Sequence[Mapping[str, Any]],
    canonical_records: Sequence[Mapping[str, Any]],
    draft: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Apply non-case decisions before canonical citations attach case membership."""

    eligible_ids = _string_list(draft.get("eligible_atom_ids"))
    cited_ids = {
        atom_id
        for record in canonical_records
        for atom_id in _string_list(record.get("evidence_atom_ids"))
    }
    owner_decisions: dict[str, dict[str, Any]] = {}
    for raw_miner in draft.get("miners", []):
        if not isinstance(raw_miner, Mapping):
            continue
        for raw_decision in raw_miner.get("atom_decisions", []):
            if not isinstance(raw_decision, Mapping):
                continue
            atom_id = _text(raw_decision.get("atom_id"))
            if atom_id is None or atom_id in owner_decisions:
                raise ValueError(f"problem_mining_final_decision_owner_invalid:{atom_id}")
            owner_decisions[atom_id] = dict(raw_decision)
    if sorted(owner_decisions) != sorted(eligible_ids):
        raise ValueError("problem_mining_final_decision_partition_incomplete")

    cross_raw = draft.get("cross_job_synthesis")
    cross = cross_raw if isinstance(cross_raw, Mapping) else {}
    overrides_raw = cross.get("decision_overrides")
    overrides = overrides_raw if isinstance(overrides_raw, list) else []
    overridden: set[str] = set()
    for raw_override in overrides:
        if not isinstance(raw_override, Mapping):
            raise ValueError("problem_mining_cross_job_decision_override_invalid")
        atom_id = _text(raw_override.get("atom_id"))
        if atom_id is None or atom_id not in owner_decisions or atom_id in overridden:
            raise ValueError(f"problem_mining_cross_job_decision_owner_invalid:{atom_id}")
        if owner_decisions[atom_id].get("disposition") == "supports_case":
            raise ValueError(f"problem_mining_cross_job_supported_anchor_overridden:{atom_id}")
        if raw_override.get("disposition") != "supports_case" or not _string_list(
            raw_override.get("problem_ids")
        ):
            raise ValueError(f"problem_mining_cross_job_decision_invalid:{atom_id}")
        if atom_id not in cited_ids:
            raise ValueError(f"problem_mining_cross_job_decision_uncited:{atom_id}")
        owner_decisions[atom_id] = dict(raw_override)
        overridden.add(atom_id)

    updated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_atom in atoms:
        atom = dict(raw_atom)
        atom_id = _text(atom.get("atom_id"))
        if atom_id not in owner_decisions:
            updated.append(atom)
            continue
        seen.add(atom_id)
        if atom_id in cited_ids:
            updated.append(atom)
            continue
        decision = owner_decisions[atom_id]
        disposition = _text(decision.get("disposition"))
        rationale = _text(decision.get("rationale"))
        if disposition not in _NON_SUPPORT_DISPOSITIONS or rationale is None:
            raise ValueError(f"problem_mining_uncited_atom_decision_invalid:{atom_id}")
        atom = apply_atom_disposition_decision(
            {
                **atom,
                **(
                    {"disposition_proof": dict(decision["disposition_proof"])}
                    if isinstance(decision.get("disposition_proof"), Mapping)
                    else {}
                ),
            },
            disposition=disposition,
            source="problem_mining_evidence_partition",
            rationale=rationale,
        )
        revisit_when = _text(decision.get("revisit_when"))
        if disposition == "deferred" and revisit_when is not None:
            atom["disposition_revisit_when"] = revisit_when
        else:
            atom.pop("disposition_revisit_when", None)
        updated.append(atom)
    if seen != set(eligible_ids):
        raise ValueError("problem_mining_final_decision_atoms_missing")
    return updated


def finalize_problem_mining_evidence_receipt(
    *,
    draft: Mapping[str, Any],
    atoms: Sequence[Mapping[str, Any]],
    receipt_path: Path,
) -> ProblemMiningEvidenceReceipt:
    """Persist the final exact decision partition and disposition provenance."""

    eligible_ids = _string_list(draft.get("eligible_atom_ids"))
    atoms_by_id = {
        atom_id: atom
        for atom in atoms
        for atom_id in [_text(atom.get("atom_id"))]
        if atom_id is not None
    }
    decisions: list[dict[str, Any]] = []
    for atom_id in eligible_ids:
        atom = atoms_by_id.get(atom_id)
        if atom is None:
            raise ValueError(f"problem_mining_final_atom_missing:{atom_id}")
        errors = atom_disposition_receipt_errors(atom, require_decided=True)
        disposition = _text(atom.get("disposition"))
        receipt_raw = atom.get("disposition_receipt")
        disposition_receipt = receipt_raw if isinstance(receipt_raw, Mapping) else {}
        if errors or disposition not in _FINAL_DISPOSITIONS:
            raise ValueError(
                f"problem_mining_final_disposition_invalid:{atom_id}:" + ",".join(errors)
            )
        case_ids = sorted(
            set(_string_list(atom.get("supporting_case_ids")))
            | ({str(atom["case_id"])} if _text(atom.get("case_id")) is not None else set())
        )
        if disposition == "supports_case" and not case_ids:
            raise ValueError(f"problem_mining_final_support_case_missing:{atom_id}")
        if disposition != "supports_case" and case_ids:
            raise ValueError(f"problem_mining_final_non_support_case_present:{atom_id}")
        decisions.append(
            {
                "atom_id": atom_id,
                "evidence_role": _text(atom.get("evidence_role")) or "observation",
                "disposition": disposition,
                "case_ids": case_ids,
                "rationale": _text(disposition_receipt.get("rationale")),
                "revisit_when": _text(atom.get("disposition_revisit_when")),
                "disposition_receipt_sha256": disposition_receipt.get("receipt_sha256"),
            }
        )

    mode = _text(draft.get("mode")) or "invalid"
    miners = list(draft.get("miners", []))
    live_jobs_verified = all(
        isinstance(miner, Mapping) and miner.get("status") == "verified" for miner in miners
    )
    cross_raw = draft.get("cross_job_synthesis")
    cross_job_synthesis = dict(cross_raw) if isinstance(cross_raw, Mapping) else {}
    if not cross_job_synthesis and len(miners) <= 1:
        cross_job_synthesis = {
            "schema_version": 1,
            "status": "not_required",
            "reason": "fewer_than_two_leaf_jobs",
            "routing_levels": [],
            "exact_syntheses": [],
            "decision_overrides": [],
        }
    cross_job_verified = cross_job_synthesis.get("status") in {"verified", "not_required"}
    receipt: dict[str, Any] = {
        "schema_version": PROBLEM_MINING_EVIDENCE_SCHEMA_VERSION,
        "receipt_kind": "problem_mining_evidence",
        "mode": mode,
        "status": (
            "verified"
            if mode == "live" and live_jobs_verified and cross_job_verified
            else "partial_failed_jobs"
            if mode == "live"
            else "dry_run_not_exportable"
        ),
        "eligible_for_shadow_export": (
            mode == "live" and live_jobs_verified and cross_job_verified
        ),
        "eligible_atom_ids": eligible_ids,
        "eligible_source_atom_ids": _string_list(draft.get("eligible_source_atom_ids")),
        "eligible_derived_atom_ids": _string_list(draft.get("eligible_derived_atom_ids")),
        "eligible_corpus_sha256": draft.get("eligible_corpus_sha256"),
        "atom_evidence": list(draft.get("atom_evidence", [])),
        "miners": miners,
        "cross_job_synthesis": cross_job_synthesis,
        "decision_partition": sorted(decisions, key=lambda item: item["atom_id"]),
    }
    receipt["receipt_sha256"] = _receipt_hash(receipt)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return receipt  # type: ignore[return-value]


def problem_mining_evidence_receipt_ref(
    *, receipt: Mapping[str, Any], receipt_path: Path
) -> dict[str, Any]:
    return {
        "schema_version": PROBLEM_MINING_EVIDENCE_SCHEMA_VERSION,
        "path": str(receipt_path.resolve()),
        "file_sha256": _file_sha256(receipt_path),
        "receipt_sha256": receipt.get("receipt_sha256"),
        "mode": receipt.get("mode"),
        "status": receipt.get("status"),
        "eligible_atom_count": len(_string_list(receipt.get("eligible_atom_ids"))),
        "eligible_source_atom_count": len(_string_list(receipt.get("eligible_source_atom_ids"))),
    }


def _required_workspace_read_errors(
    miner: Mapping[str, Any],
    *,
    workspace: Path,
    events: Sequence[Mapping[str, Any]],
    tag: str,
) -> list[str]:
    errors: list[str] = []
    manifest_path = workspace / "atoms.json"
    try:
        manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return [f"problem_mining_workspace_manifest_unreadable:{tag}"]
    manifest = manifest_raw if isinstance(manifest_raw, Mapping) else {}
    required: set[tuple[str, str]] = {("manifest", "atoms.json")}
    index_file = _text(manifest.get("index_file"))
    if index_file is None:
        errors.append(f"problem_mining_workspace_index_missing:{tag}")
    else:
        required.add(("index", index_file))
    chunks_raw = manifest.get("chunks")
    chunks = chunks_raw if isinstance(chunks_raw, list) else []
    for raw_chunk in chunks:
        if not isinstance(raw_chunk, Mapping):
            errors.append(f"problem_mining_workspace_chunk_invalid:{tag}")
            continue
        text_file = _text(raw_chunk.get("text_file"))
        if text_file is None:
            errors.append(f"problem_mining_workspace_chunk_text_missing:{tag}")
            continue
        required.add(("chunk_markdown", text_file))

    attestations_raw = miner.get("required_workspace_read_attestations")
    attestations = attestations_raw if isinstance(attestations_raw, list) else []
    observed: set[tuple[str, str]] = set()
    workspace_resolved = workspace.resolve()
    for raw_attestation in attestations:
        if not isinstance(raw_attestation, Mapping):
            errors.append(f"problem_mining_required_read_attestation_invalid:{tag}")
            continue
        file_kind = _text(raw_attestation.get("file_kind"))
        rel_path = _text(raw_attestation.get("file"))
        event_index = raw_attestation.get("event_index")
        key = (file_kind or "", rel_path or "")
        if (
            key not in required
            or key in observed
            or isinstance(event_index, bool)
            or not isinstance(event_index, int)
            or event_index < 0
            or event_index >= len(events)
        ):
            errors.append(f"problem_mining_required_read_attestation_fields_invalid:{tag}")
            continue
        path = (workspace / Path(rel_path or "")).resolve()
        try:
            path.relative_to(workspace_resolved)
        except ValueError:
            errors.append(f"problem_mining_required_read_attestation_outside_workspace:{tag}")
            continue
        event = events[event_index]
        data_raw = event.get("data")
        data = data_raw if isinstance(data_raw, Mapping) else {}
        event_path = _text(data.get("path"))
        normalized_event_path = (event_path or "").replace("\\", "/").casefold()
        normalized_rel = (rel_path or "").replace("\\", "/").casefold()
        expected_sha = raw_attestation.get("file_sha256")
        expected_size = raw_attestation.get("file_size_bytes")
        event_size = data.get("file_size_bytes", data.get("observed_bytes"))
        if (
            not path.is_file()
            or not isinstance(expected_sha, str)
            or _file_sha256(path) != expected_sha
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or path.stat().st_size != expected_size
            or event.get("type") != "read_file"
            or not (
                normalized_event_path == normalized_rel
                or normalized_event_path.endswith("/" + normalized_rel)
            )
            or data.get("content_observed") is not True
            or data.get("whole_file_observed") is not True
            or data.get("source_exit_code") != 0
            or data.get("file_sha256") != expected_sha
            or (event_size is not None and event_size != expected_size)
            or raw_attestation.get("event_sha256") != _canonical_hash(event)
        ):
            errors.append(f"problem_mining_required_read_attestation_changed:{tag}:{rel_path}")
            continue
        observed.add(key)
    if observed != required:
        errors.append(f"problem_mining_required_workspace_read_coverage_mismatch:{tag}")
    return errors


def _workspace_atom_partition_errors(
    *,
    workspace: Path,
    assigned_atom_ids: Sequence[str],
    context_atom_ids: Sequence[str],
    tag: str,
) -> list[str]:
    """Rebind a retained receipt's decision/context split to its exact workspace."""

    manifest_path = workspace / "atoms.json"
    try:
        manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return [f"problem_mining_workspace_manifest_unreadable:{tag}"]
    if not isinstance(manifest_raw, Mapping):
        return [f"problem_mining_workspace_manifest_invalid:{tag}"]
    manifest = dict(manifest_raw)
    assigned = list(assigned_atom_ids)
    context = list(context_atom_ids)
    manifest_assigned_raw = manifest.get("assigned_atom_ids")
    manifest_context_raw = manifest.get("context_atom_ids", [])
    manifest_assigned = _string_list(manifest_assigned_raw)
    manifest_context = _string_list(manifest_context_raw)
    if (
        not isinstance(manifest_assigned_raw, list)
        or len(manifest_assigned) != len(manifest_assigned_raw)
        or manifest_assigned != assigned
    ):
        return [f"problem_mining_workspace_assignment_mismatch:{tag}"]
    if (
        not isinstance(manifest_context_raw, list)
        or len(manifest_context) != len(manifest_context_raw)
        or manifest_context != context
        or set(manifest_assigned) & set(manifest_context)
    ):
        return [f"problem_mining_workspace_context_mismatch:{tag}"]
    expected_ids = set(assigned) | set(context)

    atom_files_raw = manifest.get("atom_files")
    atom_files = atom_files_raw if isinstance(atom_files_raw, list) else []
    atom_file_ids = [
        atom_id
        for raw in atom_files
        if isinstance(raw, Mapping)
        for atom_id in [_text(raw.get("atom_id"))]
        if atom_id is not None
    ]
    if set(atom_file_ids) != expected_ids or len(atom_file_ids) != len(set(atom_file_ids)):
        return [f"problem_mining_workspace_atom_partition_mismatch:{tag}"]

    chunks_raw = manifest.get("chunks")
    chunks = chunks_raw if isinstance(chunks_raw, list) else []
    chunk_atom_ids = [
        atom_id
        for raw in chunks
        if isinstance(raw, Mapping)
        for atom_id in _string_list(raw.get("atom_ids"))
    ]
    if set(chunk_atom_ids) != expected_ids or len(chunk_atom_ids) != len(set(chunk_atom_ids)):
        return [f"problem_mining_workspace_chunk_partition_mismatch:{tag}"]
    try:
        workspace_atoms = _workspace_atoms_by_id(
            workspace_manifest=manifest,
            workspace_dir=workspace,
        )
    except ValueError:
        return [f"problem_mining_workspace_payload_changed:{tag}"]
    if set(workspace_atoms) != expected_ids:
        return [f"problem_mining_workspace_payload_partition_mismatch:{tag}"]
    return []


def _attempt_history_errors(miner: Mapping[str, Any], *, tag: str) -> list[str]:
    raw_history = miner.get("attempt_history")
    if raw_history is None:
        return []
    if not isinstance(raw_history, list) or not raw_history:
        return [f"problem_mining_attempt_history_invalid:{tag}"]
    errors: list[str] = []
    observed_numbers: list[int] = []
    verified_tags: list[str] = []
    initial_session_id: str | None = None
    initial_workspace_dir: str | None = None
    initial_manifest_sha256: str | None = None
    for index, raw_attempt in enumerate(raw_history, start=1):
        if not isinstance(raw_attempt, Mapping):
            errors.append(f"problem_mining_attempt_invalid:{tag}:{index}")
            continue
        attempt_number = raw_attempt.get("attempt_number")
        attempt_tag = _text(raw_attempt.get("attempt_tag"))
        status = _text(raw_attempt.get("status"))
        schema_version = raw_attempt.get("schema_version", 1)
        if schema_version not in {1, 2}:
            errors.append(f"problem_mining_attempt_schema_invalid:{tag}:{index}")
        if isinstance(attempt_number, bool) or not isinstance(attempt_number, int):
            errors.append(f"problem_mining_attempt_number_invalid:{tag}:{index}")
        else:
            observed_numbers.append(attempt_number)
        if attempt_tag is None or status not in {
            "verified",
            "response_contract_failed",
            "failed",
            "failed_evidence_changed",
        }:
            errors.append(f"problem_mining_attempt_state_invalid:{tag}:{index}")
        if status == "verified" and attempt_tag is not None:
            verified_tags.append(attempt_tag)
        if schema_version == 2:
            session_raw = _text(raw_attempt.get("agent_session_id"))
            resumed_raw = raw_attempt.get("resumed_from_session_id")
            try:
                session_id = str(UUID(session_raw)) if session_raw is not None else None
            except (ValueError, AttributeError):
                session_id = None
            if status in {"verified", "response_contract_failed"} and (
                session_id is None or session_raw != session_id
            ):
                errors.append(f"problem_mining_attempt_session_invalid:{tag}:{index}")
            workspace_dir = _text(raw_attempt.get("workspace_dir"))
            manifest_sha256 = _text(raw_attempt.get("workspace_manifest_sha256"))
            if index == 1:
                initial_session_id = session_id
                initial_workspace_dir = workspace_dir
                initial_manifest_sha256 = manifest_sha256
                if resumed_raw is not None:
                    errors.append(f"problem_mining_initial_attempt_resumed:{tag}")
            else:
                if (
                    session_id is None
                    or session_id != initial_session_id
                    or resumed_raw != initial_session_id
                ):
                    errors.append(f"problem_mining_attempt_session_continuity_invalid:{tag}:{index}")
                if (
                    workspace_dir != initial_workspace_dir
                    or manifest_sha256 != initial_manifest_sha256
                ):
                    errors.append(
                        f"problem_mining_attempt_workspace_continuity_invalid:{tag}:{index}"
                    )
            elapsed = raw_attempt.get("attempt_elapsed_seconds")
            if (
                isinstance(elapsed, bool)
                or not isinstance(elapsed, (int, float))
                or float(elapsed) < 0.0
            ):
                errors.append(f"problem_mining_attempt_elapsed_invalid:{tag}:{index}")
        artifacts_raw = raw_attempt.get("artifacts")
        artifacts = artifacts_raw if isinstance(artifacts_raw, Mapping) else {}
        required_artifacts = {"workspace_manifest"}
        if status in {"verified", "response_contract_failed"}:
            required_artifacts.update({"prompt", "response", "raw_events"})
        if status == "verified":
            required_artifacts.add("normalized_events")
        if not required_artifacts.issubset(set(artifacts)):
            errors.append(f"problem_mining_attempt_artifacts_missing:{tag}:{index}")
        for artifact_name, raw_ref in artifacts.items():
            if not isinstance(raw_ref, Mapping):
                errors.append(
                    f"problem_mining_attempt_artifact_ref_invalid:{tag}:{index}:{artifact_name}"
                )
                continue
            path_raw = _text(raw_ref.get("path"))
            expected_sha = raw_ref.get("sha256")
            expected_bytes = raw_ref.get("bytes")
            path = Path(path_raw) if path_raw is not None else None
            if (
                path is None
                or not path.is_file()
                or not isinstance(expected_sha, str)
                or _file_sha256(path) != expected_sha
                or isinstance(expected_bytes, bool)
                or not isinstance(expected_bytes, int)
                or path.stat().st_size != expected_bytes
            ):
                errors.append(
                    f"problem_mining_attempt_artifact_changed:{tag}:{index}:{artifact_name}"
                )
    if observed_numbers != list(range(1, len(raw_history) + 1)):
        errors.append(f"problem_mining_attempt_numbers_noncontiguous:{tag}")
    successful_tag = _text(miner.get("successful_attempt_tag"))
    if miner.get("status") == "verified" and verified_tags != [successful_tag]:
        errors.append(f"problem_mining_successful_attempt_invalid:{tag}")
    return errors


def _miner_receipt_errors(
    miner: Mapping[str, Any],
    *,
    eligible_ids: set[str],
    require_live: bool,
) -> list[str]:
    tag = _text(miner.get("tag")) or "(missing)"
    errors: list[str] = []
    assigned_raw = miner.get("assigned_atom_ids")
    assigned = _string_list(assigned_raw)
    if (
        not isinstance(assigned_raw, list)
        or len(assigned) != len(assigned_raw)
        or assigned != sorted(assigned)
    ):
        errors.append(f"problem_mining_miner_assignment_invalid:{tag}")
    context_raw = miner.get("context_atom_ids", [])
    context_atom_ids = _string_list(context_raw)
    if (
        not isinstance(context_raw, list)
        or len(context_atom_ids) != len(context_raw)
        or context_atom_ids != sorted(context_atom_ids)
    ):
        errors.append(f"problem_mining_miner_context_invalid:{tag}")
    if set(assigned) & set(context_atom_ids):
        errors.append(f"problem_mining_miner_assignment_context_overlap:{tag}")
    cited_raw = miner.get("cited_atom_ids")
    cited_atom_ids = _string_list(cited_raw)
    if (
        not isinstance(cited_raw, list)
        or len(cited_atom_ids) != len(cited_raw)
        or not set(cited_atom_ids).issubset(set(assigned))
    ):
        errors.append(f"problem_mining_miner_citation_outside_assignment:{tag}")
    decisions_raw = miner.get("atom_decisions")
    decisions = decisions_raw if isinstance(decisions_raw, list) else []
    decision_ids = [
        atom_id
        for raw in decisions
        if isinstance(raw, Mapping)
        for atom_id in [_text(raw.get("atom_id"))]
        if atom_id is not None
    ]
    if sorted(decision_ids) != sorted(assigned) or len(decision_ids) != len(set(decision_ids)):
        errors.append(f"problem_mining_miner_partition_invalid:{tag}")
    if not set(assigned).issubset(eligible_ids):
        errors.append(f"problem_mining_miner_assignment_outside_corpus:{tag}")
    if not require_live:
        return errors
    errors.extend(_attempt_history_errors(miner, tag=tag))
    if miner.get("status") != "verified":
        errors.append(f"problem_mining_miner_not_verified:{tag}")
        return errors
    if not assigned and not cited_atom_ids and not context_atom_ids:
        return errors
    workspace_raw = _text(miner.get("workspace_dir"))
    normalized_raw = _text(miner.get("normalized_events_path"))
    workspace = Path(workspace_raw) if workspace_raw is not None else None
    normalized = Path(normalized_raw) if normalized_raw is not None else None
    if workspace is None or not workspace.is_dir():
        errors.append(f"problem_mining_workspace_missing:{tag}")
    if (
        normalized is None
        or not normalized.is_file()
        or miner.get("normalized_events_sha256") != _file_sha256(normalized)
    ):
        errors.append(f"problem_mining_normalized_events_changed:{tag}")
        return errors
    try:
        events = _load_jsonl(normalized)
    except (OSError, ValueError):
        errors.append(f"problem_mining_normalized_events_unreadable:{tag}")
        return errors
    if workspace is not None:
        errors.extend(
            _workspace_atom_partition_errors(
                workspace=workspace,
                assigned_atom_ids=assigned,
                context_atom_ids=context_atom_ids,
                tag=tag,
            )
        )
        errors.extend(
            _required_workspace_read_errors(
                miner,
                workspace=workspace,
                events=events,
                tag=tag,
            )
        )
    for attestation_field, expected_ids, error_stem, coverage_error in (
        (
            "read_attestations",
            set(assigned),
            "problem_mining_read_attestation",
            "problem_mining_full_read_coverage_mismatch",
        ),
        (
            "context_read_attestations",
            set(context_atom_ids),
            "problem_mining_context_read_attestation",
            "problem_mining_context_full_read_coverage_mismatch",
        ),
    ):
        raw_attestations = miner.get(attestation_field, [])
        attestations = raw_attestations if isinstance(raw_attestations, list) else []
        if not isinstance(raw_attestations, list):
            errors.append(f"{error_stem}_invalid:{tag}")
        read_ids: set[str] = set()
        for raw_attestation in attestations:
            if not isinstance(raw_attestation, Mapping):
                errors.append(f"{error_stem}_invalid:{tag}")
                continue
            atom_id = _text(raw_attestation.get("atom_id"))
            rel_path = _text(raw_attestation.get("atom_file"))
            event_index = raw_attestation.get("event_index")
            if (
                atom_id is None
                or rel_path is None
                or workspace is None
                or isinstance(event_index, bool)
                or not isinstance(event_index, int)
                or event_index < 0
                or event_index >= len(events)
            ):
                errors.append(f"{error_stem}_fields_invalid:{tag}")
                continue
            atom_file = workspace / Path(rel_path)
            event = events[event_index]
            data_raw = event.get("data")
            data = data_raw if isinstance(data_raw, Mapping) else {}
            if (
                not atom_file.is_file()
                or raw_attestation.get("atom_file_sha256") != _file_sha256(atom_file)
                or event.get("type") != "read_file"
                or data.get("content_observed") is not True
                or data.get("whole_file_observed") is not True
                or data.get("source_exit_code") != 0
                or data.get("file_sha256") != raw_attestation.get("atom_file_sha256")
                or raw_attestation.get("event_sha256") != _canonical_hash(event)
            ):
                errors.append(f"{error_stem}_changed:{tag}:{atom_id}")
                continue
            if atom_id in read_ids:
                errors.append(f"{error_stem}_duplicate:{tag}:{atom_id}")
                continue
            read_ids.add(atom_id)
        if read_ids != expected_ids:
            errors.append(f"{coverage_error}:{tag}")
    origin_manifest_raw = miner.get("origin_attachment_evidence")
    origin_manifest = dict(origin_manifest_raw) if isinstance(origin_manifest_raw, Mapping) else {}
    if workspace is not None:
        errors.extend(
            f"problem_mining_{error}"
            for error in verify_materialized_origin_attachments(
                workspace_dir=workspace,
                manifest=origin_manifest,
            )
        )
    requirements = origin_attachment_requirements(
        origin_manifest,
        atom_ids=sorted(set(assigned) | set(context_atom_ids)),
    )
    expected_attachment_files = {str(item["file"]): item for item in requirements}
    attachment_attestations_raw = miner.get("origin_attachment_read_attestations")
    attachment_attestations = (
        attachment_attestations_raw if isinstance(attachment_attestations_raw, list) else []
    )
    attested_attachment_files: set[str] = set()
    for raw_attestation in attachment_attestations:
        if not isinstance(raw_attestation, Mapping):
            errors.append(f"problem_mining_origin_attachment_attestation_invalid:{tag}")
            continue
        rel_path = _text(raw_attestation.get("file"))
        event_index = raw_attestation.get("event_index")
        requirement = expected_attachment_files.get(rel_path or "")
        if (
            rel_path is None
            or requirement is None
            or workspace is None
            or isinstance(event_index, bool)
            or not isinstance(event_index, int)
            or event_index < 0
            or event_index >= len(events)
        ):
            errors.append(f"problem_mining_origin_attachment_attestation_fields_invalid:{tag}")
            continue
        path = (workspace / Path(rel_path)).resolve()
        try:
            path.relative_to(workspace.resolve())
        except ValueError:
            errors.append(f"problem_mining_origin_attachment_attestation_outside_workspace:{tag}")
            continue
        event = events[event_index]
        data_raw = event.get("data")
        data = data_raw if isinstance(data_raw, Mapping) else {}
        event_path = _text(data.get("path"))
        normalized_event_path = (event_path or "").replace("\\", "/").casefold()
        normalized_rel = rel_path.replace("\\", "/").casefold()
        if (
            not path.is_file()
            or raw_attestation.get("file_sha256") != requirement.get("sha256")
            or _file_sha256(path) != requirement.get("sha256")
            or path.stat().st_size != requirement.get("size_bytes")
            or event.get("type") != "read_file"
            or not (
                normalized_event_path == normalized_rel
                or normalized_event_path.endswith("/" + normalized_rel)
            )
            or data.get("content_observed") is not True
            or data.get("whole_file_observed") is not True
            or data.get("source_exit_code") != 0
            or data.get("file_sha256") != requirement.get("sha256")
            or data.get("file_size_bytes") != requirement.get("size_bytes")
            or raw_attestation.get("event_sha256") != _canonical_hash(event)
        ):
            errors.append(f"problem_mining_origin_attachment_attestation_changed:{tag}:{rel_path}")
            continue
        if rel_path in attested_attachment_files:
            errors.append(
                f"problem_mining_origin_attachment_attestation_duplicate:{tag}:{rel_path}"
            )
            continue
        attested_attachment_files.add(rel_path)
    if attested_attachment_files != set(expected_attachment_files):
        errors.append(f"problem_mining_origin_attachment_read_coverage_mismatch:{tag}")
    decisions_by_id = {
        atom_id: raw
        for raw in decisions
        if isinstance(raw, Mapping)
        for atom_id in [_text(raw.get("atom_id"))]
        if atom_id is not None
    }
    origin_errors_raw = origin_manifest.get("errors")
    origin_errors = origin_errors_raw if isinstance(origin_errors_raw, list) else []
    for origin_error in origin_errors:
        if not isinstance(origin_error, Mapping):
            continue
        atom_id = _text(origin_error.get("atom_id"))
        if (
            atom_id in set(assigned)
            and (decisions_by_id.get(atom_id or "") or {}).get("disposition") != "unresolved"
        ):
            errors.append(f"problem_mining_unavailable_attachment_decision_changed:{tag}:{atom_id}")
    review_raw = miner.get("non_support_review")
    if isinstance(review_raw, Mapping):
        review_status = _text(review_raw.get("status"))
        if review_status is not None and not review_status.startswith("not_required"):
            errors.extend(
                _miner_receipt_errors(
                    review_raw,
                    eligible_ids=eligible_ids,
                    require_live=require_live,
                )
            )
    return errors


def _cross_job_synthesis_errors(
    cross_job_synthesis: Mapping[str, Any],
    *,
    eligible_ids: set[str],
    eligible_evidence_sha256_by_atom: Mapping[str, str],
    require_live: bool,
    owner_decisions_by_atom: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Revalidate every retained routing/exact pass and its final override provenance."""

    errors: list[str] = []
    status = _text(cross_job_synthesis.get("status"))
    if status == "not_required":
        return errors
    if status != "verified":
        return ["problem_mining_cross_job_synthesis_not_verified"]

    leaf_raw = cross_job_synthesis.get("leaf_membership")
    leaf = leaf_raw if isinstance(leaf_raw, list) else []
    leaf_atom_ids: list[str] = []
    leaf_hashes: dict[str, str] = {}
    leaf_original_dispositions: dict[str, str] = {}
    for index, raw in enumerate(leaf):
        if not isinstance(raw, Mapping):
            errors.append(f"problem_mining_cross_job_leaf_invalid:{index}")
            continue
        member_ids = _string_list(raw.get("member_atom_ids"))
        hashes_raw = raw.get("evidence_sha256_by_atom")
        hashes = hashes_raw if isinstance(hashes_raw, Mapping) else {}
        if len(member_ids) != 1 or set(member_ids) != set(hashes):
            errors.append(f"problem_mining_cross_job_leaf_membership_invalid:{index}")
            continue
        atom_id = member_ids[0]
        evidence_sha = hashes.get(atom_id)
        if (
            atom_id not in eligible_ids
            or not _valid_sha256(evidence_sha)
            or evidence_sha != eligible_evidence_sha256_by_atom.get(atom_id)
        ):
            errors.append(f"problem_mining_cross_job_leaf_evidence_invalid:{index}")
            continue
        membership_payload = [{"atom_id": atom_id, "evidence_sha256": evidence_sha}]
        if raw.get("membership_sha256") != _canonical_hash(membership_payload):
            errors.append(f"problem_mining_cross_job_leaf_membership_hash_invalid:{index}")
        original_dispositions_raw = raw.get("original_disposition_by_atom")
        original_dispositions = (
            original_dispositions_raw if isinstance(original_dispositions_raw, Mapping) else {}
        )
        original_problem_ids_raw = raw.get("original_problem_ids_by_atom")
        original_problem_ids = (
            original_problem_ids_raw if isinstance(original_problem_ids_raw, Mapping) else {}
        )
        leaf_claim_hashes_raw = raw.get("leaf_claim_sha256_by_atom")
        leaf_claim_hashes = (
            leaf_claim_hashes_raw if isinstance(leaf_claim_hashes_raw, Mapping) else {}
        )
        if (
            set(original_dispositions) != {atom_id}
            or set(original_problem_ids) != {atom_id}
            or set(leaf_claim_hashes) != {atom_id}
        ):
            errors.append(f"problem_mining_cross_job_leaf_claim_missing:{index}")
        owner_decision = (
            owner_decisions_by_atom.get(atom_id) if owner_decisions_by_atom is not None else None
        )
        if owner_decision is not None:
            owner_problem_ids = sorted(_string_list(owner_decision.get("problem_ids")))
            owner_disposition = _text(owner_decision.get("disposition"))
            claim_projection = {
                "atom_id": atom_id,
                "disposition": owner_decision.get("disposition"),
                "problem_ids": owner_problem_ids,
                "rationale": owner_decision.get("rationale"),
            }
            if (
                original_dispositions.get(atom_id) != owner_disposition
                or sorted(_string_list(original_problem_ids.get(atom_id))) != owner_problem_ids
                or leaf_claim_hashes.get(atom_id) != _canonical_hash(claim_projection)
            ):
                errors.append(f"problem_mining_cross_job_leaf_claim_changed:{index}")
        original_disposition = _text(original_dispositions.get(atom_id))
        if original_disposition is not None:
            leaf_original_dispositions[atom_id] = original_disposition
        leaf_atom_ids.append(atom_id)
        leaf_hashes[atom_id] = str(evidence_sha)
    if len(leaf_atom_ids) != len(set(leaf_atom_ids)):
        errors.append("problem_mining_cross_job_leaf_membership_duplicate")

    leaf_source_jobs_by_atom: dict[str, set[str]] = {}
    for raw in leaf:
        if not isinstance(raw, Mapping):
            continue
        for atom_id in _string_list(raw.get("member_atom_ids")):
            leaf_source_jobs_by_atom.setdefault(atom_id, set()).update(
                _string_list(raw.get("source_job_tags"))
            )

    levels_raw = cross_job_synthesis.get("routing_levels")
    levels = levels_raw if isinstance(levels_raw, list) else []
    if not levels:
        errors.append("problem_mining_cross_job_routing_levels_missing")
    for level_index, raw_level in enumerate(levels, start=1):
        if not isinstance(raw_level, Mapping):
            errors.append(f"problem_mining_cross_job_routing_level_invalid:{level_index}")
            continue
        receipts_raw = raw_level.get("receipts")
        receipts = receipts_raw if isinstance(receipts_raw, list) else []
        if len(receipts) != raw_level.get("batch_count"):
            errors.append(f"problem_mining_cross_job_routing_receipt_count_invalid:{level_index}")
        keys_raw = raw_level.get("routing_keys_by_route")
        keys_by_route = keys_raw if isinstance(keys_raw, Mapping) else {}
        if len(keys_by_route) != raw_level.get("input_node_count"):
            errors.append(
                f"problem_mining_cross_job_routing_semantic_coverage_invalid:{level_index}"
            )
        for route_id, raw_keys in keys_by_route.items():
            keys = _string_list(raw_keys)
            canonical_keys = ["-".join(key.casefold().split()) for key in keys]
            if (
                not isinstance(route_id, str)
                or not (2 <= len(keys) <= 5)
                or keys != canonical_keys
                or any(len(key) > 80 for key in keys)
            ):
                errors.append(
                    f"problem_mining_cross_job_routing_keys_invalid:{level_index}:{route_id}"
                )
        if raw_level.get("routing_semantic_sha256") != _canonical_hash(dict(keys_by_route)):
            errors.append(f"problem_mining_cross_job_routing_semantic_hash_invalid:{level_index}")
        for raw_receipt in receipts:
            if not isinstance(raw_receipt, Mapping):
                errors.append(f"problem_mining_cross_job_routing_receipt_invalid:{level_index}")
                continue
            assigned = set(_string_list(raw_receipt.get("assigned_atom_ids")))
            errors.extend(
                _miner_receipt_errors(
                    raw_receipt,
                    eligible_ids=assigned,
                    require_live=require_live,
                )
            )

    signals_raw = cross_job_synthesis.get("routing_signals")
    signals = signals_raw if isinstance(signals_raw, list) else []
    if not isinstance(signals_raw, list):
        errors.append("problem_mining_cross_job_routing_signals_missing")
    candidate_signal_groups: set[tuple[str, ...]] = set()
    signal_hashes: list[str] = []
    nondiscriminative_count = 0
    for signal_index, raw_signal in enumerate(signals, start=1):
        if not isinstance(raw_signal, Mapping):
            errors.append(f"problem_mining_cross_job_routing_signal_invalid:{signal_index}")
            continue
        routing_key = _text(raw_signal.get("routing_key"))
        canonical_key = "-".join((routing_key or "").casefold().split())
        member_ids = _string_list(raw_signal.get("member_atom_ids"))
        source_job_tags = _string_list(raw_signal.get("source_job_tags"))
        batches_raw = raw_signal.get("measured_exact_batches")
        measured_batches = (
            [_string_list(batch) for batch in batches_raw] if isinstance(batches_raw, list) else []
        )
        measured_atom_ids = [atom_id for batch in measured_batches for atom_id in batch]
        refinement_raw = raw_signal.get("refinement_groups")
        refinement_groups = (
            [_string_list(group) for group in refinement_raw]
            if isinstance(refinement_raw, list)
            else []
        )
        max_atoms = raw_signal.get("max_atoms")
        max_bytes = raw_signal.get("max_bytes")
        disposition = _text(raw_signal.get("disposition"))
        reason = _text(raw_signal.get("reason"))
        expected_reason = {
            "candidate": "within_exact_model_job_ceiling",
            "partitioned_candidate": ("routing_key_partitioned_into_bounded_cross_job_groups"),
            "nondiscriminative": "routing_key_has_no_bounded_cross_job_group",
        }.get(disposition or "")
        expected_source_jobs = sorted(
            {
                source_job
                for atom_id in member_ids
                for source_job in leaf_source_jobs_by_atom.get(atom_id, set())
            }
        )
        expected_refinement_groups = [
            batch
            for batch in measured_batches
            if len(batch) >= 2
            and len(
                {
                    source_job
                    for atom_id in batch
                    for source_job in leaf_source_jobs_by_atom.get(atom_id, set())
                }
            )
            >= 2
        ]
        if (
            not isinstance(raw_signal.get("level"), int)
            or int(raw_signal.get("level", 0)) < 1
            or routing_key is None
            or routing_key != canonical_key
            or len(routing_key) > 80
            or len(member_ids) < 2
            or member_ids != sorted(set(member_ids))
            or not set(member_ids).issubset(set(leaf_atom_ids))
            or len(expected_source_jobs) < 2
            or source_job_tags != expected_source_jobs
        ):
            errors.append(
                f"problem_mining_cross_job_routing_signal_membership_invalid:{signal_index}"
            )
        expected_membership_sha = _canonical_hash(
            [
                {
                    "atom_id": atom_id,
                    "evidence_sha256": leaf_hashes.get(atom_id),
                }
                for atom_id in member_ids
            ]
        )
        if raw_signal.get("membership_sha256") != expected_membership_sha:
            errors.append(
                f"problem_mining_cross_job_routing_signal_membership_hash_invalid:{signal_index}"
            )
        if (
            not measured_batches
            or any(not batch or batch != sorted(set(batch)) for batch in measured_batches)
            or sorted(measured_atom_ids) != member_ids
            or len(measured_atom_ids) != len(set(measured_atom_ids))
            or raw_signal.get("measured_exact_job_count") != len(measured_batches)
            or not isinstance(max_atoms, int)
            or max_atoms <= 0
            or any(len(batch) > max_atoms for batch in measured_batches)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
            or expected_reason is None
            or reason != expected_reason
            or (disposition == "candidate" and len(measured_batches) != 1)
            or (
                disposition == "partitioned_candidate"
                and (len(measured_batches) <= 1 or not expected_refinement_groups)
            )
            or (
                disposition == "nondiscriminative"
                and (len(measured_batches) <= 1 or bool(expected_refinement_groups))
            )
            or refinement_groups != expected_refinement_groups
        ):
            errors.append(f"problem_mining_cross_job_routing_signal_ceiling_invalid:{signal_index}")
        signal_payload = {
            "level": raw_signal.get("level"),
            "routing_key": routing_key,
            "member_atom_ids": member_ids,
            "membership_sha256": raw_signal.get("membership_sha256"),
            "source_job_tags": source_job_tags,
            "measured_exact_batches": measured_batches,
            "refinement_groups": refinement_groups,
            "measured_exact_job_count": raw_signal.get("measured_exact_job_count"),
            "max_atoms": max_atoms,
            "max_bytes": max_bytes,
            "disposition": disposition,
            "reason": reason,
        }
        signal_sha256 = _canonical_hash(signal_payload)
        signal_hashes.append(signal_sha256)
        if (
            raw_signal.get("signal_sha256") != signal_sha256
            or raw_signal.get("signal_id") != f"routing-signal:{signal_sha256[:24]}"
        ):
            errors.append(f"problem_mining_cross_job_routing_signal_hash_invalid:{signal_index}")
        if disposition == "candidate":
            candidate_signal_groups.add(tuple(member_ids))
        elif disposition == "partitioned_candidate":
            candidate_signal_groups.update(tuple(group) for group in refinement_groups)
        elif disposition == "nondiscriminative":
            nondiscriminative_count += 1
    if cross_job_synthesis.get("nondiscriminative_routing_signal_count") != (
        nondiscriminative_count
    ):
        errors.append("problem_mining_cross_job_nondiscriminative_count_invalid")

    exact_raw = cross_job_synthesis.get("exact_syntheses")
    exact = exact_raw if isinstance(exact_raw, list) else []
    candidate_groups_raw = cross_job_synthesis.get("candidate_groups")
    candidate_groups = (
        [sorted(_string_list(group)) for group in candidate_groups_raw]
        if isinstance(candidate_groups_raw, list)
        else []
    )
    if any(not group or not set(group).issubset(eligible_ids) for group in candidate_groups):
        errors.append("problem_mining_cross_job_candidate_group_invalid")
    schema_version = cross_job_synthesis.get("schema_version")
    routing_groups_raw = cross_job_synthesis.get("routing_candidate_groups")
    routing_groups = (
        [sorted(_string_list(group)) for group in routing_groups_raw]
        if isinstance(routing_groups_raw, list)
        else []
    )
    recall_groups_raw = cross_job_synthesis.get("recall_candidate_groups")
    recall_groups = (
        [sorted(_string_list(group)) for group in recall_groups_raw]
        if isinstance(recall_groups_raw, list)
        else []
    )
    expected_routing_groups = [list(group) for group in sorted(candidate_signal_groups)]
    expected_recall_groups = [
        group
        for group in expected_routing_groups
        if any(leaf_original_dispositions.get(atom_id) != "supports_case" for atom_id in group)
    ]
    if schema_version == 1:
        if candidate_groups != expected_routing_groups:
            errors.append("problem_mining_cross_job_candidate_signal_partition_invalid")
    else:
        if schema_version != 2:
            errors.append("problem_mining_cross_job_schema_invalid")
        if routing_groups != expected_routing_groups:
            errors.append("problem_mining_cross_job_candidate_signal_partition_invalid")
        if recall_groups != expected_recall_groups:
            errors.append("problem_mining_cross_job_recall_partition_invalid")
        if cross_job_synthesis.get("supported_only_candidate_group_count") != (
            len(expected_routing_groups) - len(expected_recall_groups)
        ):
            errors.append("problem_mining_cross_job_supported_only_count_invalid")
        packed_sets = [set(group) for group in candidate_groups]
        recall_sets = [set(group) for group in expected_recall_groups]
        if candidate_groups != sorted(candidate_groups):
            errors.append("problem_mining_cross_job_candidate_pack_order_invalid")
        for packed in packed_sets:
            contributors = [group for group in recall_sets if group <= packed]
            if not contributors or set().union(*contributors) != packed:
                errors.append("problem_mining_cross_job_candidate_pack_invalid")
                break
        if any(not any(group <= packed for packed in packed_sets) for group in recall_sets):
            errors.append("problem_mining_cross_job_recall_coverage_invalid")
    if cross_job_synthesis.get("routing_sha256") != _canonical_hash(
        {
            "leaf": [str(raw.get("membership_sha256")) for raw in leaf if isinstance(raw, Mapping)],
            **(
                {
                    "routing_groups": routing_groups,
                    "recall_groups": recall_groups,
                }
                if schema_version == 2
                else {}
            ),
            "groups": candidate_groups,
            "signals": signal_hashes,
        }
    ):
        errors.append("problem_mining_cross_job_routing_hash_invalid")
    expected_override_state: dict[str, dict[str, Any]] = {}
    exact_tags: set[str] = set()
    for exact_index, raw_exact in enumerate(exact, start=1):
        if not isinstance(raw_exact, Mapping):
            errors.append(f"problem_mining_cross_job_exact_invalid:{exact_index}")
            continue
        receipt_raw = raw_exact.get("receipt")
        receipt = receipt_raw if isinstance(receipt_raw, Mapping) else None
        if receipt is None:
            errors.append(f"problem_mining_cross_job_exact_receipt_missing:{exact_index}")
            continue
        assigned = set(_string_list(receipt.get("assigned_atom_ids")))
        candidate_ids = sorted(_string_list(raw_exact.get("candidate_atom_ids")))
        if candidate_ids not in candidate_groups or assigned != set(candidate_ids):
            errors.append(f"problem_mining_cross_job_exact_candidate_mismatch:{exact_index}")
        if schema_version == 2:
            source_groups_raw = raw_exact.get("source_candidate_groups")
            source_groups = (
                [sorted(_string_list(group)) for group in source_groups_raw]
                if isinstance(source_groups_raw, list)
                else []
            )
            expected_source_groups = [
                group
                for group in recall_groups
                if set(group) <= set(candidate_ids)
            ]
            if source_groups != expected_source_groups:
                errors.append(
                    f"problem_mining_cross_job_exact_source_groups_invalid:{exact_index}"
                )
        if raw_exact.get("candidate_membership_sha256") != _canonical_hash(
            [
                {
                    "atom_id": atom_id,
                    "evidence_sha256": leaf_hashes.get(atom_id),
                }
                for atom_id in candidate_ids
            ]
        ):
            errors.append(f"problem_mining_cross_job_exact_candidate_hash_invalid:{exact_index}")
        if not assigned.issubset(eligible_ids):
            errors.append(f"problem_mining_cross_job_exact_assignment_invalid:{exact_index}")
        errors.extend(
            _miner_receipt_errors(
                receipt,
                eligible_ids=eligible_ids,
                require_live=require_live,
            )
        )
        exact_tag = _text(raw_exact.get("tag"))
        if exact_tag is None or exact_tag in exact_tags:
            errors.append(f"problem_mining_cross_job_exact_tag_invalid:{exact_index}")
            exact_tag = f"(invalid:{exact_index})"
        exact_tags.add(exact_tag)
        exact_records_raw = raw_exact.get("records")
        exact_record_ids = {
            problem_id
            for item in (exact_records_raw if isinstance(exact_records_raw, list) else [])
            if isinstance(item, Mapping)
            for problem_id in [_text(item.get("problem_id"))]
            if problem_id is not None
        }
        exact_overrides_raw = raw_exact.get("decision_overrides")
        if isinstance(exact_overrides_raw, list):
            local_atom_ids: set[str] = set()
            for local_index, item in enumerate(exact_overrides_raw, start=1):
                if not isinstance(item, Mapping):
                    errors.append(
                        "problem_mining_cross_job_exact_override_invalid:"
                        f"{exact_index}:{local_index}"
                    )
                    continue
                atom_id = _text(item.get("atom_id"))
                problem_ids = sorted(set(_string_list(item.get("problem_ids"))))
                if (
                    atom_id is None
                    or atom_id in local_atom_ids
                    or atom_id not in assigned
                    or item.get("disposition") != "supports_case"
                    or not problem_ids
                    or not set(problem_ids).issubset(exact_record_ids)
                    or leaf_original_dispositions.get(atom_id) == "supports_case"
                ):
                    errors.append(
                        "problem_mining_cross_job_exact_override_invalid:"
                        f"{exact_index}:{local_index}"
                    )
                    continue
                local_atom_ids.add(atom_id)
                state = expected_override_state.setdefault(
                    atom_id,
                    {"problem_ids": set(), "exact_synthesis_provenance": []},
                )
                state["problem_ids"].update(problem_ids)
                state["exact_synthesis_provenance"].append(
                    {"tag": exact_tag, "problem_ids": problem_ids}
                )

    overrides_raw = cross_job_synthesis.get("decision_overrides")
    overrides = (
        [dict(item) for item in overrides_raw if isinstance(item, Mapping)]
        if isinstance(overrides_raw, list)
        else []
    )
    override_ids = [_text(item.get("atom_id")) for item in overrides]
    if None in override_ids or len(override_ids) != len(set(override_ids)):
        errors.append("problem_mining_cross_job_override_partition_invalid")
    overrides_by_atom = {
        atom_id: item
        for item in overrides
        for atom_id in [_text(item.get("atom_id"))]
        if atom_id is not None
    }
    if set(overrides_by_atom) != set(expected_override_state):
        errors.append("problem_mining_cross_job_override_provenance_mismatch")
    for atom_id, expected in expected_override_state.items():
        override = overrides_by_atom.get(atom_id, {})
        provenance_raw = override.get("exact_synthesis_provenance")
        provenance = (
            [dict(item) for item in provenance_raw if isinstance(item, Mapping)]
            if isinstance(provenance_raw, list)
            else []
        )
        if (
            override.get("disposition") != "supports_case"
            or sorted(set(_string_list(override.get("problem_ids"))))
            != sorted(expected["problem_ids"])
            or provenance != expected["exact_synthesis_provenance"]
        ):
            errors.append(f"problem_mining_cross_job_override_provenance_mismatch:{atom_id}")
    supported_anchor_overrides = sorted(
        atom_id
        for atom_id in overrides_by_atom
        if leaf_original_dispositions.get(atom_id) == "supports_case"
    )
    if supported_anchor_overrides:
        errors.append("problem_mining_cross_job_supported_anchor_overridden")
    exact_candidate_groups = sorted(
        {
            tuple(sorted(_string_list(item.get("candidate_atom_ids"))))
            for item in exact
            if isinstance(item, Mapping)
        }
    )
    if exact_candidate_groups != sorted(tuple(group) for group in candidate_groups):
        errors.append("problem_mining_cross_job_exact_coverage_invalid")
    return errors


def verify_problem_mining_evidence_receipt(
    *,
    stage1: Mapping[str, Any],
    atoms: Sequence[Mapping[str, Any]],
    require_live: bool,
) -> list[str]:
    """Revalidate receipt hashes, retained reads, and the final source partition."""

    artifacts_raw = stage1.get("artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, Mapping) else {}
    meta_raw = stage1.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}
    ref_raw = meta.get("problem_mining_evidence_receipt")
    ref = ref_raw if isinstance(ref_raw, Mapping) else {}
    path_raw = _text(artifacts.get("problem_mining_evidence_receipt")) or _text(ref.get("path"))
    if path_raw is None:
        return ["problem_mining_evidence_receipt_missing"]
    path = Path(path_raw)
    if not path.is_file():
        return ["problem_mining_evidence_receipt_file_missing"]
    errors: list[str] = []
    try:
        receipt_raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ["problem_mining_evidence_receipt_unreadable"]
    if not isinstance(receipt_raw, dict):
        return ["problem_mining_evidence_receipt_invalid"]
    receipt = receipt_raw
    if ref.get("file_sha256") != _file_sha256(path):
        errors.append("problem_mining_evidence_receipt_file_hash_mismatch")
    if receipt.get("receipt_sha256") != _receipt_hash(receipt) or ref.get(
        "receipt_sha256"
    ) != receipt.get("receipt_sha256"):
        errors.append("problem_mining_evidence_receipt_hash_mismatch")
    if (
        receipt.get("schema_version") != PROBLEM_MINING_EVIDENCE_SCHEMA_VERSION
        or receipt.get("receipt_kind") != "problem_mining_evidence"
    ):
        errors.append("problem_mining_evidence_receipt_schema_invalid")
    if require_live and (
        receipt.get("mode") != "live"
        or receipt.get("status") != "verified"
        or receipt.get("eligible_for_shadow_export") is not True
    ):
        errors.append("problem_mining_evidence_receipt_not_live_verified")

    eligible_ids = _string_list(receipt.get("eligible_atom_ids"))
    source_ids = _string_list(receipt.get("eligible_source_atom_ids"))
    derived_ids = _string_list(receipt.get("eligible_derived_atom_ids"))
    if sorted(set(source_ids) | set(derived_ids)) != sorted(eligible_ids) or set(source_ids) & set(
        derived_ids
    ):
        errors.append("problem_mining_evidence_role_partition_invalid")
    evidence_rows_raw = receipt.get("atom_evidence")
    evidence_rows = evidence_rows_raw if isinstance(evidence_rows_raw, list) else []
    rows_by_id = {
        atom_id: dict(raw)
        for raw in evidence_rows
        if isinstance(raw, Mapping)
        for atom_id in [_text(raw.get("atom_id"))]
        if atom_id is not None
    }
    if sorted(rows_by_id) != sorted(eligible_ids) or receipt.get(
        "eligible_corpus_sha256"
    ) != _canonical_hash(sorted(rows_by_id.values(), key=lambda item: str(item["atom_id"]))):
        errors.append("problem_mining_eligible_corpus_hash_mismatch")

    atoms_by_id = {
        atom_id: atom
        for atom in atoms
        for atom_id in [_text(atom.get("atom_id"))]
        if atom_id is not None
    }
    for atom_id in eligible_ids:
        atom = atoms_by_id.get(atom_id)
        row = rows_by_id.get(atom_id)
        if atom is None or row is None:
            errors.append(f"problem_mining_eligible_atom_missing:{atom_id}")
            continue
        role = _text(atom.get("evidence_role")) or "observation"
        is_source = role not in _DERIVED_EVIDENCE_ROLES
        if row.get("evidence_sha256") != _canonical_hash(
            immutable_atom_evidence_projection(atom)
        ):
            errors.append(f"problem_mining_atom_evidence_changed:{atom_id}")
        if row.get("evidence_role") != role or row.get("is_source_observation") is not is_source:
            errors.append(f"problem_mining_atom_evidence_role_changed:{atom_id}")
        if atom_id in source_ids and not is_source:
            errors.append(f"problem_mining_derived_atom_counted_as_source:{atom_id}")
        disposition_errors = atom_disposition_receipt_errors(atom, require_decided=True)
        if disposition_errors:
            errors.append(
                f"problem_mining_atom_disposition_invalid:{atom_id}:" + ",".join(disposition_errors)
            )

    miners_raw = receipt.get("miners")
    miners = miners_raw if isinstance(miners_raw, list) else []
    assigned_ids: list[str] = []
    owner_decisions_by_atom: dict[str, Mapping[str, Any]] = {}
    for raw_miner in miners:
        if not isinstance(raw_miner, Mapping):
            errors.append("problem_mining_miner_receipt_invalid")
            continue
        assigned_ids.extend(_string_list(raw_miner.get("assigned_atom_ids")))
        for raw_decision in raw_miner.get("atom_decisions", []):
            if not isinstance(raw_decision, Mapping):
                continue
            atom_id = _text(raw_decision.get("atom_id"))
            if atom_id is not None:
                owner_decisions_by_atom[atom_id] = raw_decision
        errors.extend(
            _miner_receipt_errors(
                raw_miner,
                eligible_ids=set(eligible_ids),
                require_live=require_live,
            )
        )
    if sorted(assigned_ids) != sorted(eligible_ids) or len(assigned_ids) != len(set(assigned_ids)):
        errors.append("problem_mining_assignment_partition_invalid")

    cross_raw = receipt.get("cross_job_synthesis")
    cross = cross_raw if isinstance(cross_raw, Mapping) else {}
    errors.extend(
        _cross_job_synthesis_errors(
            cross,
            eligible_ids=set(eligible_ids),
            eligible_evidence_sha256_by_atom={
                atom_id: str(row.get("evidence_sha256"))
                for atom_id, row in rows_by_id.items()
                if _valid_sha256(row.get("evidence_sha256"))
            },
            require_live=require_live,
            owner_decisions_by_atom=owner_decisions_by_atom,
        )
    )

    decisions_raw = receipt.get("decision_partition")
    decisions = decisions_raw if isinstance(decisions_raw, list) else []
    decision_ids = [
        atom_id
        for raw in decisions
        if isinstance(raw, Mapping)
        for atom_id in [_text(raw.get("atom_id"))]
        if atom_id is not None
    ]
    if sorted(decision_ids) != sorted(eligible_ids) or len(decision_ids) != len(set(decision_ids)):
        errors.append("problem_mining_final_decision_partition_invalid")
    for raw in decisions:
        if not isinstance(raw, Mapping):
            continue
        atom_id = _text(raw.get("atom_id"))
        atom = atoms_by_id.get(atom_id or "")
        if atom is None:
            continue
        receipt_raw = atom.get("disposition_receipt")
        disposition_receipt = receipt_raw if isinstance(receipt_raw, Mapping) else {}
        case_ids = sorted(
            set(_string_list(atom.get("supporting_case_ids")))
            | ({str(atom["case_id"])} if _text(atom.get("case_id")) is not None else set())
        )
        if (
            raw.get("disposition") != atom.get("disposition")
            or _string_list(raw.get("case_ids")) != case_ids
            or raw.get("disposition_receipt_sha256") != disposition_receipt.get("receipt_sha256")
            or raw.get("rationale") != disposition_receipt.get("rationale")
            or raw.get("revisit_when") != _text(atom.get("disposition_revisit_when"))
        ):
            errors.append(f"problem_mining_final_decision_changed:{atom_id}")

    # Every source observation is explicitly dispositioned at all severities. A
    # derived atom can never compensate for a missing source decision.
    for atom_id, atom in atoms_by_id.items():
        role = _text(atom.get("evidence_role")) or "observation"
        if role in _DERIVED_EVIDENCE_ROLES or atom_is_idea_originated(atom):
            continue
        disposition_errors = atom_disposition_receipt_errors(atom, require_decided=True)
        if disposition_errors:
            errors.append(
                f"source_atom_without_explicit_disposition:{atom_id}:"
                + ",".join(disposition_errors)
            )
    return list(dict.fromkeys(errors))


__all__ = [
    "PROBLEM_MINING_EVIDENCE_SCHEMA_VERSION",
    "STAGE1_ATOM_DECISION_FIELDS",
    "ProblemMiningEvidenceReceipt",
    "ProblemMiningResponseContractError",
    "apply_problem_mining_decision_partition",
    "build_dry_run_miner_receipt",
    "build_failed_miner_receipt",
    "build_live_miner_receipt",
    "build_problem_mining_evidence_draft",
    "finalize_problem_mining_evidence_receipt",
    "immutable_atom_evidence_projection",
    "normalize_problem_mining_events",
    "parse_problem_mining_response_envelope",
    "problem_mining_evidence_receipt_ref",
    "verify_problem_mining_evidence_receipt",
]
