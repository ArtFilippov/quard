"""Identify and select Quard-managed architecture in a consumer."""
from __future__ import annotations

from pathlib import Path

from quard._common import QuardError, read_json, write_json


MARKER = ".quard/architecture.generated.json"
IDENTITY = {"policy": "contract-only", "version": 2}
LEGACY_IDENTITY = {"policy": "contract-only", "version": 1}


def generated(root: Path) -> bool:
    """Check the versioned policy selection, rejecting unknown formats.

    Args:
        root (Path): Selected project root.

    Returns:
        bool: Whether generated policy is selected.

    Raises:
        QuardError: The marker has an unrecognized schema.
    """
    path = root / MARKER
    if not path.exists():
        return False
    if read_json(path) not in (IDENTITY, LEGACY_IDENTITY):
        raise QuardError("INVALID_GENERATED_ARCHITECTURE",
                         f"Invalid generated policy marker: {path}")
    return True


def select(root: Path) -> bool:
    """Select generation for a fresh install; keep existing native policies.

    Args:
        root (Path): Selected project root.

    Returns:
        bool: Whether this installation uses generated architecture.
    """
    if generated(root):
        return True
    if (root / "tach.toml").exists() or (root / ".quard/architecture.ready.json").exists():
        return False
    _ = write_json(root / MARKER, IDENTITY, root)
    return True
