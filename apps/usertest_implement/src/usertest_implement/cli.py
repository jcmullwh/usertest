#!/usr/bin/env python
# ruff: noqa: E501
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency `pyyaml` (import name: `yaml`). "
        "Fix: `python -m pip install -r requirements-dev.txt`."
    ) from exc


def _from_source_import_remediation(*, missing_module: str) -> str:
    return (
        f"Missing import `{missing_module}`.\n"
        "This usually means you're running from source without editable installs or PYTHONPATH.\n"
        "\n"
        "Fix (from repo root):\n"
        "  python -m pip install -r requirements-dev.txt\n"
        "  PowerShell: . .\\scripts\\set_pythonpath.ps1\n"
        "  macOS/Linux: source scripts/set_pythonpath.sh\n"
        "\n"
        "Or install editables (recommended):\n"
        "  python -m pip install -e apps/usertest_implement\n"
    )


try:
    from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once
    from runner_core.pathing import slugify
except ModuleNotFoundError as exc:
    if exc.name == "runner_core":
        raise SystemExit(_from_source_import_remediation(missing_module="runner_core")) from exc
    raise

from usertest_implement.finalize import finalize_commit, finalize_push
from usertest_implement.ledger import update_ledger_file
from usertest_implement.summarize import iter_implementation_rows, write_jsonl
from usertest_implement.tickets import (
    build_ticket_index,
    move_ticket_file,
    parse_ticket_markdown_metadata,
    select_next_ticket,
)


@dataclass(frozen=True)
class SelectedTicket:
    fingerprint: str
    ticket_id: str | None
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


