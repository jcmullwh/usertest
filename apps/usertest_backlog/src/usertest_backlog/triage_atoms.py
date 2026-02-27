from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from backlog_repo.plan_index import scan_plan_ticket_index
from triage_engine import cluster_items_knn
from triage_engine.embeddings import Embedder, get_default_embedder
from triage_engine.testing import HashingEmbedder

TextNormalization = Literal["raw", "smart"]
EmbedderSpec = Literal["hashing", "openai"]

_COMMAND_FAILURE_TEXT_RE = re.compile(r"^Command failed: exit_code=\d+; command=(?P<cmd>.+)$")


def load_atoms_jsonl(path: Path) -> list[dict[str, Any]]:
    atoms: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on line {line_no}: {path}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"Expected JSON object on line {line_no}: {path}")
        atom_id = obj.get("atom_id")
        text = obj.get("text")
        if not isinstance(atom_id, str) or not atom_id.strip():
            raise ValueError(f"Atom missing atom_id on line {line_no}: {path}")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Atom missing text on line {line_no}: {path}")
        atoms.append(obj)
    return atoms


def load_backlog_json(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"Backlog JSON must be an object: {path}")
    return doc


def _coerce_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if cleaned:
            out.append(cleaned)
    return out


def _normalize_atom_text(text: str, *, mode: TextNormalization) -> str:
    cleaned = text.strip()
    if mode == "raw":
        return cleaned
    match = _COMMAND_FAILURE_TEXT_RE.match(cleaned)
    if match is not None:
        cmd = match.group("cmd").strip()
        return cmd or cleaned
    return cleaned


def resolve_embedder(spec: EmbedderSpec) -> tuple[Embedder, dict[str, Any]]:
    if spec == "hashing":
        return HashingEmbedder(), {"embedder": "hashing"}
    if spec == "openai":
        return get_default_embedder(), {"embedder": "openai"}
    raise ValueError(f"Unknown embedder: {spec}")


@dataclass(frozen=True)
class TicketStatus:
    ticket_id: str
    plan_status: str | None
    plan_buckets: list[str]
    plan_paths: list[str]
    fingerprints: list[str]


def build_ticket_status_index(*, owner_root: Path) -> dict[str, TicketStatus]:
    raw = scan_plan_ticket_index(owner_root=owner_root)
    by_ticket: dict[str, dict[str, Any]] = {}

    for fingerprint, meta in raw.items():
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            continue
        if not isinstance(meta, dict):
            continue

        plan_status = meta.get("status") if isinstance(meta.get("status"), str) else None
        buckets = [b for b in meta.get("buckets", []) if isinstance(b, str) and b.strip()]
        paths = [p for p in meta.get("paths", []) if isinstance(p, str) and p.strip()]
        ticket_ids = [t for t in meta.get("ticket_ids", []) if isinstance(t, str) and t.strip()]

        for ticket_id in ticket_ids:
            entry = by_ticket.setdefault(
                ticket_id,
                {
                    "plan_status": None,
                    "buckets": set(),
                    "paths": set(),
                    "fingerprints": set(),
                },
            )
            entry["fingerprints"].add(fingerprint)
            entry["buckets"].update(buckets)
            entry["paths"].update(paths)
            # Prefer actioned when mixed.
            if entry["plan_status"] is None:
                entry["plan_status"] = plan_status
            elif entry["plan_status"] != "actioned" and plan_status == "actioned":
                entry["plan_status"] = plan_status

    out: dict[str, TicketStatus] = {}
    for ticket_id, meta in by_ticket.items():
        out[ticket_id] = TicketStatus(
            ticket_id=ticket_id,
            plan_status=meta.get("plan_status"),
            plan_buckets=sorted(meta.get("buckets", set())),
            plan_paths=sorted(meta.get("paths", set())),
            fingerprints=sorted(meta.get("fingerprints", set())),
        )
    return out


