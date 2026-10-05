"""Validate independent consumer job selections and their upgrade defaults."""
from __future__ import annotations

from typing import Any, cast

from eryx._common import EryxError

OPTIONAL_JOBS = {
    "lcom4": "lcom4", "method_decorators": "method-decorators",
    "instance_fields": "instance-fields", "class_contracts": "class-contracts",
    "contract_folders": "contract-folders", "test_placement": "test-placement",
    "outer_classes": "outer-classes", "free_function_placement": "free-function-placement",
    "encapsulated_only": "encapsulated-only",
}
CONSTRUCTOR_PURITY = "constructor_purity"
CHECK_DEFAULTS = {**dict.fromkeys(OPTIONAL_JOBS, True), CONSTRUCTOR_PURITY: False}


def validate_checks(data: object) -> dict[str, bool]:
    expected = ", ".join(sorted(CHECK_DEFAULTS))
    if not isinstance(data, dict):
        raise EryxError("INVALID_CHECKS", f"[checks] expects boolean switches: {expected}.")
    entries = cast(dict[str, Any], data)
    if (set(entries) - CHECK_DEFAULTS.keys() or CONSTRUCTOR_PURITY not in entries
            or any(not isinstance(value, bool) for value in entries.values())):
        raise EryxError("INVALID_CHECKS", f"[checks] expects boolean switches: {expected}.")
    return cast(dict[str, bool], {**CHECK_DEFAULTS, **entries})