def _fingerprint_from_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _select_ticket_from_export(
    *,
    tickets_export_path: Path,
    fingerprint: str | None,
    ticket_id: str | None,
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
        source_ticket = export.get("source_ticket")
        source_ticket_id: str | None = None
        if isinstance(source_ticket, dict):
            tid = source_ticket.get("ticket_id")
            source_ticket_id = tid if isinstance(tid, str) else None

        if fingerprint is not None and export_fp_s == fingerprint:
            matches.append((idx, export))
            continue
        if ticket_id is not None and source_ticket_id == ticket_id:
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

    source_ticket = export.get("source_ticket")
    tid_s: str | None = None
    if isinstance(source_ticket, dict):
        tid = source_ticket.get("ticket_id")
        tid_s = tid.strip() if isinstance(tid, str) and tid.strip() else None

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

    return SelectedTicket(
        fingerprint=export_fp.strip(),
        ticket_id=tid_s,
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
    meta = parse_ticket_markdown_metadata(text)
    fingerprint = meta.get("fingerprint") or _fingerprint_from_text(text)
    ticket_id = meta.get("ticket_id")
    title = meta.get("title")

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
        ticket_id=ticket_id,
        title=title,
        export_kind=None,
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
    if selected.ticket_id is not None:
        lines.append(f"- ticket_id: {selected.ticket_id}")
    if selected.title is not None:
        lines.append(f"- title: {selected.title}")
    if selected.export_kind is not None:
        lines.append(f"- export_kind: {selected.export_kind}")
    if selected.owner_root is not None:
        lines.append(f"- owner_repo_root: {selected.owner_root}")
    if selected.idea_path is not None:
        lines.append(f"- idea_path: {selected.idea_path}")
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
    ticket_part = slugify(selected.ticket_id or "ticket").lower()
    fp_part = selected.fingerprint[:12].lower()
    return f"backlog/{ticket_part}-{fp_part}"


def _write_pr_manifest(*, run_dir: Path, selected: SelectedTicket, branch: str) -> tuple[str, str]:
    title = f"{selected.ticket_id or selected.fingerprint}: {selected.title or 'Implement backlog ticket'}"
    excerpt_lines = selected.ticket_markdown.strip().splitlines()
    excerpt_text = "\n".join(excerpt_lines[:20]).strip()
    if len(excerpt_text) > 1200:
        excerpt_text = excerpt_text[:1200] + "..."

    body_lines: list[str] = []
    body_lines.append(f"Fingerprint: `{selected.fingerprint}`")
    if selected.ticket_id:
        body_lines.append(f"Source ticket: `{selected.ticket_id}`")
    body_lines.append("")
    body_lines.append("## Ticket excerpt")
    body_lines.append("")
    body_lines.append("```")
    body_lines.append(excerpt_text)
    body_lines.append("```")
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


def _cmd_run(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    selected: SelectedTicket
    if args.ticket_path is not None:
        selected = _select_ticket_from_path(args.ticket_path)
    else:
        selected = _select_ticket_from_export(
            tickets_export_path=args.tickets_export,
            fingerprint=args.fingerprint,
            ticket_id=args.ticket_id,
        )

    repo_input: str | None = None
    if isinstance(args.repo, str) and args.repo.strip():
        repo_input = args.repo.strip()
    elif selected.owner_root is not None:
        repo_input = str(selected.owner_root)
    elif selected.idea_path is not None:
        inferred = _infer_git_root(selected.idea_path.parent)
        repo_input = str(inferred) if inferred is not None else str(selected.idea_path.parent)
    else:
        raise SystemExit("Unable to infer target repo. Provide --repo.")

    if args.push or args.pr:
        if not args.commit:
            raise SystemExit("--push/--pr requires --commit")

    keep_workspace = bool(args.keep_workspace) or bool(args.commit) or bool(args.push) or bool(args.pr)

    ticket_blob = _compose_ticket_blob(selected)
    request = RunRequest(
        repo=repo_input,
        ref=args.ref,
        agent=str(args.agent),
        policy=str(args.policy),
        persona_id=args.persona_id,
        mission_id=args.mission_id,
        seed=int(args.seed),
        model=args.model,
        agent_config_overrides=tuple(args.agent_config_override or []),
        agent_append_system_prompt=ticket_blob,
        keep_workspace=keep_workspace,
        exec_backend=str(args.exec_backend),
        exec_keep_container=bool(args.exec_keep_container),
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
            },
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

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
            "ticket_id": selected.ticket_id,
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

    branch = args.branch or _default_branch_name(selected)
    commit_message = (
        args.commit_message
        or f"{selected.ticket_id or selected.fingerprint}: {selected.title or 'Implement backlog ticket'}"
    )

    git_ref: dict[str, Any] | None = None
    push_ref: dict[str, Any] | None = None
    pr_ref: dict[str, Any] | None = None

    if args.commit:
        git_ref = finalize_commit(run_dir=run_dir, branch=branch, commit_message=commit_message)

    if args.push:
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

    if args.push or args.pr:
        title, body = _write_pr_manifest(run_dir=run_dir, selected=selected, branch=branch)
        pr_ref = {
            "schema_version": 1,
            "requested": bool(args.pr),
            "created": False,
            "url": None,
            "title": title,
            "body": body,
            "error": None,
        }
        if args.pr:
            if shutil.which("gh") is None:
                pr_ref["error"] = "gh not found on PATH"
            else:
                workspace_ref = _read_json(run_dir / "workspace_ref.json")
                workspace_dir: Path | None = None
                if isinstance(workspace_ref, dict):
                    ws = workspace_ref.get("workspace_dir")
                    if isinstance(ws, str) and ws.strip():
                        workspace_dir = Path(ws)
                if workspace_dir is None:
                    pr_ref["error"] = "Missing workspace_ref.json; cannot locate workspace"
                else:
                    proc = subprocess.run(
                        ["gh", "pr", "create", "--title", title, "--body", body],
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

    if args.move_on_commit and selected.owner_root is not None and selected.idea_path is not None and args.commit:
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
            "ticket_id": selected.ticket_id,
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

    exit_code = int(result.exit_code or 0)

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
                "ticket_id": e.ticket_id,
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
        "ticket_id": entry.ticket_id,
        "paths": [str(p) for p in entry.paths],
        "buckets": entry.buckets,
        "status": entry.status,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


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
    run_p.add_argument("--fingerprint", help="Ticket fingerprint selector (requires --tickets-export).")
    run_p.add_argument("--ticket-id", help="Ticket id selector (requires --tickets-export).")

    run_p.add_argument("--repo", help="Override target repo input (path or git URL).")
    run_p.add_argument("--ref", help="Optional git ref to checkout in the acquired workspace.")

    run_p.add_argument("--agent", choices=["claude", "codex", "gemini"], default="codex")
    run_p.add_argument("--model", help="Optional model override.")
    run_p.add_argument("--policy", default="write")
    run_p.add_argument("--persona-id", dest="persona_id")
    run_p.add_argument("--mission-id", dest="mission_id", default="implement_backlog_ticket_v1")
    run_p.add_argument("--seed", type=int, default=0)
    run_p.add_argument(
        "--agent-config-override",
        action="append",
        default=[],
        help="Repeatable agent config override strings.",
    )
    run_p.add_argument("--keep-workspace", action="store_true", help="Keep workspace directory after run.")

    run_p.add_argument("--exec-backend", choices=["local", "docker"], default="local")
    run_auth_group = run_p.add_mutually_exclusive_group()
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
    run_p.add_argument("--exec-use-target-sandbox-cli-install", action="store_true", default=False)
    run_p.add_argument("--exec-keep-container", action="store_true")

    run_p.add_argument("--dry-run", action="store_true")

    run_p.add_argument("--commit", action="store_true", help="Create branch + commit changes in kept workspace.")
    run_p.add_argument("--branch", help="Branch name override.")
    run_p.add_argument("--commit-message", dest="commit_message", help="Commit message override.")

    run_p.add_argument("--push", action="store_true", help="Push branch to remote.")
    run_p.add_argument("--remote-name", default="origin")
    run_p.add_argument("--remote-url")
    run_p.add_argument("--force-push", dest="force_push", action="store_true")
    run_p.add_argument("--pr", action="store_true", help="Best-effort PR creation via gh.")

    run_p.add_argument(
        "--move-on-start",
        action="store_true",
        help="Move ticket file to 3 - in_progress if possible.",
    )
    run_p.add_argument(
        "--move-on-commit",
        action="store_true",
        help="Move ticket file to 4 - for_review after --commit.",
    )
    run_p.add_argument(
        "--ledger",
        nargs="?",
        const=Path("configs/backlog_implement_actions.yaml"),
        type=Path,
        help=(
            "Optional attempt ledger YAML. If provided without a value, defaults to "
            "<repo_root>/configs/backlog_implement_actions.yaml."
        ),
    )

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
            if bool(args.fingerprint) == bool(args.ticket_id):
                raise SystemExit(
                    "Provide exactly one of --fingerprint or --ticket-id with --tickets-export."
                )
        raise SystemExit(args.func(args))

    raise SystemExit(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
