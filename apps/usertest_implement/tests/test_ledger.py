from __future__ import annotations

from pathlib import Path

from usertest_implement.ledger import update_ledger_file


def test_update_ledger_file_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    updates = {"last_run_dir": "runs/x", "last_exit_code": 0}

    update_ledger_file(path, fingerprint="deadbeefdeadbeef", updates=updates)
    first = path.read_text(encoding="utf-8")

    update_ledger_file(path, fingerprint="deadbeefdeadbeef", updates=updates)
    second = path.read_text(encoding="utf-8")

    assert first == second

