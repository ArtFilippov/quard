"""Folder names and worker execution for local contract ownership."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re

from quard._common import read_json, write_json
from quard.architecture_draft.generated_selection_utils import IDENTITY, MARKER, generated
from quard.folder_analysis.envelope_evidence_utils import proven
from quard.policy_utils import gateways, read_scope

SOURCE_ROOT = "src"
SOURCE_ROOT_DEPTH = 2

def snake_case(name: str) -> str:
    """Convert a class name, including acronym boundaries, to a folder name."""
    name = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def analyze(root: Path, paths: list[str], *, evidence: Path | None = None
            ) -> tuple[list[dict[str, object]], str | None]:
    from quard.folder_analysis.folder_ownership import FolderOwnership

    trees: dict[str, ast.Module] = {}
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid contract folder source path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "CONTRACT_FOLDERS_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
    scope = read_scope(root)
    if generated(root) and read_json(root / MARKER) == IDENTITY:
        from quard.architecture_draft.group_area_utils import main_directories
        from quard.folder_analysis.generated_group_roles_utils import analyze as group_roles
        from quard.folder_analysis.generated_external_contracts import GeneratedOwnership

        main_areas = main_directories(root, scope)
        for path in main_areas if len(main_areas) > 1 else []:
            diagnostics.append({"rule": "MULTIPLE_MAIN_DIRECTORIES", "path": path,
                                "line": 1, "column": 1, "symbol": "main",
                                "message": f"{path}: generated architecture permits only one "
                                           + "production main directory."})
        for path in sorted(trees):
            if (Path(path).parts[0] == SOURCE_ROOT
                    and len(Path(path).parts) == SOURCE_ROOT_DEPTH):
                diagnostics.append({
                    "rule": "SOURCE_ROOT_LAYOUT", "path": path, "line": 1, "column": 1,
                    "symbol": Path(path).name,
                    "message": f"{path}: place Python files inside a source directory."})
        ownership = GeneratedOwnership(trees, gateways(root))
        diagnostics.extend(group_roles(trees, ownership))
    else:
        ownership = FolderOwnership(trees, gateways(root))
        diagnostics.extend(ownership.folder_violations())
    failures = ownership.base_violations(frozenset(scope["test_directory_names"]))
    diagnostics.extend(failures)
    if evidence is not None and error is None:
        _ = write_json(evidence, proven(ownership, failures), root)
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description="Check folder ownership of customer contracts and implementations.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Contract folder manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), paths, evidence=args.evidence)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        error = "CONTRACT_FOLDERS_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc), "is_infrastructure_error": True}]
    print(json.dumps({"quard_contract_folders_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)
