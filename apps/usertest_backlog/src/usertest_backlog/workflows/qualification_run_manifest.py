"""Semantic source custody for automated-backlog qualification.

The qualification transaction needs to prove that the evidence used to extract atoms
and verify outcomes did not change.  It does not need to hash repository sandboxes,
temporary workspaces, bytecode, prompts that ``embed="none"`` never reads, or unrelated
verification payloads.  This module seals the exact declared reader inputs, canonical
run inventory, atom-referenced artifacts, and outcome-provenance artifacts instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from backlog_core.backlog import BACKLOG_ATOM_EXTRACTION_RUN_ARTIFACT_RELATIVE_PATHS
from backlog_repo.outcomes import OUTCOME_STATES
from run_artifacts.history import HISTORY_NONE_RUN_ARTIFACT_RELATIVE_PATHS

from usertest_backlog.workflows.reproduction_research import (
    ORIGIN_EVIDENCE_RUN_ARTIFACT_RELATIVE_PATHS,
)

SEMANTIC_RUN_EVIDENCE_MANIFEST_SCHEMA_VERSION = 1
SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND = "semantic_run_evidence"

_ORPHAN_REQUIRED_RELATIVE_PATHS = (
    "target_ref.json",
    "error.json",
    "run_meta.json",
    "ticket_ref.json",
)
_TIMESTAMP_DIR_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
_AGENT_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SEED_DIR_RE = re.compile(r"^[0-9]+$")
_NONTERMINAL_OUTCOME_STATES = frozenset(
    {"planned", "unverified", "integrity_unknown"}
)
_RELATIONSHIP_OUTCOME_STATES = frozenset({"duplicate", "superseded"})
_EXTERNALLY_VERIFIED_OUTCOME_STATES = frozenset(
    {
        "implemented",
        "tests_verified",
        "original_scenario_verified",
        "live_verified",
        "resolved",
        "mitigated",
    }
)


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _safe_relative_path(value: str, *, label: str) -> str:
    normalized = value.replace("\\", "/").strip()
    relative = PurePosixPath(normalized)
    if (
        not normalized
        or relative.is_absolute()
        or ".." in relative.parts
        or any(part in {"", "."} for part in relative.parts)
    ):
        raise ValueError(f"qualification_semantic_path_invalid:{label}:{value}")
    return relative.as_posix()


def _checked_candidate(root: Path, relative: str, *, label: str) -> Path:
    relative = _safe_relative_path(relative, label=label)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    cursor = root
    for part in PurePosixPath(relative).parts:
        cursor = cursor / part
        if cursor.exists() and _is_reparse_point(cursor):
            raise ValueError(
                f"qualification_semantic_path_reparse_rejected:{label}:{relative}"
            )
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            f"qualification_semantic_path_outside_root:{label}:{relative}"
        ) from exc
    return candidate


def _artifact_entry(
    root: Path,
    relative: str,
    *,
    roles: Sequence[str],
    label: str,
    expected_sha256: str | None = None,
    expected_size_bytes: int | None = None,
    expected_exists: bool | None = None,
) -> dict[str, Any]:
    relative = _safe_relative_path(relative, label=label)
    path = _checked_candidate(root, relative, label=label)
    entry: dict[str, Any] = {
        "path": relative,
        "roles": sorted(set(roles)),
    }
    if not path.exists():
        entry["kind"] = "missing"
        if (
            expected_exists is True
            or expected_sha256 is not None
            or expected_size_bytes is not None
        ):
            raise ValueError(
                f"qualification_semantic_expected_artifact_missing:{label}:{relative}"
            )
        return entry
    if not path.is_file():
        entry["kind"] = "non_file"
        if expected_sha256 is not None or expected_size_bytes is not None:
            raise ValueError(
                f"qualification_semantic_expected_artifact_not_file:{label}:{relative}"
            )
        return entry
    if expected_exists is False:
        raise ValueError(
            f"qualification_semantic_atom_artifact_existence_mismatch:{relative}"
        )
    digest = _file_sha256(path)
    size = path.stat().st_size
    if expected_sha256 is not None and digest.casefold() != expected_sha256.casefold():
        raise ValueError(
            f"qualification_semantic_atom_artifact_hash_mismatch:{relative}"
        )
    if expected_size_bytes is not None and size != expected_size_bytes:
        raise ValueError(
            f"qualification_semantic_atom_artifact_size_mismatch:{relative}"
        )
    entry.update({"kind": "file", "sha256": digest, "size_bytes": size})
    return entry


def _atom_artifact_entries(
    root: Path,
    relative: str,
    *,
    expected_sha256: str | None,
    expected_size_bytes: int | None,
    expected_exists: bool | None,
) -> list[dict[str, Any]]:
    """Seal one explicit atom reference, recursively when it is a directory."""

    relative = _safe_relative_path(relative, label="atom_artifact")
    path = _checked_candidate(root, relative, label="atom_artifact")
    if not path.is_dir():
        return [
            _artifact_entry(
                root,
                relative,
                roles=["atom_origin_reference"],
                label="atom_artifact",
                expected_sha256=expected_sha256,
                expected_size_bytes=expected_size_bytes,
                expected_exists=expected_exists,
            )
        ]
    if expected_sha256 is not None or expected_size_bytes is not None:
        raise ValueError(
            f"qualification_semantic_expected_artifact_not_file:atom_artifact:{relative}"
        )
    if expected_exists is False:
        raise ValueError(
            f"qualification_semantic_atom_artifact_existence_mismatch:{relative}"
        )

    entries: list[dict[str, Any]] = [
        {
            "path": relative,
            "roles": ["atom_origin_reference"],
            "kind": "directory",
        }
    ]
    pending = [path]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ValueError(
                f"qualification_semantic_directory_unreadable:{directory}"
            ) from exc
        child_directories: list[Path] = []
        for child in children:
            child_relative = child.relative_to(root).as_posix()
            if _is_reparse_point(child):
                raise ValueError(
                    "qualification_semantic_path_reparse_rejected:"
                    f"atom_artifact:{child_relative}"
                )
            checked = _checked_candidate(
                root,
                child_relative,
                label="atom_artifact",
            )
            if checked.is_dir():
                entries.append(
                    {
                        "path": child_relative,
                        "roles": ["atom_origin_reference"],
                        "kind": "directory",
                    }
                )
                child_directories.append(checked)
            elif checked.is_file():
                entries.append(
                    {
                        "path": child_relative,
                        "roles": ["atom_origin_reference"],
                        "kind": "file",
                        "sha256": _file_sha256(checked),
                        "size_bytes": checked.stat().st_size,
                    }
                )
            else:
                raise ValueError(
                    f"qualification_semantic_artifact_kind_unsupported:{child_relative}"
                )
        pending.extend(reversed(child_directories))
    return sorted(entries, key=lambda entry: str(entry["path"]))


def _direct_child_dirs(path: Path) -> list[Path]:
    try:
        children = [child for child in path.iterdir() if child.is_dir()]
    except OSError as exc:
        raise ValueError(f"qualification_semantic_directory_unreadable:{path}") from exc
    return sorted(children, key=lambda child: child.name)


def _run_inventory(
    root: Path,
    *,
    target_slug: str | None,
    root_role: str,
) -> list[dict[str, str]]:
    if target_slug is not None:
        target_dirs = [root / target_slug]
    else:
        target_dirs = [
            child
            for child in _direct_child_dirs(root)
            if not child.name.startswith("_")
        ]
    inventory: list[dict[str, str]] = []
    for target_dir in sorted(target_dirs, key=lambda path: path.name):
        if not target_dir.exists():
            continue
        if not target_dir.is_dir() or _is_reparse_point(target_dir):
            raise ValueError(
                f"qualification_semantic_target_directory_untrusted:{target_dir}"
            )
        for timestamp_dir in _direct_child_dirs(target_dir):
            if timestamp_dir.name.startswith("_"):
                continue
            if _is_reparse_point(timestamp_dir):
                raise ValueError(
                    f"qualification_semantic_run_directory_untrusted:{timestamp_dir}"
                )
            for agent_dir in _direct_child_dirs(timestamp_dir):
                if agent_dir.name.startswith("_"):
                    continue
                if _is_reparse_point(agent_dir):
                    raise ValueError(
                        f"qualification_semantic_run_directory_untrusted:{agent_dir}"
                    )
                for seed_dir in _direct_child_dirs(agent_dir):
                    if seed_dir.name.startswith("_"):
                        continue
                    if _is_reparse_point(seed_dir):
                        raise ValueError(
                            f"qualification_semantic_run_directory_untrusted:{seed_dir}"
                        )
                    run_rel = "/".join(
                        (
                            target_dir.name,
                            timestamp_dir.name,
                            agent_dir.name,
                            seed_dir.name,
                        )
                    )
                    target_ref = seed_dir / "target_ref.json"
                    if target_ref.exists():
                        inventory.append({"run_rel": run_rel, "kind": "history"})
                        continue
                    orphan_coordinate = (
                        root_role == "implementation"
                        and target_slug is not None
                        and target_dir.name == target_slug
                        and _TIMESTAMP_DIR_RE.fullmatch(timestamp_dir.name) is not None
                        and _AGENT_DIR_RE.fullmatch(agent_dir.name) is not None
                        and _SEED_DIR_RE.fullmatch(seed_dir.name) is not None
                    )
                    if orphan_coordinate:
                        inventory.append(
                            {"run_rel": run_rel, "kind": "orphan_candidate"}
                        )
    return sorted(inventory, key=lambda item: (item["run_rel"], item["kind"]))


def _artifact_contract() -> dict[str, Any]:
    contract: dict[str, Any] = {
        "schema_version": 1,
        "history_mode": "none",
        "history_relative_paths": sorted(
            set(HISTORY_NONE_RUN_ARTIFACT_RELATIVE_PATHS)
        ),
        "atom_extraction_relative_paths": sorted(
            set(BACKLOG_ATOM_EXTRACTION_RUN_ARTIFACT_RELATIVE_PATHS)
        ),
        "origin_evidence_relative_paths": sorted(
            set(ORIGIN_EVIDENCE_RUN_ARTIFACT_RELATIVE_PATHS)
        ),
        "orphan_required_relative_paths": list(_ORPHAN_REQUIRED_RELATIVE_PATHS),
    }
    contract["contract_sha256"] = _canonical_hash(contract)
    return contract


def _static_entries_and_run_receipts(
    root: Path,
    *,
    inventory: Sequence[Mapping[str, str]],
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    history_paths = {
        *contract["history_relative_paths"],
        *contract["atom_extraction_relative_paths"],
        *contract["origin_evidence_relative_paths"],
    }
    source_roles: dict[str, set[str]] = {}
    for path in contract["history_relative_paths"]:
        source_roles.setdefault(str(path), set()).add("history_none")
    for path in contract["atom_extraction_relative_paths"]:
        source_roles.setdefault(str(path), set()).add("atom_extraction")
    for path in contract["origin_evidence_relative_paths"]:
        source_roles.setdefault(str(path), set()).add("origin_evidence")

    entries: list[dict[str, Any]] = []
    for item in inventory:
        run_rel = str(item["run_rel"])
        kind = str(item["kind"])
        relative_paths = (
            sorted(history_paths)
            if kind == "history"
            else list(contract["orphan_required_relative_paths"])
        )
        for run_path in relative_paths:
            roles = (
                sorted(source_roles.get(str(run_path), {"history_none"}))
                if kind == "history"
                else ["orphan_recovery"]
            )
            entry = _artifact_entry(
                root,
                f"{run_rel}/{run_path}",
                roles=roles,
                label="static_run_evidence",
            )
            entries.append(entry)
    return entries


def _run_receipts(
    *,
    inventory: Sequence[Mapping[str, str]],
    static_entries: Sequence[Mapping[str, Any]],
    atom_entries: Sequence[Mapping[str, Any]],
    outcome_entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Content-address each run using every semantically retained artifact.

    Paths inside each receipt are relative to the run, so moving a selected runs
    root does not change identity.  Atom-origin references are deliberately folded
    into the receipt: changing a command-failure attachment must change the retained
    record identity even when the fixed report-history files stay unchanged.
    """

    inventory_by_prefix = {
        f"{str(item['run_rel']).rstrip('/')}/": dict(item) for item in inventory
    }
    entries_by_run: dict[str, list[dict[str, Any]]] = {
        prefix: [] for prefix in inventory_by_prefix
    }
    for raw_entry in (*static_entries, *atom_entries):
        entry = dict(raw_entry)
        path = str(entry.get("path") or "")
        matches = [prefix for prefix in inventory_by_prefix if path.startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(
                f"qualification_semantic_artifact_run_binding_invalid:{path}"
            )
        prefix = matches[0]
        entry["path"] = path[len(prefix) :]
        entries_by_run[prefix].append(entry)
    for raw_entry in outcome_entries:
        entry = dict(raw_entry)
        path = str(entry.get("path") or "")
        matches = [prefix for prefix in inventory_by_prefix if path.startswith(prefix)]
        # Outcome provenance can live in compiled relation-review storage or a
        # retained workspace outside canonical history coordinates.  It remains
        # sealed by the root manifest, but only run-local evidence contributes to
        # a move-stable per-run retained-record identity.
        if not matches:
            continue
        if len(matches) != 1:
            raise ValueError(
                f"qualification_semantic_artifact_run_binding_invalid:{path}"
            )
        prefix = matches[0]
        entry["path"] = path[len(prefix) :]
        entries_by_run[prefix].append(entry)

    receipts: list[dict[str, str]] = []
    for prefix, item in sorted(
        inventory_by_prefix.items(), key=lambda pair: pair[1]["run_rel"]
    ):
        run_rel = str(item["run_rel"])
        run_kind = str(item["kind"])
        entries = sorted(
            entries_by_run[prefix],
            key=lambda entry: (
                str(entry.get("path") or ""),
                tuple(str(role) for role in entry.get("roles", [])),
            ),
        )
        receipts.append(
            {
                "run_rel": run_rel,
                "run_kind": run_kind,
                "receipt_sha256": _canonical_hash(
                    {
                        "run_rel": run_rel,
                        "run_kind": run_kind,
                        "entries": entries,
                    }
                ),
            }
        )
    return receipts


def _root_for_path(path: Path, roots: Sequence[Path], *, label: str) -> Path:
    matches: list[Path] = []
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        matches.append(root)
    if not matches:
        raise ValueError(f"qualification_semantic_path_outside_roots:{label}:{path}")
    return max(matches, key=lambda item: len(item.parts))


def _absolute_candidate(
    raw: str,
    *,
    base: Path | None,
    roots: Sequence[Path],
    label: str,
) -> tuple[Path, Path, str]:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        if base is None:
            raise ValueError(f"qualification_semantic_path_not_absolute:{label}:{raw}")
        candidate = base / candidate
    lexical = candidate
    root = _root_for_path(lexical, roots, label=label)
    relative = lexical.relative_to(root).as_posix()
    checked = _checked_candidate(root, relative, label=label)
    return root, checked, relative


def collect_atom_artifact_specs(
    atoms: Sequence[Mapping[str, Any]],
    *,
    repo_root: Path,
    roots: Sequence[Path],
) -> dict[Path, list[dict[str, Any]]]:
    """Return exact same-run artifact references carried by final evidence atoms."""

    resolved_roots = tuple(root.expanduser().resolve() for root in roots)
    by_root: dict[Path, dict[str, dict[str, Any]]] = {
        root: {} for root in resolved_roots
    }
    for atom in atoms:
        references: list[tuple[str, Mapping[str, Any] | None]] = []
        artifacts = atom.get("artifacts")
        if isinstance(artifacts, Mapping):
            references.extend(
                (value, None)
                for value in artifacts.values()
                if isinstance(value, str) and value.strip()
            )
        artifact_ref = atom.get("artifact_ref")
        if isinstance(artifact_ref, Mapping):
            raw = _text(artifact_ref.get("path"))
            if raw is not None:
                references.append((raw, artifact_ref))
        attachments = atom.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, Mapping):
                    continue
                ref = attachment.get("artifact_ref")
                if not isinstance(ref, Mapping):
                    continue
                raw = _text(ref.get("path"))
                if raw is not None:
                    references.append((raw, ref))
        if not references:
            continue
        run_dir_raw = _text(atom.get("run_dir"))
        if run_dir_raw is None:
            raise ValueError("qualification_semantic_atom_run_dir_missing")
        run_candidate = Path(run_dir_raw).expanduser()
        if run_candidate.is_absolute():
            run_candidates = [run_candidate]
        else:
            # Real history records carry absolute run_dir values.  Accept the two
            # useful serialized forms as well: repo-relative paths and run-root-
            # relative coordinates.  The latter matters for explicitly selected
            # retained roots that can live outside the pipeline repository.
            run_candidates = [repo_root / run_candidate]
            run_candidates.extend(root / run_candidate for root in resolved_roots)
        bound_candidates: list[tuple[Path, Path]] = []
        for candidate in run_candidates:
            try:
                candidate_root = _root_for_path(
                    candidate, resolved_roots, label="atom_run_dir"
                )
                candidate_relative = candidate.relative_to(candidate_root).as_posix()
                checked = _checked_candidate(
                    candidate_root,
                    candidate_relative,
                    label="atom_run_dir",
                )
            except ValueError:
                continue
            if checked.is_dir():
                binding = (candidate_root, checked)
                if binding not in bound_candidates:
                    bound_candidates.append(binding)
        if len(bound_candidates) != 1:
            raise ValueError(
                f"qualification_semantic_atom_run_dir_binding_invalid:{run_dir_raw}"
            )
        run_root, run_dir = bound_candidates[0]
        run_parts = run_dir.relative_to(run_root).parts
        if len(run_parts) != 4 or not (run_dir / "target_ref.json").is_file():
            raise ValueError(
                f"qualification_semantic_atom_run_dir_not_canonical:{run_dir_raw}"
            )
        for raw_path, ref in references:
            root, candidate, relative = _absolute_candidate(
                raw_path,
                base=run_dir,
                roots=resolved_roots,
                label="atom_artifact",
            )
            if root != run_root:
                raise ValueError(
                    f"qualification_semantic_atom_artifact_cross_root:{raw_path}"
                )
            try:
                candidate.relative_to(run_dir)
            except ValueError as exc:
                raise ValueError(
                    f"qualification_semantic_atom_artifact_outside_run:{raw_path}"
                ) from exc
            expected_sha: str | None = None
            expected_size: int | None = None
            expected_exists: bool | None = None
            if ref is not None:
                if "sha256" in ref and ref.get("sha256") is not None:
                    expected_sha = _text(ref.get("sha256"))
                    if expected_sha is None or not _valid_sha256(expected_sha):
                        raise ValueError(
                            f"qualification_semantic_atom_artifact_sha256_invalid:{relative}"
                        )
                    expected_sha = expected_sha.casefold()
                if "size_bytes" in ref and ref.get("size_bytes") is not None:
                    expected_size_raw = ref.get("size_bytes")
                    if (
                        not isinstance(expected_size_raw, int)
                        or isinstance(expected_size_raw, bool)
                        or expected_size_raw < 0
                    ):
                        raise ValueError(
                            f"qualification_semantic_atom_artifact_size_invalid:{relative}"
                        )
                    expected_size = expected_size_raw
                if "exists" in ref and ref.get("exists") is not None:
                    exists_raw = ref.get("exists")
                    if not isinstance(exists_raw, bool):
                        raise ValueError(
                            f"qualification_semantic_atom_artifact_exists_invalid:{relative}"
                        )
                    expected_exists = exists_raw
            existing = by_root[root].get(relative)
            candidate_spec = {
                "path": relative,
                "expected_sha256": expected_sha,
                "expected_size_bytes": expected_size,
                "expected_exists": expected_exists,
            }
            if existing is not None:
                for key in (
                    "expected_sha256",
                    "expected_size_bytes",
                    "expected_exists",
                ):
                    old = existing.get(key)
                    new = candidate_spec.get(key)
                    if old is not None and new is not None and old != new:
                        raise ValueError(
                            f"qualification_semantic_atom_artifact_expectation_conflict:{relative}"
                        )
                    if old is None and new is not None:
                        existing[key] = new
            else:
                by_root[root][relative] = candidate_spec
    return {
        root: [records[path] for path in sorted(records)]
        for root, records in by_root.items()
    }


