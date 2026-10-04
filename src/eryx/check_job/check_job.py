"""Contract for executing one native analyzer job and archiving its raw output."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


class CheckJob(Protocol):
    """Execute a job without hiding tool failures or native diagnostics."""

    def execute(self, job: dict[str, Any], root: Path, run_dir: Path, timeout: int, *,
                capture_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        ...
