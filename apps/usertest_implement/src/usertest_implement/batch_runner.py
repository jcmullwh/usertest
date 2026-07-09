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
    load_atom_actions_yaml,
    reconcile_atom_actions_from_plan_folders,
    write_atom_actions_yaml,
)
from runner_core.execution_backend import _load_maintenance_docker_config

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
from usertest_implement.ledger import load_ledger
from usertest_implement.tickets import move_ticket_file

RESUME_STATE_ARTIFACT_NAME = "ticket_resume_state.json"

LIFECYCLE_AWAITING_VERIFICATION = "awaiting_verification"
LIFECYCLE_AWAITING_CI = "awaiting_ci"
LIFECYCLE_AWAITING_PR_REVIEW = "awaiting_pr_review"
LIFECYCLE_AWAITING_REVIEW = "awaiting_review"
LIFECYCLE_VERIFICATION_FAILED = "verification_failed"
LIFECYCLE_VERIFICATION_FAILED_RESUME_READY = "verification_failed_resume_ready"
LIFECYCLE_CI_FAILED = "ci_failed"
LIFECYCLE_CI_FAILED_RESUME_READY = "ci_failed_resume_ready"
LIFECYCLE_REVIEW_CHANGES_REQUESTED = "review_changes_requested"
LIFECYCLE_REVIEW_FAILED_RESUME_READY = "review_failed_resume_ready"
LIFECYCLE_MERGE_READY = "merge_ready"
LIFECYCLE_COMPLETE = "complete"
LIFECYCLE_IMPLEMENTED_LOCAL = "implemented_local"
_DEFAULT_LEDGER_PATH = Path(".agents/state/backlog_implement_actions.yaml")


def _resolve_batch_ledger_path(*, repo_root: Path, raw: Path | None) -> Path:
    ledger_path = raw if raw is not None else _DEFAULT_LEDGER_PATH
    if ledger_path.is_absolute():
        return ledger_path.resolve()
    return (repo_root / ledger_path).resolve()


SEVERITY_RANK = {
    "blocker": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}
VALID_AGENTS = {"claude", "codex", "gemini"}
LATEST_CODEX_MODEL = "gpt-5.5"
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
PARKED_LIFECYCLE_STATES = {
    LIFECYCLE_AWAITING_VERIFICATION,
    LIFECYCLE_AWAITING_CI,
    LIFECYCLE_AWAITING_PR_REVIEW,
    # Backward-compatible with pre-awaiting_pr_review artifacts.
    LIFECYCLE_AWAITING_REVIEW,
    # Review and CI passed but the merge has not happened yet.
    LIFECYCLE_MERGE_READY,
}
RESUME_READY_LIFECYCLE_STATES = {
    LIFECYCLE_VERIFICATION_FAILED_RESUME_READY,
    LIFECYCLE_CI_FAILED_RESUME_READY,
    LIFECYCLE_REVIEW_FAILED_RESUME_READY,
    # Backward-compatible with pre-resume-ready PR artifacts.
    LIFECYCLE_VERIFICATION_FAILED,
    LIFECYCLE_CI_FAILED,
    LIFECYCLE_REVIEW_CHANGES_REQUESTED,
}
COMPLETE_LIFECYCLE_STATES = {LIFECYCLE_COMPLETE}
LOCAL_COMPLETE_LIFECYCLE_STATES = {LIFECYCLE_IMPLEMENTED_LOCAL}

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


def _sync_ticket_atom_actions(*, repo_root: Path, owner_root: Path) -> None:
    atom_actions_path = repo_root / "configs" / "backlog_atom_actions.yaml"
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
    resume_state_path: Path | None = None
    resume_lifecycle_state: str | None = None
    resume_attempt_count: int = 0

    @property
    def is_resume(self) -> bool:
        return self.resume_state_path is not None

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


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_ticket_metadata(path: Path) -> dict[str, str]:
    try:
        markdown = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    title_match = re.search(r"^#\s+(.+)$", markdown, flags=re.MULTILINE)
    if title_match is not None:
        out["title"] = title_match.group(1).strip()
    for key, label in (
        ("severity", "Severity"),
        ("execution_domain", "Execution domain"),
    ):
        match = re.search(
            rf"^-\s*{re.escape(label)}:\s*`([^`]+)`\s*$",
            markdown,
            flags=re.MULTILINE,
        )
        if match is not None and match.group(1).strip():
            out[key] = match.group(1).strip()
    conflict_line_match = re.search(
        r"^-\s*Execution conflict keys:\s*(.+)$",
        markdown,
        flags=re.MULTILINE,
    )
    if conflict_line_match is not None:
        conflicts = [
            value.strip()
            for value in re.findall(r"`([^`]+)`", conflict_line_match.group(1))
            if value.strip()
        ]
        if conflicts:
            out["execution_conflict_keys"] = "\n".join(conflicts)
    return out


