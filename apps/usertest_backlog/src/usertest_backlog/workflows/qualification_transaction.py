"""Sealed, model-free release-qualification transactions.

The release benchmark is prepared before any author model runs.  Preparation
freezes the exact evidence roots, copied lifecycle ledger, explicit case-registry
seed, pipeline implementation/configuration, target revision, and atom corpus in a
content-addressed bundle.  Phase one consumes that bundle and only the byte digest
of held-out labels.  Phase two supplies the actual manifest and proves its bytes
match the pre-run digest before any semantic score is recorded.

This module also owns operator-facing adjudication templates.  The template is
derived from the same accepted-output projection used by the scorer, so an
independent adjudicator never has to reproduce private readiness filtering rules.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from backlog_core.case_lineage import atom_is_idea_originated

from usertest_backlog.workflows.pipeline_provenance import (
    pipeline_runtime_compatibility_bindings,
    pipeline_source_config_bindings,
)
from usertest_backlog.workflows.problem_mining_evidence import (
    immutable_atom_evidence_projection,
)
from usertest_backlog.workflows.qualification import (
    build_qualification_output_adjudication,
    qualification_manifest_errors,
    qualification_source_correction_findings,
    qualification_source_correction_findings_errors,
)
from usertest_backlog.workflows.qualification_run_manifest import (
    SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND,
    build_semantic_run_evidence_manifest,
    collect_atom_artifact_specs,
    collect_outcome_artifact_paths,
    semantic_manifest_base_projection,
    verify_semantic_run_evidence_manifest,
)
from usertest_backlog.workflows.shadow_validation import (
    qualification_accepted_outputs,
    validate_pending_shadow_run,
)

QUALIFICATION_INPUT_BUNDLE_SCHEMA_VERSION = 2
_LEGACY_QUALIFICATION_INPUT_BUNDLE_SCHEMA_VERSION = 1
QUALIFICATION_ADJUDICATION_TEMPLATE_SCHEMA_VERSION = 1


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _content_hash(document: Mapping[str, Any]) -> str:
    return _canonical_hash(
        {key: value for key, value in document.items() if key != "content_sha256"}
    )


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


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_receipt(path: Path, *, name: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"qualification_input_file_missing:{name}:{resolved}")
    return {
        "name": name,
        "path": str(resolved),
        "sha256": _file_sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _tree_manifest(
    path: Path,
    *,
    name: str,
    ignored_directory_names: Iterable[str] = (),
) -> dict[str, Any]:
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"qualification_input_tree_missing:{name}:{root}")
    ignored = {
        item.strip().casefold()
        for item in ignored_directory_names
        if isinstance(item, str) and item.strip()
    }
    entries: list[dict[str, Any]] = []
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative_path = candidate.relative_to(root)
        if any(part.casefold() in ignored for part in relative_path.parts):
            continue
        relative = relative_path.as_posix()
        if candidate.is_symlink():
            entries.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "target": os.readlink(candidate),
                }
            )
        elif candidate.is_file():
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "sha256": _file_sha256(candidate),
                    "size_bytes": candidate.stat().st_size,
                }
            )
    return {
        "name": name,
        "root": str(root),
        "ignored_directory_names": sorted(ignored),
        "entries": entries,
        "entries_sha256": _canonical_hash(entries),
    }


def _validated_additional_evidence_roots(
    roots: Iterable[Path],
    *,
    source_runs_dir: Path,
    target: str | None,
) -> list[tuple[Path, str | None]]:
    """Return explicit canonical run roots outside the inferred source pair.

    Qualification preparation must not recursively discover archives or moved storage.
    Each additional root is therefore an absolute operator-selected ``runs`` root with
    an immediate target/timestamp/agent/seed layout consumed by ``iter_report_history``.
    The primary target is preferred; a different target is accepted only when it is the
    root's sole canonical target and is recorded in that root's signed manifest. Selecting
    a narrow canonical root is the performance boundary; the selected tree itself remains
    fully content-addressed so newly added evidence readers cannot silently escape the seal.
    """

    primary = source_runs_dir.expanduser().resolve()
    inferred_implementation = primary.parent / "usertest_implement"
    selected: dict[Path, str | None] = {}
    for raw_root in roots:
        expanded = raw_root.expanduser()
        if not expanded.is_absolute():
            raise ValueError(
                f"qualification_additional_evidence_root_not_absolute:{raw_root}"
            )
        root = expanded.resolve()
        if root in {primary, inferred_implementation}:
            raise ValueError(
                f"qualification_additional_evidence_root_duplicates_inferred:{root}"
            )
        if not root.is_dir():
            raise ValueError(f"qualification_additional_evidence_root_missing:{root}")
        requested_target = target.strip() if isinstance(target, str) else ""
        if requested_target and next(
            root.glob(f"{requested_target}/*/*/*/target_ref.json"), None
        ) is not None:
            selected_target: str | None = requested_target
        else:
            target_slugs = sorted(
                {
                    path.relative_to(root).parts[0]
                    for path in root.glob("*/*/*/*/target_ref.json")
                    if len(path.relative_to(root).parts) == 5
                }
            )
            if not requested_target and target_slugs:
                selected_target = None
            elif len(target_slugs) == 1:
                selected_target = target_slugs[0]
            elif len(target_slugs) > 1:
                raise ValueError(
                    f"qualification_additional_evidence_root_target_ambiguous:{root}"
                )
            else:
                selected_target = None
        if selected_target is None and (
            requested_target or next(root.glob("*/*/*/*/target_ref.json"), None) is None
        ):
            raise ValueError(
                f"qualification_additional_evidence_root_not_canonical:{root}"
            )
        selected[root] = selected_target
    return sorted(selected.items(), key=lambda item: item[0].as_posix())


def capture_qualification_source_snapshot(
    source_runs_dir: Path,
    *,
    target: str | None = None,
    additional_evidence_runs_dirs: Iterable[Path] = (),
    atoms: Sequence[Mapping[str, Any]] = (),
    repo_root: Path | None = None,
    outcome_documents: Sequence[Any] = (),
) -> dict[str, Any]:
    """Capture only evidence that can change extracted atoms or trusted outcomes."""

    source_runs_dir = source_runs_dir.expanduser().resolve()
    implementation_root = (source_runs_dir.parent / "usertest_implement").resolve()
    additional_root_targets = _validated_additional_evidence_roots(
        additional_evidence_runs_dirs,
        source_runs_dir=source_runs_dir,
        target=target,
    )
    additional_roots = [root for root, _target_slug in additional_root_targets]
    roots = [source_runs_dir, implementation_root, *additional_roots]
    atom_specs = collect_atom_artifact_specs(
        atoms,
        repo_root=(repo_root or source_runs_dir.parent).expanduser().resolve(),
        roots=roots,
    )
    outcome_paths = collect_outcome_artifact_paths(
        outcome_documents,
        roots=roots,
    )
    return {
        "source_runs": build_semantic_run_evidence_manifest(
            source_runs_dir,
            name="source_runs",
            target_slug=target,
            root_role="primary",
            atom_artifact_specs=atom_specs[source_runs_dir],
            outcome_artifact_paths=outcome_paths[source_runs_dir],
        ),
        "implementation_runs": build_semantic_run_evidence_manifest(
            implementation_root,
            name="implementation_runs",
            target_slug=target,
            root_role="implementation",
            atom_artifact_specs=atom_specs[implementation_root],
            outcome_artifact_paths=outcome_paths[implementation_root],
        ),
        "additional_evidence_runs": [
            build_semantic_run_evidence_manifest(
                root,
                name=f"additional_evidence_runs:{index:04d}",
                target_slug=target_slug,
                root_role="retained",
                atom_artifact_specs=atom_specs[root],
                outcome_artifact_paths=outcome_paths[root],
            )
            for index, (root, target_slug) in enumerate(
                additional_root_targets, start=1
            )
        ],
    }


def _load_structured_document(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"qualification_input_document_unreadable:{path}") from exc
    try:
        if path.suffix.casefold() == ".json":
            return json.loads(text)
        return yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"qualification_input_document_unreadable:{path}") from exc


def _ledger_owner_roots(path: Path) -> set[Path]:
    document = _load_structured_document(path)
    rows = document if isinstance(document, Mapping) else {}
    roots: set[Path] = set()
    for row in rows.values():
        if not isinstance(row, Mapping):
            continue
        roots_raw = row.get("queue_owner_roots")
        values = roots_raw if isinstance(roots_raw, list) else []
        for raw in values:
            value = _text(raw)
            if value is None:
                continue
            root = Path(value).expanduser()
            if not root.is_absolute():
                raise ValueError(f"qualification_input_owner_root_not_absolute:{value}")
            root = root.resolve()
            if not root.is_dir():
                raise ValueError(f"qualification_input_owner_root_missing:{root}")
            roots.add(root)
    return roots


def _normalize_remote_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[: -len(".git")]
    if "://" in raw:
        parsed = urlparse(raw)
        host = (parsed.hostname or parsed.netloc or "").strip().casefold()
        path = (parsed.path or "").strip().strip("/")
        if path.endswith(".git"):
            path = path[: -len(".git")]
        return f"{host}/{path.casefold()}" if host and path else (host or raw.casefold())
    match = re.match(r"^[^@]+@(?P<host>[^:]+):(?P<path>.+)$", raw)
    if match is not None:
        host = match.group("host").strip().casefold()
        path = match.group("path").strip().strip("/")
        if path.endswith(".git"):
            path = path[: -len(".git")]
        return f"{host}/{path.casefold()}" if host and path else (host or raw.casefold())
    return raw.casefold()


def _git_result(repo: Path, *args: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
    }


def _outcome_git_queries(*documents: Any) -> list[dict[str, str | None]]:
    queries: set[tuple[str, str, str | None]] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            commit = _text(value.get("merged_commit"))
            branch = _text(value.get("target_branch"))
            provenance = value.get("ticket_provenance")
            provenance = provenance if isinstance(provenance, Mapping) else {}
            implementation_head = _text(provenance.get("verified_implementation_head"))
            if commit is not None and branch is not None:
                queries.add((commit, branch, implementation_head))
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    for document in documents:
        visit(document)
    return [
        {
            "merged_commit": commit,
            "target_branch": branch,
            "verified_implementation_head": implementation_head,
        }
        for commit, branch, implementation_head in sorted(
            queries,
            key=lambda item: (item[0], item[1], item[2] or ""),
        )
    ]


def _owner_git_fact(
    root: Path,
    *,
    outcome_queries: Sequence[Mapping[str, str | None]],
) -> dict[str, Any]:
    remotes = _git_result(root, "remote", "-v")
    remote_urls = sorted(
        {
            _normalize_remote_url(parts[1])
            for line in str(remotes["stdout"]).splitlines()
            for parts in [line.split()]
            if len(parts) >= 2 and parts[1].strip()
        }
    )
    refs_result = _git_result(
        root,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
        "refs/heads",
        "refs/remotes",
    )
    refs = sorted(
        [
            {"ref": parts[0], "commit": parts[1].casefold()}
            for line in str(refs_result["stdout"]).splitlines()
            for parts in [line.split("\0", 1)]
            if len(parts) == 2 and parts[0] and parts[1]
        ],
        key=lambda item: (item["ref"], item["commit"]),
    )
    query_results: list[dict[str, Any]] = []
    for query in outcome_queries:
        commit = str(query["merged_commit"])
        branch = str(query["target_branch"])
        implementation_head = query.get("verified_implementation_head")
        commit_result = _git_result(root, "rev-parse", "--verify", f"{commit}^{{commit}}")
        target_results = [
            {
                "ref": ref,
                **_git_result(root, "rev-parse", "--verify", f"{ref}^{{commit}}"),
            }
            for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}")
        ]
        resolved_commit = (
            str(commit_result["stdout"]).casefold()
            if commit_result["returncode"] == 0 and commit_result["stdout"]
            else None
        )
        target_commit = next(
            (
                str(item["stdout"]).casefold()
                for item in target_results
                if item["returncode"] == 0 and item["stdout"]
            ),
            None,
        )
        head_result = (
            _git_result(
                root,
                "rev-parse",
                "--verify",
                f"{implementation_head}^{{commit}}",
            )
            if implementation_head is not None
            else None
        )
        resolved_head = (
            str(head_result["stdout"]).casefold()
            if isinstance(head_result, Mapping)
            and head_result.get("returncode") == 0
            and head_result.get("stdout")
            else None
        )
        query_results.append(
            {
                **dict(query),
                "merged_commit_resolution": commit_result,
                "verified_head_resolution": head_result,
                "target_ref_resolutions": target_results,
                "verified_head_is_ancestor_of_merged": (
                    _git_result(
                        root,
                        "merge-base",
                        "--is-ancestor",
                        resolved_head,
                        resolved_commit,
                    )
                    if resolved_head is not None and resolved_commit is not None
                    else None
                ),
                "merged_is_ancestor_of_target": (
                    _git_result(
                        root,
                        "merge-base",
                        "--is-ancestor",
                        resolved_commit,
                        target_commit,
                    )
                    if resolved_commit is not None and target_commit is not None
                    else None
                ),
            }
        )
    return {
        "root": str(root.resolve()),
        "head": _git_result(root, "rev-parse", "HEAD^{commit}"),
        "remote_command_returncode": remotes["returncode"],
        "remote_urls": remote_urls,
        "ref_command_returncode": refs_result["returncode"],
        "refs": refs,
        "outcome_queries": query_results,
    }


def _pipeline_paths(repo_root: Path) -> list[Path]:
    bindings = pipeline_source_config_bindings(
        source_root=repo_root,
        config_root=repo_root / "configs",
        # Git identity/status is bound separately by the qualification contract.
        include_git_metadata=False,
    )
    return sorted(set(bindings.values()), key=lambda path: path.as_posix())


def _runtime_compatibility_paths(repo_root: Path) -> list[Path]:
    bindings = pipeline_runtime_compatibility_bindings(
        source_root=repo_root,
        config_root=repo_root / "configs",
    )
    return sorted(set(bindings.values()), key=lambda path: path.as_posix())


def _file_manifest(repo_root: Path, paths: Sequence[Path]) -> dict[str, Any]:
    receipts = [
        {
            "path": path.relative_to(repo_root).as_posix(),
            "sha256": _file_sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in paths
    ]
    return {
        "repo_root": str(repo_root.resolve()),
        "files": receipts,
        "files_sha256": _canonical_hash(receipts),
    }


def _pipeline_manifest(repo_root: Path) -> dict[str, Any]:
    manifest = _file_manifest(repo_root, _pipeline_paths(repo_root))
    if not manifest["files"]:
        raise ValueError("qualification_input_pipeline_manifest_empty")
    return manifest


def _runtime_compatibility_manifest(repo_root: Path) -> dict[str, Any]:
    manifest = _file_manifest(repo_root, _runtime_compatibility_paths(repo_root))
    if not manifest["files"]:
        raise ValueError("qualification_runtime_compatibility_manifest_empty")
    return manifest


def current_pipeline_runtime_compatibility(repo_root: Path) -> dict[str, Any]:
    """Build the public runtime-stability projection for operational handoff."""

    manifest = _runtime_compatibility_manifest(repo_root.expanduser().resolve())
    return {
        "manifest": manifest,
        "sha256": _canonical_hash(manifest),
    }


def qualification_runtime_compatibility_errors(
    bundle: Mapping[str, Any],
    *,
    repo_root: Path,
) -> list[str]:
    """Compare only backlog-runtime behavior, independent of the full seal."""

    pipeline = bundle.get("pipeline")
    pipeline = pipeline if isinstance(pipeline, Mapping) else {}
    recorded_manifest = pipeline.get("runtime_compatibility")
    recorded_sha256 = pipeline.get("runtime_compatibility_sha256")
    if not isinstance(recorded_manifest, Mapping) or not _valid_sha256(recorded_sha256):
        return ["qualification_runtime_compatibility_receipt_invalid"]
    if recorded_sha256 != _canonical_hash(recorded_manifest):
        return ["qualification_runtime_compatibility_hash_invalid"]
    current = current_pipeline_runtime_compatibility(repo_root)
    if (
        current["sha256"] != recorded_sha256
        or current["manifest"] != recorded_manifest
    ):
        return ["qualification_runtime_compatibility_changed"]
    return []


def _protected_path_manifest(path: Path, *, name: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        return {"kind": "file", **_file_receipt(resolved, name=name)}
    if resolved.is_dir():
        return {"kind": "tree", **_tree_manifest(resolved, name=name)}
    raise ValueError(f"qualification_protected_path_missing:{name}:{resolved}")


def _git_output(repo: Path, *args: str) -> str:
    safe_directory = repo.expanduser().resolve().as_posix()
    completed = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={safe_directory}",
            "-C",
            str(repo),
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(
            f"qualification_input_git_command_failed:{' '.join(args)}:{detail}"
        )
    return completed.stdout.strip()


def _exact_research_revision(repo_input: Path, research_ref: str) -> str:
    normalized = research_ref.strip().casefold()
    if len(normalized) != 40 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("qualification_input_research_ref_not_exact_40hex")
    resolved = _git_output(repo_input, "rev-parse", f"{normalized}^{{commit}}").casefold()
    if resolved != normalized:
        raise ValueError(
            "qualification_input_research_ref_resolution_mismatch:"
            f"expected={normalized}:observed={resolved}"
        )
    return normalized


def _atom_receipts(atoms: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    receipts: list[dict[str, str]] = []
    seen: set[str] = set()
    for atom in atoms:
        if atom_is_idea_originated(dict(atom)):
            continue
        atom_id = _text(atom.get("atom_id"))
        if atom_id is None:
            raise ValueError("qualification_input_atom_id_missing")
        if atom_id in seen:
            raise ValueError(f"qualification_input_atom_id_duplicate:{atom_id}")
        seen.add(atom_id)
        receipts.append(
            {
                "atom_id": atom_id,
                "atom_sha256": _canonical_hash(
                    immutable_atom_evidence_projection(atom)
                ),
            }
        )
    return sorted(receipts, key=lambda item: item["atom_id"])


def _default_protected_paths(repo_input: Path) -> list[Path]:
    plan_root = repo_input / ".agents" / "plans"
    protected: list[Path] = []
    # Release qualification is non-exporting, but that alone does not prove a
    # model did not edit or delete an existing ticket. Snapshot the complete plan
    # tree as the mutation baseline, then retain explicit IDEA roots/receipts so
    # an audit can distinguish user-originated content from generated backlog.
    if plan_root.is_dir():
        protected.append(plan_root)
    roadmap_root = plan_root / "0 - roadmaps"
    if roadmap_root.is_dir():
        protected.append(roadmap_root)
    ideas_root = plan_root / "1 - ideas"
    if ideas_root.is_dir():
        protected.append(ideas_root)
    if plan_root.is_dir():
        protected.extend(
            child
            for child in plan_root.iterdir()
            if child.is_dir() and "idea" in child.name.casefold()
        )
    if plan_root.is_dir():
        markers = (
            "generated from tracked idea plan",
            "generated from tracked token-saving plan",
            "origin_category: idea",
            '"origin_category": "idea"',
        )
        for path in plan_root.rglob("*.md"):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore").casefold()
                filename = path.name.casefold()
                idea_named = "idea" in filename or "token-saving" in filename
                provenance_marked = any(marker in text for marker in markers)
                source_plan_marked = "source plan" in text and (
                    "idea" in text or "token-saving" in text
                )
                if idea_named or provenance_marked or source_plan_marked:
                    protected.append(path)
            except OSError:
                continue
    return sorted(set(protected), key=lambda item: item.as_posix())


def _protected_manifests(
    *,
    owner_roots: Iterable[Path],
    protected_paths: Iterable[Path],
) -> list[dict[str, Any]]:
    protected = [
        path
        for owner_root in sorted(set(owner_roots), key=lambda item: item.as_posix())
        for path in _default_protected_paths(owner_root)
    ]
    protected.extend(path.expanduser().resolve() for path in protected_paths)
    return [
        _protected_path_manifest(path, name=f"protected:{index:04d}")
        for index, path in enumerate(
            sorted(set(protected), key=lambda item: item.as_posix()),
            start=1,
        )
    ]


def capture_qualification_preparation_snapshot(
    *,
    repo_root: Path,
    repo_input: Path,
    research_ref: str,
    source_runs_dir: Path,
    atom_actions_path: Path,
    case_registry_seed_path: Path,
    target: str | None = None,
    additional_evidence_runs_dirs: Iterable[Path] = (),
    atoms: Sequence[Mapping[str, Any]] = (),
    protected_paths: Iterable[Path] = (),
    owner_roots: Iterable[Path] = (),
) -> dict[str, Any]:
    """Capture every mutable input consumed by deterministic preparation."""

    repo_root = repo_root.expanduser().resolve()
    repo_input = repo_input.expanduser().resolve()
    source_runs_dir = source_runs_dir.expanduser().resolve()
    atom_actions_path = atom_actions_path.expanduser().resolve()
    case_registry_seed_path = case_registry_seed_path.expanduser().resolve()
    if not repo_input.is_dir():
        raise ValueError(f"qualification_input_repo_missing:{repo_input}")

    ledger_receipt = _file_receipt(atom_actions_path, name="copied_atom_actions")
    registry_receipt = _file_receipt(case_registry_seed_path, name="case_registry_seed")
    ledger_document = _load_structured_document(atom_actions_path)
    registry_document = _load_structured_document(case_registry_seed_path)
    source_snapshot = capture_qualification_source_snapshot(
        source_runs_dir,
        target=target,
        additional_evidence_runs_dirs=additional_evidence_runs_dirs,
        atoms=atoms,
        repo_root=repo_root,
        outcome_documents=(ledger_document, registry_document),
    )
    all_owner_roots = {
        repo_root,
        repo_input,
        *_ledger_owner_roots(atom_actions_path),
        *(path.expanduser().resolve() for path in owner_roots),
    }
    if any(not root.is_dir() for root in all_owner_roots):
        raise ValueError("qualification_input_owner_root_missing")
    outcome_queries = _outcome_git_queries(ledger_document, registry_document)
    pipeline_status = _git_output(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
    )
    runtime_compatibility = current_pipeline_runtime_compatibility(repo_root)
    snapshot = {
        "scope": {
            "repo_input": str(repo_input),
            "research_ref": _exact_research_revision(repo_input, research_ref),
        },
        "source_inputs": {
            **source_snapshot,
            "atom_actions": ledger_receipt,
            "case_registry_seed": registry_receipt,
            "owner_roots": [
                str(path)
                for path in sorted(all_owner_roots, key=lambda item: item.as_posix())
            ],
            "owner_git_facts": [
                _owner_git_fact(root, outcome_queries=outcome_queries)
                for root in sorted(all_owner_roots, key=lambda item: item.as_posix())
            ],
        },
        "pipeline": {
            "git_head": _git_output(repo_root, "rev-parse", "HEAD^{commit}").casefold(),
            "git_status_sha256": sha256(pipeline_status.encode("utf-8")).hexdigest(),
            "files": _pipeline_manifest(repo_root),
            "runtime_compatibility": runtime_compatibility["manifest"],
            "runtime_compatibility_sha256": runtime_compatibility["sha256"],
        },
        "protected_paths": _protected_manifests(
            owner_roots=all_owner_roots,
            protected_paths=protected_paths,
        ),
    }
    # Close the small window between receipt creation and structured parsing.
    if ledger_receipt != _file_receipt(atom_actions_path, name="copied_atom_actions"):
        raise ValueError("qualification_input_atom_actions_changed_during_snapshot")
    if registry_receipt != _file_receipt(case_registry_seed_path, name="case_registry_seed"):
        raise ValueError("qualification_input_registry_changed_during_snapshot")
    return snapshot


def _semantic_source_value_base_equal(prior: Any, observed: Any) -> bool:
    if isinstance(prior, Mapping) and isinstance(observed, Mapping):
        if (
            prior.get("manifest_kind") == SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND
            and observed.get("manifest_kind") == SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND
        ):
            return semantic_manifest_base_projection(prior) == (
                semantic_manifest_base_projection(observed)
            )
        return dict(prior) == dict(observed)
    if isinstance(prior, list) and isinstance(observed, list):
        return len(prior) == len(observed) and all(
            _semantic_source_value_base_equal(left, right)
            for left, right in zip(prior, observed, strict=True)
        )
    return prior == observed


def _protected_manifest_content(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return stable protected content without its display-only ordinal name."""

    return {key: value for key, value in manifest.items() if key != "name"}


