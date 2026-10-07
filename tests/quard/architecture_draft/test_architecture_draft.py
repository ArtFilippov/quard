"""Real Tach regressions for the opt-in, consumer-owned contract boundary draft."""
from __future__ import annotations

import ast
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, cast
import unittest

import tomlkit

if TYPE_CHECKING:
    from quard._common import QuardError, capture, load_toml, write_json
    from quard.policy_utils import configure
    from quard.architecture_draft.generated_architecture_utils import IDENTITY
    from quard.architecture_draft.configured_architecture_draft import ConfiguredArchitectureDraft
    from quard.check_runner.native_check_runner import NativeCheckRunner
    from quard.folder_analysis.folder_ownership import FolderOwnership
    from quard.free_function_placement_utils import analyze as free_functions
    from quard.tach_worker_utils import run
    from offline_fixtures import create_repo, fake_tools

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

if not TYPE_CHECKING:
    common = import_module("quard._common")
    QuardError = common.QuardError
    capture = common.capture
    load_toml = common.load_toml
    write_json = common.write_json
    IDENTITY = import_module("quard.architecture_draft.generated_architecture_utils").IDENTITY
    ConfiguredArchitectureDraft = import_module(
        "quard.architecture_draft.configured_architecture_draft").ConfiguredArchitectureDraft
    run = import_module("quard.tach_worker_utils").run
    create_repo = import_module("offline_fixtures").create_repo
    fake_tools = import_module("offline_fixtures").fake_tools
    FolderOwnership = import_module("quard.folder_analysis.folder_ownership").FolderOwnership
    free_functions = import_module("quard.free_function_placement_utils").analyze
    NativeCheckRunner = import_module("quard.check_runner.native_check_runner").NativeCheckRunner
    configure = import_module("quard.policy_utils").configure


