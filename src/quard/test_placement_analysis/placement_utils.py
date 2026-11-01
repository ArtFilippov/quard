"""Discover top-level tests and execute mirrored test-placement analysis."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import cast

from quard._common import read_json
from quard.architecture_draft.generated_selection_utils import IDENTITY, MARKER, generated
from quard.policy_utils import gateways


def top_level_tests(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Find module-level tests, including declarations under conditional suites."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test_"):
                    found.append(child)
            elif not isinstance(child, ast.ClassDef):
                visit(child)

    visit(tree)
    return found


def analyze(root: Path, production: list[str], tests: list[str],
            test_directories: list[str]) -> tuple[list[dict[str, object]], str | None]:
    from quard.test_placement_analysis.test_placement import TestPlacement

    trees: dict[str, ast.Module] = {}
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in production + tests:
        if (Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid test placement source path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "TEST_PLACEMENT_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
    placement = TestPlacement(trees, set(production), set(tests), set(test_directories),
                              gateways(root), generated_v2=(generated(root) and
                                                             read_json(root / MARKER) == IDENTITY))
    diagnostics.extend(placement.violations())
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description="Map discovered test classes to local production classes and their folders.")
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        data: object = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Test placement manifest requires production, tests and test_directory_names lists")
        fields = cast(dict[str, object], data)
        if any(not isinstance(fields.get(key), list) or any(
                not isinstance(value, str) for value in cast(list[object], fields[key]))
                for key in ("production", "tests", "test_directory_names")):
            raise ValueError("Test placement manifest requires production, tests and test_directory_names lists")
        entries = cast(dict[str, list[str]], data)
        diagnostics, error = analyze(Path.cwd(), entries["production"], entries["tests"],
                                     entries["test_directory_names"])
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        error = "TEST_PLACEMENT_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc), "is_infrastructure_error": True}]
    print(json.dumps({"quard_test_placement_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)
