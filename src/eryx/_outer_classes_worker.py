"""Check that each discovered Python source or stub has at most one outer class."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import cast

from eryx.outer_classes.parsed_outer_classes import ParsedOuterClasses

def outer_classes(tree: ast.Module) -> list[ast.ClassDef]:
    """Count classes unless an enclosing class contains the declaration."""
    return ParsedOuterClasses().collect(tree)


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    return ParsedOuterClasses().analyze(root, paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths: object = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Outer-class manifest must be a list of Python paths")
        values = cast(list[object], paths)
        if not all(isinstance(path, str) for path in values):
            raise ValueError("Outer-class manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), [path for path in values if isinstance(path, str)])
    except (OSError, ValueError, TypeError) as exc:
        error = "OUTER_CLASSES_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc), "is_infrastructure_error": True}]
    print(json.dumps({"eryx_outer_classes_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
