from __future__ import annotations

from agent_adapters.codex_config import toml_basic_string


def test_toml_basic_string_escapes_quotes_and_backslashes() -> None:
    out = toml_basic_string('a"b\\c')
    assert out.startswith('"') and out.endswith('"')
    assert '\\"' in out
    assert "\\\\" in out


def test_toml_basic_string_escapes_newlines_and_tabs() -> None:
    out = toml_basic_string("line1\n\tline2")
    assert "\\n" in out
    assert "\\t" in out


def test_toml_basic_string_escapes_nul() -> None:
    out = toml_basic_string("a\x00b")
    assert "\\u0000" in out
