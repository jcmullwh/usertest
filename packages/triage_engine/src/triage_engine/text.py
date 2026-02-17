from __future__ import annotations

import re
from collections.abc import Iterable

# NOTE:
# - `tokenize` is part of the public API.
# - Keep it dependency-free and deterministic.
# - Favor recall over precision (downstream scoring applies thresholds).

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATOR_RE = re.compile(r"[_\-]+")
_PATH_LIKE_RE = re.compile(
    r"(?:[A-Za-z]:[\\/])?[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+){1,}"
)


def tokenize(text: str) -> set[str]:
    """Tokenize free text into lowercase alphanumeric terms.

    Parameters
    ----------
    text:
        Raw text input.

    Returns
    -------
    set[str]
        Unique normalized token set.
    """

    if not text:
        return set()

    # Split camelCase ("QuickStart" -> "Quick Start").
    normalized = _CAMEL_BOUNDARY_RE.sub(" ", text)

    # Treat common separators as token boundaries.
    normalized = _SEPARATOR_RE.sub(" ", normalized)

    tokens = {token for token in _TOKEN_RE.findall(normalized.lower()) if token}

    # Drop 1-character tokens to avoid noise (e.g., drive letters in paths).
    return {token for token in tokens if len(token) >= 2}


def normalized_title(title: str, *, max_tokens: int = 12) -> str:
    """Build deterministic normalized title phrase for coarse comparisons.

    Parameters
    ----------
    title:
        Original title text.
    max_tokens:
        Maximum sorted tokens to include in the normalized output.

    Returns
    -------
    str
        Space-separated normalized title representation.
    """

    tokens = sorted(tokenize(title))
    return " ".join(tokens[:max_tokens])


def title_jaccard(a: str, b: str) -> float:
    """Compute Jaccard overlap between tokenized titles.

    Parameters
    ----------
    a:
        First title text.
    b:
        Second title text.

    Returns
    -------
    float
        Jaccard similarity in the inclusive range ``[0.0, 1.0]``.
    """

    a_tokens = tokenize(a)
    b_tokens = tokenize(b)
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)
    if union == 0:
        return 0.0
    return overlap / union


def extract_path_anchors_from_chunks(chunks: Iterable[str]) -> set[str]:
    """Extract normalized path-like anchors from text chunks.

    Parameters
    ----------
    chunks:
        Iterable of text chunks that may reference filesystem-like paths.

    Returns
    -------
    set[str]
        Normalized lowercase anchors using forward-slash separators.
    """

    anchors: set[str] = set()
    for chunk in chunks:
        for match in _PATH_LIKE_RE.findall(chunk):
            anchors.add(match.lower().replace("\\", "/"))
    return anchors
