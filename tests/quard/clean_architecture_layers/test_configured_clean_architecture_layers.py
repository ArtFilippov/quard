"""Regression tests for the reviewed Tach-to-clean-architecture layer mapping."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import tomllib
from typing import override
import unittest


QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))

from quard.clean_architecture_layers.configured_clean_architecture_layers import (
    ConfiguredCleanArchitectureLayers,
)
from quard.init_architecture import sync_clean_arch


class TestConfiguredCleanArchitectureLayers(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path(self.enterContext(
            TemporaryDirectory(prefix="architecture-layers-")))
        self.path: Path = self.root / "pyproject.toml"
        self.unmanaged: str = (
            '# Preserve project policy and its rationale.\n'
            '[project]\nname = "sample"\n'
            '[tool.pyright]\nstubPath = "src/stubs" # Local library declarations.\n'
            '[tool.pylint.design]\n'
            'ignored-parents = ["sample.ForeignBase"] # Reviewed ancestor policy.\n'
        )
        self.path.write_text(
            self.unmanaged
            + '[tool.clean-arch]\nproject_type = "generic"\n'
            'visibility_enforcement = true # Keep enforcement enabled.\n'
            'infrastructure_modules = ["sample.old_adapter"]\n'
            '[tool.clean-arch.layer_map]\n'
            'Bootstrap = ["bootstrap", "composition"]\n'
            'Interface = ["interfaces", "interface"]\n'
            '[[tool.clean-arch.layers]]\nmodule = "sample.stale"\nname = "Interface"\n',
            encoding="utf-8",
        )
        self.layers: dict[str, str] = {
            "sample": "Domain",
            "sample.contracts": "Domain",
            "sample.execution": "UseCase",
            "sample.transport": "Infrastructure",
            "sample.terminal": "Interface",
            "sample.wiring": "Bootstrap",
        }
        self.tach: dict[str, list[dict[str, str]]] = {
            "modules": [{"path": name, "layer": layer} for name, layer in self.layers.items()]
        }

    def test_preserves_bootstrap_and_orders_specific_layers_before_the_package(self):
        layers = ConfiguredCleanArchitectureLayers()
        projected, infrastructure = layers.project(self.tach)
        self.assertEqual(projected[0], {"module": "sample.contracts", "name": "Domain"})
        self.assertEqual(infrastructure, ["sample.transport"])
        layers.sync(self.root, self.tach)
        config = tomllib.loads(self.path.read_text(encoding="utf-8"))["tool"]["clean-arch"]
        definitions = config["layers"]
        self.assertEqual(definitions, projected)
        self.assertEqual(
            {item["module"]: item["name"] for item in definitions}, self.layers,
        )
        self.assertEqual(len(definitions), len(self.layers))
        self.assertEqual(config["infrastructure_modules"], ["sample.transport"])
        self.assertEqual(definitions[-1], {"module": "sample", "name": "Domain"})
        for name, layer in self.layers.items():
            with self.subTest(module=name):
                # The first matching prefix must retain each file's reviewed layer.
                matched = next(
                    item["name"] for item in definitions
                    if name == item["module"] or name.startswith(item["module"] + ".")
                )
                self.assertEqual(matched, layer)

    def test_preserves_other_settings_and_comments_and_is_idempotent(self):
        original = tomllib.loads(self.path.read_text(encoding="utf-8"))
        sync_clean_arch(self.root, self.tach)
        rendered = self.path.read_text(encoding="utf-8")
        updated = tomllib.loads(rendered)
        self.assertTrue(rendered.startswith(self.unmanaged))
        self.assertIn('visibility_enforcement = true # Keep enforcement enabled.', rendered)
        for document in (original, updated):
            config = document["tool"]["clean-arch"]
            del config["layers"]
            del config["infrastructure_modules"]
        self.assertEqual(updated, original)
        sync_clean_arch(self.root, self.tach)
        self.assertEqual(self.path.read_text(encoding="utf-8"), rendered)


if __name__ == "__main__":
    unittest.main()
