from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _format_path(path: Path | None) -> str:
    if path is None:
        return "<none>"
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke_import_guard",
        description=(
            "Detect when `usertest` imports from outside the current workspace "
            "(commonly due to a conflicting global editable install)."
        ),
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        help="Repository root to validate imports against.",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()

    print("==> Import-origin guard (usertest)")
    print(f"    repo_root: {repo_root}")

    try:
        import usertest  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print("ERROR: failed to import `usertest`.", file=sys.stderr)
        print(f"    repo_root: {repo_root}", file=sys.stderr)
        print(f"    error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    module_file_raw = getattr(usertest, "__file__", None)
    module_file: Path | None = None
    if isinstance(module_file_raw, str) and module_file_raw:
        module_file = Path(module_file_raw)

    print(f"    usertest.__file__: {_format_path(module_file)}")

    module_paths = getattr(usertest, "__path__", None)
    if module_paths is not None:
        try:
            module_paths_list = [str(Path(p).resolve()) for p in list(module_paths)]
        except Exception:  # noqa: BLE001
            module_paths_list = [str(p) for p in list(module_paths)]
        if module_paths_list:
            print("    usertest.__path__:")
            for p in module_paths_list:
                print(f"      - {p}")

    if module_file is not None:
        resolved = module_file.resolve()
        ok = _is_under(resolved, repo_root)
        resolved_str = str(resolved)
    else:
        ok = False
        resolved_str = "<none>"

    if ok:
        print("    OK: usertest resolves within this workspace.")
        return 0

    print("ERROR: import shadowing detected.", file=sys.stderr)
    print("    `usertest` resolved outside this workspace checkout.", file=sys.stderr)
    print(f"    expected under: {repo_root}", file=sys.stderr)
    print(f"    resolved at:    {resolved_str}", file=sys.stderr)
    print("", file=sys.stderr)
    print("    Run the prerequisite check first:", file=sys.stderr)
    print("      python tools/scaffold/scaffold.py doctor", file=sys.stderr)
    print("        (Windows wrapper: powershell -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\doctor.ps1)", file=sys.stderr)
    print("        (macOS/Linux wrapper: bash ./scripts/doctor.sh)", file=sys.stderr)
    print("", file=sys.stderr)
    print("    Fix options:", file=sys.stderr)
    print("      - Use PYTHONPATH mode (recommended for smoke scripts):", file=sys.stderr)
    print("          bash ./scripts/smoke.sh --use-pythonpath", file=sys.stderr)
    print(
        "          powershell -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\smoke.ps1 -UsePythonPath",
        file=sys.stderr,
    )
    print("      - Or use an isolated venv and reinstall editables from this repo.", file=sys.stderr)

    # Extra hint when it looks like a global editable path is involved (common on Windows).
    if os.name == "nt" and resolved_str.lower().endswith("\\usertest\\__init__.py"):
        print(
            "      - If you have another checkout installed editable, uninstall it from this interpreter.",
            file=sys.stderr,
        )

    return 3


if __name__ == "__main__":
    raise SystemExit(main())