def _candidate_clone(
    candidate: BatchCandidate,
    *,
    retry_count: int | None = None,
    execution_conflict_keys: tuple[str, ...] | None = None,
) -> BatchCandidate:
    return BatchCandidate(
        source_name=candidate.source_name,
        export_path=candidate.export_path,
        fingerprint=candidate.fingerprint,
        severity=candidate.severity,
        title=candidate.title,
        owner_root=candidate.owner_root,
        ticket_path=candidate.ticket_path,
        execution_domain=candidate.execution_domain,
        execution_conflict_keys=(
            execution_conflict_keys
            if execution_conflict_keys is not None
            else candidate.execution_conflict_keys
        ),
        retry_count=candidate.retry_count if retry_count is None else retry_count,
        resume_state_path=candidate.resume_state_path,
        resume_lifecycle_state=candidate.resume_lifecycle_state,
        resume_attempt_count=candidate.resume_attempt_count,
    )


def _remove_ticket_key(entries: object, ticket_key: str) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    return [
        item
        for item in entries
        if not (isinstance(item, dict) and item.get("ticket_key") == ticket_key)
    ]


def _remember_state_entry(
    state: dict[str, Any],
    list_name: str,
    entry: dict[str, Any],
) -> None:
    ticket_key = str(entry.get("ticket_key") or "")
    if ticket_key:
        for key in ("parked", "resume_ready", "completed", "failed"):
            state[key] = _remove_ticket_key(state.get(key), ticket_key)
    state.setdefault(list_name, []).append(entry)


def _load_resume_state_for_run(run_dir: Path | None) -> dict[str, Any] | None:
    if run_dir is None:
        return None
    return _read_json_if_exists(run_dir / RESUME_STATE_ARTIFACT_NAME)


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


def _refresh_backlog(
    *,
    repo_root: Path,
    source: BacklogSource,
    repo_input: str,
    backlog_python: Path,
    agent: str,
    model: str,
    batch_dir_path: Path,
) -> Path:
    compiled_dir = _compiled_dir(source)
    compiled_dir.mkdir(parents=True, exist_ok=True)
    env = _batch_subprocess_env(repo_root)
    common = [
        "--repo-root",
        str(repo_root),
        "--runs-dir",
        str(source.runs_dir),
        "--target",
        source.target,
    ]
    log_dir = batch_dir_path / "refresh_logs" / source.name
    _run_logged_command(
        [
            str(backlog_python),
            "-m",
            "usertest_backlog.cli",
            "reports",
            "backlog",
            *common,
            "--repo-input",
            repo_input,
            "--breadth-profile",
            source.breadth_profile,
            "--agent",
            agent,
            "--model",
            model,
        ],
        cwd=repo_root,
        log_path=log_dir / "backlog.log",
        env=env,
    )
    _run_logged_command(
        [
            str(backlog_python),
            "-m",
            "usertest_backlog.cli",
            "reports",
            "intent-snapshot",
            *common,
            "--repo-input",
            repo_input,
        ],
        cwd=repo_root,
        log_path=log_dir / "intent_snapshot.log",
        env=env,
    )
    _run_logged_command(
        [
            str(backlog_python),
            "-m",
            "usertest_backlog.cli",
            "reports",
            "review-ux",
            *common,
            "--repo-input",
            repo_input,
            "--breadth-profile",
            source.breadth_profile,
            "--agent",
            agent,
            "--model",
            model,
        ],
        cwd=repo_root,
        log_path=log_dir / "review_ux.log",
        env=env,
    )
    export_path = _export_path(source)
    _run_logged_command(
        [
            str(backlog_python),
            "-m",
            "usertest_backlog.cli",
            "reports",
            "export-tickets",
            *common,
            "--repo-input",
            repo_input,
            "--stage",
            "ready_for_ticket",
            "--out-json",
            str(export_path),
        ],
        cwd=repo_root,
        log_path=log_dir / "export_tickets.log",
        env=env,
    )
    return export_path


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




