# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.shared import *


def _default_triage_output_path(input_json: Path, *, suffix: str) -> Path:
    """Return deterministic default output path for PR triage artifacts.

    Parameters
    ----------
    input_json:
        Source PR-list JSON path.
    suffix:
        Output suffix (for example ``".triage_prs.json"``).

    Returns
    -------
    Path
        Output path in the same directory as the input payload.
    """

    return input_json.with_name(f"{input_json.stem}{suffix}")


def _cmd_triage_backlog(args: argparse.Namespace) -> int:
    """Execute the ``triage-backlog`` command."""

    input_json = args.input_json.resolve()
    if not input_json.exists():
        raise FileNotFoundError(f"Input file not found: {input_json}")

    issues, input_metadata = load_issue_items(input_json)
    report = triage_issues(
        issues,
        group_key=args.group_key,
        dedupe_overall_threshold=float(args.dedupe_overall_threshold),
        theme_overall_threshold=float(args.theme_overall_threshold),
        theme_k=int(args.theme_k),
        theme_representative_threshold=float(args.theme_representative_threshold),
    )
    report["input_json"] = str(input_json)
    if input_metadata:
        report["input_metadata"] = input_metadata

    out_json = (
        args.out_json.resolve()
        if args.out_json is not None
        else _default_triage_output_path(input_json, suffix=".triage_backlog.json")
    )
    out_md = (
        args.out_md.resolve()
        if args.out_md is not None
        else _default_triage_output_path(input_json, suffix=".triage_backlog.md")
    )
    out_xlsx = args.out_xlsx.resolve() if args.out_xlsx is not None else None

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(
        render_backlog_triage_markdown(report, title="Backlog Triage Report"),
        encoding="utf-8",
    )

    if out_xlsx is not None:
        write_triage_xlsx(report, out_xlsx)

    print(str(out_json))
    print(str(out_md))
    if out_xlsx is not None:
        print(str(out_xlsx))
    print(json.dumps(report.get("totals", {}), indent=2, ensure_ascii=False))
    return 0


def _coerce_pr_items(raw_payload: Any) -> list[dict[str, Any]]:
    """Normalize PR input payload into canonical in-memory records.

    Parameters
    ----------
    raw_payload:
        JSON-decoded payload containing either a list of PR objects or an object
        with a ``pullRequests`` list.

    Returns
    -------
    list[dict[str, Any]]
        Normalized PR records with keys ``number``, ``title``, ``body``, and ``files``.

    Raises
    ------
    ValueError
        Raised when payload is not list-like in the expected shape.
    """

    payload = raw_payload
    if isinstance(payload, dict) and isinstance(payload.get("pullRequests"), list):
        payload = payload.get("pullRequests")
    if not isinstance(payload, list):
        raise ValueError("Expected a JSON list or object containing a pullRequests list.")

    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        number_raw = item.get("number")
        number: int
        used_number_fallback = False
        if isinstance(number_raw, bool):
            number = idx
            used_number_fallback = True
        elif isinstance(number_raw, int):
            number = number_raw
        elif isinstance(number_raw, float):
            number = int(number_raw)
        elif isinstance(number_raw, str) and number_raw.strip().isdigit():
            number = int(number_raw.strip())
        else:
            number = idx
            used_number_fallback = True

        if used_number_fallback:
            warnings.warn(
                (
                    "PR triage input item is missing a valid `number`; "
                    f"falling back to positional index {idx}."
                ),
                RuntimeWarning,
                stacklevel=2,
            )

        title = _coerce_string(item.get("title"))
        if title is None:
            title = f"PR {number}"
            warnings.warn(
                (
                    "PR triage input item is missing a valid `title`; "
                    f"falling back to {title!r}."
                ),
                RuntimeWarning,
                stacklevel=2,
            )
        body = _coerce_string(item.get("body")) or ""
        files_raw = item.get("files")
        files = (
            [entry.strip() for entry in files_raw if isinstance(entry, str) and entry.strip()]
            if isinstance(files_raw, list)
            else []
        )

        normalized.append(
            {
                "number": number,
                "title": title,
                "body": body,
                "files": files,
            }
        )
    return normalized


