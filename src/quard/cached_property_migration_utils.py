"""Suggest the read-only qraft descriptor at proven standard-library use sites."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import TypeGuard

from quard.marker_analysis.marker_index import MarkerIndex

RULE = "FUNCTOOLS_CACHED_PROPERTY"
MESSAGE = ("Replace functools.cached_property with qraft.cached_property; "
           "add qraft as a runtime dependency.")


def array(value: object) -> TypeGuard[list[object]]:
    """Establish the JSON array shape before validating its elements.

    Args:
        value (object): Decoded manifest.

    Returns:
        TypeGuard[list[object]]: Whether the manifest is an array.
    """
    return isinstance(value, list)


def manifest(path: Path) -> list[str]:
    """Read the discovered Python path list without accepting malformed entries.

    Args:
        path (Path): Path to the discovered-file manifest.

    Returns:
        list[str]: Validated Python paths.

    Raises:
        ValueError: The manifest is not a list of paths.
    """
    entries: object = json.loads(path.read_text(encoding="utf-8"))
    if not array(entries):
        raise ValueError("Cached-property migration manifest must list Python paths")
    paths: list[str] = []
    for item in entries:
        if not isinstance(item, str):
            raise ValueError("Cached-property migration manifest must list Python paths")
        paths.append(item)
    return paths


def valid(root: Path, source: Path) -> bool:
    """Require discovered Python paths within the selected project.

    Args:
        root (Path): Consumer project root.
        source (Path): Proposed manifest path.

    Returns:
        bool: Whether the path is a discovered-file candidate.
    """
    if (source.is_absolute() or ".." in source.parts or source.suffix not in {".py", ".pyi"}
            or not source.parts or source.parts[0] not in {"src", "tests"}):
        return False
    return (root / source).resolve().is_relative_to(root.resolve())


def parse(root: Path, paths: list[str]) -> tuple[dict[str, ast.Module], list[dict[str, object]], str | None]:
    """Parse paths while retaining findings from readable peers.

    Args:
        root (Path): Consumer project root.
        paths (list[str]): Discovered production, test and stub paths.

    Returns:
        tuple[dict[str, ast.Module], list[dict[str, object]], str | None]: Trees, errors and status.
    """
    diagnostics: list[dict[str, object]] = []
    trees: dict[str, ast.Module] = {}
    error: str | None = None
    for path in paths:
        source = Path(path)
        if not valid(root, source):
            error = "CACHED_PROPERTY_MIGRATION_EXECUTION_ERROR"
            diagnostics.append({"rule": error, "path": path,
                                "message": f"Invalid source path: {path!r}"})
            continue
        try:
            trees[path] = ast.parse((root / source).read_text(encoding="utf-8"), filename=path)
        except (OSError, ValueError, SyntaxError) as exc:
            error = "CACHED_PROPERTY_MIGRATION_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "message": str(exc),
                                "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None)})
    return trees, diagnostics, error


def uses(path: str, tree: ast.Module, index: MarkerIndex) -> list[dict[str, object]]:
    """Locate each proven decorator or descriptor-constructor call.

    Args:
        path (str): Discovered source path.
        tree (ast.Module): Parsed source or stub.
        index (MarkerIndex): Matching evidence-profile bindings.

    Returns:
        list[dict[str, object]]: Positioned migration findings.
    """
    diagnostics: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                if index.symbol(decorator) == "functools.cached_property":
                    diagnostics.append({"rule": RULE, "path": path,
                                        "line": decorator.lineno,
                                        "column": decorator.col_offset + 1,
                                        "symbol": node.name, "message": MESSAGE})
        elif isinstance(node, ast.Call) and index.call_symbol(node) == "functools.cached_property":
            diagnostics.append({"rule": RULE, "path": path, "line": node.func.lineno,
                                "column": node.func.col_offset + 1,
                                "symbol": "cached_property", "message": MESSAGE})
    return diagnostics


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    """Find proven uses independently in source and stub evidence.

    Args:
        root (Path): Consumer project root.
        paths (list[str]): Discovered production, test and stub paths.

    Returns:
        tuple[list[dict[str, object]], str | None]: Findings and any scan error.
    """
    trees, diagnostics, error = parse(root, paths)
    for suffix in (".py", ".pyi"):
        selected = {path: tree for path, tree in trees.items() if path.endswith(suffix)}
        index = MarkerIndex(selected, collect_calls=True)
        for path, tree in selected.items():
            diagnostics.extend(uses(path, tree, index))
    return diagnostics, error


def main() -> int:
    """Emit a validated adapter payload.

    Returns:
        int: Two on error, one on findings, zero on a clean scan.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    try:
        diagnostics, error = analyze(Path.cwd(), manifest(args.manifest))
    except (OSError, ValueError, TypeError) as exc:
        error = "CACHED_PROPERTY_MIGRATION_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc)}]
    print(json.dumps({"quard_cached_property_migration_adapter": 1,
                      "diagnostics": diagnostics, "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
