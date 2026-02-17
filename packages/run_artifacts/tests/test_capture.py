from __future__ import annotations

from pathlib import Path

from run_artifacts.capture import TextCapturePolicy, capture_text_artifact


def test_capture_text_artifact_missing_file_returns_metadata_only(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.txt"
    result = capture_text_artifact(missing_path, policy=TextCapturePolicy(), root=tmp_path)
    assert result.artifact.path == "missing.txt"
    assert result.artifact.exists is False
    assert result.excerpt is None
    assert result.error is None


def test_capture_text_artifact_large_file_keeps_head_tail_with_truncation(tmp_path: Path) -> None:
    path = tmp_path / "agent_stderr.txt"
    payload = "EPIPE writing to socket\n" + ("x" * 200 + "\n") * 6000
    path.write_text(payload, encoding="utf-8")

    policy = TextCapturePolicy(max_excerpt_bytes=8_000, head_bytes=4_000, tail_bytes=4_000)
    result = capture_text_artifact(path, policy=policy, root=tmp_path)

    assert result.artifact.path == "agent_stderr.txt"
    assert result.artifact.exists is True
    assert result.artifact.size_bytes is not None
    assert result.artifact.sha256 is not None
    assert result.excerpt is not None
    assert result.excerpt.truncated is True
    assert "EPIPE writing to socket" in result.excerpt.head
    assert result.excerpt.tail.strip() != ""


def test_capture_text_artifact_binary_file_reports_error(tmp_path: Path) -> None:
    path = tmp_path / "agent_stderr.txt"
    path.write_bytes(b"\x00\x01\x02binary\x00data")
    result = capture_text_artifact(path, policy=TextCapturePolicy(), root=tmp_path)
    assert result.artifact.exists is True
    assert result.excerpt is None
    assert isinstance(result.error, str)
    assert "binary_artifact_detected" in result.error
