"""Public full-check adoption fixture and independent forbidden-case regressions."""

from __future__ import annotations

from contextlib import AbstractContextManager
from importlib import import_module
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from adoption_fixture_utils import temporary_consumer
from offline_fixtures import create_repo
from legacy_clone_fixture_utils import historical_cli
from mutation_fixture_utils import temporary_edits
from quard._common import capture, load_toml, read_json, write_json

discover_plugins = import_module("quard.audit_utils").discover_plugins
configure = import_module("quard.policy_utils").configure
native_check_runner = import_module("quard.check_runner.native_check_runner")
NativeCheckRunner = native_check_runner.NativeCheckRunner


class TestAdoptionConsumer(unittest.TestCase):
    def setUp(self) -> None:
        historical_cli(self)

    def consumer(self) -> AbstractContextManager[Path]:
        return temporary_consumer()

    def check(self, root: Path, exit_code: int = 0) -> dict[str, Any]:
        report = NativeCheckRunner().run(root)
        self.assertEqual(read_json(root / ".quard/reports/latest.json"), report)
        self.assertEqual(report["exit_code"], exit_code, report["diagnostics"])
        errors = report["summary"]["infrastructure_errors"]
        if exit_code == 2:
            self.assertGreater(errors, 0, report["diagnostics"])
        else:
            self.assertEqual(errors, 0, report["diagnostics"])
        return report

    def finding(self, report: dict[str, Any], rule: str, job: str, path: str,
                *, native: bool = False) -> None:
        status = {item["name"]: item for item in report["tools"]}[job]
        self.assertEqual((status["status"], status["exit_code"]), ("violations", 1), status)
        self.assertTrue(any(item["rule"] == rule and item["job"] == job and
                            item["path"] == path and item["line"] >= 1 and
                            (job == "tach-external" or item["column"] >= 1) and
                            not item["is_infrastructure_error"]
                            for item in report["diagnostics"]), report["diagnostics"])
        if native:
            tach = {item["name"]: item for item in report["tools"]}["tach"]
            self.assertEqual((tach["status"], tach["exit_code"]), ("violations", 1))
            self.assertTrue(any(item["job"] == "tach" and item["path"] == path and
                                item["line"] >= 1 for item in report["diagnostics"]),
                            report["diagnostics"])

    def test_full_adoption_and_runtime(self) -> None:
        with self.consumer() as root:
            report = self.check(root)
            self.assertFalse(report["diagnostics"])
            self.assertTrue(all((item["status"], item["exit_code"]) in
                                {("passed", 0), ("skipped", None)} for item in report["tools"]))
            self.assertEqual({item["name"] for item in report["tools"] if
                              item["status"] == "skipped"}, {"constructor-purity"})
            modules = {item["path"]: item for item in load_toml(root / "tach.toml")["modules"]}
            self.assertIn("placeholder", modules)
            self.assertEqual(modules["demo.records.entry_shape"]["layer"], "Contract")
            self.assertEqual(modules["demo.records.entry"]["layer"], "Infrastructure")
            self.assertEqual(modules["demo.records.envelopes.adapter"]["layer"], "Envelopes")
            self.assertEqual(modules["demo.main.wire"]["layer"], "Bootstrap")
            self.assertEqual(modules["gateway.client.value_utils"]["depends_on"],
                             ["demo", "demo.records.entry_shape"])
            self.assertTrue(load_toml(root / "tach.toml")["exact"])
            script = ("import sys; sys.path.insert(0, 'src'); " +
                      "from demo.main.wire import connect; " +
                      "from demo.records.identity import Identity; " +
                      "from demo.records.entry import Entry; " +
                      "from demo.records.sender import Sender; " +
                      "from gateway.client.value_utils import read_value; " +
                      "assert connect() == 'ready:ready:ok'; " +
                      "assert read_value(Entry(Identity('ok')), Sender()) == 'ready:ok'")
            self.assertEqual(capture([sys.executable, "-c", script], root, 15)["exit_code"], 0)
            self.assertEqual(capture([sys.executable, "-m", "unittest", "discover", "-s",
                                      "tests/demo/records", "-p", "test_sender.py"], root, 15)[
                                          "exit_code"], 0)

    def test_independent_forbidden_mutations(self) -> None:
        with self.consumer() as root:
            self.check(root)
            group = "src/demo/records/"
            client = "src/gateway/client/value_utils.py"
            cases = (
                (group + "__init__.py", "from .sender import Sender as Concrete\n",
                 "CONCRETE_FACADE_EXPORT", "contract-folders", False),
                (group + "__init__.py",
                 "from .sender import Sender as Alias\n__all__ = ['Alias']\n",
                 "CONCRETE_FACADE_EXPORT", "contract-folders", False),
                ("src/demo/__init__.py", "from .records.sender import Sender as Hidden\n",
                 "CONCRETE_FACADE_EXPORT", "contract-folders", False),
                (group + "__init__.py", "from .entry import Entry as PublicEntry\n",
                 "CONCRETE_FACADE_EXPORT", "contract-folders", False),
                (group + "__init__.py", "__all__ = ['Missing']\n",
                 "UNRESOLVED_FACADE_EXPORT", "contract-folders", False),
                (group + "__init__.pyi", "from .sender import Sender as Port\n",
                 "CONFLICTING_FACADE_EXPORT", "contract-folders", False),
                (group + "entry.pyi", "from .identity import Identity\n",
                 "CONCRETE_VALUE_DEPENDENCY", "contract-folders", False),
                (group + "sender.py", "instance = Sender()\n",
                 "CONCRETE_IMPLEMENTATION_REFERENCE", "contract-folders", False),
                (group + "sender.py", "class Child(Sender):\n    pass\n",
                 "CONCRETE_IMPLEMENTATION_BASE", "contract-folders", False),
                (client, "from demo.records.sender import Sender\n",
                 "CONCRETE_IMPLEMENTATION_IMPORT", "contract-folders", True),
                ("src/gateway/client/main.py", "from demo.records.sender import Sender\n",
                 "CONCRETE_IMPLEMENTATION_IMPORT", "contract-folders", True),
                ("src/gateway/client/value_utils.pyi", "from demo.records.sender import Sender\n",
                 "CONCRETE_IMPLEMENTATION_IMPORT", "contract-folders", False),
                 (client, "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n" +
                  "    from demo.records.sender import Sender\n",
                 "CONCRETE_IMPLEMENTATION_IMPORT", "contract-folders", True),
                ("src/demo/records/envelopes/adapter.py", "from demo.main.wire import connect\n",
                 "BOOTSTRAP_DEPENDENCY", "contract-folders", True),
                ("src/demo/main/wire.py", "from demo.main.peer import result\n",
                 "BOOTSTRAP_DEPENDENCY", "contract-folders", True),
                ("src/demo/main/wire.pyi", "from demo.main.peer import result\n",
                 "BOOTSTRAP_DEPENDENCY", "contract-folders", False),
                (client, "import tomlkit\n", "UndeclaredExternalDependency",
                 "tach-external", False),
                ("src/direct.py", "from demo.records.service import Service\n",
                 "SOURCE_ROOT_LAYOUT", "contract-folders", False),
                ("src/direct.pyi", "from demo.records.service import Service\n",
                 "SOURCE_ROOT_LAYOUT", "contract-folders", False),
            )
            (root / "src/demo/main/peer.py").write_text("result = 1\n")
            batches: list[list[tuple[str, str, str, str, bool]]] = []
            for case in cases:
                for batch in batches:
                    if all(other[0] != case[0] for other in batch):
                        batch.append(case)
                        break
                else:
                    batches.append([case])
            for batch in batches:
                changes = {root / path: ((root / path).read_text()
                                         if (root / path).exists() else "") + addition
                           for path, addition, *_ in batch}
                with temporary_edits(changes):
                    report = self.check(root, 1)
                    for path, addition, rule, job, native in batch:
                        with self.subTest(path=path, rule=rule, addition=addition):
                            self.finding(report, rule, job, path, native=native)
            (root / "src/demo/main/peer.py").unlink()
            self.check(root)

    def test_provenance_and_composition_mutations(self) -> None:
        with self.consumer() as root:
            self.check(root)
            group = "src/demo/records/"
            mutations = (
                (group + "identity.py", "from typing import NamedTuple\n",
                 "from typing import NamedTuple\nNamedTuple = str\n",
                 "UNRESOLVED_VALUE_FACTORY", "contract-folders", False),
                (group + "entry.pyi", "('identity', IdentityShape)", "('identity', str)",
                 "CONFLICTING_VALUE_FACTORY", "contract-folders", False),
                (group + "external_items.pyi", "UserList[str]", "UserList[int]",
                 "UNRESOLVED_EXTERNAL_CONTRACT", "contract-folders", False),
                (group + "external_items.py", "class ExternalItems",
                 "UserList = object\n\nclass ExternalItems",
                 "UNRESOLVED_EXTERNAL_CONTRACT", "contract-folders", False),
                (group + "sender.pyi", "from demo.records.service import Service",
                 "from demo.records.sender import Sender as Service",
                 "CONCRETE_IMPLEMENTATION_BASE", "contract-folders", False),
                (group + "sender.py", "class Sender(Service):",
                 "class Sender(Service, Ghost):", "UNRESOLVED_GROUP_OWNER",
                 "contract-folders", False),
                ("src/demo/__init__.py", "from .records import Port as PublicPort",
                 "from .records.sender import Sender as PublicPort",
                 "CONCRETE_FACADE_EXPORT", "contract-folders", False),
            )
            originals = {path: (root / path).read_text() for path, *_ in mutations}
            for path, old, *_ in mutations:
                self.assertIn(old, originals[path])
            with temporary_edits({root / path: originals[path].replace(old, new)
                                  for path, old, new, *_ in mutations}):
                report = self.check(root, 1)
                for path, _, _, rule, job, native in mutations:
                    with self.subTest(path=path, rule=rule):
                        self.finding(report, rule, job, path, native=native)
            additions = (
                ("src/gateway/client/value_utils.py",
                  "from demo.records.entry import Entry\n", "CONCRETE_VALUE_DEPENDENCY",
                  "contract-folders", True),
                 ("src/gateway/client/value_utils.py",
                  "from collections import UserDict as Unrelated\n" +
                  "from demo.records.external_items import ExternalItems\n",
                 "CONCRETE_IMPLEMENTATION_IMPORT", "contract-folders", True),
                (group + "sender.py", "class Peer(Service):\n    pass\n",
                 "MULTIPLE_OUTER_CLASSES", "outer-classes", False),
                (group + "service.py", "class Accidental(Service):\n    pass\n",
                  "MIXED_MODULE_ROLES", "contract-folders", False),
            )
            for batch in (additions[::2], additions[1::2]):
                with temporary_edits({root / path: (root / path).read_text() + addition
                                      for path, addition, *_ in batch}):
                    report = self.check(root, 1)
                    for path, _, rule, job, native in batch:
                        with self.subTest(path=path, rule=rule):
                            self.finding(report, rule, job, path, native=native)
            client = root / "src/gateway/client/value_utils.py"
            before = client.read_text()
            client.write_text(before.replace("demo.records.entry_shape as shape",
                                             "demo.records.entry as shape").replace(
                                                 "value: shape.EntryShape", "value: shape.Entry"))
            self.finding(self.check(root, 1), "CONCRETE_DEPENDENCY_TYPE",
                         "dependency-types", "src/gateway/client/value_utils.py", native=True)
            client.write_text(before)
            service_stub = root / group / "service.pyi"
            service_stub.write_text("class Service(object):\n" +
                                    "    def first(self, value: str) -> str: ...\n" +
                                    "    def second(self, value: str) -> str: ...\n")
            self.finding(self.check(root, 1), "CONFLICTING_MODULE_ROLE",
                         "contract-folders", group + "service.pyi")
            service_stub.unlink()
            bridge = root / "src/gateway/client/bridge.py"
            bridge.write_text("from demo.records.service import Service\n" +
                              "class Bridge(Service):\n" +
                              "    def first(self, value: str) -> str: return value\n" +
                              "    def second(self, value: str) -> str: return value\n")
            self.finding(self.check(root, 1), "CROSS_GROUP_CONTRACT",
                         "contract-folders", "src/gateway/client/bridge.py")
            bridge.unlink()
            sender = root / group / "sender.py"
            before = sender.read_text()
            sender.write_text(before.replace("class Sender(Service):",
                                            "class Sender(Service):\n    peer: Peer") +
                              "\nclass Peer(Service):\n    pass\n")
            self.finding(self.check(root, 1), "CONCRETE_DEPENDENCY_TYPE",
                         "dependency-types", group + "sender.py")
            sender.write_text(before)
            nested = root / "src/demo/records/envelopes/nested"
            nested.mkdir()
            (nested / "wire.py").write_text("from demo.records.sender import Sender\n")
            self.finding(self.check(root, 1), "CONCRETE_IMPLEMENTATION_IMPORT",
                         "contract-folders", "src/demo/records/envelopes/nested/wire.py",
                         native=True)
            (nested / "wire.py").unlink()
            nested.rmdir()
            duplicate = root / "src/gateway/main"
            duplicate.mkdir()
            report = self.check(root, 1)
            self.finding(report, "MULTIPLE_MAIN_DIRECTORIES", "contract-folders",
                         "src/gateway/main")
            duplicate.rmdir()
            self.check(root)

    def test_real_failures_preserve_independent_findings(self) -> None:
        with self.consumer() as root:
            self.check(root)
            client = root / "src/gateway/client/value_utils.py"
            client.write_text(client.read_text() + "\nimport tomlkit\n")
            source = root / "src/demo/records/sender.py"
            source.write_text(source.read_text() + "\nclass Broken(\n")
            parsed = self.check(root, 2)
            jobs = {item["name"]: item for item in parsed["tools"]}
            self.assertEqual((jobs["contract-folders"]["status"],
                              jobs["contract-folders"]["exit_code"]), ("error", 2))
            self.assertEqual(jobs["tach"]["status"], "blocked")
            self.assertEqual(jobs["tach-external"]["status"], "blocked")
            self.assertTrue(any(d["rule"] == "CONTRACT_FOLDERS_PARSE_ERROR" and
                                d["is_infrastructure_error"] for d in parsed["diagnostics"]))
            self.assertTrue(any(d.get("job") == "pylint-production" and d["rule"] == "W0611"
                                for d in parsed["diagnostics"]))
            source.write_text(source.read_text().replace("\nclass Broken(\n", ""))
            original = native_check_runner.make_jobs

            def broken_worker(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
                jobs = original(*args, **kwargs)
                next(item for item in jobs if item["name"] == "contract-folders")[
                    "argv"].append("--invalid-worker-flag")
                return jobs

            with patch.object(native_check_runner, "make_jobs", side_effect=broken_worker):
                failed = self.check(root, 2)
            jobs = {item["name"]: item for item in failed["tools"]}
            self.assertEqual(jobs["contract-folders"]["status"], "error")
            self.assertNotEqual(jobs["contract-folders"]["exit_code"], 0)
            self.assertEqual((jobs["tach-external"]["status"],
                              jobs["tach-external"]["exit_code"]), ("violations", 1))
            self.assertTrue(any(d["rule"] == "UNREADABLE_TOOL_OUTPUT" and
                                d["is_infrastructure_error"] for d in failed["diagnostics"]))
            for stream in jobs["contract-folders"]["raw_output"].values():
                self.assertTrue((root / stream).is_file())

    def test_generated_core_and_reviewed_plugin_full_reports(self) -> None:
        with self.consumer() as generated:
            self.check(generated)
            contract = generated / "src/demo/records/service.py"
            contract.write_text("import socket\n" + contract.read_text())
            tests = generated / "tests/test_value.py"
            tests.write_text("import pathlib\n")
            report = self.check(generated, 1)
            jobs = {item["name"]: item for item in report["tools"]}
            for name in ("pylint-production", "pylint-tests"):
                self.assertEqual(jobs[name]["status"], "violations")
                self.assertGreater(jobs[name]["exit_code"], 0)
            self.assertTrue({("W0611", "src/demo/records/service.py"),
                             ("W0611", "tests/test_value.py")} <= {
                                 (d["rule"], d["path"]) for d in report["diagnostics"]})
            self.assertFalse(any(d["rule"].startswith("W9") for d in report["diagnostics"]))
            self.assertFalse(any(d["rule"] in {"E1111", "R0903"} and
                                 d["path"] == "src/demo/records/service.py"
                                 for d in report["diagnostics"]))
            for config in (load_toml(generated / "pyproject.toml")["tool"]["pylint"],
                           load_toml(generated / ".quard/pylint-tests.toml")["tool"]["pylint"]):
                self.assertNotIn("clean_architecture_linter", config["main"]["load-plugins"])
        with TemporaryDirectory(prefix="quard-public-reviewed-") as directory:
            root = Path(directory)
            create_repo(root)
            before = (root / "tach.toml").read_bytes()
            (root / "src/demo/domain/api.py").write_text(
                "import socket\nfrom typing import Protocol\n\n" +
                "class Port(Protocol):\n    def fetch(self) -> int: ...\n")
            (root / "src/demo/client.py").write_text("from demo.domain import api\n")
            (root / "tests/test_value.py").write_text("import pathlib\n")
            logs = root / ".quard/logs/install-probes"
            logs.mkdir(parents=True)
            plugins, catalog = discover_plugins(root, logs)
            write_json(root / ".quard/pylint-catalog.json", catalog)
            configure(root, initial=True, plugins=plugins, catalog=catalog)
            report = self.check(root, 1)
            self.assertEqual((root / "tach.toml").read_bytes(), before)
            self.assertTrue(any(d["rule"] == "W9004" and d["job"] == "pylint-production"
                                for d in report["diagnostics"]))
            self.assertFalse(any(d["rule"] in {"E1111", "R0903"} and
                                 d["path"] == "src/demo/domain/api.py"
                                 for d in report["diagnostics"]))
            self.assertEqual({item["name"]: item["status"] for item in report["tools"]}[
                                 "pylint-production"], "violations")
            self.assertIn("clean_architecture_linter", load_toml(root / "pyproject.toml")[
                "tool"]["pylint"]["main"]["load-plugins"])


if __name__ == "__main__":
    unittest.main()
