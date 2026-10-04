"""Local contract for native diagnostic parsing and normalization."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class ReportParsing(Protocol):
    """Convert native analyzer output into consumer-relative diagnostics."""

    def parse_pylint(self, text: str) -> tuple[list[dict[str, Any]], str | None]: ...

    def parse_pyright(self, text: str) -> list[dict[str, Any]]: ...

    def parse_pydoclint(self, text: str) -> list[dict[str, Any]]: ...

    def parse_tach(self, text: str, external: bool = False) -> list[dict[str, Any]]: ...

    def normalize(self, tool: str, result: dict[str, Any],
                  json_file: Path | None = None) -> dict[str, Any]: ...
