"""Check direct and indirect lambda injection at production composition sites."""
from __future__ import annotations

import argparse
import ast
import builtins
import json
from pathlib import Path
from typing import cast

from quard.constructor_origins.origins import Origins
from quard.constructor_origins.origins_utils import builtin
from quard.lambda_injection.lambda_sites import LambdaSites

RULE = "LAMBDA_INJECTION"
PARSE_ERROR = "LAMBDA_INJECTION_PARSE_ERROR"
EXECUTION_ERROR = "LAMBDA_INJECTION_EXECUTION_ERROR"


def assembly_site(path: str) -> bool:
    """Production main descendants and files directly in a group's envelopes."""
    parts = Path(path).parts
    return (len(parts) >= 3 and parts[0] == "src" and
            ("main" in parts[1:-1] or
             len(parts) >= 5 and parts[-2] == "envelopes"))


def analyze(root: Path, paths: list[object]) -> tuple[list[dict[str, object]], str | None]:
    trees: dict[str, ast.Module] = {}
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or not path.startswith("src/") or Path(path).suffix != ".py"):
            raise ValueError(f"Invalid lambda injection source path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = PARSE_ERROR
            diagnostics.append({"rule": PARSE_ERROR, "path": path,
                                "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc)})
    origins = Origins(trees)
    for path, tree in trees.items():
        if assembly_site(path):
            env = {name: builtin(name) for name in vars(builtins)}
            checker = LambdaSites(path, origins)
            checker.suite(tree.body, env)
            diagnostics.extend(checker.diagnostics)
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description="Check lambda injection at assembly sites.")
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Lambda injection manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), cast(list[object], paths))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        error = EXECUTION_ERROR
        diagnostics = [{"rule": error, "message": str(exc)}]
    print(json.dumps({"quard_lambda_injection_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)
