"""Contract for static evidence about stored-field types."""
from __future__ import annotations

import ast
from collections.abc import Set as AbstractSet
from typing import Protocol

EMPTY_NAMES: frozenset[str] = frozenset()


class FieldTypeAnalysis(Protocol):
    """Resolve annotations and uncomplicated expressions without runtime inference."""

    def name(self, node: ast.expr, module: str, local: AbstractSet[str] = EMPTY_NAMES,
             seen: frozenset[str] = EMPTY_NAMES) -> str: ...
    def annotation(self, node: ast.expr, module: str,
                   local: AbstractSet[str] = EMPTY_NAMES) -> str: ...
    def expression(self, node: ast.expr, module: str,
                   local: AbstractSet[str] = EMPTY_NAMES) -> str: ...
