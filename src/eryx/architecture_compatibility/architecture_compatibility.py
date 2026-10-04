"""Contract for installing pinned in-process native architecture repairs."""

from __future__ import annotations

from typing import Protocol


class ArchitectureCompatibility(Protocol):
    """Install the reviewed compatibility fixes before analyzer initialization."""

    def install(self) -> None:
        ...
