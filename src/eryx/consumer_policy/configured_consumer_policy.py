"""Bind existing native policy rendering and scope validation to one consumer."""
from __future__ import annotations

from pathlib import Path
from typing import Any, override

from eryx import _policy
from eryx.consumer_policy.consumer_policy import ConsumerPolicy


class ConfiguredConsumerPolicy(ConsumerPolicy):
    """Operate on the selected project's settings, never on the tool checkout.

    Args:
        root (Path): Selected consumer project.
    """

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root

    @override
    def limits(self) -> dict[str, dict[str, int]]:
        return _policy.read_limits(self._root)

    @override
    def scope(self) -> dict[str, list[str]]:
        return _policy.read_scope(self._root)

    @override
    def files(self) -> dict[str, list[str]]:
        return _policy.discover_files(self._root, self.scope())

    @override
    def constructor_purity(self) -> bool:
        return _policy.read_checks(self._root)

    @override
    def apply(self, *, initial: bool = False, plugins: list[str] | None = None,
              catalog: dict[str, Any] | None = None) -> list[str]:
        return _policy.configure(self._root, initial=initial, plugins=plugins, catalog=catalog)

    @override
    def limit_drift(self) -> list[str]:
        return _policy.limit_drift(self._root)
