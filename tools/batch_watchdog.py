#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _latest_batch_dir(repo_root: Path) -> Path | None:
    root = repo_root / "runs" / "_batch" / "usertest_implement"
    if not root.exists():
        return None
    dirs = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name)
    return dirs[-1] if dirs else None


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _latest_activity_epoch(paths: list[Path], *, batch_dir: Path | None) -> float | None:
    candidates: list[float] = []
    for path in paths:
        if path.exists():
            candidates.append(path.stat().st_mtime)
    if batch_dir is not None and batch_dir.exists():
        for path in batch_dir.rglob("*"):
            try:
                if path.is_file():
                    candidates.append(path.stat().st_mtime)
            except OSError:
                continue
    return max(candidates) if candidates else None


def _append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{_utc_now_z()} {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="batch_watchdog")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--batch-dir", type=Path, default=None)
    parser.add_argument(
        "--launcher-stdout",
        type=Path,
        default=Path("runs/_batch/usertest_implement/_launcher/batch.stdout.txt"),
    )
    parser.add_argument(
        "--launcher-stderr",
        type=Path,
        default=Path("runs/_batch/usertest_implement/_launcher/batch.stderr.txt"),
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=Path("runs/_batch/usertest_implement/_watchdog/watchdog.log"),
    )
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--stale-seconds", type=float, default=600.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    launcher_stdout = (
        args.launcher_stdout
        if args.launcher_stdout.is_absolute()
        else (repo_root / args.launcher_stdout).resolve()
    )
    launcher_stderr = (
        args.launcher_stderr
        if args.launcher_stderr.is_absolute()
        else (repo_root / args.launcher_stderr).resolve()
    )
    log_path = args.log_path if args.log_path.is_absolute() else (repo_root / args.log_path).resolve()
    batch_dir = (
        args.batch_dir.resolve()
        if isinstance(args.batch_dir, Path)
        else _latest_batch_dir(repo_root)
    )
    if batch_dir is None:
        _append_log(log_path, "no batch directory found")
        return 1

    last_status: tuple[str | None, str | None] | None = None
    last_alert_at = 0.0
    _append_log(log_path, f"watching batch_dir={batch_dir}")
    while True:
        batch_state = _read_json(batch_dir / "batch_state.json") or {}
        status = str(batch_state.get("status") or "").strip().lower() or None
        phase = str(batch_state.get("phase") or "").strip() or None
        marker = (status, phase)
        if marker != last_status:
            _append_log(
                log_path,
                f"status={status or 'unknown'} phase={phase or '-'} "
                f"in_flight={len(batch_state.get('in_flight', [])) if isinstance(batch_state.get('in_flight'), list) else 0} "
                f"completed={len(batch_state.get('completed', [])) if isinstance(batch_state.get('completed'), list) else 0} "
                f"failed={len(batch_state.get('failed', [])) if isinstance(batch_state.get('failed'), list) else 0}",
            )
            last_status = marker

        activity_epoch = _latest_activity_epoch(
            [launcher_stdout, launcher_stderr, batch_dir / "batch_state.json"],
            batch_dir=batch_dir,
        )
        now = time.time()
        if activity_epoch is not None and status == "running":
            stale_seconds = now - activity_epoch
            if stale_seconds >= float(args.stale_seconds) and (now - last_alert_at) >= float(
                args.poll_seconds
            ):
                _append_log(
                    log_path,
                    f"ALERT stale_activity batch_dir={batch_dir} stale_seconds={stale_seconds:.1f}",
                )
                last_alert_at = now

        if status in {"completed", "failed", "blocked"}:
            _append_log(log_path, f"terminal_status={status} batch_dir={batch_dir}")
            return 0 if status == "completed" else 2

        time.sleep(max(1.0, float(args.poll_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
