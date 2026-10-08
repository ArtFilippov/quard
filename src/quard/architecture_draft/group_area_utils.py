"""Identify the owning directory and assembly area of a production module."""
from __future__ import annotations

from pathlib import Path

MAIN_FOLDER = "main"
ENVELOPES_FOLDER = "envelopes"
ORDINARY = "ordinary"


def area(path: str) -> tuple[str, str]:
    """Identify the nearest group and its assembly directory.

    Args:
        path (str): Relative Python source or stub path.

    Returns:
        tuple[str, str]: Group identity and assembly kind.
    """
    directories = Path(path).parts[1:-1]
    if MAIN_FOLDER in directories:
        return ".".join(directories[:directories.index(MAIN_FOLDER) + 1]), MAIN_FOLDER
    if len(directories) > 1 and directories[-1] == ENVELOPES_FOLDER:
        return ".".join(directories[:-1]), ENVELOPES_FOLDER
    return ".".join(directories), ORDINARY
