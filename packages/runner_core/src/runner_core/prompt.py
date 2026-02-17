from __future__ import annotations

import re
from collections.abc import Mapping


class TemplateSubstitutionError(ValueError):
    pass


_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def build_prompt_from_template(*, template_text: str, variables: Mapping[str, str]) -> str:
    """
    Strict substitution: errors if the template contains placeholders not present in variables.
    """

    missing: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            missing.add(key)
            return match.group(0)

        value = variables[key]
        if not isinstance(value, str):
            raise TemplateSubstitutionError(
                f"Template variable {key!r} must be a string, got {type(value).__name__}."
            )
        return value

    rendered = _PLACEHOLDER_RE.sub(_replace, template_text)
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise TemplateSubstitutionError(f"Missing template variables: {missing_list}.")
    return rendered