def _render_triage_markdown(doc: dict[str, Any]) -> str:
    """Render PR triage JSON payload into human-readable markdown.

    Parameters
    ----------
    doc:
        Triage document payload emitted by ``_cmd_triage_prs``.

    Returns
    -------
    str
        Markdown report content.
    """

    lines: list[str] = []
    lines.append("# PR Triage Report")
    lines.append("")
    lines.append(f"- Generated: `{doc.get('generated_at', '')}`")
    lines.append(f"- Input: `{doc.get('input_json', '')}`")
    lines.append(f"- PRs: **{int(doc.get('pull_requests_total', 0))}**")
    lines.append(f"- Clusters: **{int(doc.get('clusters_total', 0))}**")
    lines.append("")

    clusters_raw = doc.get("clusters")
    clusters = [item for item in clusters_raw if isinstance(item, dict)] if isinstance(clusters_raw, list) else []
    if not clusters:
        lines.append("No clusters were produced.")
        lines.append("")
        return "\n".join(lines)

    for cluster in clusters:
        cluster_id = int(cluster.get("cluster_id", 0))
        size = int(cluster.get("size", 0))
        score = float(cluster.get("score", 0.0))
        representative = _coerce_string(cluster.get("representative_title")) or "Unknown"
        lines.append(f"## Cluster {cluster_id}")
        lines.append(f"- Size: **{size}**")
        lines.append(f"- Score: **{score:.3f}**")
        lines.append(f"- Representative: {representative}")

        anchors_raw = cluster.get("common_path_anchors")
        anchors = [item for item in anchors_raw if isinstance(item, str)] if isinstance(anchors_raw, list) else []
        if anchors:
            lines.append(f"- Common anchors: {', '.join(f'`{anchor}`' for anchor in anchors)}")

        prs_raw = cluster.get("pull_requests")
        prs = [item for item in prs_raw if isinstance(item, dict)] if isinstance(prs_raw, list) else []
        for pr in prs:
            number = int(pr.get("number", 0))
            title = _coerce_string(pr.get("title")) or "Untitled"
            lines.append(f"- PR #{number}: {title}")
        lines.append("")

    return "\n".join(lines)


