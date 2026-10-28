"""Identify proven source Envelopes for Pylint's native method-count check."""
from __future__ import annotations

from pathlib import Path

from quard.contract_analysis.contract_utils import module_name
from quard.folder_analysis.folder_analysis import FolderAnalysis

ENVELOPES = "envelopes"
BASE = "base"
ASSEMBLY = "assembly"


def proven(ownership: FolderAnalysis, failures: list[dict[str, object]]) -> list[tuple[str, int]]:
    """Return source locations whose Envelope behavior has been proved.

    Args:
        ownership (FolderAnalysis): Analyzed source and stub declarations.
        failures (list[dict[str, object]]): Existing Envelope violations.

    Returns:
        list[tuple[str, int]]: Paths and lines of valid source Envelopes.
    """
    rejected = {(item["path"], item.get("symbol")) for item in failures}
    evidence: list[tuple[str, int]] = []
    for name, node, path in ownership.all_declarations():
        if not path.startswith("src/") or not path.endswith(".py"):
            continue
        if (ENVELOPES not in Path(path).parts[1:-1]
                or (path, name.removeprefix(module_name(path) + ".")) in rejected
                or (str(Path(path).with_suffix(".pyi")),
                    name.removeprefix(module_name(path) + ".")) in rejected):
            continue
        role = ownership.envelope_role(name, node, path)
        if ((role == BASE and ownership.delegation(name, node, path) is None)
                or (role == ASSEMBLY and ownership.assembly(node, path) is None)):
            evidence.append((path, node.lineno))
    return evidence
