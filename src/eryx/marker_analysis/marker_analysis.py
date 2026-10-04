"""Contract for collecting marker and descriptor declarations."""
from __future__ import annotations

import ast
from typing import Protocol


class MarkerAnalysis(Protocol):
    """Resolve identity decorators and property declarations in discovered source."""

    def is_identity(self, node: ast.expr) -> bool: ...
    def symbol(self, node: ast.expr) -> str | None: ...
    def property_declarations(self, node: ast.ClassDef
                              ) -> dict[str, list[tuple[ast.stmt, str, str]]]: ...
    def imported(self, qualified: str) -> str | None: ...
