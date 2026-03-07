from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from usertest_implement.ledger import load_ledger, update_ledger_file


def test_update_ledger_file_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    updates = {"last_run_dir": "runs/x", "last_exit_code": 0}

    update_ledger_file(path, fingerprint="deadbeefdeadbeef", updates=updates)
    first = path.read_text(encoding="utf-8")

    update_ledger_file(path, fingerprint="deadbeefdeadbeef", updates=updates)
    second = path.read_text(encoding="utf-8")

    assert first == second


def test_update_ledger_file_preserves_concurrent_updates(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"

    def _worker(i: int) -> None:
        fingerprint = f"{i:016x}"
        update_ledger_file(
            path,
            fingerprint=fingerprint,
            updates={"last_run_dir": f"runs/{i}", "last_exit_code": 0},
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(_worker, range(12)))

    doc = load_ledger(path)
    actions = doc.get("actions")
    assert isinstance(actions, dict)
    assert len(actions) == 12
    for i in range(12):
        fingerprint = f"{i:016x}"
        entry = actions.get(fingerprint)
        assert isinstance(entry, dict)
        assert entry.get("last_run_dir") == f"runs/{i}"
