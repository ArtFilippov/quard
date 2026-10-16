"""Find proven raw tuple types in callable signature annotations."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import TypeGuard, cast

from quard.marker_utils import local_names, module_name
from quard.tuple_evidence.tuple_evidence import EMPTY, TupleEvidence
from quard.tuple_evidence.tuple_index import TupleIndex

RULE = "RAW_TUPLE_SIGNATURE"
ERROR = "TUPLE_SIGNATURES_EXECUTION_ERROR"
PARSE_ERROR = "TUPLE_SIGNATURES_PARSE_ERROR"
PARENT = ".."
EXTENSIONS = frozenset({".py", ".pyi"})
ROOTS = frozenset({"src", "tests"})


def signatures(path: str, tree: ast.Module, index: TupleEvidence) -> list[dict[str, object]]:
    """Report one finding for each tuple-shaped parameter or result.

    Args:
        path (str): Consumer source path.
        tree (ast.Module): Parsed source.
        index (TupleEvidence): Matching source or stub type evidence.

    Returns:
        list[dict[str, object]]: Positioned signature violations.
    """
    findings: list[dict[str, object]] = []

    def visit(node: ast.AST, owners: list[str], local: frozenset[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbol = ".".join([*owners, node.name])
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                         *[arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None]]
            for argument in arguments:
                if argument.annotation and index.contains(
                        argument.annotation, module_name(path), local):
                    findings.append(finding(path, f"{symbol}.{argument.arg}",
                                            argument.annotation, False))
            if node.returns and index.contains(node.returns, module_name(path), local):
                findings.append(finding(path, symbol, node.returns, True))
            owners = [*owners, node.name]
            local = local | local_names(node)
        elif isinstance(node, ast.ClassDef):
            owners = [*owners, node.name]
        for child in ast.iter_child_nodes(node):
            visit(child, owners, local)

    visit(tree, [], EMPTY)
    return findings


def finding(path: str, symbol: str, annotation: ast.expr, result: bool) -> dict[str, object]:
    """Locate a signature violation at its declared annotation.

    Args:
        path (str): Consumer source path.
        symbol (str): Callable or parameter identity.
        annotation (ast.expr): Declared type.
        result (bool): Whether this is a result annotation.

    Returns:
        dict[str, object]: Positioned diagnostic.
    """
    return {"rule": RULE, "path": path, "symbol": symbol,
            "line": annotation.lineno, "column": annotation.col_offset + 1,
            "message": f"Raw tuple type in {'result' if result else 'parameter'} {symbol}."}


def valid(root: Path, source: Path) -> bool:
    """Reject non-discovered paths and project escapes before reading sources.

    Args:
        root (Path): Consumer root.
        source (Path): Manifest path.

    Returns:
        bool: Whether the source belongs to the project.
    """
    if source.is_absolute() or PARENT in source.parts:
        return False
    if not source.parts or source.parts[0] not in ROOTS or source.suffix not in EXTENSIONS:
        return False
    return (root / source).resolve().is_relative_to(root.resolve())


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    """Scan discovered source and stub files independently, retaining peer findings.

    Args:
        root (Path): Consumer root.
        paths (list[str]): Discovered Python paths.

    Returns:
        tuple[list[dict[str, object]], str | None]: Findings and scan error.
    """
    trees: dict[str, ast.Module] = {}
    findings: list[dict[str, object]] = []
    error: str | None = None
    for path in paths:
        source = Path(path)
        if not valid(root, source):
            error = ERROR
            findings.append({"rule": error, "path": path,
                             "message": f"Invalid source path: {path!r}"})
            continue
        try:
            trees[path] = ast.parse((root / source).read_text(encoding="utf-8"), filename=path)
        except (OSError, ValueError, SyntaxError) as exc:
            error = PARSE_ERROR
            findings.append({"rule": error, "path": path, "message": str(exc),
                             "line": getattr(exc, "lineno", None),
                             "column": getattr(exc, "offset", None)})
    indexes = {suffix: TupleIndex({path: tree for path, tree in trees.items()
                                   if path.endswith(suffix)}) for suffix in (".py", ".pyi")}
    for path, tree in trees.items():
        findings.extend(signatures(path, tree, indexes[Path(path).suffix]))
    return findings, error


def paths_valid(value: object) -> TypeGuard[list[str]]:
    """Validate manifest entries before scanning consumer paths.

    Args:
        value (object): Decoded JSON payload.

    Returns:
        TypeGuard[list[str]]: Whether every entry is a path string.
    """
    return isinstance(value, list) and all(isinstance(path, str)
                                           for path in cast(list[object], value))


def manifest(path: Path) -> list[str]:
    """Require a JSON list of Python paths in the selected scope.

    Args:
        path (Path): Manifest path.

    Returns:
        list[str]: Python paths.

    Raises:
        ValueError: The manifest does not contain a path list.
    """
    paths: object = json.loads(path.read_text(encoding="utf-8"))
    if not paths_valid(paths):
        raise ValueError("Tuple-signatures manifest must list Python paths")
    return paths


def main() -> int:
    """Emit the positioned signature report for a discovered-file manifest.

    Returns:
        int: Two on error, one on findings, zero on clean scan.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        findings, error = analyze(Path.cwd(), manifest(args.manifest))
    except (OSError, ValueError, TypeError) as exc:
        error = ERROR
        findings = [{"rule": error, "message": str(exc)}]
    print(json.dumps({"quard_tuple_signatures_adapter": 1, "diagnostics": findings,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if findings else 0)


if __name__ == "__main__":
    sys.exit(main())
