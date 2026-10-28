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
from quard.folder_analysis.envelope_evidence_utils import proven
from quard.pylint_config_utils import LEGACY_PLUGIN
from quard.pylint_imports_utils import install as install_imports
from quard.pylint_similarity_utils import install as install_similarity


def patch_envelope_methods(paths: list[str], evidence: Path | None = None) -> None:
    """Exempt proven production Envelopes from Pylint's method-count heuristic.

    Args:
        paths (list[str]): Discovered production source and stub paths.
        evidence (Path | None, default=None): Reusable proof from the folder job.
    """
    if not any("envelopes" in Path(path).parts[1:-1] for path in paths):
        return
    from pylint.checkers import design_analysis
    if evidence is not None and evidence.exists():
        locations = json.loads(evidence.read_text(encoding="utf-8"))
    else:
        from quard._common import read_json
        from quard.architecture_draft.generated_selection_utils import IDENTITY, MARKER, generated
        from quard.folder_analysis.folder_ownership import FolderOwnership
        from quard.folder_analysis.generated_external_contracts import GeneratedOwnership
        from quard.policy_utils import gateways, read_scope

        trees = {path: ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
                 for path in paths}
        excluded = gateways(Path.cwd())
        ownership = (GeneratedOwnership(trees, excluded) if generated(Path.cwd()) and
                     read_json(Path(MARKER)) == IDENTITY else FolderOwnership(trees, excluded))
        failures = ownership.base_violations(frozenset(read_scope(Path.cwd())["test_directory_names"]))
        locations = proven(ownership, failures)
    approved = {(str(Path(path).resolve()), line) for path, line in locations}

    original = design_analysis._is_exempt_from_public_methods

    @wraps(original)
    def exempt(node):
        return (node.root().file, node.lineno) in approved or original(node)

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
    _ = parser.add_argument("--envelope-evidence", type=Path)
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
    install_similarity()
    install_imports()
    patch_pathlib_alias_check()
    if args.generated:
        install_pylint_protocol_compat()
    else:
        install_architecture_compat(files)
    if args.envelopes_manifest:
        patch_envelope_methods(json.loads(Path(args.envelopes_manifest).read_text(
            encoding="utf-8")), args.envelope_evidence)
    result = Run(["--rcfile=" + args.config, "--output-format=json2", "--output=" + args.output,
                  "--jobs=1", *files], exit=False)
    return result.linter.msg_status
