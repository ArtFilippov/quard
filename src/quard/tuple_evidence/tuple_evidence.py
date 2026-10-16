"""Contract for conservative type evidence in callable signatures."""
from __future__ import annotations

import ast
from typing import Protocol

EMPTY: frozenset[str] = frozenset()


class TupleEvidence(Protocol):
    """Decide whether an annotation contains a proven raw tuple type."""

    def contains(self, node: ast.expr, module: str,
                 local: frozenset[str] = EMPTY,
                 seen: frozenset[str] = EMPTY) -> bool: ...

    def alias(self, name: str, seen: frozenset[str]) -> bool: ...

    def reference(self, node: ast.expr, module: str, local: frozenset[str]) -> str | None: ...