def _resume_ledger_path(
    *,
    repo_root: Path,
    defaults: dict[str, Any],
) -> Path:
    raw = defaults.get("ledger")
    if isinstance(raw, Path):
        return _resolve_batch_ledger_path(repo_root=repo_root, raw=raw)
    if isinstance(raw, str) and raw.strip():
        return _resolve_batch_ledger_path(repo_root=repo_root, raw=Path(raw))
    return _resolve_batch_ledger_path(repo_root=repo_root, raw=_DEFAULT_LEDGER_PATH)




def _latest_resume_state_from_ledger_path(
    *,
    repo_root: Path,
    state_path: Path,
) -> tuple[Path, dict[str, Any], int] | None:
    current_path = state_path
    seen: set[Path] = set()
    latest_state: dict[str, Any] | None = None
    latest_path = current_path.resolve()
    attempt_count = 0
    for _ in range(10):
        resolved_path = current_path.resolve()
        if resolved_path in seen:
            break
        seen.add(resolved_path)
        state = _read_json_if_exists(resolved_path)
        if not isinstance(state, dict):
            break
        latest_state = state
        latest_path = resolved_path
        attempts = state.get("resume_attempts")
        if isinstance(attempts, list):
            attempt_count += len(attempts)
        next_raw = _clean_str(state.get("last_resumed_state_path"))
        if next_raw is None:
            return resolved_path, state, attempt_count
        if not isinstance(attempts, list):
            attempt_count += 1
        next_path = Path(next_raw)
        current_path = next_path if next_path.is_absolute() else (repo_root / next_path)
    if latest_state is None:
        return None
    return latest_path, latest_state, attempt_count


def _candidate_from_resume_ledger_entry(
    *,
    repo_root: Path,
    ledger_path: Path,
    fingerprint: str,
    entry: dict[str, Any],
) -> BatchCandidate | None:
    state_path_raw = _clean_str(entry.get("last_resume_state_path"))
    if state_path_raw is None:
        return None
    state_path = Path(state_path_raw)
    if not state_path.is_absolute():
        state_path = (repo_root / state_path).resolve()
    latest = _latest_resume_state_from_ledger_path(
        repo_root=repo_root,
        state_path=state_path,
    )
    if latest is None:
        return None
    state_path, resume_state, resume_attempt_count = latest
    lifecycle = _clean_str(resume_state.get("lifecycle_state")) or _clean_str(
        entry.get("last_resume_lifecycle_state")
    )
    if lifecycle not in RESUME_READY_LIFECYCLE_STATES:
        return None

    ticket = resume_state.get("ticket") if isinstance(resume_state.get("ticket"), dict) else {}
    ticket_path_raw = (
        _clean_str(ticket.get("path"))
        or _clean_str(entry.get("idea_path"))
    )
    if ticket_path_raw is None:
        return None
    ticket_path = Path(ticket_path_raw)
    if not ticket_path.is_absolute():
        ticket_path = (repo_root / ticket_path).resolve()

    owner_root_raw = _clean_str(resume_state.get("owner_root")) or _clean_str(
        entry.get("owner_root")
    )
    owner_root = (
        Path(owner_root_raw).resolve()
        if owner_root_raw is not None
        else repo_root.resolve()
    )
    metadata = _parse_ticket_metadata(ticket_path)
    severity = (metadata.get("severity") or "high").strip().lower()
    if severity not in SEVERITY_RANK:
        severity = "high"
    title = (
        _clean_str(ticket.get("title"))
        or _clean_str(entry.get("title"))
        or metadata.get("title")
        or ticket_path.stem
    )
    execution_domain = metadata.get("execution_domain") or "resume"
    conflict_keys_raw = metadata.get("execution_conflict_keys")
    conflict_keys = (
        tuple(value for value in conflict_keys_raw.splitlines() if value.strip())
        if conflict_keys_raw
        else (f"ticket:{fingerprint}",)
    )
    return BatchCandidate(
        source_name="resume_ready",
        export_path=ledger_path,
        fingerprint=fingerprint,
        severity=severity,
        title=title,
        owner_root=owner_root,
        ticket_path=ticket_path,
        execution_domain=execution_domain,
        execution_conflict_keys=conflict_keys,
        resume_state_path=state_path,
        resume_lifecycle_state=lifecycle,
        resume_attempt_count=resume_attempt_count,
    )


