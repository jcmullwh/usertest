# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.shared import *


def _render_problem_records_markdown(
    problem_records: list[dict[str, Any]],
    *,
    title: str = "Problem Records",
) -> str:
    """Render a list of problem records as a human-readable Markdown document.

    Parameters
    ----------
    problem_records:
        Stage-1 problem record dicts.
    title:
        Document title.

    Returns
    -------
    str
        Markdown text.
    """
    lines: list[str] = [f"# {title}\n"]
    if not problem_records:
        lines.append("_No problem records produced._\n")
        return "\n".join(lines)

    for rec in problem_records:
        pid = rec.get("problem_id") or "(no id)"
        rec_title = rec.get("title") or pid
        severity = rec.get("severity") or "unknown"
        confidence = rec.get("confidence")
        conf_str = f"{float(confidence):.2f}" if isinstance(confidence, (int, float)) else "?"
        status = rec.get("problem_status") or "identified"
        lines.append(f"## {rec_title}")
        lines.append(f"**ID**: `{pid}` | **Severity**: {severity} | "
                     f"**Confidence**: {conf_str} | **Status**: {status}\n")
        problem_text = rec.get("problem") or ""
        if problem_text:
            lines.append(f"**Problem**: {problem_text}\n")
        impact = rec.get("user_impact") or ""
        if impact:
            lines.append(f"**User impact**: {impact}\n")
        summary = rec.get("evidence_summary") or ""
        if summary:
            lines.append(f"**Evidence summary**: {summary}\n")
        eids = rec.get("evidence_atom_ids") or []
        if eids:
            lines.append(f"**Evidence atoms** ({len(eids)}): "
                         + ", ".join(f"`{e}`" for e in eids[:8])
                         + (" …" if len(eids) > 8 else "") + "\n")
        warn = rec.get("_parse_warning")
        if warn:
            lines.append(f"> ⚠ parse warning: {warn}\n")
        lines.append("")

    return "\n".join(lines)


