# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

import re
from collections.abc import Mapping

from backlog_core import (
    SOURCE_EVIDENCE_PROJECTION_VERSION,
    derived_source_atom_id_aliases,
    operational_candidate_receipt_errors,
    provisional_same_cause_group_errors,
    source_evidence_atom_projection,
    source_evidence_atom_sha256,
)
from backlog_core.stage_contracts import evidence_assignment_sha256
from backlog_miner.origin_evidence import (
    RESEARCH_RUN_CONTEXT_FILES,
    source_observation_classification,
)
from backlog_miner.research_evidence import (
    BlockedReplayExecutor,
    DockerReplayExecutor,
    PlatformRoutingReplayExecutor,
    ReplayExecutor,
    TrustedHostReplayExecutor,
)
from backlog_miner.research_runner import (
    _authenticate_assignment_source_classifications,
    _completed_prefix_checkpoint,
    _materialize_terminal_research_validation_error_rescore,
    _persisted_source_evidence_assignment_sha256,
    _resume_completed_prefix_from_stage_document,
    _source_evidence_assignment_sha256,
    _valid_stage3_research_compatibility_contract,
    _validated_completed_stage3_checkpoint,
    completed_stage3_checkpoint,
    stage3_research_compatibility_contract,
)

from usertest_backlog.shared import *
from usertest_backlog.workflows.post_research_relations import (
    authenticated_split_child_occurrence_evidence,
)

_REPLAY_EXECUTOR_MODES = frozenset({"blocked", "docker", "platform_router", "trusted_host"})
_REPLAY_REPO_INPUT_ROOT = "${repo_input}"
_OPERATIONAL_CANDIDATE_ID_RE = re.compile(
    r"^operational_failure:(?P<signature>[0-9a-f]{64}):(?P<occurrence_set>[0-9a-f]{64})$"
)

# Canonical run-local context that Stage 3 hashes for every assigned origin atom.
# Qualification source custody imports the same declaration; keep this as the single
# source of truth instead of duplicating the list in the snapshot implementation.
ORIGIN_EVIDENCE_RUN_ARTIFACT_RELATIVE_PATHS: tuple[str, ...] = (
    "preflight.json",
    "agent_shell_probe/raw_events.jsonl",
    "agent_attempts.json",
    "settings_ref.json",
    "effective_run_spec.json",
    "report.json",
    "error.json",
    "report_validation_errors.json",
    "normalized_events.jsonl",
    "raw_events.jsonl",
    "metrics.json",
    "workspace_ref.json",
    "target_ref.json",
    "run_meta.json",
)


