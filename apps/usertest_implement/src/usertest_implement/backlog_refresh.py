from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

CommandExecutor = Callable[[list[str], Path, str], None]
OpenPullRequestProbe = Callable[["BacklogRefreshRequest"], list[dict[str, Any]]]
OutcomeProgressor = Callable[..., list[Any]]


class BacklogRefreshError(RuntimeError):
    """Raised when an atomic shadow-backed backlog refresh cannot complete."""


class BacklogRefreshLockedError(BacklogRefreshError):
    """Raised when another process owns the same backlog refresh scope."""


class OpenPullRequestsError(BacklogRefreshError):
    """Raised when regenerating a backlog would race active implementation work."""


@dataclass(frozen=True)
class BacklogRefreshRequest:
    """One exact backlog scope and the immutable inputs used by every refresh step."""

    repo_root: Path
    repo_input: str
    runs_dir: Path
    target: str
    backlog_python: Path
    research_ref: str = "origin/dev"
    breadth_profile: str = "internal_maintenance"
    agent: str = "codex"
    model: str | None = None
    actions_yaml: Path | None = None
    atom_actions_yaml: Path | None = None
    qualified_shadow_state_path: Path | None = None
    gh_bin: str = "gh"

    def normalized(self) -> BacklogRefreshRequest:
        repo_root = self.repo_root.expanduser().resolve()
        runs_dir = self.runs_dir.expanduser().resolve()
        backlog_python = self.backlog_python.expanduser().resolve()
        target = self.target.strip()
        repo_input = self.repo_input.strip()
        research_ref = self.research_ref.strip()
        breadth_profile = self.breadth_profile.strip()
        agent = self.agent.strip()
        model = self.model.strip() if isinstance(self.model, str) and self.model.strip() else None
        if not target or not repo_input or not research_ref or not breadth_profile or not agent:
            raise BacklogRefreshError(
                "Backlog refresh requires non-empty target, repo_input, research_ref, "
                "breadth_profile, and agent"
            )
        actions_yaml = (
            self.actions_yaml.expanduser().resolve()
            if self.actions_yaml is not None
            else (repo_root / "configs" / "backlog_actions.yaml").resolve()
        )
        atom_actions_yaml = (
            self.atom_actions_yaml.expanduser().resolve()
            if self.atom_actions_yaml is not None
            else (repo_root / "configs" / "backlog_atom_actions.yaml").resolve()
        )
        qualified_shadow_state_path = (
            self.qualified_shadow_state_path.expanduser().resolve()
            if self.qualified_shadow_state_path is not None
            else (runs_dir / target / "_compiled" / f"{target}.shadow_state.json").resolve()
        )
        return BacklogRefreshRequest(
            repo_root=repo_root,
            repo_input=repo_input,
            runs_dir=runs_dir,
            target=target,
            backlog_python=backlog_python,
            research_ref=research_ref,
            breadth_profile=breadth_profile,
            agent=agent,
            model=model,
            actions_yaml=actions_yaml,
            atom_actions_yaml=atom_actions_yaml,
            qualified_shadow_state_path=qualified_shadow_state_path,
            gh_bin=self.gh_bin.strip() or "gh",
        )

    @property
    def compiled_dir(self) -> Path:
        return self.runs_dir / self.target / "_compiled"

    @property
    def backlog_json(self) -> Path:
        return self.compiled_dir / f"{self.target}.backlog.json"

    @property
    def intent_json(self) -> Path:
        return self.compiled_dir / f"{self.target}.intent_snapshot.json"

    @property
    def ux_json(self) -> Path:
        return self.compiled_dir / f"{self.target}.ux_review.json"

    @property
    def export_json(self) -> Path:
        return self.compiled_dir / f"{self.target}.tickets_export.json"

    @property
    def shadow_state_json(self) -> Path:
        return (
            self.qualified_shadow_state_path
            if self.qualified_shadow_state_path is not None
            else self.compiled_dir / f"{self.target}.shadow_state.json"
        )

    @property
    def lock_path(self) -> Path:
        return self.compiled_dir / ".backlog_refresh.lock"

    @property
    def receipt_path(self) -> Path:
        return self.compiled_dir / f"{self.target}.refresh_receipt.json"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_command_executor(argv: list[str], cwd: Path, label: str) -> None:
    print(f"[backlog-refresh] {label}: {subprocess.list2cmdline(argv)}", file=sys.stderr)
    try:
        proc = subprocess.run(argv, cwd=str(cwd), check=False)
    except OSError as exc:
        raise BacklogRefreshError(f"Backlog refresh step could not start: {label}: {exc}") from exc
    if proc.returncode != 0:
        raise BacklogRefreshError(
            f"Backlog refresh step failed: {label}: returncode={proc.returncode}"
        )


