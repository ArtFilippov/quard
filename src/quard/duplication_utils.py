"""Validate the shared native Python clone minimum for a consumer."""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from quard._common import QuardError, load_toml


def validate_duplication(data: object) -> int:
    if not isinstance(data, dict):
        raise QuardError("INVALID_DUPLICATION", "[duplication] expects min_lines only.")
    entries = cast(dict[str, Any], data)
    if set(entries) - {"min_lines"}:
        raise QuardError("INVALID_DUPLICATION", "[duplication] expects min_lines only.")
    value = entries.get("min_lines", 3)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise QuardError("INVALID_DUPLICATION",
                         "[duplication] min_lines must be a positive integer.")
    return value


def read_duplication_min_lines(root: Path) -> int:
    return validate_duplication(load_toml(root / ".quard/config.toml").get("duplication", {}))
