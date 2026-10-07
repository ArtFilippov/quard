"""Pylint adapter using an input manifest instead of unbounded OS arguments."""
from __future__ import annotations
import argparse
from functools import wraps
import json
import sys
from pathlib import Path
import tomllib
sys.dont_write_bytecode = True

from quard._architecture_compat import install_architecture_compat, install_pylint_protocol_compat
from quard.pylint_config_utils import LEGACY_PLUGIN


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
    _ = parser.add_argument("--generated", action="store_true")
    args = parser.parse_args()
    files = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if not files:
        raise ValueError("No files for Pylint")
    if args.generated:
        with Path(args.config).open("rb") as stream:
            configured = tomllib.load(stream)["tool"]["pylint"]["main"]["load-plugins"]
        if LEGACY_PLUGIN in configured:
            raise ValueError("Generated Pylint config still loads the legacy plugin; "
                             + "reinstall Quard or run apply.py to migrate both profiles.")
    from pylint.lint import Run
    patch_pathlib_alias_check()
    if args.generated:
        install_pylint_protocol_compat()
    else:
        install_architecture_compat()
    result = Run(["--rcfile=" + args.config, "--output-format=json2", "--output=" + args.output,
                  "--jobs=1", *files], exit=False)
    return result.linter.msg_status
