"""Collect declaration-time ancestry using restricted-call binding traversal."""
from __future__ import annotations

from eryx.restricted_calls.calls import Calls
from eryx.restricted_targets.restricted_targets import RestrictedTargets


class Ancestry(Calls):
    """Capture class bases with declaration-time aliases and nested lexical scopes."""

    def __init__(self, restrictions: RestrictedTargets, path: str):
        self.parents: dict[str, tuple[str | None, ...]] = {}
        super().__init__(restrictions, path, parents=self.parents)
