"""Parse sources and check the one-outer-class-per-file rule."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import override

from eryx.outer_classes.outer_classes import OuterClasses


class ParsedOuterClasses(OuterClasses):
    """Report positioned declarations and parsing failures without hiding either."""

    @override
    def collect(self, tree: ast.Module) -> list[ast.ClassDef]:
        classes: list[ast.ClassDef] = []

        def visit(node: ast.AST, enclosed: bool) -> None:
            if isinstance(node, ast.ClassDef):
                if not enclosed:
                    classes.append(node)
                enclosed = True
            for child in ast.iter_child_nodes(node):
                visit(child, enclosed)

        visit(tree, False)
        return classes

    @override
    def analyze(self, root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
        diagnostics: list[dict[str, object]] = []
        error = None
        for path in paths:
            if (Path(path).is_absolute() or ".." in Path(path).parts
                    or Path(path).suffix not in {".py", ".pyi"}):
                raise ValueError(f"Invalid outer-class source path: {path!r}")
            try:
                tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
            except (OSError, ValueError, SyntaxError) as exc:
                error = "OUTER_CLASSES_PARSE_ERROR"
                diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                    "column": getattr(exc, "offset", None), "message": str(exc),
                                    "is_infrastructure_error": True})
                continue
            classes = self.collect(tree)
            if len(classes) > 1:
                for node in classes:
                    message = f"{node.name}: {path} has {len(classes)} outer classes (max one per file)."
                    diagnostics.append({"rule": "MULTIPLE_OUTER_CLASSES", "path": path,
                                        "line": node.lineno, "column": node.col_offset + 1,
                                        "symbol": node.name, "message": message})
        return diagnostics, error
