"""Pylint checker for notebook output and removable metadata."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from pylint.checkers import BaseRawFileChecker

_notebooks: list[str] = []


def configure_notebooks(paths: list[str]) -> None:
    """Supply paths only to the production Pylint worker for this check."""
    global _notebooks
    _notebooks = paths


class NotebookChecker(BaseRawFileChecker):
    name = "eryx-notebooks"
    msgs = {
        "W9801": ("Notebook %s contains removable output or metadata",
                   "unstripped-notebook", "nbstripout --verify would change this notebook."),
        "F9802": ("Notebook %s could not be verified: %s",
                   "notebook-verification-failed", "nbstripout could not reliably verify this notebook."),
    }

    def open(self) -> None:
        for path in _notebooks:
            self.linter.set_current_module(Path(path).stem, str(Path.cwd() / path))
            try:
                result = subprocess.run([sys.executable, "-m", "nbstripout", "--verify", path],
                                        capture_output=True, text=True, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                self.add_message("notebook-verification-failed", line=1, args=(path, str(exc)))
                continue
            expected = f"Dry run: would have stripped {path}\n"
            if result.returncode == 0 and not result.stdout and not result.stderr:
                continue
            if result.returncode == 1 and result.stdout == expected and not result.stderr:
                self.add_message("unstripped-notebook", line=1, args=(path,))
                continue
            detail = (result.stderr or result.stdout).strip() or f"unexpected exit code {result.returncode}"
            self.add_message("notebook-verification-failed", line=1,
                             args=(path, f"exit {result.returncode}: {detail[:500]}"))

    def process_module(self, node) -> None:
        """Pylint calls this for Python modules; notebooks are handled in open()."""


def register(linter) -> None:
    linter.register_checker(NotebookChecker(linter))
