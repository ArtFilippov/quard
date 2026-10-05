"""Constructor contract for a statically resolved implicit dispatch site."""
from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Protocol

from quard._instance_fields_worker import Binding


class DispatchSiteModel(Protocol):
    """One implicit call, its receiver and its possible type-level targets."""

    def __init__(self, operation: ast.expr, receiver: ast.expr,
                 origins: frozenset[Binding], candidates: tuple[str, ...]) -> None:
        """Declare the four parts of an implicit dispatch.

        Args:
            operation (ast.expr): Source expression being checked.
            receiver (ast.expr): Expression supplying the receiver.
            origins (frozenset[Binding]): Statically known receiver origins.
            candidates (tuple[str, ...]): Possible dispatched methods.
        """
        super().__init__()

    def forbidden_targets(self, permitted: Callable[[ast.expr], bool],
                          intermediate: bool = False) -> tuple[tuple[str, int, int], ...]: ...
