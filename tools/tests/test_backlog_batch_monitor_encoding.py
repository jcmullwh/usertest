from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType


def _load_module() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "backlog_batch_monitor.py"
    spec = importlib.util.spec_from_file_location("backlog_batch_monitor", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Cp1252LikeStdout:
    def __init__(self) -> None:
        self.encoding = "cp1252"
        self.errors = "strict"
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        text.encode(self.encoding, errors="strict")
        self.parts.append(text)
        return len(text)

    def flush(self) -> None:
        return


def test_write_stdout_backslashescapes_unencodable_text() -> None:
    mod = _load_module()
    fake_stdout = _Cp1252LikeStdout()
    mod.sys.stdout = fake_stdout

    mod._write_stdout("status \ufffd\n")

    assert fake_stdout.parts == ["status \\ufffd\n"]
