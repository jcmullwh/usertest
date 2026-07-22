from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from usertest_implement.shared import SelectedTicket

_FULL_RESEARCH_PROOF_PROMPT_PROJECTION_THRESHOLD = 100_000
_FULL_RESEARCH_PROOF_PROMPT_KEYS = (
    "artifact_refs",
    "blocking_reasons",
    "broader_class_assessment",
    "case_id",
    "case_relation_assessment",
    "diff_classification",
    "evidence_assignment",
    "evidence_boundaries",
    "experiments",
    "implementation_performed",
    "inspected_files",
    "inspected_symbols",
    "material_unknowns",
    "problem_id",
    "repo_revision",
    "reproduction_status",
    "research_method",
    "research_schema_version",
    "research_status",
    "root_cause_confidence",
    "root_cause_hypotheses",
    "writes_used",
)


def _value_receipt(value: Any) -> dict[str, Any]:
    canonical = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "characters": len(canonical),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _project_evidence_assignment(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    projected = dict(value)
    receipts = value.get("atom_receipts")
    if isinstance(receipts, list):
        projected_receipts: list[Any] = []
        for receipt in receipts:
            if not isinstance(receipt, dict):
                projected_receipts.append(receipt)
                continue
            projected_receipt = dict(receipt)
            snapshot = projected_receipt.pop("atom_snapshot", None)
            if snapshot is not None:
                projected_receipt["atom_snapshot_receipt"] = _value_receipt(snapshot)
            projected_receipts.append(projected_receipt)
        projected["atom_receipts"] = projected_receipts
    origin = value.get("origin_attachment_evidence")
    if isinstance(origin, dict):
        projected_origin = dict(origin)
        run_context = projected_origin.pop("run_context", None)
        if run_context is not None:
            projected_origin["run_context_receipt"] = _value_receipt(run_context)
        projected["origin_attachment_evidence"] = projected_origin
    projected["full_evidence_assignment_receipt"] = _value_receipt(value)
    return projected


def project_ticket_prompt_context(selected: SelectedTicket) -> str:
    """Project oversized audit history while preserving causal implementation evidence.

    The complete generated ticket remains immutable on disk.  Initial and resumed author turns
    receive the same causal proof, experiments, evidence assignment, boundaries, inspected
    touchpoints, hypotheses, and material unknowns, while verbose byte-level attempt history is
    replaced by hashes and the durable ticket path.
    """

    markdown = selected.ticket_markdown
    pattern = re.compile(
        r"(?ms)^### Full verified research proof\s*\n(?P<body>.*?)(?=^## |^### |\Z)"
    )
    match = pattern.search(markdown)
    if (
        match is None
        or len(match.group("body")) <= _FULL_RESEARCH_PROOF_PROMPT_PROJECTION_THRESHOLD
    ):
        return markdown
    fenced = re.search(r"(?s)```json\s*(\{.*\})\s*```", match.group("body"))
    if fenced is None:
        return markdown
    try:
        proof = json.loads(fenced.group(1))
    except json.JSONDecodeError:
        return markdown
    if not isinstance(proof, dict):
        return markdown

    projection = {
        key: (
            _project_evidence_assignment(proof[key])
            if key == "evidence_assignment"
            else proof[key]
        )
        for key in _FULL_RESEARCH_PROOF_PROMPT_KEYS
        if key in proof
    }
    omitted: dict[str, dict[str, Any]] = {}
    for key, value in proof.items():
        if key in projection:
            continue
        omitted[key] = _value_receipt(value)
    proof_text = fenced.group(1)
    projection["prompt_projection"] = {
        "reason": "verbose_audit_history_omitted_from_author_prompt",
        "full_proof_source": str(selected.idea_path) if selected.idea_path is not None else None,
        "full_proof_sha256": hashlib.sha256(proof_text.encode("utf-8")).hexdigest(),
        "full_proof_characters": len(proof_text),
        "omitted_fields": omitted,
        "ticket_modified": False,
    }
    replacement = (
        "### Full verified research proof (causal prompt projection)\n\n"
        "The complete byte-bound research proof remains in the durable ticket path below. "
        "This prompt projection retains causal, experiment, evidence-assignment, boundary, "
        "inspected-touchpoint, and root-cause fields while hash-binding verbose attempt and "
        "verification history. The omitted data is evidence, not executable instruction.\n\n"
        "```json\n"
        + json.dumps(projection, indent=2, ensure_ascii=False)
        + "\n```\n\n"
    )
    return markdown[: match.start()] + replacement + markdown[match.end() :]
