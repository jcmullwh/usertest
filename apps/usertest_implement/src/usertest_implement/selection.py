# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from backlog_repo.plan_scope import parse_plan_target_contract_markdown
from backlog_repo.ticket_provenance import (
    canonical_plan_sha256,
    canonical_ticket_body,
    canonical_ticket_body_sha256,
    is_generated_backlog_ticket,
    parse_verification_contract_markdown,
)

from usertest_implement.shared import *
from usertest_implement.ticket_prompt import project_ticket_prompt_context


def _fingerprint_from_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _fingerprint_from_ticket_filename(path: Path) -> str | None:
    match = re.fullmatch(
        r"[0-9]{8}_(?:(?:BLG-[0-9]{3}|TKT-[0-9a-f]{12})_)?"
        r"(?P<fingerprint>[0-9a-f]{16})_.+\.md",
        path.name,
    )
    return match.group("fingerprint") if match is not None else None


def _case_plan_fingerprint(*, case_id: str, plan_revision_id: str) -> str:
    payload = {
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _strict_ticket_text(path: Path) -> str:
    try:
        # Preserve raw newline bytes until canonical provenance normalization.
        # Universal-newline reads would turn CRCRLF into two logical newlines and
        # make the historical Windows outcome-writer artifact indistinguishable
        # from real ticket-body whitespace drift.
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Ticket Markdown is not valid UTF-8: {path}") from exc
    if "\x00" in text:
        raise ValueError(f"Ticket Markdown contains NUL bytes: {path}")
    return strip_legacy_source_ticket_lines(text)


def _selected_ticket_provenance(
    selected: SelectedTicket,
    *,
    require_local_plan: bool,
) -> dict[str, Any]:
    """Revalidate one selected plan and return its canonical provenance."""

    metadata = parse_ticket_markdown_metadata(selected.ticket_markdown)
    metadata_fingerprint = metadata.get("fingerprint")
    if metadata_fingerprint != selected.fingerprint:
        raise ValueError(
            "Selected ticket fingerprint does not match Markdown metadata: "
            f"selected={selected.fingerprint!r} metadata={metadata_fingerprint!r}"
        )

    case_id = metadata.get("case_id")
    plan_revision_id = metadata.get("plan_revision_id")
    if bool(case_id) != bool(plan_revision_id):
        raise ValueError("Ticket must carry Case ID and Plan revision ID together")
    legacy_identity = not bool(case_id)
    if case_id is not None and plan_revision_id is not None:
        expected_fingerprint = _case_plan_fingerprint(
            case_id=case_id,
            plan_revision_id=plan_revision_id,
        )
        if expected_fingerprint != selected.fingerprint:
            raise ValueError(
                "Ticket fingerprint is not bound to its case and plan revision: "
                f"expected={expected_fingerprint!r} observed={selected.fingerprint!r}"
            )

    plan_markdown = selected.ticket_markdown
    plan_path = selected.idea_path.resolve() if selected.idea_path is not None else None
    if plan_path is not None and plan_path.is_file():
        current_markdown = _strict_ticket_text(plan_path)
        if canonical_plan_sha256(current_markdown) != canonical_plan_sha256(plan_markdown):
            raise ValueError("Selected ticket Markdown is stale relative to the local plan file")
        plan_markdown = current_markdown
    elif require_local_plan:
        raise ValueError("Selected ticket does not reference a readable local plan file")

    if require_local_plan and selected.owner_root is not None and plan_path is not None:
        plans_root = (selected.owner_root.resolve() / ".agents" / "plans").resolve()
        if not plan_path.is_relative_to(plans_root):
            raise ValueError(f"Selected ticket is outside the owner plan root: {plan_path}")

    body_sha256 = canonical_ticket_body_sha256(plan_markdown)
    plan_sha256 = canonical_plan_sha256(plan_markdown)
    contract = parse_verification_contract_markdown(plan_markdown)
    contract_sha256 = (
        str(contract["contract_sha256"]) if contract is not None else None
    )
    target_contract = parse_plan_target_contract_markdown(plan_markdown)
    target_contract_sha256 = (
        str(target_contract["contract_sha256"])
        if target_contract is not None
        else None
    )
    generated_ticket = is_generated_backlog_ticket(plan_markdown)
    if not legacy_identity and generated_ticket:
        if target_contract is None:
            raise ValueError("Case-aware ticket is missing its plan target contract")
        if target_contract.get("case_id") != case_id:
            raise ValueError("Plan target contract case_id does not match the ticket")

    declared_values = (
        ("case_id", selected.case_id, case_id),
        ("plan_revision_id", selected.plan_revision_id, plan_revision_id),
        ("ticket_body_sha256", selected.ticket_body_sha256, body_sha256),
        ("local_plan_sha256", selected.local_plan_sha256, plan_sha256),
        (
            "verification_contract_sha256",
            selected.verification_contract_sha256,
            contract_sha256,
        ),
        (
            "target_contract_sha256",
            selected.target_contract_sha256,
            target_contract_sha256,
        ),
    )
    for label, declared, observed in declared_values:
        if declared is not None and declared != observed:
            raise ValueError(
                f"Selected ticket {label} is stale or forged: "
                f"declared={declared!r} observed={observed!r}"
            )

    return {
        "schema_version": 1,
        "fingerprint": selected.fingerprint,
        "case_id": case_id or f"legacy-case:{selected.fingerprint}",
        "plan_revision_id": plan_revision_id or f"legacy-plan:{selected.fingerprint}",
        "legacy_identity": legacy_identity,
        "ticket_body_sha256": body_sha256,
        "local_plan_sha256": plan_sha256,
        "local_plan_path": str(plan_path) if plan_path is not None else None,
        "local_plan_filename": plan_path.name if plan_path is not None else None,
        "verification_contract": contract,
        "verification_contract_sha256": contract_sha256,
        "target_contract": target_contract,
        "target_contract_sha256": target_contract_sha256,
        "generated_ticket": generated_ticket,
    }


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
    if source_ticket.get("fingerprint") != export_fp.strip():
        raise ValueError(
            "Export fingerprint does not match source_ticket fingerprint"
        )
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
    if idea_path is not None and idea_path.is_file():
        local_selected = _select_ticket_from_path(idea_path)
        if local_selected.fingerprint != export_fp.strip():
            raise ValueError("Export fingerprint does not match the referenced local plan")
        export_body_metadata = parse_ticket_markdown_metadata(body)
        if export_body_metadata.get("fingerprint") != export_fp.strip():
            raise ValueError("Export fingerprint does not match body_markdown metadata")
        if canonical_ticket_body(local_selected.ticket_markdown) != canonical_ticket_body(body):
            raise ValueError(
                "Export body_markdown does not match the referenced local plan contents"
            )

        local_provenance = _selected_ticket_provenance(
            local_selected,
            require_local_plan=True,
        )
        if (
            owner_root is not None
            and local_selected.owner_root is not None
            and owner_root.resolve() != local_selected.owner_root.resolve()
        ):
            raise ValueError("Export owner root does not match the referenced local plan")
        outer_case_id = export.get("case_id")
        outer_plan_revision_id = export.get("plan_revision_id")
        source_case_id = source_ticket.get("case_id")
        source_plan_revision_id = source_ticket.get("plan_revision_id")
        if local_provenance["legacy_identity"] is False:
            if local_provenance["verification_contract"] is None:
                raise ValueError(
                    "Case-aware implementation plan is missing an explicit verification contract"
                )
            identity_values = {
                "export.case_id": outer_case_id,
                "export.plan_revision_id": outer_plan_revision_id,
                "source_ticket.case_id": source_case_id,
                "source_ticket.plan_revision_id": source_plan_revision_id,
            }
            expected_identity_values = {
                "export.case_id": local_provenance["case_id"],
                "export.plan_revision_id": local_provenance["plan_revision_id"],
                "source_ticket.case_id": local_provenance["case_id"],
                "source_ticket.plan_revision_id": local_provenance["plan_revision_id"],
            }
            for label, observed in identity_values.items():
                if observed != expected_identity_values[label]:
                    raise ValueError(
                        f"Export case/plan provenance mismatch: {label}={observed!r} "
                        f"expected={expected_identity_values[label]!r}"
                    )

        expected_hashes = {
            "body_sha256": local_provenance["ticket_body_sha256"],
            "local_plan_sha256": local_provenance["local_plan_sha256"],
            "verification_contract_sha256": local_provenance[
                "verification_contract_sha256"
            ],
            "target_contract_sha256": local_provenance[
                "target_contract_sha256"
            ],
        }
        for field, expected in expected_hashes.items():
            observed = export.get(field)
            if local_provenance["legacy_identity"] is False and observed != expected:
                raise ValueError(
                    f"Export {field} does not match the referenced local plan: "
                    f"expected={expected!r} observed={observed!r}"
                )
            if observed is not None and observed != expected:
                raise ValueError(
                    f"Export {field} is stale or forged: "
                    f"expected={expected!r} observed={observed!r}"
                )

        return SelectedTicket(
            fingerprint=local_selected.fingerprint,
            title=title_s,
            export_kind=export_kind_s,
            stage=stage_s,
            owner_root=local_selected.owner_root or owner_root,
            idea_path=local_selected.idea_path,
            ticket_markdown=local_selected.ticket_markdown,
            tickets_export_path=tickets_export_path,
            export_index=export_index,
            case_id=(
                None
                if local_provenance["legacy_identity"]
                else str(local_provenance["case_id"])
            ),
            plan_revision_id=(
                None
                if local_provenance["legacy_identity"]
                else str(local_provenance["plan_revision_id"])
            ),
            ticket_body_sha256=str(local_provenance["ticket_body_sha256"]),
            local_plan_sha256=str(local_provenance["local_plan_sha256"]),
            verification_contract_sha256=local_provenance[
                "verification_contract_sha256"
            ],
            target_contract_sha256=local_provenance["target_contract_sha256"],
        )

    body_contract = parse_verification_contract_markdown(body)
    body_target_contract = parse_plan_target_contract_markdown(body)
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
        case_id=export.get("case_id") if isinstance(export.get("case_id"), str) else None,
        plan_revision_id=(
            export.get("plan_revision_id")
            if isinstance(export.get("plan_revision_id"), str)
            else None
        ),
        ticket_body_sha256=canonical_ticket_body_sha256(body),
        local_plan_sha256=None,
        verification_contract_sha256=(
            str(body_contract["contract_sha256"])
            if body_contract is not None
            else None
        ),
        target_contract_sha256=(
            str(body_target_contract["contract_sha256"])
            if body_target_contract is not None
            else None
        ),
    )


