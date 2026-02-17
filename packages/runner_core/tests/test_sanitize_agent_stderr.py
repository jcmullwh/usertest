from __future__ import annotations

from pathlib import Path

from runner_core.runner import _sanitize_agent_stderr_file


def test_sanitize_agent_stderr_file_strips_gemini_credential_line(tmp_path: Path) -> None:
    path = tmp_path / "agent_stderr.txt"
    path.write_text("Loaded cached credentials.\nSomething else.\n", encoding="utf-8")

    _sanitize_agent_stderr_file(agent="gemini", path=path)

    text = path.read_text(encoding="utf-8")
    assert "Loaded cached credentials." not in text
    assert "Something else." in text


def test_sanitize_agent_stderr_file_dedupes_codex_personality_warning(tmp_path: Path) -> None:
    path = tmp_path / "agent_stderr.txt"
    warning = (
        "2026-02-11T07:26:19.697569Z  WARN codex_protocol::openai_models: "
        "Model personality requested but model_messages is missing, falling back to base "
        "instructions. model=gpt-5.2 personality=pragmatic"
    )
    path.write_text(
        "\n".join(
            [
                "before",
                warning,
                "after",
                "",
            ]
        ),
        encoding="utf-8",
    )

    _sanitize_agent_stderr_file(agent="codex", path=path)

    text = path.read_text(encoding="utf-8")
    assert "Model personality requested but model_messages is missing" in text
    assert "before" in text
    assert "after" in text
