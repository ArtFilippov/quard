"""Collect declaration-time ancestry using restricted-call binding traversal."""
from __future__ import annotations

from typing import override

from eryx.field_ownership.ownership import Ownership
from eryx.ancestry_traversal.ancestry_traversal import AncestryTraversal
from eryx.restricted_calls.calls import Calls
from eryx.restricted_targets.restricted_targets import RestrictedTargets


class Ancestry(Calls, AncestryTraversal):
    """Capture class bases with declaration-time aliases and nested lexical scopes."""

    def __init__(self, restrictions: RestrictedTargets, path: str,
                 ownership: Ownership):
        """Initialize the ancestry map and the shared traversal.

        Args:
            restrictions (RestrictedTargets): Resolved call targets and ownership.
            path (str): Path to the visited source module.
            ownership (Ownership): Indexed source ownership.
        """
        super().__init__(restrictions, path, ownership, parents={})

    @property
    @override
    def parents(self) -> dict[str, tuple[str | None, ...]]:
        return self._parent_map()