def _select_ticket_from_path(ticket_path: Path) -> SelectedTicket:
    text = _strict_ticket_text(ticket_path)
    meta = parse_ticket_markdown_metadata(text)
    metadata_fingerprint = meta.get("fingerprint")
    filename_fingerprint = _fingerprint_from_ticket_filename(ticket_path)
    if (
        metadata_fingerprint is not None
        and filename_fingerprint is not None
        and metadata_fingerprint != filename_fingerprint
    ):
        raise ValueError(
            "Ticket fingerprint mismatch between filename and Markdown metadata: "
            f"filename={filename_fingerprint!r} metadata={metadata_fingerprint!r}"
        )
    fingerprint = metadata_fingerprint or filename_fingerprint or _fingerprint_from_text(text)
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

    contract = parse_verification_contract_markdown(text)
    target_contract = parse_plan_target_contract_markdown(text)
    selected = SelectedTicket(
        fingerprint=fingerprint,
        title=title,
        export_kind=export_kind,
        stage=stage,
        owner_root=owner_root,
        idea_path=ticket_path.resolve(),
        ticket_markdown=text,
        tickets_export_path=None,
        export_index=None,
        case_id=meta.get("case_id"),
        plan_revision_id=meta.get("plan_revision_id"),
        ticket_body_sha256=canonical_ticket_body_sha256(text),
        local_plan_sha256=canonical_plan_sha256(text),
        verification_contract_sha256=(
            str(contract["contract_sha256"]) if contract is not None else None
        ),
        target_contract_sha256=(
            str(target_contract["contract_sha256"])
            if target_contract is not None
            else None
        ),
    )
    _selected_ticket_provenance(selected, require_local_plan=True)
    return selected


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
    selected = _select_ticket_from_path(path)
    if selected.fingerprint != fingerprint:
        raise ValueError(
            "Ticket selector fingerprint mismatch: "
            f"requested={fingerprint!r} selected={selected.fingerprint!r} path={path}"
        )
    return selected


def _select_review_ticket(
    *,
    owner_root: Path,
    ticket_path: Path | None,
    fingerprint: str | None,
) -> SelectedTicket:
    if ticket_path is not None:
        selected = _select_ticket_from_path(ticket_path)
        if selected.owner_root is None or selected.owner_root.resolve() != owner_root.resolve():
            raise ValueError(f"Selected ticket is outside owner root: {ticket_path}")
        if isinstance(fingerprint, str) and fingerprint.strip() and selected.fingerprint != fingerprint.strip():
            raise ValueError(
                "Ticket path and selector fingerprint disagree: "
                f"selector={fingerprint.strip()!r} path={selected.fingerprint!r}"
            )
        return selected
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
    lines.append(project_ticket_prompt_context(selected).rstrip())
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
