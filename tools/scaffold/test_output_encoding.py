from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_scaffold_module():
    scaffold_path = Path(__file__).resolve().with_name("scaffold.py")
    spec = importlib.util.spec_from_file_location("scaffold_cli_module_output_encoding_tests", scaffold_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load scaffold module from {scaffold_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scaffold = _load_scaffold_module()


class _Cp1252LikeStream:
    def __init__(self) -> None:
        self.encoding = "cp1252"
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        text.encode(self.encoding, errors="strict")
        self.parts.append(text)
        return len(text)

    def flush(self) -> None:
        return


def test_emit_captured_process_output_backslashescapes_unencodable_text() -> None:
    fake_stdout = _Cp1252LikeStream()
    fake_stderr = _Cp1252LikeStream()
    scaffold.sys.stdout = fake_stdout
    scaffold.sys.stderr = fake_stderr

    cp = scaffold.subprocess.CompletedProcess(
        args=["demo"],
        returncode=0,
        stdout="out \ufffd",
        stderr="err \ufffd",
    )

    scaffold._emit_captured_process_output(cp)

    assert fake_stdout.parts == ["out \\ufffd", "\n"]
    assert fake_stderr.parts == ["err \\ufffd", "\n"]
