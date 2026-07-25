# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from backlog_repo import extract_outcome_markdown
from runner_core import run_outcome_evidence_role

from usertest_implement.ledger import (
    bind_outcome_verification_amendment_files,
    transition_outcome_files,
)
from usertest_implement.outcome_evidence import (
    validate_bound_outcome_role_receipt,
    validate_bound_runner_verification,
)
from usertest_implement.selection import (
    _select_review_ticket,
    _selected_ticket_provenance,
)
from usertest_implement.shared import *

_EVIDENCE_FIELDS = (
    "test_evidence",
    "original_scenario_evidence",
    "live_evidence",
    "mitigation_evidence",
)
_EVIDENCE_KIND_BY_FIELD = {
    "test_evidence": "test",
    "original_scenario_evidence": "original_scenario",
    "live_evidence": "live",
    "mitigation_evidence": "mitigation_effect",
}
_OUTCOME_UPDATE_FIELDS = frozenset(
    {
        *_EVIDENCE_FIELDS,
        "remaining_risks",
        "recurrence_check",
    }
)


def _resolve_git_commit(repository: Path, commit: str, *, label: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repository}",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            f"{commit.strip()}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    resolved = str(proc.stdout or "").strip().casefold()
    if proc.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", resolved) is None:
        raise ValueError(f"Outcome verification {label} commit is unavailable: {commit}")
    return resolved


def _git_is_ancestor(repository: Path, *, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repository}",
            "-C",
            str(repository),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def _verification_commit_on_target_branch(
    repository: Path,
    *,
    verification_commit: str,
    target_branch: str,
) -> bool:
    if re.fullmatch(r"[A-Za-z0-9._/-]+", target_branch) is None:
        return False
    for ref in (f"refs/heads/{target_branch}", f"refs/remotes/origin/{target_branch}"):
        try:
            branch_commit = _resolve_git_commit(repository, ref, label="target branch")
        except ValueError:
            continue
        if _git_is_ancestor(
            repository,
            ancestor=verification_commit,
            descendant=branch_commit,
        ):
            return True
    return False


def _outcome_execution_provenance(
    current: dict[str, Any],
) -> tuple[str, str, str | None]:
    implementation_commit = str(current.get("merged_commit") or "").strip().casefold()
    if not implementation_commit:
        raise ValueError("Outcome record is missing merged_commit for role evidence")
    amendment = current.get("verification_amendment")
    if not isinstance(amendment, dict):
        return implementation_commit, implementation_commit, None
    execution_commit = str(amendment.get("verification_commit") or "").strip().casefold()
    amendment_id = str(amendment.get("amendment_id") or "").strip()
    if not execution_commit or not amendment_id:
        raise ValueError("Outcome verification amendment is incomplete")
    return implementation_commit, execution_commit, amendment_id
def _validate_evidence_receipt(
    item: dict[str, Any],
    *,
    evidence_kind: str,
    expected_fingerprint: str,
    current: dict[str, Any],
    trusted_runs_root: Path,
    expected_ticket_provenance: dict[str, Any],
    owner_root: Path,
) -> dict[str, Any]:
    """Accept only exact, ticket-bound runner verification receipts."""

    if str(item.get("result") or "").strip().lower() != "passed":
        raise ValueError("Outcome advancement evidence must record result=passed")
    runner_receipt = item.get("runner_receipt")
    if not isinstance(runner_receipt, dict):
        raise ValueError("Outcome evidence requires a runner_receipt")
    if runner_receipt.get("evidence_kind") != evidence_kind:
        raise ValueError("Outcome runner_receipt evidence_kind mismatch")
    if evidence_kind == "test":
        run_dir_raw = runner_receipt.get("run_dir")
        verification_sha256 = runner_receipt.get("verification_sha256")
        if not isinstance(run_dir_raw, str) or not run_dir_raw.strip():
            raise ValueError("Outcome runner_receipt.run_dir must be a non-empty string")
        if not isinstance(verification_sha256, str) or re.fullmatch(
            r"[0-9a-fA-F]{64}", verification_sha256.strip()
        ) is None:
            raise ValueError(
                "Outcome runner_receipt.verification_sha256 must be a SHA-256 digest"
            )
        normalized_receipt = validate_bound_runner_verification(
            run_dir=Path(run_dir_raw),
            fingerprint=expected_fingerprint,
            case_id=str(current["case_id"]),
            plan_revision_id=str(current["plan_revision_id"]),
            evidence_kind=evidence_kind,
            expected_verification_sha256=verification_sha256,
            trusted_runs_root=trusted_runs_root,
            owner_root=owner_root,
            expected_ticket_provenance=expected_ticket_provenance,
        )
    else:
        role_path = runner_receipt.get("role_artifact_path")
        role_hash = runner_receipt.get("role_artifact_sha256")
        if not isinstance(role_path, str) or not role_path.strip():
            raise ValueError(
                "Outcome runner_receipt.role_artifact_path must be a non-empty string"
            )
        if not isinstance(role_hash, str) or re.fullmatch(
            r"[0-9a-fA-F]{64}", role_hash.strip()
        ) is None:
            raise ValueError(
                "Outcome runner_receipt.role_artifact_sha256 must be a SHA-256 digest"
            )
        merged_commit, execution_commit, amendment_id = _outcome_execution_provenance(
            current
        )
        normalized_receipt = validate_bound_outcome_role_receipt(
            role_artifact_path=Path(role_path),
            evidence_kind=evidence_kind,
            case_id=str(current["case_id"]),
            plan_revision_id=str(current["plan_revision_id"]),
            merged_commit=merged_commit,
            expected_ticket_provenance=expected_ticket_provenance,
            trusted_runs_root=trusted_runs_root,
            expected_role_artifact_sha256=role_hash,
            execution_commit=execution_commit,
            verification_amendment_id=amendment_id,
        )
    return {**item, "result": "passed", "runner_receipt": normalized_receipt}


