"""Public consumer regressions for the gateway source boundary."""

from __future__ import annotations

from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from adoption_fixture_utils import pin, reviewed_consumer, temporary_consumer
from offline_fixtures import create_repo
from quard.architecture_draft.generated_architecture_utils import render
from quard.architecture_draft.generated_architecture_utils import regenerate
from quard.config_drift_utils import scope_drift
from quard._common import capture
from quard.check_runner.native_check_runner import NativeCheckRunner
from quard.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
from quard.policy_utils import configure


class TestGatewayConsumer(unittest.TestCase):
    """Exercise the public consumer scope and generated native configuration."""

    def test_gateway_source_and_stub_are_excluded(self) -> None:
        with temporary_consumer(probe_plugins=False) as root:
            for name in ("bridge_gw.py", "bridge_gw.pyi"):
                (root / "src/demo/records" / name).write_text("from unknown import Missing\n")
            files = ConfiguredConsumerPolicy(root).files()
            self.assertEqual(files["excluded_gateways"], [
                "src/demo/records/bridge_gw.py", "src/demo/records/bridge_gw.pyi"
            ], "Gateway files were not excluded from production coverage")

    def test_gateway_filename_does_not_exclude_neighbor_or_test(self) -> None:
        with temporary_consumer(probe_plugins=False) as root:
            (root / "src/demo/records/bridge_gw_extra.py").write_text("value = 1\n")
            (root / "tests/demo/records/test_gw.py").write_text("value = 2\n")
            files = ConfiguredConsumerPolicy(root).files()
            self.assertTrue("src/demo/records/bridge_gw_extra.py" in files["production"] and
                            "tests/demo/records/test_gw.py" in files["tests"],
                            "An ordinary neighbor or test was excluded by a gateway suffix")

    def test_pydoclint_keeps_mirrored_gateway_named_test(self) -> None:
        with temporary_consumer(probe_plugins=True) as root:
            (root / "src/demo/records/bridge_gw.py").write_text("value = 1\n")
            mirror = root / "tests/src/demo/records"
            mirror.mkdir(parents=True)
            (mirror / "bridge_gw.py").write_text(
                "def inspect(value: int) -> int:\n"
                "    \"\"\"Broken.\n\n    Args:\n        missing (int): Missing.\n"
                "    \"\"\"\n    return value\n")
            pin(root)
            configure(root)
            report = NativeCheckRunner().run(root)
            self.assertTrue(any(item["tool"] == "pydoclint" and
                                item["path"] == "tests/src/demo/records/bridge_gw.py"
                                for item in report["diagnostics"]),
                            "Pydoclint incorrectly excluded a gateway-named test")

    def test_gateway_only_source_is_empty_for_analysis(self) -> None:
        with TemporaryDirectory(prefix="quard-empty-gateway-") as directory:
            root = Path(directory)
            create_repo(root)
            (root / "src/demo/__init__.py").unlink()
            (root / "src/demo/domain/api.py").unlink()
            (root / "src/demo/domain/bridge_gw.py").write_text("value = 1\n")
            report = NativeCheckRunner().run(root)
            self.assertTrue(report["exit_code"] == 2 and any(
                item["rule"] == "EMPTY_SOURCE_SCOPE" for item in report["diagnostics"]),
                "A gateway-only source tree appeared fully checked")

    def test_native_gateway_exclusions_survive_regeneration(self) -> None:
        with temporary_consumer(probe_plugins=False) as root:
            (root / "src/demo/records/bridge_gw.py").write_text("from unknown import Missing\n")
            nested = root / "src/demo/records/nested"
            nested.mkdir()
            (nested / "bridge_gw.pyi").write_text("from unknown import Missing\n")
            configure(root)
            native = render(root)[0]
            pyright = json.loads((root / "pyrightconfig.json").read_text())
            regenerate(root)
            stable = not regenerate(root)
            self.assertEqual(("src/demo/records/bridge_gw.py" in native["exclude"],
                              "src/demo/records/nested/bridge_gw.pyi" in native["exclude"],
                              "src/demo/records/bridge_gw.py" in pyright["exclude"],
                              "src/demo/records/nested/bridge_gw.pyi" in pyright["ignore"],
                              stable), (True, True, True, True, True),
                             "Gateway native exclusions drifted during regeneration")

    def test_full_check_omits_gateway_diagnostics(self) -> None:
        with temporary_consumer(probe_plugins=True) as root:
            (root / "src/demo/records/bridge_gw.py").write_text(
                "import unknown_missing_package\nclass Broken:\n  pass\n"
                "def broken(value: int) -> int:\n"
                "    \"\"\"Wrong.\n\n    Args:\n        missing (int): Missing.\n"
                "    \"\"\"\n    return value\n")
            (root / "src/demo/records/bridge_gw.pyi").write_text(
                "from unknown_missing_package import Missing\nclass Broken: ...\n")
            client = root / "src/gateway/client/value_utils.py"
            client.write_text("import demo.records.bridge_gw as bridge\n" +
                              client.read_text().replace(
                                  "    return port.first(value.identity.code)",
                                  "    broken = bridge.Broken()\n"
                                  "    return port.first(value.identity.code) if broken else ''"))
            pin(root)
            configure(root)
            report = NativeCheckRunner().run(root)
            status = {job["name"]: job["status"] for job in report["tools"]}
            self.assertEqual((report["exit_code"], report["coverage"]["excluded_gateways"],
                              {name: status.get(name) for name in ("tach", "tach-external",
                               "pyright", "pydoclint", "pylint-production", "stairfmt")}),
                             (0, ["src/demo/records/bridge_gw.py",
                                  "src/demo/records/bridge_gw.pyi"],
                              dict.fromkeys(("tach", "tach-external", "pyright",
                                             "pydoclint", "pylint-production", "stairfmt"),
                                            "passed")),
                             f"The gateway full check lost native coverage: {report['diagnostics']}")

    def test_ordinary_and_bootstrap_import_gateway_alias(self) -> None:
        with temporary_consumer(probe_plugins=True) as root:
            (root / "src/demo/records/bridge_gw.py").write_text(
                "from collections import UserList as Bridge\n__all__ = ['Bridge']\n")
            (root / "src/demo/records/bridge_gw.pyi").write_text(
                "from collections import UserList as Bridge\n__all__ = ['Bridge']\n")
            client = root / "src/gateway/client/value_utils.py"
            client.write_text(client.read_text().replace(
                "from demo import PublicPort\n",
                "from demo import PublicPort\nfrom demo.records.bridge_gw import Bridge\n"
                "import demo.records.bridge_gw as bridge\n").replace(
                "    return port.first(value.identity.code)",
                "    first = Bridge([''])[0]\n    second = bridge.Bridge([''])[0]\n"
                "    return port.first(value.identity.code) + first + second"))
            stub = root / "src/gateway/client/value_utils.pyi"
            stub.write_text("from demo.records.bridge_gw import Bridge\n" + stub.read_text() +
                            "def read_bridge(value: Bridge[str]) -> str: ...\n")
            client.write_text(client.read_text() +
                              "\ndef read_bridge(value: Bridge[str]) -> str:\n"
                              "    return value[0]\n")
            wire = root / "src/demo/main/wire.py"
            wire.write_text("from demo.records.bridge_gw import Bridge\n" +
                wire.read_text().replace("    return Adapter(\n",
                                         "    part = Bridge([''])[0]\n    result = Adapter(\n") +
                "    return part + result\n")
            pin(root)
            configure(root)
            report = NativeCheckRunner().run(root)
            runtime = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                               "from demo.records.bridge_gw import Bridge; "
                               "assert Bridge(['ok'])[0] == 'ok'"], root, 15)
            self.assertEqual((report["exit_code"], runtime["exit_code"]), (0, 0),
                             f"Gateway imports failed static or runtime checks: {report['diagnostics']}")

    def test_missing_gateway_import_remains_diagnostic(self) -> None:
        with temporary_consumer(probe_plugins=True) as root:
            client = root / "src/gateway/client/value_utils.py"
            client.write_text("from demo.records.missing_gw import Missing\n" +
                              client.read_text())
            pin(root)
            configure(root)
            report = NativeCheckRunner().run(root)
            self.assertTrue(any(item["path"] == "src/gateway/client/value_utils.py" and
                                item["tool"] == "pyright" for item in report["diagnostics"]),
                            "A nonexistent gateway import escaped importer diagnostics")

    def test_gateway_import_does_not_exempt_ordinary_facade(self) -> None:
        with temporary_consumer(probe_plugins=True) as root:
            (root / "src/demo/records/bridge_gw.py").write_text(
                "from collections import UserList as Bridge\n__all__ = ['Bridge']\n")
            facade = root / "src/demo/records/__init__.py"
            facade.write_text(facade.read_text() +
                              "from .bridge_gw import Bridge as PublicBridge\n")
            pin(root)
            configure(root)
            report = NativeCheckRunner().run(root)
            self.assertTrue(any(item["path"] == "src/demo/records/__init__.py" and
                                item["tool"] == "contract-folders" and
                                item["rule"] == "UNRESOLVED_FACADE_EXPORT"
                                for item in report["diagnostics"]),
                            "A facade gained an exemption from its gateway import")

    def test_gateway_filename_does_not_exempt_same_named_package(self) -> None:
        with temporary_consumer(probe_plugins=True) as root:
            group = root / "src/demo/records"
            (group / "bridge_gw.py").write_text("from collections import UserList as Bridge\n")
            package = group / "bridge_gw"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (group / "peer.py").write_text(
                "from demo.records.bridge_gw import Ghost\n"
                "class Peer(Ghost):\n    pass\n")
            pin(root)
            configure(root)
            report = NativeCheckRunner().run(root)
            self.assertTrue(any(item["path"] == "src/demo/records/peer.py" and
                                item["rule"] == "UNRESOLVED_GROUP_OWNER"
                                for item in report["diagnostics"]),
                            "A same-named ordinary package acquired gateway ancestry")

    def test_ordinary_sibling_keeps_concrete_import_finding(self) -> None:
        with temporary_consumer(probe_plugins=True) as root:
            (root / "src/demo/records/bridge_gw.py").write_text("value = 1\n")
            (root / "src/demo/records/neighbor.py").write_text(
                "from demo.records.sender import Sender\n")
            pin(root)
            configure(root)
            report = NativeCheckRunner().run(root)
            self.assertTrue(any(item["path"] == "src/demo/records/neighbor.py" and
                                item["rule"] == "CONCRETE_IMPLEMENTATION_IMPORT"
                                for item in report["diagnostics"]),
                            "The gateway exclusion spread to a neighboring implementation")

    def test_external_check_still_reports_ordinary_imports(self) -> None:
        with temporary_consumer(probe_plugins=True) as root:
            (root / "src/demo/records/bridge_gw.py").write_text("import tomlkit\n")
            client = root / "src/gateway/client/value_utils.py"
            client.write_text("import tomlkit\n" + client.read_text())
            pin(root)
            configure(root)
            report = NativeCheckRunner().run(root)
            self.assertTrue(any(item["tool"] == "tach" and
                                item["job"] == "tach-external" and
                                item["rule"] == "UndeclaredExternalDependency" and
                                item["path"] == "src/gateway/client/value_utils.py"
                                for item in report["diagnostics"]),
                            "The gateway disabled Tach's external check on ordinary code")

    def test_explicit_stairfmt_fix_skips_gateway(self) -> None:
        with temporary_consumer(probe_plugins=False) as root:
            gateway = root / "src/demo/records/bridge_gw.py"
            gateway.write_text("value = f(g(h(1)))\n")
            result = capture([sys.executable, str(QUARD / "stairfmt.py"), "--fix",
                              "--project", str(root), "--json",
                              "src/demo/records/bridge_gw.py"], root, 30)
            report = json.loads(result["stdout"])
            self.assertEqual((report["exit_code"], report["checked_files"],
                              gateway.read_text()), (0, 0, "value = f(g(h(1)))\n"),
                             "Explicit Stairfmt changed or analyzed the excluded gateway")

    def test_reviewed_gateway_requires_apply_without_replacing_architecture(self) -> None:
        with TemporaryDirectory(prefix="quard-reviewed-gateway-") as directory:
            root = Path(directory)
            create_repo(root)
            gateway = root / "src/demo/domain/bridge_gw.py"
            gateway.write_text("from collections import UserList as Bridge\n")
            before = (root / "tach.toml").read_text()
            drift = scope_drift(root)
            stale = NativeCheckRunner().run(root)
            applied = capture([sys.executable, str(QUARD / "apply.py"), "--project",
                                str(root), "--json"], root, 30)
            after = (root / "tach.toml").read_text()
            repeated = capture([sys.executable, str(QUARD / "apply.py"), "--project",
                                str(root), "--json"], root, 30)
            self.assertEqual(("tach.exclude" in drift, stale["exit_code"],
                              "SCOPE_NOT_APPLIED" in {item["rule"] for item in stale["diagnostics"]},
                              applied["exit_code"], after.replace(
                                  ', "src/demo/domain/bridge_gw.py"]', ']') == before,
                              json.loads(repeated["stdout"])["modified_files"]),
                             (True, 2, True, 0, True, []),
                             "Reviewed gateway apply did not preserve accepted policy")

    def test_reviewed_consumer_imports_gateway_after_apply(self) -> None:
        with TemporaryDirectory(prefix="quard-reviewed-gateway-") as directory:
            root = Path(directory)
            reviewed_consumer(root)
            report = NativeCheckRunner().run(root)
            runtime = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                               "from demo.domain.bridge_gw import Bridge; "
                               "assert Bridge(['ok'])[0] == 'ok'"], root, 15)
            self.assertEqual((report["exit_code"], report["coverage"]["excluded_gateways"],
                              runtime["exit_code"]),
                             (0, ["src/demo/domain/bridge_gw.py"], 0),
                             f"Reviewed gateway check failed: {report['diagnostics']}")

    def test_reviewed_unused_dependency_stays_native(self) -> None:
        with TemporaryDirectory(prefix="quard-reviewed-gateway-") as directory:
            root = Path(directory)
            reviewed_consumer(root)
            native = root / "tach.toml"
            native.write_text(native.read_text().replace(
                'depends_on = ["demo.domain.value.value"]',
                'depends_on = ["demo.domain.value.value", "demo.domain.api"]', 1))
            unused = NativeCheckRunner().run(root)
            self.assertTrue(any(item["tool"] == "tach" and
                                item["rule"] == "unused-dependency"
                                for item in unused["diagnostics"]),
                            f"Reviewed Tach silently accepted an unused declaration: {unused['diagnostics']}")


if __name__ == "__main__":
    unittest.main()