class TestArchitectureDraft(unittest.TestCase):
    def fixture(self, directory: str) -> Path:
        return Path(directory)

    def write_config(self, root: Path, config: dict[str, Any]) -> None:
        serialize = cast(Callable[[dict[str, Any]], str], getattr(tomlkit, "dumps"))
        _ = (root / "tach.toml").write_text(serialize(config))

    def test_native_contract_imports_and_private_implementation_utilities(self):
        with TemporaryDirectory(prefix="quard-contract-tach-") as directory:
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
        with TemporaryDirectory(prefix="quard-contract-draft-") as directory:
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
            self.assertFalse((root / ".quard/architecture.ready.json").exists())
            self.assertFalse((root / ".quard/logs/init-sync.log").exists())
            self.assertEqual(load_toml(root / "tach.toml")["exact"], True)

    def main_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        create_repo(root, ready=False)
        write_json(root / ".quard/architecture.generated.json", IDENTITY)
        configure(root)
        (root / "src/demo/domain/api.py").write_text("")
        (root / "tests/test_value.py").write_text("")
        pkg = root / "src/demo"
        for label in ("reader", "writer"):
            folder = pkg / label
            folder.mkdir()
            contract = label.capitalize()
            (folder / f"{label}.py").write_text(
                f"from typing import Protocol\nclass {contract}(Protocol): pass\n")
            (folder / "impl.py").write_text(
                f"from demo.{label}.{label} import {contract}\n" +
                f"class {contract}Impl({contract}): pass\n")
            (folder / "__init__.py").write_text(
                f"from demo.{label}.impl import {contract}Impl\n" +
                ("from demo.writer.private_utils import helper\n" if label == "writer" else ""))
            (folder / "private_utils.py").write_text("def helper(): pass\n")
        main = pkg / "main/nested"
        main.mkdir(parents=True)
        (main / "assembly.py").write_text(
            "from demo.reader.impl import ReaderImpl\n" +
            "from demo.writer.impl import WriterImpl\n" +
            "class Assembly(ReaderImpl, WriterImpl): pass\n" +
            "def assemble(reader: ReaderImpl) -> WriterImpl: return WriterImpl()\n" +
            '__all__ = ["Assembly", "assemble"]\n')
        (main / "types.pyi").write_text(
            "from demo.reader.impl import ReaderImpl\n" +
            "from demo.writer.impl import WriterImpl\n" +
            "def assemble(reader: ReaderImpl) -> WriterImpl: ...\n")
        (main / "typed.py").write_text(
            "from typing import TYPE_CHECKING\n" +
            "if TYPE_CHECKING:\n    from demo.reader.impl import ReaderImpl\n")
        (main / "modules.py").write_text("import demo.reader.impl\n")
        (main / "exports.py").write_text("from demo.reader import ReaderImpl\n")
        client = pkg / "client.py"
        client.write_text("from demo.reader.reader import Reader\n")
        return pkg, main, client

    def main_check(self, root: Path) -> dict[str, Any]:
        config, _ = ConfiguredArchitectureDraft(root).draft(
            contract_only=True, validate_ownership=False)
        self.write_config(root, config)
        self.assertTrue(config["exact"])
        self.assertFalse(config["ignore_type_checking_imports"])
        return run("check", root)

    def test_main_folder_assembles_multiple_implementations_with_native_checks(self):
        with TemporaryDirectory(prefix="quard-main-assembly-") as directory:
            root = Path(directory)
            _, main, client = self.main_fixture(root)
            self.assertEqual(self.main_check(root)["exit_code"], 0)
            fake, _ = fake_tools()

            def real_boundaries(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
                if any(script in " ".join(argv) for script in (
                    "_tach_worker.py", "_contract_folders_worker.py",
                    "_free_function_placement_worker.py")):
                    return capture(argv, cwd, timeout)
                return fake(argv, cwd, timeout)

            def versions(_root: Path) -> tuple[dict[str, str], list[Any]]:
                return {}, []

            report = NativeCheckRunner().run(root, capture_fn=real_boundaries,
                                             versions_fn=versions)
            self.assertEqual(report["exit_code"], 0, report["diagnostics"])
            self.assertTrue(all(job["status"] == "passed" for job in report["tools"]
                                if job["name"] in {"tach", "contract-folders",
                                                   "free-function-placement"}))
            client.write_text(client.read_text() + "from demo.reader.impl import ReaderImpl\n")
            rejected = self.main_check(root)
            self.assertEqual(rejected["exit_code"], 1, rejected)
            self.assertTrue(any(item["path"] == "src/demo/client.py"
                                for item in rejected["diagnostics"]), rejected)
            client.write_text("from demo.reader import ReaderImpl\n")
            rejected = self.main_check(root)
            self.assertEqual(rejected["exit_code"], 1, rejected)
            self.assertTrue(any(item["path"] == "src/demo/client.py"
                                for item in rejected["diagnostics"]), rejected)
            client.write_text("from demo.reader.reader import Reader\n")
            (main / "assembly.py").write_text(
                (main / "assembly.py").read_text() +
                "from demo.writer.private_utils import helper\n")
            rejected = self.main_check(root)
            self.assertEqual(rejected["exit_code"], 1, rejected)
            self.assertTrue(any(item["path"] == "src/demo/main/nested/assembly.py"
                                for item in rejected["diagnostics"]), rejected)
            (main / "assembly.py").write_text(
                (main / "assembly.py").read_text().replace(
                    "from demo.writer.private_utils import helper\n",
                    "from demo.writer import helper\n"))
            rejected = self.main_check(root)
            self.assertEqual(rejected["exit_code"], 1, rejected)
            self.assertTrue(any(item["path"] == "src/demo/main/nested/assembly.py"
                                for item in rejected["diagnostics"]), rejected)

    def test_main_placement_keeps_outside_diagnostics(self):
        with TemporaryDirectory(prefix="quard-main-placement-") as directory:
            root = Path(directory)
            pkg, _, _ = self.main_fixture(root)
            (pkg / "outside.py").write_text(
                "from demo.reader.impl import ReaderImpl\n" +
                "class Misplaced(ReaderImpl): pass\n" +
                "def misplaced(): pass\n")
            paths = [str(path.relative_to(root)) for path in pkg.rglob("*.py")]
            stub_paths = [str(path.relative_to(root)) for path in pkg.rglob("*.pyi")]
            trees = {path: ast.parse((root / path).read_text()) for path in paths}
            folder_rules = {(item["path"], item["rule"])
                            for item in FolderOwnership(trees).folder_violations()}
            self.assertIn(("src/demo/outside.py", "IMPLEMENTATION_FOLDER"), folder_rules)
            self.assertNotIn(("src/demo/main/nested/assembly.py", "COMPETING_LOCAL_BASES"),
                             folder_rules)
            placements, error = free_functions(root, paths + stub_paths)
            self.assertIsNone(error)
            self.assertIn(("src/demo/outside.py", "FREE_FUNCTION_PLACEMENT"),
                          {(item["path"], item["rule"]) for item in placements})
            self.assertFalse(any("/main/" in str(item["path"]) for item in placements))
            (pkg / "main/bad_contract.py").write_text(
                "from typing import Protocol\nclass Bad(Protocol): pass\n")
            trees["src/demo/main/bad_contract.py"] = ast.parse(
                (pkg / "main/bad_contract.py").read_text())
            self.assertIn(("src/demo/main/bad_contract.py", "CONTRACT_FOLDER"),
                          {(item["path"], item["rule"])
                           for item in FolderOwnership(trees).folder_violations()})

    def envelope_fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        pkg, _, client = self.main_fixture(root)
        envelopes = pkg / "reader/envelopes"
        envelopes.mkdir()
        base = envelopes / "reader_envelope.py"
        base.write_text(
            "from demo.reader.reader import Reader\n" +
            "from demo.writer.impl import WriterImpl\n" +
            "class ReaderEnvelope(Reader):\n" +
            "    def wire(self, implementation: WriterImpl) -> WriterImpl: " +
            "return implementation\n")
        (envelopes / "reader_envelope.pyi").write_text(
            "from demo.reader.reader import Reader\n" +
            "from demo.writer.impl import WriterImpl\n" +
            "class ReaderEnvelope(Reader):\n" +
            "    def wire(self, implementation: WriterImpl) -> WriterImpl: ...\n")
        named = envelopes / "named_envelope.py"
        named.write_text(
            "from demo.reader.envelopes.reader_envelope import ReaderEnvelope\n" +
            "from typing import TYPE_CHECKING\n" +
            "if TYPE_CHECKING:\n    from demo.writer.impl import WriterImpl\n" +
            "class NamedEnvelope(ReaderEnvelope): pass\n")
        (envelopes / "named_envelope.pyi").write_text(
            "from demo.reader.envelopes.reader_envelope import ReaderEnvelope\n" +
            "from demo.writer.impl import WriterImpl\n" +
            "class NamedEnvelope(ReaderEnvelope): ...\n")
        return envelopes, base, named, client

    def test_envelope_assembles_unrelated_implementations_with_native_checks(self):
        with TemporaryDirectory(prefix="quard-envelope-assembly-") as directory:
            root = Path(directory)
            envelopes, base, named, client = self.envelope_fixture(root)
            config, _ = ConfiguredArchitectureDraft(root).draft(
                contract_only=True, validate_ownership=False)
            self.write_config(root, config)
            self.assertEqual(run("check", root)["exit_code"], 0)
            visible = next(interface for interface in config["interfaces"]
                           if interface["from"] == [r"^demo\.writer\.impl$"]
                           and "visibility" in interface)
            self.assertIn("demo.reader.envelopes.reader_envelope", visible["visibility"])
            self.assertIn("demo.reader.envelopes.named_envelope", visible["visibility"])
            self.assertEqual(next(module for module in config["modules"]
                                  if module["path"] == "demo.reader.envelopes.reader_envelope")
                             ["layer"], "Bootstrap")
            self.assertEqual(next(module for module in config["modules"]
                                  if module["path"] == "demo.reader.envelopes.named_envelope")
                             ["depends_on"], ["demo.reader.envelopes.reader_envelope",
                                              "demo.writer.impl"])
            stub_only = envelopes / "assembly_types.pyi"
            stub_only.write_text("from demo.writer.impl import WriterImpl\n")
            config, _ = ConfiguredArchitectureDraft(root).draft(
                contract_only=True, validate_ownership=False)
            self.write_config(root, config)
            self.assertIn("demo.reader.envelopes.assembly_types", next(
                interface for interface in config["interfaces"]
                if interface["from"] == [r"^demo\.writer\.impl$"]
                and "visibility" in interface)["visibility"])
            self.assertEqual(next(module for module in config["modules"]
                                  if module["path"] == "demo.reader.envelopes.assembly_types")
                             ["depends_on"], [])
            fake, _ = fake_tools()

            def real_boundaries(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
                if any(script in " ".join(argv) for script in (
                    "_tach_worker.py", "_contract_folders_worker.py")):
                    return capture(argv, cwd, timeout)
                return fake(argv, cwd, timeout)

            def versions(_root: Path) -> tuple[dict[str, str], list[Any]]:
                return {}, []

            def check() -> dict[str, Any]:
                return NativeCheckRunner().run(root, capture_fn=real_boundaries,
                                               versions_fn=versions)

            report = check()
            self.assertEqual(report["exit_code"], 0, report["diagnostics"])
            self.assertTrue(all(job["status"] == "passed" for job in report["tools"]
                                if job["name"] in {"tach", "contract-folders"}))
            client.write_text("from demo.writer.writer import Writer\n" +
                              "from demo.writer.impl import WriterImpl\n")
            rejected = self.main_check(root)
            self.assertEqual(rejected["exit_code"], 1, rejected)
            self.assertTrue(any(item["path"] == "src/demo/client.py"
                                for item in rejected["diagnostics"]), rejected)
            client.write_text("from demo.writer.writer import Writer\n")
            self.assertEqual(self.main_check(root)["exit_code"], 0)
            base.write_text(base.read_text() + "from demo.writer import WriterImpl\n")
            self.assertEqual(self.main_check(root)["exit_code"], 0)
            base.write_text(base.read_text().replace("from demo.writer import WriterImpl\n", ""))
            base.write_text(base.read_text() +
                            "from demo.writer.private_utils import helper\n")
            rejected = self.main_check(root)
            self.assertEqual(rejected["exit_code"], 1, rejected)
            self.assertTrue(any(item["path"] == "src/demo/reader/envelopes/reader_envelope.py"
                                for item in rejected["diagnostics"]), rejected)
            base.write_text(base.read_text().replace(
                "from demo.writer.private_utils import helper\n", ""))
            nested = envelopes / "nested"
            nested.mkdir()
            (nested / "assembly.py").write_text("from demo.writer.impl import WriterImpl\n")
            rejected = self.main_check(root)
            self.assertEqual(rejected["exit_code"], 1, rejected)
            self.assertTrue(any(item["path"] == "src/demo/reader/envelopes/nested/assembly.py"
                                for item in rejected["diagnostics"]), rejected)
            (nested / "assembly.py").unlink()
            named.write_text(named.read_text().replace("class NamedEnvelope(ReaderEnvelope)",
                                                        "class NamedEnvelope(ReaderImpl)"))
            named.write_text("from demo.reader.impl import ReaderImpl\n" + named.read_text())
            report = check()
            self.assertEqual(report["exit_code"], 1, report["diagnostics"])
            self.assertIn("ENVELOPE_INHERITANCE", {item["rule"] for item in
                                                  report["diagnostics"]})
            named.write_text("from demo.writer.impl import WriterImpl\n" +
                             named.read_text().replace("class NamedEnvelope(ReaderImpl)",
                                                       "class NamedEnvelope(WriterImpl)")
                             .replace("from demo.reader.impl import ReaderImpl\n", ""))
            report = check()
            self.assertEqual(report["exit_code"], 1, report["diagnostics"])
            self.assertIn("ENVELOPE_FOLDER", {item["rule"] for item in
                                             report["diagnostics"]})

    def test_envelope_standalone_full_check_and_runtime(self):
        with TemporaryDirectory(prefix="quard-envelope-consumer-") as directory:
            root = Path(directory)
            self.envelope_fixture(root)
            self.assertEqual(self.main_check(root)["exit_code"], 0)
            report = NativeCheckRunner().run(root)
            self.assertEqual(report["exit_code"], 1, report["summary"])
            self.assertEqual(report["summary"]["infrastructure_errors"], 0, report["diagnostics"])
            for job in report["tools"]:
                if job["name"] in {"tach", "contract-folders"}:
                    self.assertEqual((job["status"], job["exit_code"]), ("passed", 0), job)
            execution = capture([sys.executable, "-c",
                                 "import sys; sys.path.insert(0, 'src'); " +
                                 "from demo.reader.envelopes.reader_envelope " +
                                 "import ReaderEnvelope; " +
                                 "from demo.reader.envelopes.named_envelope " +
                                 "import NamedEnvelope; " +
                                 "from demo.writer.impl import WriterImpl; " +
                                 "assert ReaderEnvelope().wire(WriterImpl()).__class__ " +
                                 "is WriterImpl; assert issubclass(NamedEnvelope, ReaderEnvelope)"],
                                root, 10)
            self.assertEqual(execution["exit_code"], 0, execution)

    def test_layer_direction_and_exact_unused_declarations_stay_native(self):
        with TemporaryDirectory(prefix="quard-contract-layers-") as directory:
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
        with TemporaryDirectory(prefix="quard-contract-invalid-") as directory:
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
            with self.assertRaisesRegex(QuardError, "move this implementation"):
                architecture.draft(contract_only=True)
            misplaced.unlink()
            stub = port / "port.pyi"
            stub.write_text("class Port(\n")
            with self.assertRaisesRegex(QuardError, r"port\.pyi") as error:
                architecture.draft(contract_only=True)
            self.assertEqual(error.exception.code, "CONTRACT_BOUNDARY_SOURCE_ERROR")


if __name__ == "__main__":
    unittest.main()
