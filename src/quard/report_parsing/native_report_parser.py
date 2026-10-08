"""Consumer-bound access to native report parsing and normalization."""
from __future__ import annotations

from pathlib import Path
from typing import Any, override

from quard import _reports
from quard.report_parsing.report_parsing import ReportParsing


class NativeReportParser(ReportParsing):
    """Parse native output with paths relative to a selected consumer.

    Args:
        root (Path): Consumer project root.
    """

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root

    @override
    def parse_pylint(self, text: str) -> tuple[list[dict[str, Any]], str | None]:
        return _reports.parse_pylint(text, self._root)

    @override
    def parse_pyright(self, text: str) -> list[dict[str, Any]]:
        return _reports.parse_pyright(text, self._root)

    @override
    def parse_pydoclint(self, text: str) -> list[dict[str, Any]]:
        return _reports.parse_pydoclint(text, self._root)

    @override
    def parse_tach(self, text: str, external: bool = False) -> list[dict[str, Any]]:
        return _reports.parse_tach(text, self._root, external)

    @override
    def normalize(self, tool: str, result: dict[str, Any],
                  json_file: Path | None = None,
                   expected_paths: list[str] | None = None,
                   expected_engine: bool = False) -> dict[str, Any]:
        return _reports.normalize(tool, result, self._root, json_file, expected_paths,
                                  expected_engine)
