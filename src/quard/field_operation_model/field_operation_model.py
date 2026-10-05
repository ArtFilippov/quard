"""Constructor contract for recorded field operations."""
from __future__ import annotations

import ast
from typing import Protocol


class FieldOperationModel(Protocol):
    """An operation has a field, kind, source location and optional value."""

    def __init__(self, name: str, kind: str, location: ast.expr,
                 value: ast.expr | None = None) -> None:
        """Declare a named field operation and its source location.

        Args:
            name (str): Stored field name.
            kind (str): Operation kind.
            location (ast.expr): Source expression for the operation.
            value (ast.expr | None, default=None): Value written, when applicable.
        """
        super().__init__()