def _atomic_write_research_json(path: Path, document: Mapping[str, Any]) -> None:
    """Durably replace a Stage-3 document without exposing a partial JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _configured_replay_executor(
    *,
    research_config: dict[str, Any],
    repo_root: Path,
    repo_input: str | None,
) -> tuple[ReplayExecutor, dict[str, Any]]:
    """Build the explicit stage-3 replay boundary from repo-owned configuration.

    A missing mode remains fail-closed for checkouts that have not adopted the
    replay contract.  When a mode is configured, its required fields are
    validated before any research agent is launched.
    """
    mode_raw = research_config.get("replay_executor")
    if mode_raw is None:
        executor = BlockedReplayExecutor(reason="backlog_research.replay_executor_missing")
        return executor, {
            "executor": "blocked",
            "reason": "backlog_research.replay_executor_missing",
        }
    if not isinstance(mode_raw, str) or mode_raw.strip() not in _REPLAY_EXECUTOR_MODES:
        choices = "|".join(sorted(_REPLAY_EXECUTOR_MODES))
        raise ValueError(f"backlog_research.replay_executor must be one of {choices}")
    mode = mode_raw.strip()

    image_raw = research_config.get("replay_docker_image")
    roots_raw = research_config.get("replay_trusted_host_roots")
    if mode == "platform_router":
        if not isinstance(image_raw, str) or not image_raw.strip():
            raise ValueError(
                "backlog_research.replay_docker_image is required for "
                "replay_executor=platform_router"
            )
        if not isinstance(roots_raw, list) or not roots_raw:
            raise ValueError(
                "backlog_research.replay_trusted_host_roots must be a non-empty "
                "list for replay_executor=platform_router"
            )
        if not isinstance(repo_input, str) or not repo_input.strip():
            raise ValueError("replay_executor=platform_router requires a local --repo-input path")
        source_identity = Path(repo_input.strip()).expanduser()
        if not source_identity.is_absolute():
            source_identity = repo_root / source_identity
        source_identity = source_identity.resolve()
        roots: list[Path] = []
        for index, value in enumerate(roots_raw):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "backlog_research.replay_trusted_host_roots"
                    f"[{index}] must be a non-empty path string"
                )
            root_value = value.strip()
            if root_value == _REPLAY_REPO_INPUT_ROOT:
                candidate = source_identity
            else:
                candidate = Path(root_value).expanduser()
                if not candidate.is_absolute():
                    candidate = repo_root / candidate
                candidate = candidate.resolve()
            if not candidate.is_dir():
                raise ValueError(
                    "backlog_research.replay_trusted_host_roots"
                    f"[{index}] is not an existing directory: {candidate}"
                )
            roots.append(candidate)
        if not source_identity.is_dir() or not any(
            source_identity == root or _path_is_within(source_identity, root) for root in roots
        ):
            raise ValueError(
                "platform router repo_input is not an existing repository within "
                f"replay_trusted_host_roots: {source_identity}"
            )
        host = TrustedHostReplayExecutor(
            approved_source_roots=list(dict.fromkeys(roots)),
            source_identity=source_identity,
        )
        docker = DockerReplayExecutor(image_ref=image_raw.strip())
        host_platform = str(
            host.isolation_receipt(source_workspace=source_identity).get("platform") or "unknown"
        )
        routes: dict[str, ReplayExecutor] = {"linux": docker}
        routes[host_platform] = host
        return PlatformRoutingReplayExecutor(
            default_executor=docker,
            platform_executors=routes,
        ), {
            "executor": "platform_router",
            "default_executor": "docker",
            "docker_image": image_raw.strip(),
            "approved_source_roots": [str(path) for path in dict.fromkeys(roots)],
            "source_identity": str(source_identity),
            "platform_routes": {
                requirement: type(executor).__name__ for requirement, executor in routes.items()
            },
        }
    if mode == "docker":
        if not isinstance(image_raw, str) or not image_raw.strip():
            raise ValueError(
                "backlog_research.replay_docker_image is required for replay_executor=docker"
            )
        image = image_raw.strip()
        if any(character.isspace() or ord(character) < 32 for character in image):
            raise ValueError(
                "backlog_research.replay_docker_image must be one Docker image reference"
            )
        if roots_raw not in (None, []):
            raise ValueError(
                "backlog_research.replay_trusted_host_roots is only valid for "
                "replay_executor=trusted_host"
            )
        return DockerReplayExecutor(image_ref=image), {
            "executor": "docker",
            "docker_image": image,
            "network": "none",
            "host_environment": "not_forwarded",
        }

    if mode == "trusted_host":
        if image_raw not in (None, ""):
            raise ValueError(
                "backlog_research.replay_docker_image is only valid for replay_executor=docker"
            )
        if not isinstance(roots_raw, list) or not roots_raw:
            raise ValueError(
                "backlog_research.replay_trusted_host_roots must be a non-empty list "
                "for replay_executor=trusted_host"
            )
        if not isinstance(repo_input, str) or not repo_input.strip():
            raise ValueError("replay_executor=trusted_host requires a local --repo-input path")
        source_identity = Path(repo_input.strip()).expanduser()
        if not source_identity.is_absolute():
            source_identity = repo_root / source_identity
        source_identity = source_identity.resolve()
        roots: list[Path] = []
        for index, value in enumerate(roots_raw):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "backlog_research.replay_trusted_host_roots"
                    f"[{index}] must be a non-empty path string"
                )
            root_value = value.strip()
            if root_value == _REPLAY_REPO_INPUT_ROOT:
                candidate = source_identity
            else:
                candidate = Path(root_value).expanduser()
                if not candidate.is_absolute():
                    candidate = repo_root / candidate
                candidate = candidate.resolve()
            if not candidate.is_dir():
                raise ValueError(
                    "backlog_research.replay_trusted_host_roots"
                    f"[{index}] is not an existing directory: {candidate}"
                )
            roots.append(candidate)
        unique_roots = list(dict.fromkeys(roots))
        if not source_identity.is_dir():
            raise ValueError(
                "replay_executor=trusted_host requires an existing local repository: "
                f"{source_identity}"
            )
        if not any(
            source_identity == root or _path_is_within(source_identity, root)
            for root in unique_roots
        ):
            raise ValueError(
                f"trusted host repo_input is outside replay_trusted_host_roots: {source_identity}"
            )
        return TrustedHostReplayExecutor(
            approved_source_roots=unique_roots,
            source_identity=source_identity,
        ), {
            "executor": "trusted_host",
            "approved_source_roots": [str(path) for path in unique_roots],
            "source_identity": str(source_identity),
            "network": "not_enforced",
            "host_environment": "sanitized",
        }

    if image_raw not in (None, "") or roots_raw not in (None, []):
        raise ValueError(
            "blocked replay_executor cannot configure replay_docker_image or "
            "replay_trusted_host_roots"
        )
    executor = BlockedReplayExecutor(reason="backlog_research.replay_executor_blocked")
    return executor, {
        "executor": "blocked",
        "reason": "backlog_research.replay_executor_blocked",
    }


def _research_file_receipt(path: Path, *, run_dir: Path | None = None) -> dict[str, Any]:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    receipt = {
        "path": str(path.resolve()),
        "sha256": digest.hexdigest(),
        "size_bytes": path.stat().st_size,
    }
    if run_dir is not None:
        try:
            relative = path.resolve().relative_to(run_dir.resolve())
        except ValueError:
            relative = None
        if relative is not None:
            relative_path = relative.as_posix()
            receipt["source_relpath"] = relative_path
            role = RESEARCH_RUN_CONTEXT_FILES.get(relative_path)
            if role is not None:
                receipt["research_context_role"] = role
    return receipt


def _origin_artifact_receipts(atom: dict[str, Any], *, repo_root: Path) -> list[dict[str, Any]]:
    """Hash the retained source artifacts that make an atom auditable."""
    run_dir_raw = atom.get("run_dir")
    if not isinstance(run_dir_raw, str) or not run_dir_raw.strip():
        return []
    run_dir = Path(run_dir_raw)
    if not run_dir.is_absolute():
        run_dir = repo_root / run_dir
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        return []

    candidates: list[Path] = []
    artifacts_raw = atom.get("artifacts")
    if isinstance(artifacts_raw, dict):
        for value in artifacts_raw.values():
            if not isinstance(value, str) or not value.strip():
                continue
            candidate = Path(value)
            candidates.append(candidate if candidate.is_absolute() else run_dir / candidate)
    artifact_ref_raw = atom.get("artifact_ref")
    artifact_ref = artifact_ref_raw if isinstance(artifact_ref_raw, dict) else {}
    artifact_path_raw = artifact_ref.get("path")
    if isinstance(artifact_path_raw, str) and artifact_path_raw.strip():
        candidate = Path(artifact_path_raw)
        candidates.append(candidate if candidate.is_absolute() else run_dir / candidate)
    attachments_raw = atom.get("attachments")
    for attachment in attachments_raw if isinstance(attachments_raw, list) else []:
        if not isinstance(attachment, dict):
            continue
        attachment_ref_raw = attachment.get("artifact_ref")
        attachment_ref = attachment_ref_raw if isinstance(attachment_ref_raw, dict) else {}
        attachment_path_raw = attachment_ref.get("path")
        if not isinstance(attachment_path_raw, str) or not attachment_path_raw.strip():
            continue
        candidate = Path(attachment_path_raw)
        candidates.append(candidate if candidate.is_absolute() else run_dir / candidate)

    status = str(
        atom.get("status") or atom.get("report_status") or atom.get("outcome") or ""
    ).casefold()
    source = str(atom.get("source") or "").casefold()
    failure_atom = (
        status in {"failed", "failure", "error", "partial", "blocked"}
        or any(marker in source for marker in ("failure", "error", "stderr"))
        or bool(attachments_raw)
    )
    if failure_atom:
        # Older retained failure records do not always project the attachment
        # references onto each atom.  Hash the canonical full streams when they
        # exist so research receives the actual diagnostic, not only an excerpt.
        candidates.extend([run_dir / "agent_stderr.txt", run_dir / "agent_last_message.txt"])
    for name in ORIGIN_EVIDENCE_RUN_ARTIFACT_RELATIVE_PATHS:
        candidates.append(run_dir / name)

    receipts: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            if candidate.is_symlink():
                continue
            resolved = candidate.resolve()
            resolved.relative_to(run_dir)
        except (OSError, ValueError):
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        receipts.append(_research_file_receipt(resolved, run_dir=run_dir))
    return receipts


def _evidence_assignment(
    *,
    case_id: str,
    problem_id: str,
    evidence_atom_ids: list[str],
    evidence_atoms: list[dict[str, Any]],
    repo_root: Path,
) -> tuple[dict[str, Any], list[str]]:
    receipts: list[dict[str, Any]] = []
    missing: list[str] = []
    by_id = {
        str(atom.get("atom_id")): atom
        for atom in evidence_atoms
        if isinstance(atom.get("atom_id"), str)
    }
    for atom_id in evidence_atom_ids:
        atom = by_id.get(atom_id)
        if atom is None:
            missing.append(atom_id)
            continue
        artifacts = _origin_artifact_receipts(atom, repo_root=repo_root)
        atom_projection = source_evidence_atom_projection(atom)
        receipts.append(
            {
                "atom_id": atom_id,
                "atom_sha256": source_evidence_atom_sha256(atom),
                "atom_snapshot": atom_projection,
                "source_projection_version": SOURCE_EVIDENCE_PROJECTION_VERSION,
                "source_classification": source_observation_classification(atom),
                "artifact_receipts": artifacts,
                "origin_evidence_mode": (
                    "snapshot_and_artifacts" if artifacts else "signed_snapshot"
                ),
            }
        )
    assignment: dict[str, Any] = {
        "status": "incomplete" if missing else "complete",
        "errors": [f"origin_evidence_unavailable:{item}" for item in missing],
        "case_id": case_id,
        "problem_id": problem_id,
        "expected_atom_ids": list(evidence_atom_ids),
        "atom_receipts": receipts,
    }
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    return assignment, missing


def _expand_operational_candidate_evidence(
    *,
    evidence_atom_ids: list[str],
    atoms_by_id: Mapping[str, dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    """Attach the observed occurrences behind verified operational candidates.

    Operational candidates intentionally expose only a compact typed-signal projection to
    problem mining.  Stage 3 needs the underlying occurrence atoms, however, or it can prove
    only that the classifier emitted a candidate rather than establish the original failure's
    locus, actionability, or causal mechanism.  The runner-owned candidate receipt is the
    authority for this one-hop expansion; arbitrary model-authored lineage is not followed.
    """

    expanded_ids = list(dict.fromkeys(evidence_atom_ids))
    occurrence_ids: list[str] = []
    errors: list[str] = []
    for atom_id in evidence_atom_ids:
        atom = atoms_by_id.get(atom_id)
        if atom is None or atom.get("source") != "operational_failure_candidate":
            continue
        receipt_errors = operational_candidate_receipt_errors(atom)
        if receipt_errors:
            errors.extend(
                f"operational_candidate_lineage_invalid:{atom_id}:{error}"
                for error in receipt_errors
            )
            continue
        receipt_raw = atom.get("operational_candidate_receipt")
        receipt = receipt_raw if isinstance(receipt_raw, Mapping) else {}
        source_ids_raw = receipt.get("source_derived_atom_ids")
        source_ids = (
            [value.strip() for value in source_ids_raw if isinstance(value, str) and value.strip()]
            if isinstance(source_ids_raw, list)
            else []
        )
        if not source_ids:
            errors.append(f"operational_candidate_lineage_empty:{atom_id}")
            continue
        for source_id in source_ids:
            if source_id not in occurrence_ids:
                occurrence_ids.append(source_id)
            if source_id not in expanded_ids:
                expanded_ids.append(source_id)
    return expanded_ids, occurrence_ids, errors


def _select_current_operational_candidate_evidence(
    *,
    evidence_atom_ids: list[str],
    atoms_by_id: Mapping[str, dict[str, Any]],
) -> tuple[list[str], list[dict[str, str]]]:
    """Use the one verified current revision of a cited operational aggregate.

    An operational candidate's stable failure signature identifies the case while its
    second identity component identifies the exact occurrence-set revision.  The durable
    case graph intentionally retains prior revision IDs, but a later atom corpus contains
    only the currently materialized aggregate.  Requiring every historical revision to be
    rematerialized makes an expanded occurrence set look like missing evidence and parks
    Stage 3 before research can begin.

    This adapter is deliberately narrow.  It replaces a missing prior revision only when
    the same problem record already cites exactly one available candidate with the same
    authenticated stable signature.  Different signatures, invalid receipts, and multiple
    available revisions remain unresolved and therefore keep the assignment incomplete.
    The returned alias records make the active/historical boundary explicit and hash-bound;
    the historical atom ID remains in the durable case graph.
    """

    available_by_signature: dict[str, list[str]] = {}
    for atom_id in evidence_atom_ids:
        atom = atoms_by_id.get(atom_id)
        match = _OPERATIONAL_CANDIDATE_ID_RE.fullmatch(atom_id)
        if (
            atom is None
            or match is None
            or atom.get("source") != "operational_failure_candidate"
            or operational_candidate_receipt_errors(atom)
        ):
            continue
        signature = match.group("signature")
        if atom.get("operational_candidate_signature") != signature:
            continue
        available_by_signature.setdefault(signature, [])
        if atom_id not in available_by_signature[signature]:
            available_by_signature[signature].append(atom_id)

    selected_ids: list[str] = []
    aliases: list[dict[str, str]] = []
    for atom_id in evidence_atom_ids:
        selected_id = atom_id
        if atom_id not in atoms_by_id:
            match = _OPERATIONAL_CANDIDATE_ID_RE.fullmatch(atom_id)
            if match is not None:
                signature = match.group("signature")
                available = available_by_signature.get(signature, [])
                if len(available) == 1:
                    selected_id = available[0]
                    aliases.append(
                        {
                            "historical_atom_id": atom_id,
                            "current_atom_id": selected_id,
                            "candidate_signature": signature,
                            "authority": ("verified_cited_operational_candidate_signature"),
                        }
                    )
        if selected_id not in selected_ids:
            selected_ids.append(selected_id)
    return selected_ids, aliases


def _select_current_derived_source_evidence(
    *,
    evidence_atom_ids: list[str],
    atoms_by_id: Mapping[str, dict[str, Any]],
) -> tuple[list[str], list[dict[str, str]]]:
    """Resolve a durable derived-run ID to its exact content-addressed identity.

    Derived-run ingestion changed from path-shaped IDs to content-addressed IDs.  The
    case graph correctly retains both history and the current observation, but Stage 3
    must not treat the old spelling as missing evidence when exactly one current atom
    authenticates that durable alias from runner-owned source fields.  Ambiguous or
    malformed aliases remain unresolved and therefore retain the normal evidence gate.
    """

    current_by_alias: dict[str, list[str]] = {}
    for current_id, atom in atoms_by_id.items():
        for alias in derived_source_atom_id_aliases(atom):
            current_by_alias.setdefault(alias, [])
            if current_id not in current_by_alias[alias]:
                current_by_alias[alias].append(current_id)

    selected_ids: list[str] = []
    aliases: list[dict[str, str]] = []
    for atom_id in evidence_atom_ids:
        selected_id = atom_id
        if atom_id not in atoms_by_id:
            available = current_by_alias.get(atom_id, [])
            if len(available) == 1:
                selected_id = available[0]
                aliases.append(
                    {
                        "historical_atom_id": atom_id,
                        "current_atom_id": selected_id,
                        "current_atom_sha256": source_evidence_atom_sha256(
                            atoms_by_id[selected_id]
                        ),
                        "authority": "content_addressed_derived_source_alias",
                    }
                )
        if selected_id not in selected_ids:
            selected_ids.append(selected_id)
    return selected_ids, aliases


def _initial_research_evidence_roles(
    evidence_atom_ids: list[str],
    *,
    atoms_by_id: Mapping[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Separate aggregate case signals from the occurrences research must explain.

    A normal source atom is itself an occurrence.  The only current aggregate
    evidence contract is the runner-minted operational candidate, whose authenticated
    receipt is expanded separately.  This keeps splitting general: it works for any
    case with multiple direct observations, not only for operational aggregates.
    """

    case_evidence_ids: list[str] = []
    occurrence_evidence_ids: list[str] = []
    for atom_id in evidence_atom_ids:
        atom = atoms_by_id.get(atom_id)
        if isinstance(atom, Mapping) and atom.get("source") == "operational_failure_candidate":
            case_evidence_ids.append(atom_id)
        else:
            occurrence_evidence_ids.append(atom_id)
    return case_evidence_ids, occurrence_evidence_ids


