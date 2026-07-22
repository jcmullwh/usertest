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


def test_builtin_personas_are_clean_utf8_text() -> None:
    failures: list[str] = []
    persona_paths = sorted(BUILTIN_PERSONAS.rglob("*.persona.md"))
    assert persona_paths, f"No built-in personas found under {BUILTIN_PERSONAS}"

    for path in persona_paths:
        text = path.read_text(encoding="utf-8", errors="strict")
        for line_number, line in enumerate(text.splitlines(), start=1):
            sentinels = [value for value in MOJIBAKE_SENTINELS if value in line]
            c1_controls = [
                f"U+{ord(character):04X}"
                for character in line
                if 0x80 <= ord(character) <= 0x9F
            ]
            if sentinels or c1_controls:
                escaped = line.encode("unicode_escape").decode("ascii")
                failures.append(
                    f"{path.relative_to(BUILTIN_PERSONAS)}:{line_number}: "
                    f"sentinels={sentinels!r}, c1_controls={c1_controls!r}, "
                    f"text={escaped}"
                )

    assert not failures, "Built-in persona text contains mojibake:\n" + "\n".join(
        failures
    )
