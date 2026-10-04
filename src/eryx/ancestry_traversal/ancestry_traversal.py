"""Combined contract for declaration-time ancestry and restricted-call traversal."""
from __future__ import annotations

from typing import Protocol

from eryx.restricted_calls.restricted_calls import RestrictedCalls


class AncestryTraversal(RestrictedCalls, Protocol):
    """Expose resolved bases after visiting a module."""

    @property
    def parents(self) -> dict[str, tuple[str | None, ...]]: ...