def _provisional_same_cause_source_evidence(
    problem_record: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    """Return the complete authenticated evidence boundary for a provisional group.

    A provisional same-cause group is one research unit, not an alias.  Its member
    facets preserve the evidence that made each candidate case independently real.
    Stage 3 must receive that union or it can neither test the shared-mechanism
    hypothesis nor clear it honestly.  Cross-check the runner-owned group against the
    canonical record before trusting facet membership; inconsistent packets block the
    assignment instead of silently narrowing or widening it.
    """

    if problem_record.get("case_identity_status") != "provisional_same_cause":
        return [], []

    case_id_raw = problem_record.get("case_id")
    case_id = case_id_raw.strip() if isinstance(case_id_raw, str) else ""
    group_raw = problem_record.get("provisional_same_cause_group")
    group = group_raw if isinstance(group_raw, Mapping) else {}
    errors = provisional_same_cause_group_errors(
        group_raw,
        owning_case_id=case_id or None,
    )

    def strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(
            dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip())
        )

    record_case_ids = strings(problem_record.get("case_identity_candidate_ids"))
    group_case_ids = strings(group.get("member_case_ids"))
    if set(record_case_ids) != set(group_case_ids):
        errors.append("provisional_same_cause_record_case_members_mismatch")

    record_problem_ids = strings(problem_record.get("case_member_problem_ids"))
    group_problem_ids = strings(group.get("member_problem_ids"))
    if set(record_problem_ids) != set(group_problem_ids):
        errors.append("provisional_same_cause_record_problem_members_mismatch")

    facets_raw = group.get("member_facets")
    facets = (
        [dict(item) for item in facets_raw if isinstance(item, Mapping)]
        if isinstance(facets_raw, list)
        else []
    )
    facet_pairs: list[tuple[str, str]] = []
    for facet in facets:
        facet_case_raw = facet.get("case_id")
        facet_problem_raw = facet.get("problem_id")
        facet_case_id = facet_case_raw.strip() if isinstance(facet_case_raw, str) else ""
        facet_problem_id = facet_problem_raw.strip() if isinstance(facet_problem_raw, str) else ""
        if not facet_problem_id:
            errors.append(
                f"provisional_same_cause_facet_problem_missing:{facet_case_id or '(missing)'}"
            )
        facet_pairs.append((facet_case_id, facet_problem_id))
    facet_problem_ids = [problem_id for _, problem_id in facet_pairs if problem_id]
    if (
        len(facet_pairs) != len(set(facet_pairs))
        or set(facet_problem_ids) != set(group_problem_ids)
        or len(facet_problem_ids) != len(group_problem_ids)
    ):
        errors.append("provisional_same_cause_facet_problem_members_mismatch")

    if errors:
        return [], list(
            dict.fromkeys(f"provisional_same_cause_group_invalid:{error}" for error in errors)
        )

    evidence_atom_ids = [
        atom_id for facet in facets for atom_id in strings(facet.get("source_evidence_atom_ids"))
    ]
    return list(dict.fromkeys(evidence_atom_ids)), []


