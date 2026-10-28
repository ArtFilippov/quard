"""Render Pylint's mode-dependent policy notes and legacy plugin settings."""
from __future__ import annotations

from typing import Any

LEGACY_PLUGIN = "clean_architecture_linter"


def official_plugins(plugins: list[str]) -> list[str]:
    return [name for name in plugins if name != LEGACY_PLUGIN]


def clean_config(layer_dirs: dict[str, str]) -> dict[str, Any]:
    # Plugin 1.2.2 reads Layer -> patterns, not the reverse from some README examples.
    mapping: dict[str, list[str]] = {}
    for name, layer in layer_dirs.items():
        mapping.setdefault(layer, []).append(name)
    return {"project_type": "generic", "visibility_enforcement": True, "layer_map": mapping}


def policy_notes(plugins: list[str], production_disabled: dict[str, str],
                 test_disabled: dict[str, str], test_arch_disabled: list[str],
                 enabled: list[str]) -> dict[str, Any]:
    reviewed = LEGACY_PLUGIN in plugins
    return {
        "production_disabled": production_disabled,
        "test_additional_disabled": test_disabled,
        "test_architecture_disabled_if_registered": test_arch_disabled if reviewed else [],
        "pyright_disabled": {
            "reportImportCycles": "Pyright and Pylint cycle findings are disabled; Tach checks configured module boundaries and cycles."},
        "pylint_enabled": enabled,
        "pylint_tests_enabled": [name for name in enabled if name not in test_disabled],
        "official_extensions_loaded": [name for name in plugins
                                       if name.startswith("pylint.extensions.")],
        "official_extension_not_loaded": {
            "pylint.extensions.docparams":
            "Google docstrings validated by pydoclint; reviewed mode "
            + "also has plugin message-ID collisions. Not a claim of identical rule sets."},
        "kept_partial_overlaps": [],
        "deferred": ["Blanket ban on internal comments", "Function length in physical lines",
                     "Automatic dependency-community discovery",
                     "Universal Protocol/ABC-only proof"],
        "not_provided": ["Formatter",
                         "Natural-language spell checking (requires Enchant and dictionaries)",
                         "Runtime tests", "Dependency vulnerability scanner"],
    }
