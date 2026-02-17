from __future__ import annotations

import subprocess
import sys


def test_agent_adapters_does_not_import_runner_core_or_reporter() -> None:
    code = "\n".join(
        [
            "import agent_adapters, sys",
            "assert 'runner_core' not in sys.modules",
            "assert 'reporter' not in sys.modules",
        ]
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