def _render_research_dossiers_markdown(
    research_dossiers: list[dict[str, Any]],
    *,
    problem_records_by_id: dict[str, dict[str, Any]],
    title: str = "Research Dossiers",
) -> str:
    """Render stage-3 research dossiers as a human-readable Markdown document."""
    lines: list[str] = [f"# {title}\n"]
    if not research_dossiers:
        lines.append("_No research dossiers produced._\n")
        return "\n".join(lines)

    for dossier in research_dossiers:
        pid = dossier.get("problem_id") or "(no id)"
        rec = problem_records_by_id.get(str(pid)) or {}
        rec_title = rec.get("title") or pid
        status = dossier.get("reproduction_status") or "unknown"
        research_status = dossier.get("research_status") or "unknown"
        repo_revision = dossier.get("repo_revision") or "unknown"
        diff_cls = dossier.get("diff_classification") or "unknown"
        impl = dossier.get("implementation_performed")
        impl_s = "true" if impl is True else "false" if impl is False else "?"

        lines.append(f"## {rec_title}")
        lines.append(
            f"**ID**: `{pid}` | **Reproduction**: `{status}` | "
            f"**Research status**: `{research_status}` | **Diff**: `{diff_cls}` | "
            f"**Implementation performed**: {impl_s}\n"
        )
        lines.append(f"- Repository revision: `{repo_revision}`")

        writes_used = dossier.get("writes_used")
        writes_used_s = "true" if writes_used is True else "false" if writes_used is False else "?"
        writes_purpose = dossier.get("writes_purpose") or []
        purpose_list = (
            [p for p in writes_purpose if isinstance(p, str) and p.strip()]
            if isinstance(writes_purpose, list)
            else []
        )
        purpose_s = ", ".join(f"`{p}`" for p in purpose_list) if purpose_list else "`(none)`"
        lines.append(f"- Writes used: `{writes_used_s}`; purpose: {purpose_s}")

        broader = dossier.get("broader_class_assessment")
        if isinstance(broader, str) and broader.strip():
            lines.append(f"- Broader class assessment: `{broader.strip()}`")

        diff_reasons = dossier.get("diff_suspicious_reasons") or []
        diff_reasons_list = (
            [r for r in diff_reasons if isinstance(r, str) and r.strip()]
            if isinstance(diff_reasons, list)
            else []
        )
        if diff_reasons_list:
            lines.append("- Diff notes:")
            for r in diff_reasons_list[:12]:
                lines.append(f"  - {r}")

        hypotheses_raw = dossier.get("root_cause_hypotheses")
        hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
        if hypotheses:
            lines.append("- Root cause hypotheses:")
            for hypothesis in hypotheses[:8]:
                if isinstance(hypothesis, dict):
                    statement = hypothesis.get("statement")
                    hypothesis_id = hypothesis.get("hypothesis_id")
                    if isinstance(statement, str) and statement.strip():
                        prefix = f"`{hypothesis_id}`: " if hypothesis_id else ""
                        lines.append(f"  - {prefix}{statement.strip()}")
                    support = hypothesis.get("supporting_evidence")
                    support_items = support if isinstance(support, list) else []
                    for evidence in support_items[:6]:
                        if isinstance(evidence, str) and evidence.strip():
                            lines.append(f"    - Supports: {evidence.strip()}")
                    counter = hypothesis.get("counterevidence")
                    counter_items = counter if isinstance(counter, list) else []
                    for evidence in counter_items[:6]:
                        if isinstance(evidence, str) and evidence.strip():
                            lines.append(f"    - Counterevidence: {evidence.strip()}")
                    attempts_raw = hypothesis.get("falsification_attempts")
                    attempts = attempts_raw if isinstance(attempts_raw, list) else []
                    for attempt in attempts[:6]:
                        if not isinstance(attempt, dict):
                            continue
                        attempt_id = str(attempt.get("attempt_id") or "").strip()
                        outcome = str(attempt.get("outcome") or "").strip()
                        challenge_id = str(attempt.get("challenge_experiment_id") or "").strip()
                        if attempt_id and outcome and challenge_id:
                            lines.append(
                                f"    - Falsification `{attempt_id}`: `{outcome}` via "
                                f"`{challenge_id}`"
                            )
                elif isinstance(hypothesis, str) and hypothesis.strip():
                    # Historical rendering only; strict new records use objects.
                    lines.append(f"  - {hypothesis.strip()}")

        material_unknowns_raw = dossier.get("material_unknowns")
        material_unknowns = material_unknowns_raw if isinstance(material_unknowns_raw, list) else []
        if material_unknowns:
            lines.append("- Material unknowns:")
            for unknown in material_unknowns[:10]:
                if not isinstance(unknown, dict):
                    continue
                text = unknown.get("unknown")
                evidence_needed = unknown.get("evidence_needed")
                affects = unknown.get("affects")
                if isinstance(text, str) and text.strip():
                    affects_items = affects if isinstance(affects, list) else []
                    affects_s = ", ".join(str(item) for item in affects_items)
                    lines.append(f"  - {text.strip()} (affects: {affects_s or 'unspecified'})")
                    if isinstance(evidence_needed, str) and evidence_needed.strip():
                        lines.append(f"    - Evidence needed: {evidence_needed.strip()}")

        blocking_reasons = dossier.get("blocking_reasons")
        blocking_items = blocking_reasons if isinstance(blocking_reasons, list) else []
        if blocking_items:
            lines.append("- Blocking reasons:")
            for reason in blocking_items[:10]:
                if isinstance(reason, str) and reason.strip():
                    lines.append(f"  - {reason.strip()}")

        run_dir = dossier.get("run_dir")
        if isinstance(run_dir, str) and run_dir.strip():
            lines.append(f"- Run dir: `{run_dir.strip()}`")

        artifacts = dossier.get("artifacts")
        artifacts_dict = artifacts if isinstance(artifacts, dict) else {}
        report_json = artifacts_dict.get("report_json")
        patch_diff = artifacts_dict.get("patch_diff")
        if isinstance(report_json, str) and report_json.strip():
            lines.append(f"- report.json: `{report_json.strip()}`")
        if isinstance(patch_diff, str) and patch_diff.strip():
            lines.append(f"- patch.diff: `{patch_diff.strip()}`")

        warn = dossier.get("_parse_warning")
        if isinstance(warn, str) and warn.strip():
            lines.append(f"> ⚠ parse warning: {warn.strip()}")

        lines.append("")

    return "\n".join(lines)


