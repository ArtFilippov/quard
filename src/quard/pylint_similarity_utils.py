"""Reuse native Pylint similarity windows across the project's file pairs."""
from __future__ import annotations

from functools import cache
from importlib import metadata

from pylint.checkers import symilar

SUPPORTED_VERSION = "4.0.8"


def install() -> None:
    """Cache native similarity hashes for the lifetime of a Pylint worker.

    Raises:
        RuntimeError: Pylint's pinned implementation has changed.
    """
    if metadata.version("pylint") != SUPPORTED_VERSION:
        raise RuntimeError("Pylint similarity caching requires review for this version")
    symilar.hash_lineset = cache(symilar.hash_lineset)
