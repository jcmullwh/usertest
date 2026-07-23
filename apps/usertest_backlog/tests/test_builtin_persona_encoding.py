from __future__ import annotations

from pathlib import Path

BUILTIN_PERSONAS = (
    Path(__file__).resolve().parents[3] / "configs" / "personas" / "builtin"
)
MOJIBAKE_SENTINELS = (
    "\ufffd",  # Unicode replacement character from a lossy decode.
    "\u00e2\u20ac",  # Corrupted curly quotes, dashes, and ellipses.
    "\u00f0\u0178",  # Corrupted four-byte Unicode, commonly emoji.
    "\u00c2\u00a0",  # Corrupted non-breaking space.
    "\u00ef\u00bb\u00bf",  # UTF-8 BOM bytes interpreted as text.
    "\ufeff",  # Actual UTF-8 BOM decoded as a code point.
)
MOJIBAKE_UTF8_LEADERS = ("\u00c2", "\u00c3")
WINDOWS_1252_CONTINUATION_CHARACTERS = frozenset(
    character
    for byte_value in range(0x80, 0xC0)
    for character in bytes([byte_value]).decode("cp1252", errors="ignore")
)


def _mojibake_utf8_pairs(text: str) -> list[str]:
    return [
        leader + continuation
        for leader in MOJIBAKE_UTF8_LEADERS
        for continuation in WINDOWS_1252_CONTINUATION_CHARACTERS
        if leader + continuation in text
    ]


def test_mojibake_pair_detector_covers_common_c2_c3_sequences() -> None:
    assert set(_mojibake_utf8_pairs("caf\u00c3\u00a9 costs \u00c2\u00a310")) == {
        "\u00c3\u00a9",
        "\u00c2\u00a3",
    }
    assert _mojibake_utf8_pairs("\u00c3lvaro and \u00c2ngela") == []


def test_builtin_personas_are_clean_utf8_text() -> None:
    failures: list[str] = []
    persona_paths = sorted(BUILTIN_PERSONAS.rglob("*.persona.md"))
    assert persona_paths, f"No built-in personas found under {BUILTIN_PERSONAS}"

    for path in persona_paths:
        text = path.read_text(encoding="utf-8", errors="strict")
        for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
            sentinels = [value for value in MOJIBAKE_SENTINELS if value in line]
            utf8_pairs = _mojibake_utf8_pairs(line)
            c1_controls = [
                f"U+{ord(character):04X}"
                for character in line
                if 0x80 <= ord(character) <= 0x9F
            ]
            if sentinels or utf8_pairs or c1_controls:
                escaped = line.encode("unicode_escape").decode("ascii")
                failures.append(
                    f"{path.relative_to(BUILTIN_PERSONAS)}:{line_number}: "
                    f"sentinels={sentinels!r}, utf8_pairs={utf8_pairs!r}, "
                    f"c1_controls={c1_controls!r}, "
                    f"text={escaped}"
                )

    assert not failures, "Built-in persona text contains mojibake:\n" + "\n".join(
        failures
    )
