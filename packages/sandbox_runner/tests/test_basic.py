from __future__ import annotations

from pathlib import Path

from sandbox_runner.image_hash import compute_image_hash


def test_compute_image_hash_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("world\n", encoding="utf-8")

    h1 = compute_image_hash(context_dir=tmp_path, dockerfile=tmp_path / "Dockerfile")
    h2 = compute_image_hash(context_dir=tmp_path, dockerfile=tmp_path / "Dockerfile")
    assert h1 == h2


def test_compute_image_hash_changes_on_context_change(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    target = tmp_path / "requirements.txt"
    target.write_text("requests==2.0.0\n", encoding="utf-8")

    h1 = compute_image_hash(context_dir=tmp_path, dockerfile=tmp_path / "Dockerfile")
    target.write_text("requests==2.0.1\n", encoding="utf-8")
    h2 = compute_image_hash(context_dir=tmp_path, dockerfile=tmp_path / "Dockerfile")

    assert h1 != h2
