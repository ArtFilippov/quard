"""Subprocess adapter for the existing AFK issue shell command."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import subprocess
from typing import override

from eryx.afk_issue_runner.afk_issue_runner import AFKIssueRunner


class ShellAFKIssueRunner(AFKIssueRunner):
    """Invoke ralph.sh without changing its selection or session semantics."""

    def __init__(self, script: Path) -> None:
        """Select the public shell entry point.

        Args:
            script (Path): Path to ralph.sh.
        """
        super().__init__()
        self._script = script

    @override
    def command(self, args: list[str]) -> list[str]:
        """Build the shell invocation with its original argument order.

        Args:
            args (list[str]): Command-line arguments.

        Returns:
            list[str]: Bash command and arguments.
        """
        return ["bash", str(self._script), *args]

    @override
    def run(self, args: list[str], *, cwd: Path, env: Mapping[str, str],
            timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(self.command(args), cwd=cwd, env=env,
                              text=True, capture_output=True, timeout=timeout, check=False)
