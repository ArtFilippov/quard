"""Pylint adapter using an input manifest instead of unbounded OS arguments."""
from __future__ import annotations
import argparse
import ast
from functools import wraps
import json
import sys
from pathlib import Path
import tomllib
sys.dont_write_bytecode = True

from quard._architecture_compat import install_architecture_compat, install_pylint_protocol_compat
from quard.pylint_config_utils import LEGACY_PLUGIN


def patch_envelope_methods(paths: list[str]) -> None:
    """Exempt proven production Envelopes from Pylint's method-count heuristic.

    Args:
        paths (list[str]): Discovered production source and stub paths.
    """
    if not any("envelopes" in Path(path).parts[1:-1] for path in paths):
        return
    from pylint.checkers import design_analysis

    from quard._common import read_json
    from quard.architecture_draft.generated_selection_utils import IDENTITY, MARKER, generated
    from quard.folder_analysis.folder_ownership import FolderOwnership
    from quard.folder_analysis.generated_external_contracts import GeneratedOwnership
    from quard.contract_analysis.contracts import module_name
    from quard.policy_utils import gateways, read_scope

    trees = {path: ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
             for path in paths}
    excluded = gateways(Path.cwd())
    ownership = (GeneratedOwnership(trees, excluded) if generated(Path.cwd()) and
                 read_json(Path(MARKER)) == IDENTITY else FolderOwnership(trees, excluded))
    failures = ownership.base_violations(frozenset(read_scope(Path.cwd())["test_directory_names"]))
    rejected = {(item["path"], item.get("symbol")) for item in failures}
    proven: set[tuple[str, int]] = set()
    for name, node, path in ownership.declarations:
        if (not path.endswith(".py") or "envelopes" not in Path(path).parts[1:-1]
                or (path, name.removeprefix(module_name(path) + ".")) in rejected
                or (str(Path(path).with_suffix(".pyi")),
                    name.removeprefix(module_name(path) + ".")) in rejected):
            continue
        role = ownership.envelope_role(name, node, path)
        if ((role == "base" and ownership.delegation(name, node, path) is None)
                or (role == "assembly" and ownership.assembly(node, path) is None)):
            proven.add((str(Path(path).resolve()), node.lineno))

    original = design_analysis._is_exempt_from_public_methods

    @wraps(original)
    def exempt(node):
        return (node.root().file, node.lineno) in proven or original(node)

    design_analysis._is_exempt_from_public_methods = exempt


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
    _ = parser.add_argument("--envelopes-manifest")
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
        install_architecture_compat(files)
    if args.envelopes_manifest:
        patch_envelope_methods(json.loads(Path(args.envelopes_manifest).read_text(
            encoding="utf-8")))
    result = Run(["--rcfile=" + args.config, "--output-format=json2", "--output=" + args.output,
                  "--jobs=1", *files], exit=False)
    return result.linter.msg_status
