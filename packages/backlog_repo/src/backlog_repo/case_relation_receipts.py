from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CASE_RELATION_RECEIPT_SCHEMA_VERSION = 1
CASE_RELATION_RECEIPT_PRODUCER = "usertest_backlog"
CASE_RELATION_RECEIPT_KIND = "canonical_case_relations"
CASE_RELATION_DIRECTIONS = frozenset({"source_to_canonical"})
CASE_RELATION_KINDS = frozenset({"canonical_absorption", "supersession"})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def canonical_case_relation_sha256(value: Any) -> str:
    """Return the canonical digest used by relation receipt content and entries."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"case_relation_receipt_field_invalid:{field}")
    return value.strip()


def _string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"case_relation_receipt_field_invalid:{field}")
    cleaned = [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    ]
    if len(cleaned) != len(value) or len(cleaned) != len(set(cleaned)):
        raise ValueError(f"case_relation_receipt_field_invalid:{field}")
    return sorted(cleaned)


def _validate_acyclic_relations(relations: Sequence[Mapping[str, Any]]) -> None:
    targets: dict[str, str] = {}
    for relation in relations:
        source = str(relation["source_case_id"])
        target = str(relation["target_case_id"])
        if source == target:
            raise ValueError(f"case_relation_receipt_self_relation:{source}")
        previous = targets.get(source)
        if previous is not None and previous != target:
            raise ValueError(
                f"case_relation_receipt_conflicting_targets:{source}:{previous}:{target}"
            )
        targets[source] = target

    for source in targets:
        seen: set[str] = set()
        cursor = source
        while cursor in targets:
            if cursor in seen:
                raise ValueError(f"case_relation_receipt_cycle:{source}")
            seen.add(cursor)
            cursor = targets[cursor]


def normalize_case_relation(
    relation: Mapping[str, Any],
    *,
    context: str = "relation",
) -> dict[str, Any]:
    source = _required_string(relation.get("source_case_id"), field=f"{context}.source_case_id")
    target = _required_string(relation.get("target_case_id"), field=f"{context}.target_case_id")
    direction = _required_string(relation.get("direction"), field=f"{context}.direction")
    if direction not in CASE_RELATION_DIRECTIONS:
        raise ValueError(f"case_relation_receipt_direction_invalid:{context}:{direction}")
    relation_kind = _required_string(
        relation.get("relation_kind"),
        field=f"{context}.relation_kind",
    )
    if relation_kind not in CASE_RELATION_KINDS:
        raise ValueError(
            f"case_relation_receipt_relation_kind_invalid:{context}:{relation_kind}"
        )
    decision_actions = _string_list(
        relation.get("decision_actions"),
        field=f"{context}.decision_actions",
    )
    if relation_kind == "canonical_absorption" and not decision_actions:
        raise ValueError(f"case_relation_receipt_decision_actions_missing:{context}")

    normalized = {
        "source_case_id": source,
        "target_case_id": target,
        "direction": direction,
        "relation_kind": relation_kind,
        "decision_actions": decision_actions,
    }
    expected_hash = canonical_case_relation_sha256(normalized)
    supplied_hash = relation.get("relation_sha256")
    if supplied_hash is not None and supplied_hash != expected_hash:
        raise ValueError(f"case_relation_receipt_relation_hash_mismatch:{context}")
    normalized["relation_sha256"] = expected_hash
    return normalized


def validate_case_relation_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a runner-authored canonical relation graph receipt."""

    if payload.get("schema_version") != CASE_RELATION_RECEIPT_SCHEMA_VERSION:
        raise ValueError("case_relation_receipt_schema_invalid")
    if payload.get("producer") != CASE_RELATION_RECEIPT_PRODUCER:
        raise ValueError("case_relation_receipt_producer_invalid")
    if payload.get("receipt_kind") != CASE_RELATION_RECEIPT_KIND:
        raise ValueError("case_relation_receipt_kind_invalid")
    stage = _required_string(payload.get("stage"), field="stage")
    response_path = _required_string(
        payload.get("relation_review_response_path"),
        field="relation_review_response_path",
    )
    response_hash = _required_string(
        payload.get("relation_review_response_sha256"),
        field="relation_review_response_sha256",
    )
    if len(response_hash) != 64 or any(
        character not in "0123456789abcdef" for character in response_hash.casefold()
    ):
        raise ValueError("case_relation_receipt_response_hash_invalid")

    raw_relations = payload.get("relations")
    if not isinstance(raw_relations, list):
        raise ValueError("case_relation_receipt_relations_invalid")
    relations = [
        normalize_case_relation(relation, context=f"relations[{index}]")
        for index, relation in enumerate(raw_relations)
        if isinstance(relation, Mapping)
    ]
    if len(relations) != len(raw_relations):
        raise ValueError("case_relation_receipt_relations_invalid")
    relations.sort(
        key=lambda item: (
            item["source_case_id"],
            item["target_case_id"],
            item["relation_kind"],
        )
    )
    if len({item["source_case_id"] for item in relations}) != len(relations):
        raise ValueError("case_relation_receipt_duplicate_source")
    _validate_acyclic_relations(relations)

    normalized_without_hash = {
        "schema_version": CASE_RELATION_RECEIPT_SCHEMA_VERSION,
        "producer": CASE_RELATION_RECEIPT_PRODUCER,
        "receipt_kind": CASE_RELATION_RECEIPT_KIND,
        "stage": stage,
        "relation_review_response_path": response_path,
        "relation_review_response_sha256": response_hash.casefold(),
        "relations": relations,
    }
    expected_content_hash = canonical_case_relation_sha256(normalized_without_hash)
    supplied_content_hash = payload.get("content_sha256")
    if supplied_content_hash != expected_content_hash:
        raise ValueError("case_relation_receipt_content_hash_mismatch")
    return {**normalized_without_hash, "content_sha256": expected_content_hash}


