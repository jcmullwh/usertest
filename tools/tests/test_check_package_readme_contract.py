from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType


def _load_module() -> ModuleType:
    module_path = (
        Path(__file__).resolve().parents[1] / "check_package_readme_contract.py"
    )
    spec = importlib.util.spec_from_file_location(
        "check_package_readme_contract", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RECIPE_BLOCK = """import json
from pathlib import Path

from normalized_events import iter_events_jsonl, make_event, write_events_jsonl


events_path = Path("normalized-events-example.jsonl")
events = [
    make_event(
        "run.started",
        {"run_id": "demo"},
        ts="2026-01-01T00:00:00Z",
    ),
    make_event(
        "run.completed",
        {"run_id": "demo", "status": "success"},
        ts="2026-01-01T00:00:01Z",
    ),
]

write_events_jsonl(events_path, events)
read_back = list(iter_events_jsonl(events_path))
print(json.dumps(read_back, sort_keys=True))
"""


def _normalized_events_readme(recipe: str = _RECIPE_BLOCK) -> str:
    return f"""# normalized_events

## Standalone package checkout (recommended first path)

```bash
pdm run smoke
pdm run test
pdm run lint
```

## JSONL helpers

```python
{recipe.rstrip()}
```

## Monorepo contributor workflow
"""


def _incomplete_recipe_variants(recipe: str) -> list[str]:
    removals = [
        "from pathlib import Path\n",
        (
            "from normalized_events import iter_events_jsonl, make_event, "
            "write_events_jsonl\n"
        ),
        'events_path = Path("normalized-events-example.jsonl")\n',
        "write_events_jsonl(events_path, events)\n",
        "read_back = list(iter_events_jsonl(events_path))\n",
    ]
    variants: list[str] = []
    for removal in removals:
        assert recipe.count(removal) == 1
        variants.append(recipe.replace(removal, "", 1))
    return variants


def test_validate_readme_text_accepts_standalone_and_monorepo_sections() -> None:
    module = _load_module()
    readme = "\n".join(
        [
            "# Example",
            "",
            "## Standalone package checkout (recommended first path)",
            "",
            "```bash",
            "pdm run smoke",
            "pdm run test",
            "pdm run lint",
            "pip install --index-url https://example.test/api/v4/projects/1/packages/pypi/simple pkg",
            "```",
            "",
            "## Monorepo contributor workflow",
            "",
            "```bash",
            "python tools/scaffold/scaffold.py run test --project example",
            "```",
            "",
        ]
    )
    issues = module.validate_readme_text(
        readme_path=Path("packages/example/README.md"), text=readme
    )
    assert issues == []


def test_validate_readme_text_rejects_missing_required_headings() -> None:
    module = _load_module()
    readme = "# Example\n\n## Development\n\nRun commands.\n"
    issues = module.validate_readme_text(
        readme_path=Path("packages/example/README.md"), text=readme
    )
    assert any("Standalone package checkout" in issue for issue in issues)
    assert any("Monorepo contributor workflow" in issue for issue in issues)


def test_validate_readme_text_rejects_monorepo_commands_in_standalone_section() -> None:
    module = _load_module()
    readme = "\n".join(
        [
            "# Example",
            "",
            "## Standalone package checkout (recommended first path)",
            "",
            "```bash",
            "pdm run smoke",
            "pdm run test",
            "pdm run lint",
            "python tools/scaffold/scaffold.py run test --project example",
            "```",
            "",
            "## Monorepo contributor workflow",
            "Use scaffold here.",
            "",
        ]
    )
    issues = module.validate_readme_text(
        readme_path=Path("packages/example/README.md"), text=readme
    )
    assert any("forbidden monorepo scaffold command" in issue for issue in issues)


def test_normalized_events_readme_accepts_complete_jsonl_recipe() -> None:
    module = _load_module()
    issues = module.validate_readme_text(
        readme_path=module.NORMALIZED_EVENTS_README,
        text=_normalized_events_readme(),
    )
    assert issues == []


def test_normalized_events_readme_rejects_each_incomplete_jsonl_recipe() -> None:
    module = _load_module()
    expected_issue = (
        f"{module.NORMALIZED_EVENTS_README}: {module.NORMALIZED_EVENTS_RECIPE_ISSUE}"
    )
    variants = _incomplete_recipe_variants(_RECIPE_BLOCK)

    for recipe in variants:
        issues = module.validate_readme_text(
            readme_path=module.NORMALIZED_EVENTS_README,
            text=_normalized_events_readme(recipe),
        )
        assert issues == [expected_issue]


def test_normalized_events_readme_jsonl_recipe_executes(tmp_path: Path) -> None:
    module = _load_module()
    repo_root = Path(__file__).resolve().parents[2]
    readme_path = repo_root / module.NORMALIZED_EVENTS_README
    readme = readme_path.read_text(encoding="utf-8")
    issues = module.validate_readme_text(
        readme_path=module.NORMALIZED_EVENTS_README,
        text=readme,
    )
    assert issues == []

    section = module._extract_section(readme, "Write and read a JSONL file")
    assert section is not None
    recipe_blocks = module._extract_python_fenced_blocks(section)
    assert len(recipe_blocks) == 1
    recipe = recipe_blocks[0]

    expected_issue = (
        f"{module.NORMALIZED_EVENTS_README}: {module.NORMALIZED_EVENTS_RECIPE_ISSUE}"
    )
    incomplete_variants_rejected = 0
    for incomplete_recipe in _incomplete_recipe_variants(recipe):
        variant_readme = readme.replace(recipe, incomplete_recipe, 1)
        variant_issues = module.validate_readme_text(
            readme_path=module.NORMALIZED_EVENTS_README,
            text=variant_readme,
        )
        if variant_issues == [expected_issue]:
            incomplete_variants_rejected += 1
    assert incomplete_variants_rejected == 5

    artifact_name = "normalized-events-example.jsonl"
    repo_artifact = repo_root / artifact_name
    assert not repo_artifact.exists()
    env = os.environ.copy()
    package_src = repo_root / "packages" / "normalized_events" / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(package_src), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    completed = subprocess.run(
        [sys.executable, "-c", recipe],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    artifact_path = tmp_path / artifact_name
    assert artifact_path.exists()
    assert not repo_artifact.exists()
    raw_events = [
        json.loads(line)
        for line in artifact_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(raw_events) == 2
    assert all(set(event) == {"ts", "type", "data"} for event in raw_events)
    read_back = json.loads(completed.stdout)
    assert read_back == raw_events
    assert [event["type"] for event in raw_events] == [
        "run.started",
        "run.completed",
    ]
    assert raw_events[1]["data"]["status"] == "success"

    print(
        json.dumps(
            {
                "missing_copy_paste_jsonl_recipe": bool(issues),
                "artifact_exists": artifact_path.exists(),
                "incomplete_variants_rejected": incomplete_variants_rejected,
                "raw_event_count": len(raw_events),
                "read_back_equals_written": read_back == raw_events,
                "event_types": [event["type"] for event in raw_events],
            }
        )
    )


def test_discover_package_readmes_finds_only_package_dirs_with_pyproject(
    tmp_path: Path,
) -> None:
    module = _load_module()
    pkg_a = tmp_path / "packages" / "pkg_a"
    pkg_b = tmp_path / "packages" / "pkg_b"
    docs = tmp_path / "packages" / "docs_only"
    pkg_a.mkdir(parents=True)
    pkg_b.mkdir(parents=True)
    docs.mkdir(parents=True)
    (pkg_a / "pyproject.toml").write_text("[project]\nname='a'\n", encoding="utf-8")
    (pkg_a / "README.md").write_text("# a\n", encoding="utf-8")
    (pkg_b / "README.md").write_text("# b\n", encoding="utf-8")
    (docs / "README.md").write_text("# docs\n", encoding="utf-8")

    readmes = module.discover_package_readmes(tmp_path)
    assert readmes == [pkg_a / "README.md"]
