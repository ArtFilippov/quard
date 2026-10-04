"""Apply the reviewed native plugin patches for the pinned version."""

from __future__ import annotations

from typing import override

from eryx import _architecture_compat as compat
from eryx.architecture_compatibility.architecture_compatibility import ArchitectureCompatibility


class PinnedArchitectureCompatibility(ArchitectureCompatibility):
    """Patch native checkers only once and reject unreviewed plugin versions."""

    @override
    def install(self) -> None:
        compat.install_pinned_plugin()
