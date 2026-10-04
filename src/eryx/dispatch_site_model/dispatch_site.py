"""Resolved implicit-call evidence kept separate from call traversal."""
from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from eryx._instance_fields_worker import Binding
from eryx.dispatch_site_model.dispatch_site_model import DispatchSiteModel


@dataclass(frozen=True)
class DispatchSite(DispatchSiteModel):
    """One implicit invocation with its original receiver and possible targets.

    Attributes:
        operation (ast.expr): Source expression being checked.
        receiver (ast.expr): Expression supplying the receiver.
        origins (frozenset[Binding]): Statically known receiver origins.
        candidates (tuple[str, ...]): Possible dispatched methods.
    """

    operation: ast.expr
    receiver: ast.expr
    origins: frozenset[Binding]
    candidates: tuple[str, ...]

    @override
    def forbidden_targets(self, permitted: Callable[[ast.expr], bool],
                          intermediate: bool = False) -> tuple[tuple[str, int, int], ...]:
        """Return forbidden methods with the implicit operation's source position.

        Args:
            permitted (Callable[[ast.expr], bool]): Whether the receiver is privileged.
            intermediate (bool, default=False): Whether this is a produced intermediate receiver.

        Returns:
            tuple[tuple[str, int, int], ...]: Targets and one-based positions.
        """
        if not intermediate and permitted(self.receiver) and all(
                kind != "class" for kind, _ in self.origins):
            return ()
        return tuple((target, self.operation.lineno, self.operation.col_offset + 1)
                     for target in self.candidates)