def _synthesize_problem_records_from_atoms(
    atoms: list[dict[str, Any]],
    *,
    max_records: int,
) -> list[dict[str, Any]]:
    """Synthesize deterministic problem records from atoms (dry-run mode only).

    The six-stage pipeline uses LLMs for problem mining. In ``--dry-run`` mode the
    CLI must avoid network calls, but downstream stages (stage 2+) still require
    problem records in order to produce observable artifacts on offline fixtures.

    This function provides an explicit, inspectable, deterministic approximation:
    it groups atoms by ``source`` and emits one problem record per source.
    """

    def _severity_rank(atom: dict[str, Any]) -> int:
        score_hint = atom.get("severity_score_hint")
        if isinstance(score_hint, int):
            return max(0, min(3, score_hint))
        sev = _coerce_string(atom.get("severity_hint")) or "medium"
        return {"low": 0, "medium": 1, "high": 2, "blocker": 3}.get(sev, 1)

    def _severity_label(rank: int) -> str:
        return {0: "low", 1: "medium", 2: "high", 3: "blocker"}.get(rank, "medium")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for atom in atoms:
        source = _coerce_string(atom.get("source")) or "unknown"
        grouped.setdefault(source, []).append(atom)

    # Order groups deterministically: higher severity first, then more atoms, then source name.
    group_order: list[tuple[int, int, str]] = []
    for source, group_atoms in grouped.items():
        max_rank = 0
        for a in group_atoms:
            max_rank = max(max_rank, _severity_rank(a))
        group_order.append((max_rank, len(group_atoms), source))
    group_order.sort(key=lambda t: (-t[0], -t[1], t[2]))

    title_by_source: dict[str, str] = {
        "run_failure_event": "Run failures observed",
        "command_failure": "Command failures observed",
        "confusion_point": "User confusion observed",
        "suggested_change": "Suggested changes imply gaps",
        "report_validation_error": "Report validation errors observed",
    }
    impact_by_source: dict[str, str] = {
        "run_failure_event": "Runs fail to complete, blocking progress.",
        "command_failure": "Commands fail during execution, blocking tasks.",
        "confusion_point": "Users are confused about expected behavior or usage.",
        "suggested_change": "Users suggest changes, indicating missing guidance or friction.",
        "report_validation_error": "Report output is invalid, breaking automation and analysis.",
    }

    out: list[dict[str, Any]] = []
    for idx, (_max_rank, _count, source) in enumerate(group_order, start=1):
        if len(out) >= max_records:
            break
        group_atoms = grouped[source]
        group_atoms_sorted = sorted(
            group_atoms, key=lambda a: str(a.get("atom_id") or "")
        )
        evidence_atom_ids = [
            atom_id
            for atom_id in (str(a.get("atom_id") or "").strip() for a in group_atoms_sorted)
            if atom_id
        ]
        if not evidence_atom_ids:
            continue

        max_rank = 0
        run_ids: set[str] = set()
        agents: set[str] = set()
        for atom in group_atoms_sorted:
            max_rank = max(max_rank, _severity_rank(atom))
            run_id = _coerce_string(atom.get("run_id"))
            if run_id:
                run_ids.add(run_id)
            agent = _coerce_string(atom.get("agent"))
            if agent:
                agents.add(agent)

        severity = _severity_label(max_rank)
        distinct_runs = len(run_ids)
        distinct_agents = len(agents)

        # Confidence heuristic: more breadth and more evidence implies higher confidence.
        confidence = 0.35 + 0.12 * min(3, max(0, distinct_runs - 1)) + 0.06 * min(
            4, max(0, len(evidence_atom_ids) - 1)
        )
        if severity in {"high", "blocker"}:
            confidence += 0.10
        confidence = max(0.0, min(0.90, confidence))

        # Evidence summary: short excerpts from the first few atoms.
        excerpts: list[str] = []
        for atom in group_atoms_sorted[:3]:
            text = _coerce_string(atom.get("text")) or ""
            if text:
                excerpt = text if len(text) <= 140 else text[:140] + "..."
                excerpts.append(excerpt)
        evidence_summary = " | ".join(excerpts) if excerpts else f"{len(evidence_atom_ids)} atoms"

        slug = slugify(f"dryrun-{source}-{idx}")
        title = title_by_source.get(source, source.replace("_", " ").strip().title())
        user_impact = impact_by_source.get(source, "Users are affected by this issue.")

        out.append(
            {
                "problem_id": f"problem:{slug}",
                "title": title,
                "problem": f"Evidence atoms of type `{source}` indicate a recurring issue.",
                "user_impact": user_impact,
                "severity": severity,
                "confidence": round(confidence, 4),
                "evidence_atom_ids": evidence_atom_ids,
                "evidence_summary": evidence_summary,
                "problem_status": "identified",
                "_dry_run_synthesized": True,
                "_dry_run_meta": {
                    "source": source,
                    "distinct_runs": distinct_runs,
                    "distinct_agents": distinct_agents,
                    "evidence_atoms_cited": len(evidence_atom_ids),
                },
            }
        )

    return out


