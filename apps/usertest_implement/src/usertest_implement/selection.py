# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_implement.shared import *


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
    source_ticket_raw = export.get("source_ticket")
    source_ticket = source_ticket_raw if isinstance(source_ticket_raw, dict) else {}
    stage_raw = source_ticket.get("stage")
    stage_s = stage_raw.strip() if isinstance(stage_raw, str) and stage_raw.strip() else None

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
        stage=stage_s,
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
    stage = meta.get("stage")

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
        stage=stage,
        owner_root=owner_root,
        idea_path=ticket_path,
        ticket_markdown=text,
        tickets_export_path=None,
        export_index=None,
    )


def _select_ticket_from_owner_root(
    *,
    owner_root: Path,
    fingerprint: str,
) -> SelectedTicket:
    index = build_ticket_index(owner_root=owner_root)
    entry = index.get(fingerprint)
    if entry is None or not entry.paths:
        raise ValueError(f"Unknown fingerprint under {owner_root}: {fingerprint}")
    path = sorted(entry.paths, key=lambda item: str(item))[0]
    return _select_ticket_from_path(path)


def _select_review_ticket(
    *,
    owner_root: Path,
    ticket_path: Path | None,
    fingerprint: str | None,
) -> SelectedTicket:
    if ticket_path is not None:
        return _select_ticket_from_path(ticket_path)
    if isinstance(fingerprint, str) and fingerprint.strip():
        return _select_ticket_from_owner_root(owner_root=owner_root, fingerprint=fingerprint.strip())
    raise SystemExit("Provide either --ticket-path or --fingerprint.")


def _compose_ticket_blob(selected: SelectedTicket) -> str:
    lines: list[str] = []
    lines.append("# Ticket context")
    lines.append(f"- fingerprint: {selected.fingerprint}")
    if selected.title is not None:
        lines.append(f"- title: {selected.title}")
    if selected.export_kind is not None:
        lines.append(f"- export_kind: {selected.export_kind}")
    if selected.stage is not None:
        lines.append(f"- stage: {selected.stage}")
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


def _resolve_remote_url_for_push(
    *,
    remote_name: str,
    remote_url: str | None,
    candidate_repo_dirs: list[Path],
) -> str | None:
    if isinstance(remote_url, str) and remote_url.strip():
        return remote_url.strip()
    for candidate in candidate_repo_dirs:
        url = _git_remote_url(repo_dir=candidate, remote_name=remote_name)
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _remote_branch_exists(*, remote_url: str, branch: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--heads", remote_url, branch],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    return bool((proc.stdout or "").strip())


def _resolve_default_branch_name(
    *,
    selected: SelectedTicket,
    remote_name: str,
    remote_url: str | None,
    candidate_repo_dirs: list[Path],
    wants_remote_handoff: bool,
) -> str:
    base_branch = _default_branch_name(selected)
    if not wants_remote_handoff:
        return base_branch
    resolved_remote_url = _resolve_remote_url_for_push(
        remote_name=remote_name,
        remote_url=remote_url,
        candidate_repo_dirs=candidate_repo_dirs,
    )
    if resolved_remote_url is None:
        return base_branch
    if not _remote_branch_exists(remote_url=resolved_remote_url, branch=base_branch):
        return base_branch
    suffix = 1
    while True:
        candidate = f"{base_branch}-rerun-{suffix}"
        if not _remote_branch_exists(remote_url=resolved_remote_url, branch=candidate):
            return candidate
        suffix += 1


def _should_move_ticket_to_review(
    *,
    commit_performed: bool,
    push_requested: bool,
    pr_requested: bool,
    push_ref: dict[str, Any] | None,
    pr_ref: dict[str, Any] | None,
) -> bool:
    if not commit_performed:
        return False
    if pr_requested:
        return bool(pr_ref is not None and pr_ref.get("created") is True)
    if push_requested:
        return bool(push_ref is not None and push_ref.get("pushed") is True)
    return True


def _require_stage6_implementation_ticket(selected: SelectedTicket) -> None:
    export_kind = (
        selected.export_kind.strip().lower()
        if isinstance(selected.export_kind, str) and selected.export_kind.strip()
        else None
    )
    stage = (
        selected.stage.strip().lower()
        if isinstance(selected.stage, str) and selected.stage.strip()
        else None
    )
    if export_kind != "implementation":
        raise SystemExit(
            "Ticket is not implementation-ready for `usertest-implement` "
            f"(fingerprint={selected.fingerprint}, export_kind={selected.export_kind!r}). "
            "Select a stage-6 implementation ticket (`export_kind=implementation`, `stage=ready_for_ticket`)."
        )
    if stage != "ready_for_ticket":
        raise SystemExit(
            "Ticket is not stage-6 ready for implementation "
            f"(fingerprint={selected.fingerprint}, stage={selected.stage!r}). "
            "Select a ticket with `stage=ready_for_ticket`."
        )


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




__all__ = [name for name in globals() if not name.startswith("__")]