@dataclass(frozen=True)
class ImplementationRun:
    run_dir: str
    started_at_utc: str | None
    pr_url: str | None
    pr_error: str | None
    head_commit: str | None
    branch: str | None
    diff_numstat: list[dict[str, Any]]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json_object(path: Path) -> dict[str, Any]:
    doc = _read_json(path)
    if not isinstance(doc, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return doc


def _read_json_list(path: Path) -> list[Any]:
    doc = _read_json(path)
    if not isinstance(doc, list):
        raise ValueError(f"Expected JSON list: {path}")
    return doc


def _rel_path(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def build_implementation_index(
    *,
    repo_root: Path,
    implementation_root: Path,
) -> dict[str, list[ImplementationRun]]:
    out: dict[str, list[ImplementationRun]] = defaultdict(list)
    if not implementation_root.exists():
        return {}

    for ticket_ref_path in sorted(implementation_root.glob("**/ticket_ref.json"), key=str):
        if "_compiled" in {part.lower() for part in ticket_ref_path.parts}:
            continue

        try:
            ticket_ref = _read_json_object(ticket_ref_path)
        except Exception:
            continue

        ticket_id = _coerce_string(ticket_ref.get("ticket_id"))
        if ticket_id is None:
            continue

        run_dir = ticket_ref_path.parent

        started_at = None
        timing_path = run_dir / "timing.json"
        if timing_path.exists():
            try:
                timing = _read_json_object(timing_path)
            except Exception:
                timing = {}
            started_at = _coerce_string(timing.get("started_at"))

        pr_url = None
        pr_error = None
        pr_ref_path = run_dir / "pr_ref.json"
        if pr_ref_path.exists():
            try:
                pr_ref = _read_json_object(pr_ref_path)
            except Exception:
                pr_ref = {}
            pr_url = _coerce_string(pr_ref.get("url"))
            pr_error = _coerce_string(pr_ref.get("error"))

        head_commit = None
        branch = None
        git_ref_path = run_dir / "git_ref.json"
        if git_ref_path.exists():
            try:
                git_ref = _read_json_object(git_ref_path)
            except Exception:
                git_ref = {}
            head_commit = _coerce_string(git_ref.get("head_commit"))
            branch = _coerce_string(git_ref.get("branch"))

        diff_numstat: list[dict[str, Any]] = []
        diff_path = run_dir / "diff_numstat.json"
        if diff_path.exists():
            try:
                raw_list = _read_json_list(diff_path)
            except Exception:
                raw_list = []
            diff_numstat = [item for item in raw_list if isinstance(item, dict)]

        out[ticket_id].append(
            ImplementationRun(
                run_dir=_rel_path(repo_root, run_dir),
                started_at_utc=started_at,
                pr_url=pr_url,
                pr_error=pr_error,
                head_commit=head_commit,
                branch=branch,
                diff_numstat=diff_numstat,
            )
        )

    # Sort newest-first when timing is present (else stable by path).
    for ticket_id, runs in out.items():
        runs.sort(
            key=lambda run: (
                run.started_at_utc is None,
                "" if run.started_at_utc is None else run.started_at_utc,
                run.run_dir,
            ),
            reverse=True,
        )
        out[ticket_id] = runs

    return dict(out)


def infer_backlog_json(atoms_jsonl: Path, *, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    name = atoms_jsonl.name
    if name.endswith(".backlog.atoms.jsonl"):
        candidate = atoms_jsonl.with_name(name.replace(".backlog.atoms.jsonl", ".backlog.json"))
        if candidate.exists():
            return candidate
    return None


def _default_triage_output_paths(atoms_jsonl: Path) -> tuple[Path, Path]:
    name = atoms_jsonl.name
    if name.endswith(".backlog.atoms.jsonl"):
        prefix = name.replace(".backlog.atoms.jsonl", ".backlog")
        return (
            atoms_jsonl.with_name(f"{prefix}.triage_atoms.json"),
            atoms_jsonl.with_name(f"{prefix}.triage_atoms.md"),
        )
    return (
        atoms_jsonl.with_name(f"{atoms_jsonl.stem}.triage_atoms.json"),
        atoms_jsonl.with_name(f"{atoms_jsonl.stem}.triage_atoms.md"),
    )


def triage_atoms(
    atoms: list[dict[str, Any]],
    *,
    embedder: Embedder,
    text_normalization: TextNormalization,
    k: int,
    overall_similarity_threshold: float,
    representative_similarity_threshold: float | None,
    min_cluster_size: int,
    tickets: list[dict[str, Any]] | None = None,
    ticket_status_by_id: dict[str, TicketStatus] | None = None,
    implementation_runs_by_ticket: dict[str, list[ImplementationRun]] | None = None,
) -> dict[str, Any]:
    atom_to_ticket_ids: dict[str, list[str]] = defaultdict(list)
    tickets_by_id: dict[str, dict[str, Any]] = {}
    if tickets is not None:
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            ticket_id = _coerce_string(ticket.get("ticket_id"))
            if ticket_id is None:
                continue
            tickets_by_id[ticket_id] = ticket
            for atom_id in _coerce_string_list(ticket.get("evidence_atom_ids")):
                atom_to_ticket_ids[atom_id].append(ticket_id)

    def _atom_text(atom: dict[str, Any]) -> str:
        raw = atom.get("text")
        text = raw if isinstance(raw, str) else ""
        return _normalize_atom_text(text, mode=text_normalization)

    def _atom_title(atom: dict[str, Any]) -> str:
        return _atom_text(atom).replace("\n", " ")[:120]

    clusters_idx = cluster_items_knn(
        atoms,
        get_title=_atom_title,
        get_text_chunks=lambda atom: [_atom_text(atom)],
        embedder=embedder,
        k=int(k),
        overall_similarity_threshold=float(overall_similarity_threshold),
        representative_similarity_threshold=representative_similarity_threshold,
        include_singletons=True,
    )

    clusters_payload: list[dict[str, Any]] = []
    skipped_clusters = 0
    for cluster in clusters_idx:
        if len(cluster) < max(1, int(min_cluster_size)):
            skipped_clusters += 1
            continue

        timestamps: list[str] = []
        sources: Counter[str] = Counter()
        severities: Counter[str] = Counter()
        atom_ids: list[str] = []

        ticket_ids: set[str] = set()
        atoms_cited_by_ticket: Counter[str] = Counter()

        for idx in cluster:
            atom = atoms[idx]
            atom_id = _coerce_string(atom.get("atom_id")) or ""
            atom_ids.append(atom_id)
            ts = _coerce_string(atom.get("timestamp_utc"))
            if ts:
                timestamps.append(ts)
            sources[_coerce_string(atom.get("source")) or "unknown"] += 1
            severities[_coerce_string(atom.get("severity_hint")) or "unknown"] += 1

            for tid in atom_to_ticket_ids.get(atom_id, []):
                ticket_ids.add(tid)
                atoms_cited_by_ticket[tid] += 1

        representative_idx = min(cluster)
        rep_atom = atoms[representative_idx]
        rep_atom_id = _coerce_string(rep_atom.get("atom_id")) or ""
        rep_text = _atom_text(rep_atom)

        tickets_payload: list[dict[str, Any]] = []
        for tid in sorted(ticket_ids):
            ticket = tickets_by_id.get(tid, {})
            plan = ticket_status_by_id.get(tid) if ticket_status_by_id else None
            impl_runs = (
                implementation_runs_by_ticket.get(tid, [])
                if implementation_runs_by_ticket
                else []
            )
            tickets_payload.append(
                {
                    "ticket_id": tid,
                    "title": _coerce_string(ticket.get("title")),
                    "stage": _coerce_string(ticket.get("stage")),
                    "severity": _coerce_string(ticket.get("severity")),
                    "atoms_cited_in_cluster": int(atoms_cited_by_ticket.get(tid, 0)),
                    "plan": (
                        None
                        if plan is None
                        else {
                            "plan_status": plan.plan_status,
                            "plan_buckets": plan.plan_buckets,
                            "fingerprints": plan.fingerprints,
                            "paths": plan.plan_paths,
                        }
                    ),
                    "implementation_runs": [
                        {
                            "run_dir": run.run_dir,
                            "started_at_utc": run.started_at_utc,
                            "pr_url": run.pr_url,
                            "pr_error": run.pr_error,
                            "head_commit": run.head_commit,
                            "branch": run.branch,
                            "diff_numstat": run.diff_numstat,
                        }
                        for run in impl_runs
                    ],
                }
            )

        plan_status_counts: Counter[str] = Counter()
        plan_bucket_counts: Counter[str] = Counter()
        for ticket in tickets_payload:
            plan = ticket.get("plan")
            if not isinstance(plan, dict):
                plan_status_counts["unknown"] += 1
                plan_bucket_counts["unknown"] += 1
                continue
            plan_status_counts[_coerce_string(plan.get("plan_status")) or "unknown"] += 1
            buckets = plan.get("plan_buckets")
            if isinstance(buckets, list) and buckets:
                for bucket in buckets:
                    if isinstance(bucket, str) and bucket.strip():
                        plan_bucket_counts[bucket.strip()] += 1
            else:
                plan_bucket_counts["unknown"] += 1

        clusters_payload.append(
            {
                "cluster_id": 0,  # reassigned after sorting
                "size": len(cluster),
                "first_seen_utc": min(timestamps) if timestamps else None,
                "last_seen_utc": max(timestamps) if timestamps else None,
                "representative_atom_id": rep_atom_id,
                "representative_text": rep_text,
                "atom_ids": atom_ids,
                "sources": dict(sources),
                "severity_hints": dict(severities),
                "tickets_total": len(ticket_ids),
                "tickets": tickets_payload,
                "tickets_plan_status_counts": dict(plan_status_counts),
                "tickets_plan_bucket_counts": dict(plan_bucket_counts),
            }
        )

    clusters_payload.sort(
        key=lambda c: (
            -int(c.get("tickets_total", 0)),
            -int(c.get("size", 0)),
            _coerce_string(c.get("representative_atom_id")) or "",
        )
    )
    for idx, cluster in enumerate(clusters_payload, start=1):
        cluster["cluster_id"] = idx

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": 1,
        "generated_at_utc": generated_at,
        "config": {
            "embedder": getattr(embedder, "__class__", type(embedder)).__name__,
            "text_normalization": text_normalization,
            "k": int(k),
            "overall_similarity_threshold": float(overall_similarity_threshold),
            "representative_similarity_threshold": representative_similarity_threshold,
            "min_cluster_size": int(min_cluster_size),
        },
        "totals": {
            "atoms_total": len(atoms),
            "clusters_total": len(clusters_idx),
            "clusters_emitted": len(clusters_payload),
            "clusters_skipped": int(skipped_clusters),
        },
        "clusters": clusters_payload,
    }


def write_triage_atoms(
    report: dict[str, Any],
    *,
    atoms_jsonl: Path,
    out_json: Path | None,
    out_md: Path | None,
) -> tuple[Path, Path]:
    default_json, default_md = _default_triage_output_paths(atoms_jsonl)
    out_json_path = out_json.resolve() if out_json is not None else default_json.resolve()
    out_md_path = out_md.resolve() if out_md is not None else default_md.resolve()

    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    out_md_path.parent.mkdir(parents=True, exist_ok=True)
    out_json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out_md_path.write_text(render_triage_atoms_markdown(report), encoding="utf-8")
    return out_json_path, out_md_path


def render_triage_atoms_markdown(report: dict[str, Any]) -> str:
    totals = report.get("totals", {})
    cfg = report.get("config", {})
    clusters_raw = report.get("clusters")

    lines: list[str] = []
    lines.append("# Atom Cluster Report")
    lines.append("")
    lines.append(f"- Generated (UTC): `{_coerce_string(report.get('generated_at_utc')) or ''}`")
    lines.append(f"- Atoms: **{int(totals.get('atoms_total', 0))}**")
    lines.append(f"- Clusters emitted: **{int(totals.get('clusters_emitted', 0))}**")
    lines.append(f"- Text normalization: `{_coerce_string(cfg.get('text_normalization')) or ''}`")
    lines.append(f"- Embedder: `{_coerce_string(cfg.get('embedder')) or ''}`")
    lines.append("")

    clusters = clusters_raw if isinstance(clusters_raw, list) else []
    if not clusters:
        lines.append("No clusters found.")
        lines.append("")
        return "\n".join(lines)

    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        cid = int(cluster.get("cluster_id", 0))
        size = int(cluster.get("size", 0))
        tickets_total = int(cluster.get("tickets_total", 0))

        rep_text = _coerce_string(cluster.get("representative_text")) or ""
        if len(rep_text) > 200:
            rep_text = rep_text[:200] + "..."

        lines.append(f"## Cluster {cid}")
        lines.append(f"- Size: **{size}**")
        lines.append(f"- Tickets: **{tickets_total}**")

        first_seen = _coerce_string(cluster.get("first_seen_utc"))
        last_seen = _coerce_string(cluster.get("last_seen_utc"))
        if first_seen or last_seen:
            lines.append(f"- Seen: `{first_seen or ''}` → `{last_seen or ''}`")
        if rep_text:
            lines.append(f"- Representative: {rep_text}")

        sources = cluster.get("sources")
        if isinstance(sources, dict) and sources:
            items = []
            for key, value in sources.items():
                if not isinstance(key, str):
                    continue
                try:
                    count = int(value)
                except (TypeError, ValueError):
                    continue
                items.append((key, count))
            items.sort(key=lambda kv: (-kv[1], kv[0]))
            preview = ", ".join([f"{k}={v}" for k, v in items[:12]])
            suffix = "" if len(items) <= 12 else ", ..."
            lines.append(f"- Sources: {preview}{suffix}")

        tickets = cluster.get("tickets")
        if isinstance(tickets, list) and tickets:
            lines.append("- Tickets:")
            for ticket in tickets[:20]:
                if not isinstance(ticket, dict):
                    continue
                tid = _coerce_string(ticket.get("ticket_id")) or "unknown"
                title = _coerce_string(ticket.get("title")) or ""
                cited = int(ticket.get("atoms_cited_in_cluster", 0))

                bucket_hint = ""
                plan = ticket.get("plan")
                if isinstance(plan, dict):
                    buckets = plan.get("plan_buckets")
                    if isinstance(buckets, list) and buckets:
                        bucket_list = [b for b in buckets if isinstance(b, str) and b.strip()]
                        if bucket_list:
                            bucket_hint = f" ({', '.join(bucket_list)})"

                pr_hint = ""
                impl_runs = ticket.get("implementation_runs")
                if isinstance(impl_runs, list) and impl_runs:
                    first = impl_runs[0] if isinstance(impl_runs[0], dict) else None
                    if first is not None:
                        pr_url = _coerce_string(first.get("pr_url"))
                        head_commit = _coerce_string(first.get("head_commit"))
                        if pr_url:
                            pr_hint = f" PR: {pr_url}"
                        elif head_commit:
                            pr_hint = f" commit: {head_commit}"

                title_part = f" - {title}" if title else ""
                lines.append(
                    f"  - {tid}{bucket_hint} (cites {cited} atom(s)){pr_hint}{title_part}"
                )
            if len(tickets) > 20:
                lines.append(f"  - ... ({len(tickets) - 20} more)")

        lines.append("")

    return "\n".join(lines)
