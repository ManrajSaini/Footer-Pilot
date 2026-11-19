"""Turning Pydantic validation errors into sentences a person can act on."""

from typing import Any


def error_text(detail: dict[str, Any]) -> str:
    """One validation error (an item of `ValidationError.errors()`) as plain words."""
    if detail["type"] == "value_error":
        # Our own validators raise ValueError with a ready-made sentence.
        return detail["msg"].removeprefix("Value error, ")
    if detail["type"] in ("string_too_long", "too_long"):
        return f"Use at most {detail['ctx']['max_length']} characters"
    return "This value is not valid"
