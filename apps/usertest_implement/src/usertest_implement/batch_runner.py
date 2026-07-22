from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml
from backlog_repo import (
    is_generated_backlog_ticket,
    load_atom_actions_yaml,
    reconcile_atom_actions_from_plan_folders,
    write_atom_actions_yaml,
)
from backlog_repo.plan_scope import parse_plan_target_contract_markdown
from runner_core.execution_backend import _load_maintenance_docker_config

from usertest_implement.backlog_refresh import (
    BacklogRefreshRequest,
    run_shadow_backlog_refresh,
)
from usertest_implement.batch_failure import classify_run_outcome, write_batch_failure
from usertest_implement.batch_preflight import run_batch_preflight
from usertest_implement.batch_state import (
    append_jsonl,
    batch_dir,
    build_initial_state,
    latest_batch_dir,
    load_json,
    new_batch_id,
    outcomes_path,
    persist_state,
    state_path,
    utc_now_z,
    write_json,
)
from usertest_implement.tickets import move_ticket_file

SEVERITY_RANK = {
    "blocker": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}
VALID_AGENTS = {"claude", "codex", "gemini"}
RUN_HISTORY_ARTIFACT_NAMES = {
    "agent_attempts.json",
    "effective_run_spec.json",
    "error.json",
    "metrics.json",
    "preflight.json",
    "report.json",
    "report_validation_errors.json",
    "run_meta.json",
    "target_ref.json",
    "ticket_ref.json",
    "ticket_resume_state.json",
    "timing.json",
}
DOCKER_RESOURCE_PLAN_REASON_SUMMARIES = {
    "per_ticket_image_resolution": (
        "Maintenance Docker image resolution currently runs inside each ticket run, so "
        "parallel tickets can contend on pull/build/tag operations."
    ),
    "cleanup_on_prepare": (
        "Maintenance image cleanup is configured to run during Docker profile preparation, "
        "which mutates shared local Docker image state."
    ),
}
BACKLOG_INPUT_PATHS = (
    Path("apps/usertest_backlog/src/usertest_backlog"),
    Path("packages/backlog_core/src/backlog_core"),
    Path("packages/backlog_miner/src/backlog_miner"),
    Path("configs/backlog_policy.yaml"),
    Path("configs/backlog_policy_internal_maintenance.yaml"),
    Path("configs/backlog_prompts"),
    Path("configs/backlog_prompts_internal_maintenance"),
    Path("configs/backlog_stage_guidance"),
    Path("configs/backlog_stage_guidance_internal_maintenance"),
    Path("configs/repo_intent.md"),
)
AUTOMATED_ACTIVE_PLAN_BUCKETS: tuple[str, ...] = (
    "0.5 - to_triage",
    "1 - ideas",
    "1.5 - to_plan",
    "2 - ready",
    "3 - in_progress",
    "4 - for_review",
)
TERMINAL_CASE_STATES = frozenset({"resolved", "duplicate", "superseded", "split"})


def _sync_ticket_atom_actions(*, owner_root: Path) -> None:
    """Update queue lifecycle state only in the explicitly configured owner root."""

    atom_actions_path = owner_root / "configs" / "backlog_atom_actions.yaml"
    atom_actions = load_atom_actions_yaml(atom_actions_path)
    reconcile_atom_actions_from_plan_folders(
        atom_actions=atom_actions,
        owner_roots=[owner_root.resolve()],
        generated_at=utc_now_z(),
    )
    write_atom_actions_yaml(atom_actions_path, atom_actions)


@dataclass(frozen=True)
class BacklogSource:
    name: str
    runs_dir: Path
    target: str
    breadth_profile: str = "internal_maintenance"
    research_ref: str = "origin/dev"
    shadow_state_path: Path | None = None


@dataclass(frozen=True)
class WorkerTemplate:
    worker_index: int
    agent: str
    model: str | None = None


@dataclass(frozen=True)
class PhaseConfig:
    name: str
    sources: list[BacklogSource]
    severities: set[str]


@dataclass(frozen=True)
class BatchCandidate:
    source_name: str
    export_path: Path
    fingerprint: str
    severity: str
    title: str
    owner_root: Path
    ticket_path: Path
    execution_domain: str
    execution_conflict_keys: tuple[str, ...]
    retry_count: int = 0

    @property
    def ticket_key(self) -> str:
        return _ticket_key(self.owner_root, self.fingerprint)


@dataclass
class SourceRefreshState:
    input_fingerprint: str
    export_path: Path
    refreshes: int = 0
    reuses: int = 0


@dataclass(frozen=True)
class TicketRunResult:
    run_dir: Path | None
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float


def _configured_owner_root(*, code_root: Path, config: dict[str, Any]) -> Path:
    """Resolve historical runs, queues, and ledgers independently of code CWD."""

    defaults_raw = config.get("defaults")
    defaults = defaults_raw if isinstance(defaults_raw, dict) else {}
    raw = defaults.get("owner_root") or defaults.get("repo_root") or code_root
    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        candidate = code_root / candidate
    return candidate.resolve()