def _collect_resume_ready_candidates(
    *,
    repo_root: Path,
    ledger_path: Path,
    severities: set[str],
    processed: set[str],
) -> list[BatchCandidate]:
    doc = load_ledger(ledger_path)
    actions = doc.get("actions")
    if not isinstance(actions, dict):
        return []
    candidates: list[BatchCandidate] = []
    for fingerprint_raw, entry_raw in actions.items():
        fingerprint = str(fingerprint_raw or "").strip()
        if not fingerprint or not isinstance(entry_raw, dict):
            continue
        candidate = _candidate_from_resume_ledger_entry(
            repo_root=repo_root,
            ledger_path=ledger_path,
            fingerprint=fingerprint,
            entry=entry_raw,
        )
        if candidate is None:
            continue
        if candidate.severity not in severities:
            continue
        if candidate.ticket_key in processed:
            continue
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            SEVERITY_RANK.get(item.severity, 99),
            item.execution_domain,
            item.title.lower(),
            item.ticket_key,
        )
    )
    return candidates


def _collect_wave_candidates(
    *,
    repo_root: Path,
    repo_input: str,
    backlog_python: Path,
    refresh_agent: str,
    refresh_model: str,
    batch_dir_path: Path,
    sources: list[BacklogSource],
    severities: set[str],
    processed: set[str],
    refresh_state: dict[str, SourceRefreshState],
    resume_ledger_path: Path | None = None,
) -> list[BatchCandidate]:
    by_key: dict[str, BatchCandidate] = {}
    if resume_ledger_path is not None:
        for candidate in _collect_resume_ready_candidates(
            repo_root=repo_root,
            ledger_path=resume_ledger_path,
            severities=severities,
            processed=processed,
        ):
            by_key.setdefault(candidate.ticket_key, candidate)
    for source in sources:
        export_path = _export_path(source)
        for candidate in _load_ready_queue_candidates(
            repo_root=repo_root,
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
    if _ready_queue_has_work(repo_root):
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
        if (
            current_state is not None
            and current_state.input_fingerprint == source_fingerprint
            and current_state.export_path.exists()
        ):
            current_state.reuses += 1
            export_path = current_state.export_path
            _print(
                f"REUSE source={source.name} fingerprint={source_fingerprint[:12]} "
                f"export={export_path}"
            )
        else:
            export_path = _refresh_backlog(
                repo_root=repo_root,
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
    _sync_ticket_atom_actions(repo_root=repo_root, owner_root=candidate.owner_root)
    return path


def _requeue_ticket(*, candidate: BatchCandidate, repo_root: Path) -> Path:
    path = move_ticket_file(
        owner_root=candidate.owner_root,
        fingerprint=candidate.fingerprint,
        to_bucket="2 - ready",
        dry_run=False,
    ).resolve()
    _sync_ticket_atom_actions(repo_root=repo_root, owner_root=candidate.owner_root)
    return path


def _move_ticket_for_review(*, candidate: BatchCandidate, repo_root: Path) -> Path:
    path = move_ticket_file(
        owner_root=candidate.owner_root,
        fingerprint=candidate.fingerprint,
        to_bucket="4 - for_review",
        dry_run=False,
    ).resolve()
    _sync_ticket_atom_actions(repo_root=repo_root, owner_root=candidate.owner_root)
    return path


def _move_ticket_complete(*, candidate: BatchCandidate, repo_root: Path) -> Path:
    path = move_ticket_file(
        owner_root=candidate.owner_root,
        fingerprint=candidate.fingerprint,
        to_bucket="5 - complete",
        dry_run=False,
    ).resolve()
    _sync_ticket_atom_actions(repo_root=repo_root, owner_root=candidate.owner_root)
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
    return _candidate_clone(candidate, execution_conflict_keys=merged_keys)


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
    maintenance_image_metadata_path: Path | None = None,
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
    if worker.model is not None:
        command.extend(["--model", worker.model])
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




def _run_resume_process(
    *,
    repo_root: Path,
    implement_python: Path,
    batch_dir_path: Path,
    candidate: BatchCandidate,
    repo_input: str,
    worker: WorkerTemplate,
    settings_path: Path,
    settings_profile: str,
    ticket_timeout_seconds: float | None,
    exec_backend: str,
    maintenance_image_metadata_path: Path | None = None,
) -> TicketRunResult:
    if candidate.resume_state_path is None:
        raise ValueError("resume candidate is missing resume_state_path")
    run_common = _run_common_settings(
        run_settings_path=settings_path,
        run_settings_profile=settings_profile,
    )
    command = [
        str(implement_python),
        "-m",
        "usertest_implement.cli",
        "--repo-root",
        str(repo_root),
        "resume",
        "--resume-state",
        str(candidate.resume_state_path),
        "--repo",
        repo_input,
        "--agent",
        worker.agent,
        "--exec-backend",
        exec_backend.strip().lower() or "docker",
        "--ledger",
        str(
            _resume_ledger_path(
                repo_root=repo_root,
                defaults={"ledger": run_common.get("ledger")},
            )
        ),
    ]
    if worker.model is not None:
        command.extend(["--model", worker.model])
    exec_docker_profile = run_common.get("exec_docker_profile")
    if isinstance(exec_docker_profile, str) and exec_docker_profile.strip():
        command.extend(["--exec-docker-profile", exec_docker_profile.strip()])
    exec_cache = run_common.get("exec_cache")
    if isinstance(exec_cache, str) and exec_cache.strip():
        command.extend(["--exec-cache", exec_cache.strip()])
    if run_common.get("maintenance_venv_cache") is False:
        command.append("--no-maintenance-venv-cache")
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
        log_root / f"{candidate.fingerprint}_resume.log",
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


def _build_phases(config: dict[str, Any], *, repo_root: Path) -> list[PhaseConfig]:
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
                    runs_dir=(repo_root / runs_dir_raw).resolve(),
                    target=target,
                    breadth_profile=str(
                        source_raw.get("breadth_profile") or "internal_maintenance"
                    ).strip(),
                )
            )
        if sources:
            phases.append(PhaseConfig(name=name, sources=sources, severities=severities))
    if not phases:
        raise ValueError("Batch config must define at least one phase")
    return phases


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
            "run_mode": "resume" if candidate.is_resume else "run",
            "resume_state_path": (
                str(candidate.resume_state_path)
                if candidate.resume_state_path is not None
                else None
            ),
            "resume_lifecycle_state": candidate.resume_lifecycle_state,
            "resume_attempt_count": candidate.resume_attempt_count,
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




