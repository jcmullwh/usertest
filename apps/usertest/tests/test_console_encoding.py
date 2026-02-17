from __future__ import annotations

import io

import pytest

import usertest.cli as cli


def _make_cp1252_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="")
    return raw, stream


def test_enable_console_backslashreplace_prevents_unicodeencodeerror() -> None:
    _raw, strict_stream = _make_cp1252_stream()
    with pytest.raises(UnicodeEncodeError):
        strict_stream.write("\u2192")
        strict_stream.flush()

    raw, stream = _make_cp1252_stream()
    cli._enable_console_backslashreplace(stream)
    stream.write("\u2192")
    stream.flush()

    assert raw.getvalue().decode("cp1252") == "\\u2192"


def test_configure_console_output_makes_stdout_and_stderr_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_raw, stdout_stream = _make_cp1252_stream()
    stderr_raw, stderr_stream = _make_cp1252_stream()
    monkeypatch.setattr(cli.sys, "stdout", stdout_stream)
    monkeypatch.setattr(cli.sys, "stderr", stderr_stream)

    cli._configure_console_output()
    print("stdout arrow: \u2192", file=stdout_stream)
    print("stderr arrow: \u2192", file=stderr_stream)
    stdout_stream.flush()
    stderr_stream.flush()

    assert "\\u2192" in stdout_raw.getvalue().decode("cp1252")
    assert "\\u2192" in stderr_raw.getvalue().decode("cp1252")
