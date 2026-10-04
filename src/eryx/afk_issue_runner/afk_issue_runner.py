"""Contract for running the public AFK issue command in a selected checkout."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import subprocess
from typing import Protocol


class AFKIssueRunner(Protocol):
    """Run the shell entry point and return its observable process result."""

    def command(self, args: list[str]) -> list[str]:
        ...

    def run(self, args: list[str], *, cwd: Path, env: Mapping[str, str],
            timeout: int = 30) -> subprocess.CompletedProcess[str]:
        ...