def _resume_state_lifecycle(resume_state: dict[str, Any] | None) -> str | None:
    if not isinstance(resume_state, dict):
        return None
    return _clean_str(resume_state.get("lifecycle_state"))


def _state_entry_base(
    *,
    candidate: BatchCandidate,
    run_result: TicketRunResult,
    resume_state: dict[str, Any] | None,
) -> dict[str, Any]:
    lifecycle = _resume_state_lifecycle(resume_state)
    state_path = None
    if run_result.run_dir is not None:
        state_path = str(run_result.run_dir / RESUME_STATE_ARTIFACT_NAME)
    return {
        "ticket_key": candidate.ticket_key,
        "fingerprint": candidate.fingerprint,
        "title": candidate.title,
        "run_mode": "resume" if candidate.is_resume else "run",
        "run_dir": str(run_result.run_dir) if run_result.run_dir is not None else None,
        "resume_state_path": state_path,
        "lifecycle_state": lifecycle,
        "blocking_reason": (
            resume_state.get("blocking_reason")
            if isinstance(resume_state, dict)
            else None
        ),
    }


def _record_parked_ticket(
    *,
    state: dict[str, Any],
    candidate: BatchCandidate,
    run_result: TicketRunResult,
    resume_state: dict[str, Any],
) -> None:
    entry = _state_entry_base(
        candidate=candidate,
        run_result=run_result,
        resume_state=resume_state,
    )
    entry["parked_utc"] = utc_now_z()
    _remember_state_entry(state, "parked", entry)