def _merge_evidence_entries(
    existing: list[dict[str, Any]],
    additions: list[Any],
) -> list[dict[str, Any]]:
    merged = [dict(item) for item in existing if isinstance(item, dict)]
    seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in merged}
    for item in additions:
        if not isinstance(item, dict):
            raise ValueError("Outcome evidence entries must be JSON objects")
        marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(dict(item))
    return merged


def _load_outcome_updates(
    path: Path,
    *,
    current: dict[str, Any],
    expected_fingerprint: str,
    trusted_runs_root: Path,
    expected_ticket_provenance: dict[str, Any],
    owner_root: Path,
) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Outcome evidence JSON does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Outcome evidence JSON is invalid: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Outcome evidence JSON must contain an object")
    unknown = sorted(set(raw) - _OUTCOME_UPDATE_FIELDS)
    if unknown:
        raise ValueError(f"Outcome evidence JSON contains unsupported fields: {unknown!r}")
    updates: dict[str, Any] = {}
    evidence_receipts = 0
    recorded_unobserved_recurrence = False
    for field in _EVIDENCE_FIELDS:
        if field not in raw:
            continue
        additions = raw[field]
        if not isinstance(additions, list):
            raise ValueError(f"Outcome evidence field must be a list: {field}")
        normalized_additions: list[dict[str, Any]] = []
        for item in additions:
            if not isinstance(item, dict):
                raise ValueError("Outcome evidence entries must be JSON objects")
            normalized_additions.append(
                _validate_evidence_receipt(
                    item,
                    evidence_kind=_EVIDENCE_KIND_BY_FIELD[field],
                    expected_fingerprint=expected_fingerprint,
                    current=current,
                    trusted_runs_root=trusted_runs_root,
                    expected_ticket_provenance=expected_ticket_provenance,
                    owner_root=owner_root,
                )
            )
            evidence_receipts += 1
        previous = current.get(field)
        existing = previous if isinstance(previous, list) else []
        updates[field] = _merge_evidence_entries(existing, normalized_additions)
    if "recurrence_check" in raw:
        recurrence_raw = raw["recurrence_check"]
        if not isinstance(recurrence_raw, dict):
            raise ValueError("recurrence_check must be a JSON object")
        recurrence_status = str(recurrence_raw.get("status") or "").strip().lower()
        if recurrence_status == "not_observed":
            if str(recurrence_raw.get("result") or "").strip().lower() != (
                "no_new_source_window"
            ):
                raise ValueError(
                    "not_observed recurrence_check.result must be no_new_source_window"
                )
            evidence = recurrence_raw.get("evidence", [])
            if not isinstance(evidence, list) or evidence:
                raise ValueError(
                    "not_observed recurrence_check cannot claim recurrence evidence"
                )
            updates["recurrence_check"] = {
                **recurrence_raw,
                "status": "not_observed",
                "result": "no_new_source_window",
                "evidence": [],
            }
            recorded_unobserved_recurrence = True
        elif recurrence_status not in {"completed", "no_recurrence"}:
            raise ValueError(
                "recurrence_check.status must record completed, no_recurrence, "
                "or not_observed"
            )
        else:
            if str(recurrence_raw.get("result") or "").strip().lower() != "passed":
                raise ValueError("recurrence_check.result must record passed")
            additions = recurrence_raw.get("evidence")
            if not isinstance(additions, list) or not additions:
                raise ValueError("recurrence_check requires runner-owned recurrence evidence")
            normalized_additions = []
            for item in additions:
                if not isinstance(item, dict):
                    raise ValueError("Recurrence evidence entries must be JSON objects")
                normalized_additions.append(
                    _validate_evidence_receipt(
                        item,
                        evidence_kind="recurrence",
                        expected_fingerprint=expected_fingerprint,
                        current=current,
                        trusted_runs_root=trusted_runs_root,
                        expected_ticket_provenance=expected_ticket_provenance,
                        owner_root=owner_root,
                    )
                )
                evidence_receipts += 1
            existing_check = current.get("recurrence_check")
            existing_evidence = (
                existing_check.get("evidence", [])
                if isinstance(existing_check, dict)
                else []
            )
            updates["recurrence_check"] = {
                **recurrence_raw,
                "status": recurrence_status,
                "result": "passed",
                "evidence": _merge_evidence_entries(
                    existing_evidence if isinstance(existing_evidence, list) else [],
                    normalized_additions,
                ),
            }
    if "remaining_risks" in raw:
        updates["remaining_risks"] = raw["remaining_risks"]
    if evidence_receipts == 0 and not recorded_unobserved_recurrence:
        raise ValueError(
            "Outcome advancement requires at least one verifiable evidence receipt"
        )
    return updates


