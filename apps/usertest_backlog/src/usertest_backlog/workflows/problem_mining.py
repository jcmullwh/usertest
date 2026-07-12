# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

import time as _time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from uuid import uuid4

from backlog_core.case_lineage import (
    verified_causal_identities_from_case_registry,
    verified_mechanism_identities_from_case_registry,
)
from backlog_miner.origin_evidence import materialize_origin_attachments
from backlog_miner.pipeline import model_invocation_manifest_ref
from backlog_miner.prompt_correction import (
    CorrectionRunResult,
    acquire_author_session,
    correction_metrics_with_session_acquisition,
)
from backlog_repo import validate_case_relation_receipt, write_case_relation_receipt

from usertest_backlog.shared import *
from usertest_backlog.workflows.problem_mining_evidence import (
    ProblemMiningResponseContractError,
    apply_problem_mining_decision_partition,
    build_dry_run_miner_receipt,
    build_failed_miner_receipt,
    build_live_miner_receipt,
    build_problem_mining_evidence_draft,
    finalize_problem_mining_evidence_receipt,
    normalize_problem_mining_events,
    parse_problem_mining_response_envelope,
    problem_mining_evidence_receipt_ref,
)


def _render_problem_records_markdown(
    problem_records: list[dict[str, Any]],
    *,
    title: str = "Problem Records",
) -> str:
    """Render a list of problem records as a human-readable Markdown document.

    Parameters
    ----------
    problem_records:
        Stage-1 problem record dicts.
    title:
        Document title.

    Returns
    -------
    str
        Markdown text.
    """
    lines: list[str] = [f"# {title}\n"]
    if not problem_records:
        lines.append("_No problem records produced._\n")
        return "\n".join(lines)

    for rec in problem_records:
        pid = rec.get("problem_id") or "(no id)"
        rec_title = rec.get("title") or pid
        severity = rec.get("severity") or "unknown"
        confidence = rec.get("confidence")
        conf_str = f"{float(confidence):.2f}" if isinstance(confidence, (int, float)) else "?"
        status = rec.get("problem_status") or "identified"
        lines.append(f"## {rec_title}")
        lines.append(
            f"**ID**: `{pid}` | **Severity**: {severity} | "
            f"**Confidence**: {conf_str} | **Status**: {status}\n"
        )
        problem_text = rec.get("problem") or ""
        if problem_text:
            lines.append(f"**Problem**: {problem_text}\n")
        impact = rec.get("user_impact") or ""
        if impact:
            lines.append(f"**User impact**: {impact}\n")
        summary = rec.get("evidence_summary") or ""
        if summary:
            lines.append(f"**Evidence summary**: {summary}\n")
        eids = rec.get("evidence_atom_ids") or []
        if eids:
            lines.append(
                f"**Evidence atoms** ({len(eids)}): "
                + ", ".join(f"`{e}`" for e in eids[:8])
                + (" …" if len(eids) > 8 else "")
                + "\n"
            )
        warn = rec.get("_parse_warning")
        if warn:
            lines.append(f"> ⚠ parse warning: {warn}\n")
        lines.append("")

    return "\n".join(lines)


def _synthesize_problem_records_from_atoms(
    atoms: list[dict[str, Any]],
    *,
    max_records: int,
) -> list[dict[str, Any]]:
    """Synthesize deterministic problem records from atoms (dry-run mode only).

    The six-stage pipeline uses LLMs for problem mining. In ``--dry-run`` mode the
    CLI must avoid network calls, but downstream stages (stage 2+) still require
    problem records in order to produce observable artifacts on offline fixtures.

    This function provides an explicit, inspectable, deterministic approximation:
    it groups atoms by ``source`` and emits one problem record per source.
    """

    def _severity_rank(atom: dict[str, Any]) -> int:
        score_hint = atom.get("severity_score_hint")
        if isinstance(score_hint, int):
            return max(0, min(3, score_hint))
        sev = _coerce_string(atom.get("severity_hint")) or "medium"
        return {"low": 0, "medium": 1, "high": 2, "blocker": 3}.get(sev, 1)

    def _severity_label(rank: int) -> str:
        return {0: "low", 1: "medium", 2: "high", 3: "blocker"}.get(rank, "medium")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for atom in atoms:
        source = _coerce_string(atom.get("source")) or "unknown"
        evidence_class = _coerce_string(atom.get("evidence_class"))
        if evidence_class == "proposal" or (
            evidence_class is None and source == "suggested_change"
        ):
            # A requested change is a proposed answer, not proof that its implied
            # problem exists.  Dry-run synthesis has no model investigation that
            # could bind it to independent observed evidence, so leave it
            # explicitly deferred instead of manufacturing a problem case from it.
            continue
        grouped.setdefault(source, []).append(atom)

    # Order groups deterministically: higher severity first, then more atoms, then source name.
    group_order: list[tuple[int, int, str]] = []
    for source, group_atoms in grouped.items():
        max_rank = 0
        for a in group_atoms:
            max_rank = max(max_rank, _severity_rank(a))
        group_order.append((max_rank, len(group_atoms), source))
    group_order.sort(key=lambda t: (-t[0], -t[1], t[2]))

    title_by_source: dict[str, str] = {
        "run_failure_event": "Run failures observed",
        "command_failure": "Command failures observed",
        "confusion_point": "User confusion observed",
        "suggested_change": "Suggested changes imply gaps",
        "report_validation_error": "Report validation errors observed",
    }
    impact_by_source: dict[str, str] = {
        "run_failure_event": "Runs fail to complete, blocking progress.",
        "command_failure": "Commands fail during execution, blocking tasks.",
        "confusion_point": "Users are confused about expected behavior or usage.",
        "suggested_change": "Users suggest changes, indicating missing guidance or friction.",
        "report_validation_error": "Report output is invalid, breaking automation and analysis.",
    }

    out: list[dict[str, Any]] = []
    for idx, (_max_rank, _count, source) in enumerate(group_order, start=1):
        if len(out) >= max_records:
            break
        group_atoms = grouped[source]
        group_atoms_sorted = sorted(group_atoms, key=lambda a: str(a.get("atom_id") or ""))
        evidence_atom_ids = [
            atom_id
            for atom_id in (str(a.get("atom_id") or "").strip() for a in group_atoms_sorted)
            if atom_id
        ]
        if not evidence_atom_ids:
            continue

        max_rank = 0
        run_ids: set[str] = set()
        agents: set[str] = set()
        for atom in group_atoms_sorted:
            max_rank = max(max_rank, _severity_rank(atom))
            run_id = _coerce_string(atom.get("run_id"))
            if run_id:
                run_ids.add(run_id)
            agent = _coerce_string(atom.get("agent"))
            if agent:
                agents.add(agent)

        severity = _severity_label(max_rank)
        distinct_runs = len(run_ids)
        distinct_agents = len(agents)

        # Confidence heuristic: more breadth and more evidence implies higher confidence.
        confidence = (
            0.35
            + 0.12 * min(3, max(0, distinct_runs - 1))
            + 0.06 * min(4, max(0, len(evidence_atom_ids) - 1))
        )
        if severity in {"high", "blocker"}:
            confidence += 0.10
        confidence = max(0.0, min(0.90, confidence))

        # Evidence summary: short excerpts from the first few atoms.
        excerpts: list[str] = []
        for atom in group_atoms_sorted[:3]:
            text = _coerce_string(atom.get("text")) or ""
            if text:
                excerpt = text if len(text) <= 140 else text[:140] + "..."
                excerpts.append(excerpt)
        evidence_summary = " | ".join(excerpts) if excerpts else f"{len(evidence_atom_ids)} atoms"

        slug = slugify(f"dryrun-{source}-{idx}")
        title = title_by_source.get(source, source.replace("_", " ").strip().title())
        user_impact = impact_by_source.get(source, "Users are affected by this issue.")

        out.append(
            {
                "problem_id": f"problem:{slug}",
                "title": title,
                "problem": f"Evidence atoms of type `{source}` indicate a recurring issue.",
                "user_impact": user_impact,
                "severity": severity,
                "confidence": round(confidence, 4),
                "evidence_atom_ids": evidence_atom_ids,
                "evidence_summary": evidence_summary,
                "problem_status": "identified",
                "_dry_run_synthesized": True,
                "_dry_run_meta": {
                    "source": source,
                    "distinct_runs": distinct_runs,
                    "distinct_agents": distinct_agents,
                    "evidence_atoms_cited": len(evidence_atom_ids),
                },
            }
        )

    return out


