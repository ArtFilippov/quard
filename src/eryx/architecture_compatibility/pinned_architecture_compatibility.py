"""Apply the reviewed native plugin patches for the pinned version."""

from __future__ import annotations

from importlib.metadata import version
from typing import override

from eryx.architecture_compatibility.architecture_compatibility import ArchitectureCompatibility


class PinnedArchitectureCompatibility(ArchitectureCompatibility):
    """Patch native checkers only once and reject unreviewed plugin versions."""

    @override
    def install(self) -> None:
        from eryx import _architecture_compat as compat

        if compat._installed:
            return
        installed_version = version("clean-architecture-linter-plugin")
        if installed_version != "1.2.2":
            raise RuntimeError(f"Review architecture compatibility for plugin {installed_version}")
        compat.install_pylint_protocol_compat()

        from clean_architecture_linter.config import ConfigurationLoader
        from clean_architecture_linter.checks.boundaries import ResourceChecker, VisibilityChecker
        from clean_architecture_linter.checks.contracts import ContractChecker
        from clean_architecture_linter.checks.dependencies import DependencyChecker
        from clean_architecture_linter.checks.design import DesignChecker
        from clean_architecture_linter.checks.patterns import CouplingChecker

        compat._patch_layers(ConfigurationLoader)
        compat._patch_imports(ResourceChecker, DependencyChecker)
        compat._patch_contracts(ContractChecker)
        compat._patch_design(DesignChecker)
        compat._patch_visibility(VisibilityChecker)
        compat._patch_coupling(CouplingChecker)
        compat._installed = True
