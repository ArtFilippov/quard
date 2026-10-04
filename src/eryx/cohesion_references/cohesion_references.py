"""Visitor interface for method-reference collection."""
from __future__ import annotations

import ast
from abc import ABC, abstractmethod

from eryx.cohesion_analysis.cohesion_analysis import CohesionAnalysis


class CohesionReferences(CohesionAnalysis, ABC):
    """AST callbacks used by the cohesion reference visitor."""

    @abstractmethod
    def __init__(self, *args: object, **kwargs: object) -> None:
        """Create a reference visitor.

        Args:
            *args (object): Receiver and method names for the concrete visitor.
            **kwargs (object): Named visitor inputs.
        """

    def visit_Call(self, node: ast.Call) -> None: ...
    def visit_Attribute(self, node: ast.Attribute) -> None: ...
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None: ...
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None: ...
    def visit_Lambda(self, node: ast.Lambda) -> None: ...