def _iter_outcome_records(value: Any) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            if {
                "schema_version",
                "case_id",
                "outcome_scope",
                "state",
            }.issubset(node):
                record = dict(node)
                records[_canonical_hash(record)] = record
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return [records[key] for key in sorted(records)]


def _load_json_mapping(path: Path) -> Mapping[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def collect_outcome_artifact_paths(
    documents: Sequence[Any],
    *,
    roots: Sequence[Path],
) -> dict[Path, list[str]]:
    """Close every run-root path the outcome provenance verifier can consume."""

    resolved_roots = tuple(root.expanduser().resolve() for root in roots)
    by_root: dict[Path, set[str]] = {root: set() for root in resolved_roots}

    def add(raw: Any, *, label: str, base: Path | None = None) -> Path | None:
        value = _text(raw)
        if value is None:
            return None
        root, path, relative = _absolute_candidate(
            value,
            base=base,
            roots=resolved_roots,
            label=label,
        )
        by_root[root].add(relative)
        return path

    records = [
        record
        for document in documents
        for record in _iter_outcome_records(document)
    ]
    for record in records:
        state = _text(record.get("state"))
        if state not in OUTCOME_STATES:
            raise ValueError(
                f"qualification_semantic_outcome_state_unsupported:{state or 'missing'}"
            )
        if state in _NONTERMINAL_OUTCOME_STATES:
            continue
        if state in _RELATIONSHIP_OUTCOME_STATES:
            if record.get("outcome_scope") == "plan_copy":
                continue
            relation = record.get("relation_receipt")
            if not isinstance(relation, Mapping):
                continue
            receipt_path = add(
                relation.get("receipt_path"),
                label="outcome_relation_receipt",
            )
            receipt = _load_json_mapping(receipt_path) if receipt_path is not None else None
            if receipt is not None:
                add(
                    receipt.get("relation_review_response_path"),
                    label="outcome_relation_response",
                )
            continue
        if state not in _EXTERNALLY_VERIFIED_OUTCOME_STATES:
            raise ValueError(f"qualification_semantic_outcome_closure_unsupported:{state}")

        review_dir_raw = _text(record.get("review_run_dir"))
        review_dir: Path | None = None
        if review_dir_raw is not None:
            review_root, review_dir, review_relative = _absolute_candidate(
                review_dir_raw,
                base=None,
                roots=resolved_roots,
                label="outcome_review_run_dir",
            )
            for filename in ("review_ref.json", "review_summary.json", "merge_ref.json"):
                by_root[review_root].add(f"{review_relative.rstrip('/')}/{filename}")
            review_ref = _load_json_mapping(review_dir / "review_ref.json")
            implementation_raw = (
                review_ref.get("implementation_run_dir")
                if review_ref is not None
                else None
            )
            implementation_value = _text(implementation_raw)
            if implementation_value is not None:
                implementation_root, _, implementation_relative = _absolute_candidate(
                    implementation_value,
                    base=None,
                    roots=resolved_roots,
                    label="outcome_implementation_run_dir",
                )
                for filename in (
                    "ticket_ref.json",
                    "verification.json",
                    "target_ref.json",
                    "git_ref.json",
                    "workspace_ref.json",
                ):
                    by_root[implementation_root].add(
                        f"{implementation_relative.rstrip('/')}/{filename}"
                    )

        evidence_groups = (
            ("test", record.get("test_evidence")),
            ("original_scenario", record.get("original_scenario_evidence")),
            ("live", record.get("live_evidence")),
            ("mitigation_effect", record.get("mitigation_evidence")),
            (
                "recurrence",
                record.get("recurrence_check", {}).get("evidence")
                if isinstance(record.get("recurrence_check"), Mapping)
                else None,
            ),
        )
        for kind, raw_items in evidence_groups:
            items = raw_items if isinstance(raw_items, list) else []
            for item in items:
                if (
                    not isinstance(item, Mapping)
                    or str(item.get("result") or "").casefold() != "passed"
                ):
                    continue
                receipt = item.get("runner_receipt")
                if not isinstance(receipt, Mapping):
                    continue
                if kind == "test":
                    add(
                        receipt.get("verification_path"),
                        label="outcome_test_verification",
                    )
                    add(
                        receipt.get("ticket_ref_path"),
                        label="outcome_test_ticket_ref",
                    )
                    continue
                role_path = add(
                    receipt.get("role_artifact_path"),
                    label=f"outcome_{kind}_role_artifact",
                )
                role_artifact = (
                    _load_json_mapping(role_path) if role_path is not None else None
                )
                predicates = (
                    role_artifact.get("predicate_results")
                    if role_artifact is not None
                    else None
                )
                for predicate in predicates if isinstance(predicates, list) else []:
                    if not isinstance(predicate, Mapping):
                        continue
                    artifact_receipt = predicate.get("artifact_receipt")
                    if isinstance(artifact_receipt, Mapping):
                        add(
                            artifact_receipt.get("snapshot_path"),
                            label=f"outcome_{kind}_snapshot",
                        )
    return {root: sorted(paths) for root, paths in by_root.items()}


def build_semantic_run_evidence_manifest(
    root: Path,
    *,
    name: str,
    target_slug: str | None,
    root_role: str,
    atom_artifact_specs: Sequence[Mapping[str, Any]] = (),
    outcome_artifact_paths: Sequence[str] = (),
) -> dict[str, Any]:
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise ValueError(f"qualification_input_tree_missing:{name}:{resolved_root}")
    if root_role not in {"primary", "implementation", "retained"}:
        raise ValueError(f"qualification_semantic_root_role_invalid:{root_role}")
    contract = _artifact_contract()
    inventory = _run_inventory(
        resolved_root,
        target_slug=target_slug,
        root_role=root_role,
    )
    static_entries = _static_entries_and_run_receipts(
        resolved_root,
        inventory=inventory,
        contract=contract,
    )
    normalized_atom_specs = sorted(
        (
            {
                "path": _safe_relative_path(str(spec.get("path") or ""), label="atom_artifact"),
                "expected_sha256": (
                    str(spec["expected_sha256"])
                    if _valid_sha256(spec.get("expected_sha256"))
                    else None
                ),
                "expected_size_bytes": (
                    spec.get("expected_size_bytes")
                    if isinstance(spec.get("expected_size_bytes"), int)
                    and not isinstance(spec.get("expected_size_bytes"), bool)
                    else None
                ),
                "expected_exists": (
                    spec.get("expected_exists")
                    if isinstance(spec.get("expected_exists"), bool)
                    else None
                ),
            }
            for spec in atom_artifact_specs
        ),
        key=lambda item: item["path"],
    )
    atom_entries_by_path: dict[str, dict[str, Any]] = {}
    for spec in normalized_atom_specs:
        for entry in _atom_artifact_entries(
            resolved_root,
            spec["path"],
            expected_sha256=spec["expected_sha256"],
            expected_size_bytes=spec["expected_size_bytes"],
            expected_exists=spec["expected_exists"],
        ):
            path = str(entry["path"])
            existing = atom_entries_by_path.get(path)
            if existing is not None and existing != entry:
                raise ValueError(
                    f"qualification_semantic_atom_artifact_entry_conflict:{path}"
                )
            atom_entries_by_path[path] = entry
    atom_entries = [
        atom_entries_by_path[path] for path in sorted(atom_entries_by_path)
    ]
    normalized_outcome_paths = sorted(
        {
            _safe_relative_path(path, label="outcome_artifact")
            for path in outcome_artifact_paths
        }
    )
    outcome_entries = [
        _artifact_entry(
            resolved_root,
            path,
            roles=["outcome_provenance"],
            label="outcome_artifact",
        )
        for path in normalized_outcome_paths
    ]
    run_receipts = _run_receipts(
        inventory=inventory,
        static_entries=static_entries,
        atom_entries=atom_entries,
        outcome_entries=outcome_entries,
    )
    manifest: dict[str, Any] = {
        "manifest_kind": SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND,
        "schema_version": SEMANTIC_RUN_EVIDENCE_MANIFEST_SCHEMA_VERSION,
        "name": name,
        "root": str(resolved_root),
        "target_slug": target_slug,
        "root_role": root_role,
        "artifact_contract": contract,
        "inventory": inventory,
        "inventory_sha256": _canonical_hash(inventory),
        "static_entries": static_entries,
        "static_entries_sha256": _canonical_hash(static_entries),
        "atom_artifact_paths": [spec["path"] for spec in normalized_atom_specs],
        "atom_artifact_entries": atom_entries,
        "atom_artifact_entries_sha256": _canonical_hash(atom_entries),
        "outcome_artifact_paths": normalized_outcome_paths,
        "outcome_artifact_entries": outcome_entries,
        "outcome_artifact_entries_sha256": _canonical_hash(outcome_entries),
        "run_receipts": run_receipts,
        "run_receipts_sha256": _canonical_hash(run_receipts),
    }
    manifest["entries_sha256"] = _canonical_hash(
        {
            "static": static_entries,
            "atom": atom_entries,
            "outcome": outcome_entries,
        }
    )
    manifest["manifest_sha256"] = _canonical_hash(manifest)
    return manifest


def extend_semantic_manifest_atom_closure(
    manifest: Mapping[str, Any],
    *,
    atoms: Sequence[Mapping[str, Any]],
    repo_root: Path,
) -> dict[str, Any]:
    """Rebuild one sealed root with the references discovered during extraction."""

    if manifest.get("manifest_kind") != SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND:
        raise ValueError("qualification_semantic_manifest_kind_invalid:atom_extension")
    root_raw = _text(manifest.get("root"))
    root_role = _text(manifest.get("root_role"))
    target_raw = manifest.get("target_slug")
    outcome_raw = manifest.get("outcome_artifact_paths")
    if (
        root_raw is None
        or root_role is None
        or not isinstance(outcome_raw, list)
        or any(not isinstance(path, str) for path in outcome_raw)
    ):
        raise ValueError("qualification_semantic_manifest_invalid:atom_extension")
    root = Path(root_raw).expanduser().resolve()
    specs = collect_atom_artifact_specs(
        atoms,
        repo_root=repo_root.expanduser().resolve(),
        roots=[root],
    )[root]
    observed = build_semantic_run_evidence_manifest(
        root,
        name=str(manifest.get("name") or "semantic_run_evidence"),
        target_slug=target_raw if isinstance(target_raw, str) else None,
        root_role=root_role,
        atom_artifact_specs=specs,
        outcome_artifact_paths=outcome_raw,
    )
    if semantic_manifest_base_projection(manifest) != semantic_manifest_base_projection(
        observed
    ):
        raise ValueError("qualification_input_source_changed_during_extraction")
    return observed


def semantic_manifest_base_projection(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return the pre-extraction portion; atom references are a planned extension."""

    return {
        key: manifest.get(key)
        for key in (
            "manifest_kind",
            "schema_version",
            "name",
            "root",
            "target_slug",
            "root_role",
            "artifact_contract",
            "inventory",
            "inventory_sha256",
            "static_entries",
            "static_entries_sha256",
            "outcome_artifact_paths",
            "outcome_artifact_entries",
            "outcome_artifact_entries_sha256",
        )
    }


def verify_semantic_run_evidence_manifest(
    manifest: Mapping[str, Any],
    *,
    name: str,
) -> list[str]:
    if manifest.get("manifest_kind") != SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND:
        return [f"qualification_input_semantic_manifest_kind_invalid:{name}"]
    root_raw = _text(manifest.get("root"))
    target_raw = manifest.get("target_slug")
    target_slug = target_raw if isinstance(target_raw, str) else None
    root_role = _text(manifest.get("root_role"))
    atom_paths_raw = manifest.get("atom_artifact_paths")
    outcome_paths_raw = manifest.get("outcome_artifact_paths")
    if (
        root_raw is None
        or root_role is None
        or not isinstance(atom_paths_raw, list)
        or any(not isinstance(path, str) for path in atom_paths_raw)
        or not isinstance(outcome_paths_raw, list)
        or any(not isinstance(path, str) for path in outcome_paths_raw)
    ):
        return [f"qualification_input_semantic_manifest_invalid:{name}"]
    try:
        observed = build_semantic_run_evidence_manifest(
            Path(root_raw),
            name=str(manifest.get("name") or name),
            target_slug=target_slug,
            root_role=root_role,
            atom_artifact_specs=[{"path": path} for path in atom_paths_raw],
            outcome_artifact_paths=outcome_paths_raw,
        )
    except ValueError as exc:
        return [str(exc)]
    if observed != dict(manifest):
        return [f"qualification_input_semantic_tree_changed:{name}"]
    return []


__all__ = [
    "SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND",
    "SEMANTIC_RUN_EVIDENCE_MANIFEST_SCHEMA_VERSION",
    "build_semantic_run_evidence_manifest",
    "collect_atom_artifact_specs",
    "collect_outcome_artifact_paths",
    "extend_semantic_manifest_atom_closure",
    "semantic_manifest_base_projection",
    "verify_semantic_run_evidence_manifest",
]
