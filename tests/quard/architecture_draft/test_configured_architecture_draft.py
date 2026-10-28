"""Standalone regressions for drafting and explicitly accepting consumer architecture."""

from __future__ import annotations

from pathlib import Path
from importlib import import_module
import sys
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, override
import unittest
from unittest.mock import patch

import tomlkit

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from quard._common import QuardError, capture, load_toml, read_json, write_json
from quard.architecture_draft.generated_architecture_utils import render
from quard.architecture_draft.configured_architecture_draft import ConfiguredArchitectureDraft
from quard.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
from quard.contract_analysis.dependency_type_utils import analyze as dependency_types
from quard.folder_analysis.folder_utils import analyze as contract_folders
from quard.outer_classes_worker_utils import analyze as outer_classes
from quard.test_placement_analysis.placement_utils import analyze as placement_analysis
from offline_fixtures import CATALOG, create_repo, native_result
from legacy_clone_fixture_utils import historical_cli
from mutation_fixture_utils import temporary_edits
from selected_report_utils import selected_report

if TYPE_CHECKING:
    from quard.architecture_draft.generated_architecture_utils import (
        IDENTITY, generated, regenerate,
    )
    from quard.architecture_draft.generated_selection_utils import LEGACY_IDENTITY
    from quard.audit_utils import discover_plugins
    from quard.installation.transactional_installation import TransactionalInstallation
    from quard.policy_utils import configure
    from quard import apply_utils, install as entry
else:
    generated_policy = import_module("quard.architecture_draft.generated_architecture_utils")
    IDENTITY = generated_policy.IDENTITY
    LEGACY_IDENTITY = import_module(
        "quard.architecture_draft.generated_selection_utils").LEGACY_IDENTITY
    generated = generated_policy.generated
    regenerate = generated_policy.regenerate
    discover_plugins = import_module("quard.audit_utils").discover_plugins
    configure = import_module("quard.policy_utils").configure
    TransactionalInstallation = import_module(
        "quard.installation.transactional_installation").TransactionalInstallation
    apply_utils = import_module("quard.apply_utils")
    entry = import_module("quard.install")


