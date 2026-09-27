"""Pylint adapter using an input manifest instead of unbounded OS arguments."""
from __future__ import annotations
import argparse
from functools import wraps
import json
import sys
sys.dont_write_bytecode = True
from pathlib import Path

from _architecture_compat import install_architecture_compat


def patch_pathlib_alias_check() -> None:
    """Keep Pylint 4.0.8's file checks working for aliased pathlib methods.

    The upstream checker reads node.func.attrname even when inference resolves a
    Name such as ``write_text = Path.write_text; write_text(...)``. Supply the
    already resolved function name for that call only; run every native check.
    """
    from astroid import nodes
    from pylint.checkers.stdlib import PATHLIB_MODULE, StdlibChecker

    original = StdlibChecker._check_open_call

    @wraps(original)
    def check_open_call(self, node, open_module, func_name):
        function = node.func
        if isinstance(function, nodes.Name) and open_module in PATHLIB_MODULE:
            function.attrname = func_name
            try:
                return original(self, node, open_module, func_name)
            finally:
                del function.attrname
        return original(self, node, open_module, func_name)

    StdlibChecker._check_open_call = check_open_call


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--notebooks")
    args = parser.parse_args()
    files = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if not files:
        raise ValueError("No files for Pylint")
    from pylint.lint import Run
    if args.notebooks:
        from _notebook_checker import configure_notebooks
        configure_notebooks(json.loads(Path(args.notebooks).read_text(encoding="utf-8")))
    patch_pathlib_alias_check()
    install_architecture_compat()
    result = Run(["--rcfile=" + args.config, "--output-format=json2", "--output=" + args.output,
                  "--jobs=1", *files], exit=False)
    return result.linter.msg_status


if __name__ == "__main__":
    sys.exit(main())
