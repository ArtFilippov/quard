"""Contract for restricted explicit and implicit call traversal."""
from __future__ import annotations

import ast
from typing import Protocol


class RestrictedCalls(Protocol):
    """Expose source traversal and positioned call/property diagnostics to the worker."""

    def visit(self, node: ast.AST) -> object: ...
    def results(self) -> list[dict[str, object]]: ...