class TestConfiguredArchitectureDraft(unittest.TestCase):
    def setUp(self) -> None:
        historical_cli(self)

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path(self.enterContext(
            TemporaryDirectory(prefix="quard-architecture with spaces-")))
        create_repo(self.root)
        self.architecture = ConfiguredArchitectureDraft(self.root)

    def test_one_package_and_invalid_layouts(self):
        self.assertEqual(self.architecture.package_name(), "demo")
        (self.root / "src/second").mkdir()
        (self.root / "src/second/a.py").write_text("pass")
        with self.assertRaisesRegex(QuardError, "one import package"):
            self.architecture.draft()
        (self.root / "src/second/a.py").unlink()
        (self.root / "src/root.py").write_text("pass")
        with self.assertRaisesRegex(QuardError, "one import package"):
            self.architecture.package_name()
        (self.root / "src/root.py").unlink()
        (self.root / "src/demo").rename(self.root / "src/not-valid")
        with self.assertRaisesRegex(QuardError, "Invalid Python package name"):
            self.architecture.package_name()

    def test_layer_discovery_prohibitions_and_public_exports(self):
        for folder in ("application", "infrastructure", "interfaces", "misc"):
            target = self.root / "src/demo" / folder
            target.mkdir()
            (target / "api.py").write_text("pass")
        (self.root / "src/demo/main.py").write_text("pass")
        draft, notes = self.architecture.draft()
        layers = {module["path"]: module["layer"] for module in draft["modules"]}
        self.assertEqual(layers["demo.application"], "UseCase")
        self.assertEqual(layers["demo.infrastructure"], "Infrastructure")
        self.assertEqual(layers["demo.interfaces"], "Interface")
        self.assertEqual(layers["demo.misc"], "Domain")
        self.assertEqual(layers["demo.main"], "Bootstrap")
        domain = next(module for module in draft["modules"] if module["path"] == "demo.domain")
        self.assertIn("demo.application", domain["cannot_depend_on"])
        self.assertEqual(domain["depends_on"], [])
        self.assertEqual(draft["interfaces"][-1]["expose"], [r"api(?:\..*)?"])
        self.assertTrue(draft["exact"])
        self.assertFalse(draft["ignore_type_checking_imports"])
        self.assertTrue(any("tach sync records current imports" in note for note in notes))

    def test_validate_native_architecture_config(self):
        self.assertEqual(self.architecture.validate()["root_module"], "forbid")
        path = self.root / "tach.toml"
        original = load_toml(path)
        for key, value in (("root_module", "ignore"), ("source_roots", ["other"]),
                           ("interfaces", []), ("modules", [])):
            changed = dict(original, **{key: value})
            path.write_text(tomlkit.dumps(changed))
            with self.subTest(key=key), self.assertRaises(QuardError):
                self.architecture.validate()
        for change in ({"layer": "Unknown"}, {"depends_on": "demo.domain"}, {"unchecked": True}):
            changed = dict(original)
            changed["modules"] = [dict(original["modules"][0], **change)]
            path.write_text(tomlkit.dumps(changed))
            with self.subTest(change=change), self.assertRaises(QuardError):
                self.architecture.validate()
        path.unlink()
        with self.assertRaisesRegex(QuardError, "review tach.toml"):
            self.architecture.validate()

    def test_existing_reviewed_config_is_retained_without_force(self):
        before = (self.root / "tach.toml").read_bytes()
        with patch("quard.architecture_draft.configured_architecture_draft.capture") as capture:
            event, details = self.architecture.initialize()
        capture.assert_not_called()
        self.assertEqual(event, "review_required")
        self.assertIn("Existing configuration retained", details["message"])
        self.assertEqual((self.root / "tach.toml").read_bytes(), before)
        self.assertTrue(read_json(self.root / ".quard/architecture.ready.json")["accepted"])

    def test_force_generates_unaccepted_draft_and_syncs_once(self):
        def sync(argv: list[str], cwd: Path) -> dict[str, Any]:
            self.assertEqual(argv[-1], "sync")
            self.assertEqual(cwd, self.root)
            path = cwd / "tach.toml"
            path.write_text(path.read_text() + "\n# native sync retained\n")
            return native_result(argv)

        with patch("quard.architecture_draft.configured_architecture_draft.capture",
                   side_effect=sync) as capture:
            event, details = self.architecture.initialize(force=True)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(event, "review_required")
        self.assertIn("notes", details)
        self.assertFalse((self.root / ".quard/architecture.ready.json").exists())
        self.assertIn("# native sync retained", (self.root / "tach.toml").read_text())
        self.assertEqual(load_toml(self.root / "tach.toml")["modules"][0]["depends_on"], [])

    def test_accept_preserves_boundaries_and_reports_violations(self):
        path = self.root / "tach.toml"
        config = load_toml(path)
        config["modules"][0]["depends_on"] = ["demo.domain"]
        path.write_text(tomlkit.dumps(config))
        before = path.read_bytes()
        write_json(self.root / ".quard/installation.json",
                   {"architecture_initialization_required": True})
        def check(argv: list[str], cwd: Path) -> dict[str, Any]:
            self.assertEqual(argv[-1], "check")
            self.assertEqual(cwd, self.root)
            return native_result(
                argv, code=1, stdout="src/demo/domain/api.py[L1]: dependency violation")

        with patch("quard.architecture_draft.configured_architecture_draft.capture",
                   side_effect=check) as capture:
            event, details = self.architecture.initialize(accept=True)
        capture.assert_called_once()
        self.assertEqual(event, "initialized")
        self.assertEqual(details["architecture_violations"], 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("NOT a violations baseline",
                      read_json(self.root / ".quard/architecture.ready.json")["note"])
        self.assertFalse(read_json(self.root / ".quard/installation.json")
                         ["architecture_initialization_required"])

    def test_accept_rejects_native_error_without_approval(self):
        marker = self.root / ".quard/architecture.ready.json"
        marker.unlink()
        with patch("quard.architecture_draft.configured_architecture_draft.capture",
                   return_value=native_result(["tach"], code=2, stderr="invalid native config")):
            with self.assertRaisesRegex(QuardError, "Cannot validate Tach config"):
                self.architecture.initialize(accept=True)
        self.assertFalse(marker.exists())
        self.assertIn("invalid native config",
                      (self.root / ".quard/logs/init-check.log").read_text())

    def test_multi_package_reviewed_boundaries_are_accepted_without_regeneration(self):
        (self.root / "src/second").mkdir()
        (self.root / "src/second/api.py").write_text("pass")
        path = self.root / "tach.toml"
        config = load_toml(path)
        config["modules"].append({"path": "second", "layer": "Interface", "depends_on": []})
        path.write_text(tomlkit.dumps(config))
        before = path.read_bytes()
        with patch("quard.architecture_draft.configured_architecture_draft.capture",
                   return_value=native_result(["tach"], stdout="", code=0)) as capture:
            event, details = self.architecture.initialize(accept=True)
        self.assertEqual(event, "initialized")
        self.assertEqual(details["module_count"], len(config["modules"]))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(capture.call_args.args[0][-1], "check")

    def test_sync_failure_does_not_approve_draft(self):
        with patch("quard.architecture_draft.configured_architecture_draft.capture",
                   return_value=native_result(["tach", "sync"], code=1, stderr="invalid imports")):
            with self.assertRaisesRegex(QuardError, "sync failed"):
                self.architecture.initialize(force=True)
        self.assertFalse((self.root / ".quard/architecture.ready.json").exists())
        self.assertIn("invalid imports", (self.root / ".quard/logs/init-sync.log").read_text())

    def test_command_uses_selected_consumer_and_reports_review_required(self):
        from quard import init_architecture

        with patch.object(sys, "argv", ["init_architecture.py", "--project",
                                        str(self.root), "--json"]), \
             patch.object(init_architecture, "require_runtime"), \
             patch.object(init_architecture, "emit_event") as event:
            self.assertEqual(init_architecture.main(), 0)
        self.assertEqual(event.call_args.args, ("review_required",))
        self.assertEqual(event.call_args.kwargs["exit_code"], 0)

    def generated_fixture(self) -> Path:
        root = self.root
        (root / "tach.toml").unlink()
        (root / ".quard/architecture.ready.json").unlink()
        port = root / "src/demo/port"
        port.mkdir()
        (port / "port.py").write_text(
            "from typing import Protocol\nclass Port(Protocol):\n"
            + "    def send(self) -> None: ...\n", encoding="utf-8")
        (port / "impl.py").write_text(
            "from demo.port.port import Port\nclass Impl(Port):\n"
            + "    def send(self) -> None: pass\n", encoding="utf-8")
        (root / "src/demo/client.py").write_text(
            "from demo.port.port import Port\n", encoding="utf-8")
        return root

    def test_generated_pylint_upgrade_removes_old_plugin_and_map(self):
        root = self.generated_fixture()
        write_json(root / ".quard/architecture.generated.json", IDENTITY)
        project = root / "pyproject.toml"
        before = project.read_bytes()
        self.assertTrue(regenerate(root))
        self.assertEqual(project.read_bytes(), before)
        self.assertIn("clean_architecture_linter", load_toml(project)["tool"][
            "pylint"]["main"]["load-plugins"])
        configure(root)
        tool = load_toml(project)["tool"]
        self.assertNotIn("clean-arch", tool)
        for config in (tool["pylint"], load_toml(root / ".quard/pylint-tests.toml")[
                "tool"]["pylint"]):
            self.assertNotIn("clean_architecture_linter", config["main"]["load-plugins"])
            self.assertFalse(set(config["messages_control"]["disable"]) & {"W9001", "W9201"})

    def test_reviewed_w9201_is_disabled_on_install_and_upgrade(self):
        for fresh in (True, False):
            with self.subTest(fresh=fresh), TemporaryDirectory(
                    prefix="quard-reviewed-w9201-") as directory:
                root = Path(directory)
                create_repo(root)
                project = root / "pyproject.toml"
                doc = tomlkit.parse(project.read_text(encoding="utf-8"))
                pylint = doc["tool"]["pylint"]
                pylint["design"]["ignored-parents"].append("foreign.Framework")
                pylint["messages_control"]["disable"].append("consumer-own-rule")
                pylint["messages_control"]["enable"].append("W9202")
                project.write_text(tomlkit.dumps(doc), encoding="utf-8")
                configure(root, initial=fresh, plugins=CATALOG["plugins"], catalog=CATALOG)
                production = load_toml(project)["tool"]["pylint"]
                tests = load_toml(root / ".quard/pylint-tests.toml")["tool"]["pylint"]
                self.assertNotIn("W9202", production["messages_control"]["disable"])
                for config in (production, tests):
                    control = config["messages_control"]
                    self.assertEqual(control["disable"].count("W9201"), 1)
                    self.assertIn("consumer-own-rule", control["disable"])
                    self.assertIn("foreign.Framework", config["design"]["ignored-parents"])
                    self.assertIn("W9202", control["enable"])
                self.assertFalse(configure(root))
                notes = read_json(root / ".quard/policy-notes.json")
                self.assertIn("W9201", notes["production_disabled"])

    def test_reviewed_native_w9202_and_unrelated_rules_still_report(self):
        root = self.root
        (root / "src/demo/domain/api.py").write_text(
            "from typing import Protocol\nclass Port(Protocol):\n"
            "    def run(self): ...\n", encoding="utf-8")
        adapter = root / "src/demo/infrastructure"
        adapter.mkdir()
        (adapter / "driver.py").write_text(
            "import os\nfrom demo.domain.api import Port\n"
            "class Driver(Port):\n"
            "    def run(self): return 1\n"
            "    def extra(self): pass\n", encoding="utf-8")
        report = selected_report(self, root, "pylint-production", "class-contracts")
        rules = {(item["rule"], item["path"]) for item in report["diagnostics"]}
        self.assertIn(("W9202", "src/demo/infrastructure/driver.py"), rules)
        self.assertIn(("W0611", "src/demo/infrastructure/driver.py"), rules)
        self.assertNotIn(("W9201", "src/demo/infrastructure/driver.py"), rules)
        self.assertFalse(any(item["rule"] == "UNCOVERED_CLASS_MEMBER" for item in
                             report["diagnostics"]))

    def test_return_contract_and_naming_jobs_in_both_architecture_modes(self):
        for managed in (False, True):
            with self.subTest(generated=managed), TemporaryDirectory(
                    prefix="quard-return-policy-") as directory:
                root = Path(directory)
                create_repo(root, ready=not managed)
                if managed:
                    write_json(root / ".quard/architecture.generated.json", IDENTITY)
                    configure(root, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
                (root / "src/demo/domain/api.py").write_text(
                    "from typing import Protocol\nclass Port(Protocol):\n"
                    "    def fetch(self): ...\n    def _hidden(self): ...\n",
                    encoding="utf-8")
                folder = root / "src/demo/infrastructure"
                folder.mkdir()
                (folder / "driver.py").write_text(
                    "from demo.domain.api import Port\n"
                    "class Connection: pass\n"
                    "class Driver(Port):\n"
                    "    def fetch(self): return Connection()\n"
                    "    def _hidden(self): return Connection()\n"
                    "    def helper(self) -> Port: return Driver()\n"
                    "    def _old_helper(self) -> Port: return Driver()\n"
                    "    def stub(self): pass\n", encoding="utf-8")
                jobs = ("pylint-production", "class-contracts", "leading-underscores")
                report = selected_report(self, root, *jobs)
                findings = {(item["rule"], item["symbol"]) for item in report["diagnostics"]}
                self.assertIn(("LEADING_UNDERSCORE_NAME", "_hidden"), findings)
                self.assertIn(("LEADING_UNDERSCORE_NAME", "_old_helper"), findings)
                rules = {(item["rule"], item["path"], item["symbol"])
                         for item in report["diagnostics"]}
                driver = "src/demo/infrastructure/driver.py"
                if managed:
                    self.assertFalse(any(rule.startswith("W9") for rule, _, _ in rules), rules)
                else:
                    self.assertEqual({item["line"] for item in report["diagnostics"] if
                                      item["rule"] == "W9007" and item["path"] == driver},
                                     {4, 5})
                    self.assertIn(("W9202", driver, "concrete-method-stub"), rules)
                    self.assertFalse(any(rule == "W9201" for rule, _, _ in rules), rules)
                config = root / ".quard/config.toml"
                config.write_text(config.read_text(encoding="utf-8").replace(
                    "leading_underscores = true", "leading_underscores = false"), encoding="utf-8")
                independent = selected_report(self, root, "pylint-production", "class-contracts")
                naming = next(tool for tool in independent["tools"] if tool["name"] ==
                              "leading-underscores")
                self.assertEqual((naming["status"], naming["exit_code"]), ("skipped", None))
                self.assertFalse(any(item["rule"].startswith("LEADING_UNDERSCORE") for item in
                                     independent["diagnostics"]))
                before = {(item["rule"], item["path"], item["symbol"]) for item in
                          report["diagnostics"] if item["tool"] != "leading-underscores"}
                after = {(item["rule"], item["path"], item["symbol"]) for item in
                         independent["diagnostics"]}
                self.assertEqual(before, after)

    def test_generated_pylint_report_and_reviewed_profile(self):
        for managed in (False, True):
            with self.subTest(generated=managed), TemporaryDirectory(
                    prefix="quard-pylint-mode-") as directory:
                root = Path(directory) if managed else self.root
                if managed:
                    create_repo(root, ready=False)
                    write_json(root / ".quard/architecture.generated.json", IDENTITY)
                reviewed_tach = (root / "tach.toml").read_bytes() if not managed else None
                reviewed_layers = (load_toml(root / "pyproject.toml")["tool"]["clean-arch"]
                                   if not managed else None)
                source = root / "src/demo/domain/api.py"
                source.write_text("import socket\nfrom typing import Protocol\n" +
                                  "class Port(Protocol):\n    def fetch(self) -> int: ...\n",
                                  encoding="utf-8")
                (root / "src/demo/client.py").write_text("from demo.domain import api\n",
                                                          encoding="utf-8")
                (root / "src/demo/domain/small.py").write_text(
                    "class Small(object):\n    def fetch(self) -> int:\n        return 1\n",
                    encoding="utf-8")
                (root / "tests/test_value.py").write_text("import pathlib\n", encoding="utf-8")
                logs = root / ".quard/logs/install-probes"
                logs.mkdir(parents=True, exist_ok=True)
                plugins, catalog = (discover_plugins(root, logs) if managed else
                                    (CATALOG["plugins"], CATALOG))
                write_json(root / ".quard/pylint-catalog.json", catalog)
                configure(root, initial=True, plugins=plugins, catalog=catalog)
                if reviewed_tach is not None:
                    self.assertEqual((root / "tach.toml").read_bytes(), reviewed_tach)
                production = load_toml(root / "pyproject.toml")["tool"]
                if reviewed_layers is not None:
                    self.assertEqual(production["clean-arch"], reviewed_layers)
                tests = load_toml(root / ".quard/pylint-tests.toml")["tool"]["pylint"]
                for config in (production["pylint"], tests):
                    self.assertEqual("clean_architecture_linter" in config["main"]["load-plugins"],
                                     not managed)
                self.assertEqual("clean-arch" in production, not managed)
                self.assertEqual(any(code.startswith("W9") for code in catalog["message_ids"]),
                                 not managed)

                if managed:
                    report = selected_report(self, root, "pylint-production", "pylint-tests")
                    self.assertEqual(report["exit_code"], 1, report["diagnostics"])
                    rules = {(item["tool"], item["rule"], item["path"])
                             for item in report["diagnostics"]}
                    self.assertIn(("pylint", "W0611", "src/demo/domain/api.py"), rules)
                    self.assertIn(("pylint", "W0611", "tests/test_value.py"), rules)
                    self.assertIn(("pylint", "R0903", "src/demo/domain/small.py"), rules)
                    self.assertFalse(any(item["rule"].startswith("W9") for item in
                                         report["diagnostics"]))
                    self.assertFalse(any(item["rule"] in {"E1111", "R0903"} and
                                         item["path"] == "src/demo/domain/api.py"
                                         for item in report["diagnostics"]))
                    self.assertEqual(read_json(root / ".quard/reports/latest.json"), report)
                if reviewed_tach is not None:
                    self.assertEqual((root / "tach.toml").read_bytes(), reviewed_tach)

    def install_generated_fixture(self, root: Path) -> dict[str, Any]:
        policy = import_module("quard.consumer_policy.configured_consumer_policy")

        def fake_uv(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            if "_audit.py" in " ".join(argv):
                policy.ConfiguredConsumerPolicy(cwd).apply(
                    initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
                write_json(cwd / ".quard/logs/install-probes/result.json",
                           {"versions": {}, "smoke_tests": {"native": "mocked"}})
            return native_result(argv)

        with patch.object(entry, "capture", fake_uv), patch.object(
                entry, "clone_source", return_value=None):
            return TransactionalInstallation(entry).install(root, QUARD, progress=False)

    def test_generated_install_apply_check_and_external_project(self):
        root = self.generated_fixture()
        tool_tach = QUARD / "tach.toml"
        original_tool = tool_tach.read_bytes() if tool_tach.exists() else None
        installed = self.install_generated_fixture(root)
        self.assertFalse(installed["architecture_initialization_required"])
        self.assertTrue(generated(root))
        self.assertIn("/tach.toml", (root / ".gitignore").read_text().splitlines())
        path = root / "tach.toml"
        expected = path.read_bytes()
        self.assertFalse(regenerate(root))
        self.assertEqual(path.read_bytes(), expected)
        path.write_text("exact = false\n")
        with patch.object(sys, "argv", ["apply.py", "--project", str(root), "--json"]):
            self.assertEqual(apply_utils.main(), 0)
        self.assertEqual(path.read_bytes(), expected)
        path.unlink()
        self.assertTrue(regenerate(root))
        self.assertEqual(path.read_bytes(), expected)
        self.assertEqual(tool_tach.read_bytes() if tool_tach.exists() else None,
                         original_tool)
        self.assertEqual(self.install_generated_fixture(root)["modified_files"], [])

    def test_existing_version_one_generated_boundaries_are_retained(self):
        root = self.generated_fixture()
        write_json(root / ".quard/architecture.generated.json", LEGACY_IDENTITY)
        expected, _ = ConfiguredArchitectureDraft(root).draft(
            contract_only=True, validate_ownership=False)
        self.assertTrue(regenerate(root))
        self.assertEqual(load_toml(root / "tach.toml"), expected)
        self.assertFalse(regenerate(root))
        self.assertEqual(read_json(root / ".quard/architecture.generated.json"), LEGACY_IDENTITY)

    def test_generated_toml_retains_every_boundary(self):
        root = self.generated_fixture()
        write_json(root / ".quard/architecture.generated.json", IDENTITY)
        config, content = render(root)
        self.assertEqual(tomlkit.loads(content).unwrap(), config,
                         "Generated TOML lost architecture boundaries")

    def multi_package_fixture(self, root: Path) -> Path:
        create_repo(root, ready=False)
        (root / "src/demo/domain/api.py").write_text("")
        (root / "tests/test_value.py").write_text("")
        for folder in ("src/placeholder", "src/demo/arbitrary", "src/other",
                       "src/other/group", "src/other/group/deep"):
            target = root / folder
            target.mkdir(exist_ok=True, parents=True)
            (target / "__init__.py").write_text("")
        service = root / "src/demo/arbitrary/service.py"
        declaration = ("from typing import Protocol\n\n" +
                       "class Service(Protocol):\n" +
                       "    def first(self, value: str) -> str: ...\n" +
                       "    def second(self, value: str) -> str: ...\n")
        service.write_text(declaration)
        service.with_suffix(".pyi").write_text(declaration)
        sender = root / "src/demo/arbitrary/sender.py"
        sender.write_text(
            "from typing import override\nfrom demo.arbitrary.service import Service\n\n" +
            "class Sender(Service):\n" +
            "    def __init__(self) -> None:\n" +
            "        super().__init__()\n" +
            "        self._prefix: str = 'ready:'\n\n" +
            "    @override\n" +
            "    def first(self, value: str) -> str:\n" +
            "        return self.second(value)\n\n" +
            "    @override\n" +
            "    def second(self, value: str) -> str:\n" +
            "        return self._prefix + value\n")
        sender.with_suffix(".pyi").write_text(
            "from typing import override\nfrom demo.arbitrary.service import Service\n\n" +
            "class Sender(Service):\n" +
            "    @override\n" +
            "    def first(self, value: str) -> str: ...\n" +
            "    @override\n" +
            "    def second(self, value: str) -> str: ...\n")
        (root / "src/other/group/main.py").write_text(
            "from demo.arbitrary.service import Service as PublicService\n" +
            "__all__ = ['PublicService']\n")
        alias = root / "src/other/group/deep/aliases.py"
        alias.write_text("from demo.arbitrary.service import Service as PublicService\n" +
                         "__all__ = ['PublicService']\n")
        utility = root / "src/other/group/deep/operations_utils.py"
        utility.write_text("from other.group.deep.aliases import PublicService\n\n" +
                           "def inspect(service: PublicService) -> str:\n" +
                           "    return service.first('ok')\n")
        utility.with_suffix(".pyi").write_text(
            "from other.group.deep.aliases import PublicService\n\n" +
            "def inspect(service: PublicService) -> str: ...\n")
        installed = self.install_generated_fixture(root)
        self.assertFalse(installed["architecture_initialization_required"])
        self.assertTrue(generated(root))
        configure(root, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
        return utility

    def focused_diagnostics(self, root: Path, job: str) -> list[dict[str, Any]]:
        """Exercise a real rule against the same discovered scope as a full check."""
        policy = ConfiguredConsumerPolicy(root)
        files = policy.files()
        paths = sorted(files["production"] + files["tests"])
        if job == "contract-folders":
            diagnostics, error = contract_folders(root, paths)
        elif job == "dependency-types":
            diagnostics, error = dependency_types(root, paths)
        elif job == "test-placement":
            diagnostics, error = placement_analysis(root, files["production"], files["tests"],
                                                    policy.scope()["test_directory_names"])
        elif job == "outer-classes":
            diagnostics, error = outer_classes(root, paths)
        else:
            raise AssertionError(f"Unknown focused job: {job}")
        self.assertIsNone(error, diagnostics)
        self.assertFalse(any(item.get("is_infrastructure_error") for item in diagnostics),
                         diagnostics)
        return diagnostics

    def root_main_fixture(self) -> Path:
        write_json(self.root / ".quard/architecture.generated.json", IDENTITY)
        main = self.root / "src/main"
        main.mkdir()
        return main

    def root_main_sender_fixture(self) -> Path:
        main = self.root_main_fixture()
        (self.root / "src/demo/service.py").write_text(
            "from typing import Protocol\nclass Service(Protocol): pass\n")
        (self.root / "src/demo/sender.py").write_text(
            "from demo.service import Service\nclass Sender(Service): pass\n")
        return main

    def test_root_main_generated_role_and_concrete_import(self):
        main = self.root_main_sender_fixture()
        (main / "wire.py").write_text("from demo.sender import Sender\ndef wire(): return Sender()\n")
        (main / "wire.pyi").write_text("from demo.sender import Sender\ndef wire() -> Sender: ...\n")
        modules = {item["path"]: item for item in render(self.root)[0]["modules"]}
        self.assertEqual((modules["main.wire"]["layer"], modules["main.wire"]["depends_on"]),
                         ("Bootstrap", ["demo.sender"]))

    def test_root_main_is_not_an_importable_dependency(self):
        main = self.root_main_fixture()
        (main / "wire.py").write_text("def wire(): pass\n")
        client = self.root / "src/demo/client.py"
        client.write_text("from main.wire import wire\n")
        client.with_suffix(".pyi").write_text("from main.wire import wire\n")
        findings = self.focused_diagnostics(self.root, "contract-folders")
        self.assertEqual({item["path"] for item in findings
                          if item["rule"] == "BOOTSTRAP_DEPENDENCY"},
                         {"src/demo/client.py", "src/demo/client.pyi"})

    def test_root_main_can_assemble_classes_and_annotate_concrete_types(self):
        main = self.root_main_sender_fixture()
        for suffix in (".py", ".pyi"):
            (main / f"wire{suffix}").write_text(
                "from demo.sender import Sender\n"
                "class Assembly(Sender): pass\n"
                "def wire(value: Sender) -> Sender: ...\n")
        folders = self.focused_diagnostics(self.root, "contract-folders")
        types = self.focused_diagnostics(self.root, "dependency-types")
        self.assertEqual([item for item in folders + types
                          if str(item["path"]).startswith("src/main/")], [])

    def test_root_main_counts_toward_single_directory_limit(self):
        _ = self.root_main_fixture()
        (self.root / "src/demo/main").mkdir()
        findings = self.focused_diagnostics(self.root, "contract-folders")
        self.assertEqual({item["path"] for item in findings
                          if item["rule"] == "MULTIPLE_MAIN_DIRECTORIES"},
                         {"src/main", "src/demo/main"})

    def test_single_root_main_is_allowed(self):
        _ = self.root_main_fixture()
        (self.root / "src/main.py").write_text("pass\n")
        findings = self.focused_diagnostics(self.root, "contract-folders")
        self.assertFalse(any(item["rule"] == "MULTIPLE_MAIN_DIRECTORIES"
                             for item in findings))

    def module(self, root: Path, name: str) -> dict[str, Any]:
        return next(item for item in generated_policy.render(root)[0]["modules"]
                    if item["path"] == name)

    def denied_dependency(self, root: Path, source: str, target: str) -> None:
        self.assertNotIn(target, self.module(root, source)["depends_on"])

    def test_generated_directory_groups_and_policy(self):
        with TemporaryDirectory(prefix="quard-multi-package-") as directory:
            root = Path(directory)
            utility = self.multi_package_fixture(root)
            self.assertEqual(self.focused_diagnostics(root, "contract-folders"), [])
            self.assertEqual(self.focused_diagnostics(root, "dependency-types"), [])
            self.assertEqual(self.focused_diagnostics(root, "outer-classes"), [])
            config = load_toml(root / "tach.toml")
            modules = {item["path"]: item for item in config["modules"]}
            self.assertEqual(modules["other.group.main"]["layer"], "Contract")
            self.assertEqual(modules["other.group.deep.operations_utils"]["layer"],
                             "Infrastructure")
            self.assertEqual(modules["other.group.deep.aliases"]["layer"], "Contract")
            self.assertEqual(modules["demo.arbitrary.sender"]["layer"], "Infrastructure")
            self.assertEqual(modules["other.group.deep.operations_utils"]["depends_on"],
                             ["other.group.deep.aliases"])
            self.assertEqual(modules["other.group.deep.aliases"]["depends_on"],
                             ["demo.arbitrary.service"])
            self.assertIn("placeholder", modules)
            self.assertEqual(regenerate(root), False)
            expected = (root / "tach.toml").read_bytes()
            (root / "tach.toml").write_text("exact = false\n")
            with patch.object(sys, "argv", ["apply.py", "--project", str(root), "--json"]):
                self.assertEqual(apply_utils.main(), 0)
            self.assertEqual((root / "tach.toml").read_bytes(), expected)
            (root / "tach.toml").unlink()
            self.assertTrue(regenerate(root))
            self.assertEqual((root / "tach.toml").read_bytes(), expected)
            execution = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); " +
                                 "from other.group.deep.operations_utils import inspect; " +
                                 "from demo.arbitrary.sender import Sender; " +
                                 "assert inspect(Sender()) == 'ready:ok'"], root, 10)
            self.assertEqual(execution["exit_code"], 0, execution)

            for suffix in (".py", ".pyi"):
                direct = root / f"src/root{suffix}"
                direct.write_text("from typing import Protocol\nclass Root(Protocol): ...\n")
                diagnostics = self.focused_diagnostics(root, "contract-folders")
                self.assertTrue(any(d["rule"] == "SOURCE_ROOT_LAYOUT" and
                                    d["path"] == f"src/root{suffix}" and
                                    d["line"] == d["column"] == 1
                                    for d in diagnostics), diagnostics)
                direct.unlink()

            (root / "src/root.txt").write_text("unrelated")
            self.assertEqual(self.focused_diagnostics(root, "contract-folders"), [])
            (root / "src/root.txt").unlink()
            utility.write_text(utility.read_text() +
                               "from demo.domain import api\n")
            forbidden = generated_policy.render(root)[0]
            self.assertTrue(forbidden["exact"])
            self.assertNotIn("demo.domain.api", next(item for item in
                             load_toml(root / "tach.toml")["modules"] if item["path"] ==
                             "other.group.deep.operations_utils")["depends_on"])

    def test_generated_package_facades_and_policy(self):
        with TemporaryDirectory(prefix="quard-facade-consumer-") as directory:
            root = Path(directory)
            self.multi_package_fixture(root)
            group = root / "src/demo/arbitrary/__init__.py"
            ancestor = root / "src/demo/__init__.py"
            stub = group.with_suffix(".pyi")
            group.write_text("from .service import Service as Port\n"
                             "__all__ = ['Port']\n")
            stub.write_text(group.read_text())
            ancestor.write_text("from .arbitrary import Port as PublicPort\n"
                                "__all__ = ['PublicPort']\n")
            consumer = root / "src/other/group/deep/operations_utils.py"
            original_consumer = consumer.read_text()
            consumer.write_text(original_consumer.replace(
                "from other.group.deep.aliases import PublicService",
                "from demo import PublicPort as PublicService"))
            consumer.with_suffix(".pyi").write_text(consumer.with_suffix(".pyi").read_text().replace(
                "from other.group.deep.aliases import PublicService",
                "from demo import PublicPort as PublicService"))
            assembly = root / "src/other/group/deep/main"
            assembly.mkdir()
            (assembly / "__init__.py").write_text("")
            wire = assembly / "wire.py"
            wire.write_text("from demo.arbitrary.sender import Sender\n"
                            "result = Sender().first('ok')\n")

            def check(expected: int, rule: str | None = None, path: str | None = None):
                diagnostics = self.focused_diagnostics(root, "contract-folders")
                self.assertEqual(int(bool(diagnostics)), expected, diagnostics)
                if rule is not None:
                    self.assertTrue(any(item["rule"] == rule and item["path"] == path and
                                         item["line"] > 0 and item["column"] > 0 for item in
                                         diagnostics), diagnostics)

            check(0)
            config = generated_policy.render(root)[0]
            self.assertEqual(next(item for item in config["interfaces"] if item["from"] ==
                                  [r"^demo\.arbitrary$"])["expose"], [r"^Port$"])
            execution = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                                 "from other.group.deep.main.wire import result; "
                                 "assert result == 'ready:ok'"], root, 10)
            self.assertEqual(execution["exit_code"], 0, execution)

            original_ancestor = ancestor.read_text()
            ancestor.write_text("from .arbitrary import Port\n"
                                "PublicPort = Port\n__all__ = ['PublicPort']\n")
            check(0)
            ancestor.write_text(original_ancestor)

            original_stub = stub.read_text()
            stub.write_text(original_stub.replace("['Port']", "[]"))
            check(1, "CONFLICTING_FACADE_EXPORT", str(stub.relative_to(root)))
            stub.write_text(original_stub)

            for file, contents, rule in (
                (group, "from .sender import Sender\n", "CONCRETE_FACADE_EXPORT"),
                (group, "from .sender import Sender as PublicSender\n",
                 "CONCRETE_FACADE_EXPORT"),
                (group, "from .sender import Sender as PublicSender\n"
                        "__all__ = ['PublicSender']\n", "CONCRETE_FACADE_EXPORT"),
                (group, "from .service import Missing\n", "UNRESOLVED_FACADE_EXPORT"),
                (group, "__all__ = ['Unknown']\n", "UNRESOLVED_FACADE_EXPORT"),
                (group, "__all__ = names\n", "UNRESOLVED_FACADE_EXPORT"),
                (group, "if True:\n    from .sender import Sender as Conditional\n",
                 "UNRESOLVED_FACADE_EXPORT"),
                (group, "from .service import Service as Port\n", "CONFLICTING_FACADE_EXPORT"),
                (stub, "from .sender import Sender as Port\n", "CONFLICTING_FACADE_EXPORT"),
                (ancestor, "from .arbitrary.sender import Sender as PublicSender\n",
                 "CONCRETE_FACADE_EXPORT"),
                (group, "from .sender import Sender as PrivateSender\n"
                        "PrivateSender = Port\n", "CONFLICTING_FACADE_EXPORT"),
            ):
                with self.subTest(file=file, rule=rule):
                    before = file.read_text()
                    file.write_text(before + contents)
                    try:
                        check(1, rule, str(file.relative_to(root)))
                        if file == group:
                            exposed = next(item["expose"] for item in
                                           generated_policy.render(root)[0]["interfaces"] if
                                           item["from"] == [r"^demo\.arbitrary$"])
                            self.assertEqual(exposed, [])
                    finally:
                        file.write_text(before)

            value = root / "src/demo/arbitrary/value.py"
            value.write_text("from typing import NamedTuple\n"
                             "Value = NamedTuple('Value', [('code', str)])\n")
            group.write_text(group.read_text() + "from .value import Value as PublicValue\n")
            check(1, "CONCRETE_FACADE_EXPORT", str(group.relative_to(root)))
            group.write_text(original_stub)
            value.unlink()

            # An indirect export is forbidden even when no consumer imports it.
            group.write_text(group.read_text() + "from .sender import Sender as Hidden\n")
            stub.write_text(group.read_text())
            ancestor.write_text(ancestor.read_text() + "from .arbitrary import Hidden\n")
            check(1, "CONCRETE_FACADE_EXPORT", str(ancestor.relative_to(root)))
            group.write_text(original_stub)
            stub.write_text(original_stub)
            ancestor.write_text(original_ancestor)
            wire.write_text("from demo.arbitrary import Sender\nresult = Sender().first('ok')\n")
            self.denied_dependency(root, "other.group.deep.main.wire", "demo.arbitrary.sender")
            self.assertEqual(self.focused_diagnostics(root, "contract-folders"), [])

    def test_generated_multiple_contracts_combined_groups_and_mixed_roles(self):
        with TemporaryDirectory(prefix="quard-group-roles-") as directory:
            root = Path(directory)
            self.multi_package_fixture(root)
            group = root / "src/demo/arbitrary"
            extra = group / "extra.py"
            extra.write_text("from typing import Protocol\n"
                             "class Extra(Protocol):\n    def third(self) -> str: ...\n")
            extra.with_suffix(".pyi").write_text(extra.read_text())
            sender = group / "sender.py"
            sender_stub = sender.with_suffix(".pyi")
            for file in (sender, sender_stub):
                text = file.read_text().replace("class Sender(Service):", "class Sender(Service, Extra):")
                text = text.replace("from typing import override\n", "from typing import override\n"
                                    "from demo.arbitrary.extra import Extra\n")
                text += "\n    @override\n    def third(self) -> str:\n"
                text += "        return self.first('ok')\n" if file == sender else "        ...\n"
                file.write_text(text)
            remote = root / "src/other/group/deep"
            combined = remote / "combined.py"
            combined.write_text("from typing import Protocol\n"
                                "from demo.arbitrary.service import Service\n"
                                "class Combined(Service, Protocol):\n    pass\n")
            combined.with_suffix(".pyi").write_text(combined.read_text())
            bridge = remote / "bridge.py"
            bridge.write_text("from typing import override\n"
                              "from other.group.deep.combined import Combined\n"
                              "class Bridge(Combined):\n"
                              "    @override\n    def first(self, value: str) -> str:\n"
                              "        return self.second(value).strip()\n"
                              "    @override\n    def second(self, value: str) -> str:\n"
                              "        return value\n")
            bridge.with_suffix(".pyi").write_text(
                "from typing import override\n"
                "from other.group.deep.combined import Combined\n"
                "class Bridge(Combined):\n"
                "    @override\n    def first(self, value: str) -> str: ...\n"
                "    @override\n    def second(self, value: str) -> str: ...\n")

            self.assertEqual(self.focused_diagnostics(root, "contract-folders"), [])
            self.assertEqual(next(item for item in generated_policy.render(root)[0]["modules"]
                                  if item["path"] == "other.group.deep.combined")["layer"],
                             "Contract")
            self.assertEqual(next(item for item in generated_policy.render(root)[0]["modules"]
                                  if item["path"] == "demo.arbitrary.extra")["layer"],
                             "Contract")
            execution = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                                 + "from other.group.deep.bridge import Bridge; "
                                 + "from demo.arbitrary.sender import Sender; "
                                 + "assert Bridge().first(' hi ') == 'hi'; "
                                 + "assert Sender().third() == 'ready:ok'"], root, 10)
            self.assertEqual(execution["exit_code"], 0, execution)

            # A foreign contract alone cannot select the implementation's group.
            before = bridge.read_text()
            bridge.write_text(before.replace("class Bridge(Combined):", "class Bridge(Service):")
                              .replace("from other.group.deep.combined import Combined",
                                       "from demo.arbitrary.service import Service"))
            failed = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] == "CROSS_GROUP_CONTRACT" and
                                item["path"] == "src/other/group/deep/bridge.py" and
                                item["line"] == 3 and item["column"] == 1
                                for item in failed), failed)
            bridge.write_text(before)

            # Source and stubs are checked separately, even when the source passes.
            stub = bridge.with_suffix(".pyi")
            before_stub = stub.read_text()
            stub.write_text(before_stub.replace("class Bridge(Combined):", "class Bridge(Service):")
                            .replace("from other.group.deep.combined import Combined",
                                     "from demo.arbitrary.service import Service"))
            failed = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] == "CROSS_GROUP_CONTRACT" and
                                item["path"] == "src/other/group/deep/bridge.pyi"
                                for item in failed), failed)
            stub.write_text(before_stub)

            # A concrete class in the same module must not acquire a Contract role.
            extra.write_text(extra.read_text() + "\nclass Accidental(Extra):\n"
                             "    def third(self) -> str: return 'ok'\n")
            failed = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] == "MIXED_MODULE_ROLES" and
                                item["path"] == "src/demo/arbitrary/extra.py" and
                                item["line"] > 0 and item["column"] > 0
                                for item in failed), failed)
            self.assertEqual(next(item for item in generated_policy.render(root)[0]["modules"]
                                  if item["path"] == "demo.arbitrary.extra")["layer"],
                             "Infrastructure")
            extra.write_text(extra.read_text().split("\nclass Accidental")[0] + "\n")

            # A companion stub cannot redefine the source contract as concrete.
            extra_stub = extra.with_suffix(".pyi")
            original_stub = extra_stub.read_text()
            extra_stub.write_text("class Extra(object):\n    def third(self) -> str: ...\n")
            failed = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] == "CONFLICTING_MODULE_ROLE" and
                                item["path"] == "src/demo/arbitrary/extra.pyi" and
                                item["line"] == item["column"] == 1
                                for item in failed), failed)
            extra_stub.write_text(original_stub)

            # An unresolved customer base must not select a group by guesswork.
            bridge.write_text(before.replace("class Bridge(Combined):", "class Bridge(Combined, Ghost):")
                              .replace("from other.group.deep.combined import Combined",
                                       "from other.group.deep.combined import Combined\n"
                                       "from demo.absent import Ghost"))
            failed = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] == "UNRESOLVED_GROUP_OWNER" and
                                item["path"] == "src/other/group/deep/bridge.py"
                                for item in failed), failed)

    def test_generated_exact_external_inherited_contracts(self):
        with TemporaryDirectory(prefix="quard-external-contracts-") as directory:
            root = Path(directory)
            self.multi_package_fixture(root)
            group = root / "src/demo/arbitrary"
            implementation = group / "external_items.py"
            implementation.write_text(
                "from collections import UserList\n\n"
                "class ExternalItems(UserList[str]):\n"
                "    def first(self) -> str:\n        return self.last()\n\n"
                "    def last(self) -> str:\n        return self[0]\n")
            implementation.with_suffix(".pyi").write_text(
                "from collections import UserList\n\n"
                "class ExternalItems(UserList[str]):\n"
                "    def first(self) -> str: ...\n"
                "    def last(self) -> str: ...\n")
            errors = group / "errors.py"
            errors.write_text("class LocalError(Exception):\n"
                              "    def detail(self) -> str:\n"
                              "        prefix = 'error'\n"
                              "        return prefix + self.summary()\n\n"
                              "    def summary(self) -> str:\n"
                              "        return ': ' + str(self)\n")
            signature = group / "external_port.py"
            signature.write_text(
                "from collections import UserDict\nfrom typing import Protocol\n\n"
                "class ExternalPort(Protocol):\n"
                "    def adapt(self, value: UserDict[str, str]) -> str: ...\n")
            facade = root / "src/other/group/deep/external_api.py"
            facade.write_text("from collections import UserList\n"
                              "from builtins import Exception as PublicError\n"
                              "PublicItems = UserList[str]\n"
                              "__all__ = ['PublicItems', 'PublicError']\n")
            use = root / "src/other/group/deep/external_utils.py"
            use.write_text("from other.group.deep.external_api import PublicItems\n\n"
                            "def read(items: PublicItems) -> str:\n"
                           "    return items[0]\n")

            self.assertEqual(self.focused_diagnostics(root, "contract-folders"), [])
            modules = {item["path"]: item for item in generated_policy.render(root)[0]["modules"]}
            self.assertEqual(modules["other.group.deep.external_api"]["layer"], "Contract")
            self.assertEqual(modules["demo.arbitrary.external_items"]["layer"], "Infrastructure")
            self.assertEqual(modules["demo.arbitrary.errors"]["layer"], "Infrastructure")
            self.assertEqual(modules["other.group.deep.external_utils"]["depends_on"],
                             ["other.group.deep.external_api"])
            execution = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                                 + "from demo.arbitrary.external_items import ExternalItems; "
                                 + "from demo.arbitrary.errors import LocalError; "
                                 + "from other.group.deep.external_utils import read; "
                                 + "assert read(ExternalItems(['ok'])) == 'ok'; "
                                 + "assert issubclass(LocalError, Exception)"], root, 10)
            self.assertEqual(execution["exit_code"], 0, execution)

            original = facade.read_text()
            original_use = use.read_text()
            for export in ("from collections import UserDict as Unrelated\n",
                           "from demo.arbitrary.external_items import ExternalItems as Unrelated\n"):
                facade.write_text(original + export + "__all__ = ['Unrelated']\n")
                use.write_text("from other.group.deep.external_api import Unrelated\n"
                               + original_use)
                interfaces = [item for item in generated_policy.render(root)[0]["interfaces"]
                              if item["from"] == [r"^other\.group\.deep\.external_api$"]]
                self.assertFalse(any(r"^Unrelated$" in item["expose"] for item in interfaces))
            facade.write_text(original)
            use.write_text(original_use)

            original_stub = implementation.with_suffix(".pyi").read_text()
            implementation.with_suffix(".pyi").write_text(original_stub.replace(
                "UserList[str]", "UserDict[str, str]").replace(
                    "from collections import UserList", "from collections import UserDict"))
            diagnostics = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] == "UNRESOLVED_EXTERNAL_CONTRACT" and
                                item["path"] == "src/demo/arbitrary/external_items.pyi" and
                                item["line"] > 0 and item["column"] > 0
                                for item in diagnostics), diagnostics)
            implementation.with_suffix(".pyi").write_text(original_stub)

            implementation.with_suffix(".pyi").write_text(original_stub.replace(
                "UserList[str]", "UserList[int]"))
            diagnostics = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] == "UNRESOLVED_EXTERNAL_CONTRACT" and
                                item["path"] == "src/demo/arbitrary/external_items.pyi"
                                for item in diagnostics), diagnostics)
            implementation.with_suffix(".pyi").write_text(original_stub)

            implementation.write_text(implementation.read_text().replace(
                "class ExternalItems", "UserList = object\n\nclass ExternalItems"))
            diagnostics = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] in {"UNRESOLVED_GROUP_OWNER",
                                                 "UNRESOLVED_EXTERNAL_CONTRACT"} and
                                item["path"] == "src/demo/arbitrary/external_items.py"
                                for item in diagnostics), diagnostics)
            implementation.write_text(implementation.read_text().replace("UserList = object\n\n", ""))

            original_source = implementation.read_text()
            for replacement in ("from collections import UserDict as UserList\n",
                                "UserList = object\n"):
                implementation.write_text(original_source.replace(
                    "class ExternalItems", replacement + "\nclass ExternalItems"))
                diagnostics = self.focused_diagnostics(root, "contract-folders")
                self.assertTrue(any(item["rule"] == "UNRESOLVED_EXTERNAL_CONTRACT" and
                                    item["path"] == "src/demo/arbitrary/external_items.py" and
                                    item["line"] > 0 and item["column"] > 0
                                    for item in diagnostics), diagnostics)
            implementation.write_text(original_source)

            unproved = group / "unproved.py"
            unproved.write_text("class Unproved(UnknownBase):\n    pass\n")
            diagnostics = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] == "UNRESOLVED_GROUP_OWNER" and
                                item["path"] == "src/demo/arbitrary/unproved.py" and
                                item["line"] == item["column"] == 1 for item in
                                diagnostics), diagnostics)
            unproved.unlink()

            use.write_text(use.read_text() + "import tomlkit\n")
            self.assertIn("import tomlkit", use.read_text())
            self.assertNotIn("tomlkit", load_toml(root / "pyproject.toml")[
                "project"]["dependencies"])

    def test_reviewed_folder_rules_survive_generated_role_changes(self):
        with TemporaryDirectory(prefix="quard-reviewed-roles-") as directory:
            root = Path(directory)
            create_repo(root)
            config = (root / "tach.toml").read_bytes()
            source = root / "src/demo/domain/api.py"
            source.write_text("from typing import Protocol\n"
                              "class AnyName(Protocol):\n    def read(self) -> str: ...\n")
            (root / "src/demo/client.py").write_text("from demo.domain.api import AnyName\n")
            configure(root, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
            diagnostics = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] == "CONTRACT_FOLDER" and
                                item["path"] == "src/demo/domain/api.py"
                                for item in diagnostics), diagnostics)
            self.assertFalse(any(item["rule"] == "MIXED_MODULE_ROLES"
                                 for item in diagnostics))
            self.assertEqual((root / "tach.toml").read_bytes(), config)

    def test_generated_forbidden_imports_are_not_approved(self):
        root = self.generated_fixture()
        self.install_generated_fixture(root)
        path = root / "tach.toml"
        client = root / "src/demo/client.py"
        client.write_text(client.read_text() + "from demo.port.impl import Impl\n",
                          encoding="utf-8")
        path.write_text(path.read_text() + "\n# tampered\n")
        self.assertTrue(regenerate(root))
        module = next(item for item in load_toml(path)["modules"]
                      if item["path"] == "demo.client")
        self.assertEqual(module["depends_on"], ["demo.port.port"])
        self.assertNotIn("tampered", path.read_text())
        self.assertNotIn("demo.port.impl", module["depends_on"])

    def test_generated_exact_role_matrix(self):
        with TemporaryDirectory(prefix="quard-native-roles-") as directory:
            root = Path(directory)
            self.multi_package_fixture(root)
            contract = root / "src/demo/arbitrary/service.py"
            implementation = root / "src/demo/arbitrary/sender.py"
            utility = root / "src/other/group/deep/operations_utils.py"
            bootstrap = root / "src/other/group/deep/main/wire.py"
            bootstrap.parent.mkdir()
            bootstrap.write_text("from demo.arbitrary.sender import Sender\n"
                                 "result = Sender().first('ok')\n")

            self.assertEqual(self.focused_diagnostics(root, "contract-folders"), [])
            execution = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                                 + "from other.group.deep.main.wire import result; "
                                 + "assert result == 'ready:ok'"], root, 10)
            self.assertEqual(execution["exit_code"], 0, execution)
            config = generated_policy.render(root)[0]
            modules = {item["path"]: item for item in config["modules"]}
            self.assertTrue(config["exact"])
            self.assertFalse(config["ignore_type_checking_imports"])
            self.assertEqual(config["root_module"], "forbid")
            self.assertTrue(all("depends_on" in module for module in modules.values()))
            self.assertEqual(modules["demo.arbitrary.service"]["depends_on"], [])
            self.assertEqual(modules["other.group.deep.operations_utils"]["depends_on"],
                             ["other.group.deep.aliases"])
            self.assertEqual(modules["other.group.deep.main.wire"]["depends_on"],
                             ["demo.arbitrary.sender"])
            self.assertIn("demo.arbitrary.sender",
                          modules["other.group.deep.operations_utils"]["cannot_depend_on"])
            self.assertNotIn("demo.arbitrary.sender", modules[
                "other.group.deep.operations_utils"]["depends_on"])
            self.assertFalse(any(item.get("utility") or item.get("unchecked") for item in
                                 modules.values()))
            self.assertEqual(next(item["expose"] for item in config["interfaces"] if
                                  item["from"] == [r"^demo\.arbitrary\.sender$"]), [])
            self.assertEqual(next(item["expose"] for item in config["interfaces"] if
                                  item["from"] == [r"^demo\.arbitrary\.service$"]),
                             ["^Service$"])
            self.assertTrue(regenerate(root))
            expected = (root / "tach.toml").read_bytes()
            self.assertFalse(regenerate(root))
            self.assertEqual((root / "tach.toml").read_bytes(), expected)

            utility_stub = utility.with_suffix(".pyi")
            original_utility = utility.read_text()
            original_utility_stub = utility_stub.read_text()
            for file in (utility, utility_stub):
                file.write_text(file.read_text().replace(
                    "from other.group.deep.aliases import PublicService",
                    "from demo.arbitrary import service as service_module").replace(
                        "PublicService", "service_module.Service"))
            self.assertEqual(next(item["depends_on"] for item in generated_policy.render(
                root)[0]["modules"] if item["path"] ==
                "other.group.deep.operations_utils"), ["demo.arbitrary.service"])
            utility.write_text(original_utility)
            utility_stub.write_text(original_utility_stub)
            self.assertEqual(self.module(root, "other.group.deep.operations_utils")[
                "depends_on"], modules["other.group.deep.operations_utils"]["depends_on"])

            forbidden_imports = (
                (utility, "\nfrom demo.arbitrary.sender import Sender\n"),
                (utility, "\nimport demo.arbitrary.sender\n"),
                (implementation, "\nfrom demo.arbitrary.sender_peer import Peer\n"),
                (contract, "\nfrom demo.arbitrary.sender import Sender\n"),
                (utility, "\nfrom typing import TYPE_CHECKING\n"
                 "if TYPE_CHECKING:\n    from demo.arbitrary.sender import Sender\n"),
                (bootstrap, "\nfrom other.group.deep.main.other import Other\n"),
            )
            batches: list[list[tuple[Path, str]]] = []
            for case in forbidden_imports:
                for batch in batches:
                    # Importing Sender from its contract changes Sender's generated
                    # dependencies; keep that case separate from Sender's own import.
                    if (all(other[0] != case[0] for other in batch) and
                            not (case[0] == contract and any(other[0] == implementation
                                                             for other in batch)) and
                            not (case[0] == implementation and any(other[0] == contract
                                                                   for other in batch))):
                        batch.append(case)
                        break
                else:
                    batches.append([case])
            for batch in batches:
                changes = {file: file.read_text() + addition for file, addition in batch}
                if implementation in changes:
                    changes[implementation.with_name("sender_peer.py")] = (
                        "class Peer(object):\n    pass\n")
                if bootstrap in changes:
                    changes[bootstrap.with_name("other.py")] = "class Other(object):\n    pass\n"
                with temporary_edits(changes):
                    for file, addition in batch:
                        with self.subTest(file=file, addition=addition):
                            changed = {item["path"]: item for item in
                                        generated_policy.render(root)[0]["modules"]}
                            self.assertEqual(changed[file.relative_to(root / "src").with_suffix(
                                "").as_posix().replace("/", ".")]["depends_on"],
                                             modules[file.relative_to(root / "src").with_suffix(
                                                 "").as_posix().replace("/", ".")]["depends_on"])

            original_contract = contract.read_text()
            contract.write_text(original_contract +
                                "\nfrom other.group.deep.aliases import PublicService\n")
            cyclic = generated_policy.render(root)[0]
            self.assertTrue(cyclic["forbid_circular_dependencies"])
            self.assertIn("demo.arbitrary.service", self.module(root, "other.group.deep.aliases")[
                "depends_on"])
            self.assertIn("other.group.deep.aliases", self.module(root,
                          "demo.arbitrary.service")["depends_on"])
            contract.write_text(original_contract)
            root_file = root / "src/direct.py"
            root_file.write_text("from demo.arbitrary.service import Service\n")
            root_failure = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] == "SOURCE_ROOT_LAYOUT" and item["path"] ==
                                "src/direct.py" and item["line"] == item["column"] == 1
                                for item in root_failure), root_failure)
            root_file.unlink()
            original_utility = utility.read_text()
            utility.write_text(original_utility + "\nimport tomlkit\n")
            self.assertNotIn("tomlkit", load_toml(root / "pyproject.toml")[
                "project"]["dependencies"])
            utility.write_text(original_utility)
            # Regeneration must remove manually inserted dependency declarations.
            config = load_toml(root / "tach.toml")
            next(item for item in config["modules"] if item["path"] ==
                 "other.group.deep.operations_utils")["depends_on"].append(
                     "demo.arbitrary.service")
            (root / "tach.toml").write_text(tomlkit.dumps(config))
            self.assertTrue(regenerate(root))
            self.assertEqual((root / "tach.toml").read_bytes(), expected)

    def test_generated_dependency_types_and_runtime(self):
        root = self.generated_fixture()
        setting = root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "dependency_types = false", "dependency_types = true"), encoding="utf-8")
        self.install_generated_fixture(root)
        client = root / "src/demo/client.py"
        client.write_text("from demo.port.port import Port\n"
                          "from demo.port.impl import Impl\n"
                          "class Client(Port):\n"
                          "    def send(self) -> None: pass\n"
                          "    def build(self, value: Impl) -> Port: return value\n",
                          encoding="utf-8")
        diagnostics = self.focused_diagnostics(root, "dependency-types")
        self.assertTrue(any(d["rule"] == "CONCRETE_DEPENDENCY_TYPE" and
                            d["path"] == "src/demo/client.py" and d["line"] > 0
                            for d in diagnostics), diagnostics)
        self.denied_dependency(root, "demo.client", "demo.port.impl")
        executed = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                            "from demo.client import Client; from demo.port.impl import Impl; "
                            "assert isinstance(Client().build(Impl()), Impl)"], root, 10)
        self.assertEqual(executed["exit_code"], 0, executed)

    def test_generated_semantic_boundaries(self):
        with TemporaryDirectory(prefix="quard-semantic-boundaries-") as directory:
            root = Path(directory)
            self.multi_package_fixture(root)
            setting = root / ".quard/config.toml"
            setting.write_text(setting.read_text().replace(
                "dependency_types = false", "dependency_types = true"))
            sender = root / "src/demo/arbitrary/sender.py"
            stub = sender.with_suffix(".pyi")
            utility = root / "src/other/group/deep/operations_utils.py"
            utility_stub = utility.with_suffix(".pyi")
            peer = root / "src/demo/arbitrary/peer_utils.py"
            peer.write_text("def label() -> str:\n    return 'ready'\n")
            assembly = root / "src/other/group/deep/main/wire.py"
            assembly.parent.mkdir()
            assembly.write_text("from demo.arbitrary.sender import Sender\n"
                                "result = Sender().first('ok')\n")
            tests = root / "tests/demo/arbitrary/test_sender.py"
            tests.parent.mkdir(parents=True)
            tests.write_text("import unittest\nfrom demo.arbitrary.sender import Sender\n\n"
                             "class TestSender(unittest.TestCase):\n"
                             "    def test_first(self) -> None:\n"
                             "        self.assertEqual(Sender().first('ok'), 'ready:ok')\n")

            for job in ("contract-folders", "dependency-types", "test-placement",
                        "outer-classes"):
                self.assertEqual(self.focused_diagnostics(root, job), [])
            execution = capture([sys.executable, "-m", "unittest", "discover", "-s",
                                 "tests/demo/arbitrary", "-p", "test_sender.py"], root, 10)
            self.assertEqual(execution["exit_code"], 0, execution)

            cases = (
                (sender, "\nclass Child(Sender):\n    pass\n", "contract-folders",
                 "CONCRETE_IMPLEMENTATION_BASE", False),
                (stub, "\nclass Child(Sender): ...\n", "contract-folders",
                 "CONCRETE_IMPLEMENTATION_BASE", False),
                (sender, "\ninstance = Sender()\n", "contract-folders",
                 "CONCRETE_IMPLEMENTATION_REFERENCE", False),
                (sender, "\nAlias = Sender\n", "contract-folders",
                 "CONCRETE_IMPLEMENTATION_REFERENCE", False),
                (utility, "\nfrom demo.arbitrary.sender import Sender\n", "contract-folders",
                 "CONCRETE_IMPLEMENTATION_IMPORT", True),
                (utility_stub, "\nfrom demo.arbitrary.sender import Sender\n", "contract-folders",
                 "CONCRETE_IMPLEMENTATION_IMPORT", False),
                (utility_stub, "\nfrom demo.arbitrary.peer_utils import label\n",
                 "contract-folders", "CONCRETE_IMPLEMENTATION_IMPORT", False),
                (utility_stub, "\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
                 "    from demo.arbitrary.sender import Sender\n", "contract-folders",
                 "CONCRETE_IMPLEMENTATION_IMPORT", False),
                (peer, "from .sender import Sender as Peer\n", "contract-folders",
                 "CONCRETE_IMPLEMENTATION_IMPORT", True),
                (peer, "import demo.arbitrary.sender\n", "contract-folders",
                 "CONCRETE_IMPLEMENTATION_IMPORT", True),
            )
            batches: list[list[tuple[Path, str, str, str, bool]]] = []
            for case in cases:
                for batch in batches:
                    if all(other[0] != case[0] for other in batch):
                        batch.append(case)
                        break
                else:
                    batches.append([case])
            for batch in batches:
                with temporary_edits({file: file.read_text() + addition
                                       for file, addition, *_ in batch}):
                    diagnostics = self.focused_diagnostics(root, "contract-folders")
                    for file, _, job, rule, native in batch:
                        with self.subTest(file=file, rule=rule):
                            self.assertEqual(job, "contract-folders")
                            self.assertTrue(any(item["rule"] == rule and item["path"] ==
                                                str(file.relative_to(root)) and item["line"] > 0 and
                                                item["column"] > 0 and
                                                not item.get("is_infrastructure_error")
                                                for item in diagnostics), diagnostics)
                            if native:
                                source = file.relative_to(root / "src").with_suffix(
                                    "").as_posix().replace("/", ".")
                                self.denied_dependency(root, source, "demo.arbitrary.sender")

            for file, addition in (
                (sender, "\nclass Peer(Service):\n    pass\n"),
                (stub, "\nclass Peer(Service): ...\n"),
            ):
                original = file.read_text()
                file.write_text(original + addition)
                annotated = file.read_text().replace("class Sender(Service):\n",
                                                     "class Sender(Service):\n    peer: Peer\n")
                file.write_text(annotated)
                try:
                    diagnostics = self.focused_diagnostics(root, "dependency-types")
                    self.assertTrue(any(item["rule"] == "CONCRETE_DEPENDENCY_TYPE" and
                                        item["path"] == str(file.relative_to(root)) and
                                        item["line"] > 0 for item in diagnostics), diagnostics)
                finally:
                    file.write_text(original)

            for file in (utility, utility_stub):
                original = file.read_text()
                file.write_text(original + "\ndef inspect_concrete("
                                "value: 'demo.arbitrary.sender.Sender') -> "
                                "'demo.arbitrary.sender.Sender': ...\n")
                try:
                    diagnostics = self.focused_diagnostics(root, "dependency-types")
                    matches = [item for item in diagnostics if item["path"] ==
                               str(file.relative_to(root))]
                    self.assertEqual({item["symbol"] for item in matches},
                                     {"inspect_concrete.inspect_concrete.value",
                                      "inspect_concrete.inspect_concrete"})
                    self.assertTrue(all(item["line"] > 0 and item["column"] > 0 and
                                         not item.get("is_infrastructure_error") for item in matches))
                finally:
                    file.write_text(original)

            misplaced = tests.with_name("test_wrong.py")
            misplaced.write_text(tests.read_text().replace("TestSender", "TestWrong"))
            diagnostics = self.focused_diagnostics(root, "test-placement")
            self.assertTrue(any(item["path"] == str(misplaced.relative_to(root)) and
                                item["line"] > 0 and item["column"] > 0 for item in diagnostics),
                            diagnostics)
            misplaced.unlink()
            original_tests = tests.read_text()
            tests.write_text(original_tests + "\nclass TestExtra(unittest.TestCase):\n"
                             "    def test_first(self) -> None: pass\n")
            diagnostics = self.focused_diagnostics(root, "outer-classes")
            self.assertTrue(any(item["rule"] == "MULTIPLE_OUTER_CLASSES" and
                                item["path"] == str(tests.relative_to(root)) and item["line"] > 0
                                for item in diagnostics), diagnostics)
            tests.write_text(original_tests)
            self.assertEqual(self.focused_diagnostics(root, "contract-folders"), [])
            self.assertEqual(self.focused_diagnostics(root, "dependency-types"), [])
            self.assertEqual(self.focused_diagnostics(root, "test-placement"), [])
            self.assertEqual(self.focused_diagnostics(root, "outer-classes"), [])

    def test_generated_envelopes_and_unique_main(self):
        with TemporaryDirectory(prefix="quard-assembly-areas-") as directory:
            root = Path(directory)
            self.multi_package_fixture(root)
            first = root / "src/demo/arbitrary/envelopes"
            first.mkdir()
            adapter = first / "adapter.py"
            adapter.write_text("from typing import override\n"
                               "from demo.arbitrary.service import Service\n"
                               "from demo.arbitrary.sender import Sender\n"
                               "class Adapter(Service):\n"
                               "    @override\n"
                               "    def first(self, value: str) -> str:\n"
                               "        return Sender().first(value)\n"
                               "    @override\n"
                               "    def second(self, value: str) -> str:\n"
                               "        return self.first(value + '!')\n")
            second = root / "src/other/group/deep/envelopes"
            second.mkdir()
            relay = second / "relay.py"
            relay.write_text("from typing import override\n"
                             "from demo.arbitrary.service import Service\n"
                             "from demo.arbitrary.envelopes.adapter import Adapter\n"
                             "from demo.arbitrary.sender import Sender\n"
                             "class Relay(Service):\n"
                             "    \"\"\"Delegate to either adapter or sender.\"\"\"\n"
                             "    @override\n"
                             "    def first(self, value: str) -> str:\n"
                             "        if value:\n"
                             "            return Adapter().first(value)\n"
                             "        return Sender().first('empty')\n"
                             "    @override\n"
                             "    def second(self, value: str) -> str:\n"
                             "        return self.first(Sender().second(value))\n")
            relay.with_suffix(".pyi").write_text(
                "from typing import override\n"
                "from demo.arbitrary.service import Service\n"
                "from demo.arbitrary.envelopes.adapter import Adapter\n"
                "from demo.arbitrary.sender import Sender\n"
                "class Relay(Service):\n"
                "    reference: Adapter | Sender\n"
                "    @override\n"
                "    def first(self, value: str) -> str: ...\n"
                "    @override\n"
                "    def second(self, value: str) -> str: ...\n")
            main = root / "src/other/group/deep/main"
            main.mkdir()
            wire = main / "wire.py"
            wire.write_text("from demo.arbitrary.service import Service\n"
                            "from demo.arbitrary.sender import Sender\n"
                            "from other.group.deep.envelopes.relay import Relay\n"
                            "def connect(value: Sender | Relay | None = None) -> Service:\n"
                            "    return Relay() if (value or Sender()).first('ok') else Sender()\n")
            wire.with_suffix(".pyi").write_text(
                "from demo.arbitrary.service import Service\n"
                "from demo.arbitrary.sender import Sender\n"
                "from other.group.deep.envelopes.relay import Relay\n"
                "def connect(value: Sender | Relay | None = None) -> Service: ...\n")
            nested_main = main / "nested"
            nested_main.mkdir()
            (nested_main / "entry.py").write_text(
                "from demo.arbitrary.sender import Sender\n"
                "result = Sender().first('nested')\n")

            self.assertEqual({item["path"] for item in self.focused_diagnostics(root, "contract-folders")
                              if item["rule"] == "ENVELOPE_DELEGATION"},
                             {"src/demo/arbitrary/envelopes/adapter.py",
                              "src/other/group/deep/envelopes/relay.py"})
            modules = {item["path"]: item for item in generated_policy.render(root)[0]["modules"]}
            self.assertEqual(modules["demo.arbitrary.envelopes.adapter"]["layer"], "Envelopes")
            self.assertEqual(modules["other.group.deep.envelopes.relay"]["layer"], "Envelopes")
            self.assertEqual(modules["other.group.deep.main.wire"]["layer"], "Bootstrap")
            self.assertEqual(modules["other.group.deep.main.nested.entry"]["layer"], "Bootstrap")
            self.assertEqual(modules["other.group.main"]["layer"], "Contract")
            self.assertEqual(modules["other.group.deep.envelopes.relay"]["depends_on"],
                             ["demo.arbitrary.envelopes.adapter", "demo.arbitrary.sender",
                              "demo.arbitrary.service"])
            self.assertEqual(modules["other.group.deep.main.wire"]["depends_on"],
                             ["demo.arbitrary.sender", "demo.arbitrary.service",
                              "other.group.deep.envelopes.relay"])
            self.assertEqual(modules["other.group.deep.main.nested.entry"]["depends_on"],
                             ["demo.arbitrary.sender"])
            executed = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                                "from other.group.deep.main.wire import connect; "
                                "from other.group.deep.main.nested.entry import result; "
                                "assert connect().first('ok') == 'ready:ok'; "
                                "assert result == 'ready:nested'"], root, 10)
            self.assertEqual(executed["exit_code"], 0, executed)

            nested = second / "nested"
            nested.mkdir()
            (nested / "__init__.py").write_text("")
            violations = (
                (nested / "wire.py", "from other.group.deep.main.extra import result\n",
                 "BOOTSTRAP_DEPENDENCY", True),
                (nested / "wire.pyi", "from other.group.deep.main.extra import result\n",
                 "BOOTSTRAP_DEPENDENCY", False),
                (root / "src/other/group/main.py",
                 "from demo.arbitrary.sender import Sender\n", "CONCRETE_IMPLEMENTATION_IMPORT", True),
                (wire, "import other.group.deep.main.extra\n", "BOOTSTRAP_DEPENDENCY", True),
                (wire.with_suffix(".pyi"), "import other.group.deep.main.extra\n",
                 "BOOTSTRAP_DEPENDENCY", False),
                (relay, "import other.group.deep.main.extra\n",
                 "BOOTSTRAP_DEPENDENCY", True),
                (relay.with_suffix(".pyi"), "import other.group.deep.main.extra\n",
                 "BOOTSTRAP_DEPENDENCY", False),
                (wire, "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
                 "    import other.group.deep.main.extra\n", "BOOTSTRAP_DEPENDENCY", True),
            )
            (main / "extra.py").write_text("")
            for file, addition, rule, native in violations:
                with self.subTest(file=file, rule=rule):
                    with temporary_edits({file: (file.read_text() if file.exists() else "") +
                                          addition}):
                        diagnostics = self.focused_diagnostics(root, "contract-folders")
                        self.assertTrue(any(item["rule"] == rule and item["path"] ==
                                            str(file.relative_to(root)) and item["line"] > 0 and
                                            item["column"] > 0 and
                                            not item.get("is_infrastructure_error")
                                            for item in diagnostics), diagnostics)
                        if native:
                            source = file.relative_to(root / "src").with_suffix(
                                "").as_posix().replace("/", ".")
                            target = ("demo.arbitrary.sender" if rule ==
                                      "CONCRETE_IMPLEMENTATION_IMPORT" else
                                      "other.group.deep.main.extra")
                            self.denied_dependency(root, source, target)
            (main / "extra.py").unlink()
            for duplicate in (root / "src/demo/main", root / "src/demo/arbitrary/envelopes/main"):
                with self.subTest(duplicate=duplicate):
                    duplicate.mkdir()
                    diagnostics = self.focused_diagnostics(root, "contract-folders")
                    self.assertTrue(any(item["rule"] == "MULTIPLE_MAIN_DIRECTORIES" and
                                        item["path"] == str(duplicate.relative_to(root)) and
                                        item["line"] == item["column"] == 1 for item in
                                        diagnostics), diagnostics)
                    duplicate.rmdir()
            self.assertEqual({item["path"] for item in self.focused_diagnostics(root, "contract-folders")
                              if item["rule"] == "ENVELOPE_DELEGATION"},
                             {"src/demo/arbitrary/envelopes/adapter.py",
                              "src/other/group/deep/envelopes/relay.py"})

    def test_generated_provenance_disagreement_does_not_expose_contracts(self):
        with TemporaryDirectory(prefix="quard-provenance-consumer-") as directory:
            root = Path(directory)
            self.multi_package_fixture(root)
            group = root / "src/demo/arbitrary"
            alias = group / "contract_alias.py"
            stub = alias.with_suffix(".pyi")
            consumer = root / "src/other/group/deep/operations_utils.py"
            original = consumer.read_text()
            consumer.write_text("from demo.arbitrary.contract_alias import Port\n" + original +
                                "\ndef inspect_port(value: Port) -> str:\n"
                                "    return value.first('ok')\n")
            source = "from demo.arbitrary.service import Service as Port\n__all__ = ['Port']\n"
            alias.write_text(source)
            stub.write_text(source)

            def check(rule: str, file: Path):
                diagnostics = self.focused_diagnostics(root, "contract-folders")
                self.assertTrue(any(item["rule"] == rule and item["path"] == str(
                    file.relative_to(root)) and item["line"] > 0 and item["column"] > 0
                    and not item.get("is_infrastructure_error") for item in diagnostics),
                    diagnostics)
                config = generated_policy.render(root)[0]
                exposed = next(item["expose"] for item in config[
                    "interfaces"] if item["from"] == [r"^demo\.arbitrary\.contract_alias$"])
                self.assertNotIn("^Port$", exposed)
                self.denied_dependency(root, "other.group.deep.operations_utils",
                                       "demo.arbitrary.contract_alias")
                self.assertFalse((root / ".quard/architecture.ready.json").exists())

            self.assertEqual(self.focused_diagnostics(root, "contract-folders"), [])
            stub.write_text("from demo.arbitrary.sender import Sender as Port\n__all__ = ['Port']\n")
            check("CONFLICTING_CONTRACT_ALIAS", stub)
            stub.write_text(source)
            alias.write_text(source + "Port = 42\n")
            check("CONFLICTING_CONTRACT_ALIAS", alias)
            alias.write_text(source)
            stub.write_text("__all__ = ['Port']\n")
            check("CONFLICTING_CONTRACT_ALIAS", alias)
            stub.write_text(source)
            consumer.write_text(original)

            contract_stub = group / "service.pyi"
            contract_stub.write_text("class Service(object):\n    def first(self, value: str) -> str: ...\n")
            diagnostics = self.focused_diagnostics(root, "contract-folders")
            self.assertTrue(any(item["rule"] == "CONFLICTING_MODULE_ROLE" and item["path"] ==
                                str(contract_stub.relative_to(root)) for item in diagnostics),
                            diagnostics)
            self.assertNotIn("^Service$", next(item["expose"] for item in
                generated_policy.render(root)[0]["interfaces"] if item["from"] ==
                [r"^demo\.arbitrary\.service$"]))
            self.assertEqual(next(item["layer"] for item in generated_policy.render(root)[0][
                "modules"] if item["path"] == "demo.arbitrary.service"), "Infrastructure")

    def test_generated_install_preserves_reviewed_configuration(self):
        root = self.generated_fixture()
        path = root / "tach.toml"
        config, _ = ConfiguredArchitectureDraft(root).draft()
        serialize = getattr(tomlkit, "dumps")
        path.write_text("# owned by consumer\n" + serialize(config))
        self.install_generated_fixture(root)
        self.assertFalse(generated(root))
        self.assertNotIn("/tach.toml", (root / ".gitignore").read_text())
        self.assertTrue(path.read_text().startswith("# owned by consumer\n"))
