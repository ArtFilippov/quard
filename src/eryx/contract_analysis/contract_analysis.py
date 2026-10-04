"""Local interface shared by contract, folder and test-placement analyzers."""
from __future__ import annotations

import ast
from abc import ABC, abstractmethod
from pathlib import Path


class ContractAnalysis(ABC):
    """Operations exposed by the contract analysis family."""

    @abstractmethod
    def __init__(self, *args: object, **kwargs: object) -> None:
        """Create an analyzer.

        Args:
            *args (object): Inputs for the concrete analyzer.
            **kwargs (object): Named inputs for the concrete analyzer.
        """

    def provenance(self, target: str) -> str: ...
    def resolved(self, name: str, base: ast.expr, path: str | None = None) -> str | None: ...
    def has_contract(self, name: str, seen: frozenset[str] = frozenset(),
                     source: tuple[ast.ClassDef, str] | None = None) -> bool: ...
    def local_module(self, resolved: str) -> bool: ...
    def inherited_api(self, name: str, seen: frozenset[str] = frozenset(),
                      source: tuple[ast.ClassDef, str] | None = None) -> tuple[set[str], bool]: ...
    def public_members(self, name: str, node: ast.ClassDef,
                       path: str) -> dict[str, ast.stmt]: ...
    def declaration(self, name: str, seen: frozenset[str] = frozenset(),
                    source: tuple[ast.ClassDef, str] | None = None) -> bool: ...
    def inherited_implementations(self, name: str, seen: frozenset[str] | None = None,
                                   source: tuple[ast.ClassDef, str] | None = None
                                   ) -> dict[str, str]: ...
    def method_overrides(self, name: str, node: ast.ClassDef,
                         path: str) -> list[dict[str, object]]: ...
    def diagnostics(self) -> list[dict[str, object]]: ...
    def owners(self, name: str, seen: frozenset[str] = frozenset(),
               source: tuple[ast.ClassDef, str] | None = None) -> tuple[set[str], bool]:
        raise NotImplementedError

    def folder(self, contract: str, profile: str) -> Path:
        raise NotImplementedError

    def contract_ancestors(self, name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        raise NotImplementedError
    def inherits_envelope(self, name: str, node: ast.ClassDef, path: str,
                          owner: str, seen: frozenset[str] = frozenset()) -> bool:
        raise NotImplementedError
    def envelope_violation(self, name: str, node: ast.ClassDef, path: str,
                           owner: str | None) -> tuple[str, str] | None:
        raise NotImplementedError

    def folder_violations(self) -> list[dict[str, object]]:
        raise NotImplementedError

    def violations(self) -> list[dict[str, object]]:
        raise NotImplementedError

    def mirrored_folder(self, path: str) -> Path:
        raise NotImplementedError
