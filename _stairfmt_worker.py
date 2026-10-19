"""Check Stairfmt rules on the discovered Python source manifest."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from stairfmt import decode_source, issues_for, read_source
from quard._common import QuardError
from quard.policy_utils import read_settings, validate_stairfmt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    findings: list[dict[str, object]] = []
    error: str | None = None
    script = Path(__file__).resolve().parent / "stairfmt.py"
    try:
        script = script.relative_to(Path.cwd())
    except ValueError:
        pass
    suggestion = f"; запустите python {shlex.quote(str(script))} --fix --project ."
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError("Stairfmt manifest must list Python paths")
        limits = validate_stairfmt(read_settings(Path.cwd()).get("stairfmt", {}))
        for path in paths:
            try:
                source, _ = decode_source(read_source(Path(path)))
                tree = ast.parse(source, filename=path)
                findings.extend({"path": path, "line": issue.line, "column": issue.col,
                                 "rule": issue.code, "message": issue.message + suggestion}
                                 for issue in issues_for(tree, source, **limits))
            except (OSError, UnicodeError, SyntaxError, LookupError, RecursionError, ValueError) as exc:
                rule = "STAIRFMT_PARSE_ERROR" if isinstance(exc, SyntaxError) else "STAIRFMT_EXECUTION_ERROR"
                error = error or rule
                findings.append({"path": path, "rule": rule,
                                 "message": (f"слишком глубокое выражение: {exc}"
                                             if isinstance(exc, RecursionError) else str(exc))})
    except (OSError, UnicodeError, ValueError, TypeError, QuardError) as exc:
        error = "STAIRFMT_EXECUTION_ERROR"
        findings.append({"rule": error, "message": str(exc)})
    print(json.dumps({"quard_stairfmt_adapter": 1, "diagnostics": findings,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if findings else 0)


if __name__ == "__main__":
    sys.exit(main())
