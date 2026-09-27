"""Pylint adapter for detect-secrets findings in selected consumer files."""
from __future__ import annotations

from pathlib import Path

from pylint.checkers import BaseRawFileChecker

_files: list[str] = []


def configure_secrets(paths: list[str]) -> None:
    """Supply the check's effective file scope to the Pylint worker."""
    global _files
    _files = paths


class SecretChecker(BaseRawFileChecker):
    name = "eryx-secrets"
    msgs = {
        "W9803": ("Potential secret (%s) detected", "potential-secret",
                  "detect-secrets identified a potential secret; the value is never reported."),
        "F9804": ("Secret scan failed for %s", "secret-scan-failed",
                  "The file could not be reliably scanned."),
    }

    def open(self) -> None:
        from detect_secrets.core.scan import scan_file
        from detect_secrets.settings import default_settings

        with default_settings():
            for path in _files:
                self.linter.set_current_module(Path(path).stem, str(Path.cwd() / path))
                try:
                    for secret in scan_file(path):
                        self.add_message("potential-secret", line=secret.line_number,
                                         args=(secret.type,))
                except Exception:
                    # Upstream exceptions can include source content; never print them.
                    self.add_message("secret-scan-failed", line=1, args=(path,))

    def process_module(self, node) -> None:
        """The selected file list is scanned once in open(), not per Python module."""


def register(linter) -> None:
    linter.register_checker(SecretChecker(linter))
