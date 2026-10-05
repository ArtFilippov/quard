"""Keep the native clean-architecture layer map aligned with reviewed Tach modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class CleanArchitectureLayers(Protocol):
    """Update the native layer map without replacing unrelated consumer settings."""

    def project(self, tach: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
        ...

    def sync(self, root: Path, tach: dict[str, Any]) -> None:
        ...