def _generated_plan_target_revision(ticket_path: Path) -> str:
    """Return the immutable stage-6 source revision for one automated plan."""

    try:
        markdown = ticket_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Generated ticket is unreadable: {ticket_path}") from exc
    if not is_generated_backlog_ticket(markdown):
        raise ValueError(f"Batch candidate is not an automated generated ticket: {ticket_path}")
    contract = parse_plan_target_contract_markdown(markdown)
    if not isinstance(contract, dict):
        raise ValueError(f"Generated ticket has no stage-6 target contract: {ticket_path}")
    revision = str(contract.get("repo_revision") or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", revision) is None:
        raise ValueError(
            f"Generated ticket target revision is not an exact commit: {ticket_path}"
        )
    return revision.lower()


def _validate_candidate_wave_revision(
    *,
    candidate: BatchCandidate,
    wave_base_revision: str,
) -> None:
    planned = _generated_plan_target_revision(candidate.ticket_path)
    if planned != wave_base_revision.lower():
        raise ValueError(
            "Generated ticket target revision does not match the pinned batch wave: "
            f"fingerprint={candidate.fingerprint} planned={planned} "
            f"wave={wave_base_revision.lower()}"
        )


def _enable_console_backslashreplace(stream: Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        if str(getattr(stream, "errors", "")).lower() == "backslashreplace":
            return
        reconfigure(errors="backslashreplace")
    except Exception:
        return


def _configure_console_output() -> None:
    _enable_console_backslashreplace(sys.stdout)
    _enable_console_backslashreplace(sys.stderr)


def _write_stream(stream: Any, text: str) -> None:
    if not text:
        return
    try:
        stream.write(text)
        stream.flush()
        return
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        escaped = text.encode(encoding, errors="backslashreplace").decode(
            encoding, errors="ignore"
        )
        try:
            stream.write(escaped)
            stream.flush()
            return
        except OSError:
            return
    except OSError:
        return


def _print(msg: str) -> None:
    _write_stream(sys.stdout, f"[{utc_now_z()}] {msg}\n")


_configure_console_output()


def _load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _merge_path_entries(*, entries: tuple[str, ...], existing: str, sep: str) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for chunk in (*entries, *existing.split(sep)):
        value = chunk.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return sep.join(merged)


def _batch_subprocess_env(repo_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    relpaths = tuple(
        path.relative_to(repo_root).as_posix()
        for base in ("apps", "packages")
        for path in sorted((repo_root / base).glob("*/src"))
        if path.is_dir()
    )
    merged = _merge_path_entries(
        entries=relpaths,
        existing=env.get("PYTHONPATH", ""),
        sep=os.pathsep,
    )
    if merged:
        env["PYTHONPATH"] = merged
    return env


def _venv_python(repo_root: Path, app_name: str) -> Path:
    rel = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return repo_root / "apps" / app_name / ".venv" / rel


def _repo_branch(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    branch = proc.stdout.strip()
    if proc.returncode != 0 or not branch:
        raise RuntimeError("Unable to determine current git branch.")
    return branch


def _repo_head(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    head = proc.stdout.strip()
    if proc.returncode != 0 or not head:
        raise RuntimeError("Unable to determine current git HEAD.")
    return head


def _resolve_wave_base_revision(
    *,
    code_root: Path,
    configured_ref: str,
    receipt_dir: Path,
) -> str:
    """Fetch and pin the exact revision shared by research and implementation."""

    ref = configured_ref.strip()
    if not ref:
        raise ValueError("Batch defaults.wave_base_ref must be non-empty")
    remote_match = re.fullmatch(r"(?P<remote>[^/]+)/(?P<branch>.+)", ref)
    if remote_match is not None and remote_match.group("remote") not in {
        "refs",
    }:
        remote = remote_match.group("remote")
        branch = remote_match.group("branch")
        _run_logged_command(
            ["git", "fetch", remote, branch],
            cwd=code_root,
            log_path=receipt_dir / "wave_base_fetch.log",
        )
    proc = _run_logged_command(
        ["git", "rev-parse", f"{ref}^{{commit}}"],
        cwd=code_root,
        log_path=receipt_dir / "wave_base_resolve.log",
    )
    revision = proc.stdout.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise RuntimeError(f"Unable to resolve exact batch wave revision from {ref!r}")
    write_json(
        receipt_dir / "wave_base_revision.json",
        {
            "schema_version": 1,
            "configured_ref": ref,
            "resolved_revision": revision,
            "resolved_at_utc": utc_now_z(),
        },
    )
    return revision


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_log(
    path: Path,
    *,
    command: list[str],
    returncode: int,
    stdout: str,
    stderr: str,
) -> None:
    lines = ["$ " + " ".join(command), f"exit_code={returncode}"]
    if stdout.strip():
        lines.extend(["stdout:", stdout.rstrip()])
    if stderr.strip():
        lines.extend(["stderr:", stderr.rstrip()])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _run_logged_command(
    args: list[str],
    *,
    cwd: Path,
    log_path: Path | None = None,
    allow_failure: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if log_path is not None:
        _write_log(
            log_path,
            command=[str(part) for part in args],
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    if proc.stdout.strip():
        _write_stream(sys.stdout, proc.stdout)
    if proc.stderr.strip():
        _write_stream(sys.stderr, proc.stderr)
    if proc.returncode != 0 and not allow_failure:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(args)}")
    return proc


def _ticket_key(owner_root: Path, fingerprint: str) -> str:
    return f"{owner_root.resolve().as_posix().lower()}::{fingerprint.strip().lower()}"


def _hash_file_metadata(hasher: Any, *, root: Path, path: Path) -> None:
    stat = path.stat()
    rel = path.relative_to(root)
    hasher.update(rel.as_posix().encode("utf-8", errors="replace"))
    hasher.update(b"\0")
    hasher.update(str(stat.st_size).encode("ascii"))
    hasher.update(b"\0")
    hasher.update(str(stat.st_mtime_ns).encode("ascii"))
    hasher.update(b"\0")


def _hash_tree_metadata(root: Path, *, names: set[str] | None = None) -> str:
    hasher = sha256()
    hasher.update(str(root).encode("utf-8", errors="replace"))
    hasher.update(b"\0")
    if not root.exists():
        hasher.update(b"MISSING")
        return hasher.hexdigest()
    if root.is_file():
        _hash_file_metadata(hasher, root=root.parent, path=root)
        return hasher.hexdigest()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in {".git", ".venv", "__pycache__", "_compiled"} for part in rel.parts):
            continue
        if names is not None and path.name not in names:
            continue
        _hash_file_metadata(hasher, root=root, path=path)
    return hasher.hexdigest()


def _shared_backlog_inputs_fingerprint(repo_root: Path) -> str:
    hasher = sha256()
    for rel_path in BACKLOG_INPUT_PATHS:
        abs_path = repo_root / rel_path
        hasher.update(rel_path.as_posix().encode("utf-8", errors="replace"))
        hasher.update(b"\0")
        hasher.update(_hash_tree_metadata(abs_path).encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def _source_inputs_fingerprint(
    *,
    repo_root: Path,
    source: BacklogSource,
    shared_inputs_fingerprint: str,
) -> str:
    hasher = sha256()
    source_root = source.runs_dir / source.target
    hasher.update(source.name.encode("utf-8", errors="replace"))
    hasher.update(b"\0")
    hasher.update(source.target.encode("utf-8", errors="replace"))
    hasher.update(b"\0")
    hasher.update(source.breadth_profile.encode("utf-8", errors="replace"))
    hasher.update(b"\0")
    hasher.update(shared_inputs_fingerprint.encode("ascii"))
    hasher.update(b"\0")
    hasher.update(
        _hash_tree_metadata(source_root, names=RUN_HISTORY_ARTIFACT_NAMES).encode("ascii")
    )
    return hasher.hexdigest()


def _compiled_dir(source: BacklogSource) -> Path:
    return source.runs_dir / source.target / "_compiled"


def _export_path(source: BacklogSource) -> Path:
    return _compiled_dir(source) / f"{source.target}.tickets_export.json"


def _source_key(source: BacklogSource) -> str:
    return f"{source.runs_dir.resolve()}::{source.target}"


def _unique_sources(phases: list[PhaseConfig]) -> list[BacklogSource]:
    unique: dict[str, BacklogSource] = {}
    for phase in phases:
        for source in phase.sources:
            unique.setdefault(_source_key(source), source)
    return [unique[key] for key in sorted(unique)]


def _artifact_receipt(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "exists": False, "sha256": None}
    return {
        "path": str(path),
        "exists": True,
        "sha256": sha256(path.read_bytes()).hexdigest(),
    }


def _build_terminal_proof(
    *,
    code_root: Path,
    owner_root: Path,
    phases: list[PhaseConfig],
    refresh_state: dict[str, SourceRefreshState],
    wave_base_revision: str,
) -> dict[str, Any]:
    """Prove that a full automated wave has no unclosed causal work.

    This is deliberately an end-state proof, not a plan-admission ratchet.  It does
    not block work because an unrelated pull request exists.  Instead, after every
    severity has run, it binds the final zero-export result to fresh shadow cycles,
    the exact researched revision, the canonical case graph, and the generated-only
    queue.  Nonterminal cases remain visible rather than being mistaken for success.
    """

    reasons: list[str] = []
    covered_severities = sorted({severity for phase in phases for severity in phase.severities})
    required_severities = set(SEVERITY_RANK)
    missing_severities = sorted(required_severities.difference(covered_severities))
    if missing_severities:
        reasons.append("severity_coverage_missing:" + ",".join(missing_severities))

    sources = _unique_sources(phases)
    low_source_keys = {
        _source_key(source)
        for phase in phases
        if "low" in phase.severities
        for source in phase.sources
    }
    missing_low_sources = [
        source.name for source in sources if _source_key(source) not in low_source_keys
    ]
    if missing_low_sources:
        reasons.append("low_phase_source_coverage_missing:" + ",".join(missing_low_sources))

    source_proofs: list[dict[str, Any]] = []
    for source in sources:
        compiled = _compiled_dir(source)
        export_path = _export_path(source)
        shadow_path = compiled / f"{source.target}.shadow_state.json"
        case_registry_path = compiled / f"{source.target}.case_registry.json"
        refresh_receipt_path = compiled / f"{source.target}.refresh_receipt.json"
        source_reasons: list[str] = []

        refresh = refresh_state.get(source.name)
        if refresh is None or refresh.refreshes < 1:
            source_reasons.append("fresh_refresh_missing")
        elif refresh.export_path.resolve() != export_path.resolve():
            source_reasons.append("refresh_export_path_mismatch")

        export_count: int | None = None
        try:
            export_document = _read_json(export_path)
            exports_raw = export_document.get("exports")
            if not isinstance(exports_raw, list) or any(
                not isinstance(item, dict) for item in exports_raw
            ):
                source_reasons.append("export_records_invalid")
            else:
                export_count = len(exports_raw)
                if export_count:
                    source_reasons.append(f"exports_remaining:{export_count}")
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            source_reasons.append("export_unreadable")

        shadow_ready = False
        validated_cycle_id: str | None = None
        try:
            shadow = _read_json(shadow_path)
            shadow_ready = shadow.get("ready_for_export") is True
            validated_cycle_id_raw = shadow.get("validated_cycle_id")
            validated_cycle_id = (
                validated_cycle_id_raw
                if isinstance(validated_cycle_id_raw, str) and validated_cycle_id_raw.strip()
                else None
            )
            required_cycles = shadow.get("required_consecutive_cycles")
            stable_passes = shadow.get("consecutive_stable_passes")
            if (
                not shadow_ready
                or validated_cycle_id is None
                or isinstance(required_cycles, bool)
                or not isinstance(required_cycles, int)
                or isinstance(stable_passes, bool)
                or not isinstance(stable_passes, int)
                or stable_passes < required_cycles
            ):
                source_reasons.append("shadow_not_ready")
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            source_reasons.append("shadow_unreadable")

        receipt_cycle_id: str | None = None
        try:
            receipt = _read_json(refresh_receipt_path)
            configuration_raw = receipt.get("configuration")
            configuration = configuration_raw if isinstance(configuration_raw, dict) else {}
            receipt_cycle_raw = receipt.get("validated_cycle_id")
            receipt_cycle_id = (
                receipt_cycle_raw
                if isinstance(receipt_cycle_raw, str) and receipt_cycle_raw.strip()
                else None
            )
            if Path(str(receipt.get("export_path") or "")).resolve() != export_path.resolve():
                source_reasons.append("receipt_export_path_mismatch")
            if (
                Path(str(receipt.get("shadow_state_path") or "")).resolve()
                != shadow_path.resolve()
            ):
                source_reasons.append("receipt_shadow_path_mismatch")
            if receipt_cycle_id is None or receipt_cycle_id != validated_cycle_id:
                source_reasons.append("receipt_validated_cycle_mismatch")
            if str(configuration.get("research_ref") or "").lower() != wave_base_revision:
                source_reasons.append("receipt_wave_revision_mismatch")
            if Path(str(configuration.get("repo_root") or "")).resolve() != code_root.resolve():
                source_reasons.append("receipt_code_root_mismatch")
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            source_reasons.append("refresh_receipt_unreadable")

        nonterminal_cases: list[dict[str, str]] = []
        terminal_case_count: int | None = None
        alias_case_count: int | None = None
        try:
            case_registry = _read_json(case_registry_path)
            cases_raw = case_registry.get("cases")
            if not isinstance(cases_raw, dict):
                source_reasons.append("case_registry_cases_invalid")
            else:
                terminal_case_count = 0
                alias_case_count = 0
                for case_id, raw_case in sorted(cases_raw.items()):
                    if not isinstance(raw_case, dict):
                        source_reasons.append(f"case_record_invalid:{case_id}")
                        continue
                    state = str(raw_case.get("state") or "active").strip().lower()
                    if state == "alias" or raw_case.get("alias_of"):
                        alias_case_count += 1
                    elif state in TERMINAL_CASE_STATES:
                        terminal_case_count += 1
                    else:
                        nonterminal_cases.append({"case_id": str(case_id), "state": state})
                if nonterminal_cases:
                    source_reasons.append(
                        f"nonterminal_cases_remaining:{len(nonterminal_cases)}"
                    )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            source_reasons.append("case_registry_unreadable")

        source_proofs.append(
            {
                "name": source.name,
                "runs_dir": str(source.runs_dir),
                "target": source.target,
                "research_revision": source.research_ref,
                "refresh_count": refresh.refreshes if refresh is not None else 0,
                "export_count": export_count,
                "shadow_ready": shadow_ready,
                "validated_cycle_id": validated_cycle_id,
                "receipt_validated_cycle_id": receipt_cycle_id,
                "terminal_case_count": terminal_case_count,
                "alias_case_count": alias_case_count,
                "nonterminal_cases": nonterminal_cases,
                "artifacts": {
                    "export": _artifact_receipt(export_path),
                    "shadow_state": _artifact_receipt(shadow_path),
                    "case_registry": _artifact_receipt(case_registry_path),
                    "refresh_receipt": _artifact_receipt(refresh_receipt_path),
                },
                "passed": not source_reasons,
                "reasons": source_reasons,
            }
        )
        reasons.extend(f"source:{source.name}:{reason}" for reason in source_reasons)

    active_generated_paths: list[str] = []
    plans_root = owner_root / ".agents" / "plans"
    for bucket in AUTOMATED_ACTIVE_PLAN_BUCKETS:
        bucket_dir = plans_root / bucket
        if not bucket_dir.is_dir():
            continue
        for path in sorted(bucket_dir.glob("*.md"), key=lambda item: item.name.casefold()):
            try:
                markdown = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                # An unreadable active ticket cannot be proven non-automated.
                active_generated_paths.append(str(path))
                continue
            if is_generated_backlog_ticket(markdown):
                active_generated_paths.append(str(path))
    if active_generated_paths:
        reasons.append(f"active_generated_plans_remaining:{len(active_generated_paths)}")

    proof: dict[str, Any] = {
        "schema_version": 1,
        "recorded_at_utc": utc_now_z(),
        "code_root": str(code_root.resolve()),
        "owner_root": str(owner_root.resolve()),
        "wave_base_revision": wave_base_revision,
        "covered_severities": covered_severities,
        "required_severities": sorted(required_severities),
        "source_proofs": source_proofs,
        "active_generated_plan_paths": active_generated_paths,
        "passed": not reasons,
        "reasons": reasons,
    }
    proof["proof_sha256"] = sha256(
        json.dumps(proof, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return proof


def _refresh_backlog(
    *,
    repo_root: Path,
    owner_root: Path | None = None,
    source: BacklogSource,
    repo_input: str,
    backlog_python: Path,
    agent: str,
    model: str | None,
    batch_dir_path: Path,
) -> Path:
    owner_root = (owner_root or repo_root).resolve()
    env = _batch_subprocess_env(repo_root)
    log_dir = batch_dir_path / "refresh_logs" / source.name

    def _execute(argv: list[str], cwd: Path, label: str) -> None:
        safe_label = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        _run_logged_command(
            argv,
            cwd=cwd,
            log_path=log_dir / f"{safe_label}.log",
            env=env,
        )

    return run_shadow_backlog_refresh(
        BacklogRefreshRequest(
            repo_root=repo_root,
            repo_input=repo_input,
            runs_dir=source.runs_dir,
            target=source.target,
            backlog_python=backlog_python,
            research_ref=source.research_ref,
            breadth_profile=source.breadth_profile,
            agent=agent,
            model=model,
            actions_yaml=owner_root / "configs" / "backlog_actions.yaml",
            atom_actions_yaml=owner_root / "configs" / "backlog_atom_actions.yaml",
            qualified_shadow_state_path=source.shadow_state_path,
        ),
        command_executor=_execute,
    )


def _load_candidates(
    *,
    source_name: str,
    export_path: Path,
    severities: set[str],
    processed: set[str],
) -> list[BatchCandidate]:
    doc = _read_json(export_path)
    exports_raw = doc.get("exports")
    exports = (
        [item for item in exports_raw if isinstance(item, dict)]
        if isinstance(exports_raw, list)
        else []
    )
    candidates: list[BatchCandidate] = []
    for item in exports:
        fingerprint = item.get("fingerprint")
        source_ticket = item.get("source_ticket")
        owner_repo = item.get("owner_repo")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            continue
        if not isinstance(source_ticket, dict) or source_ticket.get("stage") != "ready_for_ticket":
            continue
        severity = source_ticket.get("severity")
        if not isinstance(severity, str) or severity not in severities:
            continue
        if not isinstance(owner_repo, dict):
            continue
        owner_root_raw = owner_repo.get("root")
        idea_path_raw = owner_repo.get("idea_path")
        if not isinstance(owner_root_raw, str) or not owner_root_raw.strip():
            continue
        if not isinstance(idea_path_raw, str) or not idea_path_raw.strip():
            continue
        owner_root = Path(owner_root_raw).resolve()
        ticket_path = Path(idea_path_raw).resolve()
        key = _ticket_key(owner_root, fingerprint)
        if key in processed:
            continue
        execution_domain_raw = item.get("execution_domain")
        execution_domain = (
            execution_domain_raw.strip()
            if isinstance(execution_domain_raw, str) and execution_domain_raw.strip()
            else "unknown"
        )
        conflict_keys_raw = item.get("execution_conflict_keys")
        conflict_keys = (
            tuple(
                value
                for value in conflict_keys_raw
                if isinstance(value, str) and value.strip()
            )
            if isinstance(conflict_keys_raw, list)
            else (f"execution_domain:{execution_domain}",)
        )
        candidates.append(
            BatchCandidate(
                source_name=source_name,
                export_path=export_path,
                fingerprint=fingerprint.strip(),
                severity=severity,
                title=str(item.get("title") or ""),
                owner_root=owner_root,
                ticket_path=ticket_path,
                execution_domain=execution_domain,
                execution_conflict_keys=conflict_keys,
            )
        )
    candidates.sort(
        key=lambda item: (
            SEVERITY_RANK.get(item.severity, 99),
            item.execution_domain,
            item.title.lower(),
            item.ticket_key,
        )
    )
    return candidates


def _load_ready_queue_candidates(
    *,
    repo_root: Path,
    source_name: str,
    export_path: Path,
    severities: set[str],
    processed: set[str],
) -> list[BatchCandidate]:
    ready_dir = repo_root / ".agents" / "plans" / "2 - ready"
    if not ready_dir.exists():
        return []

    candidates: list[BatchCandidate] = []
    for ticket_path in sorted(ready_dir.glob("*.md"), key=lambda path: path.name.lower()):
        try:
            markdown = ticket_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not is_generated_backlog_ticket(markdown):
            continue
        fingerprint_match = re.search(
            r"^-\s*Fingerprint:\s*`([^`]+)`\s*$",
            markdown,
            flags=re.MULTILINE,
        )
        export_kind_match = re.search(
            r"^-\s*Export kind:\s*`([^`]+)`\s*$",
            markdown,
            flags=re.MULTILINE,
        )
        stage_match = re.search(
            r"^-\s*Stage:\s*`([^`]+)`\s*$",
            markdown,
            flags=re.MULTILINE,
        )
        severity_match = re.search(
            r"^-\s*Severity:\s*`([^`]+)`\s*$",
            markdown,
            flags=re.MULTILINE,
        )
        execution_domain_match = re.search(
            r"^-\s*Execution domain:\s*`([^`]+)`\s*$",
            markdown,
            flags=re.MULTILINE,
        )
        conflict_line_match = re.search(
            r"^-\s*Execution conflict keys:\s*(.+)$",
            markdown,
            flags=re.MULTILINE,
        )
        title_match = re.search(r"^#\s+(.+)$", markdown, flags=re.MULTILINE)
        if fingerprint_match is None:
            continue
        fingerprint = fingerprint_match.group(1).strip()
        if not fingerprint:
            continue
        if (
            export_kind_match is None
            or export_kind_match.group(1).strip().lower() != "implementation"
        ):
            continue
        if stage_match is None or stage_match.group(1).strip().lower() != "ready_for_ticket":
            continue
        severity = (
            severity_match.group(1).strip().lower()
            if severity_match is not None and severity_match.group(1).strip()
            else "medium"
        )
        if severity not in severities:
            continue
        owner_root = repo_root.resolve()
        key = _ticket_key(owner_root, fingerprint)
        if key in processed:
            continue
        title = title_match.group(1).strip() if title_match is not None else ticket_path.stem
        execution_domain = (
            execution_domain_match.group(1).strip()
            if execution_domain_match is not None and execution_domain_match.group(1).strip()
            else "unknown"
        )
        explicit_conflict_keys = (
            tuple(
                value.strip()
                for value in re.findall(r"`([^`]+)`", conflict_line_match.group(1))
                if value.strip()
            )
            if conflict_line_match is not None
            else ()
        )
        if explicit_conflict_keys:
            conflict_keys = explicit_conflict_keys
        else:
            conflict_keys = (f"ticket:{fingerprint}",)
            _print(
                f"WARNING ready ticket {ticket_path.name} missing conflict metadata; "
                f"using fallback conflict key {conflict_keys[0]!r}"
            )
        candidates.append(
            BatchCandidate(
                source_name=source_name,
                export_path=export_path,
                fingerprint=fingerprint,
                severity=severity,
                title=title,
                owner_root=owner_root,
                ticket_path=ticket_path.resolve(),
                execution_domain=execution_domain,
                execution_conflict_keys=conflict_keys,
            )
        )
    return candidates


def _ready_queue_has_work(repo_root: Path) -> bool:
    ready_dir = repo_root / ".agents" / "plans" / "2 - ready"
    if not ready_dir.exists():
        return False
    for ticket_path in ready_dir.glob("*.md"):
        try:
            markdown = ticket_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not is_generated_backlog_ticket(markdown):
            continue
        export_kind_match = re.search(
            r"^-\s*Export kind:\s*`([^`]+)`\s*$",
            markdown,
            flags=re.MULTILINE,
        )
        stage_match = re.search(
            r"^-\s*Stage:\s*`([^`]+)`\s*$",
            markdown,
            flags=re.MULTILINE,
        )
        if (
            export_kind_match is not None
            and export_kind_match.group(1).strip().lower() == "implementation"
            and stage_match is not None
            and stage_match.group(1).strip().lower() == "ready_for_ticket"
        ):
            return True
    return False


def _collect_wave_candidates(
    *,
    repo_root: Path,
    owner_root: Path | None = None,
    repo_input: str,
    backlog_python: Path,
    refresh_agent: str,
    refresh_model: str | None,
    batch_dir_path: Path,
    sources: list[BacklogSource],
    severities: set[str],
    processed: set[str],
    refresh_state: dict[str, SourceRefreshState],
) -> list[BatchCandidate]:
    owner_root = (owner_root or repo_root).resolve()
    by_key: dict[str, BatchCandidate] = {}
    for source in sources:
        export_path = _export_path(source)
        for candidate in _load_ready_queue_candidates(
            repo_root=owner_root,
            source_name=source.name,
            export_path=export_path,
            severities=severities,
            processed=processed,
        ):
            by_key.setdefault(candidate.ticket_key, candidate)
    if by_key:
        _print(f"QUEUE_READY candidates={len(by_key)} refresh_skipped=true")
        return sorted(
            by_key.values(),
            key=lambda item: (
                SEVERITY_RANK.get(item.severity, 99),
                item.execution_domain,
                item.title.lower(),
                item.ticket_key,
            ),
        )
    if _ready_queue_has_work(owner_root):
        _print("QUEUE_READY candidates=0 refresh_skipped=true")
        return []

    shared_inputs_fingerprint = _shared_backlog_inputs_fingerprint(repo_root)
    for source in sources:
        source_fingerprint = _source_inputs_fingerprint(
            repo_root=repo_root,
            source=source,
            shared_inputs_fingerprint=shared_inputs_fingerprint,
        )
        current_state = refresh_state.get(source.name)
        # Reaching this branch means no implementation-ready work remains. A prior
        # export cannot be reused: plan folders and outcome ledgers can change without
        # changing run-history metadata, and regeneration must earn a fresh two-cycle
        # shadow receipt immediately before export.
        export_path = _refresh_backlog(
            repo_root=repo_root,
            owner_root=owner_root,
            source=source,
            repo_input=repo_input,
            backlog_python=backlog_python,
            agent=refresh_agent,
            model=refresh_model,
            batch_dir_path=batch_dir_path,
        )
        refresh_state[source.name] = SourceRefreshState(
            input_fingerprint=source_fingerprint,
            export_path=export_path,
            refreshes=(current_state.refreshes if current_state is not None else 0) + 1,
            reuses=(current_state.reuses if current_state is not None else 0),
        )
        _print(
            f"REFRESH source={source.name} fingerprint={source_fingerprint[:12]} "
            f"export={export_path}"
        )
        for candidate in _load_candidates(
            source_name=source.name,
            export_path=export_path,
            severities=severities,
            processed=processed,
        ):
            by_key.setdefault(candidate.ticket_key, candidate)
    return sorted(
        by_key.values(),
        key=lambda item: (
            SEVERITY_RANK.get(item.severity, 99),
            item.execution_domain,
            item.title.lower(),
            item.ticket_key,
        ),
    )


def _claim_ticket(*, candidate: BatchCandidate, repo_root: Path) -> Path:
    path = move_ticket_file(
        owner_root=candidate.owner_root,
        fingerprint=candidate.fingerprint,
        to_bucket="3 - in_progress",
        dry_run=False,
    ).resolve()
    _sync_ticket_atom_actions(owner_root=candidate.owner_root)
    return path


def _requeue_ticket(*, candidate: BatchCandidate, repo_root: Path) -> Path:
    path = move_ticket_file(
        owner_root=candidate.owner_root,
        fingerprint=candidate.fingerprint,
        to_bucket="2 - ready",
        dry_run=False,
    ).resolve()
    _sync_ticket_atom_actions(owner_root=candidate.owner_root)
    return path


def _move_ticket_for_review(*, candidate: BatchCandidate, repo_root: Path) -> Path:
    path = move_ticket_file(
        owner_root=candidate.owner_root,
        fingerprint=candidate.fingerprint,
        to_bucket="4 - for_review",
        dry_run=False,
    ).resolve()
    _sync_ticket_atom_actions(owner_root=candidate.owner_root)
    return path


def _pick_launchable_candidate_index(
    queue: list[BatchCandidate],
    active_conflict_keys: set[str],
) -> int | None:
    for index, candidate in enumerate(queue):
        if set(candidate.execution_conflict_keys) & active_conflict_keys:
            continue
        return index
    return None


def _run_common_settings(
    *,
    run_settings_path: Path,
    run_settings_profile: str,
) -> dict[str, Any]:
    if not run_settings_path.exists():
        return {}
    settings_doc = _load_yaml(run_settings_path)
    profiles = settings_doc.get("profiles", {})
    profile_name = (
        run_settings_profile
        if run_settings_profile.strip()
        else str(settings_doc.get("default_profile") or "default")
    )
    if not isinstance(profiles, dict) or not isinstance(profiles.get(profile_name), dict):
        return {}
    profile = profiles[profile_name]
    run_common = profile.get("run_common", {})
    return dict(run_common) if isinstance(run_common, dict) else {}


def _bool_setting(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _docker_resource_reason(reason_id: str) -> dict[str, str]:
    return {
        "reason_id": reason_id,
        "summary": DOCKER_RESOURCE_PLAN_REASON_SUMMARIES[reason_id],
    }


def _build_docker_resource_plan(
    *,
    repo_root: Path,
    exec_backend: str,
    run_settings_path: Path,
    run_settings_profile: str,
    repo_input: str,
    maintenance_image_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Build the batch-level audit plan for Docker resource use.

    The resulting plan is the scheduler contract for Docker-backed batch launches.  The
    Docker-wide scheduler guard may only be omitted when this plan proves image resolution is
    batch-scoped and cleanup will not mutate shared Docker state during ticket execution.
    """

    backend = exec_backend.strip().lower()
    if backend != "docker":
        return None

    run_common = _run_common_settings(
        run_settings_path=run_settings_path,
        run_settings_profile=run_settings_profile,
    )
    requested_profile_raw = run_common.get("exec_docker_profile")
    requested_profile = (
        requested_profile_raw.strip()
        if isinstance(requested_profile_raw, str) and requested_profile_raw.strip()
        else None
    )
    from usertest_implement.shared import (
        _maintenance_profile_is_eligible,
        _resolve_exec_docker_profile,
    )

    maintenance_eligible = _maintenance_profile_is_eligible(
        repo_root=repo_root,
        repo_input=repo_input,
    )
    docker_profile = _resolve_exec_docker_profile(
        exec_backend=backend,
        requested_profile=requested_profile,
        maintenance_eligible=maintenance_eligible,
    )

    cache_mode = str(run_common.get("exec_cache") or "warm").strip().lower() or "warm"
    warm_cache = cache_mode == "warm"
    maintenance_venv_cache_configured = _bool_setting(
        run_common.get("maintenance_venv_cache"),
        default=True,
    )
    maintenance_venv_cache = bool(warm_cache and maintenance_venv_cache_configured)
    cleanup_on_prepare = False
    if docker_profile == "maintenance":
        maintenance_cfg = _load_maintenance_docker_config(repo_root=repo_root)
        cleanup_on_prepare = bool(
            maintenance_cfg.cleanup_enabled and maintenance_cfg.cleanup_on_prepare
        )

    pre_resolved_image_ref = None
    pre_resolved_metadata_path = None
    pre_resolved_image_available = False
    if docker_profile == "maintenance" and isinstance(maintenance_image_metadata, dict):
        image_ref = maintenance_image_metadata.get("image_ref")
        metadata_path = maintenance_image_metadata.get("path")
        if isinstance(image_ref, str) and image_ref.strip():
            pre_resolved_image_available = True
            pre_resolved_image_ref = image_ref.strip()
        if isinstance(metadata_path, str) and metadata_path.strip():
            pre_resolved_metadata_path = metadata_path.strip()

    unsafe_reasons: list[dict[str, str]] = []
    if not pre_resolved_image_available:
        unsafe_reasons.append(_docker_resource_reason("per_ticket_image_resolution"))
    if cleanup_on_prepare:
        unsafe_reasons.append(_docker_resource_reason("cleanup_on_prepare"))
    parallel_safe = not unsafe_reasons
    scheduler_guard = (
        {
            "unchanged": False,
            "conflict_key": None,
            "omitted_conflict_key": "batch_resource:docker",
            "summary": (
                "Docker-backed tickets may launch concurrently when their ticket conflict "
                "keys are disjoint because image resolution is batch-scoped and Docker "
                "cleanup is not run during ticket execution."
            ),
        }
        if parallel_safe
        else {
            "unchanged": True,
            "conflict_key": "batch_resource:docker",
            "summary": (
                "Docker-backed tickets remain serialized by the existing batch resource "
                "conflict key."
            ),
        }
    )

    return {
        "schema_version": 1,
        "generated_at": utc_now_z(),
        "exec_backend": backend,
        "docker_profile": docker_profile,
        "configured_docker_profile": requested_profile,
        "docker_profile_eligible": maintenance_eligible,
        "cache_mode": cache_mode,
        "warm_cache": warm_cache,
        "maintenance_venv_cache_configured": maintenance_venv_cache_configured,
        "maintenance_venv_cache": maintenance_venv_cache,
        "maintenance_venv_cache_strategy": (
            "per-worker-writable-copy"
            if docker_profile == "maintenance" and maintenance_venv_cache
            else "disabled"
        ),
        "cleanup_on_prepare": cleanup_on_prepare,
        "pre_resolved_image_available": pre_resolved_image_available,
        "pre_resolved_image_ref": pre_resolved_image_ref,
        "pre_resolved_metadata_path": pre_resolved_metadata_path,
        "pre_resolved_image": maintenance_image_metadata if pre_resolved_image_available else None,
        "parallel_safe": parallel_safe,
        "unsafe_reasons": unsafe_reasons,
        "scheduler_guard": scheduler_guard,
    }


def _docker_resource_plan_is_parallel_safe(
    docker_resource_plan: dict[str, Any] | None,
) -> bool:
    return (
        isinstance(docker_resource_plan, dict)
        and docker_resource_plan.get("parallel_safe") is True
    )


def _add_batch_resource_conflicts(
    candidate: BatchCandidate,
    *,
    exec_backend: str,
    docker_resource_plan: dict[str, Any] | None = None,
) -> BatchCandidate:
    extra_keys: tuple[str, ...] = ()
    if (
        exec_backend.strip().lower() == "docker"
        and not _docker_resource_plan_is_parallel_safe(docker_resource_plan)
    ):
        extra_keys = ("batch_resource:docker",)
    if not extra_keys:
        return candidate

    merged_keys = tuple(dict.fromkeys((*candidate.execution_conflict_keys, *extra_keys)))
    if merged_keys == candidate.execution_conflict_keys:
        return candidate
    return BatchCandidate(
        source_name=candidate.source_name,
        export_path=candidate.export_path,
        fingerprint=candidate.fingerprint,
        severity=candidate.severity,
        title=candidate.title,
        owner_root=candidate.owner_root,
        ticket_path=candidate.ticket_path,
        execution_domain=candidate.execution_domain,
        execution_conflict_keys=merged_keys,
        retry_count=candidate.retry_count,
    )


def _run_ticket_process(
    *,
    repo_root: Path,
    implement_python: Path,
    batch_dir_path: Path,
    ticket_path: Path,
    repo_input: str,
    worker: WorkerTemplate,
    settings_path: Path,
    settings_profile: str,
    ticket_timeout_seconds: float | None,
    implementation_ref: str | None = None,
    implementation_runs_dir: Path | None = None,
    ledger_path: Path | None = None,
    maintenance_image_metadata_path: Path | None = None,
    implementation_review_agent: str | None = None,
    implementation_review_model: str | None = None,
) -> TicketRunResult:
    command = [
        str(implement_python),
        "-m",
        "usertest_implement.cli",
        "--repo-root",
        str(repo_root),
        "run",
        "--ticket-path",
        str(ticket_path),
        "--repo",
        repo_input,
        "--settings",
        str(settings_path),
        "--settings-profile",
        settings_profile,
        "--agent",
        worker.agent,
        "--no-move-on-start",
    ]
    if implementation_ref is not None:
        command.extend(["--ref", implementation_ref])
    if implementation_runs_dir is not None:
        command.extend(["--runs-dir", str(implementation_runs_dir)])
    if ledger_path is not None:
        command.extend(["--ledger", str(ledger_path)])
    # The agent is always an explicit batch-role choice.  Make the paired model
    # explicit as well, including the empty value that means "this agent's
    # default", so the run-settings profile cannot restore a model selected for
    # another provider.
    command.extend(["--model", worker.model or ""])
    if implementation_review_agent is not None:
        command.extend(["--implementation-review-agent", implementation_review_agent])
        command.extend(
            ["--implementation-review-model", implementation_review_model or ""]
        )
    if maintenance_image_metadata_path is not None:
        command.extend(
            [
                "--exec-maintenance-image-metadata",
                str(maintenance_image_metadata_path),
            ]
        )

    started = time.monotonic()
    proc = subprocess.Popen(
        command,
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    stdout = ""
    stderr = ""
    while True:
        if proc.poll() is not None:
            out, err = proc.communicate()
            stdout = out or ""
            stderr = err or ""
            break
        if (
            ticket_timeout_seconds is not None
            and time.monotonic() - started > ticket_timeout_seconds
        ):
            timed_out = True
            proc.terminate()
            if proc.poll() is None:
                proc.kill()
            out, err = proc.communicate()
            stdout = out or ""
            stderr = err or ""
            break
        time.sleep(1.0)

    duration_seconds = max(0.0, time.monotonic() - started)
    log_root = batch_dir_path / "worker_logs" / f"worker_{worker.worker_index}"
    _write_log(
        log_root / f"{ticket_path.stem}.log",
        command=command,
        returncode=proc.returncode if proc.returncode is not None else 1,
        stdout=stdout,
        stderr=stderr,
    )
    run_dir: Path | None = None
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if lines:
        candidate_run_dir = Path(lines[-1])
        if candidate_run_dir.exists():
            run_dir = candidate_run_dir.resolve()
    return TicketRunResult(
        run_dir=run_dir,
        returncode=int(proc.returncode or 0),
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_seconds=duration_seconds,
    )


def _read_handoff_summary(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "handoff_summary.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _load_batch_config(path: Path) -> dict[str, Any]:
    raw = _load_yaml(path)
    if int(raw.get("version", 0) or 0) != 1:
        raise ValueError(f"Unsupported batch config version in {path}")
    return raw


def _build_workers(config: dict[str, Any]) -> list[WorkerTemplate]:
    defaults = config.get("defaults", {})
    roster = defaults.get("worker_roster", [])
    workers: list[WorkerTemplate] = []
    for index, item in enumerate(roster, start=1):
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent") or "").strip().lower()
        if agent not in VALID_AGENTS:
            raise ValueError(f"Invalid batch worker agent: {agent!r}")
        model_raw = item.get("model")
        model = str(model_raw).strip() if isinstance(model_raw, str) and model_raw.strip() else None
        workers.append(WorkerTemplate(worker_index=index, agent=agent, model=model))
    if not workers:
        raise ValueError("Batch config must define defaults.worker_roster")
    return workers


def _effective_refresh_role(
    *,
    defaults: dict[str, Any],
    workers: list[WorkerTemplate],
) -> tuple[str, str | None, str, str]:
    """Resolve refresh identity without carrying a model across providers."""

    configured_agent = defaults.get("refresh_agent")
    refresh_agent = (
        str(configured_agent).strip().lower()
        if isinstance(configured_agent, str) and configured_agent.strip()
        else workers[0].agent
    )
    if refresh_agent not in VALID_AGENTS:
        raise ValueError(f"Invalid refresh agent: {refresh_agent!r}")
    agent_origin = "batch_config" if configured_agent else "worker_roster"

    if "refresh_model" in defaults:
        configured_model = defaults.get("refresh_model")
        refresh_model = (
            str(configured_model).strip()
            if isinstance(configured_model, str) and configured_model.strip()
            else None
        )
        model_origin = "batch_config" if refresh_model is not None else "agent_default"
    elif refresh_agent == workers[0].agent:
        refresh_model = workers[0].model
        model_origin = "worker_roster" if refresh_model is not None else "agent_default"
    else:
        refresh_model = None
        model_origin = "agent_default"
    return refresh_agent, refresh_model, agent_origin, model_origin


def _effective_implementation_review_role(
    *,
    defaults: dict[str, Any],
    run_common: dict[str, Any],
) -> tuple[str | None, str | None, str, str]:
    """Resolve the reviewer identity actually supplied to ticket runs."""

    configured_agent = defaults.get("implementation_review_agent")
    settings_agent = run_common.get("implementation_review_agent")
    agent_raw = configured_agent if configured_agent is not None else settings_agent
    agent = (
        str(agent_raw).strip().lower()
        if isinstance(agent_raw, str) and agent_raw.strip()
        else None
    )
    if agent is not None and agent not in VALID_AGENTS:
        raise ValueError(f"Invalid implementation review agent: {agent!r}")
    agent_origin = (
        "batch_config"
        if configured_agent is not None
        else ("run_settings" if settings_agent is not None else "not_configured")
    )

    if configured_agent is not None or "implementation_review_model" in defaults:
        # A batch-level agent selection owns the paired model.  Missing/blank
        # means the selected provider's default, not a run-settings model.
        configured_model = defaults.get("implementation_review_model")
        model = (
            str(configured_model).strip()
            if isinstance(configured_model, str) and configured_model.strip()
            else None
        )
        model_origin = "batch_config" if model is not None else "agent_default"
    else:
        settings_model = run_common.get("implementation_review_model")
        model = (
            str(settings_model).strip()
            if isinstance(settings_model, str) and settings_model.strip()
            else None
        )
        model_origin = "run_settings" if model is not None else "agent_default"
    return agent, model, agent_origin, model_origin


def _preflight_agent_roster(
    *,
    workers: list[WorkerTemplate],
    refresh_agent: str,
    review_agent: str | None,
) -> list[dict[str, Any]]:
    """Return every distinct provider binary required by the batch."""

    roster = [
        {"worker_index": worker.worker_index, "agent": worker.agent, "model": worker.model}
        for worker in workers
    ]
    seen = {worker.agent for worker in workers}
    for role, agent in (("refresh", refresh_agent), ("implementation_review", review_agent)):
        if agent is None or agent in seen:
            continue
        roster.append({"worker_index": None, "role": role, "agent": agent, "model": None})
        seen.add(agent)
    return roster


def _apply_run_overrides(
    config: dict[str, Any],
    *,
    refresh_agent: str | None = None,
    refresh_model: str | None = None,
    worker_agent: str | None = None,
    worker_model: str | None = None,
    implementation_review_agent: str | None = None,
    implementation_review_model: str | None = None,
) -> dict[str, Any]:
    """Apply explicit batch CLI role overrides over the checked-in config."""

    defaults_raw = config.get("defaults")
    if defaults_raw is None:
        defaults: dict[str, Any] = {}
        config["defaults"] = defaults
    elif isinstance(defaults_raw, dict):
        defaults = defaults_raw
    else:
        raise ValueError("Batch config defaults must be a mapping")

    def normalized_agent(value: str | None, *, role: str) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized not in VALID_AGENTS:
            raise ValueError(f"Invalid {role} agent: {normalized!r}")
        return normalized

    def normalized_model(value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    effective_refresh_agent = normalized_agent(refresh_agent, role="refresh")
    effective_refresh_model = normalized_model(refresh_model)
    if effective_refresh_agent is not None:
        defaults["refresh_agent"] = effective_refresh_agent
        if effective_refresh_model is None:
            defaults.pop("refresh_model", None)
    if effective_refresh_model is not None:
        defaults["refresh_model"] = effective_refresh_model

    effective_worker_agent = normalized_agent(worker_agent, role="worker")
    effective_worker_model = normalized_model(worker_model)
    if effective_worker_model is not None and effective_worker_agent is None:
        raise ValueError("--worker-model requires --worker-agent")
    if effective_worker_agent is not None:
        worker: dict[str, Any] = {"agent": effective_worker_agent}
        if effective_worker_model is not None:
            worker["model"] = effective_worker_model
        defaults["worker_roster"] = [worker]

    effective_review_agent = normalized_agent(
        implementation_review_agent,
        role="implementation review",
    )
    effective_review_model = normalized_model(implementation_review_model)
    if effective_review_agent is not None:
        defaults["implementation_review_agent"] = effective_review_agent
        if effective_review_model is None:
            defaults.pop("implementation_review_model", None)
    if effective_review_model is not None:
        defaults["implementation_review_model"] = effective_review_model
    return config


def _build_phases(config: dict[str, Any], *, data_root: Path) -> list[PhaseConfig]:
    phases_raw = config.get("phases", [])
    phases: list[PhaseConfig] = []
    for item in phases_raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        severities = {
            str(value).strip().lower()
            for value in item.get("severities", [])
            if isinstance(value, str) and value.strip()
        }
        if not severities:
            continue
        sources: list[BacklogSource] = []
        for source_raw in item.get("sources", []):
            if not isinstance(source_raw, dict):
                continue
            source_name = str(source_raw.get("name") or "").strip()
            target = str(source_raw.get("target") or "").strip()
            runs_dir_raw = source_raw.get("runs_dir")
            if not source_name or not target or not isinstance(runs_dir_raw, str):
                continue
            sources.append(
                BacklogSource(
                    name=source_name,
                    runs_dir=(data_root / runs_dir_raw).resolve(),
                    target=target,
                    breadth_profile=str(
                        source_raw.get("breadth_profile") or "internal_maintenance"
                    ).strip(),
                    research_ref=str(
                        source_raw.get("research_ref") or "origin/dev"
                    ).strip(),
                    shadow_state_path=(
                        (data_root / str(source_raw["shadow_state_path"])).resolve()
                        if isinstance(source_raw.get("shadow_state_path"), str)
                        and str(source_raw["shadow_state_path"]).strip()
                        else None
                    ),
                )
            )
        if sources:
            phases.append(PhaseConfig(name=name, sources=sources, severities=severities))
    if not phases:
        raise ValueError("Batch config must define at least one phase")
    return phases


def _pin_phase_research_revision(
    phases: list[PhaseConfig],
    *,
    revision: str,
) -> list[PhaseConfig]:
    """Bind every same-repository source to the immutable wave revision."""

    return [
        PhaseConfig(
            name=phase.name,
            severities=set(phase.severities),
            sources=[
                BacklogSource(
                    name=source.name,
                    runs_dir=source.runs_dir,
                    target=source.target,
                    breadth_profile=source.breadth_profile,
                    research_ref=revision,
                    shadow_state_path=source.shadow_state_path,
                )
                for source in phase.sources
            ],
        )
        for phase in phases
    ]


def _record_outcome(
    *,
    batch_dir_path: Path,
    candidate: BatchCandidate,
    worker: WorkerTemplate,
    run_result: TicketRunResult,
    handoff_summary: dict[str, Any] | None,
    failure: dict[str, Any],
) -> None:
    append_jsonl(
        outcomes_path(batch_dir_path),
        {
            "schema_version": 1,
            "recorded_at": utc_now_z(),
            "source_name": candidate.source_name,
            "fingerprint": candidate.fingerprint,
            "title": candidate.title,
            "severity": candidate.severity,
            "execution_domain": candidate.execution_domain,
            "execution_conflict_keys": list(candidate.execution_conflict_keys),
            "worker": {
                "worker_index": worker.worker_index,
                "agent": worker.agent,
                "model": worker.model,
            },
            "retry_count": candidate.retry_count,
            "run_dir": str(run_result.run_dir) if run_result.run_dir is not None else None,
            "returncode": run_result.returncode,
            "timed_out": run_result.timed_out,
            "duration_seconds": run_result.duration_seconds,
            "handoff_summary": handoff_summary,
            "failure": failure,
        },
    )


def _write_batch_token_monitoring_artifacts(batch_dir_path: Path) -> None:
    try:
        from token_monitoring import write_batch_context

        write_batch_context(batch_dir_path)
    except Exception as exc:  # noqa: BLE001
        write_json(
            batch_dir_path / "token_batch_context_error.json",
            {
                "schema_version": 1,
                "type": type(exc).__name__,
                "message": str(exc),
                "non_fatal": True,
                "written_at_utc": utc_now_z(),
            },
        )


def _update_state_lists(
    state: dict[str, Any],
    *,
    remove_in_flight_fingerprint: str | None = None,
) -> None:
    if remove_in_flight_fingerprint is not None:
        state["in_flight"] = [
            item
            for item in state.get("in_flight", [])
            if item.get("fingerprint") != remove_in_flight_fingerprint
        ]


def _record_launch_wave_decision(
    state: dict[str, Any],
    *,
    phase_name: str,
    cycle: int,
    exec_backend: str,
    candidates: list[BatchCandidate],
) -> dict[str, Any]:
    docker_resource_plan = state.get("docker_resource_plan")
    docker_plan_parallel_safe = (
        _docker_resource_plan_is_parallel_safe(docker_resource_plan)
        if isinstance(docker_resource_plan, dict)
        else None
    )
    docker_conflict_key_applied = any(
        "batch_resource:docker" in candidate.execution_conflict_keys
        for candidate in candidates
    )
    wave = {
        "schema_version": 1,
        "phase": phase_name,
        "cycle": cycle,
        "recorded_utc": utc_now_z(),
        "exec_backend": exec_backend.strip().lower(),
        "candidate_count": len(candidates),
        "docker_resource_plan_parallel_safe": docker_plan_parallel_safe,
        "docker_conflict_key_applied": docker_conflict_key_applied,
        "docker_conflict_key": (
            "batch_resource:docker" if docker_conflict_key_applied else None
        ),
        "candidate_conflict_keys": [
            {
                "fingerprint": candidate.fingerprint,
                "execution_domain": candidate.execution_domain,
                "execution_conflict_keys": list(candidate.execution_conflict_keys),
            }
            for candidate in candidates
        ],
    }
    state.setdefault("launch_waves", []).append(wave)
    return wave


def _phase_blocker_id(failure_class: str, handoff_summary: dict[str, Any] | None) -> str:
    if (
        isinstance(handoff_summary, dict)
        and handoff_summary.get("pr_created") is True
        and handoff_summary.get("ci_status") == "failure"
    ):
        return "produced_pr_ci_red"
    mapping = {
        "baseline_repo_regression": "baseline_repo_red",
        "batch_control_plane": "batch_control_plane",
        "verification_control_plane": "verification_control_plane",
        "probe_false_negative": "probe_false_negative",
        "registry_or_auth": "registry_or_auth",
        "infra_transient": "infra_transient",
    }
    return mapping.get(failure_class, failure_class)


def _drain_phase(
    *,
    phase: PhaseConfig,
    repo_root: Path,
    batch_dir_path: Path,
    config: dict[str, Any],
    state: dict[str, Any],
    workers: list[WorkerTemplate],
    backlog_python: Path,
    implement_python: Path,
    settings_path: Path,
    settings_profile: str,
    repo_input: str,
    refresh_state: dict[str, SourceRefreshState],
    exec_backend: str,
    owner_root: Path | None = None,
    wave_base_revision: str | None = None,
    maintenance_image_metadata_path: Path | None = None,
) -> None:
    owner_root = (owner_root or repo_root).resolve()
    defaults = config.get("defaults", {})
    refresh_agent, refresh_model, _, _ = _effective_refresh_role(
        defaults=defaults,
        workers=workers,
    )
    implementation_review_agent, implementation_review_model, _, _ = (
        _effective_implementation_review_role(
            defaults=defaults,
            run_common=_run_common_settings(
                run_settings_path=settings_path,
                run_settings_profile=settings_profile,
            ),
        )
    )
    ticket_timeout_seconds: float | None = None
    ticket_timeout_raw = defaults.get("ticket_timeout_seconds")
    if ticket_timeout_raw not in (None, ""):
        parsed_timeout = float(ticket_timeout_raw)
        if parsed_timeout > 0:
            ticket_timeout_seconds = parsed_timeout
    infra_retry_limit = int(defaults.get("infra_retry_limit") or 1)
    max_phase_cycles = int(defaults.get("max_phase_cycles") or 20)

    state["phase"] = phase.name
    persist_state(batch_dir_path, state)

    completed_keys = {
        entry["ticket_key"]
        for entry in state.get("completed", [])
        if isinstance(entry, dict) and isinstance(entry.get("ticket_key"), str)
    }
    failed_keys = {
        entry["ticket_key"]
        for entry in state.get("failed", [])
        if isinstance(entry, dict) and isinstance(entry.get("ticket_key"), str)
    }
    processed = completed_keys | failed_keys

    worker_summaries = [
        {"worker_index": worker.worker_index, "agent": worker.agent, "model": worker.model}
        for worker in workers
    ]
    _print(
        f"BEGIN phase={phase.name} severities={sorted(phase.severities)} "
        f"workers={worker_summaries}"
    )
    launch_blocked = False

    for cycle in range(1, max_phase_cycles + 1):
        _print(f"PHASE {phase.name} cycle={cycle}")
        candidates = _collect_wave_candidates(
            repo_root=repo_root,
            owner_root=owner_root,
            repo_input=repo_input,
            backlog_python=backlog_python,
            refresh_agent=refresh_agent,
            refresh_model=refresh_model,
            batch_dir_path=batch_dir_path,
            sources=phase.sources,
            severities=phase.severities,
            processed=processed,
            refresh_state=refresh_state,
        )
        candidates = [
            _add_batch_resource_conflicts(
                candidate,
                exec_backend=exec_backend,
                docker_resource_plan=(
                    state.get("docker_resource_plan")
                    if isinstance(state.get("docker_resource_plan"), dict)
                    else None
                ),
            )
            for candidate in candidates
        ]
        if not candidates:
            _print(f"DONE phase={phase.name} cycles={cycle - 1}")
            return

        wave_decision = _record_launch_wave_decision(
            state,
            phase_name=phase.name,
            cycle=cycle,
            exec_backend=exec_backend,
            candidates=candidates,
        )
        persist_state(batch_dir_path, state)
        _print(f"WAVE phase={phase.name} cycle={cycle} candidates={len(candidates)}")
        if exec_backend.strip().lower() == "docker":
            _print(
                f"WAVE_DOCKER_GUARD phase={phase.name} cycle={cycle} "
                f"parallel_safe={wave_decision['docker_resource_plan_parallel_safe']} "
                f"docker_conflict_key_applied="
                f"{wave_decision['docker_conflict_key_applied']}"
            )
        queue = list(candidates)
        next_worker_index = 0
        active_conflict_keys: set[str] = set()
        in_flight: dict[
            Future[TicketRunResult], tuple[BatchCandidate, WorkerTemplate, tuple[str, ...]]
        ] = {}

        with ThreadPoolExecutor(max_workers=len(workers)) as executor:
            while queue or in_flight:
                while not launch_blocked and len(in_flight) < len(workers):
                    launch_index = _pick_launchable_candidate_index(queue, active_conflict_keys)
                    if launch_index is None:
                        break
                    candidate = queue.pop(launch_index)
                    if wave_base_revision is not None:
                        _validate_candidate_wave_revision(
                            candidate=candidate,
                            wave_base_revision=wave_base_revision,
                        )
                    worker = workers[next_worker_index % len(workers)]
                    next_worker_index += 1
                    claimed_path = _claim_ticket(candidate=candidate, repo_root=repo_root)
                    active_conflict_keys.update(candidate.execution_conflict_keys)
                    state.setdefault("in_flight", []).append(
                        {
                            "fingerprint": candidate.fingerprint,
                            "ticket_key": candidate.ticket_key,
                            "title": candidate.title,
                            "severity": candidate.severity,
                            "execution_domain": candidate.execution_domain,
                            "execution_conflict_keys": list(candidate.execution_conflict_keys),
                            "ticket_path": str(claimed_path),
                            "worker": {
                                "worker_index": worker.worker_index,
                                "agent": worker.agent,
                                "model": worker.model,
                            },
                            "launched_utc": utc_now_z(),
                            "retry_count": candidate.retry_count,
                        }
                    )
                    persist_state(batch_dir_path, state)
                    _print(
                        f"LAUNCH phase={phase.name} fingerprint={candidate.fingerprint} "
                        f"worker_index={worker.worker_index} agent={worker.agent} "
                        f"model={worker.model or '<default>'} "
                        f"conflict_keys={list(candidate.execution_conflict_keys)}"
                    )
                    future = executor.submit(
                        _run_ticket_process,
                        repo_root=repo_root,
                        implement_python=implement_python,
                        batch_dir_path=batch_dir_path,
                        ticket_path=claimed_path,
                        repo_input=repo_input,
                        worker=worker,
                        settings_path=settings_path,
                        settings_profile=settings_profile,
                        ticket_timeout_seconds=ticket_timeout_seconds,
                        implementation_ref=wave_base_revision,
                        implementation_runs_dir=(
                            candidate.owner_root / "runs" / "usertest_implement"
                        ),
                        ledger_path=(
                            candidate.owner_root
                            / ".agents"
                            / "state"
                            / "backlog_implement_actions.yaml"
                        ),
                        maintenance_image_metadata_path=maintenance_image_metadata_path,
                        implementation_review_agent=implementation_review_agent,
                        implementation_review_model=implementation_review_model,
                    )
                    in_flight[future] = (candidate, worker, candidate.execution_conflict_keys)

                if not in_flight:
                    break

                done, _ = wait(tuple(in_flight.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    candidate, worker, conflict_keys = in_flight.pop(future)
                    active_conflict_keys.difference_update(conflict_keys)
                    processed.add(candidate.ticket_key)
                    _update_state_lists(
                        state,
                        remove_in_flight_fingerprint=candidate.fingerprint,
                    )

                    run_result = future.result()
                    handoff_summary = (
                        _read_handoff_summary(run_result.run_dir)
                        if run_result.run_dir is not None
                        else None
                    )
                    missing_terminal_artifacts = run_result.run_dir is None
                    failure = classify_run_outcome(
                        run_dir=run_result.run_dir or batch_dir_path,
                        handoff_summary=handoff_summary,
                        timed_out=run_result.timed_out,
                        missing_terminal_artifacts=missing_terminal_artifacts,
                    )
                    if run_result.run_dir is not None:
                        write_batch_failure(run_result.run_dir, failure)
                    _record_outcome(
                        batch_dir_path=batch_dir_path,
                        candidate=candidate,
                        worker=worker,
                        run_result=run_result,
                        handoff_summary=handoff_summary,
                        failure=failure,
                    )

                    if failure["failure_class"] == "success":
                        try:
                            _move_ticket_for_review(candidate=candidate, repo_root=repo_root)
                        except Exception:
                            pass
                        state.setdefault("completed", []).append(
                            {
                                "ticket_key": candidate.ticket_key,
                                "fingerprint": candidate.fingerprint,
                                "run_dir": str(run_result.run_dir),
                                "pr_url": (
                                    handoff_summary.get("pr_url")
                                    if isinstance(handoff_summary, dict)
                                    else None
                                ),
                                "completed_utc": utc_now_z(),
                            }
                        )
                        _print(
                            f"SUCCESS phase={phase.name} fingerprint={candidate.fingerprint} "
                            f"run_dir={run_result.run_dir}"
                        )
                    else:
                        retryable = bool(failure.get("retryable"))
                        failure_class = str(failure.get("failure_class") or "")
                        if (
                            retryable
                            and failure_class == "infra_transient"
                            and candidate.retry_count < infra_retry_limit
                        ):
                            _requeue_ticket(candidate=candidate, repo_root=repo_root)
                            queue.append(
                                BatchCandidate(
                                    source_name=candidate.source_name,
                                    export_path=candidate.export_path,
                                    fingerprint=candidate.fingerprint,
                                    severity=candidate.severity,
                                    title=candidate.title,
                                    owner_root=candidate.owner_root,
                                    ticket_path=candidate.ticket_path,
                                    execution_domain=candidate.execution_domain,
                                    execution_conflict_keys=candidate.execution_conflict_keys,
                                    retry_count=candidate.retry_count + 1,
                                )
                            )
                            _print(
                                f"RETRY phase={phase.name} fingerprint={candidate.fingerprint} "
                                f"class={failure_class} retry_count={candidate.retry_count + 1}"
                            )
                        else:
                            try:
                                if (
                                    isinstance(handoff_summary, dict)
                                    and handoff_summary.get("pr_created") is True
                                ):
                                    _move_ticket_for_review(
                                        candidate=candidate,
                                        repo_root=repo_root,
                                    )
                                else:
                                    _requeue_ticket(candidate=candidate, repo_root=repo_root)
                            except Exception:
                                pass
                            state.setdefault("failed", []).append(
                                {
                                    "ticket_key": candidate.ticket_key,
                                    "fingerprint": candidate.fingerprint,
                                    "run_dir": (
                                        str(run_result.run_dir)
                                        if run_result.run_dir is not None
                                        else None
                                    ),
                                    "failure_class": failure_class,
                                    "summary": failure.get("summary"),
                                    "failed_utc": utc_now_z(),
                                }
                            )
                            _print(
                                f"FAIL phase={phase.name} fingerprint={candidate.fingerprint} "
                                f"class={failure_class} summary={failure.get('summary')}"
                            )
                            if bool(failure.get("global_blocker")):
                                blocker_id = _phase_blocker_id(failure_class, handoff_summary)
                                state.setdefault("global_blockers", []).append(
                                    {
                                        "blocker_id": blocker_id,
                                        "class": failure_class,
                                        "summary": str(failure.get("summary") or ""),
                                        "evidence": failure.get("evidence") or {},
                                        "created_utc": utc_now_z(),
                                        "fingerprint": candidate.fingerprint,
                                        "run_dir": (
                                            str(run_result.run_dir)
                                            if run_result.run_dir is not None
                                            else None
                                        ),
                                    }
                                )
                                launch_blocked = True
                    persist_state(batch_dir_path, state)

        if launch_blocked:
            state["status"] = "blocked"
            persist_state(batch_dir_path, state)
            return

    raise RuntimeError(f"Phase {phase.name} exceeded max_phase_cycles={max_phase_cycles}")


def run_batch(
    *,
    repo_root: Path,
    config_path: Path,
    refresh_agent: str | None = None,
    refresh_model: str | None = None,
    worker_agent: str | None = None,
    worker_model: str | None = None,
    implementation_review_agent: str | None = None,
    implementation_review_model: str | None = None,
) -> int:
    config = _load_batch_config(config_path)
    _apply_run_overrides(
        config,
        refresh_agent=refresh_agent,
        refresh_model=refresh_model,
        worker_agent=worker_agent,
        worker_model=worker_model,
        implementation_review_agent=implementation_review_agent,
        implementation_review_model=implementation_review_model,
    )
    workers = _build_workers(config)
    defaults_raw = config.get("defaults")
    defaults = defaults_raw if isinstance(defaults_raw, dict) else {}
    owner_root = _configured_owner_root(code_root=repo_root, config=config)
    phases = _build_phases(config, data_root=owner_root)
    batch_id = new_batch_id()
    batch_dir_path = batch_dir(owner_root, batch_id)
    batch_dir_path.mkdir(parents=True, exist_ok=True)

    run_settings_path = (
        repo_root
        / str(defaults.get("run_settings_path") or "configs/usertest_implement_settings.yaml")
    ).resolve()
    run_settings_profile = str(defaults.get("run_settings_profile") or "default")
    repo_input = str(defaults.get("repo_input") or owner_root)
    run_common = _run_common_settings(
        run_settings_path=run_settings_path,
        run_settings_profile=run_settings_profile,
    )
    effective_refresh_agent, effective_refresh_model, agent_origin, model_origin = (
        _effective_refresh_role(defaults=defaults, workers=workers)
    )
    (
        effective_review_agent,
        effective_review_model,
        review_agent_origin,
        review_model_origin,
    ) = _effective_implementation_review_role(defaults=defaults, run_common=run_common)
    exec_backend = str(run_common.get("exec_backend") or "docker")
    preliminary_docker_resource_plan = _build_docker_resource_plan(
        repo_root=repo_root,
        exec_backend=exec_backend,
        run_settings_path=run_settings_path,
        run_settings_profile=run_settings_profile,
        repo_input=repo_input,
    )
    exec_docker_profile = (
        str(preliminary_docker_resource_plan.get("docker_profile") or "standard")
        if preliminary_docker_resource_plan is not None
        else "standard"
    )

    preflight = run_batch_preflight(
        repo_root=repo_root,
        batch_dir=batch_dir_path,
        batch_config=config,
        worker_roster=_preflight_agent_roster(
            workers=workers,
            refresh_agent=effective_refresh_agent,
            review_agent=effective_review_agent,
        ),
        exec_backend=exec_backend,
        exec_docker_profile=exec_docker_profile,
        resolve_maintenance_image=bool(
            exec_backend.strip().lower() == "docker" and exec_docker_profile == "maintenance"
        ),
    )
    docker_resource_plan = _build_docker_resource_plan(
        repo_root=repo_root,
        exec_backend=exec_backend,
        run_settings_path=run_settings_path,
        run_settings_profile=run_settings_profile,
        repo_input=repo_input,
        maintenance_image_metadata=(
            preflight.get("maintenance_image_metadata")
            if isinstance(preflight.get("maintenance_image_metadata"), dict)
            else None
        ),
    )
    state = build_initial_state(
        batch_id=batch_id,
        batch_commit=preflight["head_sha"],
        batch_branch=preflight["branch"],
        base_ci_run_url=preflight.get("base_ci_run_url"),
        workers=[
            {"worker_index": worker.worker_index, "agent": worker.agent, "model": worker.model}
            for worker in workers
        ],
        docker_resource_plan=docker_resource_plan,
    )
    state["code_root"] = str(repo_root.resolve())
    state["owner_root"] = str(owner_root)
    state["repo_input"] = repo_input
    state["effective_execution_roles"] = {
        "refresh": {
            "agent": effective_refresh_agent,
            "model": effective_refresh_model,
            "agent_origin": "cli_override" if refresh_agent else agent_origin,
            "model_origin": "cli_override" if refresh_model else model_origin,
        },
        "implementation_workers": [
            {"worker_index": worker.worker_index, "agent": worker.agent, "model": worker.model}
            for worker in workers
        ],
        "implementation_workers_origin": (
            "cli_override" if worker_agent else "batch_config"
        ),
        "implementation_review": {
            "agent": effective_review_agent,
            "model": effective_review_model,
            "agent_override": defaults.get("implementation_review_agent"),
            "model_override": defaults.get("implementation_review_model"),
            "agent_origin": (
                "cli_override"
                if implementation_review_agent
                else review_agent_origin
            ),
            "model_origin": (
                "cli_override"
                if implementation_review_model
                else review_model_origin
            ),
        },
    }
    state["global_blockers"] = list(preflight.get("blockers", []))
    if state["global_blockers"]:
        state["status"] = "blocked"
        persist_state(batch_dir_path, state)
        _write_batch_token_monitoring_artifacts(batch_dir_path)
        return 2
    try:
        wave_base_revision = _resolve_wave_base_revision(
            code_root=repo_root,
            configured_ref=str(defaults.get("wave_base_ref") or "origin/dev"),
            receipt_dir=batch_dir_path / "preflight",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        state["global_blockers"].append(
            {
                "blocker_id": "wave_base_revision",
                "class": "baseline_repo_regression",
                "summary": str(exc),
                "evidence": {"type": type(exc).__name__},
                "created_utc": utc_now_z(),
            }
        )
        state["status"] = "blocked"
        persist_state(batch_dir_path, state)
        _write_batch_token_monitoring_artifacts(batch_dir_path)
        return 2
    phases = _pin_phase_research_revision(phases, revision=wave_base_revision)
    state["wave_base_revision"] = wave_base_revision
    state["wave_base_ref"] = str(defaults.get("wave_base_ref") or "origin/dev")
    persist_state(batch_dir_path, state)

    backlog_python = _venv_python(repo_root, "usertest_backlog")
    implement_python = _venv_python(repo_root, "usertest_implement")
    if not backlog_python.exists():
        raise FileNotFoundError(backlog_python)
    if not implement_python.exists():
        raise FileNotFoundError(implement_python)

    refresh_state: dict[str, SourceRefreshState] = {}
    maintenance_image_metadata_path: Path | None = None
    maintenance_image_metadata = preflight.get("maintenance_image_metadata")
    if isinstance(maintenance_image_metadata, dict):
        raw_metadata_path = maintenance_image_metadata.get("path")
        if isinstance(raw_metadata_path, str) and raw_metadata_path.strip():
            maintenance_image_metadata_path = Path(raw_metadata_path).resolve()
    try:
        for phase in phases:
            _drain_phase(
                phase=phase,
                repo_root=repo_root,
                batch_dir_path=batch_dir_path,
                config=config,
                state=state,
                workers=workers,
                backlog_python=backlog_python,
                implement_python=implement_python,
                settings_path=run_settings_path,
                settings_profile=run_settings_profile,
                repo_input=repo_input,
                refresh_state=refresh_state,
                exec_backend=exec_backend,
                owner_root=owner_root,
                wave_base_revision=wave_base_revision,
                maintenance_image_metadata_path=maintenance_image_metadata_path,
            )
            if state.get("status") == "blocked":
                break
        else:
            terminal_proof = _build_terminal_proof(
                code_root=repo_root,
                owner_root=owner_root,
                phases=phases,
                refresh_state=refresh_state,
                wave_base_revision=wave_base_revision,
            )
            terminal_proof_path = batch_dir_path / "terminal_proof.json"
            write_json(terminal_proof_path, terminal_proof)
            state["terminal_proof"] = {
                "path": str(terminal_proof_path),
                "sha256": sha256(terminal_proof_path.read_bytes()).hexdigest(),
                "proof_sha256": terminal_proof["proof_sha256"],
                "passed": terminal_proof["passed"],
                "reasons": terminal_proof["reasons"],
            }
            # A successfully drained pass is allowed to hand freshly created PRs to
            # the outer review/outcome reconciler.  Only the explicit proof may call
            # the overall automated backlog complete.
            state["status"] = (
                "completed"
                if terminal_proof["passed"] is True
                else "awaiting_terminal_proof"
            )
    except Exception as exc:
        state.setdefault("global_blockers", []).append(
            {
                "blocker_id": "batch_control_plane",
                "class": "batch_control_plane",
                "summary": str(exc),
                "evidence": {"type": type(exc).__name__},
                "created_utc": utc_now_z(),
            }
        )
        state["status"] = "failed"
        persist_state(batch_dir_path, state)
        _write_batch_token_monitoring_artifacts(batch_dir_path)
        raise
    persist_state(batch_dir_path, state)
    _write_batch_token_monitoring_artifacts(batch_dir_path)
    print(str(batch_dir_path))
    return (
        0
        if state.get("status") in {"completed", "awaiting_terminal_proof"}
        else 2
    )


def batch_status(
    *,
    repo_root: Path,
    batch_id: str | None = None,
    owner_root: Path | None = None,
) -> int:
    state_root = (owner_root or repo_root).resolve()
    batch_dir_path = batch_dir(state_root, batch_id) if batch_id else latest_batch_dir(state_root)
    if batch_dir_path is None:
        print(json.dumps({"status": "missing"}, indent=2, ensure_ascii=False))
        return 1
    state = load_json(state_path(batch_dir_path))
    if state is None:
        print(
            json.dumps(
                {"status": "missing_state", "batch_dir": str(batch_dir_path)},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(state, indent=2, ensure_ascii=False))
    return 0


def batch_recover(
    *,
    repo_root: Path,
    batch_id: str | None = None,
    owner_root: Path | None = None,
) -> int:
    state_root = (owner_root or repo_root).resolve()
    batch_dir_path = batch_dir(state_root, batch_id) if batch_id else latest_batch_dir(state_root)
    if batch_dir_path is None:
        raise SystemExit("No batch directory found to recover.")
    state = load_json(state_path(batch_dir_path))
    if state is None:
        raise SystemExit(f"Missing batch state: {batch_dir_path}")
    recovered: list[dict[str, Any]] = []
    for entry in state.get("failed", []):
        if not isinstance(entry, dict):
            continue
        fingerprint = str(entry.get("fingerprint") or "").strip()
        run_dir_raw = entry.get("run_dir")
        if not fingerprint or not isinstance(run_dir_raw, str) or not run_dir_raw:
            continue
        ticket_ref = load_json(Path(run_dir_raw) / "ticket_ref.json")
        if not isinstance(ticket_ref, dict):
            continue
        owner_repo = ticket_ref.get("owner_repo")
        if not isinstance(owner_repo, dict):
            continue
        root_raw = owner_repo.get("root")
        if not isinstance(root_raw, str) or not root_raw.strip():
            continue
        owner_root = Path(root_raw).resolve()
        handoff_summary = _read_handoff_summary(Path(run_dir_raw))
        destination_bucket = (
            "4 - for_review"
            if isinstance(handoff_summary, dict) and handoff_summary.get("pr_created") is True
            else "2 - ready"
        )
        new_path = move_ticket_file(
            owner_root=owner_root,
            fingerprint=fingerprint,
            to_bucket=destination_bucket,
            dry_run=False,
        )
        _sync_ticket_atom_actions(owner_root=owner_root)
        recovered.append(
            {"fingerprint": fingerprint, "to_bucket": destination_bucket, "path": str(new_path)}
        )
    for entry in state.get("in_flight", []):
        if not isinstance(entry, dict):
            continue
        fingerprint = str(entry.get("fingerprint") or "").strip()
        ticket_path_raw = entry.get("ticket_path")
        if not fingerprint or not isinstance(ticket_path_raw, str) or not ticket_path_raw:
            continue
        ticket_path = Path(ticket_path_raw)
        owner_root = ticket_path.parents[3] if len(ticket_path.parents) >= 4 else None
        if owner_root is None or not owner_root.exists():
            continue
        new_path = move_ticket_file(
            owner_root=owner_root,
            fingerprint=fingerprint,
            to_bucket="2 - ready",
            dry_run=False,
        )
        _sync_ticket_atom_actions(owner_root=owner_root)
        recovered.append(
            {"fingerprint": fingerprint, "to_bucket": "2 - ready", "path": str(new_path)}
        )
    print(
        json.dumps(
            {"schema_version": 1, "batch_dir": str(batch_dir_path), "recovered": recovered},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def add_batch_subcommands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    batch_p = subparsers.add_parser(
        "batch", help="Run and inspect maintenance implementation batches."
    )
    batch_sub = batch_p.add_subparsers(dest="batch_cmd", required=True)

    run_p = batch_sub.add_parser("run", help="Run the configured maintenance implementation batch.")
    run_p.add_argument(
        "--config",
        type=Path,
        default=Path("configs/backlog_implement_batch.yaml"),
        help="Batch config YAML path (default: configs/backlog_implement_batch.yaml).",
    )
    run_p.add_argument("--refresh-agent", choices=sorted(VALID_AGENTS))
    run_p.add_argument("--refresh-model")
    run_p.add_argument("--worker-agent", choices=sorted(VALID_AGENTS))
    run_p.add_argument("--worker-model")
    run_p.add_argument("--implementation-review-agent", choices=sorted(VALID_AGENTS))
    run_p.add_argument("--implementation-review-model")

    status_p = batch_sub.add_parser("status", help="Show batch state JSON.")
    status_p.add_argument("--batch-id", help="Optional batch id. Defaults to the latest batch.")
    status_p.add_argument("--owner-root", type=Path)

    recover_p = batch_sub.add_parser(
        "recover", help="Recover stale in-progress tickets for the latest batch."
    )
    recover_p.add_argument("--batch-id", help="Optional batch id. Defaults to the latest batch.")
    recover_p.add_argument("--owner-root", type=Path)

    def _cmd_batch_run(args: argparse.Namespace) -> int:
        repo_root = (
            Path(args.repo_root).resolve() if args.repo_root is not None else Path.cwd().resolve()
        )
        return run_batch(
            repo_root=repo_root,
            config_path=args.config.resolve(),
            refresh_agent=args.refresh_agent,
            refresh_model=args.refresh_model,
            worker_agent=args.worker_agent,
            worker_model=args.worker_model,
            implementation_review_agent=args.implementation_review_agent,
            implementation_review_model=args.implementation_review_model,
        )

    def _cmd_batch_status(args: argparse.Namespace) -> int:
        repo_root = (
            Path(args.repo_root).resolve() if args.repo_root is not None else Path.cwd().resolve()
        )
        return batch_status(
            repo_root=repo_root,
            batch_id=getattr(args, "batch_id", None),
            owner_root=getattr(args, "owner_root", None),
        )

    def _cmd_batch_recover(args: argparse.Namespace) -> int:
        repo_root = (
            Path(args.repo_root).resolve() if args.repo_root is not None else Path.cwd().resolve()
        )
        return batch_recover(
            repo_root=repo_root,
            batch_id=getattr(args, "batch_id", None),
            owner_root=getattr(args, "owner_root", None),
        )

    run_p.set_defaults(func=_cmd_batch_run)
    status_p.set_defaults(func=_cmd_batch_status)
    recover_p.set_defaults(func=_cmd_batch_recover)