def write_case_relation_receipt(
    path: Path,
    *,
    stage: str,
    relation_review_response_path: Path,
    relations: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Write one immutable receipt and return exact per-source references.

    Backlog cycles reuse their rolling relation-review artifact names. The receipt
    therefore retains a content-addressed response snapshot and is itself written to
    a content-addressed path so a later cycle cannot silently replace its evidence.
    """

    rolling_response_path = relation_review_response_path.expanduser().resolve()
    if not rolling_response_path.is_file():
        raise FileNotFoundError(rolling_response_path)
    response_bytes = rolling_response_path.read_bytes()
    response_hash = hashlib.sha256(response_bytes).hexdigest()
    response_path = rolling_response_path.with_name(
        f"{rolling_response_path.stem}.snapshot-{response_hash[:16]}"
        f"{rolling_response_path.suffix}"
    )
    if response_path.exists():
        if not response_path.is_file() or response_path.read_bytes() != response_bytes:
            raise OSError(f"Immutable relation response snapshot mismatch: {response_path}")
    else:
        response_path.write_bytes(response_bytes)
    normalized_relations = [
        normalize_case_relation(relation, context=f"relations[{index}]")
        for index, relation in enumerate(relations)
    ]
    normalized_relations.sort(
        key=lambda item: (
            item["source_case_id"],
            item["target_case_id"],
            item["relation_kind"],
        )
    )
    without_hash = {
        "schema_version": CASE_RELATION_RECEIPT_SCHEMA_VERSION,
        "producer": CASE_RELATION_RECEIPT_PRODUCER,
        "receipt_kind": CASE_RELATION_RECEIPT_KIND,
        "stage": _required_string(stage, field="stage"),
        "relation_review_response_path": str(response_path),
        "relation_review_response_sha256": response_hash,
        "relations": normalized_relations,
    }
    payload = {
        **without_hash,
        "content_sha256": canonical_case_relation_sha256(without_hash),
    }
    normalized_payload = validate_case_relation_receipt(payload)

    requested_path = path.expanduser().resolve()
    receipt_path = requested_path.with_name(
        f"{requested_path.stem}.{normalized_payload['content_sha256'][:16]}"
        f"{requested_path.suffix}"
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_text = (
        json.dumps(normalized_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    if receipt_path.exists():
        if not receipt_path.is_file() or receipt_path.read_text(
            encoding="utf-8"
        ) != receipt_text:
            raise OSError(f"Immutable case relation receipt mismatch: {receipt_path}")
    else:
        receipt_path.write_text(receipt_text, encoding="utf-8")
    receipt_hash = sha256_file(receipt_path)
    references = {
        relation["source_case_id"]: {
            "schema_version": CASE_RELATION_RECEIPT_SCHEMA_VERSION,
            "producer": CASE_RELATION_RECEIPT_PRODUCER,
            "receipt_path": str(receipt_path),
            "receipt_sha256": receipt_hash,
            "relation_sha256": relation["relation_sha256"],
            "source_case_id": relation["source_case_id"],
            "target_case_id": relation["target_case_id"],
            "direction": relation["direction"],
            "relation_kind": relation["relation_kind"],
        }
        for relation in normalized_payload["relations"]
    }
    return normalized_payload, references


__all__ = [
    "CASE_RELATION_DIRECTIONS",
    "CASE_RELATION_KINDS",
    "CASE_RELATION_RECEIPT_KIND",
    "CASE_RELATION_RECEIPT_PRODUCER",
    "CASE_RELATION_RECEIPT_SCHEMA_VERSION",
    "canonical_case_relation_sha256",
    "normalize_case_relation",
    "sha256_file",
    "validate_case_relation_receipt",
    "write_case_relation_receipt",
]
