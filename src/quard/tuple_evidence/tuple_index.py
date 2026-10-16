"""Index first-party aliases without importing consumer modules."""
from __future__ import annotations

import ast
from typing import override

from quard.marker_utils import module_name
from quard.tuple_evidence.tuple_evidence import EMPTY, TupleEvidence
from quard.tuple_evidence.tuple_index_utils import TUPLE, TUPLES, bindings, children


class TupleIndex(TupleEvidence):
    """Resolve source or stub alias chains without importing consumer modules.

    Args:
        trees (dict[str, ast.Module]): Source or stub declarations.
    """

    def __init__(self, trees: dict[str, ast.Module]):
        super().__init__()
        self.bindings = {module_name(path): bindings(path, tree) for path, tree in trees.items()}

    @override
    def reference(self, node: ast.expr, module: str, local: frozenset[str]) -> str | None:
        """Resolve a named type or module through statically established imports.

        Args:
            node (ast.expr): Candidate reference.
            module (str): Current module.
            local (frozenset[str]): Enclosing lexical shadowing.

        Returns:
            str | None: Proven origin or unknown.
        """
        if isinstance(node, ast.Name):
            name: str | None = None
            if node.id not in local:
                binding = self.bindings.get(module, {}).get(node.id)
                if isinstance(binding, str):
                    name = binding
                elif node.id in self.bindings.get(module, {}):
                    name = f"{module}.{node.id}"
                elif node.id == TUPLE:
                    name = "builtins.tuple"
            return name
        if isinstance(node, ast.Attribute):
            base = self.reference(node.value, module, local)
            return f"{base}.{node.attr}" if base else None
        return None

    @override
    def contains(self, node: ast.expr, module: str,
                 local: frozenset[str] = EMPTY,
                 seen: frozenset[str] = EMPTY) -> bool:
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = self.reference(node, module, local)
            return name is not None and self.alias(name, seen)
        if isinstance(node, ast.Call):
            return False
        return any(self.contains(child, module, local, seen) for child in children(
            node, self.reference(node.value, module, local) if isinstance(node, ast.Subscript)
            else None))

    @override
    def alias(self, name: str, seen: frozenset[str]) -> bool:
        """Follow only proven import and alias bindings, stopping on cycles.

        Args:
            name (str): Qualified alias.
            seen (frozenset[str]): Previously visited aliases.

        Returns:
            bool: Whether the alias contains a raw tuple.
        """
        if name in TUPLES or name in seen:
            return name in TUPLES
        owner, _, member = name.rpartition(".")
        binding = self.bindings.get(owner, {}).get(member)
        if isinstance(binding, str):
            return self.alias(binding, seen | {name})
        if isinstance(binding, ast.expr):
            return self.contains(binding, owner, frozenset(), seen | {name})
        return False
