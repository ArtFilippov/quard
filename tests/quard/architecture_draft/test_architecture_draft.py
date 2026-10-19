"""Contract boundary draft rules with focused policy and runtime checks."""
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
    from quard.architecture_draft.generated_selection_utils import IDENTITY, LEGACY_IDENTITY
    from quard.architecture_draft.configured_architecture_draft import ConfiguredArchitectureDraft
    from quard.architecture_draft.generated_architecture_utils import regenerate
    from quard.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
    from quard.contract_analysis.dependency_type_utils import analyze as dependency_types
    from quard.folder_analysis.folder_ownership import FolderOwnership
    from quard.folder_analysis.folder_utils import analyze as contract_folders
    from quard.free_function_placement_utils import analyze as free_functions
    from quard.outer_classes_worker_utils import analyze as outer_classes
    from offline_fixtures import CATALOG, create_repo

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from legacy_clone_fixture_utils import historical_cli
from quard.architecture_draft.contract_boundary_utils import contract_boundaries

if not TYPE_CHECKING:
    common = import_module("quard._common")
    QuardError = common.QuardError
    capture = common.capture
    load_toml = common.load_toml
    write_json = common.write_json
    IDENTITY = import_module("quard.architecture_draft.generated_selection_utils").IDENTITY
    LEGACY_IDENTITY = import_module(
        "quard.architecture_draft.generated_selection_utils").LEGACY_IDENTITY
    generated_policy = import_module("quard.architecture_draft.generated_architecture_utils")
    ConfiguredArchitectureDraft = import_module(
        "quard.architecture_draft.configured_architecture_draft").ConfiguredArchitectureDraft
    regenerate = import_module("quard.architecture_draft.generated_architecture_utils").regenerate
    ConfiguredConsumerPolicy = import_module(
        "quard.consumer_policy.configured_consumer_policy").ConfiguredConsumerPolicy
    dependency_types = import_module("quard.contract_analysis.dependency_type_utils").analyze
    contract_folders = import_module("quard.folder_analysis.folder_utils").analyze
    outer_classes = import_module("quard.outer_classes_worker_utils").analyze
    fixtures = import_module("offline_fixtures")
    CATALOG = fixtures.CATALOG
    create_repo = fixtures.create_repo
    FolderOwnership = import_module("quard.folder_analysis.folder_ownership").FolderOwnership
    free_functions = import_module("quard.free_function_placement_utils").analyze
    configure = import_module("quard.policy_utils").configure


