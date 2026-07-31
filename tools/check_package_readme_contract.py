from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

STANDALONE_HEADING = "Standalone package checkout (recommended first path)"
MONOREPO_HEADING = "Monorepo contributor workflow"
NORMALIZED_EVENTS_README = Path("packages/normalized_events/README.md")
NORMALIZED_EVENTS_RECIPE_ISSUE = (
    "JSONL helpers are missing a complete fenced Python "
    "import/path/write/read-back recipe."
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(?P<title>.+?)\s*$")
_PYTHON_FENCE_RE = re.compile(
    r"^```python[ \t]*\r?\n(?P<code>.*?)^```[ \t]*$",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)

_REQUIRED_STANDALONE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("`pdm run smoke` command", re.compile(r"\bpdm\s+run\s+smoke\b")),
    ("`pdm run test` command", re.compile(r"\bpdm\s+run\s+test\b")),
    ("`pdm run lint` command", re.compile(r"\bpdm\s+run\s+lint\b")),
)

_BANNED_STANDALONE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("monorepo scaffold command", re.compile(r"tools/scaffold/scaffold\.py\s+run")),
    ("monorepo package install path", re.compile(r"pip\s+install\s+-e\s+packages/")),
    (
        "monorepo `packages/<name>` path reference",
        re.compile(r"(^|[\s'\"`])(?:\./)?packages/[A-Za-z0-9_.-]+", re.MULTILINE),
    ),
)


def _extract_section(text: str, title: str) -> str | None:
    target = title.strip().lower()
    lines = text.splitlines()

    start_index: int | None = None
    heading_level: int | None = None
    for idx, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if match is None:
            continue
        if match.group("title").strip().lower() != target:
            continue
        start_index = idx
        heading_level = len(match.group(1))
        break

    if start_index is None or heading_level is None:
        return None

    body: list[str] = []
    for line in lines[start_index + 1 :]:
        match = _HEADING_RE.match(line.strip())
        if match is not None and len(match.group(1)) <= heading_level:
            break
        body.append(line)

    return "\n".join(body).strip()


def _extract_python_fenced_blocks(text: str) -> list[str]:
    return [match.group("code") for match in _PYTHON_FENCE_RE.finditer(text)]


def _direct_imports(tree: ast.AST, module: str) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != module:
            continue
        for name in node.names:
            if name.asname is None:
                imported.add(name.name)
    return imported


def _assigned_name(node: ast.Assign) -> str | None:
    if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
        return None
    return node.targets[0].id


def _called_name(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    )


def _jsonl_path_bindings(tree: ast.AST) -> set[str]:
    bindings: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not _called_name(node.value, "Path"):
            continue
        binding = _assigned_name(node)
        call = node.value
        if (
            binding is not None
            and len(call.args) == 1
            and not call.keywords
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
            and call.args[0].value.endswith(".jsonl")
        ):
            bindings.add(binding)
    return bindings


def _event_list_bindings(tree: ast.AST) -> set[str]:
    bindings: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.List):
            continue
        binding = _assigned_name(node)
        elements = node.value.elts
        if (
            binding is not None
            and elements
            and all(_called_name(element, "make_event") for element in elements)
        ):
            bindings.add(binding)
    return bindings


def _has_write_call(tree: ast.AST, path_binding: str, events_binding: str) -> bool:
    for node in ast.walk(tree):
        if not _called_name(node, "write_events_jsonl"):
            continue
        if (
            len(node.args) == 2
            and not node.keywords
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == path_binding
            and isinstance(node.args[1], ast.Name)
            and node.args[1].id == events_binding
        ):
            return True
    return False


def _has_read_back_assignment(tree: ast.AST, path_binding: str) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or _assigned_name(node) is None:
            continue
        if not _called_name(node.value, "list") or len(node.value.args) != 1:
            continue
        iterator = node.value.args[0]
        if (
            not node.value.keywords
            and _called_name(iterator, "iter_events_jsonl")
            and len(iterator.args) == 1
            and not iterator.keywords
            and isinstance(iterator.args[0], ast.Name)
            and iterator.args[0].id == path_binding
        ):
            return True
    return False


def _has_complete_jsonl_recipe(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    if "Path" not in _direct_imports(tree, "pathlib"):
        return False
    if not {
        "iter_events_jsonl",
        "make_event",
        "write_events_jsonl",
    }.issubset(_direct_imports(tree, "normalized_events")):
        return False

    for path_binding in _jsonl_path_bindings(tree):
        if not _has_read_back_assignment(tree, path_binding):
            continue
        for events_binding in _event_list_bindings(tree):
            if _has_write_call(tree, path_binding, events_binding):
                return True
    return False


def validate_readme_text(*, readme_path: Path, text: str) -> list[str]:
    issues: list[str] = []

    standalone = _extract_section(text, STANDALONE_HEADING)
    if standalone is None:
        issues.append(
            f"{readme_path}: missing required heading `{STANDALONE_HEADING}`."
        )
    else:
        for label, pattern in _REQUIRED_STANDALONE_PATTERNS:
            if pattern.search(standalone) is None:
                issues.append(f"{readme_path}: standalone section is missing {label}.")
        for label, pattern in _BANNED_STANDALONE_PATTERNS:
            if pattern.search(standalone) is not None:
                issues.append(
                    f"{readme_path}: standalone section contains forbidden {label}."
                )

    monorepo = _extract_section(text, MONOREPO_HEADING)
    if monorepo is None:
        issues.append(f"{readme_path}: missing required heading `{MONOREPO_HEADING}`.")

    if readme_path == NORMALIZED_EVENTS_README and not any(
        _has_complete_jsonl_recipe(block)
        for block in _extract_python_fenced_blocks(text)
    ):
        issues.append(f"{readme_path}: {NORMALIZED_EVENTS_RECIPE_ISSUE}")

    return issues


def discover_package_readmes(repo_root: Path) -> list[Path]:
    packages_dir = repo_root / "packages"
    if not packages_dir.exists():
        return []
    out: list[Path] = []
    for pkg_dir in sorted(packages_dir.iterdir()):
        if not pkg_dir.is_dir():
            continue
        readme = pkg_dir / "README.md"
        pyproject = pkg_dir / "pyproject.toml"
        if readme.exists() and pyproject.exists():
            out.append(readme)
    return out


def _validate_readmes(readmes: list[Path], repo_root: Path) -> list[str]:
    issues: list[str] = []
    for readme in readmes:
        try:
            text = readme.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            issues.append(f"{readme}: failed reading file: {exc}")
            continue
        rel = readme.resolve().relative_to(repo_root.resolve())
        issues.extend(validate_readme_text(readme_path=rel, text=text))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate package README standalone/monorepo command context contract."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Repository root (defaults to current directory).",
    )
    parser.add_argument(
        "--readme",
        action="append",
        type=Path,
        default=[],
        help=(
            "Optional README path to validate (repeatable). "
            "Defaults to packages/*/README.md files with pyproject.toml."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    readmes = (
        [
            ((repo_root / p).resolve() if not p.is_absolute() else p.resolve())
            for p in args.readme
        ]
        if args.readme
        else discover_package_readmes(repo_root)
    )
    if not readmes:
        print("No package READMEs found to validate.")
        return 0

    issues = _validate_readmes(readmes, repo_root)
    if issues:
        print("README contract violations detected:")
        for issue in issues:
            print(f"- {issue}")
        return 1

    print(f"README contract passed for {len(readmes)} package README(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