def _repo_selector(repo_input: str) -> str | None:
    match = re.search(
        r"(?:github\.com[:/])(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?$",
        repo_input.strip(),
        flags=re.IGNORECASE,
    )
    if match is not None:
        return f"{match.group('owner')}/{match.group('repo')}"
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_input.strip()):
        return repo_input.strip()
    return None


def probe_open_pull_requests(request: BacklogRefreshRequest) -> list[dict[str, Any]]:
    """Return live open PRs for the target repository, failing closed on probe errors."""

    local_input = Path(request.repo_input).expanduser()
    cwd = local_input.resolve() if local_input.is_dir() else request.repo_root
    argv = [
        request.gh_bin,
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,url,headRefName,baseRefName",
    ]
    selector = None if local_input.is_dir() else _repo_selector(request.repo_input)
    if selector is not None:
        argv.extend(["--repo", selector])
    elif not local_input.is_dir():
        raise BacklogRefreshError(
            "Cannot prove the target repository has no open pull requests: "
            f"unsupported repo_input={request.repo_input!r}"
        )
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise BacklogRefreshError(
            f"Cannot prove the target repository has no open pull requests: {exc}"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise BacklogRefreshError(
            "Cannot prove the target repository has no open pull requests"
            + (f": {detail}" if detail else "")
        )
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise BacklogRefreshError("Open pull request probe returned invalid JSON") from exc
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise BacklogRefreshError("Open pull request probe returned an invalid payload")
    return [dict(item) for item in payload]


def _assert_no_open_pull_requests(
    request: BacklogRefreshRequest,
    probe: OpenPullRequestProbe,
    *,
    boundary: str,
) -> None:
    open_prs = probe(request)
    if not open_prs:
        return
    identities = [
        str(item.get("url") or item.get("number") or "unknown") for item in open_prs
    ]
    raise OpenPullRequestsError(
        f"Backlog refresh is forbidden while pull requests are open ({boundary}): "
        + ", ".join(identities)
    )


def _try_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BacklogRefreshLockedError("Backlog refresh scope is already locked") from exc
        return
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise BacklogRefreshLockedError("Backlog refresh scope is already locked") from exc


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_refresh_scope(request: BacklogRefreshRequest):
    """Hold one OS-backed lock for the entire shadow-to-export transaction."""

    request.compiled_dir.mkdir(parents=True, exist_ok=True)
    request.lock_path.touch(exist_ok=True)
    with request.lock_path.open("r+b") as handle:
        _try_lock(handle)
        try:
            handle.seek(0)
            handle.truncate()
            metadata = {
                "pid": os.getpid(),
                "target": request.target,
                "repo_input": request.repo_input,
            }
            handle.write((_canonical_json(metadata) + "\n").encode("utf-8"))
            handle.flush()
            yield
        finally:
            _unlock(handle)


def _base_command(request: BacklogRefreshRequest, report: str) -> list[str]:
    return [
        str(request.backlog_python),
        "-m",
        "usertest_backlog.cli",
        "reports",
        report,
        "--repo-root",
        str(request.repo_root),
        "--runs-dir",
        str(request.runs_dir),
        "--target",
        request.target,
        "--repo-input",
        request.repo_input,
    ]


def _model_flags(request: BacklogRefreshRequest) -> list[str]:
    flags = ["--agent", request.agent]
    if request.model is not None:
        flags.extend(["--model", request.model])
    return flags


def build_refresh_commands(request: BacklogRefreshRequest) -> list[tuple[str, list[str]]]:
    """Build the exact ordered refresh contract used by every implementation entry point."""

    request = request.normalized()
    operational = [
        *_base_command(request, "backlog"),
        "--out-json",
        str(request.backlog_json),
        "--research-ref",
        request.research_ref,
        "--breadth-profile",
        request.breadth_profile,
        "--atom-actions-yaml",
        str(request.atom_actions_yaml),
        "--shadow-state",
        str(request.shadow_state_json),
        *_model_flags(request),
        "--operational-shadow",
        "--force",
        "--no-resume",
    ]
    intent = [
        *_base_command(request, "intent-snapshot"),
        "--out-json",
        str(request.intent_json),
        *_model_flags(request),
        "--with-summary",
        "--force",
        "--no-resume",
    ]
    ux = [
        *_base_command(request, "review-ux"),
        "--backlog-json",
        str(request.backlog_json),
        "--intent-snapshot-json",
        str(request.intent_json),
        "--out-json",
        str(request.ux_json),
        "--breadth-profile",
        request.breadth_profile,
        *_model_flags(request),
        "--force",
        "--no-resume",
    ]
    export = [
        *_base_command(request, "export-tickets"),
        "--backlog-json",
        str(request.backlog_json),
        "--actions-yaml",
        str(request.actions_yaml),
        "--atom-actions-yaml",
        str(request.atom_actions_yaml),
        "--stage",
        "ready_for_ticket",
        "--out-json",
        str(request.export_json),
    ]
    score_operational = [
        *operational,
        "--score-operational-shadow",
    ]
    return [
        ("operational shadow materialization", operational),
        ("intent snapshot", intent),
        ("UX review", ux),
        ("operational shadow validation", score_operational),
        ("ticket export", export),
    ]


def _read_shadow_state(state_path: Path, *, allow_missing: bool = False) -> dict[str, Any]:
    if allow_missing and not state_path.exists():
        return {"cycles": []}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BacklogRefreshError(f"Shadow state is missing or invalid: {state_path}") from exc
    if not isinstance(state, dict):
        raise BacklogRefreshError("Shadow state must contain an object")
    cycles = state.get("cycles")
    if not isinstance(cycles, list) or any(not isinstance(item, dict) for item in cycles):
        raise BacklogRefreshError("Shadow state does not contain valid completed cycles")
    return state


def _cycle_ids(state: dict[str, Any]) -> list[str]:
    cycles_raw = state.get("cycles")
    cycles = cycles_raw if isinstance(cycles_raw, list) else []
    ids: list[str] = []
    for cycle in cycles:
        cycle_id = cycle.get("cycle_id") if isinstance(cycle, dict) else None
        if not isinstance(cycle_id, str) or re.fullmatch(r"[0-9a-f]{64}", cycle_id) is None:
            raise BacklogRefreshError("Shadow cycle has no valid runner-owned identity")
        ids.append(cycle_id)
    if len(ids) != len(set(ids)):
        raise BacklogRefreshError("Shadow state repeats a runner-owned cycle identity")
    return ids


def _validate_fresh_operational_cycle(
    request: BacklogRefreshRequest,
    *,
    prior_cycle_ids: Sequence[str],
) -> tuple[dict[str, Any], str]:
    state = _read_shadow_state(request.shadow_state_json)
    cycles_raw = state.get("cycles")
    cycles = cycles_raw if isinstance(cycles_raw, list) else []
    current_ids = _cycle_ids(state)
    new_ids = [cycle_id for cycle_id in current_ids if cycle_id not in set(prior_cycle_ids)]
    if len(new_ids) != 1 or not cycles or current_ids[-1] != new_ids[0]:
        raise BacklogRefreshError(
            "Operational validation must append exactly one fresh latest cycle"
        )
    latest = cycles[-1]
    if latest.get("cycle_mode") != "operational":
        raise BacklogRefreshError("Fresh shadow cycle is not operational")
    if latest.get("passed") is not True:
        raise BacklogRefreshError("Fresh operational cycle failed depth invariants")
    if state.get("ready_for_export") is not True:
        raise BacklogRefreshError("Fresh operational cycle did not open the export gate")
    if state.get("activation_mode") != "operational_bound":
        raise BacklogRefreshError(
            "Operational export is not bound to a release-qualified pipeline/config anchor"
        )
    if state.get("validated_cycle_id") != new_ids[0]:
        raise BacklogRefreshError("Validated cycle is not the fresh operational cycle")
    required = state.get("required_consecutive_cycles")
    stable = state.get("consecutive_stable_passes")
    anchor_ids = state.get("release_anchor_cycle_ids")
    if (
        isinstance(required, bool)
        or not isinstance(required, int)
        or required < 1
        or isinstance(stable, bool)
        or not isinstance(stable, int)
        or stable < required
        or not isinstance(anchor_ids, list)
        or len(anchor_ids) < required
        or any(not isinstance(item, str) or item not in current_ids for item in anchor_ids)
    ):
        raise BacklogRefreshError("Operational release anchor is missing or invalid")
    cycles_by_id = {
        str(cycle.get("cycle_id")): cycle
        for cycle in cycles
        if isinstance(cycle, dict) and isinstance(cycle.get("cycle_id"), str)
    }
    for cycle_id in anchor_ids:
        anchor = cycles_by_id[cycle_id]
        qualification = anchor.get("qualification")
        if (
            anchor.get("cycle_mode") != "release"
            or anchor.get("passed") is not True
            or not isinstance(qualification, dict)
            or qualification.get("qualification_class") != "positive_throughput"
            or qualification.get("status") != "verified"
        ):
            raise BacklogRefreshError("Operational anchor is not a positive release cycle")
    if latest.get("stability_inputs_sha256") != state.get(
        "release_anchor_stability_inputs_sha256"
    ):
        raise BacklogRefreshError("Operational pipeline/config inputs drifted from release anchor")
    return state, new_ids[0]


def _source_observation_window(atoms_snapshot: Path) -> dict[str, Any]:
    """Summarize actual source runs so pipeline stability cannot impersonate recurrence."""

    by_run: dict[str, dict[str, Any]] = {}
    try:
        lines = atoms_snapshot.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise BacklogRefreshError("Qualifying shadow atoms snapshot is unreadable") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            atom = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BacklogRefreshError(
                f"Qualifying shadow atoms snapshot is invalid at line {line_number}"
            ) from exc
        if not isinstance(atom, dict):
            continue
        atom_id = atom.get("atom_id")
        if not isinstance(atom_id, str) or atom_id.startswith("__aggregate__/"):
            continue
        role = atom.get("evidence_role")
        if role not in {None, "observation"}:
            continue
        if atom.get("idea_originated") is True or str(atom.get("source") or "").casefold() in {
            "idea",
            "ideas",
            "external_idea",
        }:
            continue
        run_rel = atom.get("run_rel")
        if not isinstance(run_rel, str) or not run_rel.strip():
            continue
        timestamp = atom.get("timestamp_utc")
        timestamp_text = timestamp.strip() if isinstance(timestamp, str) else None
        row = by_run.setdefault(
            run_rel.strip(),
            {
                "run_rel": run_rel.strip(),
                "source_atom_count": 0,
                "latest_timestamp_utc": None,
            },
        )
        row["source_atom_count"] = int(row["source_atom_count"]) + 1
        previous = row.get("latest_timestamp_utc")
        if timestamp_text and (not isinstance(previous, str) or timestamp_text > previous):
            row["latest_timestamp_utc"] = timestamp_text
    runs = [by_run[key] for key in sorted(by_run)]
    summary = {
        "source_run_count": len(runs),
        "source_atom_count": sum(int(row["source_atom_count"]) for row in runs),
        "runs": runs,
    }
    return {**summary, "summary_sha256": _sha256_json(summary)}


def _write_refresh_receipt(
    request: BacklogRefreshRequest,
    *,
    operational_cycle_id: str,
    shadow_state: dict[str, Any],
    outcome_progression: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    config = {
        "repo_root": str(request.repo_root),
        "repo_input": request.repo_input,
        "runs_dir": str(request.runs_dir),
        "target": request.target,
        "research_ref": request.research_ref,
        "breadth_profile": request.breadth_profile,
        "agent": request.agent,
        "model": request.model,
        "actions_yaml": str(request.actions_yaml),
        "atom_actions_yaml": str(request.atom_actions_yaml),
        "shadow_state_path": str(request.shadow_state_json),
    }
    cycles_raw = shadow_state.get("cycles")
    cycles = cycles_raw if isinstance(cycles_raw, list) else []
    operational_cycles = [
        cycle
        for cycle in cycles
        if isinstance(cycle, dict)
        and cycle.get("cycle_mode") == "operational"
        and cycle.get("passed") is True
    ][-2:]
    observation_cycles: list[dict[str, Any]] = []
    for cycle in operational_cycles:
        cycle_id = str(cycle.get("cycle_id"))
        artifact_receipts_raw = cycle.get("artifact_receipts")
        artifact_receipts = (
            artifact_receipts_raw if isinstance(artifact_receipts_raw, list) else []
        )
        case_registry = next(
            (
                artifact
                for artifact in artifact_receipts
                if isinstance(artifact, dict) and artifact.get("name") == "case_registry"
            ),
            None,
        )
        if not isinstance(case_registry, dict):
            raise BacklogRefreshError(
                f"Operational shadow cycle lacks its case-registry receipt: {cycle_id}"
            )
        atoms_receipt = next(
            (
                artifact
                for artifact in artifact_receipts
                if isinstance(artifact, dict) and artifact.get("name") == "atoms"
            ),
            None,
        )
        if not isinstance(atoms_receipt, dict):
            raise BacklogRefreshError(
                f"Operational shadow cycle lacks its atoms receipt: {cycle_id}"
            )
        atoms_snapshot_raw = atoms_receipt.get("snapshot_path")
        atoms_snapshot = (
            Path(atoms_snapshot_raw).expanduser().resolve()
            if isinstance(atoms_snapshot_raw, str)
            else None
        )
        if atoms_snapshot is None or not atoms_snapshot.is_file():
            raise BacklogRefreshError(
                f"Operational shadow cycle lacks its atoms snapshot: {cycle_id}"
            )
        observation_cycles.append(
            {
                "cycle_id": cycle_id,
                "generated_at": cycle.get("generated_at"),
                "passed": cycle.get("passed"),
                "cycle_receipt_path": cycle.get("cycle_receipt_path"),
                "cycle_receipt_sha256": cycle.get("cycle_receipt_sha256"),
                "case_registry_snapshot_path": case_registry.get("snapshot_path"),
                "case_registry_sha256": case_registry.get("sha256"),
                "case_registry_content_sha256": case_registry.get("content_sha256"),
                "atoms_snapshot_path": str(atoms_snapshot),
                "atoms_sha256": atoms_receipt.get("sha256"),
                "atoms_content_sha256": atoms_receipt.get("content_sha256"),
                "source_observation_window": _source_observation_window(atoms_snapshot),
            }
        )
    receipt = {
        "schema_version": 4,
        "producer": "usertest_implement.backlog_refresh",
        "recorded_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "configuration": config,
        "configuration_sha256": _sha256_json(config),
        "operational_cycle_id": operational_cycle_id,
        "observation_cycle_ids": [item["cycle_id"] for item in observation_cycles],
        "observation_cycles": observation_cycles,
        "activation_mode": shadow_state.get("activation_mode"),
        "release_anchor_cycle_ids": shadow_state.get("release_anchor_cycle_ids"),
        "release_anchor_stability_inputs_sha256": shadow_state.get(
            "release_anchor_stability_inputs_sha256"
        ),
        "validated_cycle_id": shadow_state.get("validated_cycle_id"),
        "validated_backlog_sha256": shadow_state.get("validated_backlog_sha256"),
        "outcome_progression": list(outcome_progression),
        "shadow_state_path": str(request.shadow_state_json),
        "shadow_state_sha256": _sha256_file(request.shadow_state_json),
        "export_path": str(request.export_json),
    }
    receipt["receipt_content_sha256"] = _sha256_json(receipt)
    tmp = request.receipt_path.with_suffix(request.receipt_path.suffix + ".tmp")
    tmp.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(request.receipt_path)
    return receipt


def run_shadow_backlog_refresh(
    request: BacklogRefreshRequest,
    *,
    command_executor: CommandExecutor | None = None,
    open_pr_probe: OpenPullRequestProbe | None = None,
    outcome_progressor: OutcomeProgressor | None = None,
) -> Path:
    """Retry merged outcome proof, then refresh with pending cases suppressed locally."""

    request = request.normalized()
    execute = command_executor or _default_command_executor
    # Open pull requests are not a repository-wide refresh lock.  Generated
    # tickets already carry stable case/plan identities and the export path
    # suppresses an active generated case locally.  Treating every unrelated
    # IDEA or release PR as a global blocker made evidence mining unavailable
    # for the entire repository without improving causal quality.
    #
    # Keep ``open_pr_probe`` in the public call signature for compatibility
    # with callers that used to inject it, but do not use it as an execution
    # gate.  The OS-backed refresh lock below remains the mutation boundary.
    _ = open_pr_probe
    commands = build_refresh_commands(request)
    operational_cycle_id = ""
    outcome_progression_summary: list[dict[str, Any]] = []
    with exclusive_refresh_scope(request):
        prior_state = _read_shadow_state(request.shadow_state_json, allow_missing=True)
        prior_cycle_ids = _cycle_ids(prior_state)
        local_owner = Path(request.repo_input).expanduser()
        if local_owner.is_dir():
            if outcome_progressor is None:
                # Lazy import avoids the command-layer shared-module cycle during CLI
                # startup while still keeping this the centralized refresh boundary.
                from usertest_implement.outcome_progression import (
                    progress_pending_outcomes_before_refresh,
                )

                progress = progress_pending_outcomes_before_refresh
            else:
                progress = outcome_progressor
            progression_results = progress(
                repo_root=request.repo_root,
                owner_root=local_owner.resolve(),
            )
            blocked = [result for result in progression_results if not result.complete]
            if blocked:
                summary = "; ".join(
                    f"{result.fingerprint}:{result.final_state}:{result.detail or result.status}"
                    for result in blocked
                )
                print(
                    "[backlog-refresh] merged outcomes remain case-locally pending; "
                    f"continuing unrelated backlog work: {summary}",
                    file=sys.stderr,
                )
            for result in progression_results:
                payload = result.to_dict() if callable(getattr(result, "to_dict", None)) else {
                    "fingerprint": str(getattr(result, "fingerprint", "")),
                    "starting_state": str(getattr(result, "starting_state", "")),
                    "final_state": str(getattr(result, "final_state", "")),
                    "expected_state": getattr(result, "expected_state", None),
                    "status": str(getattr(result, "status", "")),
                    "detail": getattr(result, "detail", None),
                    "complete": bool(getattr(result, "complete", False)),
                }
                outcome_progression_summary.append(payload)
        shadow_state: dict[str, Any] | None = None
        for label, argv in commands:
            execute(argv, request.repo_root, label)
            if label == "operational shadow materialization":
                materialized_state = _read_shadow_state(
                    request.shadow_state_json,
                    allow_missing=True,
                )
                if _cycle_ids(materialized_state) != prior_cycle_ids:
                    raise BacklogRefreshError(
                        "Operational materialization changed shadow state before validation"
                    )
            elif label == "operational shadow validation":
                shadow_state, operational_cycle_id = _validate_fresh_operational_cycle(
                    request,
                    prior_cycle_ids=prior_cycle_ids,
                )
            elif label == "ticket export":
                if shadow_state is None or not operational_cycle_id:
                    raise BacklogRefreshError(
                        "Ticket export ran without a fresh validated operational cycle"
                    )
                if not request.export_json.is_file():
                    raise BacklogRefreshError(
                        "Ticket export did not produce its declared artifact: "
                        f"{request.export_json}"
                    )
        if shadow_state is None:
            raise BacklogRefreshError("Backlog refresh did not reach its guarded export boundary")
        _write_refresh_receipt(
            request,
            operational_cycle_id=operational_cycle_id,
            shadow_state=shadow_state,
            outcome_progression=outcome_progression_summary,
        )
    return request.export_json


__all__ = [
    "BacklogRefreshError",
    "BacklogRefreshLockedError",
    "BacklogRefreshRequest",
    "OpenPullRequestsError",
    "build_refresh_commands",
    "exclusive_refresh_scope",
    "probe_open_pull_requests",
    "run_shadow_backlog_refresh",
]
