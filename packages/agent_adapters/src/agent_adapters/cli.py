from __future__ import annotations

import argparse
import json
import shutil
from collections.abc import Sequence
from typing import Any

from agent_adapters import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-adapters",
        description="Utilities for validating an installed agent_adapters environment.",
    )
    parser.add_argument("--version", action="store_true", help="Print package version and exit.")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("version", help="Print package version.")

    doctor = subparsers.add_parser("doctor", help="Check common agent CLI binaries on PATH.")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def _doctor_payload() -> dict[str, Any]:
    binaries = {
        "codex": shutil.which("codex"),
        "claude": shutil.which("claude"),
        "gemini": shutil.which("gemini"),
    }
    available = [name for name, resolved in binaries.items() if isinstance(resolved, str)]
    missing = [name for name, resolved in binaries.items() if resolved is None]
    return {
        "agent_adapters_version": __version__,
        "binaries": binaries,
        "available": available,
        "missing": missing,
    }


def _print_doctor(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print(f"agent-adapters version: {payload['agent_adapters_version']}")
    binaries = payload.get("binaries")
    if not isinstance(binaries, dict):
        return
    for name in ("codex", "claude", "gemini"):
        value = binaries.get(name)
        rendered = value if isinstance(value, str) else "<missing>"
        print(f"{name}: {rendered}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.version or args.command == "version":
        print(__version__)
        return 0

    if args.command == "doctor":
        _print_doctor(_doctor_payload(), as_json=bool(args.json))
        return 0

    parser.print_help()
    return 0
