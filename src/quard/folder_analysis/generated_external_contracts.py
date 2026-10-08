"""Exact external-base evidence for generated contract exports."""
from __future__ import annotations

import ast
from typing import override

from quard.contract_analysis.contract_utils import dotted, module_name
from quard.contract_analysis.value_factory_utils import ValueFactory, values
from quard.folder_analysis.folder_ownership import FolderOwnership

_NON_CONTRACT_BASES = frozenset({"builtins.object", "typing.Protocol",
                                 "typing_extensions.Protocol", "abc.ABC", "abc.ABCMeta",
                                 "typing.NamedTuple", "typing_extensions.NamedTuple"})


class GeneratedOwnership(FolderOwnership):
    """Keep externally inherited identities separate from local class roles."""

    def __init__(self, trees: dict[str, ast.Module]):
        """Index exact external bases in discovered source and stubs.

        Args:
            trees (dict[str, ast.Module]): Discovered parsed modules.
        """
        super().__init__(trees)
        self.value_evidence: dict[str, list[ValueFactory]] = {
            path: values(tree) for path, tree in trees.items()}
        for path, evidence in self.value_evidence.items():
            for value in evidence:
                if value.declaration is None:
                    continue
                name = f"{module_name(path)}.{value.declaration.name}"
                # A companion Protocol must not turn a concrete factory into an export.
                self.contracts.discard(name)
                self.declarations.append((name, value.declaration, path))
                if name not in self.nodes or path.endswith(".py"):
                    self.nodes[name] = value.declaration
                    self.paths[name] = path
        self._ambiguous: dict[str, set[str]] = {}
        for path, tree in trees.items():
            origins: dict[str, set[str | None]] = {}
            for statement in tree.body:
                if isinstance(statement, ast.Import):
                    for alias in statement.names:
                        name = alias.asname or alias.name.split(".")[0]
                        origins.setdefault(name, set()).add(alias.name if alias.asname else name)
                elif isinstance(statement, ast.ImportFrom):
                    module = module_name(path)
                    package = (module if path.endswith(("/__init__.py", "/__init__.pyi"))
                               else module.rpartition(".")[0])
                    parts = package.split(".")
                    prefix = parts[:len(parts) - statement.level + 1] if statement.level else []
                    target = ".".join([*prefix, statement.module] if statement.module else prefix)
                    for alias in statement.names:
                        origins.setdefault(alias.asname or alias.name, set()).add(
                            f"{target}.{alias.name}")
                elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.ClassDef,
                                            ast.FunctionDef, ast.AsyncFunctionDef)):
                    names = ([statement.name] if isinstance(statement, (ast.ClassDef, ast.FunctionDef,
                                                                         ast.AsyncFunctionDef))
                             else [target.id for target in (statement.targets if isinstance(
                                 statement, ast.Assign) else [statement.target])
                                   if isinstance(target, ast.Name)])
                    for name in names:
                        origins.setdefault(name, set()).add(None)
            self._ambiguous[path] = {name for name, values in origins.items() if len(values) > 1}

        grouped: dict[str, list[tuple[ast.ClassDef, str]]] = {}
        for name, node, path in self.all_declarations():
            grouped.setdefault(name, []).append((node, path))
        self.external_contracts: set[str] = set()
        self.conflicts: set[tuple[str, str]] = set()
        for name, declarations in grouped.items():
            evidence = [self._bases(name, node, path) for node, path in declarations]
            if any(bases is None for bases in evidence) or len({frozenset(bases or ())
                                                               for bases in evidence}) != 1:
                if any(node.bases for node, _ in declarations):
                    self.conflicts.update((name, path) for _, path in declarations)
                continue
            self.external_contracts.update(identity.partition("[")[0] for identity in
                                           (next(iter(evidence)) or ()))

    def _bases(self, name: str, node: ast.ClassDef, path: str) -> set[str] | None:
        found: set[str] = set()
        for base in node.bases:
            text = dotted(base)
            first = text.split(".")[0] if text else ""
            target = self.resolved(name, base, path)
            if first in self._ambiguous[path] or target is None or (
                    self.node_for(target) is None and self.local_module(target)):
                return None
            if self.node_for(target) is None and target not in _NON_CONTRACT_BASES:
                specialization = (f"[{ast.unparse(base.slice)}]" if isinstance(base, ast.Subscript)
                                  else "")
                found.add(target + specialization)
        return found

    @override
    def export_contract(self, target: str) -> bool:
        """Check an exact local or inherited external contract export.

        Args:
            target (str): Qualified export reference.

        Returns:
            bool: Whether the resolved type is a proven contract.
        """
        proven = self.provenance(target)
        return self.is_contract(proven) or proven in self.external_contracts

    @override
    def conflicting_base(self, name: str, path: str) -> bool:
        """Check whether declaration evidence is unproved or conflicting.

        Args:
            name (str): Qualified class name.
            path (str): Declaration file.

        Returns:
            bool: Whether the base needs a positioned finding.
        """
        return (name, path) in self.conflicts
