#!/usr/bin/env python
# ruff: noqa: E501
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_WINDOWS_OFFLINE_FIRST_SUCCESS_CMD = (
    r"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline_first_success.ps1"
)
_POSIX_OFFLINE_FIRST_SUCCESS_CMD = "bash ./scripts/offline_first_success.sh"


def _one_command_first_success_remediation() -> str:
    return (
        "Quick fix (recommended): from repo root, run ONE of:\n"
        f"  - Windows PowerShell: `{_WINDOWS_OFFLINE_FIRST_SUCCESS_CMD}`\n"
        f"  - macOS/Linux: `{_POSIX_OFFLINE_FIRST_SUCCESS_CMD}`"
    )


def _missing_dependency_remediation(*, dependency: str, import_name: str) -> str:
    return (
        f"Missing dependency `{dependency}` (import name: `{import_name}`).\n"
        f"{_one_command_first_success_remediation()}\n"
        "Manual fix: `python -m pip install -r requirements-dev.txt`."
    )


try:
    import yaml
except ModuleNotFoundError as exc:
    raise SystemExit(
        _missing_dependency_remediation(dependency="pyyaml", import_name="yaml")
    ) from exc


def _from_source_import_remediation(*, missing_module: str) -> str:
    return (
        f"Missing import `{missing_module}`.\n"
        f"{_one_command_first_success_remediation()}\n"
        "Manual fix (from repo root): install deps + configure PYTHONPATH:\n"
        "  - macOS/Linux: `python -m pip install -r requirements-dev.txt && source scripts/set_pythonpath.sh`\n"
        "  - PowerShell: `python -m pip install -r requirements-dev.txt; . .\\scripts\\set_pythonpath.ps1`"
    )


def _is_missing_module(exc: ModuleNotFoundError, module: str) -> bool:
    name = getattr(exc, "name", None)
    if not name:
        return False
    return name == module or name.startswith(f"{module}.")


try:
    from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once
    from runner_core.pathing import slugify
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "runner_core"):
        raise SystemExit(_from_source_import_remediation(missing_module="runner_core")) from exc
    raise

try:
    from usertest_implement.finalize import finalize_commit, finalize_push
    from usertest_implement.ledger import update_ledger_file
    from usertest_implement.model_detect import infer_observed_model
    from usertest_implement.summarize import iter_implementation_rows, write_jsonl
    from usertest_implement.tickets import (
        build_ticket_index,
        move_ticket_file,
        parse_ticket_markdown_metadata,
        select_next_ticket,
        select_next_ticket_path,
        strip_legacy_source_ticket_lines,
    )
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "usertest_implement"):
        raise SystemExit(_from_source_import_remediation(missing_module="usertest_implement")) from exc
    raise


@dataclass(frozen=True)
class SelectedTicket:
    fingerprint: str
    title: str | None
    export_kind: str | None
    owner_root: Path | None
    idea_path: Path | None
    ticket_markdown: str
    tickets_export_path: Path | None
    export_index: int | None


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


_configure_console_output()


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}, got {type(data).__name__}")
    return data


def _resolve_repo_root(repo_root: Path | None) -> Path:
    if repo_root is None:
        return find_repo_root()
    return repo_root.resolve()