def extend_qualification_preparation_snapshot(
    snapshot: Mapping[str, Any],
    *,
    repo_root: Path,
    repo_input: Path,
    research_ref: str,
    source_runs_dir: Path,
    atom_actions_path: Path,
    case_registry_seed_path: Path,
    target: str | None = None,
    additional_evidence_runs_dirs: Iterable[Path] = (),
    atoms: Sequence[Mapping[str, Any]] = (),
    protected_paths: Iterable[Path] = (),
    owner_roots: Iterable[Path] = (),
) -> dict[str, Any]:
    """Add record-derived owners while proving earlier inputs stayed frozen."""

    observed = capture_qualification_preparation_snapshot(
        repo_root=repo_root,
        repo_input=repo_input,
        research_ref=research_ref,
        source_runs_dir=source_runs_dir,
        atom_actions_path=atom_actions_path,
        case_registry_seed_path=case_registry_seed_path,
        target=target,
        additional_evidence_runs_dirs=additional_evidence_runs_dirs,
        atoms=atoms,
        protected_paths=protected_paths,
        owner_roots=owner_roots,
    )
    for section in ("scope", "pipeline"):
        if snapshot.get(section) != observed.get(section):
            raise ValueError(f"qualification_input_{section}_changed_during_extraction")
    prior_source = snapshot.get("source_inputs")
    observed_source = observed.get("source_inputs")
    prior_source = prior_source if isinstance(prior_source, Mapping) else {}
    observed_source = observed_source if isinstance(observed_source, Mapping) else {}
    for key in (
        "source_runs",
        "implementation_runs",
        "additional_evidence_runs",
        "atom_actions",
        "case_registry_seed",
    ):
        if not _semantic_source_value_base_equal(
            prior_source.get(key), observed_source.get(key)
        ):
            label = (
                "source"
                if key
                in {"source_runs", "implementation_runs", "additional_evidence_runs"}
                else key
            )
            raise ValueError(f"qualification_input_{label}_changed_during_extraction")
    observed_git = {
        item.get("root"): item
        for item in observed_source.get("owner_git_facts", [])
        if isinstance(item, Mapping)
    }
    for item in prior_source.get("owner_git_facts", []):
        if not isinstance(item, Mapping) or observed_git.get(item.get("root")) != item:
            raise ValueError("qualification_input_owner_git_changed_during_extraction")
    observed_protected = {
        (item.get("kind"), item.get("path") or item.get("root")): item
        for item in observed.get("protected_paths", [])
        if isinstance(item, Mapping)
    }
    for item in snapshot.get("protected_paths", []):
        if not isinstance(item, Mapping):
            raise ValueError("qualification_input_protected_changed_during_extraction")
        key = (item.get("kind"), item.get("path") or item.get("root"))
        observed_item = observed_protected.get(key)
        if not isinstance(observed_item, Mapping) or _protected_manifest_content(
            observed_item
        ) != _protected_manifest_content(item):
            raise ValueError("qualification_input_protected_changed_during_extraction")
    return observed