def _cmd_triage_prs(args: argparse.Namespace) -> int:
    """Execute the ``triage-prs`` command.

    Parameters
    ----------
    args:
        Parsed argparse namespace for triage command options.

    Returns
    -------
    int
        Process exit code (`0` on success).
    """

    input_json = args.input_json.resolve()
    if not input_json.exists():
        raise FileNotFoundError(f"Input file not found: {input_json}")

    payload = json.loads(input_json.read_text(encoding="utf-8"))
    prs = _coerce_pr_items(payload)

    title_threshold = float(args.title_threshold)
    clusters_idx = cluster_items(
        prs,
        get_title=lambda pr: _coerce_string(pr.get("title")) or "",
        get_text_chunks=lambda pr: [
            _coerce_string(pr.get("title")) or "",
            _coerce_string(pr.get("body")) or "",
            *[item for item in pr.get("files", []) if isinstance(item, str)],
        ],
        title_overlap_threshold=title_threshold,
    )

    clusters: list[dict[str, Any]] = []
    for cluster_id, indexes in enumerate(clusters_idx, start=1):
        members = [prs[idx] for idx in indexes]
        members_sorted = sorted(
            members,
            key=lambda pr: int(pr.get("number", 0)),
        )
        per_pr_anchors = [
            extract_path_anchors_from_chunks(
                [
                    _coerce_string(pr.get("title")) or "",
                    _coerce_string(pr.get("body")) or "",
                    *[item for item in pr.get("files", []) if isinstance(item, str)],
                ]
            )
            for pr in members_sorted
        ]
        common_anchors = (
            sorted(set.intersection(*per_pr_anchors)) if per_pr_anchors else []
        )
        unique_anchors = sorted(set().union(*per_pr_anchors)) if per_pr_anchors else []
        score = float(len(members_sorted)) + math.log1p(float(len(unique_anchors)))

        clusters.append(
            {
                "cluster_id": cluster_id,
                "size": len(members_sorted),
                "score": score,
                "pr_numbers": [int(pr.get("number", 0)) for pr in members_sorted],
                "representative_title": _coerce_string(members_sorted[0].get("title")) or "",
                "common_path_anchors": common_anchors[:12],
                "pull_requests": [
                    {
                        "number": int(pr.get("number", 0)),
                        "title": _coerce_string(pr.get("title")) or "",
                    }
                    for pr in members_sorted
                ],
            }
        )

    clusters.sort(
        key=lambda cluster: (
            -int(cluster.get("size", 0)),
            -float(cluster.get("score", 0.0)),
            min(
                [item for item in cluster.get("pr_numbers", []) if isinstance(item, int)] or [0]
            ),
        )
    )
    for idx, cluster in enumerate(clusters, start=1):
        cluster["cluster_id"] = idx

    out_json = (
        args.out_json.resolve()
        if args.out_json is not None
        else _default_triage_output_path(input_json, suffix=".triage_prs.json")
    )
    out_md = (
        args.out_md.resolve()
        if args.out_md is not None
        else _default_triage_output_path(input_json, suffix=".triage_prs.md")
    )

    doc = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input_json": str(input_json),
        "title_threshold": title_threshold,
        "pull_requests_total": len(prs),
        "clusters_total": len(clusters),
        "clusters": clusters,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(_render_triage_markdown(doc), encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    return 0


def _cmd_triage_atoms(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)

    atoms_jsonl = args.atoms_jsonl
    if not atoms_jsonl.is_absolute() and not atoms_jsonl.exists():
        atoms_jsonl = repo_root / atoms_jsonl
    atoms_jsonl = atoms_jsonl.resolve()
    if not atoms_jsonl.exists():
        raise FileNotFoundError(f"Atoms JSONL not found: {atoms_jsonl}")

    backlog_json = _resolve_optional_path(repo_root, args.backlog_json)
    backlog_json = infer_backlog_json(atoms_jsonl, explicit=backlog_json)

    tickets: list[dict[str, Any]] | None = None
    backlog_doc: dict[str, Any] | None = None
    if backlog_json is not None:
        backlog_json = backlog_json.resolve()
        backlog_doc = load_backlog_json(backlog_json)
        tickets_raw = backlog_doc.get("tickets")
        tickets = (
            [item for item in tickets_raw if isinstance(item, dict)]
            if isinstance(tickets_raw, list)
            else []
        )

    plans_root = args.plans_root.resolve() if args.plans_root is not None else repo_root
    plan_status_by_fingerprint = build_plan_status_index(owner_root=plans_root)

    implementation_root = _resolve_optional_path(repo_root, args.implementation_root)
    if implementation_root is None and isinstance(backlog_doc, dict):
        input_meta = backlog_doc.get("input")
        if isinstance(input_meta, dict):
            runs_dir_raw = input_meta.get("runs_dir")
            target_raw = input_meta.get("target")
            if isinstance(runs_dir_raw, str) and isinstance(target_raw, str):
                runs_dir = Path(runs_dir_raw)
                if not runs_dir.is_absolute() and not runs_dir.exists():
                    runs_dir = repo_root / runs_dir
                candidate = (runs_dir / target_raw).resolve()
                if candidate.exists():
                    implementation_root = candidate

    implementation_runs_by_fingerprint: dict[str, Any] = {}
    if implementation_root is not None:
        implementation_runs_by_fingerprint = build_implementation_index(
            repo_root=repo_root,
            implementation_root=implementation_root,
        )

    embedder, embedder_meta = resolve_embedder(args.embedder)
    report = triage_atoms_report(
        load_atoms_jsonl(atoms_jsonl),
        embedder=embedder,
        embedder_label=args.embedder,
        text_normalization=args.text_normalization,
        k=int(args.k),
        overall_similarity_threshold=float(args.overall_threshold),
        representative_similarity_threshold=float(args.representative_threshold),
        min_cluster_size=int(args.min_cluster_size),
        exclude_sources=list(args.exclude_source or []),
        tickets=tickets,
        plan_status_by_fingerprint=plan_status_by_fingerprint,
        implementation_runs_by_fingerprint=implementation_runs_by_fingerprint,
    )
    report["input"] = {
        "atoms_jsonl": str(atoms_jsonl),
        "backlog_json": str(backlog_json) if backlog_json is not None else None,
        "plans_root": str(plans_root),
        "implementation_root": str(implementation_root) if implementation_root is not None else None,
        "exclude_sources": list(args.exclude_source or []),
        **embedder_meta,
    }

    out_json, out_md = write_triage_atoms(
        report,
        atoms_jsonl=atoms_jsonl,
        out_json=args.out_json,
        out_md=args.out_md,
    )

    print(str(out_json))
    print(str(out_md))
    return 0




__all__ = [name for name in globals() if not name.startswith("__")]