def _load_runner_config(repo_root: Path) -> RunnerConfig:
    agents_cfg = _load_yaml(repo_root / "configs" / "agents.yaml").get("agents", {})
    policies_cfg = _load_yaml(repo_root / "configs" / "policies.yaml").get("policies", {})
    if not isinstance(agents_cfg, dict) or not isinstance(policies_cfg, dict):
        raise ValueError("Invalid configs under configs/.")
    return RunnerConfig(
        repo_root=repo_root,
        runs_dir=repo_root / "runs" / "usertest_implement",
        agents=agents_cfg,
        policies=policies_cfg,
    )


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _git_head_sha(workspace_dir: Path) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(workspace_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha if sha else None


def _wait_for_ci_success(
    *,
    run_dir: Path,
    workspace_dir: Path,
    branch: str,
    head_sha: str,
    workflow: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """
    Wait for GitHub Actions CI to pass for the current branch HEAD before opening a PR.

    This relies on CI being triggered for `push` events on the branch.
    """

    started_utc = _utc_now_z()
    started_monotonic = time.monotonic()
    summary: dict[str, Any] = {
        "schema_version": 1,
        "workflow": workflow,
        "branch": branch,
        "head_sha": head_sha,
        "run_id": None,
        "run_url": None,
        "status": None,
        "conclusion": None,
        "passed": False,
        "error": None,
        "started_at_utc": started_utc,
        "finished_at_utc": None,
        "timeout_seconds": timeout_seconds,
    }

    def _gh_json(argv: list[str]) -> Any:
        proc = subprocess.run(
            argv,
            cwd=str(workspace_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "gh failed")
        try:
            return json.loads(proc.stdout or "null")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"gh returned invalid JSON: {e}") from e

    def _pick_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
        matches = [
            r
            for r in runs
            if isinstance(r, dict) and r.get("headSha") == head_sha and r.get("event") == "push"
        ]
        if not matches:
            matches = [
                r for r in runs if isinstance(r, dict) and r.get("headSha") == head_sha
            ]
        if not matches:
            return None
        matches.sort(key=lambda r: str(r.get("createdAt") or ""), reverse=True)
        return matches[0]

    run_id: int | None = None
    poll_interval_seconds = 5.0
    limit = 50
    while True:
        elapsed = time.monotonic() - started_monotonic
        if elapsed > timeout_seconds:
            summary["error"] = (
                f"Timed out waiting to find a GitHub Actions run for {workflow} "
                f"(branch={branch}, head_sha={head_sha})."
            )
            summary["finished_at_utc"] = _utc_now_z()
            _write_json(run_dir / "ci_gate.json", summary)
            return summary

        try:
            runs_raw = _gh_json(
                [
                    "gh",
                    "run",
                    "list",
                    "--workflow",
                    workflow,
                    "--branch",
                    branch,
                    "--limit",
                    str(limit),
                    "--json",
                    "databaseId,headSha,event,status,conclusion,createdAt,url",
                ]
            )
        except Exception as e:  # noqa: BLE001
            summary["error"] = f"Failed to list GitHub Actions runs: {e}"
            summary["finished_at_utc"] = _utc_now_z()
            _write_json(run_dir / "ci_gate.json", summary)
            return summary

        runs_list = runs_raw if isinstance(runs_raw, list) else []
        picked = _pick_run([r for r in runs_list if isinstance(r, dict)])
        if picked is not None:
            run_id_raw = picked.get("databaseId")
            run_id_parsed: int | None = None
            if isinstance(run_id_raw, int):
                run_id_parsed = run_id_raw
            elif isinstance(run_id_raw, str) and run_id_raw.strip().isdigit():
                run_id_parsed = int(run_id_raw.strip())

            if run_id_parsed is not None:
                run_id = run_id_parsed
                summary["run_id"] = run_id
                summary["run_url"] = picked.get("url")
                summary["status"] = picked.get("status")
                summary["conclusion"] = picked.get("conclusion")
                _write_json(run_dir / "ci_gate.json", summary)
                break

        time.sleep(poll_interval_seconds)

    assert run_id is not None

    remaining = max(1.0, timeout_seconds - (time.monotonic() - started_monotonic))
    try:
        watch_proc = subprocess.run(
            [
                "gh",
                "run",
                "watch",
                str(run_id),
                "--compact",
                "--exit-status",
                "--interval",
                "10",
            ],
            cwd=str(workspace_dir),
            check=False,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired:
        summary["error"] = f"Timed out waiting for GitHub Actions run {run_id} to complete."
        summary["finished_at_utc"] = _utc_now_z()
        _write_json(run_dir / "ci_gate.json", summary)
        return summary

    try:
        view_raw = _gh_json(
            [
                "gh",
                "run",
                "view",
                str(run_id),
                "--json",
                "status,conclusion,url,headSha,event,createdAt,updatedAt",
            ]
        )
        if isinstance(view_raw, dict):
            summary["status"] = view_raw.get("status")
            summary["conclusion"] = view_raw.get("conclusion")
            summary["run_url"] = view_raw.get("url") or summary.get("run_url")
    except Exception:
        pass

    summary["watch_returncode"] = int(watch_proc.returncode)
    passed = bool(watch_proc.returncode == 0)
    summary["passed"] = passed
    if passed:
        if summary.get("conclusion") is None:
            summary["conclusion"] = "success"
        if summary.get("status") is None:
            summary["status"] = "completed"
    elif not summary.get("error"):
        summary["error"] = (
            f"GitHub Actions CI did not pass (run_id={run_id}, "
            f"returncode={watch_proc.returncode}, conclusion={summary.get('conclusion')!r})."
        )

    summary["finished_at_utc"] = _utc_now_z()
    _write_json(run_dir / "ci_gate.json", summary)
    return summary


def _looks_like_local_path(value: str) -> bool:
    raw = value.strip()
    if not raw:
        return False
    if raw.startswith(("http://", "https://", "git@")):
        return False
    if raw.startswith(("pip:", "pdm:")):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        return True
    if raw.startswith(("\\\\", "/", "./", "../", ".\\", "..\\", "~")):
        return True
    return ("\\" in raw) or ("/" in raw)


def _infer_git_root(path: Path) -> Path | None:
    cur = path.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_remote_url(*, repo_dir: Path, remote_name: str) -> str | None:
    remote = remote_name.strip() or "origin"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", remote],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out if out else None


def _default_backlog_runs_dir(repo_root: Path) -> Path:
    return repo_root / "runs" / "usertest"


def _list_backlog_targets(runs_dir: Path) -> list[str]:
    if not runs_dir.exists():
        return []
    if not runs_dir.is_dir():
        return []
    slugs: list[str] = []
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not name or name.startswith("_"):
            continue
        slugs.append(name)
    slugs.sort()
    return slugs


def _resolve_backlog_target(*, runs_dir: Path, target: str | None) -> str:
    if isinstance(target, str) and target.strip():
        return target.strip()
    candidates = _list_backlog_targets(runs_dir)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SystemExit(
            "Unable to infer --backlog-target because there are no target directories under "
            f"{runs_dir}. Provide --backlog-target or --no-refresh-backlog."
        )
    raise SystemExit(
        "Unable to infer --backlog-target because multiple targets exist under "
        f"{runs_dir}: {', '.join(candidates)}. Provide --backlog-target or --no-refresh-backlog."
    )


def _run_workflow_step(argv: list[str], *, cwd: Path, label: str) -> None:
    cmd = " ".join(argv)
    print(f"[workflow] {label}: {cmd}", file=sys.stderr)
    proc = subprocess.run(argv, cwd=str(cwd), check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def _refresh_backlog_for_ticket_implementation(
    *,
    args: argparse.Namespace,
    repo_root: Path,
) -> None:
    runs_dir = (
        args.backlog_runs_dir.resolve()
        if args.backlog_runs_dir is not None
        else _default_backlog_runs_dir(repo_root)
    )
    target = _resolve_backlog_target(runs_dir=runs_dir, target=args.backlog_target)

    backlog_agent = str(args.backlog_agent) if args.backlog_agent else "claude"
    backlog_model = (
        str(args.backlog_model).strip()
        if isinstance(args.backlog_model, str) and args.backlog_model.strip()
        else None
    )
    review_agent = (
        str(args.review_agent).strip()
        if isinstance(args.review_agent, str) and args.review_agent.strip()
        else backlog_agent
    )
    review_model = (
        str(args.review_model).strip()
        if isinstance(args.review_model, str) and args.review_model.strip()
        else None
    )

    base = [sys.executable, "-m", "usertest_backlog.cli"]
    common = ["--repo-root", str(repo_root), "--runs-dir", str(runs_dir), "--target", target]

    backlog_cmd = base + ["reports", "backlog", *common, "--agent", backlog_agent]
    if backlog_model is not None:
        backlog_cmd.extend(["--model", backlog_model])
    _run_workflow_step(backlog_cmd, cwd=repo_root, label="reports backlog")

    intent_cmd = base + ["reports", "intent-snapshot", *common]
    _run_workflow_step(intent_cmd, cwd=repo_root, label="reports intent-snapshot")

    review_cmd = base + ["reports", "review-ux", *common, "--agent", review_agent]
    if review_model is not None:
        review_cmd.extend(["--model", review_model])
    _run_workflow_step(review_cmd, cwd=repo_root, label="reports review-ux")

    export_cmd = base + ["reports", "export-tickets", *common]
    _run_workflow_step(export_cmd, cwd=repo_root, label="reports export-tickets")


def _fingerprint_from_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _select_ticket_from_export(
    *,
    tickets_export_path: Path,
    fingerprint: str,
) -> SelectedTicket:
    doc = json.loads(tickets_export_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("tickets export must be a JSON object")
    exports_raw = doc.get("exports")
    exports = [e for e in exports_raw if isinstance(e, dict)] if isinstance(exports_raw, list) else []
    if not exports:
        raise ValueError("tickets export has no exports")

    matches: list[tuple[int, dict[str, Any]]] = []
    for idx, export in enumerate(exports):
        export_fp = export.get("fingerprint")
        export_fp_s = export_fp if isinstance(export_fp, str) else None
        if export_fp_s == fingerprint:
            matches.append((idx, export))

    if not matches:
        raise ValueError("No matching export found for the provided selector")
    if len(matches) > 1:
        raise ValueError(f"Selector matched multiple exports: {len(matches)}")

    export_index, export = matches[0]
    export_fp = export.get("fingerprint")
    if not isinstance(export_fp, str) or not export_fp.strip():
        raise ValueError("Export missing fingerprint")

    title = export.get("title")
    title_s = title.strip() if isinstance(title, str) and title.strip() else None
    export_kind = export.get("export_kind")
    export_kind_s = export_kind.strip() if isinstance(export_kind, str) and export_kind.strip() else None

    owner_repo = export.get("owner_repo")
    owner_root: Path | None = None
    idea_path: Path | None = None
    if isinstance(owner_repo, dict):
        root_raw = owner_repo.get("root")
        if isinstance(root_raw, str) and root_raw.strip():
            owner_root = Path(root_raw)
        idea_raw = owner_repo.get("idea_path")
        if isinstance(idea_raw, str) and idea_raw.strip():
            idea_path = Path(idea_raw)

    body = export.get("body_markdown")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Export missing body_markdown")
    body = strip_legacy_source_ticket_lines(body)

    return SelectedTicket(
        fingerprint=export_fp.strip(),
        title=title_s,
        export_kind=export_kind_s,
        owner_root=owner_root,
        idea_path=idea_path,
        ticket_markdown=body,
        tickets_export_path=tickets_export_path,
        export_index=export_index,
    )


def _select_ticket_from_path(ticket_path: Path) -> SelectedTicket:
    text = ticket_path.read_text(encoding="utf-8", errors="replace")
    text = strip_legacy_source_ticket_lines(text)
    meta = parse_ticket_markdown_metadata(text)
    fingerprint = meta.get("fingerprint") or _fingerprint_from_text(text)
    title = meta.get("title")
    export_kind = meta.get("export_kind")

    owner_root: Path | None = None
    try:
        resolved = ticket_path.resolve()
        parts_lower = [p.lower() for p in resolved.parts]
        if ".agents" in parts_lower:
            idx = parts_lower.index(".agents")
            owner_root = Path(*resolved.parts[:idx])
    except Exception:
        owner_root = None

    return SelectedTicket(
        fingerprint=fingerprint,
        title=title,
        export_kind=export_kind,
        owner_root=owner_root,
        idea_path=ticket_path,
        ticket_markdown=text,
        tickets_export_path=None,
        export_index=None,
    )


def _compose_ticket_blob(selected: SelectedTicket) -> str:
    lines: list[str] = []
    lines.append("# Ticket context")
    lines.append(f"- fingerprint: {selected.fingerprint}")
    if selected.title is not None:
        lines.append(f"- title: {selected.title}")
    if selected.export_kind is not None:
        lines.append(f"- export_kind: {selected.export_kind}")
    if selected.owner_root is not None:
        lines.append(f"- owner_repo_root: {selected.owner_root}")
    if selected.tickets_export_path is not None:
        lines.append(f"- tickets_export_path: {selected.tickets_export_path}")
    if selected.export_index is not None:
        lines.append(f"- export_index: {selected.export_index}")
    lines.append("")
    lines.append("# Ticket markdown")
    lines.append(selected.ticket_markdown.rstrip())
    lines.append("")
    return "\n".join(lines)


def _default_branch_name(selected: SelectedTicket) -> str:
    fp_part = selected.fingerprint[:12].lower()
    return f"backlog/{fp_part}"


def _write_pr_manifest(
    *,
    run_dir: Path,
    selected: SelectedTicket,
    branch: str,
    agent: str,
    model: str | None,
) -> tuple[str, str]:
    title = f"{selected.fingerprint}: {selected.title or 'Implement backlog ticket'}"

    def _markdown_fence(text: str) -> str:
        max_run = 0
        cur = 0
        for ch in text:
            if ch == "`":
                cur += 1
                if cur > max_run:
                    max_run = cur
            else:
                cur = 0
        fence_len = max(3, max_run + 1)
        return "`" * fence_len

    ticket_text = selected.ticket_markdown.rstrip()
    ticket_fence = _markdown_fence(ticket_text)

    body_lines: list[str] = []
    body_lines.append(f"Fingerprint: `{selected.fingerprint}`")
    body_lines.append(f"Agent: `{agent}`")
    body_lines.append(f"Model: `{model or 'unknown'}`")
    body_lines.append("")
    body_lines.append("## Ticket (full)")
    body_lines.append("")
    body_lines.append(ticket_fence)
    body_lines.append(ticket_text)
    body_lines.append(ticket_fence)
    body_lines.append("")
    body_lines.append("## Testing")
    body_lines.append("")
    body_lines.append("- [ ] Add notes from `report.json` / `report.md`")
    body = "\n".join(body_lines).rstrip() + "\n"

    manifest_lines: list[str] = []
    manifest_lines.append(f"# {title}")
    manifest_lines.append("")
    manifest_lines.append(body.rstrip())
    manifest_lines.append("")
    manifest_lines.append("## Branch")
    manifest_lines.append("")
    manifest_lines.append(f"- `{branch}`")
    manifest = "\n".join(manifest_lines).rstrip() + "\n"

    (run_dir / "pr_manifest.md").write_text(manifest, encoding="utf-8")
    return title, body


def _require_docker_available() -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit(
            "Docker exec backend is enabled but Docker is not available.\n"
            "\n"
            "Reason: `docker` was not found on PATH.\n"
            "\n"
            "Fix: install Docker (Docker Desktop on Windows/macOS; Docker Engine on Linux) and ensure it is running.\n"
            "\n"
            "Opt out (run without sandboxing): pass `--no-docker` (or `--exec-backend local`)."
        )
    try:
        proc = subprocess.run(
            [docker, "version"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            "Docker exec backend is enabled but Docker is not responding.\n"
            "\n"
            "Reason: `docker version` timed out.\n"
            "\n"
            "Fix: start Docker and try again.\n"
            "\n"
            "Opt out (run without sandboxing): pass `--no-docker` (or `--exec-backend local`)."
        ) from None
    except OSError as e:
        raise SystemExit(
            "Docker exec backend is enabled but Docker is not usable.\n"
            "\n"
            f"Reason: failed to run `docker version`: {e}\n"
            "\n"
            "Fix: install/start Docker and try again.\n"
            "\n"
            "Opt out (run without sandboxing): pass `--no-docker` (or `--exec-backend local`)."
        ) from e

    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip()
        detail_block = f"\n\nDocker output:\n{details}" if details else ""
        raise SystemExit(
            "Docker exec backend is enabled but Docker is not usable.\n"
            "\n"
            "Reason: `docker version` failed (non-zero exit code)."
            f"{detail_block}\n"
            "\n"
            "Fix: start Docker and try again.\n"
            "\n"
            "Opt out (run without sandboxing): pass `--no-docker` (or `--exec-backend local`)."
        )


def _run_selected_ticket(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    cfg: RunnerConfig,
    selected: SelectedTicket,
) -> int:
    repo_input: str | None = None
    repo_is_explicit = False
    if isinstance(args.repo, str) and args.repo.strip():
        repo_input = args.repo.strip()
        repo_is_explicit = True
    elif selected.owner_root is not None:
        repo_input = str(selected.owner_root)
    elif selected.idea_path is not None:
        inferred = _infer_git_root(selected.idea_path.parent)
        repo_input = str(inferred) if inferred is not None else str(selected.idea_path.parent)
    else:
        raise SystemExit("Unable to infer target repo. Provide --repo.")

    # Default handoff flags may be enabled on some subcommands (e.g. tickets run-next).
    # Normalize so disabling an earlier step disables dependent later steps.
    if not bool(args.commit):
        args.push = False
        args.pr = False
    elif not bool(args.push):
        args.pr = False

    if args.push or args.pr:
        if not args.commit:
            raise SystemExit("--push/--pr requires --commit")

    keep_workspace = bool(args.keep_workspace) or bool(args.commit) or bool(args.push) or bool(args.pr)

    verification_commands: list[str] = []
    for cmd in getattr(args, "verification_commands", None) or []:
        if not isinstance(cmd, str) or not cmd.strip():
            raise SystemExit(f"--verify-command entries must be non-empty strings; got {cmd!r}.")
        verification_commands.append(cmd.strip())

    verification_timeout_seconds = getattr(args, "verify_timeout_seconds", None)
    if verification_timeout_seconds is not None and verification_timeout_seconds <= 0:
        verification_timeout_seconds = None

    wants_handoff = bool(args.commit) or bool(args.push) or bool(args.pr)

    # For ticket implementation workflows that create branches/PRs, it's easy to accidentally run
    # the next ticket off whatever branch your local repo currently has checked out.
    #
    # Default to the PR base branch (dev by default) unless the user explicitly provided --ref.
    effective_ref = args.ref
    if wants_handoff and (effective_ref is None or not str(effective_ref).strip()):
        base = str(getattr(args, "base_branch", "") or "").strip()
        if base:
            effective_ref = base

    # Similarly, when a ticket is being turned into a PR, prefer cloning from the repo's
    # configured remote (e.g. origin) so merged changes on the base branch are picked up even
    # if the local checkout is behind.
    effective_repo_input = repo_input
    if (
        wants_handoff
        and not repo_is_explicit
        and isinstance(repo_input, str)
        and _looks_like_local_path(repo_input)
    ):
        repo_path = Path(repo_input).expanduser()
        git_root = _infer_git_root(repo_path)
        if git_root is not None:
            remote_url = _git_remote_url(
                repo_dir=git_root,
                remote_name=str(getattr(args, "remote_name", "origin") or "origin"),
            )
            if remote_url is not None:
                effective_repo_input = remote_url

    if wants_handoff and not verification_commands and not bool(getattr(args, "skip_verify", False)):
        install_gate = "python tools/scaffold/scaffold.py run --all --skip-missing install"
        lint_gate = "python tools/scaffold/scaffold.py run --all --skip-missing lint"
        test_gate = "python tools/scaffold/scaffold.py run --all --skip-missing test"

        if str(args.exec_backend).strip().lower() == "docker":
            scaffold_prefix = (
                'PYTHON_BIN=python; command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python3; '
                '"$PYTHON_BIN" tools/scaffold/scaffold.py run --all --skip-missing '
            )
            verification_commands = [
                "bash ./scripts/smoke.sh",
                f"{scaffold_prefix}install",
                f"{scaffold_prefix}lint",
                f"{scaffold_prefix}test",
            ]
        elif os.name == "nt":
            verification_commands = [
                "powershell -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\smoke.ps1",
                install_gate,
                lint_gate,
                test_gate,
            ]
        else:
            verification_commands = [
                "bash ./scripts/smoke.sh",
                install_gate,
                lint_gate,
                test_gate,
            ]

    exec_cache = str(getattr(args, "exec_cache", "cold") or "cold")
    exec_cache_dir = getattr(args, "exec_cache_dir", None)
    if exec_cache_dir is not None:
        exec_cache_dir = exec_cache_dir.resolve()
    if exec_cache == "warm" and exec_cache_dir is None:
        exec_cache_dir = repo_root / "runs" / "_cache" / "usertest_implement"

    ticket_blob = _compose_ticket_blob(selected)
    request = RunRequest(
        repo=str(effective_repo_input),
        ref=effective_ref,
        agent=str(args.agent),
        policy=str(args.policy),
        persona_id=args.persona_id,
        mission_id=args.mission_id,
        seed=int(args.seed),
        model=args.model,
        agent_config_overrides=tuple(args.agent_config_override or []),
        agent_append_system_prompt=ticket_blob,
        keep_workspace=keep_workspace,
        verification_commands=tuple(verification_commands),
        verification_timeout_seconds=verification_timeout_seconds,
        exec_backend=str(args.exec_backend),
        exec_keep_container=bool(args.exec_keep_container),
        exec_cache=exec_cache,
        exec_cache_dir=exec_cache_dir,
        exec_use_host_agent_login=bool(args.exec_use_host_agent_login),
        exec_use_target_sandbox_cli_install=bool(args.exec_use_target_sandbox_cli_install),
    )

    if args.dry_run:
        selected_dict = asdict(selected)
        selected_dict["owner_root"] = (
            str(selected.owner_root) if selected.owner_root is not None else None
        )
        selected_dict["idea_path"] = str(selected.idea_path) if selected.idea_path is not None else None
        selected_dict["tickets_export_path"] = (
            str(selected.tickets_export_path) if selected.tickets_export_path is not None else None
        )
        payload = {
            "selected_ticket": selected_dict,
            "run_request": {
                "repo": request.repo,
                "ref": request.ref,
                "agent": request.agent,
                "policy": request.policy,
                "persona_id": request.persona_id,
                "mission_id": request.mission_id,
                "seed": request.seed,
                "model": request.model,
                "keep_workspace": request.keep_workspace,
                "exec_backend": request.exec_backend,
                "exec_keep_container": request.exec_keep_container,
                "verification_commands": list(request.verification_commands),
                "verification_timeout_seconds": request.verification_timeout_seconds,
            },
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if str(args.exec_backend).strip().lower() == "docker":
        _require_docker_available()

    if args.move_on_start and selected.owner_root is not None and selected.idea_path is not None:
        try:
            move_ticket_file(
                owner_root=selected.owner_root,
                fingerprint=selected.fingerprint,
                to_bucket="3 - in_progress",
                dry_run=False,
            )
        except Exception as e:
            print(f"WARNING: failed to move ticket to in_progress: {e}", file=sys.stderr)

    started_at = _utc_now_z()
    wall_start = time.monotonic()
    result = run_once(cfg, request)
    finished_at = _utc_now_z()
    duration_seconds = max(0.0, time.monotonic() - wall_start)

    run_dir = result.run_dir
    _write_json(
        run_dir / "timing.json",
        {
            "schema_version": 1,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration_seconds,
        },
    )
    _write_json(
        run_dir / "ticket_ref.json",
        {
            "schema_version": 1,
            "fingerprint": selected.fingerprint,
            "title": selected.title,
            "export_kind": selected.export_kind,
            "tickets_export_path": (
                str(selected.tickets_export_path) if selected.tickets_export_path is not None else None
            ),
            "export_index": selected.export_index,
            "owner_repo": {
                "root": str(selected.owner_root) if selected.owner_root is not None else None,
                "idea_path": str(selected.idea_path) if selected.idea_path is not None else None,
            },
        },
    )

    exit_code = int(result.exit_code or 0)
    verification_failed = False
    failing_verification_command: str | None = None

    verification_configured = bool(request.verification_commands)
    if verification_configured and not bool(getattr(args, "skip_verify", False)):
        verification = _read_json(run_dir / "verification.json")
        if isinstance(verification, dict) and verification.get("passed") is False:
            verification_failed = True
            exit_code = max(exit_code, 2)
            commands = verification.get("commands")
            if isinstance(commands, list):
                for cmd in commands:
                    if not isinstance(cmd, dict):
                        continue
                    cmd_exit = cmd.get("exit_code")
                    if isinstance(cmd_exit, int) and cmd_exit != 0:
                        raw_cmd = cmd.get("command")
                        if isinstance(raw_cmd, str) and raw_cmd.strip():
                            failing_verification_command = raw_cmd.strip()
                        break

            if wants_handoff:
                print(
                    "[implement] ERROR: Verification gate failed; refusing to commit/push/PR.",
                    file=sys.stderr,
                )
            else:
                print("[implement] ERROR: Verification gate failed.", file=sys.stderr)
            print(f"  Run dir: {run_dir}", file=sys.stderr)
            if failing_verification_command is not None:
                print(f"  Failing command: {failing_verification_command}", file=sys.stderr)
            print(
                "  Override (debugging only): rerun with --skip-verify",
                file=sys.stderr,
            )

    handoff_blocked = bool(wants_handoff and verification_failed and not args.skip_verify)

    workspace_ref = _read_json(run_dir / "workspace_ref.json")
    workspace_dir: Path | None = None
    if isinstance(workspace_ref, dict):
        ws = workspace_ref.get("workspace_dir")
        if isinstance(ws, str) and ws.strip():
            workspace_dir = Path(ws)

    branch = args.branch or _default_branch_name(selected)
    commit_message = (
        args.commit_message
        or f"{selected.fingerprint}: {selected.title or 'Implement backlog ticket'}"
    )

    git_ref: dict[str, Any] | None = None
    push_ref: dict[str, Any] | None = None
    pr_ref: dict[str, Any] | None = None

    observed_model = infer_observed_model(run_dir=run_dir)
    commit_performed = False

    if args.commit and not handoff_blocked:
        git_ref = finalize_commit(
            run_dir=run_dir,
            branch=branch,
            commit_message=commit_message,
            git_user_name=args.git_user_name,
            git_user_email=args.git_user_email,
        )
        commit_performed = bool(git_ref.get("commit_performed") is True)

    if args.push and not handoff_blocked:
        if not commit_performed:
            push_ref = {
                "schema_version": 1,
                "remote_name": str(args.remote_name),
                "remote_url": args.remote_url,
                "branch": branch,
                "force_with_lease": bool(args.force_push),
                "pushed": False,
                "stdout": None,
                "stderr": None,
                "error": "Skipping push: no commit was performed.",
            }
            _write_json(run_dir / "push_ref.json", push_ref)
        else:
            candidates: list[Path] = []
            if selected.owner_root is not None and (selected.owner_root / ".git").exists():
                candidates.append(selected.owner_root)
            if _looks_like_local_path(repo_input) and (Path(repo_input) / ".git").exists():
                candidates.append(Path(repo_input))
            push_ref = finalize_push(
                run_dir=run_dir,
                remote_name=str(args.remote_name),
                remote_url=args.remote_url,
                candidate_repo_dirs=candidates,
                branch=branch,
                force_with_lease=bool(args.force_push),
            )

    if (args.push or args.pr) and not handoff_blocked:
        title, body = _write_pr_manifest(
            run_dir=run_dir,
            selected=selected,
            branch=branch,
            agent=str(args.agent),
            model=observed_model,
        )
        pr_ref = {
            "schema_version": 1,
            "requested": bool(args.pr),
            "created": False,
            "url": None,
            "title": title,
            "body": body,
            "agent": str(args.agent),
            "model": observed_model,
            "error": None,
        }
        if args.pr:
            if not commit_performed:
                pr_ref["error"] = "Skipping PR creation: no commit was performed."
            elif shutil.which("gh") is None:
                pr_ref["error"] = "gh not found on PATH"
            else:
                if workspace_dir is None:
                    pr_ref["error"] = "Missing workspace_ref.json; cannot locate workspace"
                else:
                    create_draft = False
                    pr_body = body

                    if bool(args.skip_ci_wait):
                        head_sha = _git_head_sha(workspace_dir)
                        _write_json(
                            run_dir / "ci_gate.json",
                            {
                                "schema_version": 1,
                                "workflow": "CI",
                                "branch": branch,
                                "head_sha": head_sha,
                                "run_id": None,
                                "run_url": None,
                                "status": None,
                                "conclusion": None,
                                "passed": None,
                                "error": None,
                                "skipped": True,
                                "skip_reason": "flag --skip-ci-wait",
                                "started_at_utc": _utc_now_z(),
                                "finished_at_utc": _utc_now_z(),
                                "timeout_seconds": float(args.ci_timeout_seconds or 0),
                            },
                        )
                    else:
                        if not (push_ref is not None and push_ref.get("pushed") is True):
                            pr_ref["error"] = (
                                "Refusing to create PR before CI: branch was not pushed successfully "
                                "(rerun with --push or pass --skip-ci-wait)."
                            )
                            _write_json(
                                run_dir / "ci_gate.json",
                                {
                                    "schema_version": 1,
                                    "workflow": "CI",
                                    "branch": branch,
                                    "head_sha": None,
                                    "run_id": None,
                                    "run_url": None,
                                    "status": None,
                                    "conclusion": None,
                                    "passed": None,
                                    "error": None,
                                    "skipped": True,
                                    "skip_reason": "branch_not_pushed",
                                    "started_at_utc": _utc_now_z(),
                                    "finished_at_utc": _utc_now_z(),
                                    "timeout_seconds": float(args.ci_timeout_seconds or 0),
                                },
                            )
                        else:
                            head_sha = _git_head_sha(workspace_dir)
                            if head_sha is None:
                                pr_ref["error"] = "Unable to determine HEAD SHA for CI gating."
                                _write_json(
                                    run_dir / "ci_gate.json",
                                    {
                                        "schema_version": 1,
                                        "workflow": "CI",
                                        "branch": branch,
                                        "head_sha": None,
                                        "run_id": None,
                                        "run_url": None,
                                        "status": None,
                                        "conclusion": None,
                                        "passed": None,
                                        "error": pr_ref["error"],
                                        "skipped": True,
                                        "skip_reason": "head_sha_unavailable",
                                        "started_at_utc": _utc_now_z(),
                                        "finished_at_utc": _utc_now_z(),
                                        "timeout_seconds": float(args.ci_timeout_seconds or 0),
                                    },
                                )
                            else:
                                ci_timeout = float(args.ci_timeout_seconds or 0)
                                if ci_timeout <= 0:
                                    ci_timeout = 3600
                                ci_ref = _wait_for_ci_success(
                                    run_dir=run_dir,
                                    workspace_dir=workspace_dir,
                                    branch=branch,
                                    head_sha=head_sha,
                                    workflow="CI",
                                    timeout_seconds=ci_timeout,
                                )
                                pr_ref["ci_gate_passed"] = bool(ci_ref.get("passed") is True)
                                pr_ref["ci_gate_run_url"] = ci_ref.get("run_url")
                                if ci_ref.get("passed") is not True:
                                    if bool(args.draft_pr_on_ci_failure):
                                        create_draft = True
                                        ci_err = ci_ref.get("error") or "CI gate failed."
                                        pr_ref["ci_gate_error"] = ci_err
                                        pr_body = (
                                            pr_body.rstrip()
                                            + "\n\n---\n\nCI gate failed (draft PR created):\n\n"
                                            + f"- {ci_err}\n"
                                        )
                                    else:
                                        pr_ref["error"] = ci_ref.get("error") or "CI gate failed."
                                if create_draft:
                                    pr_ref["draft"] = True

                    if pr_ref.get("error"):
                        pass
                    else:
                        pr_ref["body"] = pr_body
                        proc = subprocess.run(
                            [
                                "gh",
                                "pr",
                                "create",
                                "--base",
                                str(args.base_branch),
                                "--title",
                                title,
                                "--body",
                                pr_body,
                                *(["--draft"] if create_draft else []),
                            ],
                            cwd=str(workspace_dir),
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if proc.returncode == 0:
                            pr_ref["created"] = True
                            pr_ref["url"] = proc.stdout.strip() or None
                        else:
                            pr_ref["error"] = (
                                proc.stderr.strip()
                                or proc.stdout.strip()
                                or f"gh failed ({proc.returncode})"
                            )
        _write_json(run_dir / "pr_ref.json", pr_ref)

    if (
        args.move_on_commit
        and selected.owner_root is not None
        and selected.idea_path is not None
        and commit_performed
    ):
        try:
            move_ticket_file(
                owner_root=selected.owner_root,
                fingerprint=selected.fingerprint,
                to_bucket="4 - for_review",
                dry_run=False,
            )
        except Exception as e:
            print(f"WARNING: failed to move ticket to for_review: {e}", file=sys.stderr)

    if args.ledger is not None:
        ledger_path = args.ledger
        if not ledger_path.is_absolute():
            ledger_path = repo_root / ledger_path
        updates: dict[str, Any] = {
            "title": selected.title,
            "owner_root": str(selected.owner_root) if selected.owner_root is not None else None,
            "idea_path": str(selected.idea_path) if selected.idea_path is not None else None,
            "last_run_dir": str(run_dir),
            "last_exit_code": int(result.exit_code),
            "last_started_at": started_at,
            "last_finished_at": finished_at,
            "last_duration_seconds": duration_seconds,
        }
        if git_ref is not None:
            updates["last_branch"] = git_ref.get("branch")
            updates["last_head_commit"] = git_ref.get("head_commit")
        if push_ref is not None and push_ref.get("pushed") is True:
            updates["last_push_remote"] = push_ref.get("remote_name")
            updates["last_push_remote_url"] = push_ref.get("remote_url")
        if pr_ref is not None and isinstance(pr_ref.get("url"), str):
            updates["last_pr_url"] = pr_ref.get("url")

        try:
            update_ledger_file(ledger_path, fingerprint=selected.fingerprint, updates=updates)
        except Exception as e:
            print(f"WARNING: failed to update ledger: {e}", file=sys.stderr)

    if result.report_validation_errors:
        print("[implement] WARNING: report validation failed:", file=sys.stderr)
        for err in result.report_validation_errors:
            print(f"  - {err}", file=sys.stderr)
        exit_code = max(exit_code, 2)

    workspace_dir_str = str(workspace_dir) if workspace_dir else "<workspace not kept>"

    # Best-effort git operations: if the user asked for them and they failed, return non-zero and
    # provide a clear remediation path (changes may remain in the kept workspace).
    if args.commit and git_ref is not None and git_ref.get("error"):
        print("[implement] ERROR: git commit step failed:", file=sys.stderr)
        print(f"  {git_ref.get('error')}", file=sys.stderr)
        print(f"  Workspace: {workspace_dir_str}", file=sys.stderr)
        print("  Remediation:", file=sys.stderr)
        print(f"    cd {workspace_dir_str}", file=sys.stderr)
        print("    git status", file=sys.stderr)
        print("    # fix the issue, then retry commit/push/PR manually or rerun this command", file=sys.stderr)
        exit_code = max(exit_code, 3)

    if (args.push or args.pr) and push_ref is not None and push_ref.get("error"):
        print("[implement] ERROR: git push step failed:", file=sys.stderr)
        print(f"  {push_ref.get('error')}", file=sys.stderr)
        print(f"  Workspace: {workspace_dir_str}", file=sys.stderr)
        print("  Remediation:", file=sys.stderr)
        remote = push_ref.get("remote_name") or args.remote_name
        branch = None
        if isinstance(git_ref, dict):
            branch = git_ref.get("branch")
        if not branch:
            branch = args.branch or "<branch>"
        print(f"    cd {workspace_dir_str}", file=sys.stderr)
        print(f"    git push --set-upstream {remote} {branch}", file=sys.stderr)
        exit_code = max(exit_code, 4)

    if args.pr and pr_ref is not None and pr_ref.get("error"):
        print("[implement] ERROR: PR creation failed:", file=sys.stderr)
        print(f"  {pr_ref.get('error')}", file=sys.stderr)
        print(f"  Workspace: {workspace_dir_str}", file=sys.stderr)
        print("  Remediation:", file=sys.stderr)
        print(f"    cd {workspace_dir_str}", file=sys.stderr)
        print("    gh auth status", file=sys.stderr)
        print("    gh pr create --help", file=sys.stderr)
        exit_code = max(exit_code, 5)

    print(str(run_dir))
    return exit_code


def _cmd_run(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    selected: SelectedTicket
    if args.ticket_path is not None:
        selected = _select_ticket_from_path(args.ticket_path)
    else:
        selected = _select_ticket_from_export(
            tickets_export_path=args.tickets_export,
            fingerprint=str(args.fingerprint),
        )

    return _run_selected_ticket(args=args, repo_root=repo_root, cfg=cfg, selected=selected)


def _cmd_reports_summarize(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)
    runs_dir = args.runs_dir.resolve() if args.runs_dir is not None else cfg.runs_dir
    out_path = (
        args.out.resolve()
        if args.out is not None
        else (runs_dir / "_compiled" / "implementation_metrics.jsonl")
    )
    rows = iter_implementation_rows(
        runs_dir,
        target_slug=args.target,
        repo_input=args.repo_input,
        test_command_regexes=list(args.test_command_regex or []) or None,
    )
    write_jsonl(rows, out_path)
    print(str(out_path))
    return 0


def _cmd_tickets_list(args: argparse.Namespace) -> int:
    owner_root = args.owner_root.resolve()
    index = build_ticket_index(owner_root=owner_root)
    payload = {
        "schema_version": 1,
        "owner_root": str(owner_root),
        "tickets_total": len(index),
        "tickets": [
            {
                "fingerprint": e.fingerprint,
                "paths": [str(p) for p in e.paths],
                "buckets": e.buckets,
                "status": e.status,
            }
            for e in sorted(index.values(), key=lambda x: x.fingerprint)
        ],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_tickets_next(args: argparse.Namespace) -> int:
    owner_root = args.owner_root.resolve()
    index = build_ticket_index(owner_root=owner_root)
    bucket_priority = list(args.bucket_priority or [])
    if not bucket_priority:
        bucket_priority = ["2 - ready", "1.5 - to_plan", "1 - ideas", "0.5 - to_triage"]
    entry = select_next_ticket(index, bucket_priority=bucket_priority)
    if entry is None:
        print("No tickets found.")
        return 0
    payload = {
        "schema_version": 1,
        "owner_root": str(owner_root),
        "fingerprint": entry.fingerprint,
        "paths": [str(p) for p in entry.paths],
        "buckets": entry.buckets,
        "status": entry.status,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_tickets_run_next(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    owner_root = args.owner_root.resolve()
    if bool(args.refresh_backlog):
        _refresh_backlog_for_ticket_implementation(args=args, repo_root=repo_root)

    index = build_ticket_index(owner_root=owner_root)
    bucket_priority = list(args.bucket_priority or [])
    if not bucket_priority:
        bucket_priority = ["2 - ready", "1.5 - to_plan", "1 - ideas", "0.5 - to_triage"]

    kind_priority = list(args.kind_priority or [])
    if not kind_priority:
        kind_priority = ["implementation"]

    selected = select_next_ticket_path(
        index,
        bucket_priority=bucket_priority,
        kind_priority=kind_priority,
    )
    if selected is None:
        print("No tickets found.")
        return 0

    _, ticket_path = selected
    ticket = _select_ticket_from_path(ticket_path)
    return _run_selected_ticket(args=args, repo_root=repo_root, cfg=cfg, selected=ticket)


def _cmd_tickets_move(args: argparse.Namespace) -> int:
    owner_root = args.owner_root.resolve()
    dest = move_ticket_file(
        owner_root=owner_root,
        fingerprint=str(args.fingerprint),
        to_bucket=str(args.to_bucket),
        dry_run=bool(args.dry_run),
    )
    print(str(dest))
    return 0


def _add_run_execution_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="Override target repo input (path or git URL).")
    parser.add_argument("--ref", help="Optional git ref to checkout in the acquired workspace.")

    parser.add_argument("--agent", choices=["claude", "codex", "gemini"], default="codex")
    parser.add_argument("--model", help="Optional model override.")
    parser.add_argument("--policy", default="write")
    parser.add_argument("--persona-id", dest="persona_id")
    parser.add_argument("--mission-id", dest="mission_id", default="implement_backlog_ticket_v1")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--agent-config-override",
        action="append",
        default=[],
        help="Repeatable agent config override strings.",
    )
    parser.add_argument("--keep-workspace", action="store_true", help="Keep workspace directory after run.")

    exec_backend_group = parser.add_mutually_exclusive_group()
    exec_backend_group.add_argument(
        "--exec-backend",
        choices=["docker", "local"],
        default="docker",
        help="Execution backend (default: docker).",
    )
    exec_backend_group.add_argument(
        "--no-docker",
        dest="exec_backend",
        action="store_const",
        const="local",
        help="Opt out of Docker sandboxing (exec_backend=local).",
    )
    run_auth_group = parser.add_mutually_exclusive_group()
    run_auth_group.add_argument(
        "--exec-use-host-agent-login",
        dest="exec_use_host_agent_login",
        action="store_true",
        default=True,
    )
    run_auth_group.add_argument(
        "--exec-use-api-key-auth",
        dest="exec_use_host_agent_login",
        action="store_false",
    )
    parser.add_argument("--exec-use-target-sandbox-cli-install", action="store_true", default=False)
    parser.add_argument(
        "--exec-keep-container",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep Docker container after the run (default: enabled).",
    )

    parser.add_argument(
        "--exec-cache",
        choices=["cold", "warm"],
        default="warm",
        help=(
            "Docker sandbox cache mode (default: warm). "
            "warm: mount a host directory at /cache (persists across runs; used for pip + PDM caches). "
            "cold: do not mount a persistent host cache (/cache is per-container and discarded)."
        ),
    )
    parser.add_argument(
        "--exec-cache-dir",
        type=Path,
        help=(
            "Host directory mounted at /cache when --exec-cache warm. "
            "Defaults to <repo_root>/runs/_cache/usertest_implement."
        ),
    )

    parser.add_argument("--dry-run", action="store_true")

    parser.add_argument(
        "--verify-command",
        action="append",
        dest="verification_commands",
        default=[],
        help=(
            "Repeatable verification command gate that must pass before handing off "
            "(default: run scripts/smoke.{ps1,sh} then scaffold install/lint/test across the repo "
            "(tools/scaffold/scaffold.py run --all ...) "
            "when --commit/--push/--pr)."
        ),
    )
    parser.add_argument(
        "--verify-timeout-seconds",
        type=float,
        default=None,
        help="Optional per-command timeout for --verify-command (non-positive disables).",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Disable default verification gate (useful for debugging).",
    )
    parser.add_argument(
        "--ci-timeout-seconds",
        type=float,
        default=3600,
        help="Timeout waiting for GitHub Actions CI before creating a PR.",
    )
    parser.add_argument(
        "--skip-ci-wait",
        action="store_true",
        help="Skip waiting for GitHub Actions CI before creating a PR (not recommended).",
    )
    parser.add_argument(
        "--draft-pr-on-ci-failure",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If CI does not pass, create a draft PR instead of failing PR creation (default: enabled).",
    )

    parser.add_argument(
        "--commit",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Create branch + commit changes in kept workspace.",
    )
    parser.add_argument("--branch", help="Branch name override.")
    parser.add_argument("--commit-message", dest="commit_message", help="Commit message override.")
    parser.add_argument(
        "--git-user-name",
        dest="git_user_name",
        help="Git user.name used for commits (default: usertest-implement).",
    )
    parser.add_argument(
        "--git-user-email",
        dest="git_user_email",
        help="Git user.email used for commits (default: usertest-implement@local).",
    )

    parser.add_argument(
        "--push",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Push branch to remote.",
    )
    parser.add_argument("--remote-name", default="origin")
    parser.add_argument("--remote-url")
    parser.add_argument("--force-push", dest="force_push", action="store_true")
    parser.add_argument(
        "--base-branch",
        default="dev",
        help="Base branch for PR creation (default: dev).",
    )
    parser.add_argument(
        "--pr",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Best-effort PR creation via gh.",
    )

    parser.add_argument(
        "--move-on-start",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Move ticket file to 3 - in_progress if possible (default: enabled).",
    )
    parser.add_argument(
        "--move-on-commit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Move ticket file to 4 - for_review after --commit (default: enabled).",
    )
    parser.add_argument(
        "--ledger",
        nargs="?",
        const=Path("configs/backlog_implement_actions.yaml"),
        type=Path,
        help=(
            "Optional attempt ledger YAML. If provided without a value, defaults to "
            "<repo_root>/configs/backlog_implement_actions.yaml."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usertest-implement")
    parser.add_argument("--repo-root", type=Path, help="Path to the usertest runner repo root.")

    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run one ticket implementation.")
    ticket_group = run_p.add_mutually_exclusive_group(required=True)
    ticket_group.add_argument(
        "--tickets-export",
        dest="tickets_export",
        type=Path,
        help="Tickets export JSON path.",
    )
    ticket_group.add_argument("--ticket-path", dest="ticket_path", type=Path, help="Ticket markdown path.")
    run_p.add_argument(
        "--fingerprint",
        required=False,
        help="Ticket fingerprint selector (requires --tickets-export).",
    )
    _add_run_execution_args(run_p)

    run_p.set_defaults(func=_cmd_run)

    reports_p = sub.add_parser("reports", help="Reporting utilities.")
    reports_sub = reports_p.add_subparsers(dest="reports_cmd", required=True)
    summarize_p = reports_sub.add_parser("summarize", help="Summarize implementation runs into JSONL.")
    summarize_p.add_argument("--runs-dir", type=Path, help="Runs directory (default: runs/usertest_implement).")
    summarize_p.add_argument("--out", type=Path, help="Output JSONL path.")
    summarize_p.add_argument("--target", help="Optional target slug filter.")
    summarize_p.add_argument("--repo-input", help="Optional repo_input filter.")
    summarize_p.add_argument(
        "--test-command-regex",
        action="append",
        default=[],
        help="Override/extend test command regex patterns.",
    )
    summarize_p.set_defaults(func=_cmd_reports_summarize)

    tickets_p = sub.add_parser("tickets", help="Local ticket queue helpers (from .agents/plans).")
    tickets_sub = tickets_p.add_subparsers(dest="tickets_cmd", required=True)

    tickets_list_p = tickets_sub.add_parser("list", help="List tickets in .agents/plans.")
    tickets_list_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_list_p.set_defaults(func=_cmd_tickets_list)

    tickets_next_p = tickets_sub.add_parser("next", help="Select the next ticket by bucket priority.")
    tickets_next_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_next_p.add_argument("--bucket-priority", action="append", default=[])
    tickets_next_p.set_defaults(func=_cmd_tickets_next)

    tickets_run_next_p = tickets_sub.add_parser(
        "run-next",
        help=(
            "Refresh the backlog + ticket exports, then implement the next local plan ticket "
            "(implementation-only by default; commits/pushes/opens a PR unless disabled)."
        ),
    )
    tickets_run_next_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_run_next_p.add_argument("--bucket-priority", action="append", default=[])
    tickets_run_next_p.add_argument(
        "--kind-priority",
        action="append",
        default=[],
        help=(
            "Ticket kind ordering derived from markdown (repeatable). "
            "Defaults to: implementation."
        ),
    )
    tickets_run_next_p.add_argument(
        "--no-refresh-backlog",
        action="store_false",
        dest="refresh_backlog",
        default=True,
        help="Skip running usertest-backlog refresh steps before selecting the next ticket.",
    )
    tickets_run_next_p.add_argument("--backlog-target", help="Target slug for usertest-backlog refresh.")
    tickets_run_next_p.add_argument(
        "--backlog-runs-dir",
        type=Path,
        help="Runs directory for usertest-backlog refresh (default: <repo_root>/runs/usertest).",
    )
    tickets_run_next_p.add_argument(
        "--backlog-agent",
        choices=["claude", "codex", "gemini"],
        help="Agent CLI used for `usertest-backlog reports backlog`.",
    )
    tickets_run_next_p.add_argument(
        "--backlog-model",
        help="Optional model override for `usertest-backlog reports backlog`.",
    )
    tickets_run_next_p.add_argument(
        "--review-agent",
        choices=["claude", "codex", "gemini"],
        help="Agent CLI used for `usertest-backlog reports review-ux` (default: --backlog-agent).",
    )
    tickets_run_next_p.add_argument(
        "--review-model",
        help="Optional model override for `usertest-backlog reports review-ux`.",
    )
    _add_run_execution_args(tickets_run_next_p)
    tickets_run_next_p.set_defaults(commit=True, push=True, pr=True)
    tickets_run_next_p.set_defaults(func=_cmd_tickets_run_next)

    tickets_move_p = tickets_sub.add_parser("move", help="Move a ticket file between plan buckets.")
    tickets_move_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_move_p.add_argument("--fingerprint", required=True)
    tickets_move_p.add_argument("--to-bucket", required=True)
    tickets_move_p.add_argument("--dry-run", action="store_true")
    tickets_move_p.set_defaults(func=_cmd_tickets_move)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "run":
        if args.ticket_path is None:
            if args.tickets_export is None:
                raise SystemExit(2)
            if not args.fingerprint:
                raise SystemExit("Provide --fingerprint with --tickets-export.")
        raise SystemExit(args.func(args))

    raise SystemExit(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
