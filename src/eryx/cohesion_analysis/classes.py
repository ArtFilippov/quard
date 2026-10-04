"""Compute basic class cohesion from the consumer's discovered Python files."""
from __future__ import annotations

import ast
import sys
from typing import override

from eryx.cohesion_analysis.cohesion_worker_utils import analyze, main
from eryx.cohesion_analysis.cohesion_analysis import CohesionAnalysis
from eryx.cohesion_analysis.declarations import Declarations
from eryx.cohesion_analysis.declaration_utils import ABSTRACT_DECORATORS, METHOD, dotted
from eryx.cohesion_analysis.scoring_utils import (SPECIAL_METHODS, components, eligible,
                                                  is_placeholder)
from eryx.cohesion_references.method_references import MethodReferences

__all__ = ["ABSTRACT_DECORATORS", "METHOD", "SPECIAL_METHODS", "Classes", "Declarations",
           "MethodReferences", "analyze", "components", "dotted", "eligible",
           "is_placeholder", "main"]


class Classes(ast.NodeVisitor, CohesionAnalysis):
    def __init__(self, path: str, declarations: Declarations):
        super().__init__()
        self.path = path
        self.declarations = declarations
        self.parents: list[str] = []
        self.diagnostics: list[dict[str, object]] = []

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        symbol = ".".join([*self.parents, node.name])
        if (self.declarations.protocol(symbol) or
                self.declarations.abc(symbol) and self.declarations.abstract_methods(symbol)):
            self.parents.append(node.name)
            self.generic_visit(node)
            self.parents.pop()
            return
        groups = components(node, self.declarations)
        score = len(groups)
        if score != 1:
            detail = ("no eligible methods" if score == 0 else
                      "disconnected method groups: " + "; ".join(", ".join(group) for group in groups))
            self.diagnostics.append({"rule": "LCOM4", "path": self.path, "line": node.lineno,
                                     "column": node.col_offset + 1, "symbol": symbol,
                                     "message": f"{symbol}: LCOM4 score {score}; {detail}.", "score": score,
                                     "groups": groups})
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    @override
    def results(self) -> list[dict[str, object]]:
        return self.diagnostics


if __name__ == "__main__":
    sys.exit(main())
