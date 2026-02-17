from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True)
class TextCapturePolicy:
    """
    Policy controlling lossy text-artifact capture.

    Parameters
    ----------
    max_excerpt_bytes
        Total byte budget for all excerpt content returned by a capture call.
        When an artifact exceeds this budget, capture keeps only a head segment
        and a tail segment and marks the excerpt as truncated.
    head_bytes
        Desired byte budget for the beginning of the artifact.
    tail_bytes
        Desired byte budget for the end of the artifact.
    max_line_count
        Optional per-segment line cap. When set, the cap is applied after UTF-8
        decode with replacement semantics (`errors="replace"`). This cap is
        applied independently to the head and tail segments.
    binary_detection_bytes
        Number of leading bytes to inspect when deciding whether an artifact is
        likely binary. Binary artifacts are not decoded as text and return a
        capture error instead of silent omission.
    """

    max_excerpt_bytes: int = 24_000
    head_bytes: int = 12_000
    tail_bytes: int = 12_000
    max_line_count: int | None = None
    binary_detection_bytes: int = 2_048


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    abs_path: str
    exists: bool
    size_bytes: int | None
    sha256: str | None


@dataclass(frozen=True)
class TextExcerpt:
    head: str
    tail: str
    truncated: bool


@dataclass(frozen=True)
class CaptureResult:
    artifact: ArtifactRef
    excerpt: TextExcerpt | None
    error: str | None


def _safe_relpath(path: Path, root: Path | None) -> str:
    if root is None:
        return str(path).replace("\\", "/")
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except Exception:  # noqa: BLE001
        return str(path).replace("\\", "/")


def _normalize_policy(policy: TextCapturePolicy) -> tuple[int, int, int]:
    max_excerpt_bytes = max(1, int(policy.max_excerpt_bytes))
    head_bytes = max(0, int(policy.head_bytes))
    tail_bytes = max(0, int(policy.tail_bytes))

    if head_bytes + tail_bytes == 0:
        head_bytes = min(max_excerpt_bytes, 1)
        tail_bytes = 0

    if head_bytes + tail_bytes > max_excerpt_bytes:
        head_bytes = min(head_bytes, max_excerpt_bytes)
        tail_bytes = min(tail_bytes, max(0, max_excerpt_bytes - head_bytes))
    return max_excerpt_bytes, head_bytes, tail_bytes


def _apply_line_limit(text: str, *, max_line_count: int | None, from_head: bool) -> str:
    if max_line_count is None:
        return text
    line_cap = int(max_line_count)
    if line_cap <= 0:
        return ""
    lines = text.splitlines(keepends=True)
    if len(lines) <= line_cap:
        return text
    selected = lines[:line_cap] if from_head else lines[-line_cap:]
    return "".join(selected)


def _looks_binary(path: Path, *, sample_bytes: int) -> tuple[bool, str | None]:
    try:
        with path.open("rb") as f:
            sample = f.read(max(1, sample_bytes))
    except OSError as exc:
        return False, f"binary_detection_failed: {exc}"

    if not sample:
        return False, None
    if b"\x00" in sample:
        return True, None

    controls = sum(1 for byte in sample if (byte < 9) or (13 < byte < 32))
    ratio = controls / len(sample)
    return ratio > 0.30, None


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_excerpt(
    path: Path,
    *,
    size_bytes: int,
    policy: TextCapturePolicy,
) -> tuple[TextExcerpt | None, str | None]:
    max_excerpt_bytes, head_bytes, tail_bytes = _normalize_policy(policy)

    try:
        if size_bytes <= max_excerpt_bytes:
            raw = path.read_bytes()
            decoded = raw.decode("utf-8", errors="replace")
            decoded = _apply_line_limit(
                decoded,
                max_line_count=policy.max_line_count,
                from_head=True,
            )
            return TextExcerpt(head=decoded, tail="", truncated=False), None

        head_raw = b""
        tail_raw = b""
        with path.open("rb") as f:
            if head_bytes > 0:
                head_raw = f.read(head_bytes)
            if tail_bytes > 0:
                f.seek(max(size_bytes - tail_bytes, 0))
                tail_raw = f.read(tail_bytes)

        head = _apply_line_limit(
            head_raw.decode("utf-8", errors="replace"),
            max_line_count=policy.max_line_count,
            from_head=True,
        )
        tail = _apply_line_limit(
            tail_raw.decode("utf-8", errors="replace"),
            max_line_count=policy.max_line_count,
            from_head=False,
        )
        return TextExcerpt(head=head, tail=tail, truncated=True), None
    except OSError as exc:
        return None, f"read_failed: {exc}"


def capture_text_artifact(
    path: Path,
    *,
    policy: TextCapturePolicy,
    root: Path | None = None,
) -> CaptureResult:
    """
    Capture a text artifact with loss accounting and no silent drops.

    The function always returns metadata for the requested path. Existing files
    are never omitted silently: failures are reported in `error`, and available
    metadata (size/hash/path) is still returned.
    """

    rel_path = _safe_relpath(path, root)
    abs_path = str(path.resolve())
    exists = path.exists()
    size_bytes: int | None = None
    digest: str | None = None
    errors: list[str] = []

    if exists:
        try:
            size_bytes = int(path.stat().st_size)
        except OSError as exc:
            errors.append(f"stat_failed: {exc}")

        try:
            digest = _sha256_file(path)
        except OSError as exc:
            errors.append(f"hash_failed: {exc}")

    artifact = ArtifactRef(
        path=rel_path,
        abs_path=abs_path,
        exists=exists,
        size_bytes=size_bytes,
        sha256=digest,
    )
    if not exists:
        return CaptureResult(artifact=artifact, excerpt=None, error=None)

    binary, binary_error = _looks_binary(path, sample_bytes=policy.binary_detection_bytes)
    if binary_error:
        errors.append(binary_error)
    if binary:
        errors.append("binary_artifact_detected")
        return CaptureResult(
            artifact=artifact,
            excerpt=None,
            error="; ".join(errors) if errors else None,
        )

    if size_bytes is None:
        return CaptureResult(
            artifact=artifact,
            excerpt=None,
            error="; ".join(errors) if errors else "stat_failed",
        )

    excerpt, excerpt_error = _read_excerpt(path, size_bytes=size_bytes, policy=policy)
    if excerpt_error:
        errors.append(excerpt_error)

    return CaptureResult(
        artifact=artifact,
        excerpt=excerpt,
        error="; ".join(errors) if errors else None,
    )
