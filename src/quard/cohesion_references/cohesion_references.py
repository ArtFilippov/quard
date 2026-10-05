"""Operations used while collecting references from a method body."""
from __future__ import annotations

import ast
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quard.cohesion_references.method_references import MethodReferences


class CohesionReferences(ABC):
    """Collect calls and fields while respecting deferred nested scopes."""

    @abstractmethod
    def __init__(self, *args: object, **kwargs: object) -> None:
        """Create a reference visitor.

        Args:
            *args (object): Receiver and method names for the concrete visitor.
            **kwargs (object): Named visitor inputs.
        """

    @abstractmethod
    def receiver_kind(self, name: str) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def connects_to(self, other: MethodReferences, method: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def call(self, node: ast.Call) -> None:
        raise NotImplementedError

    @abstractmethod
    def attribute(self, node: ast.Attribute) -> None:
        raise NotImplementedError
