"""Real Tach regressions for the opt-in, consumer-owned contract boundary draft."""
from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, cast
import unittest

import tomlkit

if TYPE_CHECKING:
    from eryx._common import EryxError, load_toml
    from eryx.architecture_draft.configured_architecture_draft import ConfiguredArchitectureDraft
    from eryx.tach_worker_utils import run
    from offline_fixtures import create_repo

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))
sys.path.insert(0, str(ERYX / "tests"))

if not TYPE_CHECKING:
    common = import_module("eryx._common")
    EryxError = common.EryxError
    load_toml = common.load_toml
    ConfiguredArchitectureDraft = import_module(
        "eryx.architecture_draft.configured_architecture_draft").ConfiguredArchitectureDraft
    run = import_module("eryx.tach_worker_utils").run
    create_repo = import_module("offline_fixtures").create_repo


class TestArchitectureDraft(unittest.TestCase):
    def fixture(self, directory: str) -> Path:
        return Path(directory)

    def write_config(self, root: Path, config: dict[str, Any]) -> None:
        serialize = cast(Callable[[dict[str, Any]], str], getattr(tomlkit, "dumps"))
        _ = (root / "tach.toml").write_text(serialize(config))

    def test_native_contract_imports_and_private_implementation_utilities(self):
        with TemporaryDirectory(prefix="eryx-contract-tach-") as directory:
            root = self.fixture(directory)
            create_repo(root)
            pkg = root / "src/demo"
            folder = pkg / "port"
            folder.mkdir()
            port_source = ("from typing import Protocol\nclass Port(Protocol):\n" +
                           "    def send(self) -> None: ...\n")
            (folder / "port.py").write_text(port_source)
            (folder / "port.pyi").write_text(port_source)
            (folder / "impl.py").write_text(
                "from demo.port.port import Port\nclass Impl(Port):\n" +
                "    def send(self) -> None: pass\n")
            (folder / "port_utils.py").write_text("def helper() -> None: pass\n")
            nested = folder / "nested"
            nested.mkdir()
            (nested / "private_utils.py").write_text("def secret() -> None: pass\n")
            package_init = folder / "__init__.py"
            package_init.write_text("from demo.port.port import Port\n")
            (folder / "__init__.pyi").write_text("from demo.port.impl import Impl\n")
            consumer = pkg / "consumer.py"
            consumer.write_text("from demo.port.port import Port\n")
            architecture = ConfiguredArchitectureDraft(root)
            config, notes = architecture.draft(contract_only=True)
            self.assertIn("do not run tach sync", notes[0])
            self.write_config(root, config)
            self.assertEqual(next(module for module in load_toml(root / "tach.toml")["modules"]
                                  if module["path"] == "demo.consumer")["depends_on"],
                             ["demo.port.port"])
            good = run("check", root)
            self.assertEqual(good["exit_code"], 0, good)
            self.assertNotIn("^Impl$", next(interface for interface in config["interfaces"]
                                            if interface["from"] == ["^demo\\.port$"])["expose"])
            implementation = folder / "impl.py"
            implementation.write_text(implementation.read_text() +
                                      "from demo.port.port_utils import helper\n")
            internal_config, _ = architecture.draft(contract_only=True)
            self.write_config(root, internal_config)
            self.assertEqual(run("check", root)["exit_code"], 0)
            implementation.write_text(implementation.read_text().replace(
                "from demo.port.port_utils import helper\n", ""))
            self.write_config(root, config)
            consumer.write_text("from demo.port import Port\n")
            package_config, _ = architecture.draft(contract_only=True)
            self.write_config(root, package_config)
            self.assertEqual(run("check", root)["exit_code"], 0)
            consumer.write_text("from demo.port.port import Port\n")
            self.write_config(root, config)
            for statement in ("from demo.port.impl import Impl",
                              "from demo.port.port_utils import helper",
                              "from demo.port.nested.private_utils import secret",
                              "from demo.port import Impl", "import demo.port.impl"):
                with self.subTest(statement=statement):
                    consumer.write_text("from demo.port.port import Port\n" + statement + "\n")
                    bad = run("check", root)
                    self.assertEqual(bad["exit_code"], 1, bad)
                    self.assertTrue(any("consumer.py" in str(item["path"])
                                        for item in bad["diagnostics"]), bad)
                    self.assertEqual(architecture.draft(contract_only=True)[0], config)
            consumer.write_text("from demo.port.port import Port\n")
            package_init.write_text("from demo.port.port import Port\n" +
                                    "from demo.port.impl import Impl\n")
            consumer.write_text("from demo.port.port import Port\nfrom demo.port import Impl\n")
            reexport_config, _ = architecture.draft(contract_only=True)
            self.write_config(root, reexport_config)
            bad = run("check", root)
            self.assertEqual(bad["exit_code"], 1, bad)
            self.assertTrue(any(item["path"] == "src/demo/consumer.py"
                                for item in bad["diagnostics"]), bad)

    def test_opt_in_draft_does_not_sync_or_replace_default_policy(self):
        with TemporaryDirectory(prefix="eryx-contract-draft-") as directory:
            root = self.fixture(directory)
            create_repo(root)
            architecture = ConfiguredArchitectureDraft(root)
            ordinary, _ = architecture.draft()
            self.assertEqual(ordinary["interfaces"][-1]["expose"], [r"api(?:\..*)?"])
            original = (root / "tach.toml").read_bytes()
            event, _ = architecture.initialize(contract_only=True)
            self.assertEqual(event, "review_required")
            self.assertEqual((root / "tach.toml").read_bytes(), original)
            event, _ = architecture.initialize(contract_only=True, force=True)
            self.assertEqual(event, "review_required")
            self.assertFalse((root / ".eryx/architecture.ready.json").exists())
            self.assertFalse((root / ".eryx/logs/init-sync.log").exists())
            self.assertEqual(load_toml(root / "tach.toml")["exact"], True)

    def test_layer_direction_and_exact_unused_declarations_stay_native(self):
        with TemporaryDirectory(prefix="eryx-contract-layers-") as directory:
            root = self.fixture(directory)
            create_repo(root)
            port = root / "src/demo/domain/port"
            port.mkdir()
            (port / "port.py").write_text(
                "from typing import Protocol\nclass Port(Protocol):\n" +
                "    def send(self) -> None: ...\n")
            app = root / "src/demo/application"
            app.mkdir()
            (app / "service.py").write_text("from demo.domain.port.port import Port\n")
            config, _ = ConfiguredArchitectureDraft(root).draft(contract_only=True)
            self.write_config(root, config)
            self.assertEqual(run("check", root)["exit_code"], 0)
            (port / "port.py").write_text((port / "port.py").read_text() +
                                          "from demo.application import service\n")
            rejected = run("check", root)
            self.assertEqual(rejected["exit_code"], 1, rejected)
            self.assertTrue(any("port.py" in str(item["path"])
                                for item in rejected["diagnostics"]), rejected)
            (port / "port.py").write_text((port / "port.py").read_text().replace(
                "from demo.application import service\n", ""))
            config["modules"][0]["depends_on"].append("demo.application.service")
            self.write_config(root, config)
            unused = run("check", root)
            self.assertEqual(unused["exit_code"], 1, unused)
            self.assertTrue(any(item["rule"] == "unused-dependency"
                                for item in unused["diagnostics"]), unused)

    def test_invalid_folder_or_stub_source_never_produces_a_draft(self):
        with TemporaryDirectory(prefix="eryx-contract-invalid-") as directory:
            root = self.fixture(directory)
            create_repo(root)
            port = root / "src/demo/port"
            port.mkdir()
            (port / "port.py").write_text(
                "from typing import Protocol\nclass Port(Protocol):\n" +
                "    def send(self) -> None: ...\n")
            misplaced = root / "src/demo/other/impl.py"
            misplaced.parent.mkdir()
            misplaced.write_text("from demo.port.port import Port\nclass Impl(Port): pass\n")
            architecture = ConfiguredArchitectureDraft(root)
            with self.assertRaisesRegex(EryxError, "move this implementation"):
                architecture.draft(contract_only=True)
            misplaced.unlink()
            stub = port / "port.pyi"
            stub.write_text("class Port(\n")
            with self.assertRaisesRegex(EryxError, r"port\.pyi") as error:
                architecture.draft(contract_only=True)
            self.assertEqual(error.exception.code, "CONTRACT_BOUNDARY_SOURCE_ERROR")


if __name__ == "__main__":
    unittest.main()
