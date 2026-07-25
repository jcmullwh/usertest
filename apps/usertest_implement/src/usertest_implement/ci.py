# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_implement.review_context import _run_gh_json
from usertest_implement.shared import *


def _optional_timeout_seconds(value: Any) -> float | None:
    if value is None:
        return None
    timeout_seconds = float(value)
    if timeout_seconds <= 0:
        return None
    return timeout_seconds


def _ci_timeout_seconds_arg(value: Any) -> float | None:
    return _optional_timeout_seconds(value)


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
    timeout_seconds: float | None,
    required_event: str = "push",
) -> dict[str, Any]:
    """
    Wait for GitHub Actions CI to pass for the current branch HEAD before opening a PR.

    Initial implementation runs wait for the branch ``push`` workflow. PR-backed resumes pass
    ``pull_request`` so the gate follows the merge-authoritative check suite instead of an
    independent same-SHA push run that may have a different matrix or transient result.
    """

    started_utc = _utc_now_z()
    started_monotonic = time.monotonic()
    summary: dict[str, Any] = {
        "schema_version": 1,
        "workflow": workflow,
        "required_event": required_event,
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
        return _run_gh_json(cwd=workspace_dir, argv=argv)

    def _pick_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
        matches = [
            r
            for r in runs
            if isinstance(r, dict)
            and r.get("headSha") == head_sha
            and r.get("event") == required_event
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
        if timeout_seconds is not None and elapsed > timeout_seconds:
            summary["error"] = (
                f"Timed out waiting to find a GitHub Actions run for {workflow} "
                f"(branch={branch}, head_sha={head_sha}, event={required_event})."
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

    # CI commonly takes 10+ minutes. Two-minute completion checks avoid wasteful high-frequency
    # polling while keeping completion latency reasonable.
    poll_interval_seconds = 120.0
    while True:
        elapsed = time.monotonic() - started_monotonic
        if timeout_seconds is not None and elapsed > timeout_seconds:
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
        except Exception as e:  # noqa: BLE001
            summary["error"] = f"Failed to inspect GitHub Actions run {run_id}: {e}"
            summary["finished_at_utc"] = _utc_now_z()
            _write_json(run_dir / "ci_gate.json", summary)
            return summary

        if isinstance(view_raw, dict):
            summary["status"] = view_raw.get("status")
            summary["conclusion"] = view_raw.get("conclusion")
            summary["run_url"] = view_raw.get("url") or summary.get("run_url")
            _write_json(run_dir / "ci_gate.json", summary)

        status = str(summary.get("status") or "").strip().lower()
        conclusion = str(summary.get("conclusion") or "").strip().lower()
        if status == "completed":
            passed = conclusion == "success"
            summary["passed"] = passed
            if not passed:
                summary["error"] = (
                    f"GitHub Actions CI did not pass (run_id={run_id}, "
                    f"conclusion={summary.get('conclusion')!r})."
                )
            summary["finished_at_utc"] = _utc_now_z()
            _write_json(run_dir / "ci_gate.json", summary)
            return summary

        time.sleep(poll_interval_seconds)




__all__ = [name for name in globals() if not name.startswith("__")]