def _record_resume_ready_ticket(
    *,
    state: dict[str, Any],
    candidate: BatchCandidate,
    run_result: TicketRunResult,
    resume_state: dict[str, Any] | None,
    failure: dict[str, Any] | None = None,
) -> None:
    entry = _state_entry_base(
        candidate=candidate,
        run_result=run_result,
        resume_state=resume_state,
    )
    if candidate.resume_state_path is not None:
        entry["resumed_from_resume_state_path"] = str(candidate.resume_state_path)
        entry["resumed_from_lifecycle_state"] = candidate.resume_lifecycle_state
    if failure is not None:
        entry["failure_class"] = failure.get("failure_class")
        entry["summary"] = failure.get("summary")
    entry["resume_ready_utc"] = utc_now_z()
    _remember_state_entry(state, "resume_ready", entry)


def _record_resumed_ticket(
    *,
    state: dict[str, Any],
    candidate: BatchCandidate,
    run_result: TicketRunResult,
    resume_state: dict[str, Any] | None,
) -> None:
    if not candidate.is_resume:
        return
    entry = _state_entry_base(
        candidate=candidate,
        run_result=run_result,
        resume_state=resume_state,
    )
    entry["resumed_from_resume_state_path"] = (
        str(candidate.resume_state_path) if candidate.resume_state_path is not None else None
    )
    entry["resumed_from_lifecycle_state"] = candidate.resume_lifecycle_state
    entry["resumed_utc"] = utc_now_z()
    state.setdefault("resumed", []).append(entry)


def _record_completed_ticket(
    *,
    state: dict[str, Any],
    candidate: BatchCandidate,
    run_result: TicketRunResult,
    handoff_summary: dict[str, Any] | None,
    resume_state: dict[str, Any] | None,
) -> None:
    entry = _state_entry_base(
        candidate=candidate,
        run_result=run_result,
        resume_state=resume_state,
    )
    entry["pr_url"] = (
        handoff_summary.get("pr_url") if isinstance(handoff_summary, dict) else None
    )
    entry["completed_utc"] = utc_now_z()
    _remember_state_entry(state, "completed", entry)


