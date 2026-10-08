"""Identify the owning directory and assembly area of a production module."""
from __future__ import annotations

import os
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


def main_directories(root: Path, scope: dict[str, list[str]]) -> list[str]:
    """Find production main directories, including directories without Python files.

    Args:
        root (Path): Selected project root.
        scope (dict[str, list[str]]): Configured source discovery scope.

    Returns:
        list[str]: Sorted project-relative directory paths.
    """
    source = root / "src"
    if not source.is_dir():
        return []
    skipped = set(scope["exclude_directory_names"] + scope["test_directory_names"])
    found: list[str] = []
    for parent, directories, _ in os.walk(source, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in skipped)
        for name in directories:
            if name == MAIN_FOLDER:
                found.append((Path(parent) / name).relative_to(root).as_posix())
    return sorted(found)
