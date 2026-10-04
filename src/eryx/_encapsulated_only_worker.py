"""Check proven restricted invocations in full discovered consumer scope."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import cast

from eryx._instance_fields_worker import Ownership
from eryx._restricted_agreement import agreement
from eryx.restricted_calls.calls import Calls
from eryx.restricted_targets.resolution import Restrictions


def analyze(root: Path, paths: list[object]) -> tuple[list[dict[str, object]], str | None,
                                                Restrictions]:
    trees: dict[str, ast.Module] = {}
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid encapsulated-only source path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "ENCAPSULATED_ONLY_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc)})
    restrictions = Restrictions(Ownership(trees))
    diagnostics.extend(agreement(restrictions))
    for path, tree in trees.items():
        visitor = Calls(restrictions, path)
        visitor.visit(tree)
        diagnostics.extend(visitor.results())
    return diagnostics, error, restrictions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Encapsulated-only manifest must be a list of Python paths")
        diagnostics, error, restrictions = analyze(Path.cwd(), cast(list[object], paths))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        error = "ENCAPSULATED_ONLY_EXECUTION_ERROR"
        diagnostics, restrictions = [{"rule": error, "message": str(exc)}], None
    print(json.dumps({"eryx_encapsulated_only_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error,
                      "declarations": restrictions.declarations if restrictions else [],
                      "classes": restrictions.class_declarations if restrictions else [],
                      "effective_targets": restrictions.effective_targets if restrictions else {}},
                     ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
