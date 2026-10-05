"""Bind existing native policy rendering and scope validation to one consumer."""
from __future__ import annotations

from pathlib import Path
from typing import Any, override

from quard import _policy
from quard.architecture_draft.generated_architecture_utils import generated, regenerate
from quard.consumer_policy.consumer_policy import ConsumerPolicy


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
    def checks(self) -> dict[str, bool]:
        return _policy.read_checks(self._root)

    @override
    def apply(self, *, initial: bool = False, plugins: list[str] | None = None,
              catalog: dict[str, Any] | None = None) -> list[str]:
        managed = generated(self._root)
        changed = _policy.configure(self._root, initial=initial, plugins=plugins, catalog=catalog)
        if managed and regenerate(self._root):
            changed.append("tach.toml")
        return changed

    @override
    def limit_drift(self) -> list[str]:
        return _policy.limit_drift(self._root)

    def scope_drift(self) -> list[str]:
        return _policy.scope_drift(self._root)
