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
    }

    def open(self) -> None:
        for path in _notebooks:
            result = subprocess.run([sys.executable, "-m", "nbstripout", "--verify", path],
                                    capture_output=True, text=True, check=False)
            if result.returncode:
                self.linter.set_current_module(Path(path).stem, str(Path.cwd() / path))
                self.add_message("unstripped-notebook", line=1, args=(path,))

    def process_module(self, node) -> None:
        """Pylint calls this for Python modules; notebooks are handled in open()."""


def register(linter) -> None:
    linter.register_checker(NotebookChecker(linter))
