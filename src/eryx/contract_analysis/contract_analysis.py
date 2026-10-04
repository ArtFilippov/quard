"""Local interface for class-contract resolution."""
from __future__ import annotations

import ast
from typing import Protocol

_EMPTY_NAMES: frozenset[str] = frozenset()


class ContractAnalysis(Protocol):
    """Resolve class ancestry, public members and contract diagnostics."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Create an analyzer.

        Args:
            *args (object): Inputs for the concrete analyzer.
            **kwargs (object): Named inputs for the concrete analyzer.
        """
        super().__init__()

    def provenance(self, target: str) -> str: ...
    def resolved(self, name: str, base: ast.expr, path: str | None = None) -> str | None: ...
    def has_contract(self, name: str, seen: frozenset[str] = _EMPTY_NAMES,
                     source: tuple[ast.ClassDef, str] | None = None) -> bool: ...
    def local_module(self, resolved: str) -> bool: ...
    def inherited_api(self, name: str, seen: frozenset[str] = _EMPTY_NAMES,
                      source: tuple[ast.ClassDef, str] | None = None) -> tuple[set[str], bool]: ...
    def public_members(self, name: str, node: ast.ClassDef,
                       path: str) -> dict[str, ast.stmt]: ...
    def declaration(self, name: str, seen: frozenset[str] = _EMPTY_NAMES,
                    source: tuple[ast.ClassDef, str] | None = None) -> bool: ...
    def inherited_implementations(self, name: str, seen: frozenset[str] | None = None,
                                   source: tuple[ast.ClassDef, str] | None = None
                                   ) -> dict[str, str]: ...
    def method_overrides(self, name: str, node: ast.ClassDef,
                         path: str) -> list[dict[str, object]]: ...
    def diagnostics(self) -> list[dict[str, object]]: ...
    def all_declarations(self) -> tuple[tuple[str, ast.ClassDef, str], ...]: ...
    def path_for(self, name: str) -> str: ...
