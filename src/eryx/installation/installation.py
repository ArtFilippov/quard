"""Installation transaction contract for consumer-owned state and tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class Installation(Protocol):
    """Plan or apply an installation while restoring managed files on failure."""

    def install(self, root: Path, tool: Path, *, dry_run: bool = False,
                progress: bool = True, extras: list[str] | None = None) -> dict[str, Any]:
        ...
