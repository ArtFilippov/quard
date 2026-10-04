"""Check folder ownership of customer contracts and implementations."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys

from eryx._class_contracts_worker import Contracts, module_name


def snake_case(name: str) -> str:
    """Convert a class name, including acronym boundaries, to a folder name."""
    name = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


class FolderOwnership(Contracts):
    """Resolve the local contract selected by each class's explicit ancestry."""

    def __init__(self, trees: dict[str, ast.Module]):
        super().__init__(trees)
        self.contracts = {name for name in self.nodes if self.declaration(name)}

    def owners(self, name: str, seen: frozenset[str] = frozenset(),
               source: tuple[ast.ClassDef, str] | None = None) -> tuple[set[str], bool]:
        """Return nearest local contracts and whether ancestry is unresolved."""
        if name in seen:
            return set(), True
        if name in self.contracts and source is None:
            return {name}, False
        node, path = source or (self.nodes[name], self.paths[name])
        owners: set[str] = set()
        unknown = False
        for base in node.bases:
            resolved = self.resolved(name, base, path)
            if resolved in self.nodes:
                inherited, uncertain = self.owners(resolved, seen | {name})
                owners.update(inherited)
                unknown |= uncertain
            elif resolved is None or self.local_module(resolved):
                unknown = True
        return owners, unknown

    def folder(self, contract: str, profile: str) -> Path:
        """Find the expected contract directory in the current profile."""
        path = Path(self.paths[contract])
        parent = Path(profile, *path.parts[1:-1])
        expected = snake_case(contract.rpartition(".")[2])
        return parent if parent.name == expected else parent / expected

    def contract_ancestors(self, name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        """Collect contracts named by a combined contract's local bases."""
        if name in seen:
            return set()
        ancestors = {name}
        for base in self.nodes[name].bases:
            parent = self.resolved(name, base)
            if parent in self.contracts:
                ancestors.update(self.contract_ancestors(parent, seen | {name}))
        return ancestors

    def inherits_envelope(self, name: str, node: ast.ClassDef, path: str,
                          owner: str, seen: frozenset[str] = frozenset()) -> bool:
        """Find a correctly placed base envelope in the local concrete ancestry."""
        if name in seen:
            return False
        base_name = owner.rpartition(".")[2] + "Envelope"
        for base in node.bases:
            parent = self.resolved(name, base, path)
            if parent not in self.nodes or parent in self.contracts:
                continue
            parent_node = self.nodes[parent]
            parent_path = self.paths[parent]
            parent_file = Path(parent_path)
            expected = self.folder(owner, parent_file.parts[0]) / "envelopes"
            if (parent_node.name == base_name and parent_file.parent == expected
                    and parent_file.stem == snake_case(base_name)
                    and any(self.resolved(parent, direct) == owner
                            for direct in parent_node.bases)):
                return True
            if self.inherits_envelope(parent, parent_node, parent_path, owner, seen | {name}):
                return True
        return False

    def envelope_violation(self, name: str, node: ast.ClassDef, path: str,
                           owner: str | None) -> tuple[str, str] | None:
        """Check the reserved direct envelopes folder and its named classes."""
        current = Path(path)
        inside = "envelopes" in current.parts[1:-1]
        if not inside and (owner is None or not node.name.endswith("Envelope")):
            return None
        if inside and not node.name.endswith("Envelope"):
            return "ENVELOPE_CLASS", f"{name}: only named envelope classes belong in envelopes/."
        if owner is None:
            return "ENVELOPE_INHERITANCE", (f"{name}: envelope must inherit its parent "
                                            "local contract through the base envelope.")
        expected = self.folder(owner, current.parts[0]) / "envelopes"
        if current.parent != expected:
            return "ENVELOPE_FOLDER", f"{name}: place this envelope in '{expected}/'."
        if current.stem != snake_case(node.name):
            return "ENVELOPE_FILE", (f"{name}: declare this envelope in "
                                     f"'{expected / (snake_case(node.name) + current.suffix)}'.")
        base_name = owner.rpartition(".")[2] + "Envelope"
        if node.name == base_name:
            valid = any(self.resolved(name, base, path) == owner for base in node.bases)
        else:
            valid = self.inherits_envelope(name, node, path, owner)
        if not valid:
            return "ENVELOPE_INHERITANCE", (f"{name}: inherit {owner} directly as "
                                            f"{base_name}, or subclass that base envelope.")
        return None

    def violations(self) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for name, node, path in self.declarations:
            module = module_name(path)
            symbol = name.removeprefix(module + ".")
            location = {"path": path, "line": node.lineno,
                        "column": node.col_offset + 1, "symbol": symbol}
            current = Path(path)
            if self.declaration(name, source=(node, path)):
                expected = snake_case(node.name)
                if current.parent.name != expected or current.stem != expected:
                    results.append({**location, "rule": "CONTRACT_FOLDER",
                                    "message": f"{name}: declare this contract in "
                                    f"'{expected}/{expected}{current.suffix}' (folder and module "
                                    "must match the contract name)."})
                continue
            local_bases = [self.resolved(name, base, path) for base in node.bases]
            concrete_bases = [base for base in local_bases
                              if base in self.nodes and base not in self.contracts]
            direct_contracts = [base for base in local_bases if base in self.contracts]
            owners, unknown = self.owners(name, source=(node, path))
            if len(concrete_bases) > 1:
                results.append({**location, "rule": "COMPETING_LOCAL_BASES",
                                "message": f"{name}: select one local concrete base and inherit "
                                "a separate combined local contract for the others."})
                continue
            if len(direct_contracts) > 1:
                results.append({**location, "rule": "COMPETING_LOCAL_BASES",
                                "message": f"{name}: declare a separate combined local contract "
                                "instead of inheriting multiple local contracts here."})
                continue
            if len(owners) > 1 and not direct_contracts:
                results.append({**location, "rule": "COMPETING_LOCAL_BASES",
                                "message": f"{name}: inherit a separate combined local contract "
                                "and select one local base for this implementation."})
                continue
            # An explicit combined contract selects the owner over a concrete base.
            if len(direct_contracts) == 1:
                inherited = owners - {direct_contracts[0]}
                if inherited - self.contract_ancestors(direct_contracts[0]):
                    results.append({**location, "rule": "COMPETING_LOCAL_BASES",
                                    "message": f"{name}: combined contract {direct_contracts[0]} "
                                    "must inherit the selected local base's contract."})
                    continue
                owners = {direct_contracts[0]}
            if unknown or len(owners) != 1:
                if unknown:
                    results.append({**location, "rule": "UNRESOLVED_FOLDER_OWNER",
                                    "message": f"{name}: resolve local ancestry before its "
                                    "implementation folder can be verified."})
                elif violation := self.envelope_violation(name, node, path, None):
                    rule, message = violation
                    results.append({**location, "rule": rule, "message": message})
                continue
            owner = next(iter(owners))
            if violation := self.envelope_violation(name, node, path, owner):
                rule, message = violation
                results.append({**location, "rule": rule, "message": message})
                continue
            if "envelopes" in current.parts[1:-1] or node.name.endswith("Envelope"):
                continue
            expected = self.folder(owner, current.parts[0])
            if current.parent != expected and expected not in current.parents:
                results.append({**location, "rule": "IMPLEMENTATION_FOLDER",
                                "message": f"{name}: move this implementation into "
                                f"'{expected}/' (owned by {owner})."})
        return results


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    trees: dict[str, ast.Module] = {}
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid contract folder source path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "CONTRACT_FOLDERS_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
    diagnostics.extend(FolderOwnership(trees).violations())
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Contract folder manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), paths)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        error = "CONTRACT_FOLDERS_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc), "is_infrastructure_error": True}]
    print(json.dumps({"eryx_contract_folders_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
