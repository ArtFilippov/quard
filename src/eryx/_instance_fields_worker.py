"""Report forbidden class state and external access to stored instance fields."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys

from eryx.field_ownership.accesses import Accesses
from eryx.field_ownership.ownership import Ownership
from eryx.field_ownership.bindings import Binding, PROTOCOL_BUILTINS, assigned_names, module_name

__all__ = ["Accesses", "Binding", "Ownership", "PROTOCOL_BUILTINS", "assigned_names",
           "module_name", "analyze", "main"]


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    trees: dict[str, ast.Module] = {}
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid instance field source path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "INSTANCE_FIELDS_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc)})
    ownership = Ownership(trees)
    diagnostics.extend(ownership.declaration_diagnostics())
    for path, tree in trees.items():
        visitor = Accesses(ownership, path)
        visitor.visit(tree)
        diagnostics.extend(visitor.results())
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Instance field manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), paths)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        error = "INSTANCE_FIELDS_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc)}]
    print(json.dumps({"eryx_instance_fields_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
