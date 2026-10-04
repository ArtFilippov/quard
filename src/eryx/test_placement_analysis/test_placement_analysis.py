"""Local interface for mirrored test-class placement."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

class TestPlacementAnalysis(Protocol):
    """Report placement using a reviewed folder-ownership service."""

    def violations(self) -> list[dict[str, object]]: ...
    def mirrored_folder(self, path: str) -> Path: ...
