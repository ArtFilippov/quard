"""Local contract for a complete consumer check run."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


class CheckRunner(Protocol):
    """Run independent jobs and return a complete archived report."""

    def run(self, root: Path, *, serial: bool, timeout: int,
            lcom_files: str | None, capture_fn: Callable[..., dict[str, Any]],
            versions_fn: Callable[..., Any]) -> dict[str, Any]: ...