def _record_failed_ticket(
    *,
    state: dict[str, Any],
    candidate: BatchCandidate,
    run_result: TicketRunResult,
    failure: dict[str, Any],
    resume_state: dict[str, Any] | None,
) -> None:
    entry = _state_entry_base(
        candidate=candidate,
        run_result=run_result,
        resume_state=resume_state,
    )
    entry["failure_class"] = str(failure.get("failure_class") or "")
    entry["summary"] = failure.get("summary")
    entry["failed_utc"] = utc_now_z()
    _remember_state_entry(state, "failed", entry)


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
    maintenance_image_metadata_path: Path | None = None,
) -> None:
    defaults = config.get("defaults", {})
    refresh_agent = str(defaults.get("refresh_agent") or workers[0].agent)
    refresh_model = str(defaults.get("refresh_model") or workers[0].model or LATEST_CODEX_MODEL)
    ticket_timeout_seconds: float | None = None
    ticket_timeout_raw = defaults.get("ticket_timeout_seconds")
    if ticket_timeout_raw not in (None, ""):
        parsed_timeout = float(ticket_timeout_raw)
        if parsed_timeout > 0:
            ticket_timeout_seconds = parsed_timeout
    infra_retry_limit = int(defaults.get("infra_retry_limit") or 1)
    resume_retry_limit = int(defaults.get("resume_retry_limit") or 1)
    max_phase_cycles = int(defaults.get("max_phase_cycles") or 20)
    resume_ledger_path = _resume_ledger_path(repo_root=repo_root, defaults=defaults)

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
            repo_input=repo_input,
            backlog_python=backlog_python,
            refresh_agent=refresh_agent,
            refresh_model=refresh_model,
            batch_dir_path=batch_dir_path,
            sources=phase.sources,
            severities=phase.severities,
            processed=processed,
            refresh_state=refresh_state,
            resume_ledger_path=resume_ledger_path,
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
                    worker = workers[next_worker_index % len(workers)]
                    next_worker_index += 1
                    if candidate.is_resume:
                        claimed_path = candidate.ticket_path
                    else:
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
                    if candidate.is_resume:
                        if candidate.resume_attempt_count >= resume_retry_limit:
                            run_result = TicketRunResult(
                                run_dir=None,
                                returncode=2,
                                stdout="",
                                stderr=(
                                    "resume retry limit exhausted before launch: "
                                    f"{candidate.resume_attempt_count} >= {resume_retry_limit}"
                                ),
                                timed_out=False,
                                duration_seconds=0.0,
                            )
                            future = executor.submit(lambda result=run_result: result)
                        else:
                            future = executor.submit(
                                _run_resume_process,
                                repo_root=repo_root,
                                implement_python=implement_python,
                                batch_dir_path=batch_dir_path,
                                candidate=candidate,
                                repo_input=repo_input,
                                worker=worker,
                                settings_path=settings_path,
                                settings_profile=settings_profile,
                                ticket_timeout_seconds=ticket_timeout_seconds,
                                exec_backend=exec_backend,
                                maintenance_image_metadata_path=maintenance_image_metadata_path,
                            )
                    else:
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
                            maintenance_image_metadata_path=maintenance_image_metadata_path,
                        )
                    in_flight[future] = (candidate, worker, candidate.execution_conflict_keys)

                if not in_flight:
                    break

                done, _ = wait(tuple(in_flight.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    candidate, worker, conflict_keys = in_flight.pop(future)
                    active_conflict_keys.difference_update(conflict_keys)
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
                    if (
                        candidate.is_resume
                        and candidate.resume_attempt_count >= resume_retry_limit
                        and run_result.run_dir is None
                    ):
                        failure = {
                            "failure_class": "ticket_regression",
                            "retryable": False,
                            "global_blocker": False,
                            "summary": (
                                "Resume retry limit exhausted "
                                f"({candidate.resume_attempt_count} >= {resume_retry_limit})."
                            ),
                            "evidence": {
                                "resume_state_path": (
                                    str(candidate.resume_state_path)
                                    if candidate.resume_state_path is not None
                                    else None
                                )
                            },
                        }
                    else:
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

                    resume_state = _load_resume_state_for_run(run_result.run_dir)
                    lifecycle = _resume_state_lifecycle(resume_state)
                    _record_resumed_ticket(
                        state=state,
                        candidate=candidate,
                        run_result=run_result,
                        resume_state=resume_state,
                    )

                    if lifecycle in PARKED_LIFECYCLE_STATES and isinstance(resume_state, dict):
                        try:
                            if lifecycle in {
                                LIFECYCLE_AWAITING_PR_REVIEW,
                                LIFECYCLE_AWAITING_REVIEW,
                                LIFECYCLE_MERGE_READY,
                            }:
                                _move_ticket_for_review(
                                    candidate=candidate,
                                    repo_root=repo_root,
                                )
                        except Exception:
                            pass
                        _record_parked_ticket(
                            state=state,
                            candidate=candidate,
                            run_result=run_result,
                            resume_state=resume_state,
                        )
                        processed.add(candidate.ticket_key)
                        _print(
                            f"PARK phase={phase.name} fingerprint={candidate.fingerprint} "
                            f"state={lifecycle} run_dir={run_result.run_dir}"
                        )
                    elif lifecycle in RESUME_READY_LIFECYCLE_STATES:
                        _record_resume_ready_ticket(
                            state=state,
                            candidate=candidate,
                            run_result=run_result,
                            resume_state=resume_state,
                            failure=failure,
                        )
                        processed.add(candidate.ticket_key)
                        _print(
                            f"RESUME_READY phase={phase.name} fingerprint={candidate.fingerprint} "
                            f"state={lifecycle} run_dir={run_result.run_dir}"
                        )
                    elif lifecycle in COMPLETE_LIFECYCLE_STATES:
                        try:
                            _move_ticket_complete(candidate=candidate, repo_root=repo_root)
                        except Exception:
                            pass
                        _record_completed_ticket(
                            state=state,
                            candidate=candidate,
                            run_result=run_result,
                            handoff_summary=handoff_summary,
                            resume_state=resume_state,
                        )
                        processed.add(candidate.ticket_key)
                        _print(
                            f"COMPLETE phase={phase.name} fingerprint={candidate.fingerprint} "
                            f"run_dir={run_result.run_dir}"
                        )
                    elif (
                        failure["failure_class"] == "success"
                        and (lifecycle in LOCAL_COMPLETE_LIFECYCLE_STATES or lifecycle is None)
                    ):
                        try:
                            _move_ticket_for_review(candidate=candidate, repo_root=repo_root)
                        except Exception:
                            pass
                        _record_completed_ticket(
                            state=state,
                            candidate=candidate,
                            run_result=run_result,
                            handoff_summary=handoff_summary,
                            resume_state=resume_state,
                        )
                        processed.add(candidate.ticket_key)
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
                            and not candidate.is_resume
                        ):
                            _requeue_ticket(candidate=candidate, repo_root=repo_root)
                            queue.append(
                                _candidate_clone(candidate, retry_count=candidate.retry_count + 1)
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
                                elif not candidate.is_resume:
                                    _requeue_ticket(candidate=candidate, repo_root=repo_root)
                            except Exception:
                                pass
                            _record_failed_ticket(
                                state=state,
                                candidate=candidate,
                                run_result=run_result,
                                failure=failure,
                                resume_state=resume_state,
                            )
                            processed.add(candidate.ticket_key)
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


def run_batch(*, repo_root: Path, config_path: Path) -> int:
    config = _load_batch_config(config_path)
    workers = _build_workers(config)
    phases = _build_phases(config, repo_root=repo_root)
    defaults = config.get("defaults", {})
    batch_id = new_batch_id()
    batch_dir_path = batch_dir(repo_root, batch_id)
    batch_dir_path.mkdir(parents=True, exist_ok=True)

    run_settings_path = (
        repo_root
        / str(defaults.get("run_settings_path") or "configs/usertest_implement_settings.yaml")
    ).resolve()
    run_settings_profile = str(defaults.get("run_settings_profile") or "default")
    repo_input = str(defaults.get("repo_input") or repo_root)
    run_common = _run_common_settings(
        run_settings_path=run_settings_path,
        run_settings_profile=run_settings_profile,
    )
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
        worker_roster=[
            {"worker_index": worker.worker_index, "agent": worker.agent, "model": worker.model}
            for worker in workers
        ],
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
    state["global_blockers"] = list(preflight.get("blockers", []))
    if state["global_blockers"]:
        state["status"] = "blocked"
        persist_state(batch_dir_path, state)
        _write_batch_token_monitoring_artifacts(batch_dir_path)
        return 2
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
                maintenance_image_metadata_path=maintenance_image_metadata_path,
            )
            if state.get("status") == "blocked":
                break
        else:
            if state.get("resume_ready"):
                state["status"] = "resume_ready"
            elif state.get("parked"):
                state["status"] = "parked"
            else:
                state["status"] = "completed"
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
    return 0 if state.get("status") in {"completed", "parked", "resume_ready"} else 2


def batch_status(*, repo_root: Path, batch_id: str | None = None) -> int:
    batch_dir_path = batch_dir(repo_root, batch_id) if batch_id else latest_batch_dir(repo_root)
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


def batch_recover(*, repo_root: Path, batch_id: str | None = None) -> int:
    batch_dir_path = batch_dir(repo_root, batch_id) if batch_id else latest_batch_dir(repo_root)
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
        _sync_ticket_atom_actions(repo_root=repo_root, owner_root=owner_root)
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
        _sync_ticket_atom_actions(repo_root=repo_root, owner_root=owner_root)
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

    status_p = batch_sub.add_parser("status", help="Show batch state JSON.")
    status_p.add_argument("--batch-id", help="Optional batch id. Defaults to the latest batch.")

    recover_p = batch_sub.add_parser(
        "recover", help="Recover stale in-progress tickets for the latest batch."
    )
    recover_p.add_argument("--batch-id", help="Optional batch id. Defaults to the latest batch.")

    def _cmd_batch_run(args: argparse.Namespace) -> int:
        repo_root = (
            Path(args.repo_root).resolve() if args.repo_root is not None else Path.cwd().resolve()
        )
        return run_batch(repo_root=repo_root, config_path=args.config.resolve())

    def _cmd_batch_status(args: argparse.Namespace) -> int:
        repo_root = (
            Path(args.repo_root).resolve() if args.repo_root is not None else Path.cwd().resolve()
        )
        return batch_status(repo_root=repo_root, batch_id=getattr(args, "batch_id", None))

    def _cmd_batch_recover(args: argparse.Namespace) -> int:
        repo_root = (
            Path(args.repo_root).resolve() if args.repo_root is not None else Path.cwd().resolve()
        )
        return batch_recover(repo_root=repo_root, batch_id=getattr(args, "batch_id", None))

    run_p.set_defaults(func=_cmd_batch_run)
    status_p.set_defaults(func=_cmd_batch_status)
    recover_p.set_defaults(func=_cmd_batch_recover)