def _atoms_for_problem_mining_prompt(
    atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a compact, prompt-friendly projection of backlog atoms.

    Stage 1 problem mining feeds evidence atoms to an LLM. Earlier projections retained
    only the headline ``text`` and silently removed command output, issue details, artifact
    references, and other evidence-bearing fields. This projection retains all unique atom
    evidence while removing runner decision state and large text already present in
    ``text``. Operational candidates are the narrow exception: their complete hash-bound
    occurrence ledger remains in the persisted atom, while Stage 1 receives an explicit
    bounded causal summary with counts and ledger digests. Bounded chunk-aligned jobs
    provide the prompt-size control.

    Parameters
    ----------
    atoms:
        Raw evidence atoms extracted from run history.

    Returns
    -------
    list[dict[str, Any]]
        List of compact atom dicts suitable for embedding in stage-1 prompts.
    """

    compact: list[dict[str, Any]] = []
    for atom in atoms:
        atom_id = _coerce_string(atom.get("atom_id"))
        if atom_id is None:
            continue

        text = _coerce_string(atom.get("text")) or ""
        is_operational_candidate = (
            _coerce_string(atom.get("source")) == "operational_failure_candidate"
        )

        linked_raw = atom.get("linked_atom_ids")
        linked = (
            list(
                dict.fromkeys(
                    value.strip()
                    for value in linked_raw
                    if isinstance(value, str) and value.strip()
                )
            )
            if isinstance(linked_raw, list)
            else []
        )

        projection: dict[str, Any] = {
            "atom_id": atom_id,
            "run_rel": _coerce_string(atom.get("run_rel")),
            "source": _coerce_string(atom.get("source")),
            "severity_hint": _coerce_string(atom.get("severity_hint")),
            "origin_run_id": _coerce_string(atom.get("origin_run_id")),
            "origin_stage": _coerce_string(atom.get("origin_stage")),
            "parent_case_id": _coerce_string(atom.get("parent_case_id")),
            "derived_from_atom_ids": (
                []
                if is_operational_candidate
                else _coerce_string_list(atom.get("derived_from_atom_ids"))
            ),
            "evidence_role": _coerce_string(atom.get("evidence_role")),
            "disposition": _coerce_string(atom.get("disposition")),
            "text": text,
            "linked_atom_ids": linked,
        }
        excluded_fields = {
            "case_id",
            "supporting_case_ids",
            "disposition_status",
            "disposition_receipt",
            "severity_score_hint",
            # Artifact text is already composed into ``text`` by extraction. Retain the
            # artifact reference and truncation marker, not a second copy of each excerpt.
            "excerpt_head",
            "excerpt_tail",
        }
        if is_operational_candidate:
            # The persisted atom retains the complete, hash-bound occurrence ledger.
            # Stage 1 receives the bounded causal projection plus explicit counts and
            # digests; per-run hashes/IDs are audit evidence, not model context.
            excluded_fields.update(
                {
                    "operational_candidate_receipt",
                    "source_derived_atom_ids",
                    "related_parent_case_ids",
                    "derived_source_record_identities",
                    "derived_source_roots",
                    "derived_source_root_kinds",
                }
            )
            projection["operational_full_receipt_excluded_from_prompt"] = True
        for key, value in atom.items():
            if key in projection or key in excluded_fields or value is None:
                continue
            if isinstance(value, str) and len(value) >= 100 and value in text:
                continue
            if key == "attachments" and isinstance(value, list):
                value = [
                    {
                        nested_key: nested_value
                        for nested_key, nested_value in item.items()
                        if nested_key not in {"excerpt_head", "excerpt_tail"}
                    }
                    if isinstance(item, dict)
                    else item
                    for item in value
                ]
            projection[key] = value
        compact.append(projection)

    return compact


_PROBLEM_MINING_CHUNK_MAX_BYTES = 55_000
_PROBLEM_MINING_JOB_MAX_CHUNKS = 3
_PROBLEM_MINING_JOB_MAX_ATOMS = 100
_PROBLEM_MINING_JOB_MAX_BYTES = 150_000
_PROBLEM_RELATION_REVIEW_MAX_FOCI = 16


def _format_problem_mining_atom_markdown(atom: dict[str, Any]) -> str:
    """Render the complete prompt projection into the required agent-readable view."""

    import json as _json

    atom_id = _coerce_string(atom.get("atom_id")) or "(missing atom_id)"
    run_rel = _coerce_string(atom.get("run_rel")) or ""
    source = _coerce_string(atom.get("source")) or ""
    severity = _coerce_string(atom.get("severity_hint")) or ""
    linked_raw = atom.get("linked_atom_ids")
    linked = (
        [value for value in linked_raw if isinstance(value, str) and value.strip()]
        if isinstance(linked_raw, list)
        else []
    )
    text = _coerce_string(atom.get("text")) or ""
    displayed_fields = {
        "atom_id",
        "run_rel",
        "source",
        "severity_hint",
        "linked_atom_ids",
        "text",
    }
    structured_context = {
        key: value
        for key, value in atom.items()
        if key not in displayed_fields and value is not None
    }
    lines = [
        f"## {atom_id}",
        "",
        f"- run_rel: {run_rel}",
        f"- source: {source}",
        f"- severity_hint: {severity}",
        f"- linked_atom_ids: {', '.join(linked) if linked else '(none)'}",
        "",
        "Observed text:",
        text.rstrip(),
    ]
    if structured_context:
        lines.extend(
            [
                "",
                "Structured evidence context:",
                "```json",
                _json.dumps(structured_context, ensure_ascii=False, indent=2),
                "```",
            ]
        )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _order_problem_mining_atoms_for_local_context(
    prompt_atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep explicitly linked evidence together without changing corpus membership.

    Linked atoms often contain the observation, command failure, and surrounding context for
    one incident.  Splitting those atoms across model calls makes the miner reason from a
    symptom fragment.  This stable connected-component ordering keeps linked evidence local
    when the bounded job limits allow it; oversized components are still split rather than
    creating an unbounded prompt.
    """

    atom_ids = [str(atom.get("atom_id") or "") for atom in prompt_atoms]
    index_by_id = {atom_id: index for index, atom_id in enumerate(atom_ids) if atom_id}
    parent = list(range(len(prompt_atoms)))

    def _find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def _union(left: int, right: int) -> None:
        left_root = _find(left)
        right_root = _find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    for index, atom in enumerate(prompt_atoms):
        linked_raw = atom.get("linked_atom_ids")
        linked = linked_raw if isinstance(linked_raw, list) else []
        for linked_id in linked:
            if isinstance(linked_id, str) and linked_id in index_by_id:
                _union(index, index_by_id[linked_id])

    components: dict[int, list[int]] = {}
    for index in range(len(prompt_atoms)):
        components.setdefault(_find(index), []).append(index)
    ordered_components = sorted(components.values(), key=lambda indexes: min(indexes))
    return [prompt_atoms[index] for indexes in ordered_components for index in indexes]


def _partition_problem_mining_chunks(
    prompt_atoms: list[dict[str, Any]],
    *,
    chunk_max_bytes: int = _PROBLEM_MINING_CHUNK_MAX_BYTES,
) -> list[list[dict[str, Any]]]:
    """Partition complete atom payloads into deterministic file-readable chunks."""

    import json as _json

    json_base_bytes = len(b"[\n]\n")
    markdown_base_bytes = 64

    def _atom_line_bytes(atom: dict[str, Any]) -> int:
        raw = _json.dumps(atom, ensure_ascii=False, separators=(",", ":"))
        return len(f"  {raw},\n".encode())

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_json_bytes = json_base_bytes
    current_markdown_bytes = markdown_base_bytes
    for atom in _order_problem_mining_atoms_for_local_context(prompt_atoms):
        atom_json_bytes = _atom_line_bytes(atom)
        atom_markdown_bytes = len(_format_problem_mining_atom_markdown(atom).encode())
        if (
            max(
                atom_json_bytes + json_base_bytes,
                atom_markdown_bytes + markdown_base_bytes,
            )
            > chunk_max_bytes
        ):
            atom_id = _coerce_string(atom.get("atom_id")) or "(missing atom_id)"
            raise ValueError(
                "stage1 atoms chunking failed: a single atom payload is too large for the "
                f"file-read tool limits (atom_id={atom_id} "
                f"json_bytes~{atom_json_bytes} markdown_bytes~{atom_markdown_bytes} "
                f"chunk_max_bytes={chunk_max_bytes}). Refuse to truncate evidence text; "
                "reduce the atom projection or increase chunk_max_bytes."
            )
        if current and (
            current_json_bytes + atom_json_bytes > chunk_max_bytes
            or current_markdown_bytes + atom_markdown_bytes > chunk_max_bytes
        ):
            chunks.append(current)
            current = []
            current_json_bytes = json_base_bytes
            current_markdown_bytes = markdown_base_bytes
        current.append(atom)
        current_json_bytes += atom_json_bytes
        current_markdown_bytes += atom_markdown_bytes
    if current:
        chunks.append(current)
    return chunks


def _problem_mining_job_batches(
    prompt_atoms: list[dict[str, Any]],
    *,
    chunk_max_bytes: int = _PROBLEM_MINING_CHUNK_MAX_BYTES,
    max_chunks: int = _PROBLEM_MINING_JOB_MAX_CHUNKS,
    max_atoms: int = _PROBLEM_MINING_JOB_MAX_ATOMS,
    max_bytes: int = _PROBLEM_MINING_JOB_MAX_BYTES,
) -> list[list[dict[str, Any]]]:
    """Build bounded, chunk-aligned model jobs that cover the corpus exactly once."""

    import json as _json
    import os as _os

    if max_chunks <= 0 or max_atoms <= 0 or max_bytes <= 0:
        raise ValueError("problem_mining_job_limits_must_be_positive")

    chunks = _partition_problem_mining_chunks(
        prompt_atoms,
        chunk_max_bytes=chunk_max_bytes,
    )

    def _write_text_bytes(content: str) -> int:
        # ``Path.write_text`` uses the platform text newline convention.  Account
        # for that conversion so byte ceilings describe bytes actually written on
        # disk (not only UTF-8 bytes of the in-memory LF representation).
        translated = content.replace("\n", _os.linesep)
        return len(translated.encode("utf-8"))

    def _chunk_bytes(chunk: list[dict[str, Any]]) -> int:
        lines = ["["]
        for index, atom in enumerate(chunk):
            suffix = "," if index < len(chunk) - 1 else ""
            raw = _json.dumps(atom, ensure_ascii=False, separators=(",", ":"))
            lines.append(f"  {raw}{suffix}")
        lines.append("]")
        json_bytes = _write_text_bytes("\n".join(lines) + "\n")
        # Mirror ``_write_chunked_problem_mining_workspace`` byte-for-byte.  Each
        # formatted atom already ends with a newline, while the writer joins the
        # parts with another newline and normalizes the final newline.  The older
        # approximation omitted those inter-atom bytes and could exceed a job's
        # advertised byte ceiling even after splitting.
        markdown_parts = ["# Atom Chunk 000", ""]
        markdown_parts.extend(_format_problem_mining_atom_markdown(atom) for atom in chunk)
        markdown_bytes = _write_text_bytes("\n".join(markdown_parts).rstrip() + "\n")
        return max(json_bytes, markdown_bytes)

    # A byte-bounded workspace chunk can still contain more atoms than one model
    # job allows (for example, hundreds of very small structured events).  Treating
    # a chunk as indivisible made ``max_atoms`` advisory and allowed a single
    # oversized job.  Split such chunks losslessly before composing jobs; the
    # workspace writer will create fresh, hash-bound chunk files for each job.
    bounded_chunks: list[list[dict[str, Any]]] = []
    for chunk in chunks:
        current: list[dict[str, Any]] = []
        for atom in chunk:
            candidate = [*current, atom]
            if len(candidate) <= max_atoms and _chunk_bytes(candidate) <= max_bytes:
                current = candidate
                continue
            if current:
                bounded_chunks.append(current)
                current = []
            single = [atom]
            single_bytes = _chunk_bytes(single)
            if single_bytes > max_bytes:
                atom_id = _coerce_string(atom.get("atom_id")) or "(missing atom_id)"
                raise ValueError(
                    "problem_mining_job_atom_exceeds_max_bytes:"
                    f"{atom_id}:bytes={single_bytes}:max_bytes={max_bytes}"
                )
            current = single
        if current:
            bounded_chunks.append(current)

    batches: list[list[dict[str, Any]]] = []
    batch_chunks: list[list[dict[str, Any]]] = []
    batch_atoms = 0
    batch_bytes = 0
    for chunk in bounded_chunks:
        chunk_bytes = _chunk_bytes(chunk)
        would_exceed = bool(batch_chunks) and (
            len(batch_chunks) >= max_chunks
            or batch_atoms + len(chunk) > max_atoms
            or batch_bytes + chunk_bytes > max_bytes
        )
        if would_exceed:
            batches.append([atom for existing in batch_chunks for atom in existing])
            batch_chunks = []
            batch_atoms = 0
            batch_bytes = 0
        batch_chunks.append(chunk)
        batch_atoms += len(chunk)
        batch_bytes += chunk_bytes
    if batch_chunks:
        batches.append([atom for existing in batch_chunks for atom in existing])
    return batches


def _reconcile_problem_mining_reviews(
    *,
    primary_records: list[dict[str, Any]],
    primary_decisions: list[dict[str, Any]],
    review_records: list[dict[str, Any]],
    review_decisions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Conservatively reconcile a full independent coverage/depth review.

    The second pass covers every assigned atom.  A primary ``supports_case``
    decision is retained only when the reviewer emits the same problem record
    and explicitly supports the same problem ID.  The reviewer may still recover
    a problem missed by the primary pass under a new ID.
    """

    import json as _json
    from hashlib import sha256

    primary_by_atom = {
        str(decision.get("atom_id")): dict(decision)
        for decision in primary_decisions
        if isinstance(decision.get("atom_id"), str)
    }
    review_by_atom = {
        str(decision.get("atom_id")): dict(decision)
        for decision in review_decisions
        if isinstance(decision.get("atom_id"), str)
    }
    expected_review_ids = set(primary_by_atom)
    if set(review_by_atom) != expected_review_ids:
        raise ValueError("problem_mining_coverage_review_partition_mismatch")

    primary_by_problem_id = {
        str(record.get("problem_id")): dict(record)
        for record in primary_records
        if isinstance(record.get("problem_id"), str)
    }
    primary_problem_ids = set(primary_by_problem_id)
    claim_fields = (
        "problem_id",
        "title",
        "problem",
        "user_impact",
        "severity",
        "confidence",
        "evidence_atom_ids",
        "evidence_summary",
        "problem_status",
        # Routing-only records use these model-assigned semantic keys to make
        # arbitrary cross-partition themes meet. They are part of that claim and
        # must not bypass the independent comparison merely because ordinary
        # stage-1 records do not carry them.
        "routing_keys",
    )

    def _claim_projection(record: dict[str, Any]) -> dict[str, Any]:
        return {field: record.get(field) for field in claim_fields}

    review_id_map: dict[str, str] = {}
    independently_confirmed_primary_ids: set[str] = set()
    normalized_review_records: list[dict[str, Any]] = []
    for record in review_records:
        normalized = dict(record)
        problem_id = _coerce_string(normalized.get("problem_id"))
        if problem_id is None:
            raise ValueError("problem_mining_coverage_review_problem_id_missing")
        mapped_id = problem_id
        primary_record = primary_by_problem_id.get(problem_id)
        confirms_primary = primary_record is not None and _claim_projection(
            normalized
        ) == _claim_projection(primary_record)
        if confirms_primary:
            independently_confirmed_primary_ids.add(problem_id)
        elif mapped_id in primary_problem_ids or mapped_id in review_id_map.values():
            digest = sha256(
                _json.dumps(
                    normalized,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:10]
            mapped_id = f"{problem_id}-review-{digest}"
        review_id_map[problem_id] = mapped_id
        normalized["problem_id"] = mapped_id
        if not confirms_primary:
            normalized_review_records.append(normalized)

    for decision in review_by_atom.values():
        raw_ids = decision.get("problem_ids")
        ids = raw_ids if isinstance(raw_ids, list) else []
        decision["problem_ids"] = [
            review_id_map.get(str(problem_id), str(problem_id)) for problem_id in ids
        ]

    final_decisions: list[dict[str, Any]] = []
    for atom_id, primary in primary_by_atom.items():
        review = review_by_atom[atom_id]
        if primary.get("disposition") == "supports_case":
            primary_ids = sorted(_coerce_string_list(primary.get("problem_ids")))
            review_ids = sorted(_coerce_string_list(review.get("problem_ids")))
            confirmed = bool(primary_ids) and all(
                problem_id in independently_confirmed_primary_ids and problem_id in review_ids
                for problem_id in primary_ids
            )
            if review.get("disposition") == "supports_case" and confirmed:
                final_decisions.append(
                    {
                        "atom_id": atom_id,
                        "disposition": "supports_case",
                        "problem_ids": review_ids,
                        "rationale": (
                            "Primary support was independently confirmed against the full "
                            "atom evidence. Primary: "
                            + (_coerce_string(primary.get("rationale")) or "(missing)")
                            + " Review: "
                            + (_coerce_string(review.get("rationale")) or "(missing)")
                        ),
                        "revisit_when": None,
                    }
                )
            elif review.get("disposition") == "supports_case" and review_ids:
                # The primary claim was not reproduced verbatim, but the independent
                # pass may have found a different evidence-grounded problem. Retain
                # only that reviewed claim; the unconfirmed primary record falls out
                # of ``referenced_ids`` below.
                final_decisions.append(review)
            else:
                final_decisions.append(
                    {
                        "atom_id": atom_id,
                        "disposition": "unresolved",
                        "problem_ids": [],
                        "rationale": (
                            "The primary pass proposed a problem, but the independent "
                            "coverage/depth review did not confirm the same evidence-bound "
                            "claim. Primary: "
                            + (_coerce_string(primary.get("rationale")) or "(missing)")
                            + " Review: "
                            + (_coerce_string(review.get("rationale")) or "(missing)")
                        ),
                        "revisit_when": None,
                    }
                )
            continue
        if review.get("disposition") == "supports_case":
            final_decisions.append(review)
            continue
        primary_disposition = _coerce_string(primary.get("disposition")) or "unresolved"
        review_disposition = _coerce_string(review.get("disposition")) or "unresolved"
        primary_rationale = _coerce_string(primary.get("rationale")) or "(missing)"
        review_rationale = _coerce_string(review.get("rationale")) or "(missing)"
        if primary_disposition == review_disposition:
            disposition = primary_disposition
            rationale = (
                f"Primary review: {primary_rationale} Independent review: {review_rationale}"
            )
        elif "unresolved" in {primary_disposition, review_disposition}:
            disposition = "unresolved"
            rationale = (
                "Independent reviews disagreed and at least one found material uncertainty. "
                f"Primary: {primary_rationale} Review: {review_rationale}"
            )
        elif "deferred" in {primary_disposition, review_disposition}:
            disposition = "deferred"
            rationale = (
                "Independent reviews disagreed; a concrete missing-evidence trigger remains. "
                f"Primary: {primary_rationale} Review: {review_rationale}"
            )
        else:
            disposition = "unresolved"
            rationale = (
                "Independent reviews disagreed between duplicate/noise classifications. "
                f"Primary: {primary_rationale} Review: {review_rationale}"
            )
        revisit_values = [
            value
            for value in (
                _coerce_string(primary.get("revisit_when")),
                _coerce_string(review.get("revisit_when")),
            )
            if value is not None
        ]
        final_decisions.append(
            {
                "atom_id": atom_id,
                "disposition": disposition,
                "problem_ids": [],
                "rationale": rationale,
                "revisit_when": (
                    " / ".join(dict.fromkeys(revisit_values))
                    if disposition == "deferred" and revisit_values
                    else None
                ),
            }
        )

    referenced_ids = {
        problem_id
        for decision in final_decisions
        for problem_id in (
            decision.get("problem_ids") if isinstance(decision.get("problem_ids"), list) else []
        )
        if isinstance(problem_id, str)
    }
    records = [
        record
        for record in [*primary_records, *normalized_review_records]
        if _coerce_string(record.get("problem_id")) in referenced_ids
    ]
    return records, final_decisions


def _coverage_depth_review_prompt(
    *,
    primary_records: Sequence[Mapping[str, Any]],
    primary_decisions: Sequence[Mapping[str, Any]],
    template_text: str,
    stage_guidance_text: str,
    atoms_placeholder: str,
    max_records_per_miner: int,
    bound_feedback: Mapping[str, Any] | None = None,
) -> str:
    """Build the independent Stage-1 claim audit for an exact primary frontier.

    The optional feedback is not a request to agree with another model.  It tells the
    independent reviewer why the primary frontier changed while retaining the same
    evidence-first review contract used by an ordinary Stage-1 run.
    """

    import json as _json

    feedback_section = (
        "\n\nBOUND INDEPENDENT QUALIFICATION FEEDBACK (a finding to evaluate, not "
        "ground truth):\n"
        + _json.dumps(dict(bound_feedback), ensure_ascii=False, indent=2, sort_keys=True)
        if isinstance(bound_feedback, Mapping)
        else ""
    )
    return (
        "INDEPENDENT FULL COVERAGE AND DEPTH REVIEW\n\n"
        "Review every assigned atom from the evidence files, including atoms that the "
        "primary pass attached to a problem and atoms it did not attach. Do not trust "
        "the primary pass merely because it cited an atom. For every primary "
        "supports_case claim, decide whether the complete observed evidence directly "
        "establishes that exact problem. To confirm it, emit the corresponding primary "
        "problem record verbatim (including its problem_id and evidence_atom_ids) and "
        "reference that same problem_id from the atom decision. Changed wording or a "
        "different ID is a different claim, not confirmation. If a primary claim is "
        "surface-level or unsupported, do not copy it; use unresolved or another "
        "evidence-backed non-support disposition. Independently recover any concrete, "
        "unactioned observed problem the primary pass missed. Consider behavior, "
        "infrastructure, schema/output, and usability without forcing a problem that "
        "the evidence does not establish. Return the same strict response contract "
        "with exactly one decision per assigned atom."
        + feedback_section
        + "\n\nPRIMARY PROBLEM RECORDS AND DECISIONS (claims to audit, not evidence):\n"
        + _json.dumps(
            {
                "problem_records": [dict(item) for item in primary_records],
                "atom_decisions": [dict(item) for item in primary_decisions],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n\n"
        + template_text.replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
        .replace("{{ATOMS_JSON}}", atoms_placeholder)
        .replace("{{MAX_RECORDS_PER_MINER}}", str(max_records_per_miner))
    )


def _build_composite_miner_receipt(
    *,
    tag: str,
    template_name: str,
    assigned_atom_ids: Sequence[str],
    eligible_atom_ids: Sequence[str],
    primary_records: Sequence[Mapping[str, Any]],
    primary_decisions: Sequence[Mapping[str, Any]],
    primary_receipt: Mapping[str, Any],
    review_records: Sequence[Mapping[str, Any]],
    review_decisions: Sequence[Mapping[str, Any]],
    review_receipt: Mapping[str, Any],
    primary_normalized_events_path: Path,
    primary_workspace_dir: Path,
    primary_workspace_manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Reconcile and retain both authored Stage-1 component frontiers.

    A composite receipt is deliberately more than the latest model receipt.  It keeps
    the primary claim set, the independent review claim set, and the aggregate decision
    that downstream relation review consumed.  Qualification repair can therefore
    replace one exact author without fabricating or discarding the other author.
    """

    import json as _json

    primary_records_copy = [dict(item) for item in primary_records]
    primary_decisions_copy = [dict(item) for item in primary_decisions]
    review_records_copy = [dict(item) for item in review_records]
    review_decisions_copy = [dict(item) for item in review_decisions]
    final_records, final_decisions = _reconcile_problem_mining_reviews(
        primary_records=primary_records_copy,
        primary_decisions=primary_decisions_copy,
        review_records=review_records_copy,
        review_decisions=review_decisions_copy,
    )
    component_projection = {
        "primary_response_sha256": primary_receipt.get("response_sha256"),
        "coverage_depth_review_response_sha256": review_receipt.get("response_sha256"),
        "primary_problem_records": primary_records_copy,
        "primary_atom_decisions": primary_decisions_copy,
        "coverage_depth_review_problem_records": review_records_copy,
        "coverage_depth_review_atom_decisions": review_decisions_copy,
    }
    composite = build_live_miner_receipt(
        tag=tag,
        template_name=template_name,
        assigned_atom_ids=list(assigned_atom_ids),
        eligible_atom_ids=list(eligible_atom_ids),
        records=final_records,
        decisions=final_decisions,
        response_text=_json.dumps(
            component_projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        normalized_events_path=primary_normalized_events_path,
        workspace_dir=primary_workspace_dir,
        workspace_manifest=primary_workspace_manifest,
    )
    composite.update(
        {
            "primary_pass": dict(primary_receipt),
            "non_support_review": dict(review_receipt),
            "review_scope": "all_assigned_atoms_positive_and_non_support",
            "primary_problem_records": primary_records_copy,
            "primary_atom_decisions": primary_decisions_copy,
            "coverage_depth_review_problem_records": review_records_copy,
            "coverage_depth_review_atom_decisions": review_decisions_copy,
            "reconciled_problem_records": [dict(item) for item in final_records],
            "reconciled_atom_decisions": [dict(item) for item in final_decisions],
        }
    )
    composite["attempt_history"] = list(primary_receipt.get("attempt_history", []))
    composite["successful_attempt_tag"] = primary_receipt.get("successful_attempt_tag")
    return composite, final_records, final_decisions


def _preserve_primary_after_coverage_review_failure(
    *,
    primary_receipt: dict[str, Any],
    review_receipt: dict[str, Any],
    review_failure: str,
) -> dict[str, Any]:
    """Retain verified primary evidence while making the failed audit non-exportable."""

    preserved = dict(primary_receipt)
    preserved["status"] = "review_failed_primary_preserved"
    preserved["primary_pass"] = dict(primary_receipt)
    preserved["non_support_review"] = review_receipt
    preserved["review_scope"] = "all_assigned_atoms_positive_and_non_support"
    preserved["review_failure"] = review_failure
    return preserved


def _write_chunked_problem_mining_atoms_workspace(
    *,
    workspace_dir: Path,
    prompt_atoms: list[dict[str, Any]],
    max_records_per_miner: int,
    assigned_atom_ids: list[str] | None = None,
    chunk_max_bytes: int = _PROBLEM_MINING_CHUNK_MAX_BYTES,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Write stage-1 atom payload files into *workspace_dir* and return the manifest.

    Each bounded stage 1 job needs access to its full atom evidence text, but provider file-read tools
    commonly enforce token limits that make a single large JSON file unreadable. To avoid
    "randomly chopping off text" while still fitting inside tool limits, this helper writes
    a small manifest file plus multiple chunk files that together contain the full assigned job.

    Written files
    ------------
    - ``atoms.json`` (manifest; small JSON object)
    - ``atoms_index.md`` (compact, line-oriented index of every atom)
    - ``atoms_by_id/atom_####.md`` (one markdown file per atom)
    - ``atoms_chunks/atoms_###.json`` (chunk files; each is a JSON array of atom dicts)
    - ``atoms_text/atoms_###.md`` (markdown view of each chunk for file-read tools)

    The manifest includes a stable list of chunk files; a prompt can instruct the model to:
    1) Read ``atoms.json``.
    2) Read each file listed under ``chunks[*].file``.

    Parameters
    ----------
    workspace_dir:
        Stage-1 miner workspace directory.
    prompt_atoms:
        Atom projection returned by ``_atoms_for_problem_mining_prompt``.
    max_records_per_miner:
        Legacy compatibility hint recorded as ignored; evidence determines record count.
    chunk_max_bytes:
        Maximum bytes per chunk file (UTF-8). This value is recorded in the manifest so
        it is not a silent default.

    Returns
    -------
    dict[str, Any]
        Manifest JSON object written to ``atoms.json``.

    Raises
    ------
    ValueError
        When a single atom payload exceeds ``chunk_max_bytes`` and cannot be chunked
        further without truncation.
    """
    import json as _json
    from hashlib import sha256

    prompt_atom_ids = [
        atom_id
        for atom in prompt_atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        if atom_id is not None
    ]
    if len(prompt_atom_ids) != len(prompt_atoms) or len(prompt_atom_ids) != len(
        set(prompt_atom_ids)
    ):
        raise ValueError("stage1 atoms workspace requires unique non-empty atom IDs")
    assigned_ids = (
        sorted(prompt_atom_ids)
        if assigned_atom_ids is None
        else sorted(
            {
                atom_id.strip()
                for atom_id in assigned_atom_ids
                if isinstance(atom_id, str) and atom_id.strip()
            }
        )
    )
    if not set(assigned_ids).issubset(set(prompt_atom_ids)):
        raise ValueError("stage1 assigned atom IDs must be contained in the eligible corpus")
    assigned_set = set(assigned_ids)

    if workspace_dir.is_symlink():
        raise ValueError("stage1 atoms workspace may not be a symlink")
    if workspace_dir.exists():
        if not workspace_dir.is_dir() or any(workspace_dir.iterdir()):
            raise ValueError("stage1 atoms workspace must be new or empty")
    else:
        workspace_dir.mkdir(parents=True)
    attachment_evidence = materialize_origin_attachments(
        atoms=prompt_atoms,
        workspace_dir=workspace_dir,
        source_root=source_root,
    )
    attachment_refs_by_atom: dict[str, list[dict[str, Any]]] = {}
    for ref in attachment_evidence.get("atom_refs", []):
        if not isinstance(ref, dict):
            continue
        atom_id = _coerce_string(ref.get("atom_id"))
        if atom_id is not None:
            attachment_refs_by_atom.setdefault(atom_id, []).append(dict(ref))
    attachment_errors_by_atom: dict[str, list[dict[str, Any]]] = {}
    for error in attachment_evidence.get("errors", []):
        if not isinstance(error, dict):
            continue
        atom_id = _coerce_string(error.get("atom_id"))
        if atom_id is not None:
            attachment_errors_by_atom.setdefault(atom_id, []).append(dict(error))
    workspace_atoms: list[dict[str, Any]] = []
    for atom in prompt_atoms:
        projected = dict(atom)
        atom_id = _coerce_string(projected.get("atom_id"))
        refs = attachment_refs_by_atom.get(atom_id or "", [])
        errors = attachment_errors_by_atom.get(atom_id or "", [])
        if refs or errors:
            projected["origin_attachment_evidence"] = {
                "materialized_refs": refs,
                "materialization_errors": errors,
                "workspace_manifest": attachment_evidence.get("manifest_file"),
            }
        workspace_atoms.append(projected)
    chunks_dir = workspace_dir / "atoms_chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    text_dir = workspace_dir / "atoms_text"
    text_dir.mkdir(parents=True, exist_ok=True)
    atoms_by_id_dir = workspace_dir / "atoms_by_id"
    atoms_by_id_dir.mkdir(parents=True, exist_ok=True)

    chunks = _partition_problem_mining_chunks(
        workspace_atoms,
        chunk_max_bytes=chunk_max_bytes,
    )

    def _preview_text(value: Any, *, max_chars: int = 500) -> str:
        text = _coerce_string(value) or ""
        text = " ".join(text.replace("\r", "\n").split())
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "..."

    chunk_entries: list[dict[str, Any]] = []
    index_lines: list[str] = [
        "# Problem Mining Atom Index",
        "",
        "This is a compact index of every evidence atom. Use the listed markdown chunk",
        "for full text details when the preview is not enough.",
        "",
    ]
    total_chunk_bytes = 0
    total_text_chunk_bytes = 0
    total_atom_file_bytes = 0
    atom_file_count = 0
    atom_file_entries: list[dict[str, Any]] = []

    for idx, atoms_chunk in enumerate(chunks, start=1):
        rel_path = Path("atoms_chunks") / f"atoms_{idx:03d}.json"
        chunk_path = workspace_dir / rel_path
        rel_text_path = Path("atoms_text") / f"atoms_{idx:03d}.md"
        text_path = workspace_dir / rel_text_path

        lines: list[str] = ["["]
        for atom_idx, atom in enumerate(atoms_chunk):
            raw = _json.dumps(atom, ensure_ascii=False, separators=(",", ":"))
            suffix = "," if atom_idx < (len(atoms_chunk) - 1) else ""
            lines.append(f"  {raw}{suffix}")
        lines.append("]")
        content = "\n".join(lines) + "\n"

        chunk_path.write_text(content, encoding="utf-8")
        chunk_bytes = chunk_path.stat().st_size
        total_chunk_bytes += chunk_bytes

        text_parts = [f"# Atom Chunk {idx:03d}", ""]
        for atom in atoms_chunk:
            atom_file_count += 1
            atom_id = _coerce_string(atom.get("atom_id")) or "(missing atom_id)"
            source = _coerce_string(atom.get("source")) or ""
            severity = _coerce_string(atom.get("severity_hint")) or ""
            run_rel = _coerce_string(atom.get("run_rel")) or ""
            preview = _preview_text(atom.get("text"))
            rel_atom_path = Path("atoms_by_id") / f"atom_{atom_file_count:04d}.md"
            atom_file_content = _format_problem_mining_atom_markdown(atom)
            atom_file_path = workspace_dir / rel_atom_path
            atom_file_path.write_text(atom_file_content, encoding="utf-8")
            atom_file_bytes = atom_file_path.stat().st_size
            atom_file_sha256 = sha256(atom_file_path.read_bytes()).hexdigest()
            total_atom_file_bytes += atom_file_bytes
            atom_file_entries.append(
                {
                    "atom_id": atom_id,
                    "file": rel_atom_path.as_posix(),
                    "bytes": atom_file_bytes,
                    "sha256": atom_file_sha256,
                    "assigned": atom_id in assigned_set,
                }
            )
            index_lines.append(
                f"- `{atom_id}` | atom_file: `{rel_atom_path.as_posix()}` "
                f"| chunk_file: `{rel_text_path.as_posix()}` "
                f"| source: `{source}` | severity: `{severity}` | run: `{run_rel}` "
                f"| assigned: `{str(atom_id in assigned_set).lower()}` | preview: {preview}"
            )
            text_parts.append(atom_file_content)
        text_content = "\n".join(text_parts).rstrip() + "\n"
        text_path.write_text(text_content, encoding="utf-8")
        text_bytes = text_path.stat().st_size
        total_text_chunk_bytes += text_bytes

        chunk_entries.append(
            {
                "file": rel_path.as_posix(),
                "text_file": rel_text_path.as_posix(),
                "atom_ids": [
                    str(atom["atom_id"])
                    for atom in atoms_chunk
                    if isinstance(atom.get("atom_id"), str)
                ],
                "assigned_atom_ids": [
                    str(atom["atom_id"])
                    for atom in atoms_chunk
                    if isinstance(atom.get("atom_id"), str) and str(atom["atom_id"]) in assigned_set
                ],
                "atom_count": len(atoms_chunk),
                "bytes": chunk_bytes,
                "text_bytes": text_bytes,
                "sha256": sha256(chunk_path.read_bytes()).hexdigest(),
                "text_sha256": sha256(text_path.read_bytes()).hexdigest(),
            }
        )

    index_content = "\n".join(index_lines).rstrip() + "\n"
    index_path = workspace_dir / "atoms_index.md"
    index_path.write_text(index_content, encoding="utf-8")

    manifest = {
        "schema_version": 3,
        "format": "chunked_problem_mining_atoms_v3",
        # Numeric record caps caused distinct observed problems to disappear when a
        # bounded job happened to contain more issues than the old default. The atom/job
        # limits bound cost; problem count is determined only by the evidence.
        "problem_record_limit": None,
        "legacy_max_records_hint_ignored": int(max_records_per_miner),
        "total_atom_count": len(prompt_atoms),
        "assigned_atom_count": len(assigned_ids),
        "assigned_atom_ids": assigned_ids,
        "chunk_count": len(chunk_entries),
        "chunk_max_bytes": int(chunk_max_bytes),
        "total_chunk_bytes": int(total_chunk_bytes),
        "total_text_chunk_bytes": int(total_text_chunk_bytes),
        "atom_file_count": int(atom_file_count),
        "total_atom_file_bytes": int(total_atom_file_bytes),
        "index_file": "atoms_index.md",
        "index_bytes": index_path.stat().st_size,
        "index_preview_chars": 500,
        "atom_file_view": "atoms_by_id/atom_####.md",
        "atom_files": atom_file_entries,
        "text_view": "atoms_text/atoms_###.md",
        "chunks": chunk_entries,
        "origin_attachment_evidence": attachment_evidence,
    }

    manifest_path = workspace_dir / "atoms.json"
    attachment_manifest_for_agent = {
        "schema_version": attachment_evidence.get("schema_version"),
        "format": attachment_evidence.get("format"),
        "manifest_file": attachment_evidence.get("manifest_file"),
        "manifest_file_sha256": attachment_evidence.get("manifest_file_sha256"),
        "materialization_sha256": attachment_evidence.get("materialization_sha256"),
        "artifact_count": len(attachment_evidence.get("artifacts", [])),
        "atom_refs": attachment_evidence.get("atom_refs", []),
        "errors": attachment_evidence.get("errors", []),
    }
    agent_manifest = dict(manifest)
    agent_manifest["origin_attachment_evidence"] = attachment_manifest_for_agent
    manifest_path.write_text(
        _json.dumps(agent_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        "[stage1] wrote chunked atoms workspace "
        f"(manifest={manifest_path} chunks={len(chunk_entries)} "
        f"atoms={len(prompt_atoms)} bytes~{total_chunk_bytes})",
        file=sys.stderr,
    )

    return manifest


def _problem_mining_attempt_manifest_sha256(manifest: dict[str, Any]) -> str:
    import json as _json

    encoded = _json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _problem_mining_attempt_file_ref(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    content = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "sha256": sha256(content).hexdigest(),
        "bytes": len(content),
    }


def _problem_mining_attempt_artifacts(
    *,
    out_dir: Path,
    attempt_tag: str,
    workspace_dir: Path,
) -> dict[str, dict[str, Any]]:
    candidates = {
        "prompt": out_dir / f"{attempt_tag}.prompt.txt",
        "response": out_dir / f"{attempt_tag}.response.txt",
        "raw_events": out_dir / f"{attempt_tag}.raw_events.jsonl",
        "normalized_events": out_dir / f"{attempt_tag}.normalized_events.jsonl",
        "cumulative_normalized_events": (
            out_dir / f"{attempt_tag}.cumulative_normalized_events.jsonl"
        ),
        "model_invocation": out_dir / f"{attempt_tag}.model_invocation.json",
        "workspace_manifest": workspace_dir / "atoms.json",
    }
    return {
        name: ref
        for name, path in candidates.items()
        if (ref := _problem_mining_attempt_file_ref(path)) is not None
    }


def _problem_mining_correction_prompt(
    *,
    original_prompt: str,
    response: str,
    validation_errors: list[str],
    valid_item_keys: list[str],
    progress_feedback: dict[str, Any] | None = None,
) -> str:
    error_text = "\n".join(f"- {error}" for error in validation_errors)
    valid_text = "\n".join(f"- {key}" for key in valid_item_keys) or "- none verified yet"
    original_prompt_sha256 = sha256(original_prompt.encode("utf-8")).hexdigest()
    prior_response_sha256 = sha256(response.encode("utf-8")).hexdigest()
    progress_text = (
        json.dumps(progress_feedback, ensure_ascii=False, indent=2, sort_keys=True)
        if isinstance(progress_feedback, dict)
        else "null"
    )
    return (
        "SAME-AUTHOR RESPONSE CORRECTION\n\n"
        "Continue this exact mining session in the same evidence workspace. Do not restart the "
        "assignment and do not discard correct findings merely because another field failed. "
        "Your prior full-file reads remain content-bound to this session. Use the existing "
        "read-only tools only if a validator error shows that another evidence read is genuinely "
        "needed. Revise your immediately prior complete response and return the complete corrected "
        "response, not a patch. Unknown validator errors "
        "are still correction feedback.\n\n"
        f"Original assignment prompt SHA-256: {original_prompt_sha256}\n"
        f"Immediately prior response SHA-256: {prior_response_sha256}\n\n"
        "Deterministic validation errors:\n"
        f"{error_text}\n\n"
        "Correction progress feedback from the immediately prior turn:\n"
        f"{progress_text}\n\n"
        "Currently valid keyed items (preserve unless a correlated correction requires change):\n"
        f"{valid_text}\n\n"
        "Return exactly one JSON object with only `problem_records` and `atom_decisions`. "
        "Encode every literal Windows path backslash inside a JSON string as `\\\\`."
    )


def _problem_mining_valid_item_keys(
    response: str,
    *,
    assigned_atom_ids: list[str],
) -> list[str]:
    """Return independently parse-valid stable keys without treating them as a full envelope."""

    import json as _json

    try:
        raw = _json.loads(response)
    except (TypeError, _json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    assigned = set(assigned_atom_ids)
    keys: list[str] = []
    records_raw = raw.get("problem_records")
    for record in records_raw if isinstance(records_raw, list) else []:
        if not isinstance(record, dict):
            continue
        parsed, warnings = parse_problem_record_list(
            _json.dumps([record], ensure_ascii=False)
        )
        problem_id = _coerce_string(record.get("problem_id"))
        evidence_ids = {
            value
            for value in (
                record.get("evidence_atom_ids")
                if isinstance(record.get("evidence_atom_ids"), list)
                else []
            )
            if isinstance(value, str) and value.strip()
        }
        if parsed and not warnings and problem_id is not None and evidence_ids and evidence_ids <= assigned:
            keys.append("problem_record:" + problem_id)
    expected_decision_fields = {
        "atom_id",
        "disposition",
        "problem_ids",
        "rationale",
        "revisit_when",
    }
    decisions_raw = raw.get("atom_decisions")
    for decision in decisions_raw if isinstance(decisions_raw, list) else []:
        if not isinstance(decision, dict) or set(decision) != expected_decision_fields:
            continue
        atom_id = _coerce_string(decision.get("atom_id"))
        rationale = _coerce_string(decision.get("rationale"))
        problem_ids = decision.get("problem_ids")
        if (
            atom_id in assigned
            and rationale is not None
            and isinstance(decision.get("disposition"), str)
            and isinstance(problem_ids, list)
            and all(isinstance(value, str) and value.strip() for value in problem_ids)
        ):
            keys.append("atom_decision:" + str(atom_id))
    return sorted(set(keys))


_PROBLEM_MINING_RESPONSE_FAILURE_PREFIXES = (
    "problem_mining_problem_id_invalid:",
    "problem_mining_citation_outside_eligible_corpus:",
    "problem_mining_atom_decision_invalid:",
    "problem_mining_atom_decision_fields_invalid:",
    "problem_mining_atom_decision_outside_assignment:",
    "problem_mining_support_problem_invalid:",
    "problem_mining_support_citation_missing:",
    "problem_mining_non_support_has_problem_ids:",
    "problem_mining_deferred_revisit_missing:",
    "problem_mining_revisit_on_non_deferred:",
    "problem_mining_assignment_decision_partition_mismatch:",
    "problem_mining_unavailable_attachment_must_remain_unresolved:",
    "problem_mining_citation_without_support_decision:",
)


def _retryable_problem_mining_response_failure(exc: Exception) -> bool:
    if isinstance(exc, ProblemMiningResponseContractError):
        return True
    if isinstance(exc, ValueError) and str(exc).startswith(
        _PROBLEM_MINING_RESPONSE_FAILURE_PREFIXES
    ):
        return True
    return isinstance(exc, RuntimeError) and str(exc).startswith(
        "run_stage_prompt_json: empty response"
    )


def _retain_explicit_empty_problem_mining_response(
    *,
    out_dir: Path,
    attempt_tag: str,
    failure: Exception,
) -> None:
    """Retain an auditable response artifact when the backend returned no text.

    ``run_stage_prompt_json`` rejects an empty response before it writes the normal
    ``*.response.txt`` artifact.  A response-contract retry still has to retain the
    rejected attempt, and receipt revalidation deliberately treats a missing response
    as evidence loss.  Materialize the exact empty payload as a zero-byte file; never
    replace an artifact that the backend did manage to retain.
    """

    if not (
        isinstance(failure, RuntimeError)
        and str(failure).startswith("run_stage_prompt_json: empty response")
    ):
        return
    response_path = out_dir / f"{attempt_tag}.response.txt"
    if not response_path.exists():
        response_path.write_bytes(b"")


def _run_problem_mining_attempt(
    *,
    repo_root: Path,
    stage_artifacts_dir: Path,
    base_tag: str,
    attempt_tag: str,
    attempt_number: int,
    prompt: str,
    prompt_atoms: list[dict[str, Any]],
    assigned_atom_ids: list[str],
    max_records_per_miner: int,
    eligible_atom_ids: list[str],
    template_name: str,
    record_contract_error_prefix: str,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    initial_workspace_dir: Path | None = None,
    initial_manifest: dict[str, Any] | None = None,
    expected_manifest_sha256: str | None = None,
    resume_session_id: str | None = None,
    prior_normalized_events_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    # Attempt tags are stable logical identities, but a forced regeneration may reuse
    # the same artifacts root.  Put every execution in a fresh content-addressed
    # directory so an empty/failed backend response cannot inherit a stale prompt,
    # response, or event stream from an earlier cycle.
    out_dir = stage_artifacts_dir / attempt_tag / f"attempt_{attempt_number:02d}_{uuid4().hex}"
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = initial_workspace_dir or (out_dir / f"workspace_{uuid4().hex}")
    workspace_dir.mkdir(parents=True, exist_ok=True)
    manifest = initial_manifest or _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace_dir,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=max_records_per_miner,
        assigned_atom_ids=assigned_atom_ids,
        source_root=repo_root,
    )
    manifest_sha256 = _problem_mining_attempt_manifest_sha256(manifest)
    attempt_record: dict[str, Any] = {
        "schema_version": 2,
        "attempt_number": attempt_number,
        "attempt_tag": attempt_tag,
        "status": "started",
        "workspace_dir": str(workspace_dir.resolve()),
        "workspace_manifest_sha256": manifest_sha256,
        "agent_session_id": None,
        "resumed_from_session_id": resume_session_id,
        "attempt_elapsed_seconds": 0.0,
        "valid_item_keys": [],
        "artifacts": {},
    }
    if expected_manifest_sha256 is not None and manifest_sha256 != expected_manifest_sha256:
        failure = ValueError(
            "problem_mining_retry_evidence_manifest_changed:"
            f"{base_tag}:{expected_manifest_sha256}:{manifest_sha256}"
        )
        attempt_record["status"] = "failed_evidence_changed"
        attempt_record["error"] = f"{type(failure).__name__}: {failure}"
        attempt_record["artifacts"] = _problem_mining_attempt_artifacts(
            out_dir=out_dir,
            attempt_tag=attempt_tag,
            workspace_dir=workspace_dir,
        )
        return {
            "failure": failure,
            "validation_errors": [f"{type(failure).__name__}: {failure}"],
            "attempt_record": attempt_record,
            "workspace_dir": workspace_dir,
            "manifest": manifest,
        }

    normalized_events_path = out_dir / f"{attempt_tag}.normalized_events.jsonl"
    cumulative_normalized_events_path = (
        out_dir / f"{attempt_tag}.cumulative_normalized_events.jsonl"
    )
    response = ""
    agent_session_id: str | None = None
    attempt_elapsed_seconds = 0.0
    validation_errors: list[str] = []
    attempt_started = _time.monotonic()
    try:
        prompt_run = run_stage_prompt_json(
            stage="problem_mining",
            prompt=prompt,
            out_dir=out_dir,
            tag=attempt_tag,
            agent=agent,
            model=model,
            cfg=cfg,
            workspace_dir=workspace_dir,
            allowed_tools=(
                ["Read"] if agent == "claude" else ["read_file"] if agent == "gemini" else []
            ),
            include_directories=([str(workspace_dir)] if agent == "gemini" else []),
            resume_session_id=resume_session_id,
            allow_empty=True,
            structured=True,
        )
        if isinstance(prompt_run, str):
            # Test/backward-compatible prompt shims may still return raw text.
            response = prompt_run
        else:
            response = str(prompt_run.response)
            agent_session_id = _coerce_string(prompt_run.agent_session_id)
            attempt_elapsed_seconds = max(0.0, float(prompt_run.elapsed_seconds))
        attempt_record["agent_session_id"] = agent_session_id
        attempt_record["attempt_elapsed_seconds"] = attempt_elapsed_seconds
        attempt_record["valid_item_keys"] = _problem_mining_valid_item_keys(
            response,
            assigned_atom_ids=assigned_atom_ids,
        )
        normalize_problem_mining_events(
            agent=agent,
            raw_events_path=out_dir / f"{attempt_tag}.raw_events.jsonl",
            normalized_events_path=normalized_events_path,
            workspace_dir=workspace_dir,
        )
        envelope = parse_problem_mining_response_envelope(response)
        import json as _json

        records, warnings = parse_problem_record_list(
            _json.dumps(envelope["problem_records"], ensure_ascii=False)
        )
        if warnings:
            validation_errors = [
                record_contract_error_prefix + ":" + str(warning) for warning in warnings
            ]
            raise ProblemMiningResponseContractError(
                ";".join(validation_errors)
            )
        with cumulative_normalized_events_path.open("wb") as cumulative_stream:
            for event_path in (*prior_normalized_events_paths, normalized_events_path):
                if event_path.is_file():
                    payload = event_path.read_bytes()
                    cumulative_stream.write(payload)
                    if payload and not payload.endswith(b"\n"):
                        cumulative_stream.write(b"\n")
        receipt = build_live_miner_receipt(
            tag=base_tag,
            template_name=template_name,
            assigned_atom_ids=assigned_atom_ids,
            eligible_atom_ids=eligible_atom_ids,
            records=records,
            decisions=envelope["atom_decisions"],
            response_text=response,
            normalized_events_path=cumulative_normalized_events_path,
            workspace_dir=workspace_dir,
            workspace_manifest=manifest,
        )
    except Exception as exc:  # noqa: BLE001
        if attempt_elapsed_seconds <= 0.0:
            attempt_elapsed_seconds = max(0.0, _time.monotonic() - attempt_started)
            attempt_record["attempt_elapsed_seconds"] = attempt_elapsed_seconds
        if not validation_errors:
            validation_errors = [f"{type(exc).__name__}: {exc}"]
        _retain_explicit_empty_problem_mining_response(
            out_dir=out_dir,
            attempt_tag=attempt_tag,
            failure=exc,
        )
        attempt_record["status"] = (
            "response_contract_failed"
            if (_retryable_problem_mining_response_failure(exc))
            else "failed"
        )
        attempt_record["error"] = f"{type(exc).__name__}: {exc}"
        attempt_record["artifacts"] = _problem_mining_attempt_artifacts(
            out_dir=out_dir,
            attempt_tag=attempt_tag,
            workspace_dir=workspace_dir,
        )
        return {
            "failure": exc,
            "attempt_record": attempt_record,
            "workspace_dir": workspace_dir,
            "manifest": manifest,
            "response": response,
            "agent_session_id": agent_session_id,
            "attempt_elapsed_seconds": attempt_elapsed_seconds,
            "valid_item_keys": list(attempt_record["valid_item_keys"]),
            "validation_errors": list(validation_errors),
            "normalized_events_path": (
                normalized_events_path if normalized_events_path.is_file() else None
            ),
        }

    attempt_record["status"] = "verified"
    attempt_record["error"] = None
    attempt_record["read_attestation_count"] = len(receipt["read_attestations"])
    attempt_record["artifacts"] = _problem_mining_attempt_artifacts(
        out_dir=out_dir,
        attempt_tag=attempt_tag,
        workspace_dir=workspace_dir,
    )
    return {
        "failure": None,
        "attempt_record": attempt_record,
        "workspace_dir": workspace_dir,
        "manifest": manifest,
        "response": response,
        "envelope": envelope,
        "records": records,
        "warnings": warnings,
        "normalized_events_path": normalized_events_path,
        "cumulative_normalized_events_path": cumulative_normalized_events_path,
        "agent_session_id": agent_session_id,
        "attempt_elapsed_seconds": attempt_elapsed_seconds,
        "valid_item_keys": list(attempt_record["valid_item_keys"]),
        "validation_errors": [],
        "receipt": receipt,
    }


def _run_problem_mining_job_with_response_retry(
    *,
    repo_root: Path,
    stage_artifacts_dir: Path,
    base_tag: str,
    prompt: str,
    prompt_atoms: list[dict[str, Any]],
    assigned_atom_ids: list[str],
    max_records_per_miner: int,
    eligible_atom_ids: list[str],
    template_name: str,
    record_contract_error_prefix: str,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    initial_workspace_dir: Path,
    initial_manifest: dict[str, Any],
    resume_session_id: str | None = None,
    attempt_number_base: int = 0,
    prior_normalized_events_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    expected_manifest_sha256 = _problem_mining_attempt_manifest_sha256(initial_manifest)
    def observation(result: dict[str, Any]) -> CorrectionObservation[dict[str, Any]]:
        errors = tuple(
            str(value)
            for value in result.get("validation_errors", [])
            if isinstance(value, str) and value.strip()
        )
        response = str(result.get("response") or "")
        valid_item_keys = tuple(
            sorted(
                set(
                    str(value)
                    for value in result.get("valid_item_keys", [])
                    if isinstance(value, str) and value.strip()
                )
            )
        )
        manifest = result.get("manifest")
        workspace = result.get("workspace_dir")
        observed_continuity = sha256(
            json.dumps(
                {
                    "workspace_dir": str(Path(workspace).resolve()) if workspace is not None else None,
                    "workspace_manifest_sha256": (
                        _problem_mining_attempt_manifest_sha256(manifest)
                        if isinstance(manifest, dict)
                        else None
                    ),
                    "assigned_atom_ids": sorted(assigned_atom_ids),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return CorrectionObservation(
            payload=result,
            validation_errors=errors,
            state_sha256=correction_state_sha256(
                candidate=response,
                validation_errors=list(errors),
                valid_item_keys=list(valid_item_keys),
            ),
            valid_item_keys=valid_item_keys,
            agent_session_id=_coerce_string(result.get("agent_session_id")),
            continuity_key=observed_continuity,
            cost_seconds=max(0.0, float(result.get("attempt_elapsed_seconds") or 0.0)),
        )

    initial_result = _run_problem_mining_attempt(
        repo_root=repo_root,
        stage_artifacts_dir=stage_artifacts_dir,
        base_tag=base_tag,
        attempt_tag=base_tag,
        attempt_number=attempt_number_base + 1,
        prompt=prompt,
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=assigned_atom_ids,
        max_records_per_miner=max_records_per_miner,
        eligible_atom_ids=eligible_atom_ids,
        template_name=template_name,
        record_contract_error_prefix=record_contract_error_prefix,
        agent=agent,
        model=model,
        cfg=cfg,
        initial_workspace_dir=initial_workspace_dir,
        initial_manifest=initial_manifest,
        expected_manifest_sha256=expected_manifest_sha256,
        resume_session_id=resume_session_id,
        prior_normalized_events_paths=prior_normalized_events_paths,
    )
    initial = observation(initial_result)
    acquisition_attempts: tuple[CorrectionObservation[dict[str, Any]], ...] = ()
    acquisition_status = "not_required"
    if (
        resume_session_id is None
        and agent.strip().lower() == "codex"
        and initial.agent_session_id is None
    ):
        acquisition = acquire_author_session(
            initial=initial,
            invoke_fresh=lambda attempt_number: observation(
                _run_problem_mining_attempt(
                    repo_root=repo_root,
                    stage_artifacts_dir=stage_artifacts_dir,
                    base_tag=base_tag,
                    attempt_tag=(
                        f"{base_tag}_session_acquisition_{attempt_number - 1:03d}"
                    ),
                    attempt_number=attempt_number_base + attempt_number,
                    prompt=prompt,
                    prompt_atoms=prompt_atoms,
                    assigned_atom_ids=assigned_atom_ids,
                    max_records_per_miner=max_records_per_miner,
                    eligible_atom_ids=eligible_atom_ids,
                    template_name=template_name,
                    record_contract_error_prefix=record_contract_error_prefix,
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    initial_workspace_dir=initial_workspace_dir,
                    initial_manifest=initial_manifest,
                    expected_manifest_sha256=expected_manifest_sha256,
                )
            ),
        )
        acquisition_status = acquisition.status
        acquisition_attempts = acquisition.attempts
        initial = acquisition.current
        initial_result = initial.payload
    normalized_event_paths: list[Path] = []
    normalized_event_paths.extend(
        path for path in prior_normalized_events_paths if path.is_file()
    )
    initial_events = initial_result.get("normalized_events_path")
    if isinstance(initial_events, Path) and initial_events.is_file():
        normalized_event_paths.append(initial_events)

    def invoke_correction(
        current: CorrectionObservation[dict[str, Any]],
        attempt_number: int,
        prior_assessment: Any,
    ) -> CorrectionObservation[dict[str, Any]]:
        current_result = current.payload
        correction_prompt = _problem_mining_correction_prompt(
            original_prompt=prompt,
            response=str(current_result.get("response") or ""),
            validation_errors=list(current.validation_errors),
            valid_item_keys=list(current.valid_item_keys),
            progress_feedback=(
                {
                    "decision": prior_assessment.decision,
                    "reason": prior_assessment.reason,
                    "resolved_error_identities": list(
                        prior_assessment.resolved_error_identities
                    ),
                    "introduced_error_identities": list(
                        prior_assessment.introduced_error_identities
                    ),
                    "repeated_state": prior_assessment.repeated_state,
                    "safe_frontier_updated": prior_assessment.safe_frontier_updated,
                    "global_best_updated": prior_assessment.global_best_updated,
                }
                if prior_assessment is not None
                else None
            ),
        )
        corrected = _run_problem_mining_attempt(
            repo_root=repo_root,
            stage_artifacts_dir=stage_artifacts_dir,
            base_tag=base_tag,
            attempt_tag=f"{base_tag}_correction_{attempt_number - 1:03d}",
            attempt_number=(
                attempt_number_base
                + attempt_number
                + max(0, len(acquisition_attempts) - 1)
            ),
            prompt=correction_prompt,
            prompt_atoms=prompt_atoms,
            assigned_atom_ids=assigned_atom_ids,
            max_records_per_miner=max_records_per_miner,
            eligible_atom_ids=eligible_atom_ids,
            template_name=template_name,
            record_contract_error_prefix=record_contract_error_prefix,
            agent=agent,
            model=model,
            cfg=cfg,
            initial_workspace_dir=initial_workspace_dir,
            initial_manifest=initial_manifest,
            expected_manifest_sha256=expected_manifest_sha256,
            resume_session_id=resume_session_id or initial.agent_session_id,
            prior_normalized_events_paths=tuple(normalized_event_paths),
        )
        correction_failure = corrected.get("failure")
        if (
            isinstance(correction_failure, Exception)
            and _coerce_string(corrected.get("agent_session_id")) is None
        ):
            # The exact-session invocation failed before returning author state. The
            # shared frontier retains the failure and retries this same UUID.
            raise correction_failure
        corrected_events = corrected.get("normalized_events_path")
        if isinstance(corrected_events, Path) and corrected_events.is_file():
            normalized_event_paths.append(corrected_events)
        return observation(corrected)

    original_author_cost = initial.cost_seconds

    def pause_policy(
        _current: CorrectionObservation[dict[str, Any]],
        _assessment: Any,
        correction_cost_since_progress: float,
        _total_correction_cost: float,
    ) -> str | None:
        if (
            _assessment is not None
            and _assessment.reason == "first_noop_receives_feedback"
        ):
            # One exact no-op is not clear nonprogress; the author has now received
            # validator feedback and must get the distinguishing follow-up turn.
            return None
        if (
            original_author_cost > 0.0
            and correction_cost_since_progress >= original_author_cost
        ):
            return "correction_cost_reached_original"
        return None

    if acquisition_status.startswith("repairable_paused:"):
        correction = CorrectionRunResult(
            status=acquisition_status,
            current=initial,
            best=initial,
            attempts=acquisition_attempts,
            assessments=(),
            correction_cost_since_progress=acquisition.cost_since_progress,
            total_correction_cost=max(
                0.0,
                acquisition.total_cost
                - max(0.0, float(acquisition_attempts[0].cost_seconds)),
            ),
        )
    else:
        correction = run_progressive_correction(
            initial=initial,
            invoke_correction=invoke_correction,
            pause_policy=pause_policy,
        )
    correction_metrics = correction_run_metrics(correction, expected_quality=None)
    if acquisition_attempts:
        correction_metrics = correction_metrics_with_session_acquisition(
            correction_metrics,
            acquisition,
        )
    retained = correction.current if correction.status in {"accepted", "corrected"} else correction.best
    result = retained.payload
    attempt_history = [dict(item.payload["attempt_record"]) for item in correction.attempts]
    for index, assessment in enumerate(correction.assessments, start=1):
        attempt_history[index]["correction_progress"] = {
            "decision": assessment.decision,
            "reason": assessment.reason,
            "before_error_count": assessment.before_error_count,
            "after_error_count": assessment.after_error_count,
            "resolved_error_identities": list(assessment.resolved_error_identities),
            "introduced_error_identities": list(assessment.introduced_error_identities),
            "repeated_state": assessment.repeated_state,
            "safe_frontier_updated": assessment.safe_frontier_updated,
            "global_best_updated": assessment.global_best_updated,
            "reset_progress_clock": assessment.reset_progress_clock,
        }
    attempt_history.extend(
        {
            "schema_version": 2,
            "attempt_number": (
                failure.attempt_number + max(0, len(acquisition_attempts) - 1)
            ),
            "attempt_tag": (
                f"{base_tag}_correction_{failure.attempt_number - 1:03d}"
            ),
            "status": "invocation_failed",
            "agent_session_id": failure.agent_session_id,
            "resumed_from_session_id": failure.agent_session_id,
            "attempt_elapsed_seconds": failure.cost_seconds,
            "validation_errors": [
                f"{failure.error_type}: {failure.error_message}"
            ],
            "failure_identity": failure.failure_identity,
            "artifacts": {},
        }
        for failure in correction.invocation_failures
    )
    attempt_history.sort(key=lambda record: int(record["attempt_number"]))
    if acquisition_status == "acquired" and len(acquisition_attempts) > 1:
        pre_author_attempts = acquisition_attempts[:-1]
        attempt_history = [
            dict(attempt.payload["attempt_record"])
            for attempt in pre_author_attempts
        ] + attempt_history
    exact_resume_preserved = bool(
        resume_session_id is None
        or _coerce_string(result.get("agent_session_id")) == resume_session_id
    )
    successful = (
        correction.status in {"accepted", "corrected"}
        and not isinstance(result.get("failure"), Exception)
        and exact_resume_preserved
    )
    if successful:
        receipt = dict(result["receipt"])
        receipt["attempt_history"] = attempt_history
        receipt["successful_attempt_tag"] = result["attempt_record"]["attempt_tag"]
        receipt["correction_status"] = correction.status
        receipt["correction_cost_since_progress"] = correction.correction_cost_since_progress
        receipt["total_correction_cost"] = correction.total_correction_cost
        receipt["correction_metrics"] = correction_metrics
        result["receipt"] = receipt
        result["successful_attempt_tag"] = result["attempt_record"]["attempt_tag"]
    else:
        result["successful_attempt_tag"] = None
    result["attempt_history"] = attempt_history
    result["correction_status"] = correction.status
    result["correction_cost_since_progress"] = correction.correction_cost_since_progress
    result["total_correction_cost"] = correction.total_correction_cost
    result["correction_metrics"] = correction_metrics
    if not exact_resume_preserved:
        result["failure"] = ValueError(
            "problem_mining_exact_author_session_changed:"
            f"expected={resume_session_id}:observed={result.get('agent_session_id')}"
        )
        result["validation_errors"] = [
            "problem_mining_exact_author_session_changed"
        ]
        result["correction_status"] = "repairable_paused:exact_author_session_changed"
    if correction.operational_error is not None:
        result["correction_operational_error"] = correction.operational_error
    return result


_CROSS_JOB_ROUTING_MAX_BYTES = _PROBLEM_MINING_JOB_MAX_BYTES
_CROSS_JOB_ROUTING_MAX_ITEMS = _PROBLEM_MINING_JOB_MAX_ATOMS
_CROSS_JOB_THEME_TEXT_CHARS = 900
_CROSS_JOB_ROUTING_KEY_MIN = 2
_CROSS_JOB_ROUTING_KEY_MAX = 5
_CROSS_JOB_ROUTING_KEY_CHARS = 80


def _bounded_routing_text(value: Any, *, max_chars: int) -> str:
    text = _coerce_string(value) or ""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return (
        text[:head].rstrip()
        + "\n...[routing summary truncated; exact evidence retained]...\n"
        + text[-tail:].lstrip()
    )


def _routing_membership_sha256(
    member_atom_ids: list[str],
    evidence_sha256_by_atom: dict[str, str],
) -> str:
    import json as _json

    payload = [
        {"atom_id": atom_id, "evidence_sha256": evidence_sha256_by_atom[atom_id]}
        for atom_id in sorted(member_atom_ids)
    ]
    return sha256(
        _json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _cross_job_leaf_routing_nodes(
    *,
    prompt_atoms: list[dict[str, Any]],
    miner_receipts: list[dict[str, Any]],
    eligible_evidence_sha256_by_atom: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Represent every verified leaf decision without sampling or a count cap.

    Supported leaves are comparison anchors, not override candidates.  Retaining them
    lets an unresolved observation in another fixed partition meet an already-mined
    claim; otherwise deterministic partitioning can repeat the same asymmetric miss
    forever.
    """

    import json as _json

    atoms_by_id = {
        atom_id: atom
        for atom in prompt_atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        if atom_id is not None
    }
    has_non_support = any(
        isinstance(decision, dict) and decision.get("disposition") != "supports_case"
        for miner in miner_receipts
        if miner.get("status") == "verified"
        for decision in miner.get("atom_decisions", [])
    )
    if not has_non_support:
        # Supported records already meet one another in canonical relation review.
        # Anchors are needed only when another partition still has evidence to recover.
        return []
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for miner in miner_receipts:
        if miner.get("status") != "verified":
            continue
        source_job_tag = _coerce_string(miner.get("tag"))
        if source_job_tag is None:
            continue
        for decision in miner.get("atom_decisions", []):
            if not isinstance(decision, dict):
                continue
            atom_id = _coerce_string(decision.get("atom_id"))
            atom = atoms_by_id.get(atom_id or "")
            if atom_id is None or atom is None or atom_id in seen:
                continue
            seen.add(atom_id)
            evidence_sha256 = (eligible_evidence_sha256_by_atom or {}).get(atom_id) or sha256(
                _json.dumps(
                    atom,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            observed = _bounded_routing_text(
                atom.get("text"),
                max_chars=_CROSS_JOB_THEME_TEXT_CHARS,
            )
            rationale = _bounded_routing_text(
                decision.get("rationale"),
                max_chars=_CROSS_JOB_THEME_TEXT_CHARS,
            )
            problem_ids = sorted(_coerce_string_list(decision.get("problem_ids")))
            claim_projection = {
                "atom_id": atom_id,
                "disposition": decision.get("disposition"),
                "problem_ids": problem_ids,
                "rationale": decision.get("rationale"),
            }
            claim_sha256 = sha256(
                _json.dumps(
                    claim_projection,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            summary = (
                f"Observed evidence: {observed}\n"
                f"Independent leaf disposition: {decision.get('disposition')}. "
                f"Existing leaf problem IDs: {', '.join(problem_ids) or 'none'}. "
                f"Claim hash: {claim_sha256}. Rationale: {rationale}"
            )
            route_seed = f"{atom_id}\0{evidence_sha256}\0{source_job_tag}\0{claim_sha256}"
            route_id = "routing:" + sha256(route_seed.encode("utf-8")).hexdigest()[:24]
            nodes.append(
                {
                    "route_id": route_id,
                    "level": 0,
                    "summary": summary,
                    "member_atom_ids": [atom_id],
                    "evidence_sha256_by_atom": {atom_id: evidence_sha256},
                    "source_job_tags": [source_job_tag],
                    "original_disposition_by_atom": {
                        atom_id: str(decision.get("disposition") or "unresolved")
                    },
                    "original_problem_ids_by_atom": {atom_id: problem_ids},
                    "leaf_claim_sha256_by_atom": {atom_id: claim_sha256},
                    "membership_sha256": _routing_membership_sha256(
                        [atom_id], {atom_id: evidence_sha256}
                    ),
                }
            )
    return sorted(nodes, key=lambda node: str(node["route_id"]))


def _routing_node_prompt_atom(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "atom_id": node["route_id"],
        "run_rel": f"cross_job_routing/level_{node['level']}",
        "source": "cross_job_unresolved_theme",
        "severity_hint": "medium",
        "evidence_role": "observation",
        "text": node["summary"],
        "routing_level": node["level"],
        "routing_member_count": len(node["member_atom_ids"]),
        "routing_membership_sha256": node["membership_sha256"],
        "routing_source_job_tags": node["source_job_tags"],
        "routing_key_count": len(node.get("routing_keys", [])),
        "routing_original_dispositions": node.get("original_disposition_by_atom", {}),
        "routing_leaf_claim_sha256_by_atom": node.get("leaf_claim_sha256_by_atom", {}),
        "routing_only": True,
    }


def _routing_record_keys(record: dict[str, Any]) -> list[str]:
    raw = record.get("routing_keys")
    values = raw if isinstance(raw, list) else []
    normalized = list(
        dict.fromkeys(
            "-".join(value.casefold().split())
            for value in values
            if isinstance(value, str)
            and value.strip()
            and len(value.strip()) <= _CROSS_JOB_ROUTING_KEY_CHARS
        )
    )
    if not (_CROSS_JOB_ROUTING_KEY_MIN <= len(normalized) <= _CROSS_JOB_ROUTING_KEY_MAX):
        problem_id = _coerce_string(record.get("problem_id")) or "(missing)"
        raise ValueError(f"cross_job_routing_keys_invalid:{problem_id}")
    return normalized


def _combine_routing_nodes(
    nodes: list[dict[str, Any]],
    *,
    level: int,
    routing_keys_by_child: dict[str, list[str]],
) -> dict[str, Any]:
    """Carry every child through bounded, model-assigned semantic route keys."""

    member_atom_ids = sorted(
        {atom_id for node in nodes for atom_id in _coerce_string_list(node.get("member_atom_ids"))}
    )
    hashes = {
        atom_id: str(node_hashes[atom_id])
        for node in nodes
        for node_hashes in [
            node.get("evidence_sha256_by_atom")
            if isinstance(node.get("evidence_sha256_by_atom"), dict)
            else {}
        ]
        for atom_id in node_hashes
    }
    source_job_tags = sorted(
        {tag for node in nodes for tag in _coerce_string_list(node.get("source_job_tags"))}
    )
    original_disposition_by_atom = {
        atom_id: str(value)
        for node in nodes
        for mapping in [
            node.get("original_disposition_by_atom")
            if isinstance(node.get("original_disposition_by_atom"), dict)
            else {}
        ]
        for atom_id, value in mapping.items()
    }
    original_problem_ids_by_atom = {
        atom_id: _coerce_string_list(value)
        for node in nodes
        for mapping in [
            node.get("original_problem_ids_by_atom")
            if isinstance(node.get("original_problem_ids_by_atom"), dict)
            else {}
        ]
        for atom_id, value in mapping.items()
    }
    leaf_claim_sha256_by_atom = {
        atom_id: str(value)
        for node in nodes
        for mapping in [
            node.get("leaf_claim_sha256_by_atom")
            if isinstance(node.get("leaf_claim_sha256_by_atom"), dict)
            else {}
        ]
        for atom_id, value in mapping.items()
    }
    child_lines = []
    all_routing_keys: set[str] = set()
    routing_key_member_atom_ids: dict[str, set[str]] = {}
    for node in nodes:
        route_id_value = str(node["route_id"])
        keys = routing_keys_by_child.get(route_id_value, [])
        if not keys:
            raise ValueError(f"cross_job_routing_keys_missing:{route_id_value}")
        all_routing_keys.update(keys)
        node_member_ids = set(_coerce_string_list(node.get("member_atom_ids")))
        prior_key_members_raw = node.get("routing_key_member_atom_ids")
        prior_key_members = prior_key_members_raw if isinstance(prior_key_members_raw, dict) else {}
        for key in keys:
            precise_members = set(_coerce_string_list(prior_key_members.get(key)))
            routing_key_member_atom_ids.setdefault(key, set()).update(
                precise_members or node_member_ids
            )
        child_lines.append(
            f"- {route_id_value} [{node['membership_sha256']}] => " + ", ".join(keys)
        )
    membership_sha256 = _routing_membership_sha256(member_atom_ids, hashes)
    route_id = (
        "routing:" + sha256((f"level={level}\0{membership_sha256}").encode()).hexdigest()[:24]
    )
    return {
        "route_id": route_id,
        "level": level,
        "summary": (
            "Carry bundle. Every child theme remains model-visible through two to five "
            "independently assigned semantic routing keys plus its exact membership hash. "
            "Compare keys across bundles; no child was sampled or dropped.\n"
            + "\n".join(child_lines)
        ),
        "member_atom_ids": member_atom_ids,
        "evidence_sha256_by_atom": hashes,
        "source_job_tags": source_job_tags,
        "membership_sha256": membership_sha256,
        "child_route_ids": [str(node["route_id"]) for node in nodes],
        "routing_keys": sorted(all_routing_keys),
        "original_disposition_by_atom": original_disposition_by_atom,
        "original_problem_ids_by_atom": original_problem_ids_by_atom,
        "leaf_claim_sha256_by_atom": leaf_claim_sha256_by_atom,
        "routing_key_member_atom_ids": {
            key: sorted(atom_ids) for key, atom_ids in sorted(routing_key_member_atom_ids.items())
        },
    }


def _render_problem_mining_contract_prompt(
    *,
    template_text: str,
    stage_guidance_text: str,
    prefix: str,
) -> str:
    import json as _json

    atoms_placeholder = _json.dumps(
        {"atoms_file": "atoms.json"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        prefix
        + "\n\n"
        + (
            template_text.replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
            .replace("{{ATOMS_JSON}}", atoms_placeholder)
            .replace("{{MAX_RECORDS_PER_MINER}}", "uncapped")
        )
    )


def _run_independently_reviewed_problem_pass(
    *,
    repo_root: Path,
    stage_artifacts_dir: Path,
    base_tag: str,
    prompt: str,
    prompt_atoms: list[dict[str, Any]],
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
) -> dict[str, Any]:
    """Run one full-read pass and a fresh independent review over the same assignment."""

    import json as _json

    assigned_atom_ids = sorted(
        atom_id
        for atom in prompt_atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        if atom_id is not None
    )
    first_workspace = stage_artifacts_dir / base_tag / f"workspace_{uuid4().hex}"
    first_manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=first_workspace,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=0,
        assigned_atom_ids=assigned_atom_ids,
        source_root=repo_root,
    )
    first = _run_problem_mining_job_with_response_retry(
        repo_root=repo_root,
        stage_artifacts_dir=stage_artifacts_dir,
        base_tag=base_tag,
        prompt=prompt,
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=assigned_atom_ids,
        max_records_per_miner=0,
        eligible_atom_ids=assigned_atom_ids,
        template_name="cross_job_synthesis",
        record_contract_error_prefix="cross_job_problem_record_contract_invalid",
        agent=agent,
        model=model,
        cfg=cfg,
        initial_workspace_dir=first_workspace,
        initial_manifest=first_manifest,
    )
    failure = first.get("failure")
    if isinstance(failure, Exception):
        raise failure
    first_records = list(first["records"])
    first_decisions = [dict(item) for item in first["envelope"]["atom_decisions"]]
    review_tag = f"{base_tag}_independent_review"
    review_workspace = stage_artifacts_dir / review_tag / f"workspace_{uuid4().hex}"
    review_manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=review_workspace,
        prompt_atoms=prompt_atoms,
        max_records_per_miner=0,
        assigned_atom_ids=assigned_atom_ids,
        source_root=repo_root,
    )
    review_prompt = (
        "INDEPENDENT CROSS-JOB REVIEW\n\n"
        "Read the complete fresh evidence workspace. Audit every proposed grouping. "
        "To confirm a claim, reproduce its complete problem record verbatim and support "
        "the same ID. Recover any missed evidence-grounded grouping. Return the same strict "
        "one-decision-per-atom contract. The earlier output is a claim, not evidence.\n\n"
        "EARLIER CLAIMS:\n"
        + _json.dumps(
            {"problem_records": first_records, "atom_decisions": first_decisions},
            ensure_ascii=False,
            indent=2,
        )
        + "\n\n"
        + prompt
    )
    review = _run_problem_mining_job_with_response_retry(
        repo_root=repo_root,
        stage_artifacts_dir=stage_artifacts_dir,
        base_tag=review_tag,
        prompt=review_prompt,
        prompt_atoms=prompt_atoms,
        assigned_atom_ids=assigned_atom_ids,
        max_records_per_miner=0,
        eligible_atom_ids=assigned_atom_ids,
        template_name="adversarial:cross_job_synthesis",
        record_contract_error_prefix="cross_job_review_record_contract_invalid",
        agent=agent,
        model=model,
        cfg=cfg,
        initial_workspace_dir=review_workspace,
        initial_manifest=review_manifest,
    )
    review_failure = review.get("failure")
    if isinstance(review_failure, Exception):
        raise review_failure
    final_records, final_decisions = _reconcile_problem_mining_reviews(
        primary_records=first_records,
        primary_decisions=first_decisions,
        review_records=list(review["records"]),
        review_decisions=[dict(item) for item in review["envelope"]["atom_decisions"]],
    )
    combined = build_live_miner_receipt(
        tag=base_tag,
        template_name="cross_job_synthesis",
        assigned_atom_ids=assigned_atom_ids,
        eligible_atom_ids=assigned_atom_ids,
        records=final_records,
        decisions=final_decisions,
        response_text=_json.dumps(
            {"first": first["response"], "review": review["response"]},
            ensure_ascii=False,
            sort_keys=True,
        ),
        normalized_events_path=Path(first["normalized_events_path"]),
        workspace_dir=Path(first["workspace_dir"]),
        workspace_manifest=dict(first["manifest"]),
    )
    combined["primary_pass"] = dict(first["receipt"])
    combined["non_support_review"] = dict(review["receipt"])
    combined["review_scope"] = "all_assigned_cross_job_themes"
    combined["attempt_history"] = list(first["receipt"].get("attempt_history", []))
    combined["successful_attempt_tag"] = first["receipt"].get("successful_attempt_tag")
    return {
        "records": final_records,
        "decisions": final_decisions,
        "receipt": combined,
    }


def _cross_job_routing_signal(
    *,
    level: int,
    routing_key: str,
    member_atom_ids: set[str],
    source_job_by_atom: dict[str, str],
    leaf_evidence_sha256_by_atom: dict[str, str],
    exact_atoms_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Classify one semantic key without allowing it to exceed an exact-pass ceiling.

    A routing key is only a recall signal.  A generic key can legitimately span the
    whole corpus, so treating its oversized exact candidate as a stage failure makes
    the safety ceiling a denial-of-service switch.  Retain the complete, hash-bound
    membership either way, but only promote a signal that fits one measured model job.
    """

    import json as _json

    atom_ids = sorted(member_atom_ids)
    atoms_by_source_job: dict[str, list[str]] = {}
    for atom_id in atom_ids:
        atoms_by_source_job.setdefault(source_job_by_atom[atom_id], []).append(atom_id)
    balanced_atom_ids: list[str] = []
    while any(atoms_by_source_job.values()):
        for source_job in sorted(atoms_by_source_job):
            if atoms_by_source_job[source_job]:
                balanced_atom_ids.append(atoms_by_source_job[source_job].pop(0))
    exact_atoms = [exact_atoms_by_id[atom_id] for atom_id in balanced_atom_ids]
    measured_batches = _problem_mining_job_batches(
        exact_atoms,
        max_atoms=_PROBLEM_MINING_JOB_MAX_ATOMS,
        max_bytes=_PROBLEM_MINING_JOB_MAX_BYTES,
    )
    measured_batch_ids = [
        sorted(
            atom_id
            for atom in batch
            for atom_id in [_coerce_string(atom.get("atom_id"))]
            if atom_id is not None
        )
        for batch in measured_batches
    ]
    refinement_groups = [
        batch
        for batch in measured_batch_ids
        if len(batch) >= 2 and len({source_job_by_atom[atom_id] for atom_id in batch}) >= 2
    ]
    evidence_hashes = {atom_id: leaf_evidence_sha256_by_atom[atom_id] for atom_id in atom_ids}
    disposition = (
        "candidate"
        if len(measured_batches) == 1
        else "partitioned_candidate"
        if refinement_groups
        else "nondiscriminative"
    )
    reason = {
        "candidate": "within_exact_model_job_ceiling",
        "partitioned_candidate": "routing_key_partitioned_into_bounded_cross_job_groups",
        "nondiscriminative": "routing_key_has_no_bounded_cross_job_group",
    }[disposition]
    payload = {
        "level": level,
        "routing_key": routing_key,
        "member_atom_ids": atom_ids,
        "membership_sha256": _routing_membership_sha256(atom_ids, evidence_hashes),
        "source_job_tags": sorted({source_job_by_atom[atom_id] for atom_id in atom_ids}),
        "measured_exact_batches": measured_batch_ids,
        "refinement_groups": refinement_groups,
        "measured_exact_job_count": len(measured_batches),
        "max_atoms": _PROBLEM_MINING_JOB_MAX_ATOMS,
        "max_bytes": _PROBLEM_MINING_JOB_MAX_BYTES,
        "disposition": disposition,
        "reason": reason,
    }
    signal_sha256 = sha256(
        _json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "signal_id": f"routing-signal:{signal_sha256[:24]}",
        **payload,
        "signal_sha256": signal_sha256,
    }


def _independent_bounded_cross_job_groups(
    routing_signals: list[dict[str, Any]],
) -> list[list[str]]:
    """Deduplicate bounded key buckets without transitive component merging.

    Different semantic keys remain independent exact-review candidates.  Relation
    review can consolidate records after exact evidence is reopened; joining every
    overlapping key first can turn a chain of useful bounded signals into one giant
    and unusable component.
    """

    groups: set[tuple[str, ...]] = set()
    for signal in routing_signals:
        if signal.get("disposition") == "candidate":
            groups.add(tuple(sorted(_coerce_string_list(signal.get("member_atom_ids")))))
        elif signal.get("disposition") == "partitioned_candidate":
            groups.update(
                tuple(sorted(_coerce_string_list(group)))
                for group in signal.get("refinement_groups", [])
                if isinstance(group, list)
            )
    return [list(group) for group in sorted(groups)]


def _run_cross_job_problem_synthesis(
    *,
    repo_root: Path,
    stage_artifacts_dir: Path,
    template_text: str,
    stage_guidance_text: str,
    prompt_atoms: list[dict[str, Any]],
    miner_receipts: list[dict[str, Any]],
    eligible_evidence_sha256_by_atom: dict[str, str] | None = None,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
) -> dict[str, Any]:
    """Route uncapped leaf themes hierarchically, then reopen exact atoms before promotion."""

    import json as _json

    leaf_nodes = _cross_job_leaf_routing_nodes(
        prompt_atoms=prompt_atoms,
        miner_receipts=miner_receipts,
        eligible_evidence_sha256_by_atom=eligible_evidence_sha256_by_atom,
    )
    leaf_job_tags = {
        tag for node in leaf_nodes for tag in _coerce_string_list(node.get("source_job_tags"))
    }
    if len(leaf_nodes) < 2 or len(leaf_job_tags) < 2:
        return {
            "schema_version": 1,
            "status": "not_required",
            "leaf_theme_count": len(leaf_nodes),
            "leaf_job_count": len(leaf_job_tags),
            "routing_levels": [],
            "exact_syntheses": [],
            "decision_overrides": [],
        }

    source_job_by_atom = {
        atom_id: tag
        for node in leaf_nodes
        for atom_id in _coerce_string_list(node.get("member_atom_ids"))
        for tag in _coerce_string_list(node.get("source_job_tags"))
    }
    leaf_evidence_sha256_by_atom = {
        atom_id: str(node_hashes[atom_id])
        for node in leaf_nodes
        for node_hashes in [
            node.get("evidence_sha256_by_atom")
            if isinstance(node.get("evidence_sha256_by_atom"), dict)
            else {}
        ]
        for atom_id in _coerce_string_list(node.get("member_atom_ids"))
    }
    original_disposition_by_atom = {
        atom_id: str(disposition)
        for node in leaf_nodes
        for mapping in [
            node.get("original_disposition_by_atom")
            if isinstance(node.get("original_disposition_by_atom"), dict)
            else {}
        ]
        for atom_id, disposition in mapping.items()
    }
    exact_atoms_by_id = {
        atom_id: atom
        for atom in prompt_atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        if atom_id is not None
    }
    nodes = leaf_nodes
    routing_levels: list[dict[str, Any]] = []
    routing_signals: list[dict[str, Any]] = []
    level = 1
    while True:
        route_prompt_atoms = [_routing_node_prompt_atom(node) for node in nodes]
        batches = _problem_mining_job_batches(
            route_prompt_atoms,
            max_atoms=_CROSS_JOB_ROUTING_MAX_ITEMS,
            max_bytes=_CROSS_JOB_ROUTING_MAX_BYTES,
        )
        nodes_by_route = {str(node["route_id"]): node for node in nodes}
        next_nodes: list[dict[str, Any]] = []
        level_receipts: list[dict[str, Any]] = []
        level_keys_by_route: dict[str, list[str]] = {}
        for batch_index, batch_atoms in enumerate(batches, start=1):
            batch_route_ids = [str(atom["atom_id"]) for atom in batch_atoms]
            batch_nodes = [nodes_by_route[route_id] for route_id in batch_route_ids]
            pass_tag = f"cross_job_routing_l{level:02d}_b{batch_index:03d}"
            prompt = _render_problem_mining_contract_prompt(
                template_text=template_text,
                stage_guidance_text=stage_guidance_text,
                prefix=(
                    "CROSS-JOB ROUTING ONLY. These are compact, hash-bound summaries of "
                    "leaf decisions or child routing bundles. Identify possible shared "
                    "observed problems across source jobs. Do not claim a final problem or "
                    "solution; exact evidence will be reopened later. Do not group by wording "
                    "alone. This routing-only pass has one deliberate extension to the record "
                    "schema: every routing record MUST include `routing_keys`, an array of two "
                    "to five canonical semantic keys of at most 80 characters covering "
                    "mechanism, symptom, boundary, or failure phase. Every routing atom must "
                    "support exactly one routing record, including singleton themes. Keys are "
                    "used across all batches so middle themes remain semantically visible."
                ),
            )
            reviewed = _run_independently_reviewed_problem_pass(
                repo_root=repo_root,
                stage_artifacts_dir=stage_artifacts_dir,
                base_tag=pass_tag,
                prompt=prompt,
                prompt_atoms=batch_atoms,
                agent=agent,
                model=model,
                cfg=cfg,
            )
            level_receipts.append(dict(reviewed["receipt"]))
            batch_keys_by_route: dict[str, list[str]] = {}
            routing_decisions = {
                _coerce_string(decision.get("atom_id")): decision
                for decision in reviewed["decisions"]
                if isinstance(decision, dict)
                and _coerce_string(decision.get("atom_id")) is not None
            }
            invalid_routing_decisions = sorted(
                route_id
                for route_id in batch_route_ids
                if (routing_decisions.get(route_id) or {}).get("disposition") != "supports_case"
                or len(
                    _coerce_string_list((routing_decisions.get(route_id) or {}).get("problem_ids"))
                )
                != 1
            )
            if invalid_routing_decisions:
                raise ValueError(
                    "cross_job_routing_decision_contract_invalid:"
                    + ",".join(invalid_routing_decisions)
                )
            for record in reviewed["records"]:
                cited_route_ids = _coerce_string_list(record.get("evidence_atom_ids"))
                routing_keys = _routing_record_keys(record)
                for route_id in cited_route_ids:
                    if route_id not in nodes_by_route:
                        continue
                    existing_keys = batch_keys_by_route.setdefault(route_id, [])
                    existing_keys.extend(key for key in routing_keys if key not in existing_keys)
            missing_key_routes = sorted(set(batch_route_ids) - set(batch_keys_by_route))
            if missing_key_routes:
                raise ValueError(
                    "cross_job_routing_semantic_coverage_missing:" + ",".join(missing_key_routes)
                )
            over_keyed_routes = sorted(
                route_id
                for route_id, keys in batch_keys_by_route.items()
                if not (_CROSS_JOB_ROUTING_KEY_MIN <= len(keys) <= _CROSS_JOB_ROUTING_KEY_MAX)
            )
            if over_keyed_routes:
                raise ValueError(
                    "cross_job_routing_key_count_invalid:" + ",".join(over_keyed_routes)
                )
            for route_id, keys in batch_keys_by_route.items():
                level_keys_by_route[route_id] = list(dict.fromkeys(keys))
            next_nodes.append(
                _combine_routing_nodes(
                    batch_nodes,
                    level=level,
                    routing_keys_by_child=batch_keys_by_route,
                )
            )
        nodes_by_routing_key: dict[str, list[dict[str, Any]]] = {}
        for route_id, routing_keys in level_keys_by_route.items():
            node = nodes_by_route[route_id]
            for routing_key in routing_keys:
                nodes_by_routing_key.setdefault(routing_key, []).append(node)
        for routing_key, keyed_nodes in nodes_by_routing_key.items():
            member_atom_ids = {
                atom_id
                for node in keyed_nodes
                for mapping in [
                    node.get("routing_key_member_atom_ids")
                    if isinstance(node.get("routing_key_member_atom_ids"), dict)
                    else {}
                ]
                for atom_id in (
                    _coerce_string_list(mapping.get(routing_key))
                    or _coerce_string_list(node.get("member_atom_ids"))
                )
            }
            source_jobs = {
                source_job_by_atom[atom_id]
                for atom_id in member_atom_ids
                if atom_id in source_job_by_atom
            }
            # Multiple original atoms inside one carry bundle were grouped only by
            # the deterministic routing batcher.  A new key on that single bundle
            # is not cross-bundle evidence and must not reopen the whole bundle.
            if len(keyed_nodes) < 2 or len(member_atom_ids) < 2 or len(source_jobs) < 2:
                continue
            routing_signals.append(
                _cross_job_routing_signal(
                    level=level,
                    routing_key=routing_key,
                    member_atom_ids=member_atom_ids,
                    source_job_by_atom=source_job_by_atom,
                    leaf_evidence_sha256_by_atom=leaf_evidence_sha256_by_atom,
                    exact_atoms_by_id=exact_atoms_by_id,
                )
            )
        routing_levels.append(
            {
                "level": level,
                "input_node_count": len(nodes),
                "batch_count": len(batches),
                "output_node_count": len(next_nodes),
                "receipts": level_receipts,
                "routing_keys_by_route": level_keys_by_route,
                "routing_semantic_sha256": sha256(
                    _json.dumps(
                        level_keys_by_route,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "membership": [
                    {
                        "route_id": node["route_id"],
                        "member_atom_ids": node["member_atom_ids"],
                        "evidence_sha256_by_atom": node["evidence_sha256_by_atom"],
                        "membership_sha256": node["membership_sha256"],
                        "source_job_tags": node["source_job_tags"],
                    }
                    for node in next_nodes
                ],
            }
        )
        if len(batches) == 1:
            break
        nodes = next_nodes
        level += 1

    exact_syntheses: list[dict[str, Any]] = []
    override_state_by_atom: dict[str, dict[str, Any]] = {}
    final_records: list[dict[str, Any]] = []
    bounded_candidate_groups = _independent_bounded_cross_job_groups(routing_signals)
    for synthesis_index, group in enumerate(bounded_candidate_groups, start=1):
        exact_atoms = [
            exact_atoms_by_id[atom_id] for atom_id in group if atom_id in exact_atoms_by_id
        ]
        if len({source_job_by_atom.get(atom_id) for atom_id in group}) < 2:
            continue
        measured_exact_jobs = _problem_mining_job_batches(
            exact_atoms,
            max_atoms=_PROBLEM_MINING_JOB_MAX_ATOMS,
            max_bytes=_PROBLEM_MINING_JOB_MAX_BYTES,
        )
        if len(measured_exact_jobs) != 1:
            # Candidate groups are produced only from measured bounded signals.  Do
            # not make a later inconsistent ceiling a reason to run oversized work.
            raise AssertionError("bounded_cross_job_candidate_became_oversized")
        exact_tag = f"cross_job_exact_synthesis_{synthesis_index:03d}"
        exact_prompt = _render_problem_mining_contract_prompt(
            template_text=template_text,
            stage_guidance_text=stage_guidance_text,
            prefix=(
                "EXACT CROSS-JOB SYNTHESIS. A compact routing pass suggested a possible "
                "shared problem, but that routing output is not evidence. Read every exact "
                "full atom and required attachment in this fresh workspace. Emit a problem "
                "only when the exact observations establish it. Distinguish shared causal "
                "mechanism from similar wording, expected experimental failures, and unrelated "
                "symptoms. Every assigned atom must receive one final synthesis decision."
            ),
        )
        exact = _run_independently_reviewed_problem_pass(
            repo_root=repo_root,
            stage_artifacts_dir=stage_artifacts_dir,
            base_tag=exact_tag,
            prompt=exact_prompt,
            prompt_atoms=exact_atoms,
            agent=agent,
            model=model,
            cfg=cfg,
        )
        cross_records = []
        for record in exact["records"]:
            evidence_ids = _coerce_string_list(record.get("evidence_atom_ids"))
            if len({source_job_by_atom.get(atom_id) for atom_id in evidence_ids}) >= 2:
                cross_records.append(dict(record))
        retained_ids = {
            str(record["problem_id"])
            for record in cross_records
            if _coerce_string(record.get("problem_id")) is not None
        }
        local_decision_overrides: list[dict[str, Any]] = []
        for decision in exact["decisions"]:
            problem_ids = [
                problem_id
                for problem_id in _coerce_string_list(decision.get("problem_ids"))
                if problem_id in retained_ids
            ]
            atom_id = _coerce_string(decision.get("atom_id"))
            if (
                atom_id is not None
                and original_disposition_by_atom.get(atom_id) != "supports_case"
                and decision.get("disposition") == "supports_case"
                and problem_ids
            ):
                local_decision_overrides.append(
                    {**dict(decision), "problem_ids": sorted(set(problem_ids))}
                )
        for local_override in local_decision_overrides:
            atom_id = str(local_override["atom_id"])
            state = override_state_by_atom.setdefault(
                atom_id,
                {
                    "atom_id": atom_id,
                    "problem_ids": set(),
                    "exact_synthesis_provenance": [],
                },
            )
            state["problem_ids"].update(local_override["problem_ids"])
            state["exact_synthesis_provenance"].append(
                {
                    "tag": exact_tag,
                    "problem_ids": list(local_override["problem_ids"]),
                }
            )
        final_records.extend(cross_records)
        exact_syntheses.append(
            {
                "tag": exact_tag,
                "candidate_atom_ids": group,
                "candidate_membership_sha256": _routing_membership_sha256(
                    group,
                    {
                        atom_id: str(leaf["evidence_sha256_by_atom"][atom_id])
                        for leaf in leaf_nodes
                        for atom_id in _coerce_string_list(leaf.get("member_atom_ids"))
                        if atom_id in group
                    },
                ),
                "receipt": dict(exact["receipt"]),
                "records": cross_records,
                "decision_overrides": local_decision_overrides,
            }
        )

    decision_overrides = [
        {
            "atom_id": atom_id,
            "disposition": "supports_case",
            "problem_ids": sorted(state["problem_ids"]),
            "rationale": (
                "Independent exact cross-job synthesis retained support in: "
                + ", ".join(provenance["tag"] for provenance in state["exact_synthesis_provenance"])
            ),
            "revisit_when": None,
            "exact_synthesis_provenance": state["exact_synthesis_provenance"],
        }
        for atom_id, state in sorted(override_state_by_atom.items())
    ]

    return {
        "schema_version": 1,
        "status": "verified",
        "leaf_theme_count": len(leaf_nodes),
        "leaf_job_count": len(leaf_job_tags),
        "leaf_membership": [
            {
                "route_id": node["route_id"],
                "member_atom_ids": node["member_atom_ids"],
                "evidence_sha256_by_atom": node["evidence_sha256_by_atom"],
                "membership_sha256": node["membership_sha256"],
                "source_job_tags": node["source_job_tags"],
                "original_disposition_by_atom": node.get("original_disposition_by_atom", {}),
                "original_problem_ids_by_atom": node.get("original_problem_ids_by_atom", {}),
                "leaf_claim_sha256_by_atom": node.get("leaf_claim_sha256_by_atom", {}),
            }
            for node in leaf_nodes
        ],
        "routing_levels": routing_levels,
        "routing_signals": routing_signals,
        "nondiscriminative_routing_signal_count": sum(
            signal.get("disposition") == "nondiscriminative" for signal in routing_signals
        ),
        "candidate_groups": bounded_candidate_groups,
        "exact_syntheses": exact_syntheses,
        "decision_overrides": decision_overrides,
        "records": final_records,
        "routing_sha256": sha256(
            _json.dumps(
                {
                    "leaf": [node["membership_sha256"] for node in leaf_nodes],
                    "groups": bounded_candidate_groups,
                    "signals": [signal["signal_sha256"] for signal in routing_signals],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _run_problem_mining_stage(
    *,
    repo_root: Path,
    atoms: list[dict[str, Any]],
    pipeline_manifest: PipelinePromptManifest,
    artifacts_dir: Path,
    out_json: Path,
    out_md: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    dry_run: bool,
    stage_guidance_text: str,
    case_registry: dict[str, Any] | None = None,
    max_records_per_miner: int = 20,
) -> dict[str, Any]:
    """Run stage 1 problem mining and write the stage artifacts.

    Runs bounded neutral mining jobs and independently reviews every assigned atom,
    including proposed positive claims and non-support decisions.
    In dry-run mode no LLM call is made; the CLI writes the prompts and synthesizes a
    deterministic set of problem records from atoms so downstream stages can run on
    offline fixtures.
    The function always writes ``out_json`` and ``out_md``.

    Parameters
    ----------
    atoms:
        Eligible evidence atoms.
    pipeline_manifest:
        Loaded pipeline prompt manifest (version 2).
    artifacts_dir:
        Base artifacts directory (``*.backlog_artifacts``).
    out_json:
        Path for ``*.problem_records.json``.
    out_md:
        Path for ``*.problem_records.md``.
    agent:
        Agent identifier.
    model:
        Optional model override.
    cfg:
        Runner configuration.
    dry_run:
        When ``True``, skip LLM calls and synthesize deterministic problem records.
    stage_guidance_text:
        Problem-mining stage guidance text (injected into prompts).
    max_records_per_miner:
        Legacy compatibility hint retained in workspace metadata; live problem records are
        not count-capped.

    Returns
    -------
    dict[str, Any]
        Stage-1 document dict (also written to ``out_json``).
    """
    import json as _json

    stage = "problem_mining"
    stage_artifacts_dir = artifacts_dir / "problem_mining"
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)
    invocation_tracker = ModelInvocationTracker(stage_artifacts_dir)

    mining_atoms = eligible_problem_mining_atoms(atoms)
    prompt_atoms = _atoms_for_problem_mining_prompt(mining_atoms)
    evidence_draft = build_problem_mining_evidence_draft(
        atoms=atoms,
        eligible_atoms=mining_atoms,
        mode="dry_run" if dry_run else "live",
    )
    template_paths = list(pipeline_manifest.problem_miner_templates)
    if mining_atoms and not template_paths:
        raise ValueError("problem_mining_requires_at_least_one_miner_template")
    neutral_template_path = next(
        (path for path in template_paths if path.name == "problem_miner_default.md"),
        template_paths[0] if template_paths else None,
    )
    job_atom_batches = _problem_mining_job_batches(prompt_atoms)
    miner_jobs: list[dict[str, Any]] = []
    assignments: dict[str, list[str]] = {}
    for job_index, job_atoms in enumerate(job_atom_batches, start=1):
        tag = f"problem_mining_{job_index:03d}"
        if neutral_template_path is None:
            raise ValueError("problem_mining_neutral_template_missing")
        template_path = neutral_template_path
        assigned_atom_ids = [
            str(atom["atom_id"]) for atom in job_atoms if isinstance(atom.get("atom_id"), str)
        ]
        assignments[tag] = assigned_atom_ids
        miner_jobs.append(
            {
                "tag": tag,
                "template_path": template_path,
                "prompt_atoms": job_atoms,
                "assigned_atom_ids": assigned_atom_ids,
            }
        )
    atoms_placeholder = _json.dumps(
        {"atoms_file": "atoms.json"},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    all_records: list[dict[str, Any]] = []
    miner_results: list[dict[str, Any]] = []
    miner_receipts: list[dict[str, Any]] = []
    miner_contexts: dict[str, dict[str, Any]] = {}
    live_failures: list[str] = []

    for job in miner_jobs:
        tag = str(job["tag"])
        template_path = Path(job["template_path"])
        job_prompt_atoms = list(job["prompt_atoms"])
        assigned_atom_ids = list(job["assigned_atom_ids"])
        miner_out_dir = stage_artifacts_dir / tag
        miner_out_dir.mkdir(parents=True, exist_ok=True)

        workspace_dir = miner_out_dir / f"workspace_{uuid4().hex}"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        manifest = _write_chunked_problem_mining_atoms_workspace(
            workspace_dir=workspace_dir,
            prompt_atoms=job_prompt_atoms,
            max_records_per_miner=max_records_per_miner,
            assigned_atom_ids=assigned_atom_ids,
            source_root=repo_root,
        )
        atoms_json_path = workspace_dir / "atoms.json"

        template_text = template_path.read_text(encoding="utf-8")
        prompt = (
            template_text.replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
            .replace("{{ATOMS_JSON}}", atoms_placeholder)
            .replace("{{MAX_RECORDS_PER_MINER}}", str(max_records_per_miner))
        )

        meta: dict[str, Any] = {
            "tag": tag,
            "template": template_path.name,
            "atom_count": len(job_prompt_atoms),
            "assigned_atom_count": len(assigned_atom_ids),
            "assigned_atom_ids": assigned_atom_ids,
            "prompt_atom_count": len(job_prompt_atoms),
            "workspace_dir": str(workspace_dir),
            "atoms_json": str(atoms_json_path),
            "atoms_json_bytes": atoms_json_path.stat().st_size,
            "atoms_chunk_count": int(manifest.get("chunk_count") or 0),
            "atoms_total_chunk_bytes": int(manifest.get("total_chunk_bytes") or 0),
        }
        miner_contexts[tag] = {
            "template_name": template_path.name,
            "workspace_dir": workspace_dir,
            "manifest": manifest,
        }

        if dry_run:
            print(
                f"[stage1] dry-run: skipping LLM call for {tag} (template={template_path.name})",
                file=sys.stderr,
            )
            # Write the would-be prompt so developers can inspect it.
            (miner_out_dir / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
            meta["prompt_chars"] = len(prompt)
            meta["status"] = "dry_run"
            meta["records"] = []
            miner_results.append(meta)
            continue

        primary_attempt_history: list[dict[str, Any]] = []
        try:
            meta["prompt_chars"] = len(prompt)
            primary_run = _run_problem_mining_job_with_response_retry(
                repo_root=repo_root,
                stage_artifacts_dir=stage_artifacts_dir,
                base_tag=tag,
                prompt=prompt,
                prompt_atoms=job_prompt_atoms,
                assigned_atom_ids=assigned_atom_ids,
                max_records_per_miner=max_records_per_miner,
                eligible_atom_ids=list(evidence_draft["eligible_atom_ids"]),
                template_name=template_path.name,
                record_contract_error_prefix=("problem_mining_problem_record_contract_invalid"),
                agent=agent,
                model=model,
                cfg=cfg,
                initial_workspace_dir=workspace_dir,
                initial_manifest=manifest,
            )
            primary_attempt_history = list(primary_run["attempt_history"])
            workspace_dir = Path(primary_run["workspace_dir"])
            manifest = dict(primary_run["manifest"])
            meta["attempt_history"] = primary_attempt_history
            meta["successful_attempt_tag"] = primary_run.get("successful_attempt_tag")
            meta["format_retry_count"] = max(0, len(primary_attempt_history) - 1)
            meta["same_session_correction_count"] = max(
                0, len(primary_attempt_history) - 1
            )
            failure = primary_run.get("failure")
            if isinstance(failure, Exception):
                raise failure
            envelope = dict(primary_run["envelope"])
            records = list(primary_run["records"])
            warnings = list(primary_run["warnings"])
            normalized_events_path = Path(primary_run["normalized_events_path"])
            primary_receipt = dict(primary_run["receipt"])
            meta["workspace_dir"] = str(workspace_dir)
            meta["atoms_json"] = str(workspace_dir / "atoms.json")
            meta["atoms_json_bytes"] = (workspace_dir / "atoms.json").stat().st_size
            meta["atoms_chunk_count"] = int(manifest.get("chunk_count") or 0)
            meta["atoms_total_chunk_bytes"] = int(manifest.get("total_chunk_bytes") or 0)
            final_records = records
            final_decisions = [
                dict(decision)
                for decision in envelope["atom_decisions"]
                if isinstance(decision, dict)
            ]
            support_ids = {
                str(decision["atom_id"])
                for decision in primary_receipt["atom_decisions"]
                if decision.get("disposition") == "supports_case"
            }
            non_support_ids = set(assigned_atom_ids) - support_ids
            review_warnings: list[str] = []
            review_tag = f"{tag}_coverage_depth_review"
            review_out_dir = stage_artifacts_dir / review_tag
            review_out_dir.mkdir(parents=True, exist_ok=True)
            review_workspace = review_out_dir / f"workspace_{uuid4().hex}"
            review_atoms = list(job_prompt_atoms)
            review_atom_ids = list(assigned_atom_ids)
            review_manifest = _write_chunked_problem_mining_atoms_workspace(
                workspace_dir=review_workspace,
                prompt_atoms=review_atoms,
                max_records_per_miner=max_records_per_miner,
                assigned_atom_ids=review_atom_ids,
                source_root=repo_root,
            )
            review_prompt = _coverage_depth_review_prompt(
                primary_records=records,
                primary_decisions=final_decisions,
                template_text=template_text,
                stage_guidance_text=stage_guidance_text,
                atoms_placeholder=atoms_placeholder,
                max_records_per_miner=max_records_per_miner,
            )
            review_receipt: dict[str, Any]
            review_failed = False
            review_failure: str | None = None
            review_attempt_history: list[dict[str, Any]] = []
            try:
                review_run = _run_problem_mining_job_with_response_retry(
                    repo_root=repo_root,
                    stage_artifacts_dir=stage_artifacts_dir,
                    base_tag=review_tag,
                    prompt=review_prompt,
                    prompt_atoms=review_atoms,
                    assigned_atom_ids=review_atom_ids,
                    max_records_per_miner=max_records_per_miner,
                    eligible_atom_ids=list(evidence_draft["eligible_atom_ids"]),
                    template_name=f"adversarial:{template_path.name}",
                    record_contract_error_prefix=(
                        "problem_mining_coverage_depth_review_contract_invalid"
                    ),
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    initial_workspace_dir=review_workspace,
                    initial_manifest=review_manifest,
                )
                review_attempt_history = list(review_run["attempt_history"])
                review_workspace = Path(review_run["workspace_dir"])
                review_manifest = dict(review_run["manifest"])
                review_run_failure = review_run.get("failure")
                if isinstance(review_run_failure, Exception):
                    raise review_run_failure
                review_records = list(review_run["records"])
                review_warnings = list(review_run["warnings"])
                review_receipt = dict(review_run["receipt"])
                miner_receipt, final_records, final_decisions = (
                    _build_composite_miner_receipt(
                        tag=tag,
                        template_name=template_path.name,
                        assigned_atom_ids=assigned_atom_ids,
                        eligible_atom_ids=evidence_draft["eligible_atom_ids"],
                        primary_records=records,
                        primary_decisions=[
                            dict(decision)
                            for decision in primary_receipt["atom_decisions"]
                            if isinstance(decision, dict)
                        ],
                        primary_receipt=primary_receipt,
                        review_records=review_records,
                        review_decisions=[
                            dict(decision)
                            for decision in review_receipt["atom_decisions"]
                            if isinstance(decision, dict)
                        ],
                        review_receipt=review_receipt,
                        primary_normalized_events_path=normalized_events_path,
                        primary_workspace_dir=workspace_dir,
                        primary_workspace_manifest=manifest,
                    )
                )
            except Exception as review_exc:  # noqa: BLE001
                # The primary pass has already been read-attested and contract-validated.
                # Preserve those records and decisions for diagnosis/retry instead of
                # replacing the entire assignment with a failed/unresolved receipt. The
                # non-verified status keeps shadow/export closed until the independent
                # review succeeds.
                review_failed = True
                review_failure = f"{type(review_exc).__name__}: {review_exc}"
                review_warnings.append(f"coverage_depth_review_failed: {review_failure}")
                review_receipt = build_failed_miner_receipt(
                    tag=review_tag,
                    template_name=f"adversarial:{template_path.name}",
                    assigned_atom_ids=review_atom_ids,
                    workspace_dir=review_workspace,
                    workspace_manifest=review_manifest,
                    error=review_failure,
                )
                review_receipt["attempt_history"] = review_attempt_history
                review_receipt["successful_attempt_tag"] = None
                miner_receipt = _preserve_primary_after_coverage_review_failure(
                    primary_receipt=primary_receipt,
                    review_receipt=review_receipt,
                    review_failure=review_failure,
                )
                live_failures.append(f"{review_tag}:{review_failure}")
            meta["coverage_depth_review_attempt_history"] = review_attempt_history
            meta["coverage_depth_review_format_retry_count"] = max(
                0, len(review_attempt_history) - 1
            )
            meta["coverage_depth_review_same_session_correction_count"] = max(
                0, len(review_attempt_history) - 1
            )
            meta["status"] = "partial_review_failure" if review_failed else "ok"
            if review_failure is not None:
                meta["review_error"] = review_failure
            meta["records"] = len(final_records)
            meta["warnings"] = [*warnings, *review_warnings]
            meta["normalized_events"] = str(normalized_events_path)
            meta["non_support_review_atom_count"] = len(non_support_ids)
            meta["positive_review_atom_count"] = len(support_ids)
            meta["coverage_depth_review_atom_count"] = len(review_atom_ids)
            meta["full_read_attestation_count"] = len(miner_receipt["read_attestations"])
            miner_receipts.append(miner_receipt)
            all_records.extend(final_records)
            print(
                f"[stage1] {tag}: {len(final_records)} problem records "
                f"({len(warnings) + len(review_warnings)} warnings; "
                f"reviewed_positive={len(support_ids)} "
                f"reviewed_non_support={len(non_support_ids)} "
                f"review_status={'failed' if review_failed else 'verified'})",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001
            failure_text = f"{type(exc).__name__}: {exc}"
            meta["status"] = "error"
            meta["error"] = failure_text
            meta["attempt_history"] = primary_attempt_history
            meta["successful_attempt_tag"] = None
            meta["format_retry_count"] = max(0, len(primary_attempt_history) - 1)
            meta["same_session_correction_count"] = max(
                0, len(primary_attempt_history) - 1
            )
            live_failures.append(f"{tag}:{failure_text}")
            failed_receipt = build_failed_miner_receipt(
                tag=tag,
                template_name=template_path.name,
                assigned_atom_ids=assigned_atom_ids,
                workspace_dir=workspace_dir,
                workspace_manifest=manifest,
                error=failure_text,
            )
            failed_receipt["attempt_history"] = primary_attempt_history
            failed_receipt["successful_attempt_tag"] = None
            miner_receipts.append(failed_receipt)
            print(
                f"[stage1] {tag}: error during problem mining: {exc}",
                file=sys.stderr,
            )

        miner_results.append(meta)

    if dry_run:
        synthesized = _synthesize_problem_records_from_atoms(
            mining_atoms, max_records=max_records_per_miner
        )
        all_records.extend(synthesized)
        miner_receipts = [
            build_dry_run_miner_receipt(
                tag=tag,
                template_name=str(context["template_name"]),
                assigned_atom_ids=assignments[tag],
                records=synthesized,
            )
            for tag, context in sorted(miner_contexts.items())
        ]
        print(
            f"[stage1] dry-run: synthesized {len(synthesized)} problem records from atoms",
            file=sys.stderr,
        )
    cross_job_synthesis: dict[str, Any] = {
        "schema_version": 1,
        "status": "not_required" if dry_run else "pending",
        "leaf_theme_count": 0,
        "leaf_job_count": 0,
        "routing_levels": [],
        "exact_syntheses": [],
        "decision_overrides": [],
        "records": [],
    }
    if not dry_run:
        try:
            if neutral_template_path is None:
                raise ValueError("problem_mining_neutral_template_missing")
            cross_job_synthesis = _run_cross_job_problem_synthesis(
                repo_root=repo_root,
                stage_artifacts_dir=stage_artifacts_dir,
                template_text=neutral_template_path.read_text(encoding="utf-8"),
                stage_guidance_text=stage_guidance_text,
                prompt_atoms=prompt_atoms,
                miner_receipts=miner_receipts,
                eligible_evidence_sha256_by_atom={
                    str(row["atom_id"]): str(row["evidence_sha256"])
                    for row in evidence_draft.get("atom_evidence", [])
                    if isinstance(row, dict)
                    and _coerce_string(row.get("atom_id")) is not None
                    and _coerce_string(row.get("evidence_sha256")) is not None
                },
                agent=agent,
                model=model,
                cfg=cfg,
            )
            all_records.extend(
                dict(record)
                for record in cross_job_synthesis.get("records", [])
                if isinstance(record, dict)
            )
        except Exception as exc:  # noqa: BLE001
            failure_text = f"{type(exc).__name__}: {exc}"
            cross_job_synthesis = {
                **cross_job_synthesis,
                "status": "failed",
                "error": failure_text,
            }
            live_failures.append(f"cross_job_synthesis:{failure_text}")
            print(
                f"[stage1] cross-job synthesis failed: {failure_text}",
                file=sys.stderr,
            )
    evidence_draft["miners"] = miner_receipts
    evidence_draft["cross_job_synthesis"] = cross_job_synthesis
    evidence_draft["status"] = (
        "dry_run_partitioned"
        if dry_run
        else "partial_failed_jobs"
        if live_failures
        else "full_reads_and_assignments_verified"
    )

    # Repeated IDs may only combine genuinely identical problem statements. Preserve
    # the union of fully-read citations; conflicting prose under one ID fails closed.
    by_problem_id: dict[str, dict[str, Any]] = {}
    deduped: list[dict[str, Any]] = []
    for rec in all_records:
        pid = _coerce_string(rec.get("problem_id"))
        if pid is None:
            deduped.append(rec)
            continue
        previous = by_problem_id.get(pid)
        if previous is None:
            retained = dict(rec)
            by_problem_id[pid] = retained
            deduped.append(retained)
            continue
        for field in ("title", "problem", "user_impact", "severity"):
            if previous.get(field) != rec.get(field):
                raise ValueError(f"problem_mining_conflicting_problem_id:{pid}:{field}")
        previous["evidence_atom_ids"] = sorted(
            set(_coerce_string_list(previous.get("evidence_atom_ids")))
            | set(_coerce_string_list(rec.get("evidence_atom_ids")))
        )

    case_records = assign_problem_case_ids(
        deduped,
        atoms,
        case_registry=case_registry,
        strict_new_output=True,
    )

    stage_doc = build_stage_document(
        stage,
        case_records,
        input_meta={
            "atom_count": len(atoms),
            "eligible_problem_origin_atom_count": len(mining_atoms),
            "derived_or_dispositioned_atom_count": len(atoms) - len(mining_atoms),
            "miner_template_count": len(pipeline_manifest.problem_miner_templates),
            "miner_count": len(miner_jobs),
            "miner_job_max_chunks": _PROBLEM_MINING_JOB_MAX_CHUNKS,
            "miner_job_max_atoms": _PROBLEM_MINING_JOB_MAX_ATOMS,
            "miner_job_max_bytes": _PROBLEM_MINING_JOB_MAX_BYTES,
            "dry_run": dry_run,
            "dry_run_synthesized_records": len(all_records) if dry_run else 0,
            "miner_results": miner_results,
            "failed_mining_jobs": list(live_failures),
            "problem_mining_evidence_draft": evidence_draft,
        },
        artifacts={
            "problem_records_json": str(out_json),
            "problem_records_md": str(out_md),
        },
    )
    stage_doc = attach_stage_model_invocation_contract(
        stage_doc,
        agent=agent,
        dry_run=dry_run,
        manifest_refs=invocation_tracker.collect(),
        invocation_expected=bool(miner_jobs),
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        _json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    title = out_json.stem.removesuffix(".problem_records") or "Problem Records"
    md_text = _render_problem_records_markdown(
        case_records,
        title=f"{title} – Problem Records",
    )
    out_md.write_text(md_text, encoding="utf-8")

    print(f"[stage1] wrote {out_json}", file=sys.stderr)
    print(f"[stage1] wrote {out_md}", file=sys.stderr)

    return stage_doc


def _relation_case_preview(item: dict[str, Any]) -> dict[str, Any]:
    """Return the evidence-bearing case packet shown to the relation reviewer."""

    return {
        "problem_id": item.get("problem_id"),
        "case_id": item.get("case_id"),
        "title": item.get("title"),
        "problem": item.get("problem"),
        "user_impact": item.get("user_impact"),
        "canonical_symptoms": item.get("canonical_symptoms") or [],
        "evidence_summary": item.get("evidence_summary"),
        "evidence_atom_ids": item.get("evidence_atom_ids") or [],
        "root_cause_status": item.get("root_cause_status") or "unestablished",
        "verified_mechanism_sha256": item.get("verified_mechanism_sha256"),
        "verified_causal_signature_sha256": item.get(
            "verified_causal_signature_sha256"
        ),
        "case_identity_status": item.get("case_identity_status"),
        "case_identity_candidate_ids": item.get("case_identity_candidate_ids") or [],
        "provisional_same_cause_group": item.get("provisional_same_cause_group"),
        "provisional_same_cause_integrity_errors": item.get(
            "provisional_same_cause_integrity_errors"
        )
        or [],
        "case_state": item.get("case_state") or "active",
        "carried_forward": bool(item.get("_carried_forward_case")),
        "candidate_only": bool(item.get("_relation_candidate_only")),
    }


def _verified_relation_edges_from_case_registry(
    registry: dict[str, Any],
) -> set[tuple[str, str]]:
    """Load objective registry lineage and hash-verified relation-receipt edges."""

    import hashlib
    import json as _json

    cases_raw = registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, dict) else {}

    def _edge(left: str, right: str) -> tuple[str, str]:
        return tuple(sorted((left, right)))

    edges: set[tuple[str, str]] = set()
    known_case_ids = {
        case_id
        for raw_case_id, raw_entry in cases.items()
        if isinstance(raw_entry, dict)
        for case_id in [_coerce_string(raw_entry.get("case_id")) or str(raw_case_id)]
        if case_id
    }
    relation_refs: list[dict[str, Any]] = []
    for raw_case_id, raw_entry in cases.items():
        if not isinstance(raw_entry, dict):
            continue
        case_id = _coerce_string(raw_entry.get("case_id")) or str(raw_case_id)
        for field in ("alias_of", "duplicate_of", "split_from_case_id"):
            target = _coerce_string(raw_entry.get(field))
            if target in known_case_ids and target != case_id:
                edges.add(_edge(case_id, target))
        direct_ref = raw_entry.get("relation_receipt")
        if isinstance(direct_ref, dict):
            relation_refs.append(dict(direct_ref))
        incoming_raw = raw_entry.get("incoming_relation_receipts")
        if isinstance(incoming_raw, list):
            relation_refs.extend(dict(item) for item in incoming_raw if isinstance(item, dict))

    seen_refs: set[tuple[str, str, str]] = set()
    for ref in relation_refs:
        source = _coerce_string(ref.get("source_case_id"))
        target = _coerce_string(ref.get("target_case_id"))
        receipt_path_raw = _coerce_string(ref.get("receipt_path"))
        receipt_sha256 = _coerce_string(ref.get("receipt_sha256"))
        relation_sha256 = _coerce_string(ref.get("relation_sha256"))
        if (
            source not in known_case_ids
            or target not in known_case_ids
            or source == target
            or receipt_path_raw is None
            or receipt_sha256 is None
            or relation_sha256 is None
        ):
            continue
        ref_key = (source, target, relation_sha256)
        if ref_key in seen_refs:
            continue
        seen_refs.add(ref_key)
        try:
            receipt_path = Path(receipt_path_raw).expanduser().resolve()
            receipt_bytes = receipt_path.read_bytes()
            if hashlib.sha256(receipt_bytes).hexdigest() != receipt_sha256.casefold():
                continue
            payload = validate_case_relation_receipt(_json.loads(receipt_bytes.decode("utf-8")))
        except (OSError, UnicodeError, ValueError, TypeError, _json.JSONDecodeError):
            continue
        matched = any(
            isinstance(relation, dict)
            and relation.get("source_case_id") == source
            and relation.get("target_case_id") == target
            and relation.get("relation_sha256") == relation_sha256
            for relation in payload.get("relations", [])
        )
        if matched:
            edges.add(_edge(source, target))
    return edges


def _relation_review_payload(
    *,
    relation_items: list[dict[str, Any]],
    neighborhoods: list[dict[str, Any]],
    focus_problem_ids: set[str],
) -> dict[str, Any]:
    """Attach actual case evidence to similarity rankings."""

    previews = [_relation_case_preview(item) for item in relation_items]
    preview_index_by_problem_id = {
        problem_id: index
        for index, preview in enumerate(previews)
        for problem_id in [_coerce_string(preview.get("problem_id"))]
        if problem_id is not None
    }
    enriched: list[dict[str, Any]] = []
    included_case_indices: set[int] = set()
    neighbor_keys = (
        "most_related_by_semantic",
        "most_related_by_evidence_overlap",
        "most_related_by_metadata",
        "most_related_by_path_anchor",
    )
    for raw_neighborhood in neighborhoods:
        focus_id = _coerce_string(raw_neighborhood.get("focus_id"))
        if focus_id not in focus_problem_ids:
            continue
        focus_index = preview_index_by_problem_id.get(focus_id)
        if focus_index is not None:
            included_case_indices.add(focus_index)
        neighborhood = dict(raw_neighborhood)
        neighborhood["focus_item"] = next(
            (
                preview
                for preview in previews
                if _coerce_string(preview.get("problem_id")) == focus_id
            ),
            None,
        )
        for key in neighbor_keys:
            raw_neighbors = neighborhood.get(key)
            if not isinstance(raw_neighbors, list):
                continue
            detailed: list[dict[str, Any]] = []
            for raw_neighbor in raw_neighbors:
                if not isinstance(raw_neighbor, dict):
                    continue
                neighbor = dict(raw_neighbor)
                index = neighbor.get("index")
                if isinstance(index, int) and 0 <= index < len(previews):
                    included_case_indices.add(index)
                    neighbor["candidate_item"] = previews[index]
                detailed.append(neighbor)
            neighborhood[key] = detailed
        enriched.append(neighborhood)
    relevant_previews = [previews[index] for index in sorted(included_case_indices)]
    return {
        "focus_neighborhoods": enriched,
        # Keep each model call bounded to its focus cases and surfaced candidates.
        # The complete candidate universe is still ranked and validated by the runner.
        "case_index": relevant_previews,
        "case_index_count": len(relevant_previews),
        "full_case_index_count": len(previews),
        "focus_count": len(enriched),
    }


def _relation_decision_item_errors(
    decision: dict[str, Any],
    *,
    focus_problem_ids: set[str],
    known_problem_ids: set[str],
    known_evidence_atom_ids: set[str],
    allowed_actions: set[str],
) -> list[str]:
    """Return model-correctable structure/reference errors for one relation decision."""

    errors: list[str] = []
    allowed_fields = {
        "focus_id",
        "action",
        "target_ids",
        "group_id",
        "member_ids",
        "alias_target_id",
        "split_groups",
        "evidence_atom_ids",
        "rationale",
        "review_confidence",
    }
    unknown_fields = sorted(set(decision) - allowed_fields)
    if unknown_fields:
        errors.append("relation_decision_unknown_fields:" + ",".join(unknown_fields))
    focus_id = _coerce_string(decision.get("focus_id"))
    action = _coerce_string(decision.get("action"))
    rationale = _coerce_string(decision.get("rationale"))
    confidence = decision.get("review_confidence")
    if focus_id is None or focus_id not in focus_problem_ids:
        errors.append("relation_decision_focus_invalid:" + (focus_id or "(missing)"))
    if action not in allowed_actions:
        errors.append("relation_decision_action_invalid:" + (action or "(missing)"))
    if rationale is None:
        errors.append("relation_decision_rationale_missing:" + (focus_id or "(missing)"))
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        errors.append("relation_decision_confidence_invalid:" + (focus_id or "(missing)"))

    def string_list(field: str) -> list[str] | None:
        raw = decision.get(field)
        if not isinstance(raw, list) or not raw or not all(
            isinstance(value, str) and value.strip() for value in raw
        ):
            return None
        values = [str(value).strip() for value in raw]
        return values if len(values) == len(set(values)) else None

    evidence_ids = string_list("evidence_atom_ids")
    collapse_action = action in {"merge", "alias", "same_cause_group"}
    if collapse_action:
        if evidence_ids is None or not set(evidence_ids) <= known_evidence_atom_ids:
            errors.append("relation_decision_evidence_refs_invalid:" + str(focus_id))

    if action == "merge":
        target_ids = string_list("target_ids")
        if (
            target_ids is None
            or focus_id in set(target_ids)
            or not set(target_ids) <= known_problem_ids
        ):
            errors.append("relation_decision_merge_targets_invalid:" + str(focus_id))
    elif action == "alias":
        target = _coerce_string(decision.get("alias_target_id"))
        if target is None or target == focus_id or target not in known_problem_ids:
            errors.append("relation_decision_alias_target_invalid:" + str(focus_id))
    elif action == "same_cause_group":
        group_id = _coerce_string(decision.get("group_id"))
        member_ids = string_list("member_ids")
        if group_id is None:
            errors.append("relation_decision_group_id_missing:" + str(focus_id))
        if (
            member_ids is None
            or focus_id not in set(member_ids)
            or len(member_ids) < 2
            or not set(member_ids) <= known_problem_ids
        ):
            errors.append("relation_decision_group_members_invalid:" + str(focus_id))
    elif action == "split":
        split_groups = decision.get("split_groups")
        if not isinstance(split_groups, list) or len(split_groups) < 2:
            errors.append("relation_decision_split_groups_invalid:" + str(focus_id))
        else:
            for index, group in enumerate(split_groups):
                refs = group.get("evidence_atom_ids") if isinstance(group, dict) else None
                if (
                    not isinstance(refs, list)
                    or not refs
                    or not all(isinstance(value, str) and value.strip() for value in refs)
                    or not set(refs) <= known_evidence_atom_ids
                ):
                    errors.append(
                        f"relation_decision_split_group_evidence_invalid:{focus_id}:{index}"
                    )
    return list(dict.fromkeys(errors))


def _relation_review_response_errors(
    decisions: list[dict[str, Any]],
    *,
    focus_problem_ids: set[str],
    known_problem_ids: set[str],
    known_evidence_atom_ids: set[str],
    allowed_actions: set[str],
) -> tuple[list[str], set[str]]:
    errors: list[str] = []
    valid_focus_ids: list[str] = []
    for index, decision in enumerate(decisions):
        item_errors = _relation_decision_item_errors(
            decision,
            focus_problem_ids=focus_problem_ids,
            known_problem_ids=known_problem_ids,
            known_evidence_atom_ids=known_evidence_atom_ids,
            allowed_actions=allowed_actions,
        )
        errors.extend(f"relation_decision_index={index}:{error}" for error in item_errors)
        focus_id = _coerce_string(decision.get("focus_id"))
        if not item_errors and focus_id is not None:
            valid_focus_ids.append(focus_id)
    counts = {focus_id: valid_focus_ids.count(focus_id) for focus_id in set(valid_focus_ids)}
    duplicates = sorted(focus_id for focus_id, count in counts.items() if count != 1)
    missing = sorted(focus_problem_ids - set(valid_focus_ids))
    if duplicates:
        errors.append("problem_mining_relation_reviewer_duplicate_focus:" + ",".join(duplicates))
    if missing:
        errors.append("problem_mining_relation_reviewer_missing_focus:" + ",".join(missing))
    return list(dict.fromkeys(errors)), {
        focus_id for focus_id, count in counts.items() if count == 1
    }


def _validate_relation_decision_focuses(
    decisions: list[dict[str, Any]],
    *,
    work_unit_problem_ids: set[str],
) -> None:
    """Require exactly one model disposition for every active focus item."""

    focus_counts: dict[str, int] = {}
    invalid_focus_ids: set[str] = set()
    for decision in decisions:
        focus_id = _coerce_string(decision.get("focus_id"))
        if focus_id is None or focus_id not in work_unit_problem_ids:
            invalid_focus_ids.add(focus_id or "(missing)")
            continue
        focus_counts[focus_id] = focus_counts.get(focus_id, 0) + 1
    if invalid_focus_ids:
        raise ValueError(
            "problem_mining_relation_reviewer_candidate_only_focus: "
            + ", ".join(sorted(invalid_focus_ids))
        )
    duplicate_focus_ids = sorted(focus_id for focus_id, count in focus_counts.items() if count != 1)
    if duplicate_focus_ids:
        raise ValueError(
            "problem_mining_relation_reviewer_duplicate_focus: " + ", ".join(duplicate_focus_ids)
        )
    missing_focus_ids = sorted(work_unit_problem_ids - set(focus_counts))
    if missing_focus_ids:
        raise ValueError(
            "problem_mining_relation_reviewer_missing_focus: " + ", ".join(missing_focus_ids)
        )


def _relation_review_focus_batches(
    focus_problem_ids: list[str],
    *,
    max_foci: int = _PROBLEM_RELATION_REVIEW_MAX_FOCI,
) -> list[list[str]]:
    """Partition relation-review focus IDs without dropping or duplicating a case."""

    if max_foci <= 0:
        raise ValueError("problem_mining_relation_review_max_foci_must_be_positive")
    ordered = list(dict.fromkeys(focus_problem_ids))
    return [ordered[index : index + max_foci] for index in range(0, len(ordered), max_foci)]


def _failed_relation_review_batch_decisions(
    focus_problem_ids: list[str],
    *,
    error: str,
) -> list[dict[str, Any]]:
    """Retain every case independently when one relation-review batch fails."""

    return [
        {
            "focus_id": focus_id,
            "action": "keep_separate",
            "rationale": (
                "The relation-review batch failed, so the runner retained this mined "
                "case independently for research instead of suppressing it."
            ),
            "review_confidence": 0.0,
            "provisional_relation_suggestion": {
                "kind": "relation_review_batch_failure",
                "error": error,
            },
            "relation_validation_errors": ["relation_review_batch_failed"],
        }
        for focus_id in focus_problem_ids
    ]


def _failed_relation_review_batch_count(batches: Sequence[Mapping[str, Any]]) -> int:
    """Count fully and partially failed relation batches."""

    return sum(
        1
        for batch in batches
        if batch.get("status")
        in {
            "failed_provisional_keep_separate",
            "failed_partial_provisional_keep_separate",
        }
    )


def _write_relation_review_checkpoint(
    *,
    review_dir: Path,
    tag: str,
    decisions: list[dict[str, Any]],
    batches: list[dict[str, Any]],
) -> None:
    """Checkpoint completed relation batches while preserving the legacy artifact paths."""

    import json as _json

    (review_dir / f"{tag}.prompt.txt").write_text(
        _json.dumps(
            {
                "schema_version": "problem_relation_review_batches_v1",
                "batch_count": len(batches),
                "batches": batches,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (review_dir / f"{tag}.response.txt").write_text(
        _json.dumps(decisions, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_relation_review_batches(
    *,
    relation_items: list[dict[str, Any]],
    neighborhoods: list[dict[str, Any]],
    focus_problem_ids: list[str],
    template: str,
    allowed_actions: list[str],
    stage_guidance_text: str,
    review_dir: Path,
    tag: str,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    max_foci: int = _PROBLEM_RELATION_REVIEW_MAX_FOCI,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run bounded relation batches and degrade only a failed batch to provisional review."""

    import json as _json

    decisions: list[dict[str, Any]] = []
    batch_meta: list[dict[str, Any]] = []
    for batch_index, batch_focus_ids in enumerate(
        _relation_review_focus_batches(focus_problem_ids, max_foci=max_foci),
        start=1,
    ):
        batch_tag = f"{tag}_batch_{batch_index:03d}"
        batch_payload = _relation_review_payload(
            relation_items=relation_items,
            neighborhoods=neighborhoods,
            focus_problem_ids=set(batch_focus_ids),
        )
        known_problem_ids = {
            problem_id
            for item in batch_payload.get("case_index", [])
            if isinstance(item, dict)
            for problem_id in [_coerce_string(item.get("problem_id"))]
            if problem_id is not None
        }
        known_evidence_atom_ids = {
            atom_id
            for item in batch_payload.get("case_index", [])
            if isinstance(item, dict)
            for atom_id in (
                item.get("evidence_atom_ids")
                if isinstance(item.get("evidence_atom_ids"), list)
                else []
            )
            if isinstance(atom_id, str) and atom_id.strip()
        }
        prompt = (
            template.replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
            .replace(
                "{{ALLOWED_ACTIONS}}",
                _json.dumps(allowed_actions, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{NEIGHBORHOODS_JSON}}",
                _json.dumps(batch_payload, ensure_ascii=False, indent=2),
            )
        )
        prompt_path = review_dir / f"{batch_tag}.prompt.txt"
        response_path = review_dir / f"{batch_tag}.response.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        workspace_dir = review_dir / f"{batch_tag}.workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        continuity_key = sha256(
            _json.dumps(
                {
                    "workspace_dir": str(workspace_dir.resolve()),
                    "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
                    "focus_ids": sorted(batch_focus_ids),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        meta: dict[str, Any] = {
            "batch_index": batch_index,
            "tag": batch_tag,
            "focus_ids": list(batch_focus_ids),
            "focus_count": len(batch_focus_ids),
            "case_index_count": batch_payload["case_index_count"],
            "prompt_path": str(prompt_path),
            "response_path": str(response_path),
        }

        def relation_attempt(
            *,
            attempt_prompt: str,
            attempt_tag: str,
            attempt_number: int,
            resume_session_id: str | None,
            _workspace_dir: Path = workspace_dir,
            _batch_focus_ids: tuple[str, ...] = tuple(batch_focus_ids),
            _known_problem_ids: frozenset[str] = frozenset(known_problem_ids),
            _known_evidence_atom_ids: frozenset[str] = frozenset(known_evidence_atom_ids),
            _continuity_key: str = continuity_key,
        ) -> CorrectionObservation[dict[str, Any]]:
            transport_error: str | None = None
            attempt_started = _time.monotonic()
            try:
                run = run_stage_prompt_json(
                    stage="problem_mining",
                    prompt=attempt_prompt,
                    out_dir=review_dir,
                    tag=attempt_tag,
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    workspace_dir=_workspace_dir,
                    resume_session_id=resume_session_id,
                    allow_empty=True,
                    structured=True,
                )
                if isinstance(run, str):
                    response = run
                    session_id = None
                    elapsed_seconds = 0.0
                    attempt_response_path = review_dir / f"{attempt_tag}.response.txt"
                else:
                    response = str(run.response)
                    session_id = _coerce_string(run.agent_session_id)
                    elapsed_seconds = max(0.0, float(run.elapsed_seconds))
                    attempt_response_path = run.response_path
            except Exception as exc:  # noqa: BLE001 - pre-session retry remains possible
                if resume_session_id is not None:
                    raise
                response = ""
                session_id = None
                elapsed_seconds = max(0.0, _time.monotonic() - attempt_started)
                attempt_response_path = review_dir / f"{attempt_tag}.response.txt"
                transport_error = f"{type(exc).__name__}: {exc}"
            errors: list[str] = []
            batch_decisions: list[dict[str, Any]] = []
            valid_focus_ids: set[str] = set()
            try:
                parsed = _json.loads(response)
                if not isinstance(parsed, list) or not all(
                    isinstance(item, dict) for item in parsed
                ):
                    raise ValueError(
                        "problem_mining_relation_reviewer_response_not_a_list_of_objects"
                    )
                batch_decisions = [dict(item) for item in parsed]
                errors, valid_focus_ids = _relation_review_response_errors(
                    batch_decisions,
                    focus_problem_ids=set(_batch_focus_ids),
                    known_problem_ids=set(_known_problem_ids),
                    known_evidence_atom_ids=set(_known_evidence_atom_ids),
                    allowed_actions=set(allowed_actions),
                )
            except Exception as exc:  # noqa: BLE001 - exact validator feedback
                errors.append(f"{type(exc).__name__}: {exc}")
            if transport_error is not None:
                errors.insert(0, transport_error)
            valid_keys = tuple(
                sorted(
                    "relation_focus:" + focus_id
                    for focus_id in valid_focus_ids
                )
            )
            payload = {
                "response": response,
                "response_path": str(attempt_response_path.resolve()),
                "prompt_path": str((review_dir / f"{attempt_tag}.prompt.txt").resolve()),
                "tag": attempt_tag,
                "attempt_number": attempt_number,
                "decisions": batch_decisions,
                "validation_errors": list(errors),
                "agent_session_id": session_id,
                "elapsed_seconds": elapsed_seconds,
            }
            return CorrectionObservation(
                payload=payload,
                validation_errors=tuple(errors),
                state_sha256=correction_state_sha256(
                    candidate=response,
                    validation_errors=errors,
                    valid_item_keys=list(valid_keys),
                ),
                valid_item_keys=valid_keys,
                agent_session_id=session_id,
                continuity_key=_continuity_key,
                cost_seconds=elapsed_seconds,
            )

        retained_observation: CorrectionObservation[dict[str, Any]] | None = None
        try:
            initial = relation_attempt(
                attempt_prompt=prompt,
                attempt_tag=batch_tag,
                attempt_number=1,
                resume_session_id=None,
            )
            acquisition_attempts: tuple[CorrectionObservation[dict[str, Any]], ...] = ()
            acquisition_status = "not_required"
            if agent.strip().lower() == "codex" and initial.agent_session_id is None:
                acquisition = acquire_author_session(
                    initial=initial,
                    invoke_fresh=lambda attempt_number, _prompt=prompt, _batch_tag=batch_tag: relation_attempt(
                        attempt_prompt=_prompt,
                        attempt_tag=(
                            f"{_batch_tag}_session_acquisition_"
                            f"{attempt_number - 1:03d}"
                        ),
                        attempt_number=attempt_number,
                        resume_session_id=None,
                    ),
                )
                acquisition_status = acquisition.status
                acquisition_attempts = acquisition.attempts
                initial = acquisition.current
            original_cost = initial.cost_seconds
            prompt_sha256 = sha256(prompt.encode("utf-8")).hexdigest()

            def invoke_relation_correction(
                current: CorrectionObservation[dict[str, Any]],
                attempt_number: int,
                prior_assessment: Any,
                _prompt_sha256: str = prompt_sha256,
                _batch_tag: str = batch_tag,
                _session_id: str | None = initial.agent_session_id,
                _relation_attempt: Any = relation_attempt,
                _attempt_number_offset: int = max(
                    0, len(acquisition_attempts) - 1
                ),
            ) -> CorrectionObservation[dict[str, Any]]:
                progress = (
                    {
                        "decision": prior_assessment.decision,
                        "reason": prior_assessment.reason,
                        "resolved_error_identities": list(
                            prior_assessment.resolved_error_identities
                        ),
                        "introduced_error_identities": list(
                            prior_assessment.introduced_error_identities
                        ),
                        "repeated_state": prior_assessment.repeated_state,
                        "safe_frontier_updated": prior_assessment.safe_frontier_updated,
                    }
                    if prior_assessment is not None
                    else None
                )
                correction_prompt = (
                    "SAME-AUTHOR RELATION-REVIEW RESPONSE CORRECTION\n\n"
                    "Revise your immediately prior complete JSON response in this exact session. "
                    "Do not rerun the review from scratch. Preserve valid focus decisions unless "
                    "a correlated correction requires changing them. Return the complete JSON "
                    "list and no prose. Unknown validator errors are valid feedback.\n\n"
                    f"Original prompt SHA-256: {_prompt_sha256}\n"
                    "Immediately prior response SHA-256: "
                    f"{sha256(str(current.payload.get('response') or '').encode('utf-8')).hexdigest()}\n\n"
                    "Validation errors:\n"
                    + "\n".join(f"- {error}" for error in current.validation_errors)
                    + "\n\nValid keyed focus decisions:\n"
                    + (
                        "\n".join(f"- {key}" for key in current.valid_item_keys)
                        or "- none verified yet"
                    )
                    + "\n\nPrior correction progress:\n"
                    + _json.dumps(progress, ensure_ascii=False, indent=2, sort_keys=True)
                )
                return _relation_attempt(
                    attempt_prompt=correction_prompt,
                    attempt_tag=f"{_batch_tag}_correction_{attempt_number - 1:03d}",
                    attempt_number=(
                        attempt_number + _attempt_number_offset
                    ),
                    resume_session_id=_session_id,
                )

            if acquisition_status.startswith("repairable_paused:"):
                correction = CorrectionRunResult(
                    status=acquisition_status,
                    current=initial,
                    best=initial,
                    attempts=acquisition_attempts,
                    assessments=(),
                    correction_cost_since_progress=acquisition.cost_since_progress,
                    total_correction_cost=max(
                        0.0,
                        acquisition.total_cost
                        - max(0.0, float(acquisition_attempts[0].cost_seconds)),
                    ),
                )
            else:
                correction = run_progressive_correction(
                    initial=initial,
                    invoke_correction=invoke_relation_correction,
                    pause_policy=lambda _current, _assessment, since_progress, _total, original_cost=original_cost: (
                        "correction_cost_reached_original"
                        if original_cost > 0.0 and since_progress >= original_cost
                        else None
                    ),
                )
            correction_metrics = correction_run_metrics(correction, expected_quality=None)
            if acquisition_attempts:
                correction_metrics = correction_metrics_with_session_acquisition(
                    correction_metrics,
                    acquisition,
                )
            retained = (
                correction.current
                if correction.status in {"accepted", "corrected"}
                else correction.best
            )
            retained_observation = retained
            meta["correction_status"] = correction.status
            meta["correction_metrics"] = correction_metrics
            meta["attempt_history"] = [
                {
                    "attempt_number": int(
                        attempt.payload.get("attempt_number") or index
                    ),
                    "tag": attempt.payload.get("tag"),
                    "status": (
                        "invalid" if attempt.validation_errors else "verified"
                    ),
                    "prompt_path": attempt.payload.get("prompt_path"),
                    "response_path": attempt.payload.get("response_path"),
                    "validation_errors": list(attempt.validation_errors),
                    "valid_item_keys": list(attempt.valid_item_keys),
                    "agent_session_id": attempt.agent_session_id,
                    "workspace_dir": str(workspace_dir.resolve()),
                    "elapsed_seconds": attempt.cost_seconds,
                }
                for index, attempt in enumerate(correction.attempts, start=1)
            ]
            meta["attempt_history"].extend(
                {
                    "attempt_number": failure.attempt_number
                    + max(0, len(acquisition_attempts) - 1),
                    "tag": (
                        f"{batch_tag}_correction_"
                        f"{failure.attempt_number - 1:03d}"
                    ),
                    "status": "invocation_failed",
                    "prompt_path": None,
                    "response_path": None,
                    "validation_errors": [
                        f"{failure.error_type}: {failure.error_message}"
                    ],
                    "valid_item_keys": [],
                    "agent_session_id": failure.agent_session_id,
                    "workspace_dir": str(workspace_dir.resolve()),
                    "elapsed_seconds": failure.cost_seconds,
                    "failure_identity": failure.failure_identity,
                }
                for failure in correction.invocation_failures
            )
            meta["attempt_history"].sort(
                key=lambda record: int(record["attempt_number"])
            )
            if acquisition_status == "acquired" and len(acquisition_attempts) > 1:
                pre_author_attempts = acquisition_attempts[:-1]
                meta["attempt_history"] = [
                    {
                        "attempt_number": index,
                        "tag": attempt.payload.get("tag"),
                        "status": (
                            "invalid" if attempt.validation_errors else "verified"
                        ),
                        "prompt_path": attempt.payload.get("prompt_path"),
                        "response_path": attempt.payload.get("response_path"),
                        "validation_errors": list(attempt.validation_errors),
                        "valid_item_keys": list(attempt.valid_item_keys),
                        "agent_session_id": attempt.agent_session_id,
                        "workspace_dir": str(workspace_dir.resolve()),
                        "elapsed_seconds": attempt.cost_seconds,
                    }
                    for index, attempt in enumerate(pre_author_attempts, start=1)
                ] + meta["attempt_history"]
                meta["correction_metrics"] = correction_metrics
            if correction.status not in {"accepted", "corrected"}:
                raise ValueError(
                    "problem_mining_relation_reviewer_correction_incomplete:"
                    + correction.status
                )
            batch_decisions = [dict(item) for item in retained.payload["decisions"]]
            decisions.extend(batch_decisions)
            meta["status"] = "completed"
            meta["decision_count"] = len(batch_decisions)
            meta["successful_attempt_tag"] = retained.payload.get("tag")
            meta["response_path"] = retained.payload.get("response_path")
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            if not response_path.exists():
                response_path.write_text(
                    _json.dumps(
                        {
                            "status": "failed_provisional_keep_separate",
                            "error": error,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            partial: list[dict[str, Any]] = []
            if retained_observation is not None:
                candidates = retained_observation.payload.get("decisions")
                candidate_list = candidates if isinstance(candidates, list) else []
                _, valid_focus_ids = _relation_review_response_errors(
                    [dict(candidate) for candidate in candidate_list if isinstance(candidate, dict)],
                    focus_problem_ids=set(batch_focus_ids),
                    known_problem_ids=known_problem_ids,
                    known_evidence_atom_ids=known_evidence_atom_ids,
                    allowed_actions=set(allowed_actions),
                )
                partial = [
                    dict(candidate)
                    for candidate in candidate_list
                    if isinstance(candidate, dict)
                    and (focus_id := _coerce_string(candidate.get("focus_id"))) is not None
                    and focus_id in valid_focus_ids
                ]
            retained_focus_ids = {
                str(item["focus_id"])
                for item in partial
                if isinstance(item.get("focus_id"), str)
            }
            missing_focus_ids = [
                focus_id for focus_id in batch_focus_ids if focus_id not in retained_focus_ids
            ]
            fallback = _failed_relation_review_batch_decisions(missing_focus_ids, error=error)
            decisions.extend([*partial, *fallback])
            meta["status"] = (
                "failed_partial_provisional_keep_separate"
                if partial
                else "failed_provisional_keep_separate"
            )
            meta["error"] = error
            meta["retained_valid_decision_count"] = len(partial)
            meta["decision_count"] = len(partial) + len(fallback)
        batch_meta.append(meta)
        _write_relation_review_checkpoint(
            review_dir=review_dir,
            tag=tag,
            decisions=decisions,
            batches=batch_meta,
        )

    _validate_relation_decision_focuses(
        decisions,
        work_unit_problem_ids=set(focus_problem_ids),
    )
    return decisions, batch_meta


def _persist_canonical_relation_receipts(
    *,
    canonical_records: list[dict[str, Any]],
    registry: dict[str, Any],
    review_response_path: Path,
    receipt_path: Path,
    stage: str = "problem_mining",
) -> tuple[dict[str, dict[str, Any]], Path]:
    """Persist exact source-to-canonical edges and bind them into the case registry."""

    receipt_relations: list[dict[str, Any]] = []
    for canonical_record in canonical_records:
        target_case_id = _coerce_string(canonical_record.get("case_id"))
        actions = sorted(
            {
                action
                for raw_action in canonical_record.get("case_relation_actions", [])
                if isinstance(raw_action, dict)
                for action in [_coerce_string(raw_action.get("action"))]
                if action in {"merge", "alias", "same_cause_group"}
            }
        )
        absorbed_case_ids = _coerce_string_list(canonical_record.get("absorbed_case_ids"))
        if target_case_id is None or not absorbed_case_ids:
            continue
        if not actions:
            raise ValueError(
                "problem_mining_relation_receipt_missing_canonical_decision_actions: "
                + ", ".join(absorbed_case_ids)
            )
        receipt_relations.extend(
            {
                "source_case_id": source_case_id,
                "target_case_id": target_case_id,
                "direction": "source_to_canonical",
                "relation_kind": "canonical_absorption",
                "decision_actions": actions,
            }
            for source_case_id in absorbed_case_ids
        )

    _relation_receipt, relation_receipt_refs = write_case_relation_receipt(
        receipt_path,
        stage=stage,
        relation_review_response_path=review_response_path,
        relations=receipt_relations,
    )
    registry_cases_raw = registry.get("cases")
    registry_cases = registry_cases_raw if isinstance(registry_cases_raw, dict) else {}
    for source_case_id, receipt_ref in relation_receipt_refs.items():
        source_entry_raw = registry_cases.get(source_case_id)
        target_case_id = str(receipt_ref["target_case_id"])
        target_entry_raw = registry_cases.get(target_case_id)
        if (
            not isinstance(source_entry_raw, dict)
            or source_entry_raw.get("alias_of") != target_case_id
        ):
            raise ValueError(
                "problem_mining_relation_receipt_registry_direction_mismatch: "
                f"{source_case_id} -> {target_case_id}"
            )
        if not isinstance(target_entry_raw, dict):
            raise ValueError(
                f"problem_mining_relation_receipt_registry_target_missing: {target_case_id}"
            )
        target_outbound = {
            value.strip()
            for field in ("alias_of", "duplicate_of", "superseded_by")
            for value in [target_entry_raw.get(field)]
            if isinstance(value, str) and value.strip()
        }
        if target_outbound:
            raise ValueError(
                "problem_mining_relation_receipt_target_not_canonical: "
                f"{target_case_id} -> {', '.join(sorted(target_outbound))}"
            )
        source_entry_raw["relation_receipt"] = dict(receipt_ref)
        incoming_raw = target_entry_raw.get("incoming_relation_receipts")
        incoming = (
            [dict(item) for item in incoming_raw if isinstance(item, dict)]
            if isinstance(incoming_raw, list)
            else []
        )
        incoming = [item for item in incoming if item.get("source_case_id") != source_case_id]
        incoming.append(dict(receipt_ref))
        incoming.sort(key=lambda item: str(item.get("source_case_id") or ""))
        target_entry_raw["incoming_relation_receipts"] = incoming
    for source_case_id in relation_receipt_refs:
        seen: set[str] = set()
        cursor = source_case_id
        while cursor in registry_cases:
            if cursor in seen:
                raise ValueError(
                    f"problem_mining_relation_receipt_registry_cycle: {source_case_id}"
                )
            seen.add(cursor)
            entry = registry_cases.get(cursor)
            if not isinstance(entry, dict):
                break
            outbound = {
                value.strip()
                for field in ("alias_of", "duplicate_of", "superseded_by")
                for value in [entry.get(field)]
                if isinstance(value, str) and value.strip()
            }
            if len(outbound) > 1:
                raise ValueError(f"problem_mining_relation_receipt_registry_conflict: {cursor}")
            if not outbound:
                break
            cursor = next(iter(outbound))
    immutable_receipt_path = (
        Path(next(iter(relation_receipt_refs.values()))["receipt_path"])
        if relation_receipt_refs
        else receipt_path.with_name(
            f"{receipt_path.stem}.{_relation_receipt['content_sha256'][:16]}{receipt_path.suffix}"
        )
    )
    return relation_receipt_refs, immutable_receipt_path


def _run_problem_case_relation_review(
    *,
    stage_doc: dict[str, Any],
    problem_records: list[dict[str, Any]],
    atoms: list[dict[str, Any]],
    pipeline_manifest: PipelinePromptManifest,
    artifacts_dir: Path,
    out_json: Path,
    out_md: Path,
    case_registry_path: Path,
    previous_case_registry: dict[str, Any],
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    dry_run: bool,
    stage_guidance_text: str,
    relation_decisions_override: list[dict[str, Any]] | None = None,
    relation_review_batches_override: list[dict[str, Any]] | None = None,
    relation_manifest_refs: list[Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Operationalize explicit case relations before prioritization and research.

    The reviewer still makes every semantic decision.  Once made, merge, alias,
    split, and same-cause decisions change the actual work-unit list instead of being
    annotations on a temporary option-stage projection.
    """

    import json as _json

    relation_config_raw = yaml.safe_load(
        pipeline_manifest.relation_review_config_path.read_text(encoding="utf-8")
    )
    relation_config = relation_config_raw if isinstance(relation_config_raw, dict) else {}
    verified_mechanism_sha256_by_case = verified_mechanism_identities_from_case_registry(
        previous_case_registry
    )
    verified_causal_signature_sha256_by_case = (
        verified_causal_identities_from_case_registry(previous_case_registry)
    )
    verified_relation_edges = _verified_relation_edges_from_case_registry(previous_case_registry)
    problem_records = attach_supporting_atoms_to_problem_cases(problem_records, atoms)
    work_unit_problem_ids = {
        problem_id
        for record in problem_records
        for problem_id in [
            _coerce_string(record.get("problem_id")),
            *_coerce_string_list(record.get("case_member_problem_ids")),
        ]
        if problem_id is not None
    }
    active_focus_problem_id_order = [
        problem_id
        for record in problem_records
        for problem_id in [_coerce_string(record.get("problem_id"))]
        if problem_id is not None
    ]
    historical_case_records = problem_case_records_from_registry(previous_case_registry)
    historical_context_by_case = {
        case_id: record
        for record in historical_case_records
        for case_id in [_coerce_string(record.get("case_id"))]
        if case_id is not None
    }
    relation_items = [dict(record) for record in problem_records]
    for relation_item in relation_items:
        relation_item.pop("verified_mechanism_sha256", None)
        relation_item.pop("verified_mechanism_source", None)
        case_id = _coerce_string(relation_item.get("case_id"))
        verified_mechanism_sha256 = verified_mechanism_sha256_by_case.get(case_id or "")
        if verified_mechanism_sha256 is not None:
            relation_item["verified_mechanism_sha256"] = verified_mechanism_sha256
            relation_item["verified_mechanism_source"] = "runner_research_evidence_verification_v1"
        verified_causal_signature = verified_causal_signature_sha256_by_case.get(
            case_id or ""
        )
        if verified_causal_signature is not None:
            relation_item["verified_causal_signature_sha256"] = verified_causal_signature
            relation_item["verified_causal_signature_source"] = (
                "runner_verified_causal_signature_v1"
            )
        historical_context = historical_context_by_case.get(case_id or "")
        historical_provisional = (
            historical_context.get("provisional_same_cause_group")
            if isinstance(historical_context, Mapping)
            else None
        )
        if isinstance(historical_provisional, Mapping):
            relation_item["provisional_same_cause_group"] = deepcopy(
                dict(historical_provisional)
            )
            relation_item["case_identity_status"] = historical_context.get(
                "case_identity_status"
            )
            relation_item["case_identity_candidate_ids"] = _coerce_string_list(
                historical_context.get("case_identity_candidate_ids")
            )
            integrity_errors = _coerce_string_list(
                historical_context.get("provisional_same_cause_integrity_errors")
            )
            if integrity_errors:
                relation_item["provisional_same_cause_integrity_errors"] = integrity_errors
    represented_case_ids = {
        case_id
        for record in relation_items
        for case_id in [_coerce_string(record.get("case_id"))]
        if case_id is not None
    }
    represented_problem_ids = {
        problem_id
        for record in relation_items
        for problem_id in [
            _coerce_string(record.get("problem_id")),
            *_coerce_string_list(record.get("case_member_problem_ids")),
        ]
        if problem_id is not None
    }
    for historical_case in historical_case_records:
        historical_case_id = _coerce_string(historical_case.get("case_id"))
        historical_problem_ids = {
            problem_id
            for problem_id in [
                _coerce_string(historical_case.get("problem_id")),
                *_coerce_string_list(historical_case.get("case_member_problem_ids")),
            ]
            if problem_id is not None
        }
        if historical_case_id in represented_case_ids:
            continue
        if historical_problem_ids & represented_problem_ids:
            continue
        candidate = dict(historical_case)
        candidate["_relation_candidate_only"] = True
        relation_items.append(candidate)

    neighborhoods = rank_stage_related_items(
        relation_items,
        stage="problem_mining",
        relation_config=relation_config,
        embedder=None,
    )
    template_path = pipeline_manifest.relation_reviewer_template
    if template_path is None:
        raise ValueError("problem_mining: pipeline manifest missing relation_reviewer_template")
    template = pipeline_manifest.template_text(template_path)
    allowed_actions = ["merge", "alias", "split", "same_cause_group", "keep_separate"]

    tag = "problem_mining_relation_review_001"
    review_dir = artifacts_dir / "problem_mining" / tag
    review_dir.mkdir(parents=True, exist_ok=True)
    relation_invocation_tracker = ModelInvocationTracker(review_dir)
    decisions: list[dict[str, Any]] = []
    relation_review_batches: list[dict[str, Any]] = []
    if relation_decisions_override is not None:
        decisions = [dict(item) for item in relation_decisions_override]
        relation_review_batches = [
            dict(item) for item in (relation_review_batches_override or [])
        ]
        _write_relation_review_checkpoint(
            review_dir=review_dir,
            tag=tag,
            decisions=decisions,
            batches=relation_review_batches,
        )
    elif not problem_records:
        (review_dir / f"{tag}.prompt.txt").write_text(
            "[no active or newly mined cases] no relation-review batches created.\n",
            encoding="utf-8",
        )
        (review_dir / f"{tag}.response.txt").write_text(
            "[no active or newly mined cases] relation review not executed.\n",
            encoding="utf-8",
        )
    elif dry_run:
        dry_payload = _relation_review_payload(
            relation_items=relation_items,
            neighborhoods=neighborhoods,
            focus_problem_ids=set(
                active_focus_problem_id_order[:_PROBLEM_RELATION_REVIEW_MAX_FOCI]
            ),
        )
        dry_prompt = (
            template.replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
            .replace(
                "{{ALLOWED_ACTIONS}}",
                _json.dumps(allowed_actions, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{NEIGHBORHOODS_JSON}}",
                _json.dumps(dry_payload, ensure_ascii=False, indent=2),
            )
        )
        (review_dir / f"{tag}.prompt.txt").write_text(dry_prompt, encoding="utf-8")
        (review_dir / f"{tag}.response.txt").write_text(
            "[dry-run] pre-prioritization relation-review prompt not executed.\n",
            encoding="utf-8",
        )
    else:
        decisions, relation_review_batches = _run_relation_review_batches(
            relation_items=relation_items,
            neighborhoods=neighborhoods,
            focus_problem_ids=active_focus_problem_id_order,
            template=template,
            allowed_actions=allowed_actions,
            stage_guidance_text=stage_guidance_text,
            review_dir=review_dir,
            tag=tag,
            agent=agent,
            model=model,
            cfg=cfg,
        )

    canonical_candidates = canonicalize_problem_cases(
        relation_items,
        decisions,
        stage="problem_mining",
        strict_review=not dry_run,
        verified_mechanism_sha256_by_case=verified_mechanism_sha256_by_case,
        verified_causal_signature_sha256_by_case=(
            verified_causal_signature_sha256_by_case
        ),
        verified_relation_edges=verified_relation_edges,
    )
    canonical_records: list[dict[str, Any]] = []
    for candidate in canonical_candidates:
        member_problem_ids = {
            problem_id
            for problem_id in [
                _coerce_string(candidate.get("problem_id")),
                *_coerce_string_list(candidate.get("case_member_problem_ids")),
            ]
            if problem_id is not None
        }
        if not member_problem_ids.intersection(work_unit_problem_ids):
            continue
        state = _coerce_string(candidate.get("case_state")) or "active"
        if state in TERMINAL_CASE_STATES:
            reopened = dict(candidate)
            reopened["reopened_from_state"] = state
            reopened["case_state"] = "active"
            candidate = reopened
        candidate.pop("_relation_candidate_only", None)
        canonical_records.append(candidate)
    stage_input_meta_raw = stage_doc.get("input_meta")
    stage_input_meta = dict(stage_input_meta_raw) if isinstance(stage_input_meta_raw, dict) else {}
    evidence_draft_raw = stage_input_meta.get("problem_mining_evidence_draft")
    if not isinstance(evidence_draft_raw, dict):
        raise ValueError("problem_mining_evidence_draft_missing")
    original_records_by_case = {
        case_id: dict(record)
        for record in problem_records
        for case_id in [_coerce_string(record.get("case_id"))]
        if case_id is not None
    }
    historical_records_by_case = {
        case_id: dict(record)
        for record in historical_case_records
        for case_id in [_coerce_string(record.get("case_id"))]
        if case_id is not None
    }
    atoms_by_id = {
        atom_id: atom
        for atom in atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        if atom_id is not None
    }

    disposition_records_by_case: dict[str, dict[str, Any]] = {}
    registry_records_by_case: dict[str, dict[str, Any]] = {}

    def preserve_record(
        target: dict[str, dict[str, Any]],
        record: Mapping[str, Any],
        *,
        evidence_atom_ids: Sequence[str] | None = None,
    ) -> None:
        case_id = _coerce_string(record.get("case_id"))
        if case_id is None:
            return
        preserved = dict(record)
        if evidence_atom_ids is not None:
            preserved["evidence_atom_ids"] = list(dict.fromkeys(evidence_atom_ids))
            derived = set(_coerce_string_list(record.get("derived_evidence_atom_ids")))
            preserved["derived_evidence_atom_ids"] = [
                atom_id for atom_id in preserved["evidence_atom_ids"] if atom_id in derived
            ]
            preserved["source_evidence_atom_ids"] = [
                atom_id for atom_id in preserved["evidence_atom_ids"] if atom_id not in derived
            ]
        existing = target.get(case_id)
        if existing is None:
            target[case_id] = preserved
            return
        for field in (
            "evidence_atom_ids",
            "source_evidence_atom_ids",
            "derived_evidence_atom_ids",
            "case_member_problem_ids",
        ):
            existing[field] = list(
                dict.fromkeys(
                    _coerce_string_list(existing.get(field))
                    + _coerce_string_list(preserved.get(field))
                )
            )

    for candidate in canonical_records:
        identity_status = _coerce_string(candidate.get("case_identity_status"))
        candidate_case_ids = _coerce_string_list(
            candidate.get("case_identity_candidate_ids")
        )
        if identity_status == "provisional_same_cause" and candidate_case_ids:
            # One provisional research packet must not prematurely rewrite durable
            # case identity.  Disposition and registry updates retain every original
            # member until the combined research proof establishes one mechanism.
            for member_case_id in candidate_case_ids:
                source = original_records_by_case.get(
                    member_case_id
                ) or historical_records_by_case.get(member_case_id)
                if source is None:
                    raise ValueError(
                        "problem_mining_provisional_member_record_missing:"
                        + member_case_id
                    )
                preserve_record(disposition_records_by_case, source)
                preserve_record(registry_records_by_case, source)
            continue
        if identity_status == "pending_relation" and len(candidate_case_ids) > 1:
            # Ambiguous multi-case evidence remains attached only to identities it
            # already had.  The combined model record is visible to relation review,
            # but it cannot contaminate either case or mint a third one.
            pending_evidence = _coerce_string_list(candidate.get("evidence_atom_ids"))
            for member_case_id in candidate_case_ids:
                source = historical_records_by_case.get(member_case_id)
                matching_atom_ids = [
                    atom_id
                    for atom_id in pending_evidence
                    if member_case_id
                    in {
                        *_coerce_string_list(
                            (atoms_by_id.get(atom_id) or {}).get("supporting_case_ids")
                        ),
                        *(
                            [_coerce_string((atoms_by_id.get(atom_id) or {}).get("case_id"))]
                            if _coerce_string(
                                (atoms_by_id.get(atom_id) or {}).get("case_id")
                            )
                            is not None
                            else []
                        ),
                    }
                ]
                if source is None:
                    source = {
                        "problem_id": f"identity-pending:{member_case_id}",
                        "canonical_problem_id": f"identity-pending:{member_case_id}",
                        "case_id": member_case_id,
                        "case_member_problem_ids": [],
                        "evidence_atom_ids": matching_atom_ids,
                    }
                preserve_record(
                    disposition_records_by_case,
                    source,
                    evidence_atom_ids=list(
                        dict.fromkeys(
                            _coerce_string_list(source.get("evidence_atom_ids"))
                            + matching_atom_ids
                        )
                    ),
                )
            continue
        preserve_record(disposition_records_by_case, candidate)
        preserve_record(registry_records_by_case, candidate)

    disposition_records = list(disposition_records_by_case.values())
    registry_records = list(registry_records_by_case.values())
    partitioned_atoms = apply_problem_mining_decision_partition(
        atoms=atoms,
        canonical_records=canonical_records,
        draft=evidence_draft_raw,
    )
    updated_atoms = apply_atom_dispositions(partitioned_atoms, disposition_records)
    canonical_records = attach_supporting_atoms_to_problem_cases(
        canonical_records,
        updated_atoms,
    )
    registry = build_case_registry(
        registry_records,
        previous=previous_case_registry,
        supporting_atoms=updated_atoms,
    )
    registry_cases_raw = registry.get("cases")
    registry_cases = registry_cases_raw if isinstance(registry_cases_raw, dict) else {}
    for candidate in canonical_records:
        clearance = candidate.get("provisional_same_cause_clearance")
        if not isinstance(clearance, Mapping):
            continue
        for member_case_id in _coerce_string_list(clearance.get("member_case_ids")):
            entry = registry_cases.get(member_case_id)
            if not isinstance(entry, dict):
                continue
            entry["case_identity_status"] = "resolved"
            entry.pop("case_identity_candidate_ids", None)
            entry.pop("provisional_same_cause_group", None)
            entry.pop("provisional_same_cause_integrity_errors", None)
    for candidate in canonical_records:
        provisional = candidate.get("provisional_same_cause_group")
        if not isinstance(provisional, Mapping):
            continue
        member_case_ids = _coerce_string_list(provisional.get("member_case_ids"))
        for member_case_id in member_case_ids:
            entry = registry_cases.get(member_case_id)
            if not isinstance(entry, dict):
                continue
            entry["case_identity_status"] = "provisional_same_cause"
            entry["case_identity_candidate_ids"] = member_case_ids
            persisted_provisional = deepcopy(dict(provisional))
            persisted_provisional["research_unit_case_id"] = candidate.get("case_id")
            entry["provisional_same_cause_group"] = persisted_provisional
            entry.pop("provisional_same_cause_integrity_errors", None)
    relation_receipt_path = review_dir / f"{tag}.relations.json"
    _, immutable_relation_receipt_path = _persist_canonical_relation_receipts(
        canonical_records=canonical_records,
        registry=registry,
        review_response_path=review_dir / f"{tag}.response.txt",
        receipt_path=relation_receipt_path,
    )
    write_case_registry(case_registry_path, registry)

    evidence_receipt_path = out_json.with_name(f"{out_json.stem}.evidence_receipt.json")
    evidence_receipt = finalize_problem_mining_evidence_receipt(
        draft=evidence_draft_raw,
        atoms=updated_atoms,
        receipt_path=evidence_receipt_path,
    )
    evidence_receipt_ref = problem_mining_evidence_receipt_ref(
        receipt=evidence_receipt,
        receipt_path=evidence_receipt_path,
    )

    updated_doc = dict(stage_doc)
    updated_doc["items"] = canonical_records
    input_meta_raw = updated_doc.get("input_meta")
    input_meta = dict(input_meta_raw) if isinstance(input_meta_raw, dict) else {}
    input_meta.pop("problem_mining_evidence_draft", None)
    input_meta.update(
        {
            "pre_relation_problem_count": len(problem_records),
            "canonical_case_count": len(canonical_records),
            "relation_review_decision_count": len(decisions),
            "relation_review_batch_count": len(relation_review_batches),
            "relation_review_failed_batch_count": _failed_relation_review_batch_count(
                relation_review_batches
            ),
            "relation_review_batches": relation_review_batches,
            "relation_review_decisions": decisions,
            "pre_relation_problem_records": problem_records,
            "atom_dispositions": atom_disposition_summary(updated_atoms),
            "problem_mining_evidence_receipt": evidence_receipt_ref,
        }
    )
    updated_doc["input_meta"] = input_meta
    artifacts_raw = updated_doc.get("artifacts")
    artifact_refs = dict(artifacts_raw) if isinstance(artifacts_raw, dict) else {}
    artifact_refs.update(
        {
            "case_registry_json": str(case_registry_path),
            "relation_review_prompt": str(review_dir / f"{tag}.prompt.txt"),
            "relation_review_response": str(review_dir / f"{tag}.response.txt"),
            "relation_review_receipt": str(immutable_relation_receipt_path),
            "problem_mining_evidence_receipt": str(evidence_receipt_path),
        }
    )
    updated_doc["artifacts"] = artifact_refs
    updated_doc = merge_stage_model_invocation_contract(
        updated_doc,
        manifest_refs=[
            *relation_invocation_tracker.collect(),
            *(relation_manifest_refs or []),
        ],
        invocation_expected=bool(problem_records),
    )

    out_json.write_text(
        _json.dumps(updated_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    title = out_json.stem.removesuffix(".problem_records") or "Problem Records"
    out_md.write_text(
        _render_problem_records_markdown(
            canonical_records,
            title=f"{title} - Canonical Problem Cases",
        ),
        encoding="utf-8",
    )
    return updated_doc, canonical_records, updated_atoms, registry


def continue_problem_relation_review_from_independent_feedback(
    *,
    stage_doc: dict[str, Any],
    atoms: list[dict[str, Any]],
    feedback: Mapping[str, Any],
    author_provenance: Mapping[str, Any],
    pipeline_manifest: PipelinePromptManifest,
    stage_guidance_text: str,
    artifacts_dir: Path,
    out_json: Path,
    out_md: Path,
    case_registry_path: Path,
    previous_case_registry: dict[str, Any],
    repo_root: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    prior_correction_attempts: list[dict[str, Any]] | None = None,
    prior_correction_errors: list[str] | None = None,
    correction_attempt_number: int = 1,
) -> dict[str, Any]:
    """Resume the exact relation-review batch and rematerialize its case graph."""

    meta_raw = stage_doc.get("input_meta")
    meta = dict(meta_raw) if isinstance(meta_raw, Mapping) else {}
    pre_relation_raw = meta.get("pre_relation_problem_records")
    pre_relation_records = (
        [dict(item) for item in pre_relation_raw if isinstance(item, Mapping)]
        if isinstance(pre_relation_raw, list)
        else []
    )
    batches_raw = meta.get("relation_review_batches")
    batches = (
        [dict(item) for item in batches_raw if isinstance(item, Mapping)]
        if isinstance(batches_raw, list)
        else []
    )
    batch_tag = _coerce_string(author_provenance.get("relation_review_batch_tag"))
    batch = next(
        (item for item in batches if _coerce_string(item.get("tag")) == batch_tag),
        None,
    )
    if not pre_relation_records or batch is None:
        return {
            "status": "repairable_paused:relation_review_source_frontier_unavailable",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": [
                "stage1_relation_review_pre_relation_frontier_unavailable"
            ],
        }
    attempts_raw = batch.get("attempt_history")
    attempts = (
        [dict(item) for item in attempts_raw if isinstance(item, Mapping)]
        if isinstance(attempts_raw, list)
        else []
    )
    retained = next(
        (item for item in reversed(attempts) if item.get("status") == "verified"),
        attempts[-1] if attempts else None,
    )
    expected_session = _coerce_string(author_provenance.get("agent_session_id"))
    expected_workspace_raw = _coerce_string(author_provenance.get("workspace_dir"))
    if (
        retained is None
        or expected_session is None
        or expected_workspace_raw is None
        or _coerce_string(retained.get("agent_session_id")) != expected_session
        or _coerce_string(retained.get("workspace_dir")) != expected_workspace_raw
    ):
        return {
            "status": "repairable_paused:relation_review_exact_author_unavailable",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": ["stage1_relation_review_exact_author_unavailable"],
        }
    expected_workspace = Path(expected_workspace_raw).resolve()
    focus_ids = set(_coerce_string_list(batch.get("focus_ids")))
    known_problem_ids = {
        value
        for record in pre_relation_records
        for value in [
            _coerce_string(record.get("problem_id")),
            *_coerce_string_list(record.get("case_member_problem_ids")),
        ]
        if value is not None
    }
    known_evidence_ids = {
        value
        for record in pre_relation_records
        for value in _coerce_string_list(record.get("evidence_atom_ids"))
    }
    existing_decisions_raw = meta.get("relation_review_decisions")
    existing_decisions = (
        [dict(item) for item in existing_decisions_raw if isinstance(item, Mapping)]
        if isinstance(existing_decisions_raw, list)
        else []
    )
    if not existing_decisions:
        response_path_raw = _coerce_string(
            stage_doc.get("artifacts", {}).get("relation_review_response")
            if isinstance(stage_doc.get("artifacts"), Mapping)
            else None
        )
        try:
            response_loaded = json.loads(
                Path(response_path_raw or "").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            response_loaded = None
        if isinstance(response_loaded, list):
            existing_decisions = [
                dict(item) for item in response_loaded if isinstance(item, Mapping)
            ]
    prompt = (
        "INDEPENDENT QUALIFICATION CORRECTION FOR STAGE-1 RELATION REVIEW\n\n"
        "Continue the exact relation-review author session and workspace. Revise the "
        "complete decisions for this retained batch in response to every bound finding. "
        "Preserve unrelated valid focus decisions. Return one JSON list and no prose. "
        "Do not self-certify resolution; fresh independent adjudication remains required.\n\n"
        "BOUND FEEDBACK:\n"
        + json.dumps(feedback, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n\nBATCH FOCUS IDS:\n"
        + json.dumps(sorted(focus_ids), ensure_ascii=False, indent=2)
        + "\n\nCURRENT BATCH DECISIONS:\n"
        + json.dumps(
            [
                item
                for item in existing_decisions
                if _coerce_string(item.get("focus_id")) in focus_ids
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n\nPRIOR CORRECTION VALIDATION ERRORS:\n"
        + json.dumps(prior_correction_errors or [], ensure_ascii=False, indent=2)
    )
    tag = (
        "qualification_relation_"
        + sha256(str(feedback.get("content_sha256")).encode()).hexdigest()[:12]
        + f"_{max(1, correction_attempt_number):03d}"
    )
    run = run_stage_prompt_json(
        stage="problem_mining",
        prompt=prompt,
        out_dir=artifacts_dir,
        tag=tag,
        agent=agent,
        model=model,
        cfg=cfg,
        workspace_dir=expected_workspace,
        resume_session_id=expected_session,
        allow_empty=True,
        structured=True,
    )
    if isinstance(run, str):
        response = run
        actual_session = None
        actual_workspace = expected_workspace
        elapsed = 0.0
        invocation_path = None
    else:
        response = str(run.response)
        actual_session = _coerce_string(run.agent_session_id)
        actual_workspace = Path(run.workspace_dir or expected_workspace).resolve()
        elapsed = max(0.0, float(run.elapsed_seconds))
        invocation_path = Path(run.invocation_manifest_path)
    errors: list[str] = []
    decisions: list[dict[str, Any]] = []
    try:
        parsed = json.loads(response)
        if not isinstance(parsed, list) or not all(
            isinstance(item, dict) for item in parsed
        ):
            raise ValueError("relation_correction_response_not_list")
        decisions = [dict(item) for item in parsed]
        validation_errors, _ = _relation_review_response_errors(
            decisions,
            focus_problem_ids=focus_ids,
            known_problem_ids=known_problem_ids,
            known_evidence_atom_ids=known_evidence_ids,
            allowed_actions={
                "merge",
                "alias",
                "split",
                "same_cause_group",
                "keep_separate",
            },
        )
        errors.extend(validation_errors)
    except Exception as exc:  # noqa: BLE001 - exact validator feedback
        errors.append(f"{type(exc).__name__}: {exc}")
    if actual_session != expected_session:
        errors.append("stage1_relation_review_exact_session_changed")
    if actual_workspace != expected_workspace:
        errors.append("stage1_relation_review_workspace_changed")
    attempt_record = {
        "attempt_number": len(attempts) + len(prior_correction_attempts or []) + 1,
        "tag": tag,
        "status": "verified" if not errors else "invalid",
        "agent_session_id": actual_session,
        "workspace_dir": str(actual_workspace),
        "elapsed_seconds": elapsed,
        "validation_errors": list(errors),
    }
    if errors:
        return {
            "status": "repairable_paused:relation_review_correction_invalid",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": errors,
            "attempt_record": attempt_record,
            "agent_session_id": actual_session,
            "workspace_dir": str(actual_workspace),
        }
    corrected_by_focus = {
        str(item["focus_id"]): item
        for item in decisions
        if _coerce_string(item.get("focus_id")) is not None
    }
    merged_decisions = [
        item
        for item in existing_decisions
        if _coerce_string(item.get("focus_id")) not in focus_ids
    ] + [corrected_by_focus[focus_id] for focus_id in sorted(focus_ids)]
    corrected_batches = [dict(item) for item in batches]
    for corrected_batch in corrected_batches:
        if _coerce_string(corrected_batch.get("tag")) != batch_tag:
            continue
        corrected_batch["status"] = "qualification_corrected"
        corrected_batch["attempt_history"] = [
            *attempts,
            *[dict(item) for item in (prior_correction_attempts or [])],
            attempt_record,
        ]
    draft_raw = meta.get("problem_mining_evidence_draft")
    if not isinstance(draft_raw, dict):
        receipt_ref = meta.get("problem_mining_evidence_receipt")
        receipt_path_raw = (
            _coerce_string(receipt_ref.get("path"))
            if isinstance(receipt_ref, Mapping)
            else None
        )
        try:
            draft_loaded = json.loads(
                Path(receipt_path_raw or "").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            draft_loaded = None
        draft_raw = draft_loaded if isinstance(draft_loaded, dict) else None
    if not isinstance(draft_raw, dict):
        return {
            "status": "repairable_paused:relation_review_evidence_receipt_unavailable",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": ["stage1_relation_review_evidence_receipt_unavailable"],
            "attempt_record": attempt_record,
            "agent_session_id": actual_session,
            "workspace_dir": str(actual_workspace),
        }
    patched = dict(stage_doc)
    patched["items"] = pre_relation_records
    patched_meta = dict(meta)
    patched_meta["problem_mining_evidence_draft"] = json.loads(
        json.dumps(draft_raw, ensure_ascii=False)
    )
    patched["input_meta"] = patched_meta
    manifest_refs = (
        [model_invocation_manifest_ref(invocation_path)]
        if invocation_path is not None
        else []
    )
    updated, records, updated_atoms, registry = _run_problem_case_relation_review(
        stage_doc=patched,
        problem_records=pre_relation_records,
        atoms=atoms,
        pipeline_manifest=pipeline_manifest,
        artifacts_dir=artifacts_dir,
        out_json=out_json,
        out_md=out_md,
        case_registry_path=case_registry_path,
        previous_case_registry=previous_case_registry,
        agent=agent,
        model=model,
        cfg=cfg,
        dry_run=False,
        stage_guidance_text=stage_guidance_text,
        relation_decisions_override=merged_decisions,
        relation_review_batches_override=corrected_batches,
        relation_manifest_refs=manifest_refs,
    )
    return {
        "status": "corrected",
        "stage_doc": updated,
        "problem_records": records,
        "atoms": updated_atoms,
        "case_registry": registry,
        "validation_errors": [],
        "attempt_record": attempt_record,
        "agent_session_id": actual_session,
        "workspace_dir": str(actual_workspace),
    }


def continue_problem_mining_from_independent_feedback(
    *,
    stage_doc: dict[str, Any],
    atoms: list[dict[str, Any]],
    actionable_atom_ids: list[str],
    feedback: Mapping[str, Any],
    pipeline_manifest: PipelinePromptManifest,
    stage_guidance_text: str,
    artifacts_dir: Path,
    out_json: Path,
    out_md: Path,
    case_registry_path: Path,
    previous_case_registry: dict[str, Any],
    repo_root: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    prior_correction_attempts: list[dict[str, Any]] | None = None,
    prior_correction_errors: list[str] | None = None,
    correction_attempt_number: int = 1,
    author_component: str | None = None,
    author_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resume the exact Stage 1 assignment reviewer for a miss or bad mined claim."""

    meta_raw = stage_doc.get("input_meta")
    meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    draft_raw = meta.get("problem_mining_evidence_draft")
    if not isinstance(draft_raw, dict):
        receipt_ref_raw = meta.get("problem_mining_evidence_receipt")
        receipt_path_raw = (
            _coerce_string(receipt_ref_raw.get("path"))
            if isinstance(receipt_ref_raw, dict)
            else None
        )
        if receipt_path_raw is not None:
            try:
                loaded_receipt = json.loads(
                    Path(receipt_path_raw).read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
                loaded_receipt = None
            if isinstance(loaded_receipt, dict):
                draft_raw = loaded_receipt
    miners_raw = meta.get("miner_results")
    miners = (
        [dict(item) for item in miners_raw if isinstance(item, dict)]
        if isinstance(miners_raw, list)
        else []
    )
    target_atoms = {item for item in actionable_atom_ids if isinstance(item, str) and item}
    expected_miner_tag = (
        _coerce_string(author_provenance.get("miner_tag"))
        if isinstance(author_provenance, Mapping)
        else None
    )
    miner = next(
        (
            item
            for item in miners
            if expected_miner_tag is None
            or _coerce_string(item.get("tag")) == expected_miner_tag
            if target_atoms.intersection(
                set(_coerce_string_list(item.get("assigned_atom_ids")))
            )
        ),
        None,
    )
    if miner is None:
        overlapping_assignment_exists = any(
            target_atoms.intersection(
                set(_coerce_string_list(item.get("assigned_atom_ids")))
            )
            for item in miners
        )
        return {
            "status": (
                "repairable_paused:stage1_author_binding_mismatch"
                if expected_miner_tag is not None and overlapping_assignment_exists
                else "repairable_paused:stage1_assignment_not_found"
            ),
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": [
                (
                    "stage1_author_miner_tag_binding_mismatch"
                    if expected_miner_tag is not None and overlapping_assignment_exists
                    else "stage1_actionable_assignment_not_found"
                )
            ],
        }
    if not isinstance(draft_raw, dict):
        return {
            "status": "repairable_paused:stage1_composite_receipt_unavailable",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": ["stage1_composite_evidence_receipt_unavailable"],
        }
    original_tag = str(miner.get("tag") or "")
    source_receipts_raw = draft_raw.get("miners")
    source_receipts = (
        [dict(item) for item in source_receipts_raw if isinstance(item, Mapping)]
        if isinstance(source_receipts_raw, list)
        else []
    )
    source_composite_receipt = next(
        (
            item
            for item in source_receipts
            if _coerce_string(item.get("tag")) == original_tag
        ),
        None,
    )
    if not isinstance(source_composite_receipt, dict):
        return {
            "status": "repairable_paused:stage1_composite_receipt_unavailable",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": ["stage1_composite_miner_receipt_unavailable"],
        }
    review_attempts_raw = miner.get("coverage_depth_review_attempt_history")
    primary_attempts_raw = miner.get("attempt_history")
    if author_component not in {None, "problem_miner", "coverage_review"}:
        return {
            "status": "repairable_paused:stage1_author_component_invalid",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": [
                f"stage1_author_component_invalid:{author_component}"
            ],
        }
    if author_component == "problem_miner":
        attempts_raw = primary_attempts_raw
    elif author_component == "coverage_review":
        attempts_raw = review_attempts_raw
    else:
        attempts_raw = (
            review_attempts_raw
            if isinstance(review_attempts_raw, list) and review_attempts_raw
            else primary_attempts_raw
        )
    attempts = (
        [dict(item) for item in attempts_raw if isinstance(item, dict)]
        if isinstance(attempts_raw, list)
        else []
    )
    correction_attempts = [
        dict(item)
        for item in (prior_correction_attempts or [])
        if isinstance(item, dict)
    ]
    retained = next(
        (item for item in reversed(attempts) if item.get("status") == "verified"),
        attempts[-1] if attempts else None,
    )
    if retained is None:
        return {
            "status": "repairable_paused:stage1_author_attempt_missing",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": ["stage1_author_attempt_missing"],
        }
    session_id = _coerce_string(retained.get("agent_session_id"))
    workspace_raw = _coerce_string(retained.get("workspace_dir"))
    assigned_atom_ids = _coerce_string_list(miner.get("assigned_atom_ids"))
    if session_id is None or workspace_raw is None:
        return {
            "status": "repairable_paused:stage1_author_continuation_unavailable",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": ["stage1_author_session_or_workspace_missing"],
        }
    expected_session_id = (
        _coerce_string(author_provenance.get("agent_session_id"))
        if isinstance(author_provenance, Mapping)
        else None
    )
    expected_workspace_raw = (
        _coerce_string(author_provenance.get("workspace_dir"))
        if isinstance(author_provenance, Mapping)
        else None
    )
    binding_errors: list[str] = []
    if expected_miner_tag is not None and original_tag != expected_miner_tag:
        binding_errors.append("stage1_author_miner_tag_binding_mismatch")
    if expected_session_id is not None and session_id != expected_session_id:
        binding_errors.append("stage1_author_session_binding_mismatch")
    if (
        expected_workspace_raw is not None
        and Path(workspace_raw).resolve() != Path(expected_workspace_raw).resolve()
    ):
        binding_errors.append("stage1_author_workspace_binding_mismatch")
    if binding_errors:
        return {
            "status": "repairable_paused:stage1_author_binding_mismatch",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": binding_errors,
            "agent_session_id": session_id,
            "workspace_dir": workspace_raw,
        }
    workspace = Path(workspace_raw).resolve()
    manifest_path = workspace / "atoms.json"
    try:
        manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "status": "repairable_paused:stage1_workspace_manifest_unavailable",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": [
                f"stage1_workspace_manifest_unavailable:{type(exc).__name__}"
            ],
        }
    if not isinstance(manifest_raw, dict):
        raise ValueError("stage1_workspace_manifest_invalid")
    manifest = dict(manifest_raw)
    expected_manifest = _coerce_string(retained.get("workspace_manifest_sha256"))
    observed_manifest = _problem_mining_attempt_manifest_sha256(manifest)
    if expected_manifest is not None and observed_manifest != expected_manifest:
        raise ValueError("stage1_workspace_manifest_changed_before_qualification_repair")
    atoms_by_id = {
        str(atom["atom_id"]): atom
        for atom in atoms
        if isinstance(atom.get("atom_id"), str)
    }
    assigned_atoms = [atoms_by_id[item] for item in assigned_atom_ids if item in atoms_by_id]
    assignment_atom_ids = set(assigned_atom_ids)
    prior_records = [
        dict(item)
        for item in stage_doc.get("items", [])
        if isinstance(item, dict)
        and assignment_atom_ids.intersection(
            set(_coerce_string_list(item.get("evidence_atom_ids")))
        )
    ]
    feedback_kind = _coerce_string(feedback.get("feedback_kind"))
    correction_prompt = (
        "INDEPENDENT QUALIFICATION CORRECTION FOR STAGE 1\n\n"
        "Continue this exact coverage/depth-review conversation in its retained evidence "
        "workspace. The independent post-run adjudicator found either a missed source group "
        "or a mined claim that is shallow, unsupported, or incorrectly retained. Re-read "
        "retained atom files as needed. It is valid to support a corrected case, retract the "
        "bad case with an evidence-backed non-support disposition, or leave genuinely "
        "unresolved evidence unresolved. Return the complete "
        "strict problem-mining response for every atom in this assignment, preserving valid "
        "unrelated decisions and correcting every bound finding. This turn does not "
        "self-certify success; "
        "a new independent adjudication remains authoritative.\n\nBOUND FEEDBACK:\n"
        + json.dumps(feedback, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n\nBOUND TARGET ATOM IDS:\n"
        + json.dumps(sorted(target_atoms), ensure_ascii=False, indent=2)
        + "\n\nCURRENT RECORDS FROM THIS ASSIGNMENT:\n"
        + json.dumps(prior_records, ensure_ascii=False, indent=2)
        + "\n\nPRIOR QUALIFICATION-CORRECTION VALIDATION ERRORS:\n"
        + json.dumps(prior_correction_errors or [], ensure_ascii=False, indent=2)
    )
    tag = (
        f"qualification_stage1_{sha256(str(feedback.get('content_sha256')).encode()).hexdigest()[:12]}"
        f"_{max(1, correction_attempt_number):03d}"
    )
    result = _run_problem_mining_attempt(
        repo_root=repo_root,
        stage_artifacts_dir=artifacts_dir,
        base_tag=str(miner.get("tag") or "problem_mining_qualification"),
        attempt_tag=tag,
        attempt_number=len(attempts) + len(correction_attempts) + 1,
        prompt=correction_prompt,
        prompt_atoms=assigned_atoms,
        assigned_atom_ids=assigned_atom_ids,
        max_records_per_miner=max(1, len(assigned_atom_ids)),
        eligible_atom_ids=_coerce_string_list(
            draft_raw.get("eligible_atom_ids")
            if isinstance(draft_raw, dict)
            else []
        ),
        template_name=str(miner.get("template") or "problem_miner_default.md"),
        record_contract_error_prefix="qualification_stage1_contract_invalid",
        agent=agent,
        model=model,
        cfg=cfg,
        initial_workspace_dir=workspace,
        initial_manifest=manifest,
        expected_manifest_sha256=observed_manifest,
        resume_session_id=session_id,
    )
    failure = result.get("failure")
    if isinstance(failure, Exception):
        failed_attempt = result.get("attempt_record")
        failed_attempt_map = (
            failed_attempt if isinstance(failed_attempt, Mapping) else {}
        )
        return {
            "status": "repairable_paused:stage1_correction_invalid",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": list(result.get("validation_errors") or [str(failure)]),
            "attempt_record": result.get("attempt_record"),
            "agent_session_id": _coerce_string(
                failed_attempt_map.get("agent_session_id")
            )
            or session_id,
            "workspace_dir": _coerce_string(
                failed_attempt_map.get("workspace_dir")
            )
            or str(workspace),
        }
    envelope = result.get("envelope")
    decisions = (
        [dict(item) for item in envelope.get("atom_decisions", []) if isinstance(item, dict)]
        if isinstance(envelope, dict)
        else []
    )
    supporting_targets = {
        _coerce_string(decision.get("atom_id"))
        for decision in decisions
        if decision.get("disposition") == "supports_case"
    } & target_atoms
    if feedback_kind == "false_rejection" and not supporting_targets:
        incomplete_attempt = result.get("attempt_record")
        incomplete_attempt_map = (
            incomplete_attempt if isinstance(incomplete_attempt, Mapping) else {}
        )
        return {
            "status": "repairable_paused:stage1_false_rejection_not_corrected",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": ["stage1_actionable_group_still_not_supported"],
            "attempt_record": result.get("attempt_record"),
            "agent_session_id": _coerce_string(
                incomplete_attempt_map.get("agent_session_id")
            )
            or session_id,
            "workspace_dir": _coerce_string(
                incomplete_attempt_map.get("workspace_dir")
            )
            or str(workspace),
        }
    direct_records = [
        dict(item) for item in result.get("records", []) if isinstance(item, dict)
    ]
    direct_receipt = dict(result["receipt"])
    direct_decisions = [
        dict(item)
        for item in direct_receipt.get("atom_decisions", [])
        if isinstance(item, Mapping)
    ]
    complete_attempt_history = [
        *attempts,
        *correction_attempts,
        dict(result["attempt_record"]),
    ]
    eligible_atom_ids = _coerce_string_list(draft_raw.get("eligible_atom_ids"))
    primary_attempt_history = [
        dict(item) for item in primary_attempts_raw if isinstance(item, Mapping)
    ] if isinstance(primary_attempts_raw, list) else []
    review_attempt_history = [
        dict(item) for item in review_attempts_raw if isinstance(item, Mapping)
    ] if isinstance(review_attempts_raw, list) else []
    dependent_review_run: dict[str, Any] | None = None

    if attempts_raw is primary_attempts_raw:
        primary_records = direct_records
        primary_decisions = direct_decisions
        primary_receipt = direct_receipt
        primary_receipt["attempt_history"] = complete_attempt_history
        primary_normalized_raw = _coerce_string(
            primary_receipt.get("normalized_events_path")
        )
        primary_normalized_path = (
            Path(primary_normalized_raw)
            if primary_normalized_raw is not None
            else None
        )
        retained_review = next(
            (
                item
                for item in reversed(review_attempt_history)
                if item.get("status") == "verified"
            ),
            review_attempt_history[-1] if review_attempt_history else None,
        )
        review_session = (
            _coerce_string(retained_review.get("agent_session_id"))
            if isinstance(retained_review, Mapping)
            else None
        )
        review_workspace_raw = (
            _coerce_string(retained_review.get("workspace_dir"))
            if isinstance(retained_review, Mapping)
            else None
        )
        if (
            primary_normalized_path is None
            or not primary_normalized_path.is_file()
            or review_session is None
            or review_workspace_raw is None
        ):
            return {
                "status": "repairable_paused:stage1_coverage_rereview_frontier_unavailable",
                "stage_doc": stage_doc,
                "atoms": atoms,
                "validation_errors": [
                    "stage1_primary_correction_requires_retained_coverage_reviewer"
                ],
                "attempt_record": result.get("attempt_record"),
                "agent_session_id": session_id,
                "workspace_dir": str(workspace),
            }
        review_workspace = Path(review_workspace_raw).resolve()
        try:
            review_manifest_raw = json.loads(
                (review_workspace / "atoms.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {
                "status": "repairable_paused:stage1_coverage_rereview_manifest_unavailable",
                "stage_doc": stage_doc,
                "atoms": atoms,
                "validation_errors": [
                    "stage1_coverage_rereview_manifest_unavailable:"
                    + type(exc).__name__
                ],
                "attempt_record": result.get("attempt_record"),
                "agent_session_id": session_id,
                "workspace_dir": str(workspace),
            }
        if not isinstance(review_manifest_raw, dict):
            raise ValueError("stage1_coverage_rereview_manifest_invalid")
        template_name = str(miner.get("template") or "problem_miner_default.md")
        template_paths = list(
            getattr(pipeline_manifest, "problem_miner_templates", ()) or ()
        )
        template_path = next(
            (path for path in template_paths if Path(path).name == template_name),
            None,
        )
        template_text = (
            Path(template_path).read_text(encoding="utf-8")
            if template_path is not None and Path(template_path).is_file()
            else (
                "{{STAGE_GUIDANCE}}\nRead the retained evidence workspace named by "
                "{{ATOMS_JSON}} and return the strict problem-mining response."
            )
        )
        review_prompt = _coverage_depth_review_prompt(
            primary_records=primary_records,
            primary_decisions=primary_decisions,
            template_text=template_text,
            stage_guidance_text=stage_guidance_text,
            atoms_placeholder=json.dumps(
                {"atoms_file": "atoms.json"},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            max_records_per_miner=max(1, len(assigned_atom_ids)),
            bound_feedback=feedback,
        )
        dependent_review_run = _run_problem_mining_job_with_response_retry(
            repo_root=repo_root,
            stage_artifacts_dir=artifacts_dir,
            base_tag=f"{original_tag}_coverage_depth_review",
            prompt=review_prompt,
            prompt_atoms=assigned_atoms,
            assigned_atom_ids=assigned_atom_ids,
            max_records_per_miner=max(1, len(assigned_atom_ids)),
            eligible_atom_ids=eligible_atom_ids,
            template_name=f"adversarial:{template_name}",
            record_contract_error_prefix=(
                "qualification_stage1_coverage_rereview_contract_invalid"
            ),
            agent=agent,
            model=model,
            cfg=cfg,
            initial_workspace_dir=review_workspace,
            initial_manifest=dict(review_manifest_raw),
            resume_session_id=review_session,
            attempt_number_base=len(review_attempt_history),
        )
        dependent_failure = dependent_review_run.get("failure")
        if isinstance(dependent_failure, Exception):
            return {
                "status": "repairable_paused:stage1_coverage_rereview_invalid",
                "stage_doc": stage_doc,
                "atoms": atoms,
                "validation_errors": list(
                    dependent_review_run.get("validation_errors")
                    or [str(dependent_failure)]
                ),
                "attempt_record": {
                    **dict(result["attempt_record"]),
                    "dependent_coverage_review_attempt_history": list(
                        dependent_review_run.get("attempt_history") or []
                    ),
                },
                "agent_session_id": session_id,
                "workspace_dir": str(workspace),
            }
        review_records = [
            dict(item)
            for item in dependent_review_run.get("records", [])
            if isinstance(item, Mapping)
        ]
        review_receipt = dict(dependent_review_run["receipt"])
        new_review_attempts = [
            dict(item)
            for item in dependent_review_run.get("attempt_history", [])
            if isinstance(item, Mapping)
        ]
        review_attempt_history = [*review_attempt_history, *new_review_attempts]
        review_receipt["attempt_history"] = review_attempt_history
        review_decisions = [
            dict(item)
            for item in review_receipt.get("atom_decisions", [])
            if isinstance(item, Mapping)
        ]
        primary_workspace = workspace
        primary_manifest = manifest
    else:
        primary_records_raw = source_composite_receipt.get(
            "primary_problem_records"
        )
        primary_decisions_raw = source_composite_receipt.get(
            "primary_atom_decisions"
        )
        primary_receipt_raw = source_composite_receipt.get("primary_pass")
        if not (
            isinstance(primary_records_raw, list)
            and isinstance(primary_decisions_raw, list)
            and isinstance(primary_receipt_raw, Mapping)
        ):
            return {
                "status": "repairable_paused:stage1_primary_frontier_unavailable",
                "stage_doc": stage_doc,
                "atoms": atoms,
                "validation_errors": [
                    "stage1_coverage_correction_primary_component_unavailable"
                ],
                "attempt_record": result.get("attempt_record"),
                "agent_session_id": session_id,
                "workspace_dir": str(workspace),
            }
        primary_records = [
            dict(item) for item in primary_records_raw if isinstance(item, Mapping)
        ]
        primary_decisions = [
            dict(item) for item in primary_decisions_raw if isinstance(item, Mapping)
        ]
        primary_receipt = dict(primary_receipt_raw)
        primary_workspace_raw = _coerce_string(primary_receipt.get("workspace_dir"))
        primary_normalized_raw = _coerce_string(
            primary_receipt.get("normalized_events_path")
        )
        if primary_workspace_raw is None or primary_normalized_raw is None:
            return {
                "status": "repairable_paused:stage1_primary_frontier_unavailable",
                "stage_doc": stage_doc,
                "atoms": atoms,
                "validation_errors": [
                    "stage1_coverage_correction_primary_evidence_unavailable"
                ],
                "attempt_record": result.get("attempt_record"),
                "agent_session_id": session_id,
                "workspace_dir": str(workspace),
            }
        primary_workspace = Path(primary_workspace_raw).resolve()
        primary_normalized_path = Path(primary_normalized_raw)
        try:
            primary_manifest_raw = json.loads(
                (primary_workspace / "atoms.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {
                "status": "repairable_paused:stage1_primary_frontier_unavailable",
                "stage_doc": stage_doc,
                "atoms": atoms,
                "validation_errors": [
                    "stage1_coverage_correction_primary_manifest_unavailable:"
                    + type(exc).__name__
                ],
                "attempt_record": result.get("attempt_record"),
                "agent_session_id": session_id,
                "workspace_dir": str(workspace),
            }
        if not isinstance(primary_manifest_raw, dict):
            raise ValueError("stage1_primary_component_manifest_invalid")
        primary_manifest = dict(primary_manifest_raw)
        review_records = direct_records
        review_decisions = direct_decisions
        review_receipt = direct_receipt
        review_receipt["attempt_history"] = complete_attempt_history
        review_attempt_history = complete_attempt_history

    replacement, candidate_records, candidate_decisions = (
        _build_composite_miner_receipt(
            tag=original_tag,
            template_name=str(
                miner.get("template") or "problem_miner_default.md"
            ),
            assigned_atom_ids=assigned_atom_ids,
            eligible_atom_ids=eligible_atom_ids,
            primary_records=primary_records,
            primary_decisions=primary_decisions,
            primary_receipt=primary_receipt,
            review_records=review_records,
            review_decisions=review_decisions,
            review_receipt=review_receipt,
            primary_normalized_events_path=primary_normalized_path,
            primary_workspace_dir=primary_workspace,
            primary_workspace_manifest=primary_manifest,
        )
    )
    final_supporting_targets = {
        _coerce_string(decision.get("atom_id"))
        for decision in candidate_decisions
        if decision.get("disposition") == "supports_case"
    } & target_atoms
    if feedback_kind == "false_rejection" and not final_supporting_targets:
        return {
            "status": "repairable_paused:stage1_false_rejection_not_confirmed",
            "stage_doc": stage_doc,
            "atoms": atoms,
            "validation_errors": [
                "stage1_actionable_group_not_confirmed_by_independent_review"
            ],
            "attempt_record": {
                **dict(result["attempt_record"]),
                "dependent_coverage_review_attempt_history": (
                    list(dependent_review_run.get("attempt_history") or [])
                    if isinstance(dependent_review_run, Mapping)
                    else []
                ),
            },
            "agent_session_id": session_id,
            "workspace_dir": str(workspace),
        }
    assigned_case_records = assign_problem_case_ids(
        candidate_records,
        atoms,
        case_registry=previous_case_registry,
        strict_new_output=True,
    )
    existing_records = [
        dict(item)
        for item in stage_doc.get("items", [])
        if isinstance(item, dict)
        and not assignment_atom_ids.intersection(
            set(_coerce_string_list(item.get("evidence_atom_ids")))
        )
    ]
    combined_records = [*existing_records, *assigned_case_records]
    draft = json.loads(json.dumps(draft_raw, ensure_ascii=False))
    receipts_raw = draft.get("miners")
    receipts = (
        [dict(item) for item in receipts_raw if isinstance(item, dict)]
        if isinstance(receipts_raw, list)
        else []
    )
    replacement["attempt_history"] = (
        complete_attempt_history
        if attempts_raw is primary_attempts_raw
        else primary_attempt_history
    )
    receipts = [
        replacement if _coerce_string(receipt.get("tag")) == original_tag else receipt
        for receipt in receipts
    ]
    if not any(_coerce_string(receipt.get("tag")) == original_tag for receipt in receipts):
        receipts.append(replacement)
    draft["miners"] = receipts
    patched = dict(stage_doc)
    patched["items"] = combined_records
    patched_meta = dict(meta)
    updated_miner_result = dict(miner)
    if attempts_raw is review_attempts_raw:
        updated_miner_result["coverage_depth_review_attempt_history"] = (
            complete_attempt_history
        )
    else:
        updated_miner_result["attempt_history"] = complete_attempt_history
        updated_miner_result["coverage_depth_review_attempt_history"] = (
            review_attempt_history
        )
    updated_miner_result["status"] = "qualification_corrected"
    patched_meta["miner_results"] = [
        updated_miner_result
        if _coerce_string(item.get("tag")) == original_tag
        else item
        for item in miners
    ]
    patched_meta["problem_mining_evidence_draft"] = draft
    correction_attempt_record = {
        **dict(result["attempt_record"]),
        "dependent_coverage_review_attempt_history": (
            list(dependent_review_run.get("attempt_history") or [])
            if isinstance(dependent_review_run, Mapping)
            else []
        ),
    }
    patched_meta["qualification_stage1_correction"] = {
        "feedback_sha256": feedback.get("content_sha256"),
        "author_session_id": session_id,
        "workspace_dir": str(workspace),
        "target_atom_ids": sorted(target_atoms),
        "feedback_kind": feedback_kind,
        "replaced_assignment_atom_ids": sorted(assignment_atom_ids),
        "attempt_record": correction_attempt_record,
    }
    patched["input_meta"] = patched_meta
    invocation_raw = result.get("attempt_record", {}).get("artifacts")
    invocation_ref_raw = (
        invocation_raw.get("model_invocation")
        if isinstance(invocation_raw, dict)
        else None
    )
    invocation_path = (
        Path(invocation_ref_raw["path"])
        if isinstance(invocation_ref_raw, dict)
        and isinstance(invocation_ref_raw.get("path"), str)
        else None
    )
    if invocation_path is not None:
        patched = merge_stage_model_invocation_contract(
            patched,
            manifest_refs=[model_invocation_manifest_ref(invocation_path)],
            invocation_expected=True,
        )
    updated, records, updated_atoms, registry = _run_problem_case_relation_review(
        stage_doc=patched,
        problem_records=combined_records,
        atoms=atoms,
        pipeline_manifest=pipeline_manifest,
        artifacts_dir=artifacts_dir,
        out_json=out_json,
        out_md=out_md,
        case_registry_path=case_registry_path,
        previous_case_registry=previous_case_registry,
        agent=agent,
        model=model,
        cfg=cfg,
        dry_run=False,
        stage_guidance_text=stage_guidance_text,
    )
    return {
        "status": "corrected",
        "stage_doc": updated,
        "problem_records": records,
        "atoms": updated_atoms,
        "case_registry": registry,
        "validation_errors": [],
        "attempt_record": correction_attempt_record,
        "agent_session_id": _coerce_string(
            result.get("attempt_record", {}).get("agent_session_id")
        )
        or session_id,
        "workspace_dir": str(workspace),
    }


__all__ = [name for name in globals() if not name.startswith("__")]
