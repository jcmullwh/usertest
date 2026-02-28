from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

# Fingerprint logic mirrors `packages/backlog_repo/src/backlog_repo/export.py` so this
# script can run standalone (no PYTHONPATH/package install required).
_EXPORT_PATH_LIKE_RE = re.compile(r"(?:[A-Za-z]:[\\/])?[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+){1,}")
_EXPORT_TOKEN_RE = re.compile(r"[a-z0-9_]+")

_PLAN_FILENAME_RE = re.compile(
    r"^(?P<date>[0-9]{8})_(?:(?P<legacy_ticket_id>BLG-[0-9]{3})_)?(?P<fingerprint>[0-9a-f]{16})_(?P<slug>.+\.md)$"
)


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
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _ticket_export_anchors(ticket: dict[str, Any]) -> set[str]:
    chunks: list[str] = []
    for key in ("title", "problem", "user_impact", "proposed_fix"):
        value = _coerce_string(ticket.get(key))
        if value:
            chunks.append(value)
    chunks.extend(_coerce_string_list(ticket.get("investigation_steps")))

    anchors: set[str] = set()
    for chunk in chunks:
        for match in _EXPORT_PATH_LIKE_RE.findall(chunk):
            anchors.add(match.lower().replace("\\", "/"))
    return anchors


def ticket_export_fingerprint(ticket: dict[str, Any]) -> str:
    title = _coerce_string(ticket.get("title")) or ""
    title_tokens = sorted(set(_EXPORT_TOKEN_RE.findall(title.lower())))
    anchors = sorted(_ticket_export_anchors(ticket))

    change_surface_raw = ticket.get("change_surface")
    change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
    kinds = sorted(set(_coerce_string_list(change_surface.get("kinds"))))

    owner = (
        _coerce_string(ticket.get("suggested_owner"))
        or _coerce_string(ticket.get("component"))
        or "unknown"
    )

    payload = {
        "title_tokens": title_tokens[:24],
        "anchors": anchors[:24],
        "kinds": kinds[:24],
        "owner": owner,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return sha256(blob).hexdigest()[:16]


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw[:-1]).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stderr=subprocess.DEVNULL,
    ).strip()


@dataclass(frozen=True)
class PlanFile:
    fingerprint: str
    bucket: str
    date_tag: str
    path: Path
    title: str


