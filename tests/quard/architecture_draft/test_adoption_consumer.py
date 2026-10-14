"""Public full-check adoption fixture and independent forbidden-case regressions."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from adoption_fixture_utils import temporary_consumer
from offline_fixtures import CATALOG, create_repo
from legacy_clone_fixture_utils import historical_cli
from mutation_fixture_utils import temporary_edits
from quard._common import capture, load_toml
from quard.architecture_draft.generated_architecture_utils import render
from quard.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
from quard.contract_analysis.dependency_type_utils import analyze as dependency_types
from quard.folder_analysis.folder_utils import analyze as contract_folders
from quard.outer_classes_worker_utils import analyze as outer_classes
from quard.policy_utils import configure


class TestAdoptionConsumer(unittest.TestCase):
    def setUp(self) -> None:
        historical_cli(self)

    def consumer(self) -> AbstractContextManager[Path]:
        return temporary_consumer(probe_plugins=False)

    def focused_findings(self, root: Path, cases: list[tuple[str, str, str, bool]]) -> None:
        """Check mutations with real rules; report delivery is tested separately.

        Args:
            root (Path): Selected isolated consumer.
            cases (list[tuple[str, str, str, bool]]): Path, rule, job and policy requirement.
        """
        files = ConfiguredConsumerPolicy(root).files()
        paths = sorted(files["production"] + files["tests"])
        analyzers = {"contract-folders": contract_folders,
                     "dependency-types": dependency_types, "outer-classes": outer_classes}
        diagnostics: dict[str, list[dict[str, Any]]] = {}
        for job in {case[2] for case in cases} - {"tach-external"}:
            findings, error = analyzers[job](root, paths)
            self.assertIsNone(error, findings)
            self.assertFalse(any(item.get("is_infrastructure_error") for item in findings),
                             findings)
            diagnostics[job] = findings

        for path, rule, job, needs_tach in cases:
            with self.subTest(path=path, rule=rule, job=job):
                if job != "tach-external":
                    self.assertTrue(any(item["rule"] == rule and item["path"] == path and
                                        item["line"] >= 1 and item["column"] >= 1
                                        for item in diagnostics[job]), diagnostics[job])
                if job == "tach-external":
                    self.assertIn("import tomlkit", (root / path).read_text())
                    self.assertNotIn("tomlkit", load_toml(root / "pyproject.toml")[
                        "project"]["dependencies"])
                if needs_tach:
                    config = render(root)[0]
                    source = Path(path).relative_to("src").with_suffix(
                        "").as_posix().replace("/", ".")
                    target = ("demo.main.peer" if source == "demo.main.wire" else
                              "demo.main.wire" if rule == "BOOTSTRAP_DEPENDENCY" else
                              "demo.records.entry" if rule in {
                                  "CONCRETE_VALUE_DEPENDENCY", "CONCRETE_DEPENDENCY_TYPE"} else
                              "demo.records.external_items" if "external_items" in (
                                  root / path).read_text() else "demo.records.sender")
                    module = next(item for item in config["modules"] if item["path"] == source)
                    self.assertNotIn(target, module["depends_on"])

    def test_full_adoption_and_runtime(self) -> None:
        with self.consumer() as root:
            modules = {item["path"]: item for item in render(root)[0]["modules"]}
            self.assertIn("placeholder", modules)
            self.assertEqual(modules["demo.records.entry_shape"]["layer"], "Contract")
            self.assertEqual(modules["demo.records.entry"]["layer"], "Infrastructure")
            self.assertEqual(modules["demo.records.adapter"]["layer"], "Infrastructure")
            self.assertEqual(modules["demo.main.wire"]["layer"], "Bootstrap")
            self.assertEqual(modules["gateway.client.value_utils"]["depends_on"],
                             ["demo", "demo.records.entry_shape"])
            self.assertTrue(render(root)[0]["exact"])
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
                ("src/demo/records/adapter.py", "from demo.main.wire import connect\n",
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
                mutations = tuple((path, addition) for path, addition, *_ in batch)
                with self.subTest(mutations=mutations), temporary_edits(changes):
                    self.focused_findings(root, [(path, rule, job, native)
                                                 for path, _, rule, job, native in batch])
            (root / "src/demo/main/peer.py").unlink()
            self.assertEqual(self.focused_clean(root), [])

    def test_provenance_and_composition_mutations(self) -> None:
        with self.consumer() as root:
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
                self.focused_findings(root, [(path, rule, job, native)
                                             for path, _, _, rule, job, native in mutations])
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
                    self.focused_findings(root, [(path, rule, job, native)
                                                 for path, _, rule, job, native in batch])
            client = root / "src/gateway/client/value_utils.py"
            before = client.read_text()
            client.write_text(before.replace("demo.records.entry_shape as shape",
                                             "demo.records.entry as shape").replace(
                                                 "value: shape.EntryShape", "value: shape.Entry"))
            self.focused_findings(root, [("src/gateway/client/value_utils.py",
                                          "CONCRETE_DEPENDENCY_TYPE", "dependency-types", True)])
            client.write_text(before)
            service_stub = root / group / "service.pyi"
            service_stub.write_text("class Service(object):\n" +
                                    "    def first(self, value: str) -> str: ...\n" +
                                    "    def second(self, value: str) -> str: ...\n")
            self.focused_findings(root, [(group + "service.pyi", "CONFLICTING_MODULE_ROLE",
                                          "contract-folders", False)])
            service_stub.unlink()
            bridge = root / "src/gateway/client/bridge.py"
            bridge.write_text("from demo.records.service import Service\n" +
                              "class Bridge(Service):\n" +
                              "    def first(self, value: str) -> str: return value\n" +
                              "    def second(self, value: str) -> str: return value\n")
            self.focused_findings(root, [("src/gateway/client/bridge.py", "CROSS_GROUP_CONTRACT",
                                          "contract-folders", False)])
            bridge.unlink()
            sender = root / group / "sender.py"
            before = sender.read_text()
            sender.write_text(before.replace("class Sender(Service):",
                                            "class Sender(Service):\n    peer: Peer") +
                              "\nclass Peer(Service):\n    pass\n")
            self.focused_findings(root, [(group + "sender.py", "CONCRETE_DEPENDENCY_TYPE",
                                          "dependency-types", False)])
            sender.write_text(before)
            nested = root / "src/demo/records/envelopes/nested"
            nested.mkdir()
            (nested / "wire.py").write_text("from demo.records.sender import Sender\n")
            self.focused_findings(root, [("src/demo/records/envelopes/nested/wire.py",
                                          "CONCRETE_IMPLEMENTATION_IMPORT", "contract-folders",
                                          True)])
            (nested / "wire.py").unlink()
            nested.rmdir()
            duplicate = root / "src/gateway/main"
            duplicate.mkdir()
            self.focused_findings(root, [("src/gateway/main", "MULTIPLE_MAIN_DIRECTORIES",
                                          "contract-folders", False)])
            duplicate.rmdir()
            self.assertEqual(self.focused_clean(root), [])

    def focused_clean(self, root: Path) -> list[dict[str, Any]]:
        files = ConfiguredConsumerPolicy(root).files()
        paths = sorted(files["production"] + files["tests"])
        findings, error = contract_folders(root, paths)
        self.assertIsNone(error, findings)
        return findings

    def test_generated_core_and_reviewed_plugin_configuration(self) -> None:
        with self.consumer() as generated:
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
            configure(root, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
            self.assertEqual((root / "tach.toml").read_bytes(), before)
            self.assertIn("clean_architecture_linter", load_toml(root / "pyproject.toml")[
                "tool"]["pylint"]["main"]["load-plugins"])


if __name__ == "__main__":
    unittest.main()