def _cmd_outcome_bind_verification_amendment(args: argparse.Namespace) -> int:
    """Bind a merged correction PR without rewriting implementation provenance."""

    repo_root = _resolve_repo_root(args.repo_root)
    owner_root = args.owner_root.resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=args.ticket_path,
        fingerprint=args.fingerprint,
    )
    ticket_path = selected.idea_path
    if ticket_path is None:
        raise SystemExit("Outcome amendment requires a durable local ticket path.")
    completed_root = (owner_root / ".agents" / "plans" / "5 - complete").resolve()
    ticket_path = ticket_path.resolve()
    if not ticket_path.is_relative_to(completed_root):
        raise SystemExit(
            "Outcome amendment requires a completed ticket under "
            f"{completed_root}: {ticket_path}"
        )
    try:
        current = extract_outcome_markdown(ticket_path.read_text(encoding="utf-8"))
        if current is None:
            raise ValueError(f"Ticket has no durable outcome record: {ticket_path}")
        selected_provenance = _selected_ticket_provenance(
            selected,
            require_local_plan=True,
        )
        current_ticket_provenance = current.get("ticket_provenance")
        if not isinstance(current_ticket_provenance, dict):
            raise ValueError("Durable outcome ticket provenance is missing")
        for field in (
            "fingerprint",
            "case_id",
            "plan_revision_id",
            "ticket_body_sha256",
            "local_plan_sha256",
            "local_plan_filename",
            "verification_contract_sha256",
            "target_contract_sha256",
        ):
            if current_ticket_provenance.get(field) != selected_provenance.get(field):
                raise ValueError(
                    "Durable outcome ticket provenance is stale or cross-plan: "
                    f"{field}"
                )
        implementation_commit = _resolve_git_commit(
            owner_root,
            str(current.get("merged_commit") or ""),
            label="implementation",
        )
        verification_commit = _resolve_git_commit(
            owner_root,
            str(args.verification_commit),
            label="amendment",
        )
        if implementation_commit == verification_commit or not _git_is_ancestor(
            owner_root,
            ancestor=implementation_commit,
            descendant=verification_commit,
        ):
            raise ValueError(
                "Outcome verification amendment must be a distinct descendant of the "
                "implementation merged_commit"
            )
        target_branch = str(current.get("target_branch") or "").strip()
        if not _verification_commit_on_target_branch(
            owner_root,
            verification_commit=verification_commit,
            target_branch=target_branch,
        ):
            raise ValueError(
                "Outcome verification amendment commit is not on target branch: "
                f"{verification_commit}:{target_branch}"
            )
        amended = bind_outcome_verification_amendment_files(
            ledger_path=_resolve_ledger_path(repo_root=repo_root, raw=args.ledger),
            ticket_path=ticket_path,
            fingerprint=selected.fingerprint,
            verification_commit=verification_commit,
            verification_pr_url=str(args.verification_pr_url),
            recorded_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    print(
        json.dumps(
            {
                "fingerprint": selected.fingerprint,
                "case_id": amended["case_id"],
                "implementation_merged_commit": amended["merged_commit"],
                "implementation_pr_url": amended.get("pr_url"),
                "verification_amendment": amended["verification_amendment"],
                "ticket_path": str(ticket_path),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_outcome_run_role(args: argparse.Namespace) -> int:
    """Run one post-merge stage-6 evidence role with runner-owned predicates."""

    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)
    owner_root = args.owner_root.resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=args.ticket_path,
        fingerprint=args.fingerprint,
    )
    ticket_path = selected.idea_path
    if ticket_path is None:
        raise SystemExit("Outcome role execution requires a durable local ticket path.")
    try:
        current = extract_outcome_markdown(ticket_path.read_text(encoding="utf-8"))
        if current is None:
            raise ValueError(f"Ticket has no durable outcome record: {ticket_path}")
        if current.get("state") not in {
            "implemented",
            "tests_verified",
            "original_scenario_verified",
            "live_verified",
            "mitigated",
            "unverified",
        }:
            raise ValueError(
                "Outcome role execution requires a merged, nonterminal implementation outcome"
            )
        provenance = _selected_ticket_provenance(selected, require_local_plan=True)
        current_ticket_provenance = current.get("ticket_provenance")
        if not isinstance(current_ticket_provenance, dict):
            raise ValueError("Durable outcome ticket provenance is missing")
        expected_outcome_provenance = {
            key: provenance[key]
            for key in (
                "schema_version",
                "fingerprint",
                "case_id",
                "plan_revision_id",
                "ticket_body_sha256",
                "local_plan_sha256",
                "local_plan_filename",
                "verification_contract_sha256",
                "target_contract_sha256",
            )
        }
        expected_outcome_provenance["verified_implementation_head"] = (
            current_ticket_provenance.get("verified_implementation_head")
        )
        bound_provenance = {
            **provenance,
            "verified_implementation_head": current_ticket_provenance.get(
                "verified_implementation_head"
            ),
        }
        if current.get("ticket_provenance") != expected_outcome_provenance:
            raise ValueError("Durable outcome ticket provenance is missing, stale, or cross-plan")
        contract = provenance.get("verification_contract")
        roles = contract.get("outcome_roles") if isinstance(contract, dict) else None
        role = str(args.role).strip().lower()
        role_contract = roles.get(role) if isinstance(roles, dict) else None
        if not isinstance(role_contract, dict):
            raise ValueError(f"Selected stage-6 plan does not define outcome role: {role}")
        merged_commit, execution_commit, amendment_id = _outcome_execution_provenance(
            current
        )
        workspace = (
            args.workspace.resolve() if args.workspace is not None else repo_root
        )
        if args.out_dir is not None:
            output_dir = args.out_dir.resolve()
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            output_dir = (
                cfg.runs_dir
                / "_outcome_roles"
                / selected.fingerprint
                / timestamp
                / role
            ).resolve()
        trusted_runs_root = cfg.runs_dir.resolve()
        if not output_dir.is_relative_to(trusted_runs_root):
            raise ValueError(
                f"Outcome role output must stay under configured runs root: {trusted_runs_root}"
            )
        artifact_path = output_dir / "outcome_role.json"
        recurrence_receipt = (
            args.recurrence_refresh_receipt.resolve()
            if args.recurrence_refresh_receipt is not None
            else None
        )
        if recurrence_receipt is not None and not recurrence_receipt.is_relative_to(
            trusted_runs_root
        ):
            raise ValueError(
                "Recurrence refresh receipt must stay under configured runs root: "
                f"{trusted_runs_root}"
            )
        amendment_kwargs = (
            {
                "execution_commit": execution_commit,
                "verification_amendment_id": amendment_id,
            }
            if amendment_id is not None
            else {}
        )
        artifact = run_outcome_evidence_role(
            workspace=workspace,
            output_path=artifact_path,
            role=role,
            role_contract=role_contract,
            case_id=str(current["case_id"]),
            plan_revision_id=str(current["plan_revision_id"]),
            merged_commit=merged_commit,
            verification_contract_sha256=str(provenance["verification_contract_sha256"]),
            target_contract_sha256=str(provenance["target_contract_sha256"]),
            verified_implementation_head=str(
                current_ticket_provenance["verified_implementation_head"]
            ),
            timeout_seconds=args.timeout_seconds,
            recurrence_refresh_receipt_path=recurrence_receipt,
            recurrence_after=(
                str(current["recorded_at"]) if role == "recurrence" else None
            ),
            **amendment_kwargs,
        )
        if artifact.get("passed") is not True:
            print(
                json.dumps(
                    {
                        "role": role,
                        "passed": False,
                        "timed_out": artifact.get("timed_out"),
                        "artifact_path": str(artifact_path),
                    },
                    indent=2,
                )
            )
            return 3
        receipt = validate_bound_outcome_role_receipt(
            role_artifact_path=artifact_path,
            evidence_kind=role,
            case_id=str(current["case_id"]),
            plan_revision_id=str(current["plan_revision_id"]),
            merged_commit=merged_commit,
            expected_ticket_provenance=bound_provenance,
            trusted_runs_root=trusted_runs_root,
            **amendment_kwargs,
        )
        evidence_item = {
            "kind": "runner_outcome_role",
            "reference": str(artifact_path),
            "result": "passed",
            "runner_receipt": receipt,
        }
        if role == "original_scenario":
            evidence_doc = {"original_scenario_evidence": [evidence_item]}
        elif role == "live":
            evidence_doc = {"live_evidence": [evidence_item]}
        elif role == "mitigation_effect":
            evidence_doc = {"mitigation_evidence": [evidence_item]}
        else:
            evidence_doc = {
                "recurrence_check": {
                    "status": "completed",
                    "result": "passed",
                    "evidence": [evidence_item],
                }
            }
        evidence_path = output_dir / "outcome_evidence.json"
        evidence_path.write_text(
            json.dumps(evidence_doc, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    print(
        json.dumps(
            {
                "role": role,
                "passed": True,
                "artifact_path": str(artifact_path),
                "evidence_json": str(evidence_path),
                "timeout_seconds": args.timeout_seconds,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_outcome_advance(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)
    owner_root = args.owner_root.resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=args.ticket_path,
        fingerprint=args.fingerprint,
    )
    ticket_path = selected.idea_path
    if ticket_path is None:
        raise SystemExit("Outcome advancement requires a durable local ticket path.")
    ticket_path = ticket_path.resolve()
    plans_root = (owner_root / ".agents" / "plans").resolve()
    if not ticket_path.is_relative_to(plans_root):
        raise SystemExit(f"Refusing to update a ticket outside {plans_root}: {ticket_path}")

    try:
        markdown = ticket_path.read_text(encoding="utf-8")
        current = extract_outcome_markdown(markdown)
        if current is None:
            raise ValueError(f"Ticket has no durable outcome record: {ticket_path}")
        selected_provenance = _selected_ticket_provenance(
            selected,
            require_local_plan=True,
        )
        current_ticket_provenance = current.get("ticket_provenance")
        if not isinstance(current_ticket_provenance, dict):
            raise ValueError("Durable outcome ticket provenance is missing")
        expected_outcome_provenance = {
            key: selected_provenance[key]
            for key in (
                "schema_version",
                "fingerprint",
                "case_id",
                "plan_revision_id",
                "ticket_body_sha256",
                "local_plan_sha256",
                "local_plan_filename",
                "verification_contract_sha256",
                "target_contract_sha256",
            )
        }
        expected_outcome_provenance["verified_implementation_head"] = (
            current_ticket_provenance.get("verified_implementation_head")
        )
        bound_selected_provenance = {
            **selected_provenance,
            "verified_implementation_head": current_ticket_provenance.get(
                "verified_implementation_head"
            ),
        }
        if current.get("ticket_provenance") != expected_outcome_provenance:
            raise ValueError(
                "Durable outcome ticket provenance is missing, stale, or cross-plan"
            )
        updates = _load_outcome_updates(
            args.evidence_json.resolve(),
            current=current,
            expected_fingerprint=selected.fingerprint,
            trusted_runs_root=cfg.runs_dir,
            expected_ticket_provenance=bound_selected_provenance,
            owner_root=owner_root,
        )
        transitioned = transition_outcome_files(
            ledger_path=_resolve_ledger_path(repo_root=repo_root, raw=args.ledger),
            ticket_path=ticket_path,
            fingerprint=selected.fingerprint,
            state=str(args.state),
            recorded_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            updates=updates,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    print(
        json.dumps(
            {
                "fingerprint": selected.fingerprint,
                "case_id": transitioned["case_id"],
                "plan_revision_id": transitioned["plan_revision_id"],
                "state": transitioned["state"],
                "ticket_path": str(ticket_path),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


__all__ = [
    "_cmd_outcome_advance",
    "_cmd_outcome_bind_verification_amendment",
    "_cmd_outcome_run_role",
]