def _build_selected_research_payloads(
    *,
    repo_root: Path,
    selected_priority_decisions: Sequence[Mapping[str, Any]],
    problem_records: Sequence[Mapping[str, Any]],
    atoms: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build the exact ordered Stage-3 assignments used by dispatch and resume."""

    records_by_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): dict(item)
        for item in problem_records
        if isinstance(item, Mapping) and isinstance(item.get("problem_id"), str)
    }
    atoms_by_id: dict[str, dict[str, Any]] = {
        str(item.get("atom_id")): dict(item)
        for item in atoms
        if isinstance(item, Mapping) and isinstance(item.get("atom_id"), str)
    }

    selected_payloads: list[dict[str, Any]] = []
    for decision in selected_priority_decisions:
        dec = dict(decision)
        pid = dec.get("problem_id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        rec = records_by_id.get(pid) or {}
        case_id = rec.get("case_id")
        case_id = case_id.strip() if isinstance(case_id, str) and case_id.strip() else ""
        evidence_ids_raw = rec.get("evidence_atom_ids")
        all_evidence_ids = (
            [
                atom_id.strip()
                for atom_id in evidence_ids_raw
                if isinstance(atom_id, str) and atom_id.strip()
            ]
            if isinstance(evidence_ids_raw, list)
            else []
        )
        derived_ids_raw = rec.get("derived_evidence_atom_ids")
        derived_ids = (
            [
                atom_id.strip()
                for atom_id in derived_ids_raw
                if isinstance(atom_id, str) and atom_id.strip()
            ]
            if isinstance(derived_ids_raw, list)
            else []
        )
        source_ids_raw = rec.get("source_evidence_atom_ids")
        evidence_ids = (
            [
                atom_id.strip()
                for atom_id in source_ids_raw
                if isinstance(atom_id, str) and atom_id.strip()
            ]
            if isinstance(source_ids_raw, list)
            else [atom_id for atom_id in all_evidence_ids if atom_id not in set(derived_ids)]
        )
        provisional_member_evidence_ids, provisional_group_errors = (
            _provisional_same_cause_source_evidence(rec)
        )
        evidence_ids.extend(provisional_member_evidence_ids)
        evidence_ids = list(dict.fromkeys(evidence_ids))
        evidence_ids, derived_source_currentness_aliases = (
            _select_current_derived_source_evidence(
                evidence_atom_ids=evidence_ids,
                atoms_by_id=atoms_by_id,
            )
        )
        evidence_ids, operational_candidate_currentness_aliases = (
            _select_current_operational_candidate_evidence(
                evidence_atom_ids=evidence_ids,
                atoms_by_id=atoms_by_id,
            )
        )
        case_evidence_ids, occurrence_evidence_ids = _initial_research_evidence_roles(
            evidence_ids,
            atoms_by_id=atoms_by_id,
        )
        evidence_ids, expanded_occurrence_ids, evidence_lineage_errors = (
            _expand_operational_candidate_evidence(
                evidence_atom_ids=evidence_ids,
                atoms_by_id=atoms_by_id,
            )
        )
        occurrence_evidence_ids = list(
            dict.fromkeys([*occurrence_evidence_ids, *expanded_occurrence_ids])
        )
        split_occurrence_ids, split_lineage_errors = authenticated_split_child_occurrence_evidence(
            rec,
            atoms_by_id=atoms_by_id,
        )
        for atom_id in split_occurrence_ids:
            if atom_id not in evidence_ids:
                evidence_ids.append(atom_id)
            if atom_id not in occurrence_evidence_ids:
                occurrence_evidence_ids.append(atom_id)
        evidence_lineage_errors.extend(split_lineage_errors)
        evidence_lineage_errors.extend(provisional_group_errors)
        derived_ids = list(
            dict.fromkeys(
                [
                    atom_id
                    for atom_id in [*derived_ids, *all_evidence_ids]
                    if atom_id not in set(evidence_ids)
                    and atom_id
                    not in {
                        alias["historical_atom_id"]
                        for alias in [
                            *derived_source_currentness_aliases,
                            *operational_candidate_currentness_aliases,
                        ]
                    }
                ]
            )
        )
        missing_evidence_atom_ids = [
            atom_id for atom_id in evidence_ids if atom_id not in atoms_by_id
        ]
        if not rec:
            missing_evidence_atom_ids.append(f"problem_record:{pid}")
        elif not case_id:
            missing_evidence_atom_ids.append(f"case_id:{pid}")
        elif not evidence_ids:
            missing_evidence_atom_ids.append(f"problem_evidence:{pid}")
        evidence_atoms = [
            atoms_by_id[atom_id] for atom_id in evidence_ids if atom_id in atoms_by_id
        ]
        derived_evidence_atoms = [
            atoms_by_id[atom_id] for atom_id in derived_ids if atom_id in atoms_by_id
        ]
        assignment, assignment_missing = _evidence_assignment(
            case_id=case_id,
            problem_id=pid,
            evidence_atom_ids=evidence_ids,
            evidence_atoms=evidence_atoms,
            repo_root=repo_root,
        )
        assignment["case_evidence_atom_ids"] = list(case_evidence_ids)
        assignment["occurrence_evidence_atom_ids"] = list(occurrence_evidence_ids)
        assignment["provisional_same_cause_member_evidence_atom_ids"] = list(
            provisional_member_evidence_ids
        )
        assignment["operational_candidate_currentness_aliases"] = list(
            operational_candidate_currentness_aliases
        )
        assignment["derived_source_currentness_aliases"] = list(
            derived_source_currentness_aliases
        )
        missing_evidence_atom_ids.extend(assignment_missing)
        missing_evidence_atom_ids = list(dict.fromkeys(missing_evidence_atom_ids))
        assignment_errors = [
            f"origin_evidence_unavailable:{item}" for item in missing_evidence_atom_ids
        ] + evidence_lineage_errors
        assignment["status"] = "incomplete" if assignment_errors else "complete"
        assignment["errors"] = assignment_errors
        assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
        selected_payloads.append(
            {
                "case_id": case_id,
                "problem_id": pid,
                "problem_record": rec,
                "priority_decision": dec,
                "expected_evidence_atom_ids": evidence_ids,
                "case_evidence_atom_ids": case_evidence_ids,
                "occurrence_evidence_atom_ids": occurrence_evidence_ids,
                "provisional_same_cause_member_evidence_atom_ids": (
                    provisional_member_evidence_ids
                ),
                "operational_candidate_currentness_aliases": (
                    operational_candidate_currentness_aliases
                ),
                "derived_source_currentness_aliases": derived_source_currentness_aliases,
                "evidence_lineage_errors": evidence_lineage_errors,
                "missing_evidence_atom_ids": missing_evidence_atom_ids,
                "evidence_atoms": evidence_atoms,
                # Prior research/implementation output is context, never a mandatory
                # symptom to reproduce and never an independent new problem source.
                "derived_evidence_atom_ids": derived_ids,
                "derived_evidence_atoms": derived_evidence_atoms,
                "evidence_assignment": assignment,
            }
        )
    return selected_payloads


def _build_authenticated_stage3_single_case_prefix(
    *,
    repo_root: Path,
    selected_priority_decisions: Sequence[Mapping[str, Any]],
    problem_records: Sequence[Mapping[str, Any]],
    atoms: Sequence[Mapping[str, Any]],
    imported_dossier: Mapping[str, Any],
    resolved_repo_ref: str | None,
    repo_input: str | None,
    target_slug: str | None,
    agent: str,
    model: str | None,
    artifacts: Mapping[str, Any],
    prior_stage_document: Mapping[str, Any] | None = None,
    validation_error_rescore: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Authenticate one completed dossier as the next resumable Stage-3 prefix item."""

    selected_payloads = _build_selected_research_payloads(
        repo_root=repo_root,
        selected_priority_decisions=selected_priority_decisions,
        problem_records=problem_records,
        atoms=atoms,
    )
    compatibility = stage3_research_compatibility_contract(agent=agent)
    completed = _resume_completed_prefix_from_stage_document(
        prior_stage_document,
        selected_problems=selected_payloads,
        resolved_repo_ref=resolved_repo_ref,
        expected_compatibility_contract=compatibility,
    )
    if len(completed) >= len(selected_payloads):
        raise ValueError("stage3_prefix_import_has_no_next_case")

    dossier = (
        _materialize_terminal_research_validation_error_rescore(
            imported_dossier,
            validation_error_rescore=validation_error_rescore,
        )
        if isinstance(validation_error_rescore, Mapping)
        else dict(imported_dossier)
    )
    expected = selected_payloads[len(completed)]
    expected_assignment_raw = expected.get("evidence_assignment")
    expected_assignment = (
        expected_assignment_raw if isinstance(expected_assignment_raw, Mapping) else {}
    )
    expected_atoms = [
        atom
        for field in ("evidence_atoms", "derived_evidence_atoms")
        for atom in (expected.get(field) if isinstance(expected.get(field), list) else [])
        if isinstance(atom, Mapping)
    ]
    expected_assignment = _authenticate_assignment_source_classifications(
        expected_assignment,
        atoms=expected_atoms,
    )
    dossier_assignment_raw = dossier.get("evidence_assignment")
    dossier_assignment = (
        dossier_assignment_raw if isinstance(dossier_assignment_raw, Mapping) else {}
    )
    verification_raw = dossier.get("evidence_verification")
    verification = verification_raw if isinstance(verification_raw, Mapping) else {}
    if dossier.get("problem_id") != expected.get("problem_id"):
        raise ValueError("stage3_prefix_import_problem_order_mismatch")
    if dossier.get("case_id") != expected.get("case_id"):
        raise ValueError("stage3_prefix_import_case_mismatch")
    if _persisted_source_evidence_assignment_sha256(
        dossier_assignment
    ) != _source_evidence_assignment_sha256(expected_assignment):
        raise ValueError("stage3_prefix_import_assignment_mismatch")
    if dossier.get("repo_revision") != resolved_repo_ref:
        raise ValueError("stage3_prefix_import_revision_mismatch")
    if verification.get("status") != "verified":
        raise ValueError("stage3_prefix_import_evidence_not_verified")

    candidate_items = [*completed, dossier]
    progress_checkpoint = _completed_prefix_checkpoint(
        selected_problems=selected_payloads,
        completed_dossiers=candidate_items,
        resolved_repo_ref=resolved_repo_ref,
        compatibility_contract=compatibility,
    )
    candidate = build_stage_document(
        "repro_research",
        candidate_items,
        input_meta={
            "selected_problem_count": len(selected_payloads),
            "fresh_research_dossier_count": len(candidate_items),
            "retained_research_reused_count": 0,
            "research_dossier_count": len(candidate_items),
            "stage_status": "checkpointed_progress",
            "dry_run": False,
            "agent": agent,
            "model": model,
            "repo_input": repo_input,
            "target_slug": target_slug,
            "research_compatibility": compatibility,
            "progress_checkpoint": progress_checkpoint,
            "supervised_prefix_import_count": 1,
        },
        artifacts=dict(artifacts),
    )
    authenticated = _resume_completed_prefix_from_stage_document(
        candidate,
        selected_problems=selected_payloads,
        resolved_repo_ref=resolved_repo_ref,
        expected_compatibility_contract=compatibility,
    )
    if len(authenticated) != len(candidate_items):
        raise ValueError("stage3_prefix_import_native_validation_incomplete")

    # Rebuild all hashes from the native normalized dossiers, then validate the
    # exact document that the persistence layer will receive.
    final_checkpoint = _completed_prefix_checkpoint(
        selected_problems=selected_payloads,
        completed_dossiers=authenticated,
        resolved_repo_ref=resolved_repo_ref,
        compatibility_contract=compatibility,
    )
    candidate["items"] = authenticated
    candidate["item_count"] = len(authenticated)
    candidate_meta = dict(candidate["input_meta"])
    candidate_meta["progress_checkpoint"] = final_checkpoint
    candidate["input_meta"] = candidate_meta
    final_authenticated = _resume_completed_prefix_from_stage_document(
        candidate,
        selected_problems=selected_payloads,
        resolved_repo_ref=resolved_repo_ref,
        expected_compatibility_contract=compatibility,
    )
    if final_authenticated != authenticated:
        raise ValueError("stage3_prefix_import_native_validation_unstable")
    return candidate


def _run_repro_research_stage(
    *,
    repo_root: Path,
    repo_input: str | None,
    repo_ref: str | None,
    target_slug: str | None,
    selected_priority_decisions: list[dict[str, Any]],
    problem_records: list[dict[str, Any]],
    atoms: list[dict[str, Any]],
    artifacts_dir: Path,
    out_json: Path,
    out_md: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    dry_run: bool,
    replay_timeout_seconds: float,
    replay_executor: ReplayExecutor,
    replay_executor_metadata: dict[str, Any],
    resume_stage_document: Mapping[str, Any] | None = None,
    reused_research_dossiers: Sequence[dict[str, Any]] = (),
    resume_upstream_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run stage 3 reproduce-plus-research and write the stage artifacts."""
    import json as _json

    records_by_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in problem_records
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }
    selected_payloads = _build_selected_research_payloads(
        repo_root=repo_root,
        selected_priority_decisions=selected_priority_decisions,
        problem_records=problem_records,
        atoms=atoms,
    )

    reused = [dict(item) for item in reused_research_dossiers]
    core_resume_stage_document = resume_stage_document
    resume_meta_raw = (
        resume_stage_document.get("input_meta")
        if isinstance(resume_stage_document, Mapping)
        else None
    )
    resume_meta = resume_meta_raw if isinstance(resume_meta_raw, Mapping) else {}
    resume_stage_status = resume_meta.get("stage_status")
    expected_compatibility = stage3_research_compatibility_contract(agent=agent)
    if (
        isinstance(resume_stage_document, Mapping)
        and resume_stage_status == "completed"
        and _validated_completed_stage3_checkpoint(
            resume_stage_document,
            expected_compatibility_contract=expected_compatibility,
        )
        is None
    ):
        raise ValueError("stage3_completed_resume_checkpoint_invalid")
    if (
        reused
        and isinstance(resume_stage_document, Mapping)
        and resume_stage_status != "checkpointed_progress"
    ):
        # Core checkpoints cover only freshly dispatched cases. Reused dossiers are appended
        # by this wrapper, so remove that exact suffix before asking the core runner to validate
        # either a parked frontier or an already completed fresh frontier.
        retained_doc = _json.loads(_json.dumps(resume_stage_document, ensure_ascii=False))
        retained_items_raw = retained_doc.get("items")
        retained_items = retained_items_raw if isinstance(retained_items_raw, list) else []
        if len(retained_items) < len(reused) or retained_items[-len(reused) :] != reused:
            raise ValueError("stage3_resume_reused_research_suffix_changed")
        fresh_retained = retained_items[: -len(reused)]
        retained_doc["items"] = fresh_retained
        retained_doc["item_count"] = len(fresh_retained)
        retained_meta_raw = retained_doc.get("input_meta")
        retained_meta = dict(retained_meta_raw) if isinstance(retained_meta_raw, Mapping) else {}
        retained_meta.update(
            {
                "fresh_research_dossier_count": len(fresh_retained),
                "retained_research_reused_count": 0,
                "research_dossier_count": len(fresh_retained),
                "evidence_sufficient_count": sum(
                    item.get("research_status") == "evidence_sufficient"
                    for item in fresh_retained
                    if isinstance(item, Mapping)
                ),
                "blocked_case_count": sum(
                    item.get("research_status") == "blocked"
                    for item in fresh_retained
                    if isinstance(item, Mapping)
                ),
                "insufficient_evidence_count": sum(
                    item.get("research_status") == "insufficient_evidence"
                    for item in fresh_retained
                    if isinstance(item, Mapping)
                ),
                "useful_research_output_count": sum(
                    item.get("research_status") in {"evidence_sufficient", "insufficient_evidence"}
                    for item in fresh_retained
                    if isinstance(item, Mapping)
                ),
            }
        )
        if resume_stage_status == "completed":
            retained_compatibility = _valid_stage3_research_compatibility_contract(
                retained_meta.get("research_compatibility")
            )
            retained_progress_raw = retained_meta.get("progress_checkpoint")
            retained_progress = (
                retained_progress_raw if isinstance(retained_progress_raw, Mapping) else {}
            )
            if retained_compatibility != expected_compatibility:
                raise ValueError("stage3_completed_resume_compatibility_changed")
            retained_meta["completed_stage_checkpoint"] = completed_stage3_checkpoint(
                dossiers=fresh_retained,
                fresh_research_dossier_count=len(fresh_retained),
                retained_research_reused_count=0,
                compatibility_contract=retained_compatibility,
                progress_checkpoint=retained_progress,
            )
        retained_doc["input_meta"] = retained_meta
        core_resume_stage_document = retained_doc

    def persist_progress(core_document: dict[str, Any]) -> None:
        if not isinstance(resume_upstream_contract, Mapping):
            return
        retained = _json.loads(_json.dumps(core_document, ensure_ascii=False))
        retained_meta_raw = retained.get("input_meta")
        retained_meta = dict(retained_meta_raw) if isinstance(retained_meta_raw, Mapping) else {}
        retained_meta["resume_upstream"] = _json.loads(
            _json.dumps(resume_upstream_contract, ensure_ascii=False)
        )
        retained["input_meta"] = retained_meta
        retained_artifacts_raw = retained.get("artifacts")
        retained_artifacts = (
            dict(retained_artifacts_raw) if isinstance(retained_artifacts_raw, Mapping) else {}
        )
        retained_artifacts.update({"research_json": str(out_json), "research_md": str(out_md)})
        retained["artifacts"] = retained_artifacts
        _atomic_write_research_json(out_json, retained)

    if selected_payloads or not reused:
        stage_doc = run_repro_research_stage(
            repo_root=repo_root,
            repo_input=repo_input,
            repo_ref=repo_ref,
            target_slug=target_slug,
            selected_problems=selected_payloads,
            artifacts_dir=artifacts_dir,
            agent=agent,
            model=model,
            cfg=cfg,
            dry_run=dry_run,
            replay_timeout_seconds=replay_timeout_seconds,
            replay_executor=replay_executor,
            replay_executor_metadata=replay_executor_metadata,
            resume_stage_document=core_resume_stage_document,
            progress_callback=(
                persist_progress if isinstance(resume_upstream_contract, Mapping) else None
            ),
        )
    else:
        progress_checkpoint = _completed_prefix_checkpoint(
            selected_problems=[],
            completed_dossiers=[],
            resolved_repo_ref=repo_ref,
            compatibility_contract=expected_compatibility,
        )
        stage_doc = build_stage_document(
            "repro_research",
            [],
            input_meta={
                "selected_problem_count": 0,
                "stage_status": "completed",
                "dry_run": bool(dry_run),
                "agent": agent,
                "model": model,
                "model_invocation_skipped": "all_ready_proofs_reused",
                "research_compatibility": expected_compatibility,
                "progress_checkpoint": progress_checkpoint,
                "completed_stage_checkpoint": completed_stage3_checkpoint(
                    dossiers=[],
                    fresh_research_dossier_count=0,
                    retained_research_reused_count=0,
                    compatibility_contract=expected_compatibility,
                    progress_checkpoint=progress_checkpoint,
                ),
            },
            artifacts={},
        )

    fresh_raw = stage_doc.get("items")
    fresh = (
        [dict(item) for item in fresh_raw if isinstance(item, dict)]
        if isinstance(fresh_raw, list)
        else []
    )
    all_dossiers = [*fresh, *reused]
    identities = [
        (str(item.get("case_id") or ""), str(item.get("problem_id") or "")) for item in all_dossiers
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("stage3_reused_research_duplicates_fresh_dossier")
    stage_doc["items"] = all_dossiers
    stage_doc["item_count"] = len(all_dossiers)
    input_meta_raw = stage_doc.get("input_meta")
    input_meta = dict(input_meta_raw) if isinstance(input_meta_raw, Mapping) else {}
    input_meta.update(
        {
            "fresh_research_dossier_count": len(fresh),
            "retained_research_reused_count": len(reused),
            "research_dossier_count": len(all_dossiers),
            "evidence_sufficient_count": sum(
                item.get("research_status") == "evidence_sufficient" for item in all_dossiers
            ),
            "blocked_case_count": sum(
                item.get("research_status") == "blocked" for item in all_dossiers
            ),
            "insufficient_evidence_count": sum(
                item.get("research_status") == "insufficient_evidence" for item in all_dossiers
            ),
            "requires_change_count": sum(
                isinstance(item.get("actionability_assessment"), Mapping)
                and item["actionability_assessment"].get("disposition") == "requires_change"
                for item in all_dossiers
            ),
            "already_addressed_count": sum(
                isinstance(item.get("actionability_assessment"), Mapping)
                and item["actionability_assessment"].get("disposition") == "already_addressed"
                for item in all_dossiers
            ),
            "non_actionable_count": sum(
                isinstance(item.get("actionability_assessment"), Mapping)
                and item["actionability_assessment"].get("disposition") == "non_actionable"
                for item in all_dossiers
            ),
            "actionability_undetermined_count": sum(
                not isinstance(item.get("actionability_assessment"), Mapping)
                or item["actionability_assessment"].get("disposition") == "undetermined"
                for item in all_dossiers
            ),
            "successful_negative_research_count": sum(
                item.get("research_status") == "evidence_sufficient"
                and isinstance(item.get("actionability_assessment"), Mapping)
                and item["actionability_assessment"].get("disposition")
                in {"already_addressed", "non_actionable"}
                for item in all_dossiers
            ),
            "useful_research_output_count": sum(
                item.get("research_status") in {"evidence_sufficient", "insufficient_evidence"}
                for item in all_dossiers
            ),
        }
    )
    if isinstance(resume_upstream_contract, Mapping):
        input_meta["resume_upstream"] = _json.loads(
            _json.dumps(resume_upstream_contract, ensure_ascii=False)
        )
    if input_meta.get("stage_status") == "completed":
        compatibility = _valid_stage3_research_compatibility_contract(
            input_meta.get("research_compatibility")
        )
        progress_raw = input_meta.get("progress_checkpoint")
        progress = progress_raw if isinstance(progress_raw, Mapping) else {}
        if compatibility != expected_compatibility:
            raise ValueError("stage3_completed_compatibility_contract_invalid")
        input_meta["completed_stage_checkpoint"] = completed_stage3_checkpoint(
            dossiers=all_dossiers,
            fresh_research_dossier_count=len(fresh),
            retained_research_reused_count=len(reused),
            compatibility_contract=compatibility,
            progress_checkpoint=progress,
        )
    stage_doc["input_meta"] = input_meta

    artifacts = stage_doc.get("artifacts")
    artifacts_dict = artifacts if isinstance(artifacts, dict) else {}
    artifacts_dict["research_json"] = str(out_json)
    artifacts_dict["research_md"] = str(out_md)
    stage_doc["artifacts"] = artifacts_dict

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_research_json(out_json, stage_doc)

    items_raw = stage_doc.get("items") if isinstance(stage_doc, dict) else None
    dossiers = (
        [item for item in items_raw if isinstance(item, dict)]
        if isinstance(items_raw, list)
        else []
    )
    title = out_json.stem.removesuffix(".research") or "Research"
    out_md.write_text(
        _render_research_dossiers_markdown(
            dossiers,
            problem_records_by_id=records_by_id,
            title=f"{title} – Research Dossiers",
        ),
        encoding="utf-8",
    )

    print(f"[stage3] wrote {out_json}", file=sys.stderr)
    print(f"[stage3] wrote {out_md}", file=sys.stderr)
    return stage_doc


__all__ = [name for name in globals() if not name.startswith("__")]
