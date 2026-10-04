"""Consumer-owned readability, scope and native configuration contract."""
from __future__ import annotations

from typing import Any, Protocol


class ConsumerPolicy(Protocol):
    """Validate project settings and apply them without losing native policy."""

    def limits(self) -> dict[str, dict[str, int]]: ...

    def scope(self) -> dict[str, list[str]]: ...

    def files(self) -> dict[str, list[str]]: ...

    def constructor_purity(self) -> bool: ...

    def apply(self, *, initial: bool = False, plugins: list[str] | None = None,
              catalog: dict[str, Any] | None = None) -> list[str]: ...

    def limit_drift(self) -> list[str]: ...