def _atoms_for_problem_mining_prompt(
    atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a compact, prompt-friendly projection of backlog atoms.

    Stage 1 problem mining feeds evidence atoms to an LLM. The raw atom payloads include
    many fields (paths, metrics hints, etc.) that bloat prompts and can exceed provider
    limits. Stage 1 only needs a stable identifier and enough context to describe the
    observed problem from evidence. This helper keeps full evidence text (no truncation)
    while dropping unrelated metadata to reduce token waste.

    Parameters
    ----------
    atoms:
        Raw evidence atoms extracted from run history.

    Returns
    -------
    list[dict[str, Any]]
        List of compact atom dicts suitable for embedding in stage-1 prompts.
    """

    compact: list[dict[str, Any]] = []
    for atom in atoms:
        atom_id = _coerce_string(atom.get("atom_id"))
        if atom_id is None:
            continue

        text = _coerce_string(atom.get("text")) or ""

        linked_raw = atom.get("linked_atom_ids")
        linked = (
            [x for x in linked_raw if isinstance(x, str) and x.strip()][:3]
            if isinstance(linked_raw, list)
            else []
        )

        compact.append(
            {
                "atom_id": atom_id,
                "run_rel": _coerce_string(atom.get("run_rel")),
                "source": _coerce_string(atom.get("source")),
                "severity_hint": _coerce_string(atom.get("severity_hint")),
                "text": text,
                "linked_atom_ids": linked,
            }
        )

    return compact


def _write_chunked_problem_mining_atoms_workspace(
    *,
    workspace_dir: Path,
    prompt_atoms: list[dict[str, Any]],
    max_records_per_miner: int,
    chunk_max_bytes: int = 55_000,
) -> dict[str, Any]:
    """Write stage-1 atom payload files into *workspace_dir* and return the manifest.

    Stage 1 miners need access to the full atom evidence text, but provider file-read tools
    commonly enforce token limits that make a single large JSON file unreadable. To avoid
    "randomly chopping off text" while still fitting inside tool limits, this helper writes
    a small manifest file plus multiple chunk files that together contain the full atom list.

    Written files
    ------------
    - ``atoms.json`` (manifest; small JSON object)
    - ``atoms_index.md`` (compact, line-oriented index of every atom)
    - ``atoms_by_id/atom_####.md`` (one markdown file per atom)
    - ``atoms_chunks/atoms_###.json`` (chunk files; each is a JSON array of atom dicts)
    - ``atoms_text/atoms_###.md`` (markdown view of each chunk for file-read tools)

    The manifest includes a stable list of chunk files; a prompt can instruct the model to:
    1) Read ``atoms.json``.
    2) Read each file listed under ``chunks[*].file``.

    Parameters
    ----------
    workspace_dir:
        Stage-1 miner workspace directory.
    prompt_atoms:
        Atom projection returned by ``_atoms_for_problem_mining_prompt``.
    max_records_per_miner:
        Upper-bound hint included in the manifest for prompt consumption.
    chunk_max_bytes:
        Maximum bytes per chunk file (UTF-8). This value is recorded in the manifest so
        it is not a silent default.

    Returns
    -------
    dict[str, Any]
        Manifest JSON object written to ``atoms.json``.

    Raises
    ------
    ValueError
        When a single atom payload exceeds ``chunk_max_bytes`` and cannot be chunked
        further without truncation.
    """
    import json as _json
    from hashlib import sha256

    workspace_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = workspace_dir / "atoms_chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    text_dir = workspace_dir / "atoms_text"
    text_dir.mkdir(parents=True, exist_ok=True)
    atoms_by_id_dir = workspace_dir / "atoms_by_id"
    atoms_by_id_dir.mkdir(parents=True, exist_ok=True)

    header = "[\n"
    footer = "]\n"
    base_bytes = len((header + footer).encode("utf-8"))

    def _atom_line_bytes(atom: dict[str, Any]) -> int:
        raw = _json.dumps(atom, ensure_ascii=False, separators=(",", ":"))
        # Worst-case sizing: include a trailing comma even though the last entry will omit it.
        line = f"  {raw},\n"
        return len(line.encode("utf-8"))

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = base_bytes

    for atom in prompt_atoms:
        atom_bytes = _atom_line_bytes(atom)
        if atom_bytes + base_bytes > chunk_max_bytes:
            atom_id = _coerce_string(atom.get("atom_id")) or "(missing atom_id)"
            raise ValueError(
                "stage1 atoms chunking failed: a single atom payload is too large for the "
                f"file-read tool limits (atom_id={atom_id} atom_bytes~{atom_bytes} "
                f"chunk_max_bytes={chunk_max_bytes}). Refuse to truncate evidence text; "
                "reduce the atom projection or increase chunk_max_bytes."
            )

        if current and (current_bytes + atom_bytes) > chunk_max_bytes:
            chunks.append(current)
            current = []
            current_bytes = base_bytes

        current.append(atom)
        current_bytes += atom_bytes

    if current:
        chunks.append(current)

    def _preview_text(value: Any, *, max_chars: int = 500) -> str:
        text = _coerce_string(value) or ""
        text = " ".join(text.replace("\r", "\n").split())
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "..."

    def _format_atom_markdown(atom: dict[str, Any]) -> str:
        atom_id = _coerce_string(atom.get("atom_id")) or "(missing atom_id)"
        run_rel = _coerce_string(atom.get("run_rel")) or ""
        source = _coerce_string(atom.get("source")) or ""
        severity = _coerce_string(atom.get("severity_hint")) or ""
        linked_raw = atom.get("linked_atom_ids")
        linked = (
            [x for x in linked_raw if isinstance(x, str) and x.strip()]
            if isinstance(linked_raw, list)
            else []
        )
        text = _coerce_string(atom.get("text")) or ""
        lines = [
            f"## {atom_id}",
            "",
            f"- run_rel: {run_rel}",
            f"- source: {source}",
            f"- severity_hint: {severity}",
            f"- linked_atom_ids: {', '.join(linked) if linked else '(none)'}",
            "",
            "Text:",
            text.rstrip(),
            "",
        ]
        return "\n".join(lines).rstrip() + "\n"

    chunk_entries: list[dict[str, Any]] = []
    index_lines: list[str] = [
        "# Problem Mining Atom Index",
        "",
        "This is a compact index of every evidence atom. Use the listed markdown chunk",
        "for full text details when the preview is not enough.",
        "",
    ]
    total_chunk_bytes = 0
    total_text_chunk_bytes = 0
    total_atom_file_bytes = 0
    atom_file_count = 0

    for idx, atoms_chunk in enumerate(chunks, start=1):
        rel_path = Path("atoms_chunks") / f"atoms_{idx:03d}.json"
        chunk_path = workspace_dir / rel_path
        rel_text_path = Path("atoms_text") / f"atoms_{idx:03d}.md"
        text_path = workspace_dir / rel_text_path

        lines: list[str] = ["["]
        for atom_idx, atom in enumerate(atoms_chunk):
            raw = _json.dumps(atom, ensure_ascii=False, separators=(",", ":"))
            suffix = "," if atom_idx < (len(atoms_chunk) - 1) else ""
            lines.append(f"  {raw}{suffix}")
        lines.append("]")
        content = "\n".join(lines) + "\n"

        chunk_path.write_text(content, encoding="utf-8")
        chunk_bytes = chunk_path.stat().st_size
        total_chunk_bytes += chunk_bytes

        text_parts = [f"# Atom Chunk {idx:03d}", ""]
        for atom in atoms_chunk:
            atom_file_count += 1
            atom_id = _coerce_string(atom.get("atom_id")) or "(missing atom_id)"
            source = _coerce_string(atom.get("source")) or ""
            severity = _coerce_string(atom.get("severity_hint")) or ""
            run_rel = _coerce_string(atom.get("run_rel")) or ""
            preview = _preview_text(atom.get("text"))
            rel_atom_path = Path("atoms_by_id") / f"atom_{atom_file_count:04d}.md"
            atom_file_content = _format_atom_markdown(atom)
            atom_file_path = workspace_dir / rel_atom_path
            atom_file_path.write_text(atom_file_content, encoding="utf-8")
            total_atom_file_bytes += atom_file_path.stat().st_size
            index_lines.append(
                f"- `{atom_id}` | atom_file: `{rel_atom_path.as_posix()}` "
                f"| chunk_file: `{rel_text_path.as_posix()}` "
                f"| source: `{source}` | severity: `{severity}` | run: `{run_rel}` "
                f"| preview: {preview}"
            )
            text_parts.append(atom_file_content)
        text_content = "\n".join(text_parts).rstrip() + "\n"
        text_path.write_text(text_content, encoding="utf-8")
        text_bytes = text_path.stat().st_size
        total_text_chunk_bytes += text_bytes

        chunk_entries.append(
            {
                "file": rel_path.as_posix(),
                "text_file": rel_text_path.as_posix(),
                "atom_count": len(atoms_chunk),
                "bytes": chunk_bytes,
                "text_bytes": text_bytes,
                "sha256": sha256(content.encode("utf-8")).hexdigest(),
                "text_sha256": sha256(text_content.encode("utf-8")).hexdigest(),
            }
        )

    index_content = "\n".join(index_lines).rstrip() + "\n"
    index_path = workspace_dir / "atoms_index.md"
    index_path.write_text(index_content, encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "format": "chunked_problem_mining_atoms_v1",
        "max_records_per_miner": int(max_records_per_miner),
        "total_atom_count": len(prompt_atoms),
        "chunk_count": len(chunk_entries),
        "chunk_max_bytes": int(chunk_max_bytes),
        "total_chunk_bytes": int(total_chunk_bytes),
        "total_text_chunk_bytes": int(total_text_chunk_bytes),
        "atom_file_count": int(atom_file_count),
        "total_atom_file_bytes": int(total_atom_file_bytes),
        "index_file": "atoms_index.md",
        "index_bytes": index_path.stat().st_size,
        "index_preview_chars": 500,
        "atom_file_view": "atoms_by_id/atom_####.md",
        "text_view": "atoms_text/atoms_###.md",
        "chunks": chunk_entries,
    }

    manifest_path = workspace_dir / "atoms.json"
    manifest_path.write_text(
        _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        "[stage1] wrote chunked atoms workspace "
        f"(manifest={manifest_path} chunks={len(chunk_entries)} "
        f"atoms={len(prompt_atoms)} bytes~{total_chunk_bytes})",
        file=sys.stderr,
    )

    return manifest


def _run_problem_mining_stage(
    *,
    atoms: list[dict[str, Any]],
    pipeline_manifest: PipelinePromptManifest,
    artifacts_dir: Path,
    out_json: Path,
    out_md: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    dry_run: bool,
    stage_guidance_text: str,
    max_records_per_miner: int = 20,
) -> dict[str, Any]:
    """Run stage 1 problem mining and write the stage artifacts.

    Runs each configured problem-miner template against the full atom list.
    In dry-run mode no LLM call is made; the CLI writes the prompts and synthesizes a
    deterministic set of problem records from atoms so downstream stages can run on
    offline fixtures.
    The function always writes ``out_json`` and ``out_md``.

    Parameters
    ----------
    atoms:
        Eligible evidence atoms.
    pipeline_manifest:
        Loaded pipeline prompt manifest (version 2).
    artifacts_dir:
        Base artifacts directory (``*.backlog_artifacts``).
    out_json:
        Path for ``*.problem_records.json``.
    out_md:
        Path for ``*.problem_records.md``.
    agent:
        Agent identifier.
    model:
        Optional model override.
    cfg:
        Runner configuration.
    dry_run:
        When ``True``, skip LLM calls and synthesize deterministic problem records.
    stage_guidance_text:
        Problem-mining stage guidance text (injected into prompts).
    max_records_per_miner:
        Maximum problem records per miner call.

    Returns
    -------
    dict[str, Any]
        Stage-1 document dict (also written to ``out_json``).
    """
    import json as _json

    stage = "problem_mining"
    stage_artifacts_dir = artifacts_dir / "problem_mining"
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)

    prompt_atoms = _atoms_for_problem_mining_prompt(atoms)
    atoms_placeholder = _json.dumps(
        {"atoms_file": "atoms.json"},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    all_records: list[dict[str, Any]] = []
    miner_results: list[dict[str, Any]] = []

    for idx, template_path in enumerate(pipeline_manifest.problem_miner_templates, start=1):
        tag = f"problem_mining_{idx:03d}"
        miner_out_dir = stage_artifacts_dir / tag
        miner_out_dir.mkdir(parents=True, exist_ok=True)

        workspace_dir = miner_out_dir / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        manifest = _write_chunked_problem_mining_atoms_workspace(
            workspace_dir=workspace_dir,
            prompt_atoms=prompt_atoms,
            max_records_per_miner=max_records_per_miner,
        )
        atoms_json_path = workspace_dir / "atoms.json"

        template_text = template_path.read_text(encoding="utf-8")
        prompt = (
            template_text
            .replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
            .replace("{{ATOMS_JSON}}", atoms_placeholder)
            .replace("{{MAX_RECORDS_PER_MINER}}", str(max_records_per_miner))
        )

        meta: dict[str, Any] = {
            "tag": tag,
            "template": template_path.name,
            "atom_count": len(atoms),
            "prompt_atom_count": len(prompt_atoms),
            "workspace_dir": str(workspace_dir),
            "atoms_json": str(atoms_json_path),
            "atoms_json_bytes": atoms_json_path.stat().st_size,
            "atoms_chunk_count": int(manifest.get("chunk_count") or 0),
            "atoms_total_chunk_bytes": int(manifest.get("total_chunk_bytes") or 0),
        }

        if dry_run:
            print(
                f"[stage1] dry-run: skipping LLM call for {tag} "
                f"(template={template_path.name})",
                file=sys.stderr,
            )
            # Write the would-be prompt so developers can inspect it.
            (miner_out_dir / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
            meta["prompt_chars"] = len(prompt)
            meta["status"] = "dry_run"
            meta["records"] = []
            miner_results.append(meta)
            continue

        try:
            meta["prompt_chars"] = len(prompt)
            response = run_stage_prompt_json(
                stage=stage,
                prompt=prompt,
                out_dir=miner_out_dir,
                tag=tag,
                agent=agent,
                model=model,
                cfg=cfg,
                workspace_dir=workspace_dir,
                allowed_tools=(
                    ["Read"]
                    if agent == "claude"
                    else ["read_file"]
                    if agent == "gemini"
                    else []
                ),
                include_directories=(
                    [str(workspace_dir)] if agent == "gemini" else []
                ),
            )
            records, warnings = parse_problem_record_list(response)
            meta["status"] = "ok"
            meta["records"] = len(records)
            meta["warnings"] = warnings
            all_records.extend(records)
            print(
                f"[stage1] {tag}: {len(records)} problem records "
                f"({len(warnings)} warnings)",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001
            meta["status"] = "error"
            meta["error"] = str(exc)
            print(
                f"[stage1] {tag}: error during problem mining: {exc}",
                file=sys.stderr,
            )

        miner_results.append(meta)

    if dry_run:
        synthesized = _synthesize_problem_records_from_atoms(atoms, max_records=max_records_per_miner)
        all_records.extend(synthesized)
        print(
            f"[stage1] dry-run: synthesized {len(synthesized)} problem records from atoms",
            file=sys.stderr,
        )

    # Deduplicate by problem_id (keep first occurrence).
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for rec in all_records:
        pid = rec.get("problem_id") or ""
        if pid and pid in seen_ids:
            continue
        if pid:
            seen_ids.add(pid)
        deduped.append(rec)

    stage_doc = build_stage_document(
        stage,
        deduped,
        input_meta={
            "atom_count": len(atoms),
            "miner_count": len(pipeline_manifest.problem_miner_templates),
            "dry_run": dry_run,
            "dry_run_synthesized_records": len(all_records) if dry_run else 0,
            "miner_results": miner_results,
        },
        artifacts={
            "problem_records_json": str(out_json),
            "problem_records_md": str(out_md),
        },
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        _json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    title = out_json.stem.removesuffix(".problem_records") or "Problem Records"
    md_text = _render_problem_records_markdown(
        deduped,
        title=f"{title} – Problem Records",
    )
    out_md.write_text(md_text, encoding="utf-8")

    print(f"[stage1] wrote {out_json}", file=sys.stderr)
    print(f"[stage1] wrote {out_md}", file=sys.stderr)

    return stage_doc




__all__ = [name for name in globals() if not name.startswith("__")]
