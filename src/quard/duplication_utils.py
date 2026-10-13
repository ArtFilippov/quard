"""Validate the shared native Python clone minimum for a consumer."""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from quard._common import QuardError, load_toml


def validate_duplication(data: object) -> int:
    if not isinstance(data, dict):
        raise QuardError("INVALID_DUPLICATION", "[duplication] expects min_units only.")
    entries = cast(dict[str, Any], data)
    if set(entries) - {"min_units", "min_lines"}:
        raise QuardError("INVALID_DUPLICATION", "[duplication] expects min_units only.")
    if "min_lines" in entries:
        if "min_units" in entries:
            raise QuardError("INVALID_DUPLICATION",
                             "[duplication] min_lines and min_units are ambiguous; keep only min_units.")
        raise QuardError("INVALID_DUPLICATION",
                         "[duplication] min_lines is obsolete; reinstall Quard to migrate it to min_units.")
    value = entries.get("min_units", 3)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise QuardError("INVALID_DUPLICATION",
                         "[duplication] min_units must be a positive integer.")
    return value


def read_duplication_min_units(root: Path) -> int:
    return validate_duplication(load_toml(root / ".quard/config.toml").get("duplication", {}))
