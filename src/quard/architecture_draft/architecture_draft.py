"""Generate a reviewable Tach draft and explicitly accept reviewed boundaries."""

from __future__ import annotations

from typing import Any, Protocol


class ArchitectureDraft(Protocol):
    """Manage the draft, validation and acceptance of a consumer's architecture."""

    def package_name(self) -> str: ...

    def draft(self) -> tuple[dict[str, Any], list[str]]: ...

    def validate(self) -> dict[str, Any]: ...

    def initialize(self, *, accept: bool = False, force: bool = False
                   ) -> tuple[str, dict[str, Any]]: ...