def _extract_first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _normalize_title(title: str) -> str:
    cleaned = title.strip()
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def scan_plan_files(plans_root: Path) -> list[PlanFile]:
    out: list[PlanFile] = []
    for md_path in plans_root.glob("*/*.md"):
        match = _PLAN_FILENAME_RE.match(md_path.name)
        if match is None:
            continue
        fingerprint = match.group("fingerprint")
        date_tag = match.group("date")
        bucket = md_path.parent.name
        try:
            title = _extract_first_heading(md_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            title = ""
        out.append(
            PlanFile(
                fingerprint=fingerprint,
                bucket=bucket,
                date_tag=date_tag,
                path=md_path,
                title=_normalize_title(title),
            )
        )
    return out


@dataclass(frozen=True)
class RunRef:
    run_dir: Path
    fingerprint: str | None
    title: str | None
    started_at_utc: str | None
    git_branch: str | None
    git_head_commit: str | None
    git_base_commit: str | None
    git_commit_performed: bool | None
    pr_url: str | None
    pr_error: str | None
    diff_numstat: list[dict[str, Any]]
    has_patch_diff: bool


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


def scan_implementation_runs(runs_root: Path) -> list[RunRef]:
    out: list[RunRef] = []
    for ticket_ref_path in sorted(runs_root.glob("**/ticket_ref.json"), key=lambda p: str(p)):
        if "_compiled" in {part.lower() for part in ticket_ref_path.parts}:
            continue
        run_dir = ticket_ref_path.parent

        try:
            ticket_ref = _read_json_object(ticket_ref_path)
        except Exception:
            ticket_ref = {}

        fingerprint = _coerce_string(ticket_ref.get("fingerprint"))
        title = _coerce_string(ticket_ref.get("title"))

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

        git_branch = None
        git_head = None
        git_base = None
        git_commit_performed = None
        git_ref_path = run_dir / "git_ref.json"
        if git_ref_path.exists():
            try:
                git_ref = _read_json_object(git_ref_path)
            except Exception:
                git_ref = {}
            git_branch = _coerce_string(git_ref.get("branch"))
            git_head = _coerce_string(git_ref.get("head_commit"))
            git_base = _coerce_string(git_ref.get("base_commit"))
            if isinstance(git_ref.get("commit_performed"), bool):
                git_commit_performed = bool(git_ref.get("commit_performed"))

        diff_numstat: list[dict[str, Any]] = []
        diff_path = run_dir / "diff_numstat.json"
        if diff_path.exists():
            try:
                raw_list = _read_json_list(diff_path)
            except Exception:
                raw_list = []
            diff_numstat = [item for item in raw_list if isinstance(item, dict)]

        out.append(
            RunRef(
                run_dir=run_dir,
                fingerprint=fingerprint,
                title=title,
                started_at_utc=started_at,
                git_branch=git_branch,
                git_head_commit=git_head,
                git_base_commit=git_base,
                git_commit_performed=git_commit_performed,
                pr_url=pr_url,
                pr_error=pr_error,
                diff_numstat=diff_numstat,
                has_patch_diff=(run_dir / "patch.diff").exists(),
            )
        )
    return out


def load_atoms_index(atoms_jsonl: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line_no, line in enumerate(atoms_jsonl.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on line {line_no}: {atoms_jsonl}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"Expected JSON object on line {line_no}: {atoms_jsonl}")
        atom_id = _coerce_string(obj.get("atom_id"))
        if atom_id is None:
            raise ValueError(f"Atom missing atom_id on line {line_no}: {atoms_jsonl}")
        out[atom_id] = obj
    return out


_PATCH_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)$")
_PATCH_DIFF_FILE_PREFIXES = ("--- ", "+++ ")


def _parse_patch_diff_added_lines(patch_diff: str) -> dict[str, list[str]]:
    """Extract added lines from a unified diff, grouped by file path.

    This is a best-effort parser intended for audit inference only.
    """

    out: dict[str, list[str]] = {}
    current_file: str | None = None
    for raw in patch_diff.splitlines():
        header = _PATCH_DIFF_HEADER_RE.match(raw)
        if header:
            current_file = header.group("b").strip()
            out.setdefault(current_file, [])
            continue
        if current_file is None:
            continue
        if raw.startswith(_PATCH_DIFF_FILE_PREFIXES) or raw.startswith("@@"):
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            added = raw[1:].strip()
            if added:
                out[current_file].append(added)
    return out


_NEEDLE_JUNK_RE = re.compile(r"^[^A-Za-z0-9]+$")


def _select_patch_needles(added_lines: list[str], *, limit: int) -> list[str]:
    candidates: list[str] = []
    for line in added_lines:
        candidate = line.strip()
        if len(candidate) < 12:
            continue
        if _NEEDLE_JUNK_RE.match(candidate):
            continue
        lowered = candidate.lower()
        if lowered.startswith(("import ", "from ", "pass", "return ", "raise ", "assert ")):
            continue
        candidates.append(candidate)

    limit_i = max(0, int(limit))
    if limit_i == 0 or not candidates:
        return []

    unique = sorted(set(candidates), key=lambda text: (-len(text), text))
    return unique[:limit_i]


_BUCKET_RANK: dict[str, float] = {
    "6 - archived": 6.0,
    "5 - complete": 5.0,
    "4 - for_review": 4.0,
    "3 - in_progress": 3.0,
    "2 - ready": 2.0,
    "1.5 - to_plan": 1.5,
    "1 - ideas": 1.0,
    "0.5 - to_triage": 0.5,
    "0.3 - todos": 0.3,
    "0.1 - deferred": 0.1,
}

_DONEISH_BUCKETS = {"4 - for_review", "5 - complete", "6 - archived"}


def _best_bucket(buckets: set[str]) -> str | None:
    if not buckets:
        return None
    return max(buckets, key=lambda b: _BUCKET_RANK.get(b, 0.0))


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit triage_atoms against plans + code changes.")
    parser.add_argument(
        "--compiled-dir",
        type=Path,
        default=Path("runs/usertest_implement/usertest/_compiled"),
        help="Path to a _compiled run directory.",
    )
    parser.add_argument(
        "--plans-root",
        type=Path,
        default=Path(".agents/plans"),
        help="Path to .agents/plans.",
    )
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=Path("runs/usertest_implement/usertest"),
        help="Path to runs/usertest_implement/<target>.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output markdown path (default: <compiled>/usertest.backlog.triage_atoms.audit.md).",
    )
    args = parser.parse_args()

    compiled_dir: Path = args.compiled_dir
    backlog_json = compiled_dir / "usertest.backlog.json"
    atoms_jsonl = compiled_dir / "usertest.backlog.atoms.jsonl"
    triage_atoms_json = compiled_dir / "usertest.backlog.triage_atoms.json"

    out_path: Path = args.out or (compiled_dir / "usertest.backlog.triage_atoms.audit.md")

    backlog = _read_json_object(backlog_json)
    tickets_raw = backlog.get("tickets")
    if not isinstance(tickets_raw, list):
        raise SystemExit(f"Expected tickets list in {backlog_json}")

    triage = _read_json_object(triage_atoms_json)
    clusters_raw = triage.get("clusters")
    clusters = clusters_raw if isinstance(clusters_raw, list) else []

    triage_fingerprints: set[str] = set()
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        tickets = cluster.get("tickets")
        if not isinstance(tickets, list):
            continue
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            fp = _coerce_string(ticket.get("fingerprint"))
            if fp:
                triage_fingerprints.add(fp)

    atoms_by_id = load_atoms_index(atoms_jsonl)
    plan_files = scan_plan_files(args.plans_root)
    runs = scan_implementation_runs(args.runs_root)

    plan_buckets_by_fp: dict[str, set[str]] = {}
    plan_paths_by_fp: dict[str, list[Path]] = {}
    for pf in plan_files:
        plan_buckets_by_fp.setdefault(pf.fingerprint, set()).add(pf.bucket)
        plan_paths_by_fp.setdefault(pf.fingerprint, []).append(pf.path)

    runs_by_fp: dict[str, list[RunRef]] = {}
    for run in runs:
        if run.fingerprint:
            runs_by_fp.setdefault(run.fingerprint, []).append(run)

    git_commit_meta_cache: dict[str, tuple[datetime | None, str]] = {}

    def commit_meta(commit: str) -> tuple[datetime | None, str]:
        cached = git_commit_meta_cache.get(commit)
        if cached is not None:
            return cached
        try:
            ts = _git("show", "-s", "--format=%cI", commit)
            subj = _git("show", "-s", "--format=%s", commit)
        except subprocess.CalledProcessError:
            git_commit_meta_cache[commit] = (None, "")
            return (None, "")
        dt = _parse_iso_dt(ts)
        git_commit_meta_cache[commit] = (dt, subj)
        return (dt, subj)

    _SOURCE_RANK: dict[str, int] = {"git_ref_head_commit": 2, "patch_inferred": 1}

    git_log_pickaxe_cache: dict[tuple[str, str, int], list[str]] = {}
    git_show_file_patch_cache: dict[tuple[str, str], str] = {}
    patch_inference_cache: dict[Path, list[dict[str, Any]]] = {}

    def _git_log_pickaxe_commits(file_path: str, needle: str, *, limit: int) -> list[str]:
        key = (file_path, needle, int(limit))
        cached = git_log_pickaxe_cache.get(key)
        if cached is not None:
            return cached
        try:
            raw = _git("log", f"-n{int(limit)}", "--format=%H", "-S", needle, "--", file_path)
        except subprocess.CalledProcessError:
            git_log_pickaxe_cache[key] = []
            return []
        commits = [line.strip() for line in raw.splitlines() if line.strip()]
        git_log_pickaxe_cache[key] = commits
        return commits

    def _git_show_file_patch(commit: str, file_path: str) -> str:
        key = (commit, file_path)
        cached = git_show_file_patch_cache.get(key)
        if cached is not None:
            return cached
        try:
            patch = _git("show", "--format=", "-U0", commit, "--", file_path)
        except subprocess.CalledProcessError:
            git_show_file_patch_cache[key] = ""
            return ""
        git_show_file_patch_cache[key] = patch
        return patch

    def _commit_patch_adds_needle(file_patch: str, needle: str) -> bool:
        if not file_patch:
            return False
        for line in file_patch.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            if needle in line[1:]:
                return True
        return False

    def _infer_commits_from_run_patch(run_dir: Path) -> list[dict[str, Any]]:
        cached = patch_inference_cache.get(run_dir)
        if cached is not None:
            return cached

        patch_path = run_dir / "patch.diff"
        if not patch_path.exists():
            patch_inference_cache[run_dir] = []
            return []

        try:
            patch_diff = patch_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            patch_inference_cache[run_dir] = []
            return []

        added_by_file = _parse_patch_diff_added_lines(patch_diff)
        inferred_by_commit: dict[str, dict[str, Any]] = {}
        for file_path, added_lines in added_by_file.items():
            needles = _select_patch_needles(added_lines, limit=12)
            if not needles:
                continue
            for needle in needles:
                commits = _git_log_pickaxe_commits(file_path, needle, limit=25)
                if not commits:
                    continue
                for commit in commits:
                    file_patch = _git_show_file_patch(commit, file_path)
                    if not _commit_patch_adds_needle(file_patch, needle):
                        continue
                    dt, subj = commit_meta(commit)
                    if dt is None:
                        continue
                    entry = inferred_by_commit.setdefault(
                        commit,
                        {
                            "commit": commit,
                            "committed_at_utc": dt.astimezone(timezone.utc),
                            "subject": subj,
                            "evidence": [],
                        },
                    )
                    if len(entry["evidence"]) < 6:
                        entry["evidence"].append(
                            {"run_dir": str(run_dir), "file": file_path, "needle": needle}
                        )
                    break

        inferred = sorted(inferred_by_commit.values(), key=lambda row: row["committed_at_utc"])
        patch_inference_cache[run_dir] = inferred
        return inferred

    ticket_rows: list[dict[str, Any]] = []
    tickets_considered = 0
    actioned_tickets = 0
    actioned_with_known_fix_time = 0
    actioned_with_unknown_fix_time = 0
    actioned_post_fix = 0
    missing_plan_fingerprints = 0
    missing_run_fingerprints = 0

    backlog_by_fp: dict[str, dict[str, Any]] = {}
    backlog_fp_dupes: set[str] = set()
    for ticket in tickets_raw:
        if not isinstance(ticket, dict):
            continue
        fp = _coerce_string(ticket.get("fingerprint")) or ticket_export_fingerprint(ticket)
        if fp in backlog_by_fp:
            backlog_fp_dupes.add(fp)
            continue
        backlog_by_fp[fp] = ticket

    triage_fps_missing_in_backlog = sorted(
        fp for fp in triage_fingerprints if fp not in backlog_by_fp
    )

    for fingerprint in sorted(triage_fingerprints):
        ticket = backlog_by_fp.get(fingerprint)
        if ticket is None:
            continue

        tickets_considered += 1
        title = _coerce_string(ticket.get("title")) or ""

        plan_buckets = plan_buckets_by_fp.get(fingerprint, set())
        best_plan_bucket = _best_bucket(plan_buckets)
        plan_paths = sorted(plan_paths_by_fp.get(fingerprint, []), key=lambda p: str(p))

        if not plan_buckets:
            missing_plan_fingerprints += 1

        ticket_runs = runs_by_fp.get(fingerprint, [])
        if not ticket_runs:
            missing_run_fingerprints += 1

        implemented_commits_by_hash: dict[str, dict[str, Any]] = {}
        proposed_runs: list[RunRef] = []
        head_commits_missing_in_repo: list[str] = []
        for run in ticket_runs:
            run_has_impl = False

            if run.git_head_commit:
                dt, subj = commit_meta(run.git_head_commit)
                if run.git_commit_performed and dt is not None:
                    existing = implemented_commits_by_hash.get(run.git_head_commit)
                    if (
                        existing is None
                        or _SOURCE_RANK.get(existing.get("source", ""), 0)
                        < _SOURCE_RANK["git_ref_head_commit"]
                    ):
                        implemented_commits_by_hash[run.git_head_commit] = {
                            "commit": run.git_head_commit,
                            "committed_at_utc": dt.astimezone(timezone.utc),
                            "subject": subj,
                            "source": "git_ref_head_commit",
                            "evidence": [],
                        }
                    run_has_impl = True
                elif run.git_commit_performed and dt is None:
                    head_commits_missing_in_repo.append(run.git_head_commit)

            inferred = _infer_commits_from_run_patch(run.run_dir) if run.has_patch_diff else []
            if inferred:
                for entry in inferred:
                    commit = str(entry["commit"])
                    dt = entry["committed_at_utc"]
                    subj = str(entry.get("subject") or "")
                    evidence = entry.get("evidence") if isinstance(entry.get("evidence"), list) else []
                    existing = implemented_commits_by_hash.get(commit)
                    if existing is None:
                        implemented_commits_by_hash[commit] = {
                            "commit": commit,
                            "committed_at_utc": dt,
                            "subject": subj,
                            "source": "patch_inferred",
                            "evidence": evidence,
                        }
                    else:
                        if _SOURCE_RANK.get(existing.get("source", ""), 0) < _SOURCE_RANK["patch_inferred"]:
                            existing["source"] = "patch_inferred"
                        if isinstance(existing.get("evidence"), list):
                            existing["evidence"].extend(evidence)
                        else:
                            existing["evidence"] = evidence
                run_has_impl = True

            if not run_has_impl and (run.has_patch_diff or run.diff_numstat):
                proposed_runs.append(run)

        implemented_commits = sorted(
            implemented_commits_by_hash.values(), key=lambda row: row["committed_at_utc"]
        )
        latest_fix_time: datetime | None = (
            implemented_commits[-1]["committed_at_utc"] if implemented_commits else None
        )
        latest_fix_commit = implemented_commits[-1]["commit"] if implemented_commits else None
        latest_fix_source = implemented_commits[-1]["source"] if implemented_commits else None

        evidence_atom_ids = _coerce_string_list(ticket.get("evidence_atom_ids"))
        evidence_times: list[datetime] = []
        post_fix_atoms_total = 0
        for atom_id in evidence_atom_ids:
            atom = atoms_by_id.get(atom_id)
            if atom is None:
                continue
            ts = _parse_iso_dt(
                _coerce_string(atom.get("timestamp_utc")) or _coerce_string(atom.get("timestamp"))
            )
            if ts is not None:
                evidence_times.append(ts.astimezone(timezone.utc))
            if latest_fix_time is not None and ts is not None:
                if ts.astimezone(timezone.utc) > latest_fix_time:
                    post_fix_atoms_total += 1

        first_evidence = min(evidence_times).isoformat() if evidence_times else None
        last_evidence = max(evidence_times).isoformat() if evidence_times else None

        is_actioned = best_plan_bucket in _DONEISH_BUCKETS
        if is_actioned:
            actioned_tickets += 1
            if latest_fix_time is None:
                actioned_with_unknown_fix_time += 1
            else:
                actioned_with_known_fix_time += 1
            if post_fix_atoms_total:
                actioned_post_fix += 1

        ticket_rows.append(
            {
                "fingerprint": fingerprint,
                "title": title,
                "plan_best_bucket": best_plan_bucket,
                "plan_buckets": sorted(plan_buckets),
                "plan_paths_for_fp": [str(p) for p in plan_paths],
                "runs_total_for_fp": len(ticket_runs),
                "implemented_commits": [
                    {
                        "commit": row["commit"],
                        "committed_at_utc": row["committed_at_utc"].isoformat(),
                        "subject": row.get("subject") or "",
                        "source": row.get("source") or "",
                        "evidence": row.get("evidence")
                        if isinstance(row.get("evidence"), list)
                        else [],
                    }
                    for row in implemented_commits
                ],
                "proposed_runs": [
                    {
                        "run_dir": str(r.run_dir),
                        "started_at_utc": r.started_at_utc,
                        "git_branch": r.git_branch,
                        "git_head_commit": r.git_head_commit,
                        "git_commit_performed": r.git_commit_performed,
                        "pr_url": r.pr_url,
                        "has_patch_diff": r.has_patch_diff,
                        "diff_numstat": r.diff_numstat,
                    }
                    for r in proposed_runs
                ],
                "latest_fix_time_utc": None if latest_fix_time is None else latest_fix_time.isoformat(),
                "latest_fix_commit": latest_fix_commit,
                "latest_fix_source": latest_fix_source,
                "head_commits_missing_in_repo": sorted(set(head_commits_missing_in_repo)),
                "evidence_atoms_total": len(evidence_atom_ids),
                "first_evidence_utc": first_evidence,
                "last_evidence_utc": last_evidence,
                "post_fix_atoms_total": post_fix_atoms_total,
            }
        )

    ticket_rows.sort(key=lambda r: r["fingerprint"])
    row_by_fp = {row["fingerprint"]: row for row in ticket_rows}

    reported_by_fp: dict[str, dict[str, Any]] = {}
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        tickets = cluster.get("tickets", []) if isinstance(cluster.get("tickets"), list) else []
        for t in tickets:
            if not isinstance(t, dict):
                continue
            fp = _coerce_string(t.get("fingerprint"))
            if not fp:
                continue
            entry = reported_by_fp.setdefault(
                fp,
                {
                    "plan_buckets": set(),
                    "impl_runs_count": 0,
                },
            )
            plan = t.get("plan")
            if isinstance(plan, dict):
                for bucket in (
                    plan.get("plan_buckets", [])
                    if isinstance(plan.get("plan_buckets"), list)
                    else []
                ):
                    if isinstance(bucket, str) and bucket.strip():
                        entry["plan_buckets"].add(bucket.strip())
            impl_runs = t.get("implementation_runs")
            if isinstance(impl_runs, list):
                entry["impl_runs_count"] = max(entry["impl_runs_count"], len(impl_runs))

    mismatches: list[str] = []
    for row in ticket_rows:
        fp = row["fingerprint"]
        reported = reported_by_fp.get(fp, {})
        reported_buckets = sorted(reported.get("plan_buckets", set()))
        computed_bucket = row.get("plan_best_bucket")
        reported_actioned = any(b in _DONEISH_BUCKETS for b in reported_buckets)
        computed_actioned = computed_bucket in _DONEISH_BUCKETS
        if reported_actioned != computed_actioned:
            mismatches.append(
                f"- {fp}: triage_atoms.json buckets={reported_buckets or ['none']}, "
                f"computed_best_bucket=`{computed_bucket or 'no_plan'}`"
            )
        reported_impl = int(reported.get("impl_runs_count") or 0)
        computed_impl = int(row.get("runs_total_for_fp") or 0)
        if (reported_impl > 0) != (computed_impl > 0):
            mismatches.append(
                f"- {fp}: triage_atoms.json implementation_runs={reported_impl}, "
                f"computed_runs={computed_impl}"
            )

    clusters_with_actioned = 0
    clusters_with_post_fix = 0
    cluster_lines: list[str] = []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        cid = cluster.get("cluster_id")
        size = cluster.get("size")
        first_seen = _coerce_string(cluster.get("first_seen_utc"))
        last_seen = _coerce_string(cluster.get("last_seen_utc"))
        last_seen_dt = _parse_iso_dt(last_seen).astimezone(timezone.utc) if last_seen else None
        rep = _coerce_string(cluster.get("representative_text")) or ""
        tickets = cluster.get("tickets", []) if isinstance(cluster.get("tickets"), list) else []

        cluster_fps: list[str] = []
        for t in tickets:
            if isinstance(t, dict):
                fp = _coerce_string(t.get("fingerprint"))
                if fp:
                    cluster_fps.append(fp)

        any_actioned = any(
            (row_by_fp.get(fp, {}).get("plan_best_bucket") in _DONEISH_BUCKETS) for fp in cluster_fps
        )
        if any_actioned:
            clusters_with_actioned += 1

        cluster_lines.append(f"### Cluster {cid}")
        cluster_lines.append(f"- Size: **{size}**")
        if first_seen or last_seen:
            cluster_lines.append(f"- Seen (UTC): `{first_seen or '?'} -> {last_seen or '?'}`")
        cluster_lines.append(f"- Representative: {rep.splitlines()[0] if rep else ''}")

        post_fix_fps: list[str] = []
        if last_seen_dt is not None:
            for fp in cluster_fps:
                row = row_by_fp.get(fp)
                if not row:
                    continue
                best_bucket = row.get("plan_best_bucket")
                if best_bucket not in _DONEISH_BUCKETS:
                    continue
                fix_ts = _coerce_string(row.get("latest_fix_time_utc")) if row else None
                fix_dt = _parse_iso_dt(fix_ts).astimezone(timezone.utc) if fix_ts else None
                if fix_dt is not None and last_seen_dt > fix_dt:
                    post_fix_fps.append(fp)
        if post_fix_fps:
            clusters_with_post_fix += 1
            cluster_lines.append(
                f"- Tickets with cluster evidence after their latest fix commit: **{len(sorted(set(post_fix_fps)))}** ({', '.join(sorted(set(post_fix_fps)))})"
            )

        if tickets:
            cluster_lines.append("- Tickets:")
        for t in tickets:
            if not isinstance(t, dict):
                continue
            fp = _coerce_string(t.get("fingerprint")) or "unknown"
            title = _coerce_string(t.get("title")) or ""
            row = row_by_fp.get(fp)
            plan_bucket = row.get("plan_best_bucket") if row else None
            cluster_lines.append(f"  - {fp} ({plan_bucket or 'no_plan'}) - {title}")
        cluster_lines.append("")

    generated_at = datetime.now(timezone.utc).isoformat()
    lines: list[str] = []
    lines.append("# Triage Atoms Audit")
    lines.append("")
    lines.append(f"- Generated (UTC): `{generated_at}`")
    lines.append(f"- Source: `{triage_atoms_json}`")
    lines.append(f"- Fingerprints in triage_atoms clusters: **{len(triage_fingerprints)}**")
    lines.append(f"- Tickets audited (present in backlog.json): **{tickets_considered}**")
    lines.append(
        f"- Actioned (best plan bucket in for_review/complete/archived): **{actioned_tickets}**"
    )
    lines.append(f"- Actioned with known latest fix time: **{actioned_with_known_fix_time}**")
    lines.append(f"- Actioned with unknown latest fix time: **{actioned_with_unknown_fix_time}**")
    lines.append(
        f"- Actioned with evidence after latest fix commit (known fix time only): **{actioned_post_fix}**"
    )
    lines.append(
        f"- Fingerprints in triage clusters missing from backlog.json: **{len(triage_fps_missing_in_backlog)}**"
    )
    lines.append(f"- Tickets missing any plan file for fingerprint: **{missing_plan_fingerprints}**")
    lines.append(
        f"- Tickets missing any implementation run for fingerprint: **{missing_run_fingerprints}**"
    )
    if backlog_fp_dupes:
        lines.append(f"- WARNING: duplicate fingerprints in backlog.json: **{len(backlog_fp_dupes)}**")
    lines.append("")

    lines.append("## Key Findings")
    lines.append("")
    lines.append(
        "- This audit joins plans/runs to tickets by **fingerprint** (computed from ticket text)."
    )
    lines.append(
        "- When `latest fix time` is `unknown`, this audit does not claim \"no post-fix atoms\"; it means the fix time could not be proven from repo history."
    )
    lines.append(
        f"- In the current dataset, **{actioned_post_fix} / {actioned_with_known_fix_time}** actioned tickets show evidence atoms occurring after their latest proven fix commit time."
    )
    lines.append("")

    lines.append("## Ticket Audit")
    lines.append("")
    for row in ticket_rows:
        title = row["title"]
        fp = row["fingerprint"]
        best_bucket = row["plan_best_bucket"] or "no_plan"
        lines.append(f"### {fp} - {title}")
        lines.append(f"- Fingerprint: `{fp}`")
        lines.append(f"- Best plan bucket: `{best_bucket}`")
        lines.append(f"- Plan buckets for fingerprint: `{', '.join(row['plan_buckets']) or 'none'}`")
        if row["plan_paths_for_fp"]:
            lines.append(f"- Plan paths for fingerprint: `{len(row['plan_paths_for_fp'])}` file(s)")
        else:
            lines.append("- Plan paths for fingerprint: `0` file(s)")
        if row["implemented_commits"]:
            latest_fix = row["latest_fix_time_utc"]
            fix_source = row.get("latest_fix_source") or "unknown"
            lines.append(f"- Latest fix commit time (UTC): `{latest_fix}` (source: `{fix_source}`)")
            lines.append(f"- Latest fix commit: `{row['latest_fix_commit']}`")

            if fix_source == "patch_inferred":
                evidence: list[dict[str, Any]] = []
                for c in row["implemented_commits"]:
                    if c.get("commit") == row.get("latest_fix_commit") and isinstance(
                        c.get("evidence"), list
                    ):
                        evidence = c.get("evidence")  # type: ignore[assignment]
                        break
                for sample in evidence[:3]:
                    needle = _coerce_string(sample.get("needle")) or ""
                    if len(needle) > 120:
                        needle = needle[:117] + "..."
                    lines.append(
                        f"- Fix inference evidence: `{sample.get('file')}` matched needle `{needle}` (from `{sample.get('run_dir')}`)"
                    )
        else:
            missing_head = row.get("head_commits_missing_in_repo") or []
            if missing_head:
                lines.append(
                    "- Latest fix commit time (UTC): `unknown` (recorded head_commit not in local repo; patch inference found no matching commit)"
                )
            else:
                lines.append(
                    "- Latest fix commit time (UTC): `unknown` (no committed implementation run recorded; patch inference found no matching commit)"
                )
        lines.append(
            f"- Evidence atoms: **{row['evidence_atoms_total']}** (first={row['first_evidence_utc']}, last={row['last_evidence_utc']})"
        )
        lines.append(f"- Post-fix evidence atoms: **{row['post_fix_atoms_total']}**")
        lines.append("")

    lines.append("## Cluster Audit")
    lines.append("")
    lines.append(f"- Clusters emitted: **{len(clusters)}**")
    lines.append(f"- Clusters containing any actioned ticket: **{clusters_with_actioned}**")
    lines.append(f"- Clusters with evidence after an actioned ticket fix: **{clusters_with_post_fix}**")
    lines.append("")
    lines.extend(cluster_lines)

    lines.append("## Mismatches vs triage_atoms.json (QC)")
    lines.append("")
    if triage_fps_missing_in_backlog:
        lines.append(
            f"- Fingerprints missing from backlog.json: `{', '.join(triage_fps_missing_in_backlog[:25])}`"
        )
    if mismatches:
        lines.extend(mismatches)
    else:
        lines.append("- None detected.")
    lines.append("")

    lines.append("## Better Plan (avoid repeating false positives)")
    lines.append("")
    lines.append(
        "- **Link to code reality**: when marking a ticket `for_review`/`complete`, require a commit/PR reference (or explicitly record \"proposed-only\" with the run_dir/patch.diff)."
    )
    lines.append(
        "- **Post-fix validation gate**: after merge, re-run the smallest verification path that previously produced the atom(s) and confirm no new atoms appear for that fingerprint."
    )
    lines.append(
        "- **Regression coverage**: for issues that recur, prefer a unit/integration regression test or a deterministic preflight that fails fast with structured diagnostics."
    )
    lines.append(
        "- **Review checklist**: use the Failure Analysis Template questions when a ticket shows post-fix evidence."
    )
    lines.append("")

    lines.append("## Failure Analysis Template (only when post-fix atoms exist)")
    lines.append("")
    lines.append("Use this checklist when `post-fix evidence atoms > 0` and the run includes the fix commit:")
    lines.append("")
    lines.append("- Root cause vs symptom: did we fix the underlying invariant or just a surface error message?")
    lines.append("- Scope: was the solution too narrow compared to the evidence contexts?")
    lines.append("- Assumptions: did we assume tools existed or paths behaved without verifying?")
    lines.append("- Verification: did we add a regression test or a deterministic preflight that would have caught this?")
    lines.append("- Rollout: did we update docs/next-actions so users hit the supported path instead of falling back into the broken one?")
    lines.append("- Instrumentation: did we log enough to quickly differentiate new vs old failure modes?")
    lines.append("")

    lines.append("## QC Checks")
    lines.append("")
    lines.append(
        "- Spot-check any ticket that shows `post-fix evidence atoms > 0` by opening the underlying run dir from the atom id and verifying the repo commit used in that run."
    )
    lines.append("")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
