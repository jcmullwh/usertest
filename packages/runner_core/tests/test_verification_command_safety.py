from __future__ import annotations

import pytest

from runner_core.verification_commands import verification_command_safety_errors


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests/test_real.py || echo ok",
        "pytest tests/test_real.py; echo ok",
        "python -m pytest tests/test_real.py | Out-Null; exit 0",
        'bash -lc "pytest tests/test_real.py || echo ok"',
        'powershell -Command "pytest tests/test_real.py; exit 0"',
        "pytest tests/test_real.py > verification.txt",
        'pytest "tests/fake\\"; echo ok',
        "pytest C:\\temp\\>forced-success.txt",
        'echo "$(pytest tests/test_real.py)"',
        "echo `pytest tests/test_real.py`",
        'python -c "import subprocess; subprocess.run([\'pytest\']); raise SystemExit(0)"',
        'pdm run python -c "import pytest; pytest.main(); raise SystemExit(0)"',
        'node --eval "require(\'child_process\').execSync(\'npm test\'); process.exit(0)"',
    ],
)
def test_verification_command_safety_rejects_exit_masking_composition(
    command: str,
) -> None:
    assert verification_command_safety_errors(command)


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest tests/test_real.py -q",
        'pytest "tests/test_name_with_&_literal.py"',
        "pytest 'tests/test_literal_$(name).py'",
    ],
)
def test_verification_command_safety_allows_one_literal_invocation(command: str) -> None:
    assert verification_command_safety_errors(command) == []
