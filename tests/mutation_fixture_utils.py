"""Restore independently edited consumer fixture files after a check."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def temporary_edits(changes: Mapping[Path, str]) -> Generator[None]:
    """Write temporary fixture contents and restore existing or absent files.

    Args:
        changes (Mapping[Path, str]): Replacement contents for fixture paths.

    Yields:
        None: While the edited files are in place.
    """
    originals = {file: file.read_text() if file.exists() else None for file in changes}
    try:
        for file, contents in changes.items():
            file.write_text(contents)
        yield
    finally:
        for file, original in originals.items():
            if original is None:
                file.unlink()
            else:
                file.write_text(original)
