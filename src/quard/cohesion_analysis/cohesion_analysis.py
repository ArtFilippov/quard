"""Local interface for cohesion analysis."""
from __future__ import annotations

import ast
from abc import ABC, abstractmethod


class CohesionAnalysis(ABC):
    """Declarations, references and scoring operations."""

    @abstractmethod
    def __init__(self, *args: object, **kwargs: object) -> None:
        """Create a cohesion analyzer.

        Args:
            *args (object): Inputs for the concrete analyzer.
            **kwargs (object): Named inputs for the concrete analyzer.
        """

    def imported(self, expr: ast.expr) -> str | None:
        raise NotImplementedError

    def base(self, expr: ast.expr, scope: str) -> str | None:
        raise NotImplementedError
    def decorated(self, method: ast.FunctionDef | ast.AsyncFunctionDef,
                  name: str, module: str) -> bool:
        raise NotImplementedError

    def is_abstract(self, method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        raise NotImplementedError

    def protocol(self, name: str) -> bool:
        raise NotImplementedError

    def abstract_methods(self, name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        raise NotImplementedError

    def abc(self, name: str, seen: frozenset[str] = frozenset()) -> bool:
        raise NotImplementedError

    def results(self) -> list[dict[str, object]]:
        raise NotImplementedError

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        raise NotImplementedError
