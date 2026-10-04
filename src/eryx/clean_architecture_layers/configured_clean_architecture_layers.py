"""Persist reviewed Tach layers in the consumer's native clean-arch configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, override

import tomlkit

from eryx._common import atomic_write
from eryx._policy import LAYERS
from eryx.clean_architecture_layers.clean_architecture_layers import CleanArchitectureLayers


class ConfiguredCleanArchitectureLayers(CleanArchitectureLayers):
    """Retain unrelated pyproject settings and comments when syncing layers."""

    @override
    def project(self, tach: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
        definitions: list[dict[str, str]] = []
        infra: list[str] = []
        for module in tach.get("modules", []):
            name, layer = module.get("path"), module.get("layer")
            if not name or layer not in LAYERS:
                continue
            definitions.append({"module": name, "name": layer})
            if layer == "Infrastructure":
                infra.append(name)
        definitions.sort(key=lambda item: (-len(item["module"]), item["module"]))
        return definitions, sorted(infra)

    @override
    def sync(self, root: Path, tach: dict[str, Any]) -> None:
        path = root / "pyproject.toml"
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        config = doc.setdefault("tool", {}).setdefault("clean-arch", {})
        config["layers"], config["infrastructure_modules"] = self.project(tach)
        atomic_write(path, tomlkit.dumps(doc), root)
