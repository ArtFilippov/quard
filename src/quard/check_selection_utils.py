"""Validate independent consumer job selections and their upgrade defaults."""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from quard._common import QuardError

OPTIONAL_JOBS = {
    "lcom4": "lcom4", "method_decorators": "method-decorators",
    "instance_fields": "instance-fields", "class_contracts": "class-contracts",
    "contract_folders": "contract-folders", "test_placement": "test-placement",
    "outer_classes": "outer-classes", "free_function_placement": "free-function-placement",
    "leading_underscores": "leading-underscores",
    "encapsulated_only": "encapsulated-only",
    "pyright": "pyright", "pydoclint": "pydoclint",
}
CONSTRUCTOR_PURITY = "constructor_purity"
DUPLICATE_JOBS = ("jscpd", "jscpd-renamed", "jscpd-similar")
TACH_JOBS = ("tach", "tach-external")
DEPENDENCY_TYPES = "dependency_types"
CHECK_DEFAULTS = {**dict.fromkeys(OPTIONAL_JOBS, True), "duplicate_detection": True,
                  "tach": True, DEPENDENCY_TYPES: False, CONSTRUCTOR_PURITY: False}


def architecture_defaults(root: Path) -> dict[str, bool]:
    """Return upgrade defaults for the selected architecture mode.

    Args:
        root (Path): Consumer project root.

    Returns:
        dict[str, bool]: Validated-switch defaults for this architecture mode.
    """
    return {**CHECK_DEFAULTS,
            DEPENDENCY_TYPES: (root / ".quard/architecture.generated.json").exists()}


def validate_architecture_checks(root: Path, checks: dict[str, bool]) -> None:
    """Reject dependency-type selection without a generated policy marker.

    Args:
        root (Path): Consumer project root.
        checks (dict[str, bool]): Selected jobs.

    Raises:
        QuardError: Dependency types require generated architecture.
    """
    if checks[DEPENDENCY_TYPES] and not (root / ".quard/architecture.generated.json").exists():
        raise QuardError("DEPENDENCY_TYPES_REQUIRES_GENERATED",
                        "[checks] dependency_types = true requires generated architecture.")


def validate_checks(data: object) -> dict[str, bool]:
    expected = ", ".join(sorted(CHECK_DEFAULTS))
    if not isinstance(data, dict):
        raise QuardError("INVALID_CHECKS", f"[checks] expects boolean switches: {expected}.")
    entries = cast(dict[str, Any], data)
    if (set(entries) - CHECK_DEFAULTS.keys() or CONSTRUCTOR_PURITY not in entries
            or any(not isinstance(value, bool) for value in entries.values())):
        raise QuardError("INVALID_CHECKS", f"[checks] expects boolean switches: {expected}.")
    return cast(dict[str, bool], {**CHECK_DEFAULTS, **entries})
