"""Contract for analyzing outer class declarations in discovered source files."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Protocol


class OuterClasses(Protocol):
    """Count class declarations without an enclosing class and report violations."""

    def collect(self, tree: ast.Module) -> list[ast.ClassDef]:
        ...

    def analyze(self, root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
        ...
