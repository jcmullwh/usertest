# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

import argparse
from pathlib import Path

from usertest_backlog.shared import *


def build_parser() -> argparse.ArgumentParser:
    """Build the `usertest_backlog` CLI parser.

    Returns
    -------
    argparse.ArgumentParser
        Computed return value.
    """
    parser = argparse.ArgumentParser(prog="usertest-backlog")
    sub = parser.add_subparsers(dest="cmd", required=True)

    reports_p = sub.add_parser("reports", help="Report history commands.")
    reports_sub = reports_p.add_subparsers(dest="reports_cmd", required=True)
    reports_compile_p = reports_sub.add_parser(
        "compile",
        help="Compile report.json + metadata across runs into a JSONL history file.",
    )
    reports_compile_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (e.g. tiktok_vids).",
    )
    reports_compile_p.add_argument(
        "--repo-input",
        help="Optional match for target_ref.repo_input (path or git URL).",
    )
    reports_compile_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_compile_p.add_argument(
        "--out",
        type=Path,
        help=(
            "Output JSONL path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_compile_p.add_argument(
        "--embed",
        choices=["none", "definitions", "prompt", "all"],
        default="definitions",
        help=(
            "How much extra run context to embed (beyond JSON artifacts). "
            "none: only JSON; definitions: persona/mission/schema/template; "
            "prompt: + prompt.txt; all: + users.md."
        ),
    )
    reports_compile_p.add_argument(
        "--max-embed-bytes",
        type=int,
        default=200_000,
        help="Skip embedding any single text file larger than this many bytes.",
    )
    reports_compile_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_analyze_p = reports_sub.add_parser(
        "analyze",
        help="Analyze run outcomes and cluster recurring issues from batch/historical runs.",
    )
    reports_analyze_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (e.g. tiktok_vids).",
    )
    reports_analyze_p.add_argument(
        "--repo-input",
        help="Optional match for target_ref.repo_input (path or git URL).",
    )
    reports_analyze_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_analyze_p.add_argument(
        "--history",
        type=Path,
        help="Path to a compiled report history JSONL (from `reports compile`).",
    )
    reports_analyze_p.add_argument(
        "--out-json",
        type=Path,
        help=(
            "Output analysis JSON path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_analyze_p.add_argument(
        "--out-md",
        type=Path,
        help=("Output markdown summary path (defaults next to --out-json with .md extension)."),
    )
    reports_analyze_p.add_argument(
        "--actions",
        type=Path,
        help=(
            "Optional JSON action registry for addressed comments (date/plan metadata). "
            "Defaults to configs/issue_actions.json when present."
        ),
    )
    reports_analyze_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_window_p = reports_sub.add_parser(
        "window",
        help="Summarize the last N runs vs the previous N runs (timing + outcomes + regressions).",
    )
    reports_window_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (e.g. tiktok_vids).",
    )
    reports_window_p.add_argument(
        "--repo-input",
        help="Optional match for target_ref.repo_input (path or git URL).",
    )
    reports_window_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_window_p.add_argument(
        "--last",
        type=int,
        default=12,
        help="Number of most recent runs to summarize.",
    )
    reports_window_p.add_argument(
        "--baseline",
        type=int,
        help="Number of prior runs to use as a baseline window (defaults to --last).",
    )
    reports_window_p.add_argument(
        "--out-json",
        type=Path,
        help=(
            "Output summary JSON path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_window_p.add_argument(
        "--out-md",
        type=Path,
        help=("Output markdown summary path (defaults next to --out-json with .md extension)."),
    )
    reports_window_p.add_argument(
        "--actions",
        type=Path,
        help=(
            "Optional JSON action registry for addressed comments (date/plan metadata). "
            "Defaults to configs/issue_actions.json when present."
        ),
    )
    reports_window_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_backlog_p = reports_sub.add_parser(
        "backlog",
        help="Generate an actionable backlog using ensemble ticket miners over run artifacts.",
    )
    reports_backlog_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (e.g. tiktok_vids).",
    )
    reports_backlog_p.add_argument(
        "--repo-input",
        help="Optional match for target_ref.repo_input (path or git URL).",
    )
    reports_backlog_p.add_argument(
        "--research-ref",
        help=(
            "Explicit source-of-truth Git ref for stage-3 acquisition. Defaults to "
            "configs/backlog_research.yaml; research is blocked when neither is available."
        ),
    )
    reports_backlog_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_backlog_p.add_argument(
        "--out-json",
        type=Path,
        help=(
            "Output backlog JSON path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_backlog_p.add_argument(
        "--out-md",
        type=Path,
        help="Output markdown summary path (defaults next to --out-json with .md extension).",
    )
    reports_backlog_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )
    reports_backlog_p.add_argument(
        "--prompts-dir",
        type=Path,
        help="Optional prompt template directory (defaults to configs/backlog_prompts).",
    )
    reports_backlog_p.add_argument(
        "--breadth-profile",
        choices=list(_BREADTH_PROFILE_CHOICES),
        default=_BREADTH_PROFILE_EXTERNAL,
        help=(
            "Breadth interpretation profile. "
            "Defaults to external_generalization; use internal_maintenance for same-repo "
            "self-learning runs."
        ),
    )
    reports_backlog_p.add_argument(
        "--agent",
        choices=["claude", "codex", "gemini"],
        default="codex",
        help=(
            "Agent CLI used for backlog stages. Live runs default to the signed-in "
            "host Codex subscription because exact-session correction is required; "
            "other backends remain available for dry-run analysis."
        ),
    )
    reports_backlog_p.add_argument(
        "--model",
        help="Optional model override for backlog miner prompts.",
    )
    reports_backlog_p.add_argument(
        "--miners",
        type=int,
        default=10,
        help="Total number of miner passes to run.",
    )
    reports_backlog_p.add_argument(
        "--sample-size",
        type=int,
        default=120,
        help="Atom sample size per miner pass (use 0 for uncapped/all-atoms sampling).",
    )
    reports_backlog_p.add_argument(
        "--coverage-miners",
        type=int,
        default=3,
        help="How many miners use partitioned coverage slices.",
    )
    reports_backlog_p.add_argument(
        "--bagging-miners",
        type=int,
        default=None,
        help="How many miners use weighted bagging (default: miners - coverage_miners).",
    )
    reports_backlog_p.add_argument(
        "--max-tickets-per-miner",
        type=int,
        default=12,
        help="Upper bound requested from each miner output.",
    )
    reports_backlog_p.add_argument(
        "--force",
        action="store_true",
        help="Rerun miners even when cached outputs exist.",
    )
    reports_backlog_p.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Reuse cached miner outputs when available (default).",
    )
    reports_backlog_p.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Disable cache reuse and rerun missing stages.",
    )
    reports_backlog_p.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed for miner sampling.",
    )
    reports_backlog_p.add_argument(
        "--no-merge",
        action="store_true",
        help="Skip merge-judge passes.",
    )
    reports_backlog_p.add_argument(
        "--merge-candidate-threshold",
        type=float,
        default=0.65,
        help=(
            "Minimum overall semantic similarity (in [0,1]) required for merge-candidate pairs. "
            "Default: 0.65."
        ),
    )
    reports_backlog_p.add_argument(
        "--merge-keep-anchor-pairs",
        action="store_true",
        help=(
            "Keep merge-candidate pairs based on anchor overlap (anchor_jaccard > 0) even when "
            "below the overall similarity threshold. Default: disabled."
        ),
    )
    reports_backlog_p.add_argument(
        "--orphan-pass",
        type=int,
        default=1,
        help="Number of additional miner passes for uncovered high-severity atoms.",
    )
    reports_backlog_p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run offline without agents: emit deterministic stages 1-2, a blocked stage-3 "
            "research proof, and no stage 4-6 results."
        ),
    )
    reports_backlog_p.add_argument(
        "--shadow",
        action="store_true",
        help=(
            "Run the complete six-stage pipeline without exporting tickets and record "
            "depth invariants for the configured consecutive-cycle export gate. Cannot be "
            "combined with --dry-run."
        ),
    )
    reports_backlog_p.add_argument(
        "--score-shadow",
        action="store_true",
        help=(
            "Score and record an already materialized --shadow run after independent "
            "output adjudication, without invoking or rerunning model stages. Must be "
            "combined with --shadow."
        ),
    )
    reports_backlog_p.add_argument(
        "--operational-shadow",
        action="store_true",
        help=(
            "Materialize a fresh non-exporting operational backlog run without held-out "
            "benchmark labels. Follow with --operational-shadow --score-operational-shadow "
            "after generating current intent and UX artifacts."
        ),
    )
    reports_backlog_p.add_argument(
        "--score-operational-shadow",
        action="store_true",
        help=(
            "Validate and record an already materialized operational run without rerunning "
            "model stages. This never earns or extends release qualification."
        ),
    )
    reports_backlog_p.add_argument(
        "--qualification-corpus-manifest",
        type=Path,
        help=(
            "Release-shadow-only path to the sealed external qualification corpus "
            "manifest. Overrides the export-gate config without modifying tracked files."
        ),
    )
    reports_backlog_p.add_argument(
        "--qualification-manifest-sha256",
        help=(
            "Release-shadow pre-run byte digest for held-out labels. When a sealed "
            "qualification input bundle is used, phase one receives only this digest; "
            "--score-shadow supplies and verifies the actual manifest bytes."
        ),
    )
    reports_backlog_p.add_argument(
        "--qualification-output-adjudication",
        type=Path,
        help=(
            "Release-shadow-only path to independent post-run output adjudication. "
            "The same path must be supplied to phase-one and --score-shadow."
        ),
    )
    reports_backlog_p.add_argument(
        "--no-actionable-evidence-receipt",
        type=Path,
        help=(
            "Release-shadow-only path to an independent no-actionable-evidence receipt "
            "for an exhaustion qualification."
        ),
    )
    reports_backlog_p.add_argument(
        "--qualification-input-bundle",
        type=Path,
        help=(
            "Content-addressed, model-free QualificationInputBundle produced by "
            "`reports qualification-prepare`. Required for sealed release qualification."
        ),
    )
    reports_backlog_p.add_argument(
        "--stage-runs-dir",
        type=Path,
        help=(
            "Isolated append-only destination for model/research runs. With a sealed "
            "qualification bundle this must differ from the frozen --runs-dir evidence root."
        ),
    )
    reports_backlog_p.add_argument(
        "--qualification-cycle-root",
        type=Path,
        help=(
            "Unique write root for one fresh release cycle. Prevents case-registry and "
            "stage-artifact carryover from another qualification cycle."
        ),
    )
    reports_backlog_p.add_argument(
        "--shadow-state",
        type=Path,
        help=(
            "Shared release-state JSON path used to aggregate independently isolated cycles. "
            "Defaults beside the backlog only for legacy, non-bundled shadows."
        ),
    )
    reports_backlog_p.add_argument(
        "--labelers",
        type=int,
        default=3,
        help=(
            "Run N labeler passes per ticket to classify change surface "
            "(default: 3; use 0 to disable). Labeling requires an agent CLI unless "
            "cached outputs exist."
        ),
    )
    reports_backlog_p.add_argument(
        "--policy-config",
        type=Path,
        help=(
            "Optional backlog policy YAML path. Defaults to "
            "configs/backlog_policy.yaml when present. "
            "Policy uses only structured fields (no regex, no text mining)."
        ),
    )
    reports_backlog_p.add_argument(
        "--no-policy",
        action="store_true",
        help="Disable applying the backlog policy engine.",
    )
    reports_backlog_p.add_argument(
        "--atom-actions-yaml",
        type=Path,
        help=(
            "Atom lifecycle ledger YAML path (defaults to configs/backlog_atom_actions.yaml). "
            "Backlog updates atom status to `new` or `ticketed` each run."
        ),
    )
    reports_backlog_p.add_argument(
        "--carryover-actioned-only",
        action="store_true",
        help=(
            "Reset atom carryover so only `actioned` atoms remain excluded. "
            "This demotes `ticketed`/`queued` atoms back to `new` before filtering, allowing "
            "a backlog regeneration pass without losing actioned history. "
            "Cannot be combined with --exclude-atom-status."
        ),
    )
    reports_backlog_p.add_argument(
        "--exclude-atom-status",
        action="append",
        choices=sorted(_ATOM_STATUS_ORDER.keys()),
        default=None,
        help=(
            "Atom statuses to exclude from backlog mining (repeatable). "
            "Default: ticketed + queued + actioned. "
            "For actioned-only carryover, prefer --carryover-actioned-only."
        ),
    )
    reports_backlog_p.add_argument(
        "--skip-plan-folder-sync",
        action="store_true",
        help=(
            "Skip syncing atom statuses from `.agents/plans/*` folder locations before filtering. "
            "Default behavior infers `queued`/`actioned` from ticket file locations (including "
            "demoting atoms referenced by `.agents/plans/0.2 - discarded/**` or "
            "`.agents/plans/_dequeued/**` back to `new`)."
        ),
    )

    reports_qualification_prepare_p = reports_sub.add_parser(
        "qualification-prepare",
        help=(
            "Prepare a model-free, content-addressed release-qualification input bundle "
            "from frozen evidence and copied lifecycle state."
        ),
    )
    reports_qualification_prepare_p.add_argument("--repo-root", type=Path, required=True)
    reports_qualification_prepare_p.add_argument("--repo-input", type=Path, required=True)
    reports_qualification_prepare_p.add_argument("--research-ref", required=True)
    reports_qualification_prepare_p.add_argument("--source-runs-dir", type=Path, required=True)
    reports_qualification_prepare_p.add_argument("--atom-actions-yaml", type=Path, required=True)
    reports_qualification_prepare_p.add_argument(
        "--case-registry-seed",
        type=Path,
        required=True,
    )
    reports_qualification_prepare_p.add_argument("--out-root", type=Path, required=True)
    reports_qualification_prepare_p.add_argument("--work-dir", type=Path, required=True)
    reports_qualification_prepare_p.add_argument("--target")
    reports_qualification_prepare_p.add_argument(
        "--breadth-profile",
        choices=list(_BREADTH_PROFILE_CHOICES),
        default=_BREADTH_PROFILE_EXTERNAL,
    )
    reports_qualification_prepare_p.add_argument(
        "--protected-path",
        type=Path,
        action="append",
        default=[],
        help=(
            "Additional file or directory whose exact bytes must remain unchanged "
            "through preparation, model execution, correction, and scoring."
        ),
    )

    reports_qualification_template_p = reports_sub.add_parser(
        "qualification-adjudication-template",
        help=(
            "Materialize the scorer's exact accepted-output corpus for independent "
            "post-run semantic adjudication."
        ),
    )
    reports_qualification_template_p.add_argument("--backlog-json", type=Path, required=True)
    reports_qualification_template_p.add_argument(
        "--qualification-corpus-manifest",
        type=Path,
        required=True,
    )
    reports_qualification_template_p.add_argument("--out-json", type=Path, required=True)

    reports_qualification_finalize_p = reports_sub.add_parser(
        "qualification-adjudication-finalize",
        help=(
            "Validate independent decisions against an exact adjudication template and "
            "write the hash-bound phase-two output contract."
        ),
    )
    reports_qualification_finalize_p.add_argument("--template", type=Path, required=True)
    reports_qualification_finalize_p.add_argument("--decisions", type=Path, required=True)
    reports_qualification_finalize_p.add_argument("--out-json", type=Path, required=True)
    reports_qualification_finalize_p.add_argument("--adjudicator", required=True)
    reports_qualification_finalize_p.add_argument("--method", required=True)

    reports_intent_snapshot_p = reports_sub.add_parser(
        "intent-snapshot",
        help=(
            "Generate a repo intent snapshot artifact (command surface + docs index; "
            "optional LLM summary)."
        ),
    )
    reports_intent_snapshot_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (controls output directory scope).",
    )
    reports_intent_snapshot_p.add_argument(
        "--repo-input",
        help="Optional repo_input label used for output naming (path or git URL).",
    )
    reports_intent_snapshot_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_intent_snapshot_p.add_argument(
        "--out-json",
        type=Path,
        help=(
            "Output intent snapshot JSON path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_intent_snapshot_p.add_argument(
        "--out-md",
        type=Path,
        help="Output markdown summary path (defaults next to --out-json with .md extension).",
    )
    reports_intent_snapshot_p.add_argument(
        "--repo-intent-md",
        type=Path,
        help="Path to human-owned intent doc (defaults to configs/repo_intent.md).",
    )
    reports_intent_snapshot_p.add_argument(
        "--readme-md",
        type=Path,
        help="Path to README (defaults to README.md at repo root).",
    )
    reports_intent_snapshot_p.add_argument(
        "--docs-dir",
        type=Path,
        help="Docs directory to index (defaults to repo_root/docs when present).",
    )
    reports_intent_snapshot_p.add_argument(
        "--max-readme-bytes",
        type=int,
        default=40_000,
        help="Maximum bytes to embed from README in the snapshot (excerpt).",
    )
    reports_intent_snapshot_p.add_argument(
        "--max-doc-bytes",
        type=int,
        default=8_000,
        help="Maximum bytes to read from each docs file when extracting headings.",
    )
    reports_intent_snapshot_p.add_argument(
        "--with-summary",
        action="store_true",
        help=(
            "Run an optional cached LLM summary pass using "
            "configs/backlog_prompts/intent_snapshot.md."
        ),
    )
    reports_intent_snapshot_p.add_argument(
        "--prompts-dir",
        type=Path,
        help="Optional prompt template directory (defaults to configs/backlog_prompts).",
    )
    reports_intent_snapshot_p.add_argument(
        "--agent",
        choices=["claude", "codex", "gemini"],
        default="claude",
        help="Agent CLI used for the optional summary pass (only when --with-summary is set).",
    )
    reports_intent_snapshot_p.add_argument(
        "--model",
        help="Optional model override for the optional summary pass.",
    )
    reports_intent_snapshot_p.add_argument(
        "--force",
        action="store_true",
        help="Rerun summary generation even when a cached output exists for the same prompt hash.",
    )
    reports_intent_snapshot_p.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Reuse cached summary outputs when available (default).",
    )
    reports_intent_snapshot_p.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Disable cache reuse for the optional summary pass.",
    )
    reports_intent_snapshot_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Write prompt artifacts for the optional summary pass but do not call any agent.",
    )
    reports_intent_snapshot_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_review_ux_p = reports_sub.add_parser(
        "review-ux",
        help=(
            "Run a UX/intent review stage over research_required + high-surface gated tickets "
            "(optional cached LLM pass)."
        ),
    )
    reports_review_ux_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (controls output directory scope).",
    )
    reports_review_ux_p.add_argument(
        "--repo-input",
        help="Optional repo_input label used for output naming (path or git URL).",
    )
    reports_review_ux_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_review_ux_p.add_argument(
        "--backlog-json",
        type=Path,
        help=(
            "Backlog JSON path (defaults to <compiled_dir>/<scope>.backlog.json). "
            "This must contain tickets with `stage` fields."
        ),
    )
    reports_review_ux_p.add_argument(
        "--policy-config",
        type=Path,
        help=(
            "Backlog policy config YAML path (defaults to configs/backlog_policy.yaml when "
            "present). Used to gate high-surface user-visible ready_for_ticket items into UX review."
        ),
    )
    reports_review_ux_p.add_argument(
        "--intent-snapshot-json",
        type=Path,
        help="Intent snapshot JSON path (defaults to <compiled_dir>/<scope>.intent_snapshot.json).",
    )
    reports_review_ux_p.add_argument(
        "--allow-missing-intent-snapshot",
        action="store_true",
        help="Allow running without an intent snapshot (recorded loudly in output metadata).",
    )
    reports_review_ux_p.add_argument(
        "--repo-intent-md",
        type=Path,
        help="Path to human-owned intent doc (defaults to configs/repo_intent.md).",
    )
    reports_review_ux_p.add_argument(
        "--out-json",
        type=Path,
        help="Output UX review JSON path (defaults under the compiled directory).",
    )
    reports_review_ux_p.add_argument(
        "--out-md",
        type=Path,
        help="Output UX review markdown path (defaults next to --out-json with .md extension).",
    )
    reports_review_ux_p.add_argument(
        "--prompts-dir",
        type=Path,
        help="Optional prompt template directory (defaults to configs/backlog_prompts).",
    )
    reports_review_ux_p.add_argument(
        "--breadth-profile",
        choices=list(_BREADTH_PROFILE_CHOICES),
        default=_BREADTH_PROFILE_EXTERNAL,
        help=(
            "Breadth interpretation profile. "
            "Defaults to external_generalization; use internal_maintenance for same-repo "
            "self-learning runs."
        ),
    )
    reports_review_ux_p.add_argument(
        "--agent",
        choices=["claude", "codex", "gemini"],
        default="claude",
        help="Agent CLI used for the optional reviewer pass (skipped if cached or --dry-run).",
    )
    reports_review_ux_p.add_argument(
        "--model",
        help="Optional model override for the optional reviewer pass.",
    )
    reports_review_ux_p.add_argument(
        "--force",
        action="store_true",
        help="Rerun reviewer generation even when a cached output exists for the same prompt hash.",
    )
    reports_review_ux_p.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Reuse cached reviewer outputs when available (default).",
    )
    reports_review_ux_p.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Disable cache reuse for the reviewer pass.",
    )
    reports_review_ux_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Write reviewer prompt artifacts but do not call any agent.",
    )
    reports_review_ux_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_sync_atom_actions_p = reports_sub.add_parser(
        "sync-atom-actions",
        help="Reconcile configs/backlog_atom_actions.yaml from .agents/plans state.",
    )
    reports_sync_atom_actions_p.add_argument(
        "--owner-root",
        type=Path,
        action="append",
        default=[],
        help="Repository root containing .agents/plans (repeatable; default: current directory).",
    )
    reports_sync_atom_actions_p.add_argument(
        "--atom-actions-yaml",
        type=Path,
        help="Atom lifecycle ledger YAML path (defaults to configs/backlog_atom_actions.yaml).",
    )
    reports_sync_atom_actions_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report reconciliation results without writing the atom ledger.",
    )
    reports_sync_atom_actions_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_export_tickets_p = reports_sub.add_parser(
        "export-tickets",
        help=(
            "Write staged ticket export artifacts and synchronize configured local plan files "
            "(with stage gates + action ledger)."
        ),
    )
    reports_export_tickets_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (controls output directory scope).",
    )
    reports_export_tickets_p.add_argument(
        "--repo-input",
        help="Optional repo_input label used for output naming (path or git URL).",
    )
    reports_export_tickets_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_export_tickets_p.add_argument(
        "--backlog-json",
        type=Path,
        help="Backlog JSON path (defaults to <compiled_dir>/<scope>.backlog.json).",
    )
    reports_export_tickets_p.add_argument(
        "--actions-yaml",
        type=Path,
        help="Action ledger YAML path (defaults to configs/backlog_actions.yaml).",
    )
    reports_export_tickets_p.add_argument(
        "--atom-actions-yaml",
        type=Path,
        help=(
            "Atom lifecycle ledger YAML path (defaults to configs/backlog_atom_actions.yaml). "
            "Export updates referenced atoms to `queued`."
        ),
    )
    reports_export_tickets_p.add_argument(
        "--policy-config",
        type=Path,
        help=(
            "Backlog policy config YAML path (defaults to configs/backlog_policy.yaml when "
            "present). Used to gate high-surface user-visible changes to research/design export."
        ),
    )
    reports_export_tickets_p.add_argument(
        "--stage",
        action="append",
        default=[],
        help=(
            "Stage filter (repeatable). Defaults to exporting `triage`, "
            "`ready_for_ticket`, and `research_required` when omitted."
        ),
    )
    reports_export_tickets_p.add_argument(
        "--min-severity",
        choices=["low", "medium", "high", "blocker"],
        default="low",
        help="Minimum severity to export (default: low).",
    )
    reports_export_tickets_p.add_argument(
        "--include-actioned",
        action="store_true",
        help="Include tickets already present in the action ledger (default: skip).",
    )
    reports_export_tickets_p.add_argument(
        "--include-discarded",
        action="store_true",
        help="Include tickets previously discarded from the action ledger (default: skip).",
    )
    reports_export_tickets_p.add_argument(
        "--skip-plan-folder-dedupe",
        action="store_true",
        help=(
            "Skip de-duplicating exports by scanning existing `.agents/plans/*` ticket files for "
            "matching fingerprints (default: skip duplicates)."
        ),
    )
    reports_export_tickets_p.add_argument(
        "--out-json",
        type=Path,
        help="Output export JSON path (defaults under the compiled directory).",
    )
    reports_export_tickets_p.add_argument(
        "--out-md",
        type=Path,
        help="Output export markdown path (defaults next to --out-json with .md extension).",
    )
    reports_export_tickets_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    triage_prs_p = sub.add_parser(
        "triage-prs",
        help="Cluster existing pull requests from a JSON input artifact.",
    )
    triage_prs_p.add_argument(
        "--in",
        dest="input_json",
        type=Path,
        required=True,
        help="Path to PR JSON input (list or object containing pullRequests).",
    )
    triage_prs_p.add_argument(
        "--out-json",
        type=Path,
        help="Output JSON path (default: <input>.triage_prs.json).",
    )
    triage_prs_p.add_argument(
        "--out-md",
        type=Path,
        help="Output markdown path (default: <input>.triage_prs.md).",
    )
    triage_prs_p.add_argument(
        "--title-threshold",
        type=float,
        default=0.55,
        help="Title token Jaccard threshold for similarity edges.",
    )

    triage_backlog_p = sub.add_parser(
        "triage-backlog",
        help="Cluster issue-like backlog items by dedupe + functional theme similarity.",
    )
    triage_backlog_p.add_argument(
        "--in",
        dest="input_json",
        type=Path,
        required=True,
        help="Path to issue JSON input (list, or object with a `tickets` list).",
    )
    triage_backlog_p.add_argument(
        "--group-key",
        type=str,
        help="Optional field name used to compute cross-group coverage (defaults to `package`).",
    )
    triage_backlog_p.add_argument(
        "--out-json",
        type=Path,
        help="Output JSON path (default: <input>.triage_backlog.json).",
    )
    triage_backlog_p.add_argument(
        "--out-md",
        type=Path,
        help="Output markdown path (default: <input>.triage_backlog.md).",
    )
    triage_backlog_p.add_argument(
        "--out-xlsx",
        type=Path,
        help="Optional XLSX output path.",
    )
    triage_backlog_p.add_argument(
        "--dedupe-overall-threshold",
        type=float,
        default=0.90,
        help="Overall similarity threshold used for strict dedupe clustering.",
    )
    triage_backlog_p.add_argument(
        "--theme-overall-threshold",
        type=float,
        default=0.78,
        help="Overall similarity threshold used for theme clustering edges.",
    )
    triage_backlog_p.add_argument(
        "--theme-k",
        type=int,
        default=10,
        help="Top-K neighbor count per item in the theme graph.",
    )
    triage_backlog_p.add_argument(
        "--theme-representative-threshold",
        type=float,
        default=0.75,
        help="Minimum similarity to theme representative during refinement.",
    )

    triage_atoms_p = sub.add_parser(
        "triage-atoms",
        help="Cluster backlog atoms by text and link to tickets + implementation runs.",
    )
    triage_atoms_p.add_argument(
        "--in",
        dest="atoms_jsonl",
        type=Path,
        required=True,
        help="Path to backlog atoms JSONL (e.g. *.backlog.atoms.jsonl).",
    )
    triage_atoms_p.add_argument(
        "--backlog-json",
        type=Path,
        help="Optional backlog JSON path (defaults next to --in when inferred).",
    )
    triage_atoms_p.add_argument(
        "--plans-root",
        type=Path,
        help="Repo root containing .agents/plans (default: --repo-root).",
    )
    triage_atoms_p.add_argument(
        "--implementation-root",
        type=Path,
        help=(
            "Implementation run root containing ticket_ref.json (e.g. runs/usertest_implement/usertest). "
            "If omitted, inferred from backlog.json input.runs_dir + input.target when available."
        ),
    )
    triage_atoms_p.add_argument(
        "--out-json",
        type=Path,
        help="Output JSON path (defaults next to --in).",
    )
    triage_atoms_p.add_argument(
        "--out-md",
        type=Path,
        help="Output markdown path (defaults next to --in).",
    )
    triage_atoms_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )
    triage_atoms_p.add_argument(
        "--embedder",
        choices=["openai"],
        default="openai",
        help=(
            "Embedding backend for clustering (default: openai). "
            "HashingEmbedder is test-only (basic functionality testing only) and must never be used for real triage clustering; "
            "it is intentionally not exposed here."
        ),
    )
    triage_atoms_p.add_argument(
        "--text-normalization",
        choices=["raw", "smart"],
        default="smart",
        help=(
            "Atom text normalization mode before clustering (default: smart). "
            "smart strips the generic 'Command failed: ... command=' wrapper for command_failure atoms."
        ),
    )
    triage_atoms_p.add_argument(
        "--exclude-source",
        action="append",
        default=[],
        help=(
            "Exclude atoms with this `source` value before clustering (repeatable). "
            "Useful for removing noisy sources like `agent_last_message_artifact`."
        ),
    )
    triage_atoms_p.add_argument(
        "--overall-threshold",
        type=float,
        default=0.82,
        help="Overall similarity threshold for clustering edges (default: 0.82).",
    )
    triage_atoms_p.add_argument(
        "--k",
        type=int,
        default=10,
        help="Top-K neighbor count per item in the similarity graph (default: 10).",
    )
    triage_atoms_p.add_argument(
        "--representative-threshold",
        type=float,
        default=0.75,
        help="Minimum similarity to cluster representative during refinement (default: 0.75).",
    )
    triage_atoms_p.add_argument(
        "--min-cluster-size",
        type=int,
        default=2,
        help="Minimum cluster size to emit (default: 2).",
    )

    return parser


__all__ = ["build_parser"]