class TestArchitectureDraft(unittest.TestCase):
    def setUp(self) -> None:
        historical_cli(self)

    def test_generated_value_contracts_and_independent_forbidden_exposures(self):
        with TemporaryDirectory(prefix="quard-factory-values-") as directory:
            root = Path(directory)
            create_repo(root, ready=False)
            write_json(root / ".quard/architecture.generated.json", IDENTITY)
            (root / "src/demo/domain/api.py").write_text("")
            (root / "tests/test_value.py").write_text("")
            setting = root / ".quard/config.toml"
            setting.write_text(setting.read_text().replace("dependency_types = false",
                                                           "dependency_types = true"))
            for folder in ("src/demo/arbitrary", "src/other", "src/other/group",
                           "src/other/group/deep"):
                location = root / folder
                location.mkdir(parents=True, exist_ok=True)
                (location / "__init__.py").write_text("")
            group = root / "src/demo/arbitrary"
            (group / "identity_shape.py").write_text(
                "from typing import Protocol\n\n"
                + "class IdentityShape(Protocol):\n"
                + "    @property\n    def code(self) -> str: ...\n")
            (group / "evidence_shape.py").write_text(
                "from typing import Protocol\n"
                + "from demo.arbitrary.identity_shape import IdentityShape\n\n"
                + "class EvidenceShape(Protocol):\n"
                + "    @property\n    def identity(self) -> IdentityShape: ...\n")
            identity = group / "identity.py"
            identity.write_text("from typing import NamedTuple\n\n"
                                + "Identity = NamedTuple('Identity', [('code', str)])\n")
            evidence = group / "evidence.py"
            evidence.write_text("from typing import NamedTuple\n"
                                + "from demo.arbitrary.identity_shape import IdentityShape\n\n"
                                + "Evidence = NamedTuple('Evidence', [('identity', IdentityShape)])\n")
            stub = evidence.with_suffix(".pyi")
            stub.write_text(evidence.read_text())
            client = root / "src/other/group/deep/value_utils.py"
            client.write_text("from demo.arbitrary.evidence_shape import EvidenceShape\n\n"
                              + "def read_value(value: EvidenceShape) -> str:\n"
                              + "    return value.identity.code\n")
            configure(root, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)

            def check(expected: int, job: str, rule: str | None = None,
                      path: str | None = None, *, native: bool = False) -> None:
                files = ConfiguredConsumerPolicy(root).files()
                paths = sorted(files["production"] + files["tests"])
                analyzers = {"dependency-types": dependency_types,
                             "contract-folders": contract_folders,
                             "outer-classes": outer_classes}
                findings, error = analyzers[job](root, paths)
                self.assertIsNone(error, findings)
                self.assertFalse(any(item.get("is_infrastructure_error") for item in findings),
                                 findings)
                self.assertEqual(int(bool(findings)), expected, findings)
                if rule is not None:
                    self.assertTrue(any(item["rule"] == rule and item["path"] == path and
                                         item["line"] > 0 and item["column"] > 0
                                        for item in findings), findings)
                if native:
                    regenerate(root)
                    modules = {item["path"]: item for item in load_toml(root / "tach.toml")[
                        "modules"]}
                    self.assertNotIn("demo.arbitrary.evidence", modules[
                        "other.group.deep.value_utils"]["depends_on"])

            check(0, "dependency-types")
            regenerate(root)
            modules = {item["path"]: item for item in load_toml(root / "tach.toml")["modules"]}
            self.assertEqual(modules["demo.arbitrary.evidence"]["layer"], "Infrastructure")
            self.assertEqual(modules["demo.arbitrary.evidence_shape"]["layer"], "Contract")
            self.assertEqual(modules["other.group.deep.value_utils"]["depends_on"],
                             ["demo.arbitrary.evidence_shape"])
            execution = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                                 + "from demo.arbitrary.identity import Identity; "
                                 + "from demo.arbitrary.evidence import Evidence; "
                                 + "from other.group.deep.value_utils import read_value; "
                                 + "assert read_value(Evidence(Identity('ok'))) == 'ok'"], root, 10)
            self.assertEqual(execution["exit_code"], 0, execution)

            original = client.read_text()
            client.write_text("from demo.arbitrary.evidence import Evidence\n\n"
                              + "def read_value(value: Evidence) -> str:\n"
                              + "    return value.identity.code\n")
            check(1, "dependency-types", "CONCRETE_DEPENDENCY_TYPE",
                  "src/other/group/deep/value_utils.py", native=True)
            client.write_text(original)

            client.write_text(original + "\nfrom demo.arbitrary.evidence import Evidence\n"
                              + "current: Evidence\n")
            check(1, "dependency-types", "CONCRETE_DEPENDENCY_TYPE",
                  "src/other/group/deep/value_utils.py")
            client.write_text(original)

            before = evidence.read_text()
            evidence.write_text(before.replace("IdentityShape", "Identity").replace(
                "identity_shape", "identity"))
            check(1, "dependency-types", "CONCRETE_DEPENDENCY_TYPE",
                  "src/demo/arbitrary/evidence.py")
            evidence.write_text(before)

            stub_before = stub.read_text()
            stub.write_text(stub_before.replace("IdentityShape", "Identity").replace(
                "identity_shape", "identity"))
            check(1, "dependency-types", "CONCRETE_DEPENDENCY_TYPE",
                  "src/demo/arbitrary/evidence.pyi")
            stub.write_text(stub_before)

            stub.write_text(stub_before + "\nfrom .identity import Identity\n")
            check(1, "contract-folders", "CONCRETE_VALUE_DEPENDENCY",
                  "src/demo/arbitrary/evidence.pyi")
            stub.write_text(stub_before)

            stub.write_text(stub_before.replace("IdentityShape", "str"))
            check(1, "contract-folders", "CONFLICTING_VALUE_FACTORY",
                  "src/demo/arbitrary/evidence.pyi")
            stub.write_text(stub_before)

            stub.write_text("from typing import NamedTuple\n"
                            + "class Evidence(NamedTuple):\n    identity: str\n")
            check(1, "contract-folders", "CONFLICTING_VALUE_FACTORY",
                  "src/demo/arbitrary/evidence.py")
            stub.write_text(stub_before)

            identity_before = identity.read_text()
            identity.write_text(identity_before.replace("from typing import NamedTuple",
                                                        "from typing import NamedTuple\n"
                                                        + "NamedTuple = str"))
            check(1, "contract-folders", "UNRESOLVED_VALUE_FACTORY",
                  "src/demo/arbitrary/identity.py")
            identity.write_text(identity_before)

            stub.write_text(stub_before.replace("Evidence = NamedTuple(",
                                                "from typing import Protocol\n"
                                                + "class Evidence(Protocol): ...\n"
                                                + "# Evidence = NamedTuple("))
            check(1, "contract-folders", "CONFLICTING_MODULE_ROLE",
                  "src/demo/arbitrary/evidence.pyi")
            stub.write_text(stub_before)

            identity.write_text(identity_before +
                                "Second = NamedTuple('Second', [('code', str)])\n")
            check(1, "outer-classes", "MULTIPLE_OUTER_CLASSES",
                  "src/demo/arbitrary/identity.py")
            identity.write_text(identity_before)

            stub.write_text(stub_before + "Second = NamedTuple('Second', [('code', str)])\n")
            check(1, "outer-classes", "MULTIPLE_OUTER_CLASSES",
                  "src/demo/arbitrary/evidence.pyi")
            stub.write_text(stub_before)

            test_file = root / "tests/test_value.py"
            test_file.write_text("from typing import NamedTuple\n"
                                 + "First = NamedTuple('First', [('code', str)])\n"
                                 + "Second = NamedTuple('Second', [('code', str)])\n")
            check(1, "outer-classes", "MULTIPLE_OUTER_CLASSES", "tests/test_value.py")

    def fixture(self, directory: str) -> Path:
        return Path(directory)

    def write_config(self, root: Path, config: dict[str, Any]) -> None:
        serialize = cast(Callable[[dict[str, Any]], str], getattr(tomlkit, "dumps"))
        _ = (root / "tach.toml").write_text(serialize(config))

    def test_contract_imports_and_private_implementation_policy(self):
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
            self.assertTrue(config["exact"])
            self.assertEqual(next(module["depends_on"] for module in config["modules"]
                                  if module["path"] == "demo.consumer"), ["demo.port.port"])
            self.assertNotIn("^Impl$", next(interface for interface in config["interfaces"]
                                            if interface["from"] == ["^demo\\.port$"])["expose"])
            implementation = folder / "impl.py"
            implementation.write_text(implementation.read_text() +
                                      "from demo.port.port_utils import helper\n")
            internal_config, _ = architecture.draft(contract_only=True)
            self.assertIn("demo.port.port_utils", next(module["depends_on"] for module in
                          internal_config["modules"] if module["path"] == "demo.port.impl"))
            implementation.write_text(implementation.read_text().replace(
                "from demo.port.port_utils import helper\n", ""))
            consumer.write_text("from demo.port import Port\n")
            package_config, _ = architecture.draft(contract_only=True)
            self.assertIn("demo.port", next(module["depends_on"] for module in
                          package_config["modules"] if module["path"] == "demo.consumer"))
            consumer.write_text("from demo.port.port import Port\n")
            for statement in ("from demo.port.impl import Impl",
                              "from demo.port.port_utils import helper",
                              "from demo.port.nested.private_utils import secret",
                              "from demo.port import Impl", "import demo.port.impl"):
                with self.subTest(statement=statement):
                    consumer.write_text("from demo.port.port import Port\n" + statement + "\n")
                    self.assertEqual(architecture.draft(contract_only=True)[0], config)
            consumer.write_text("from demo.port.port import Port\n")
            package_init.write_text("from demo.port.port import Port\n" +
                                    "from demo.port.impl import Impl\n")
            consumer.write_text("from demo.port.port import Port\nfrom demo.port import Impl\n")
            reexport_config, _ = architecture.draft(contract_only=True)
            self.assertNotIn("^Impl$", next(interface["expose"] for interface in
                             reexport_config["interfaces"] if interface["from"] ==
                             [r"^demo\.port$"]))

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
        create_repo(root)
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

    def root_main_fixture(self, root: Path) -> Path:
        self.main_fixture(root)
        main = root / "src/main"
        main.mkdir()
        return main

    def main_policy(self, root: Path) -> dict[str, Any]:
        config, _ = ConfiguredArchitectureDraft(root).draft(
            contract_only=True, validate_ownership=False)
        self.assertTrue(config["exact"])
        self.assertFalse(config["ignore_type_checking_imports"])
        return config

    def depends_on(self, config: dict[str, Any], source: str) -> list[str]:
        return next(item["depends_on"] for item in config["modules"]
                    if item["path"] == source)

    def folder_findings(self, root: Path) -> list[dict[str, Any]]:
        files = ConfiguredConsumerPolicy(root).files()
        findings, error = contract_folders(root, sorted(files["production"] + files["tests"]))
        self.assertIsNone(error, findings)
        self.assertFalse(any(item.get("is_infrastructure_error") for item in findings), findings)
        return findings

    def test_main_folder_assembles_multiple_implementations(self):
        with TemporaryDirectory(prefix="quard-main-assembly-") as directory:
            root = Path(directory)
            _, main, client = self.main_fixture(root)
            config = self.main_policy(root)
            self.assertIn("demo.reader.impl", self.depends_on(config,
                          "demo.main.nested.assembly"))
            self.assertEqual(self.folder_findings(root), [])
            client.write_text(client.read_text() + "from demo.reader.impl import ReaderImpl\n")
            rejected = self.main_policy(root)
            self.assertNotIn("demo.reader.impl", self.depends_on(rejected, "demo.client"))
            client.write_text("from demo.reader import ReaderImpl\n")
            rejected = self.main_policy(root)
            self.assertNotIn("demo.reader", self.depends_on(rejected, "demo.client"))
            client.write_text("from demo.reader.reader import Reader\n")
            (main / "assembly.py").write_text(
                (main / "assembly.py").read_text() +
                "from demo.writer.private_utils import helper\n")
            rejected = self.main_policy(root)
            self.assertNotIn("demo.writer.private_utils", self.depends_on(
                rejected, "demo.main.nested.assembly"))
            (main / "assembly.py").write_text(
                (main / "assembly.py").read_text().replace(
                    "from demo.writer.private_utils import helper\n",
                    "from demo.writer import helper\n"))
            rejected = self.main_policy(root)
            self.assertNotIn("demo.writer", self.depends_on(rejected,
                             "demo.main.nested.assembly"))

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

    def test_root_main_allows_assembly_classes_in_source_and_stub(self):
        with TemporaryDirectory(prefix="quard-root-main-classes-") as directory:
            root = Path(directory)
            main = self.root_main_fixture(root)
            for suffix in (".py", ".pyi"):
                (main / f"wire{suffix}").write_text(
                    "from demo.reader.impl import ReaderImpl\n"
                    "from demo.writer.impl import WriterImpl\n"
                    "class Assembly(ReaderImpl, WriterImpl): pass\n")
            findings, _ = contract_folders(root, ConfiguredConsumerPolicy(root).files()[
                "production"])
            self.assertFalse(any(str(item["path"]).startswith("src/main/") for item in
                                 findings))

    def test_root_main_legacy_draft_can_import_implementations(self):
        with TemporaryDirectory(prefix="quard-root-main-draft-") as directory:
            root = Path(directory)
            main = self.root_main_fixture(root)
            (main / "wire.py").write_text("from demo.reader.impl import ReaderImpl\n")
            files = ConfiguredConsumerPolicy(root).files()["production"]
            draft: dict[str, Any] = {"modules": []}
            contract_boundaries(root, files, draft, validate_ownership=False)
            module = next(item for item in draft["modules"] if item["path"] == "main.wire")
            self.assertEqual((module["layer"], module["depends_on"]),
                             ("Bootstrap", ["demo.reader.impl"]))

    def envelope_fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        pkg, _, client = self.main_fixture(root)
        (pkg / "reader/impl.py").write_text(
            "from demo.reader.reader import Reader\n"
            "class ReaderImpl(Reader):\n"
            "    def wire(self, implementation): return implementation\n")
        envelopes = pkg / "reader/envelopes"
        envelopes.mkdir()
        base = envelopes / "reader_envelope.py"
        base.write_text(
            "from demo.reader.reader import Reader\n" +
            "from demo.writer.impl import WriterImpl\n" +
            "class ReaderEnvelope(Reader):\n" +
            "    def __init__(self, delegate): self.delegate = delegate\n" +
            "    def wire(self, implementation: WriterImpl) -> WriterImpl: " +
            "return self.delegate.wire(implementation)\n")
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
            "class NamedEnvelope(ReaderEnvelope):\n" +
            "    def __init__(self, delegate): super().__init__(delegate)\n")
        (envelopes / "named_envelope.pyi").write_text(
            "from demo.reader.envelopes.reader_envelope import ReaderEnvelope\n" +
            "from demo.writer.impl import WriterImpl\n" +
            "class NamedEnvelope(ReaderEnvelope):\n" +
            "    def __init__(self, delegate): ...\n")
        return envelopes, base, named, client

    def test_envelope_assembles_unrelated_implementations(self):
        with TemporaryDirectory(prefix="quard-envelope-assembly-") as directory:
            root = Path(directory)
            envelopes, base, named, client = self.envelope_fixture(root)
            config, _ = ConfiguredArchitectureDraft(root).draft(
                contract_only=True, validate_ownership=False)
            self.assertIn("demo.writer.impl", self.depends_on(config,
                          "demo.reader.envelopes.reader_envelope"))
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
            self.assertEqual({(item["path"], item["rule"]) for item in self.folder_findings(root)},
                             {("src/demo/reader/envelopes/assembly_types.pyi", "ENVELOPE_CLASS")})
            stub_only.unlink()
            client.write_text("from demo.writer.writer import Writer\n" +
                              "from demo.writer.impl import WriterImpl\n")
            rejected = self.main_policy(root)
            self.assertNotIn("demo.writer.impl", self.depends_on(rejected, "demo.client"))
            client.write_text("from demo.writer.writer import Writer\n")
            self.assertIn("demo.writer.writer", self.depends_on(self.main_policy(root),
                          "demo.client"))
            base.write_text(base.read_text() + "from demo.writer import WriterImpl\n")
            self.assertIn("demo.writer", self.depends_on(self.main_policy(root),
                          "demo.reader.envelopes.reader_envelope"))
            base.write_text(base.read_text().replace("from demo.writer import WriterImpl\n", ""))
            base.write_text(base.read_text() +
                            "from demo.writer.private_utils import helper\n")
            rejected = self.main_policy(root)
            self.assertNotIn("demo.writer.private_utils", self.depends_on(
                rejected, "demo.reader.envelopes.reader_envelope"))
            base.write_text(base.read_text().replace(
                "from demo.writer.private_utils import helper\n", ""))
            nested = envelopes / "nested"
            nested.mkdir()
            (nested / "assembly.py").write_text("from demo.writer.impl import WriterImpl\n")
            accepted = self.main_policy(root)
            self.assertIn("demo.writer.impl", self.depends_on(accepted,
                          "demo.reader.envelopes.nested.assembly"))
            (nested / "assembly.py").unlink()
            named.write_text(named.read_text().replace("class NamedEnvelope(ReaderEnvelope)",
                                                        "class NamedEnvelope(ReaderImpl)"))
            named.write_text("from demo.reader.impl import ReaderImpl\n" + named.read_text())
            self.main_policy(root)
            findings = self.folder_findings(root)
            self.assertTrue(any(item["rule"] == "ENVELOPE_INHERITANCE" and
                                item["path"] == "src/demo/reader/envelopes/named_envelope.py" and
                                item["line"] > 0 and item["column"] > 0
                                for item in findings), findings)
            named.write_text("from demo.writer.impl import WriterImpl\n" +
                             named.read_text().replace("class NamedEnvelope(ReaderImpl)",
                                                       "class NamedEnvelope(WriterImpl)")
                             .replace("from demo.reader.impl import ReaderImpl\n", ""))
            findings = self.folder_findings(root)
            self.assertTrue(any(item["rule"] == "IMPLEMENTATION_FOLDER" and
                                item["path"] == "src/demo/reader/envelopes/named_envelope.py" and
                                item["line"] > 0 and item["column"] > 0
                                for item in findings), findings)

    def test_envelope_class_contract_and_runtime(self):
        with TemporaryDirectory(prefix="quard-envelope-consumer-") as directory:
            root = Path(directory)
            self.envelope_fixture(root)
            self.main_policy(root)
            class_contracts = import_module("quard.contract_analysis.contract_worker_utils").analyze
            files = ConfiguredConsumerPolicy(root).files()
            findings, error = class_contracts(root, sorted(files["production"] + files["tests"]))
            self.assertIsNone(error, findings)
            self.assertNotIn("UNCOVERED_CLASS_MEMBER", {d["rule"] for d in findings})
            self.assertEqual(self.folder_findings(root), [])
            execution = capture([sys.executable, "-c",
                                 "import sys; sys.path.insert(0, 'src'); " +
                                 "from demo.reader.envelopes.reader_envelope " +
                                 "import ReaderEnvelope; " +
                                 "from demo.reader.envelopes.named_envelope " +
                                 "import NamedEnvelope; " +
                                 "from demo.writer.impl import WriterImpl; " +
                                 "from demo.reader.impl import ReaderImpl; " +
                                 "assert ReaderEnvelope(ReaderImpl()).wire(WriterImpl()).__class__ " +
                                 "is WriterImpl; assert issubclass(NamedEnvelope, ReaderEnvelope)"],
                                root, 10)
            self.assertEqual(execution["exit_code"], 0, execution)

    def nested_envelope_fixture(self, root: Path) -> None:
        envelopes, _, _, client = self.envelope_fixture(root)
        nested = envelopes / "nested/deeper"
        nested.mkdir(parents=True)
        (nested / "relay.py").write_text(
            "from demo.reader.envelopes.reader_envelope import ReaderEnvelope\n" +
            "from demo.writer.impl import WriterImpl\n" +
            "class Relay(ReaderEnvelope):\n" +
            "    def __init__(self, delegate: WriterImpl): super().__init__(delegate)\n")
        (nested / "relay.pyi").write_text(
            "from demo.reader.envelopes.reader_envelope import ReaderEnvelope\n" +
            "from demo.writer.impl import WriterImpl\n" +
            "class Relay(ReaderEnvelope):\n" +
            "    def __init__(self, delegate: WriterImpl): ...\n")
        client.write_text("from demo.writer.impl import WriterImpl\n" +
                          "class Client:\n    def send(self, value: WriterImpl) -> None: pass\n")
        client.with_suffix(".pyi").write_text(client.read_text())

    def test_nested_assembly_executes(self):
        with TemporaryDirectory(prefix="quard-nested-runtime-") as directory:
            root = Path(directory)
            self.nested_envelope_fixture(root)
            execution = capture([sys.executable, "-c",
                                 "import sys; sys.path.insert(0, 'src'); " +
                                 "from demo.reader.envelopes.nested.deeper.relay import Relay; " +
                                 "from demo.reader.impl import ReaderImpl; " +
                                 "from demo.writer.impl import WriterImpl; " +
                                 "assert isinstance(Relay(ReaderImpl()).wire(WriterImpl()), " +
                                 "WriterImpl)"], root, 10)
            self.assertEqual(execution["exit_code"], 0, "nested Envelope failed at runtime")

    def test_nested_assembly_boundary_across_modes(self):
        with TemporaryDirectory(prefix="quard-nested-boundary-") as directory:
            root = Path(directory)
            self.nested_envelope_fixture(root)
            results = []
            for mode in ("draft", "v1", "v2"):
                if mode != "draft":
                    write_json(root / ".quard/architecture.generated.json",
                               LEGACY_IDENTITY if mode == "v1" else IDENTITY)
                config = (self.main_policy(root) if mode == "draft" else
                          generated_policy.render(root)[0])
                modules = {item["path"]: item for item in config["modules"]}
                source = "demo.reader.envelopes.nested.deeper.relay"
                results.append(("demo.writer.impl" in modules[source]["depends_on"],
                                "demo.writer.impl" in modules["demo.client"]["depends_on"],
                                source in next(item["visibility"] for item in config["interfaces"]
                                               if item["from"] == [r"^demo\.writer\.impl$"] and
                                               "visibility" in item), modules[source]["layer"]))
            self.assertEqual(results, [(True, False, True, "Bootstrap"),
                                       (True, False, True, "Bootstrap"),
                                       (True, False, True, "Envelopes")],
                             "nested assembly boundaries changed")

    def test_nested_assembly_annotations_across_modes(self):
        with TemporaryDirectory(prefix="quard-nested-types-") as directory:
            root = Path(directory)
            self.nested_envelope_fixture(root)
            files = ConfiguredConsumerPolicy(root).files()
            results = []
            for mode in ("draft", "v1", "v2"):
                if mode != "draft":
                    write_json(root / ".quard/architecture.generated.json",
                               LEGACY_IDENTITY if mode == "v1" else IDENTITY)
                types, error = dependency_types(root, sorted(files["production"] + files["tests"]))
                results.append((error, {item["path"] for item in types if item["rule"] ==
                                        "CONCRETE_DEPENDENCY_TYPE"}))
            self.assertEqual(results, [(None, {"src/demo/client.py", "src/demo/client.pyi"})] * 3,
                             "nested assembly annotations were misclassified")

    def test_nested_assembly_folder_and_ordinary_imports(self):
        with TemporaryDirectory(prefix="quard-nested-folders-") as directory:
            root = Path(directory)
            self.nested_envelope_fixture(root)
            write_json(root / ".quard/architecture.generated.json", IDENTITY)
            findings = self.folder_findings(root)
            self.assertEqual(({"src/demo/client.py", "src/demo/client.pyi"} &
                              {item["path"] for item in findings if item["rule"] ==
                               "CONCRETE_IMPLEMENTATION_IMPORT"},
                              {item["path"] for item in findings if item["path"].endswith(
                                  "/nested/deeper/relay.py")}),
                             ({"src/demo/client.py", "src/demo/client.pyi"}, set()),
                             "nested assembly folder or ordinary imports passed incorrectly")

    def test_layer_direction_and_generated_exact_declarations(self):
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
            self.assertIn("demo.application.service", next(item["cannot_depend_on"] for item
                          in config["modules"] if item["path"] == "demo.domain.port.port"))
            (port / "port.py").write_text((port / "port.py").read_text() +
                                          "from demo.application import service\n")
            self.assertNotIn("demo.application.service", self.depends_on(
                ConfiguredArchitectureDraft(root).draft(contract_only=True)[0],
                "demo.domain.port.port"))
            (port / "port.py").write_text((port / "port.py").read_text().replace(
                "from demo.application import service\n", ""))
            config["modules"][0]["depends_on"].append("demo.application.service")
            regenerated = ConfiguredArchitectureDraft(root).draft(contract_only=True)[0]
            self.assertNotIn("demo.application.service", self.depends_on(
                regenerated, config["modules"][0]["path"]))

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