def build_qualification_input_bundle(
    *,
    atoms: Sequence[Mapping[str, Any]],
    repo_root: Path,
    repo_input: Path,
    research_ref: str,
    source_runs_dir: Path,
    atom_actions_path: Path,
    case_registry_seed_path: Path,
    target: str | None,
    breadth_profile: str,
    additional_evidence_runs_dirs: Iterable[Path] = (),
    protected_paths: Iterable[Path] = (),
    owner_roots: Iterable[Path] = (),
    extraction_metadata: Mapping[str, Any] | None = None,
    source_input_snapshot: Mapping[str, Mapping[str, Any]] | None = None,
    preparation_input_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete pre-model qualification input contract."""

    repo_root = repo_root.expanduser().resolve()
    repo_input = repo_input.expanduser().resolve()
    source_runs_dir = source_runs_dir.expanduser().resolve()
    atom_actions_path = atom_actions_path.expanduser().resolve()
    case_registry_seed_path = case_registry_seed_path.expanduser().resolve()
    default_ledger = (repo_root / "configs" / "backlog_atom_actions.yaml").resolve()
    if atom_actions_path == default_ledger:
        raise ValueError("qualification_input_requires_copied_atom_actions")
    observed_snapshot = capture_qualification_preparation_snapshot(
        repo_root=repo_root,
        repo_input=repo_input,
        research_ref=research_ref,
        source_runs_dir=source_runs_dir,
        atom_actions_path=atom_actions_path,
        case_registry_seed_path=case_registry_seed_path,
        target=target,
        additional_evidence_runs_dirs=additional_evidence_runs_dirs,
        atoms=atoms,
        protected_paths=protected_paths,
        owner_roots=owner_roots,
    )
    if preparation_input_snapshot is not None:
        prior_snapshot = {
            key: value for key, value in preparation_input_snapshot.items()
        }
        if observed_snapshot != prior_snapshot:
            raise ValueError("qualification_input_changed_during_extraction")
        frozen_snapshot = prior_snapshot
    else:
        frozen_snapshot = observed_snapshot
    if source_input_snapshot is not None:
        legacy_source = dict(source_input_snapshot)
        frozen_source = frozen_snapshot.get("source_inputs")
        frozen_source = frozen_source if isinstance(frozen_source, Mapping) else {}
        if any(
            not _semantic_source_value_base_equal(frozen_source.get(key), value)
            for key, value in legacy_source.items()
        ):
            raise ValueError("qualification_input_source_changed_during_extraction")

    frozen_scope = frozen_snapshot.get("scope")
    frozen_scope = frozen_scope if isinstance(frozen_scope, Mapping) else {}
    frozen_source = frozen_snapshot.get("source_inputs")
    frozen_source = frozen_source if isinstance(frozen_source, Mapping) else {}
    frozen_pipeline = frozen_snapshot.get("pipeline")
    frozen_pipeline = frozen_pipeline if isinstance(frozen_pipeline, Mapping) else {}
    frozen_protected = frozen_snapshot.get("protected_paths")
    frozen_protected = frozen_protected if isinstance(frozen_protected, list) else []
    # A qualification corpus is evidence, not a snapshot of prior workflow
    # decisions.  Strip case/disposition/reopen/correction audit fields before
    # content addressing it so two preparations over the same observations have
    # the same identity even when a prior nondeterministic turn made different
    # decisions.  IDEA-originated atoms are outside automated qualification.
    atom_rows = [
        immutable_atom_evidence_projection(atom)
        for atom in atoms
        if not atom_is_idea_originated(dict(atom))
    ]
    receipts = _atom_receipts(atom_rows)
    bundle: dict[str, Any] = {
        "schema_version": QUALIFICATION_INPUT_BUNDLE_SCHEMA_VERSION,
        "contract_kind": "qualification_input_bundle",
        "scope": {
            "target": target,
            "repo_input": str(repo_input),
            "research_ref": frozen_scope.get("research_ref"),
            "breadth_profile": breadth_profile,
        },
        "source_inputs": {
            key: frozen_source.get(key)
            for key in (
                "source_runs",
                "implementation_runs",
                "additional_evidence_runs",
                "atom_actions",
                "case_registry_seed",
                "owner_roots",
                "owner_git_facts",
            )
        },
        "pipeline": dict(frozen_pipeline),
        "atom_corpus": {
            "count": len(receipts),
            "receipts": receipts,
            "receipts_sha256": _canonical_hash(receipts),
        },
        "atoms": atom_rows,
        "protected_paths": frozen_protected,
        "extraction_metadata": dict(extraction_metadata or {}),
    }
    bundle["content_sha256"] = _content_hash(bundle)
    errors = qualification_input_bundle_errors(bundle, verify_files=True)
    if errors:
        raise ValueError("qualification_input_bundle_invalid:" + ",".join(errors))
    return bundle


def _verify_file_receipt(receipt: Mapping[str, Any], *, name: str) -> list[str]:
    path_raw = _text(receipt.get("path"))
    if path_raw is None:
        return [f"qualification_input_receipt_path_missing:{name}"]
    path = Path(path_raw).resolve()
    if not path.is_file():
        return [f"qualification_input_receipt_file_missing:{name}"]
    errors: list[str] = []
    if receipt.get("sha256") != _file_sha256(path):
        errors.append(f"qualification_input_receipt_hash_changed:{name}")
    if receipt.get("size_bytes") != path.stat().st_size:
        errors.append(f"qualification_input_receipt_size_changed:{name}")
    return errors


def _verify_tree_manifest(manifest: Mapping[str, Any], *, name: str) -> list[str]:
    root_raw = _text(manifest.get("root"))
    if root_raw is None:
        return [f"qualification_input_tree_root_missing:{name}"]
    try:
        ignored_raw = manifest.get("ignored_directory_names")
        ignored = ignored_raw if isinstance(ignored_raw, list) else []
        observed = _tree_manifest(
            Path(root_raw),
            name=str(manifest.get("name") or name),
            ignored_directory_names=ignored,
        )
    except ValueError as exc:
        return [str(exc)]
    expected = {key: value for key, value in manifest.items() if key != "kind"}
    if observed != expected:
        return [f"qualification_input_tree_changed:{name}"]
    return []


def _verify_run_evidence_manifest(
    manifest: Mapping[str, Any],
    *,
    name: str,
) -> list[str]:
    if manifest.get("manifest_kind") == SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND:
        return verify_semantic_run_evidence_manifest(manifest, name=name)
    return _verify_tree_manifest(manifest, name=name)


def qualification_input_bundle_errors(
    value: Any,
    *,
    verify_files: bool,
) -> list[str]:
    if not isinstance(value, Mapping):
        return ["qualification_input_bundle_invalid"]
    errors: list[str] = []
    schema_version = value.get("schema_version")
    if schema_version not in {
        _LEGACY_QUALIFICATION_INPUT_BUNDLE_SCHEMA_VERSION,
        QUALIFICATION_INPUT_BUNDLE_SCHEMA_VERSION,
    }:
        errors.append("qualification_input_bundle_schema_invalid")
    if value.get("contract_kind") != "qualification_input_bundle":
        errors.append("qualification_input_bundle_kind_invalid")
    content_sha256 = value.get("content_sha256")
    if not _valid_sha256(content_sha256) or content_sha256 != _content_hash(value):
        errors.append("qualification_input_bundle_content_hash_invalid")
    scope = value.get("scope") if isinstance(value.get("scope"), Mapping) else {}
    research_ref = _text(scope.get("research_ref"))
    if (
        research_ref is None
        or len(research_ref) != 40
        or any(character not in "0123456789abcdef" for character in research_ref.casefold())
    ):
        errors.append("qualification_input_bundle_research_ref_invalid")
    atoms_raw = value.get("atoms")
    atoms = (
        [dict(atom) for atom in atoms_raw if isinstance(atom, Mapping)]
        if isinstance(atoms_raw, list)
        else []
    )
    if not isinstance(atoms_raw, list) or len(atoms) != len(atoms_raw):
        errors.append("qualification_input_bundle_atoms_invalid")
    else:
        try:
            receipts = _atom_receipts(atoms)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            corpus = value.get("atom_corpus")
            corpus = corpus if isinstance(corpus, Mapping) else {}
            if (
                corpus.get("count") != len(receipts)
                or corpus.get("receipts") != receipts
                or corpus.get("receipts_sha256") != _canonical_hash(receipts)
            ):
                errors.append("qualification_input_bundle_atom_corpus_mismatch")
    source = value.get("source_inputs")
    source = source if isinstance(source, Mapping) else {}
    run_manifests = [
        source.get("source_runs"),
        source.get("implementation_runs"),
    ]
    additional_contract_raw = source.get("additional_evidence_runs", [])
    if isinstance(additional_contract_raw, list):
        run_manifests.extend(additional_contract_raw)
    if schema_version == QUALIFICATION_INPUT_BUNDLE_SCHEMA_VERSION and any(
        not isinstance(manifest, Mapping)
        or manifest.get("manifest_kind") != SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND
        for manifest in run_manifests
    ):
        errors.append("qualification_input_semantic_manifest_required")
    if not verify_files:
        return list(dict.fromkeys(errors))
    for key in ("source_runs", "implementation_runs"):
        manifest = source.get(key)
        if not isinstance(manifest, Mapping):
            errors.append(f"qualification_input_tree_receipt_missing:{key}")
        else:
            errors.extend(_verify_run_evidence_manifest(manifest, name=key))
    additional_raw = source.get("additional_evidence_runs", [])
    additional = additional_raw if isinstance(additional_raw, list) else []
    if not isinstance(additional_raw, list) or any(
        not isinstance(item, Mapping) for item in additional
    ):
        errors.append("qualification_input_additional_evidence_roots_invalid")
    else:
        additional_roots: list[str] = []
        inferred_roots = {
            _text(source.get(key, {}).get("root"))
            for key in ("source_runs", "implementation_runs")
            if isinstance(source.get(key), Mapping)
        }
        scope_target = _text(scope.get("target"))
        for index, manifest in enumerate(additional):
            root_raw = _text(manifest.get("root"))
            target_slug = _text(manifest.get("target_slug"))
            expected_name = f"additional_evidence_runs:{index + 1:04d}"
            if (
                manifest.get("name") != expected_name
                or root_raw is None
                or (target_slug is None and scope_target is not None)
            ):
                errors.append(
                    f"qualification_input_additional_evidence_root_receipt_invalid:{index}"
                )
                continue
            root = Path(root_raw)
            pattern = (
                f"{target_slug}/*/*/*/target_ref.json"
                if target_slug is not None
                else "*/*/*/*/target_ref.json"
            )
            if (
                not root.is_absolute()
                or root_raw in inferred_roots
                or next(root.glob(pattern), None) is None
            ):
                errors.append(
                    f"qualification_input_additional_evidence_root_scope_invalid:{index}"
                )
            additional_roots.append(str(root.resolve()))
            errors.extend(
                _verify_run_evidence_manifest(
                    manifest,
                    name=f"additional_evidence_runs:{index}",
                )
            )
        if len(additional_roots) != len(set(additional_roots)):
            errors.append("qualification_input_additional_evidence_roots_duplicated")
    for key in ("atom_actions", "case_registry_seed"):
        receipt = source.get(key)
        if not isinstance(receipt, Mapping):
            errors.append(f"qualification_input_file_receipt_missing:{key}")
        else:
            errors.extend(_verify_file_receipt(receipt, name=key))
    owner_roots_raw = source.get("owner_roots")
    owner_roots = (
        owner_roots_raw if isinstance(owner_roots_raw, list) else []
    )
    if (
        not owner_roots
        or any(_text(item) is None for item in owner_roots)
        or len(owner_roots) != len(set(owner_roots))
        or any(
            not Path(str(item)).is_absolute()
            or not Path(str(item)).resolve().is_dir()
            for item in owner_roots
        )
    ):
        errors.append("qualification_input_owner_roots_invalid")
    owner_git_facts_raw = source.get("owner_git_facts")
    owner_git_facts = (
        owner_git_facts_raw if isinstance(owner_git_facts_raw, list) else []
    )
    if (
        len(owner_git_facts) != len(owner_roots)
        or any(not isinstance(item, Mapping) for item in owner_git_facts)
    ):
        errors.append("qualification_input_owner_git_facts_invalid")
    elif owner_roots:
        ledger_receipt = source.get("atom_actions")
        registry_receipt = source.get("case_registry_seed")
        try:
            ledger_path = Path(str(ledger_receipt["path"])).resolve()
            registry_path = Path(str(registry_receipt["path"])).resolve()
            outcome_queries = _outcome_git_queries(
                _load_structured_document(ledger_path),
                _load_structured_document(registry_path),
            )
            observed_owner_git_facts = [
                _owner_git_fact(Path(str(root)).resolve(), outcome_queries=outcome_queries)
                for root in owner_roots
            ]
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"qualification_input_owner_git_facts_unverifiable:{exc}")
        else:
            if observed_owner_git_facts != owner_git_facts:
                errors.append("qualification_input_owner_git_facts_changed")
    pipeline = value.get("pipeline")
    pipeline = pipeline if isinstance(pipeline, Mapping) else {}
    pipeline_files = pipeline.get("files")
    if not isinstance(pipeline_files, Mapping):
        errors.append("qualification_input_pipeline_receipt_missing")
    else:
        repo_root_raw = _text(pipeline_files.get("repo_root"))
        if repo_root_raw is None:
            errors.append("qualification_input_pipeline_root_missing")
        else:
            repo_root = Path(repo_root_raw).resolve()
            try:
                observed_manifest = _pipeline_manifest(repo_root)
                observed_head = _git_output(
                    repo_root,
                    "rev-parse",
                    "HEAD^{commit}",
                ).casefold()
                observed_status = _git_output(
                    repo_root,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=no",
                )
            except ValueError as exc:
                errors.append(str(exc))
            else:
                if observed_manifest != pipeline_files:
                    errors.append("qualification_input_pipeline_changed")
                if pipeline.get("git_head") != observed_head:
                    errors.append("qualification_input_pipeline_git_head_changed")
                observed_status_sha256 = sha256(
                    observed_status.encode("utf-8")
                ).hexdigest()
                if pipeline.get("git_status_sha256") != observed_status_sha256:
                    errors.append("qualification_input_pipeline_git_status_changed")
                errors.extend(
                    qualification_runtime_compatibility_errors(
                        value,
                        repo_root=repo_root,
                    )
                )
    repo_input_raw = _text(scope.get("repo_input"))
    if repo_input_raw is None or research_ref is None:
        errors.append("qualification_input_bundle_scope_invalid")
    else:
        try:
            observed_research_ref = _exact_research_revision(
                Path(repo_input_raw).resolve(),
                research_ref,
            )
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if observed_research_ref != research_ref:
                errors.append("qualification_input_research_ref_changed")
    protected_raw = value.get("protected_paths")
    protected = protected_raw if isinstance(protected_raw, list) else []
    for index, manifest in enumerate(protected):
        if not isinstance(manifest, Mapping):
            errors.append(f"qualification_protected_receipt_invalid:{index}")
            continue
        kind = manifest.get("kind")
        if kind == "file":
            errors.extend(_verify_file_receipt(manifest, name=f"protected:{index}"))
        elif kind == "tree":
            errors.extend(_verify_tree_manifest(manifest, name=f"protected:{index}"))
        else:
            errors.append(f"qualification_protected_receipt_kind_invalid:{index}")
    return list(dict.fromkeys(errors))


def write_qualification_input_bundle(
    bundle: Mapping[str, Any],
    *,
    output_root: Path,
) -> Path:
    errors = qualification_input_bundle_errors(bundle, verify_files=True)
    if errors:
        raise ValueError("qualification_input_bundle_invalid:" + ",".join(errors))
    digest = str(bundle["content_sha256"])
    path = output_root.expanduser().resolve() / digest / "qualification_input_bundle.json"
    encoded = (json.dumps(bundle, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ValueError(f"qualification_input_bundle_write_conflict:{path}")
        return path
    path.write_bytes(encoded)
    if path.read_bytes() != encoded:
        raise ValueError("qualification_input_bundle_write_verification_failed")
    return path


def load_qualification_input_bundle(path: Path, *, verify_files: bool = True) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"qualification_input_bundle_unreadable:{type(exc).__name__}"
        ) from exc
    errors = qualification_input_bundle_errors(value, verify_files=verify_files)
    if errors:
        raise ValueError("qualification_input_bundle_invalid:" + ",".join(errors))
    return dict(value)


def _load_json_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name}_unreadable:{type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name}_invalid")
    return value


def _backlog_stage_documents(backlog: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    artifacts = backlog.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    pipeline = artifacts.get("six_stage_pipeline")
    pipeline = pipeline if isinstance(pipeline, Mapping) else {}
    paths = {
        "stage1": pipeline.get("problem_records_json"),
        "stage2": pipeline.get("prioritized_problems_json"),
        "stage3": pipeline.get("research_json"),
        "stage4": pipeline.get("solution_options_json"),
        "stage5": pipeline.get("solution_selection_json"),
        "stage6": pipeline.get("change_plans_json"),
    }
    result: dict[str, dict[str, Any]] = {}
    for name, raw_path in paths.items():
        path_text = _text(raw_path)
        if path_text is None:
            raise ValueError(f"qualification_adjudication_stage_path_missing:{name}")
        result[name] = _load_json_object(Path(path_text), name=name)
    return result


def build_qualification_adjudication_template(
    *,
    backlog_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    backlog = _load_json_object(backlog_path, name="qualification_backlog")
    manifest = _load_json_object(manifest_path, name="qualification_manifest")
    artifacts = backlog.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    qualification = artifacts.get("shadow_qualification")
    qualification = qualification if isinstance(qualification, Mapping) else {}
    pending_path_raw = _text(qualification.get("pending_run_receipt_path"))
    if pending_path_raw is None:
        raise ValueError("qualification_adjudication_pending_path_missing")
    pending_path = Path(pending_path_raw).resolve()
    artifact_paths: dict[str, Path | None] = {}
    receipts_raw = _load_json_object(
        pending_path,
        name="qualification_pending_run",
    ).get("artifact_receipts")
    for receipt in receipts_raw if isinstance(receipts_raw, list) else []:
        if isinstance(receipt, Mapping) and _text(receipt.get("name")) is not None:
            raw_source = _text(receipt.get("source_path"))
            artifact_paths[str(receipt["name"])] = (
                Path(raw_source).resolve() if raw_source is not None else None
            )
    pending, pending_errors = validate_pending_shadow_run(
        pending_path=pending_path,
        backlog_path=backlog_path.expanduser().resolve(),
        artifact_paths=artifact_paths,
    )
    if pending_errors or pending is None:
        raise ValueError(
            "qualification_adjudication_pending_invalid:" + ",".join(pending_errors)
        )
    atoms_path_raw = _text(artifacts.get("atoms_jsonl"))
    if atoms_path_raw is None:
        raise ValueError("qualification_adjudication_atoms_path_missing")
    atoms_text = Path(atoms_path_raw).read_text(encoding="utf-8")
    atoms: list[dict[str, Any]] = []
    try:
        atoms_document = json.loads(atoms_text)
    except json.JSONDecodeError:
        atoms_document = None
    if isinstance(atoms_document, list):
        if any(not isinstance(item, dict) for item in atoms_document):
            raise ValueError("qualification_adjudication_atoms_invalid")
        atoms = [dict(item) for item in atoms_document]
    else:
        for line_number, line in enumerate(atoms_text.splitlines(), start=1):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(
                    f"qualification_adjudication_atom_invalid:{line_number}"
                )
            atoms.append(item)
    manifest_errors = qualification_manifest_errors(manifest, atoms=atoms)
    if manifest_errors:
        raise ValueError(
            "qualification_adjudication_manifest_invalid:" + ",".join(manifest_errors)
        )
    stages = _backlog_stage_documents(backlog)
    accepted = qualification_accepted_outputs(backlog=backlog, **stages)
    source_binding: dict[str, Any] = {}
    if qualification.get("same_corpus_feedback_exposed") is True:
        source_adjudication_path_raw = _text(
            qualification.get("source_qualification_output_adjudication_path")
        )
        source_adjudication_sha256 = _text(
            qualification.get("source_qualification_output_adjudication_sha256")
        )
        source_report_path_raw = _text(qualification.get("source_correction_report_path"))
        source_report_sha256 = _text(qualification.get("source_correction_report_sha256"))
        if (
            source_adjudication_path_raw is None
            or not _valid_sha256(source_adjudication_sha256)
            or source_report_path_raw is None
            or not _valid_sha256(source_report_sha256)
        ):
            raise ValueError("qualification_adjudication_source_correction_binding_missing")
        source_adjudication_path = Path(source_adjudication_path_raw).resolve()
        source_report_path = Path(source_report_path_raw).resolve()
        if (
            not source_adjudication_path.is_file()
            or _file_sha256(source_adjudication_path) != source_adjudication_sha256
            or not source_report_path.is_file()
            or _file_sha256(source_report_path) != source_report_sha256
        ):
            raise ValueError("qualification_adjudication_source_correction_binding_changed")
        source_adjudication = _load_json_object(
            source_adjudication_path,
            name="qualification_source_output_adjudication",
        )
        source_report = _load_json_object(
            source_report_path,
            name="qualification_source_correction_report",
        )
        report_raw = source_report.get("report")
        report = report_raw if isinstance(report_raw, Mapping) else {}
        report_qualification_raw = report.get("qualification")
        report_qualification = (
            report_qualification_raw
            if isinstance(report_qualification_raw, Mapping)
            else {}
        )
        routes_raw = report_qualification.get("correction_routes")
        routes = (
            [dict(item) for item in routes_raw if isinstance(item, Mapping)]
            if isinstance(routes_raw, list)
            else []
        )
        findings = qualification_source_correction_findings(
            source_adjudication=source_adjudication,
            source_adjudication_sha256=str(source_adjudication_sha256),
            manifest=manifest,
            correction_routes=routes,
        )
        recorded_findings_raw = qualification.get("source_correction_findings")
        recorded_findings = (
            [dict(item) for item in recorded_findings_raw if isinstance(item, Mapping)]
            if isinstance(recorded_findings_raw, list)
            else []
        )
        if (
            findings != recorded_findings
            or qualification.get("source_correction_findings_sha256")
            != _canonical_hash(findings)
            or qualification_source_correction_findings_errors(
                findings,
                source_adjudication_sha256=str(source_adjudication_sha256),
            )
        ):
            raise ValueError("qualification_adjudication_source_correction_findings_changed")
        source_binding = {
            "source_adjudication_sha256": source_adjudication_sha256,
            "source_correction_findings": findings,
            "source_correction_findings_sha256": _canonical_hash(findings),
        }
    template: dict[str, Any] = {
        "schema_version": QUALIFICATION_ADJUDICATION_TEMPLATE_SCHEMA_VERSION,
        "contract_kind": "qualification_output_adjudication_template",
        # Reuse the immutable phase-one timestamp. Operator retries over identical
        # inputs must produce byte-identical templates rather than write conflicts.
        "created_at": pending.get("generated_at"),
        "backlog_path": str(backlog_path.expanduser().resolve()),
        "backlog_sha256": _file_sha256(backlog_path.expanduser().resolve()),
        "pending_run_sha256": pending["content_sha256"],
        "qualification_manifest": manifest,
        "accepted_outputs_by_kind": accepted,
        **source_binding,
        "decision_contract": {
            "output_adjudications": "one row for every accepted output",
            "false_rejections": "one row for every actionable source group not recovered",
            **(
                {
                    "source_correction_resolutions": (
                        "one explicit resolved, partially_resolved, unresolved, or superseded "
                        "row per immutable source finding, with rationale and exact repaired-"
                        "output references; unresolved may omit a reference when none exists"
                    ),
                    "source_correction_output_links": (
                        "when a current output finding is the same residual, include its "
                        "source_correction_finding_ids; do not link a genuinely new defect"
                    ),
                }
                if source_binding
                else {}
            ),
        },
    }
    template["content_sha256"] = _content_hash(template)
    return template


def finalize_qualification_adjudication(
    *,
    template: Mapping[str, Any],
    decisions: Mapping[str, Any],
    adjudicator: str,
    method: str,
) -> dict[str, Any]:
    if (
        template.get("schema_version") != QUALIFICATION_ADJUDICATION_TEMPLATE_SCHEMA_VERSION
        or template.get("contract_kind") != "qualification_output_adjudication_template"
        or template.get("content_sha256") != _content_hash(template)
    ):
        raise ValueError("qualification_adjudication_template_invalid")
    accepted_raw = template.get("accepted_outputs_by_kind")
    accepted = accepted_raw if isinstance(accepted_raw, Mapping) else {}
    manifest_raw = template.get("qualification_manifest")
    manifest = manifest_raw if isinstance(manifest_raw, Mapping) else {}
    adjudications_raw = decisions.get("output_adjudications")
    false_rejections_raw = decisions.get("false_rejections", [])
    source_resolutions_raw = decisions.get("source_correction_resolutions", [])
    if (
        not isinstance(adjudications_raw, list)
        or not isinstance(false_rejections_raw, list)
        or not isinstance(source_resolutions_raw, list)
    ):
        raise ValueError("qualification_adjudication_decisions_invalid")
    return dict(
        build_qualification_output_adjudication(
            manifest=manifest,
            accepted_outputs_by_kind={
                str(kind): [dict(item) for item in values if isinstance(item, Mapping)]
                for kind, values in accepted.items()
                if isinstance(values, list)
            },
            output_adjudications=[
                dict(item) for item in adjudications_raw if isinstance(item, Mapping)
            ],
            false_rejections=[
                dict(item) for item in false_rejections_raw if isinstance(item, Mapping)
            ],
            pending_run_sha256=str(template.get("pending_run_sha256") or ""),
            adjudicator=adjudicator,
            method=method,
            source_adjudication_sha256=_text(
                template.get("source_adjudication_sha256")
            ),
            source_correction_findings=[
                dict(item)
                for item in template.get("source_correction_findings", [])
                if isinstance(item, Mapping)
            ],
            source_correction_resolutions=[
                dict(item) for item in source_resolutions_raw if isinstance(item, Mapping)
            ],
        )
    )


def _write_json_once(path: Path, value: Mapping[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    encoded = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.exists() and resolved.read_bytes() != encoded:
        raise ValueError(f"qualification_transaction_write_conflict:{resolved}")
    if not resolved.exists():
        resolved.write_bytes(encoded)
    return resolved


def _copy_file_once(source: Path, destination: Path, *, label: str) -> Path:
    source_resolved = source.expanduser().resolve()
    destination_resolved = destination.expanduser().resolve()
    try:
        source_bytes = source_resolved.read_bytes()
    except OSError as exc:
        raise ValueError(f"qualification_{label}_source_unreadable:{source_resolved}") from exc
    destination_resolved.parent.mkdir(parents=True, exist_ok=True)
    if destination_resolved.exists():
        if destination_resolved.read_bytes() != source_bytes:
            raise ValueError(
                f"qualification_{label}_copy_conflict:{destination_resolved}"
            )
    else:
        destination_resolved.write_bytes(source_bytes)
    if source_resolved.read_bytes() != source_bytes:
        raise ValueError(f"qualification_{label}_source_changed_during_copy")
    return destination_resolved


def _cmd_reports_qualification_prepare(args: argparse.Namespace) -> int:
    """Delegate deterministic atom preparation to the canonical backlog extractor."""

    from usertest_backlog.workflows.staged import _cmd_reports_backlog

    copied_atom_actions = _copy_file_once(
        args.atom_actions_yaml,
        args.work_dir / "backlog_atom_actions.seed.yaml",
        label="atom_actions",
    )
    namespace = argparse.Namespace(
        target=args.target,
        repo_input=str(args.repo_input),
        research_ref=args.research_ref,
        runs_dir=args.source_runs_dir,
        stage_runs_dir=None,
        out_json=args.work_dir / "prepared.backlog.json",
        out_md=args.work_dir / "prepared.backlog.md",
        repo_root=args.repo_root,
        prompts_dir=None,
        breadth_profile=args.breadth_profile,
        agent="codex",
        model=None,
        miners=10,
        sample_size=120,
        coverage_miners=3,
        bagging_miners=None,
        max_tickets_per_miner=12,
        force=False,
        resume=True,
        seed=0,
        no_merge=False,
        merge_candidate_threshold=0.65,
        merge_keep_anchor_pairs=False,
        orphan_pass=1,
        dry_run=False,
        shadow=False,
        score_shadow=False,
        operational_shadow=False,
        score_operational_shadow=False,
        qualification_corpus_manifest=None,
        qualification_manifest_sha256=None,
        qualification_output_adjudication=None,
        no_actionable_evidence_receipt=None,
        qualification_input_bundle=None,
        qualification_cycle_root=None,
        shadow_state=None,
        qualification_prepare_out=args.out_root,
        qualification_case_registry_seed=args.case_registry_seed,
        qualification_protected_path=args.protected_path,
        qualification_additional_evidence_runs_dir=(
            args.additional_evidence_runs_dir
        ),
        labelers=3,
        policy_config=None,
        no_policy=False,
        atom_actions_yaml=copied_atom_actions,
        carryover_actioned_only=False,
        exclude_atom_status=None,
        skip_plan_folder_sync=True,
    )
    return _cmd_reports_backlog(namespace)


def _cmd_reports_qualification_adjudication_template(args: argparse.Namespace) -> int:
    template = build_qualification_adjudication_template(
        backlog_path=args.backlog_json,
        manifest_path=args.qualification_corpus_manifest,
    )
    path = _write_json_once(args.out_json, template)
    print(path)
    print(json.dumps({"content_sha256": template["content_sha256"]}, indent=2))
    return 0


def _cmd_reports_qualification_adjudication_finalize(args: argparse.Namespace) -> int:
    template = _load_json_object(args.template, name="qualification_adjudication_template")
    decisions = _load_json_object(args.decisions, name="qualification_adjudication_decisions")
    adjudication = finalize_qualification_adjudication(
        template=template,
        decisions=decisions,
        adjudicator=args.adjudicator,
        method=args.method,
    )
    path = _write_json_once(args.out_json, adjudication)
    print(path)
    print(json.dumps({"content_sha256": adjudication["content_sha256"]}, indent=2))
    return 0


__all__ = [
    "QUALIFICATION_ADJUDICATION_TEMPLATE_SCHEMA_VERSION",
    "QUALIFICATION_INPUT_BUNDLE_SCHEMA_VERSION",
    "_cmd_reports_qualification_adjudication_finalize",
    "_cmd_reports_qualification_adjudication_template",
    "_cmd_reports_qualification_prepare",
    "build_qualification_adjudication_template",
    "build_qualification_input_bundle",
    "capture_qualification_preparation_snapshot",
    "capture_qualification_source_snapshot",
    "current_pipeline_runtime_compatibility",
    "extend_qualification_preparation_snapshot",
    "finalize_qualification_adjudication",
    "load_qualification_input_bundle",
    "qualification_input_bundle_errors",
    "qualification_runtime_compatibility_errors",
    "write_qualification_input_bundle",
]
