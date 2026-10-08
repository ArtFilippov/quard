"""Full-check regressions for generated-policy review follow-ups."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests/quard/architecture_draft"))
import test_adoption_consumer as adoption

from quard._common import load_toml
from quard._common import capture


class TestReviewFollowup(unittest.TestCase):
    consumer = adoption.TestAdoptionConsumer.consumer
    check = adoption.TestAdoptionConsumer.check
    finding = adoption.TestAdoptionConsumer.finding
    def test_invalid_companion_facade_blocks_import(self) -> None:
        with self.consumer() as root:
            self.check(root)
            stub = root / "src/demo/records/__init__.pyi"
            stub.write_text(stub.read_text() + "from .sender import Sender as Bad\n")
            client = root / "src/gateway/client/value_utils.py"
            client.write_text(client.read_text() + "from demo.records import Port\n")
            report = self.check(root, 1)
            roles = {m["path"]: m["layer"] for m in load_toml(root / "tach.toml")["modules"]}
            self.assertEqual(roles["demo.records"], "Infrastructure")
            modules = {m["path"]: m for m in load_toml(root / "tach.toml")["modules"]}
            self.assertNotIn("demo.records", modules["gateway.client.value_utils"]["depends_on"])
            self.finding(report, "CONFLICTING_FACADE_EXPORT", "contract-folders",
                         "src/demo/records/__init__.pyi")
            self.assertEqual(next(item for item in report["tools"] if item["name"] == "tach")[
                "status"], "violations")
            self.assertTrue(any(d["job"] == "tach" and d["path"] ==
                                "src/gateway/client/value_utils.py" for d in report["diagnostics"]))

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
            self.check(root)
            roles = {m["path"]: m["layer"] for m in load_toml(root / "tach.toml")["modules"]}
            self.assertEqual(roles["demo.records.alias_api"], "Contract")
            self.assertEqual(capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                                      "from demo.records.alias_api import Port; "
                                      "from demo.records.service import Service; "
                                      "assert Port is Service"], root, 10)["exit_code"], 0)
            module.with_suffix(".pyi").write_text(text.replace("Port = Service", "Port = object"))
            report = self.check(root, 1)
            self.finding(report, "CONFLICTING_CONTRACT_ALIAS", "contract-folders",
                         "src/demo/records/alias_api.pyi")
            self.assertEqual(next(item for item in report["tools"] if item["name"] == "tach")[
                "status"], "violations")
            module.with_suffix(".pyi").write_text(text)
            module.write_text(text.replace("__all__ = ['Port']", "__all__ = ['Missing']"))
            self.finding(self.check(root, 1), "UNRESOLVED_CONTRACT_ALIAS", "contract-folders",
                         "src/demo/records/alias_api.py")

    def test_exact_generic_export_and_bare_import(self) -> None:
        with self.consumer() as root:
            module = root / "src/demo/records/generic_api.py"
            module.write_text("from collections import UserList\n"
                              "Special = UserList[str]\n__all__ = ['Special']\n")
            client = root / "src/gateway/client/value_utils.py"
            client.write_text("from demo.records.generic_api import Special\n" + client.read_text() +
                              "\ndef first(items: Special) -> str:\n    return items[0]\n")
            self.check(root)
            roles = {m["path"]: m["layer"] for m in load_toml(root / "tach.toml")["modules"]}
            self.assertEqual(roles["demo.records.generic_api"], "Contract")
            original = client.read_text()
            for assignment, annotation in (("Bare = UserList", "Bare[str]"),
                                           ("Other = UserList[int]", "Other")):
                with self.subTest(assignment=assignment):
                    module.write_text(module.read_text() + assignment + "\n")
                    name = assignment.partition(" = ")[0]
                    client.write_text(f"from demo.records.generic_api import {name}\n" +
                                      original + f"\ndef extra(items: {annotation}) -> str:\n"
                                      "    return items[0]\n")
                    report = self.check(root, 1)
                    self.assertEqual(next(item for item in report["tools"] if item["name"] ==
                                          "tach")["status"], "violations")
                    self.assertTrue(any(d["job"] == "tach" and d["path"] ==
                                        "src/gateway/client/value_utils.py" for d in
                                        report["diagnostics"]))
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
            self.check(root)
            self.assertEqual(capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                                      "from demo.main.wire import connect; "
                                      "assert connect() == 'ready:ready:ok'"], root, 10)["exit_code"], 0)
            ordinary = root / "src/gateway/client/value_utils.py"
            ordinary.write_text("from demo.records.envelopes.wire_utils import wire\n" +
                                ordinary.read_text().replace("port.first(value.identity.code)",
                                                             "port.first(value.identity.code + wire())"))
            report = self.check(root, 1)
            self.finding(report, "CONCRETE_IMPLEMENTATION_IMPORT", "contract-folders",
                         "src/gateway/client/value_utils.py", native=True)
            peer = root / "src/demo/main/peer.py"
            peer.write_text("from demo.main.wire import connect\n\n"
                            "def result() -> str:\n    return connect()\n")
            report = self.check(root, 1)
            self.finding(report, "BOOTSTRAP_DEPENDENCY", "contract-folders",
                         "src/demo/main/peer.py", native=True)


if __name__ == "__main__":
    unittest.main()
