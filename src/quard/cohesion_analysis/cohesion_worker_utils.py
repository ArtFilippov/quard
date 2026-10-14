"""Parse selected Python files and emit cohesion diagnostics."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

from quard._common import read_json
from quard.architecture_draft.generated_selection_utils import IDENTITY, MARKER, generated
from quard.cohesion_analysis.declarations import Declarations
from quard.folder_analysis.folder_ownership import FolderOwnership
from quard.folder_analysis.generated_external_contracts import GeneratedOwnership
from quard.policy_utils import read_scope


def assemblies(root: Path, trees: dict[str, ast.Module]) -> set[tuple[str, str]]:
    """Prove assemblies using the selected contract-folder policy.

    Args:
        root (Path): Consumer directory.
        trees (dict[str, ast.Module]): Discovered source and stub syntax.

    Returns:
        set[tuple[str, str]]: Proven source paths and class symbols.
    """
    ownership = (GeneratedOwnership(trees) if generated(root)
                 and read_json(root / MARKER) == IDENTITY else FolderOwnership(trees))
    return ownership.assemblies(frozenset(read_scope(root)["test_directory_names"]))


def scores(paths: list[str], trees: dict[str, ast.Module],
           proven: set[tuple[str, str]]) -> list[dict[str, object]]:
    """Score selected classes using proven assembly evidence.

    Args:
        paths (list[str]): Selected Python source paths.
        trees (dict[str, ast.Module]): Parsed discovered files.
        proven (set[tuple[str, str]]): Valid assembly path and symbol pairs.

    Returns:
        list[dict[str, object]]: LCOM4 class diagnostics.
    """
    from quard.cohesion_analysis.classes import Classes

    diagnostics: list[dict[str, object]] = []
    for path in paths:
        if path not in trees:
            continue
        visitor = Classes(path, Declarations(trees[path]), frozenset(
            symbol for source, symbol in proven if source == path))
        visitor.visit(trees[path])
        diagnostics.extend(visitor.results())
    return diagnostics


def analyze(root: Path, paths: list[str], context: list[str] | None = None
            ) -> tuple[list[dict[str, object]], str | None]:
    diagnostics: list[dict[str, object]] = []
    error = None
    trees: dict[str, ast.Module] = {}
    for path in dict.fromkeys([*paths, *(context or [])]):
        if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError(f"Invalid LCOM4 source path: {path!r}")
        if Path(path).suffix not in {".py", ".pyi"} or path in paths and not path.endswith(".py"):
            raise ValueError(f"Invalid LCOM4 source path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "LCOM4_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
    proven: set[tuple[str, str]] = (
        assemblies(root, trees) if context is not None and error is None else set())
    diagnostics.extend(scores(paths, trees, proven))
    return diagnostics, error


def execution(args: argparse.Namespace) -> tuple[list[dict[str, object]], str | None]:
    """Read LCOM4 manifests before analyzing the selected consumer files.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.

    Returns:
        tuple[list[dict[str, object]], str | None]: Diagnostics and execution error.
    """
    return analyze(Path.cwd(), manifest(args.manifest),
                   manifest(args.envelopes_manifest) if args.envelopes_manifest else None)


def manifest(path: Path) -> list[str]:
    """Read a manifest without accepting unproved path values.

    Args:
        path (Path): Manifest JSON path.

    Returns:
        list[str]: Paths from the manifest.

    Raises:
        ValueError: Manifest is not a list of strings.
    """
    parsed: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, list):
        raise ValueError("LCOM4 manifest must be a list of Python paths")
    paths = [item for item in parsed if isinstance(item, str)]
    if len(paths) != len(parsed):
        raise ValueError("LCOM4 manifest must be a list of Python paths")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute basic class cohesion from discovered Python files.")
    _ = parser.add_argument("--envelopes-manifest", type=Path)
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        diagnostics, error = execution(args)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        diagnostics, error = ([{"rule": "LCOM4_EXECUTION_ERROR", "message": str(exc),
                                "is_infrastructure_error": True}], "LCOM4_EXECUTION_ERROR")
    print(json.dumps({"quard_lcom4_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)
