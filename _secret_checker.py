"""Pylint adapter for detect-secrets findings in selected consumer files."""
from __future__ import annotations

from pathlib import Path

from pylint.checkers import BaseRawFileChecker

_files: list[str] = []
_scanned: list[str] = []
_skipped: list[str] = []


def configure_secrets(paths: list[str]) -> None:
    """Supply the check's effective file scope to the Pylint worker."""
    global _files, _scanned, _skipped
    _files = paths
    _scanned, _skipped = [], []


def secret_coverage() -> dict[str, list[str]]:
    """Return only files for which the scanner reached a definite outcome."""
    return {"scanned": _scanned, "skipped": _skipped}


class SecretChecker(BaseRawFileChecker):
    name = "eryx-secrets"
    msgs = {
        "W9803": ("Potential secret (%s) detected", "potential-secret",
                  "detect-secrets identified a potential secret; the value is never reported."),
        "F9804": ("Secret scan failed for %s", "secret-scan-failed",
                  "The file could not be reliably scanned."),
    }

    def open(self) -> None:
        from detect_secrets.core.scan import _get_lines_from_file, _process_line_based_plugins
        from detect_secrets.filters.heuristic import is_non_text_file
        from detect_secrets.settings import default_settings, get_plugins

        with default_settings():
            if not get_plugins():
                for path in _files:
                    self.linter.set_current_module(Path(path).stem, str(Path.cwd() / path))
                    self.add_message("secret-scan-failed", line=1, args=(path,))
                return
            for path in _files:
                self.linter.set_current_module(Path(path).stem, str(Path.cwd() / path))
                try:
                    source = Path(path)
                    if source.is_symlink() or not source.is_file():
                        raise OSError("Input is not a regular file")
                    # detect-secrets' scan_file silently swallows IOError and UnicodeError.
                    # Validate the input, then use its pinned scan primitives so failures
                    # remain observable instead of counting an unexamined file as clean.
                    try:
                        text = source.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        if is_non_text_file(path):
                            _skipped.append(path)
                            continue
                        raise
                    if "\0" in text:
                        _skipped.append(path)
                        continue
                    has_secret = False
                    for lines in _get_lines_from_file(path):
                        for secret in _process_line_based_plugins(
                                list(enumerate(lines, start=1)), filename=path):
                            has_secret = True
                            self.add_message("potential-secret", line=secret.line_number,
                                             args=(secret.type,))
                        if has_secret:
                            break
                    _scanned.append(path)
                except Exception:
                    # Upstream exceptions can include source content; never print them.
                    self.add_message("secret-scan-failed", line=1, args=(path,))

    def process_module(self, node) -> None:
        """The selected file list is scanned once in open(), not per Python module."""


def register(linter) -> None:
    linter.register_checker(SecretChecker(linter))
