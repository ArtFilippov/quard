"""Real rule and native Tach regressions for generated-policy review follow-ups."""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests/quard/architecture_draft"))
import test_adoption_consumer as adoption

from quard._common import capture
from quard.architecture_draft.generated_architecture_utils import render
from quard.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
from quard.folder_analysis.folder_utils import analyze as contract_folders
from selected_report_utils import selected_report


class TestReviewFollowup(unittest.TestCase):
    setUp = adoption.TestAdoptionConsumer.setUp
    consumer = adoption.TestAdoptionConsumer.consumer

    def folders(self, root: Path) -> list[dict[str, Any]]:
        files = ConfiguredConsumerPolicy(root).files()
        findings, error = contract_folders(root, sorted(files["production"] + files["tests"]))
        self.assertIsNone(error, findings)
        self.assertFalse(any(item.get("is_infrastructure_error") for item in findings), findings)
        return findings

    def policy(self, root: Path) -> dict[str, Any]:
        return render(root)[0]

    def denied(self, root: Path, source: str, target: str) -> None:
        modules = {item["path"]: item for item in self.policy(root)["modules"]}
        self.assertNotIn(target, modules[source]["depends_on"])

    def rule(self, findings: list[dict[str, Any]], rule: str, path: str) -> None:
        self.assertTrue(any(item["rule"] == rule and item["path"] == path and
                            item["line"] > 0 and item["column"] > 0
                            for item in findings), findings)

    def test_invalid_companion_facade_blocks_import(self) -> None:
        with self.consumer() as root:
            self.assertEqual(self.folders(root), [])
            stub = root / "src/demo/records/__init__.pyi"
            stub.write_text(stub.read_text() + "from .sender import Sender as Bad\n")
            client = root / "src/gateway/client/value_utils.py"
            client.write_text(client.read_text() + "from demo.records import Port\n"
                              "import tomlkit\n")
            report = selected_report(self, root, "tach", "tach-external", "contract-folders")
            self.assertEqual(report["exit_code"], 1, report["diagnostics"])
            roles = {m["path"]: m["layer"] for m in self.policy(root)["modules"]}
            self.assertEqual(roles["demo.records"], "Infrastructure")
            modules = {m["path"]: m for m in self.policy(root)["modules"]}
            self.assertNotIn("demo.records", modules["gateway.client.value_utils"]["depends_on"])
            self.assertTrue(any(d["rule"] == "CONFLICTING_FACADE_EXPORT" and
                                d["job"] == "contract-folders" and
                                d["path"] == "src/demo/records/__init__.pyi" and
                                d["line"] > 0 and d["column"] > 0
                                for d in report["diagnostics"]), report["diagnostics"])
            self.assertEqual(next(item for item in report["tools"] if item["name"] == "tach")[
                "status"], "violations")
            self.assertTrue(any(d["job"] == "tach" and d["path"] ==
                                "src/gateway/client/value_utils.py" for d in report["diagnostics"]))
            self.assertEqual(next(item for item in report["tools"] if item["name"] ==
                                  "tach-external")["status"], "violations")
            self.assertTrue(any(d["job"] == "tach-external" and
                                d["rule"] == "UndeclaredExternalDependency" and
                                d["path"] == "src/gateway/client/value_utils.py"
                                for d in report["diagnostics"]), report["diagnostics"])

    def test_non_facade_alias_contract_and_conflict(self) -> None:
        with self.consumer() as root:
            module = root / "src/demo/records/alias_api.py"
            text = "from .service import Service\nPort = Service\n__all__ = ['Port']\n"
            module.write_text(text)
            module.with_suffix(".pyi").write_text(text)
            client = root / "src/gateway/client/value_utils.py"
            client.write_text(client.read_text().replace(
                "from demo import PublicPort", "from demo.records.alias_api import Port as PublicPort"))
            client.with_suffix(".pyi").write_text(client.with_suffix(".pyi").read_text().replace(
                "from demo import PublicPort", "from demo.records.alias_api import Port as PublicPort"))
            self.assertEqual(self.folders(root), [])
            roles = {m["path"]: m["layer"] for m in self.policy(root)["modules"]}
            self.assertEqual(roles["demo.records.alias_api"], "Contract")
            self.assertIn("demo.records.alias_api", {m["path"]: m for m in self.policy(root)[
                "modules"]}["gateway.client.value_utils"]["depends_on"])
            self.assertEqual(capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                                      "from demo.records.alias_api import Port; "
                                      "from demo.records.service import Service; "
                                      "assert Port is Service"], root, 10)["exit_code"], 0)
            module.with_suffix(".pyi").write_text(text.replace("Port = Service", "Port = object"))
            self.rule(self.folders(root), "CONFLICTING_CONTRACT_ALIAS",
                      "src/demo/records/alias_api.pyi")
            self.denied(root, "gateway.client.value_utils", "demo.records.alias_api")
            module.with_suffix(".pyi").write_text(text)
            module.write_text(text.replace("__all__ = ['Port']", "__all__ = ['Missing']"))
            self.rule(self.folders(root), "UNRESOLVED_CONTRACT_ALIAS",
                      "src/demo/records/alias_api.py")

    def test_exact_generic_export_and_bare_import(self) -> None:
        with self.consumer() as root:
            module = root / "src/demo/records/generic_api.py"
            module.write_text("from collections import UserList\n"
                              "Special = UserList[str]\n__all__ = ['Special']\n")
            client = root / "src/gateway/client/value_utils.py"
            client.write_text("from demo.records.generic_api import Special\n" + client.read_text() +
                              "\ndef first(items: Special) -> str:\n    return items[0]\n")
            roles = {m["path"]: m["layer"] for m in self.policy(root)["modules"]}
            self.assertEqual(roles["demo.records.generic_api"], "Contract")
            self.assertIn("^Special$", next(i["expose"] for i in self.policy(root)[
                "interfaces"] if i["from"] == [r"^demo\.records\.generic_api$"]))
            original = client.read_text()
            for assignment, annotation in (("Bare = UserList", "Bare[str]"),
                                           ("Other = UserList[int]", "Other")):
                with self.subTest(assignment=assignment):
                    module.write_text(module.read_text() + assignment + "\n")
                    name = assignment.partition(" = ")[0]
                    client.write_text(f"from demo.records.generic_api import {name}\n" +
                                      original + f"\ndef extra(items: {annotation}) -> str:\n"
                                      "    return items[0]\n")
                    exposed = next(i["expose"] for i in self.policy(root)["interfaces"]
                                   if i["from"] == [r"^demo\.records\.generic_api$"])
                    self.assertNotIn(f"^{name}$", exposed)
                    module.write_text(module.read_text().replace(assignment + "\n", ""))

    def test_assembly_defining_function_only(self) -> None:
        with self.consumer() as root:
            implementation = root / "src/demo/records/sender.py"
            implementation.write_text(implementation.read_text() + "\n__all__ = ['Sender']\n")
            utility = root / "src/demo/records/envelopes/wire_utils.py"
            utility.write_text("def wire() -> str:\n    return 'ok'\n")
            infrastructure = root / "src/demo/records/helpers_utils.py"
            infrastructure.write_text("def suffix() -> str:\n    return ''\n")
            assembly = root / "src/demo/main/wire.py"
            assembly.write_text("from demo.records.envelopes.wire_utils import wire\n"
                                  "from demo.records.helpers_utils import suffix\n" +
                                  assembly.read_text().replace("second('ok')",
                                                               "second(wire() + suffix())"))
            self.rule(self.folders(root), "ENVELOPE_CLASS",
                      "src/demo/records/envelopes/wire_utils.py")
            modules = {m["path"]: m for m in self.policy(root)["modules"]}
            self.assertIn("demo.records.envelopes.wire_utils",
                          modules["demo.main.wire"]["depends_on"])
            self.assertIn("demo.records.helpers_utils", modules["demo.main.wire"]["depends_on"])
            self.assertEqual(capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                                      "from demo.main.wire import connect; "
                                      "assert connect() == 'ready:ready:ok'"], root, 10)["exit_code"], 0)
            ordinary = root / "src/gateway/client/value_utils.py"
            ordinary.write_text("from demo.records.envelopes.wire_utils import wire\n" +
                                ordinary.read_text().replace("port.first(value.identity.code)",
                                                             "port.first(value.identity.code + wire())"))
            self.rule(self.folders(root), "CONCRETE_IMPLEMENTATION_IMPORT",
                      "src/gateway/client/value_utils.py")
            self.denied(root, "gateway.client.value_utils", "demo.records.envelopes.wire_utils")
            peer = root / "src/demo/main/peer.py"
            peer.write_text("from demo.main.wire import connect\n\n"
                            "def result() -> str:\n    return connect()\n")
            self.rule(self.folders(root), "BOOTSTRAP_DEPENDENCY", "src/demo/main/peer.py")
            self.denied(root, "demo.main.peer", "demo.main.wire")


if __name__ == "__main__":
    unittest.main()
