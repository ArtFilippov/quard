"""Standalone regressions for drafting and explicitly accepting consumer architecture."""

from __future__ import annotations

from pathlib import Path
from importlib import import_module
import json
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
from quard.architecture_draft.configured_architecture_draft import ConfiguredArchitectureDraft
from offline_fixtures import CATALOG, create_repo, fake_tools, native_result

if TYPE_CHECKING:
    from quard.architecture_draft.generated_architecture_utils import (
        IDENTITY, generated, regenerate,
    )
    from quard.architecture_draft.generated_selection_utils import LEGACY_IDENTITY
    from quard.audit_utils import discover_plugins
    from quard.check_runner.native_check_runner import NativeCheckRunner
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
    NativeCheckRunner = import_module("quard.check_runner.native_check_runner").NativeCheckRunner
    TransactionalInstallation = import_module(
        "quard.installation.transactional_installation").TransactionalInstallation
    apply_utils = import_module("quard.apply_utils")
    entry = import_module("quard.install")


class TestConfiguredArchitectureDraft(unittest.TestCase):
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
        stale = NativeCheckRunner().run(root)
        self.assertEqual(stale["exit_code"], 2)
        self.assertEqual(next(job for job in stale["tools"] if job["name"] ==
                              "pylint-production")["status"], "error")
        self.assertTrue(any(d["is_infrastructure_error"] for d in stale["diagnostics"]))
        configure(root)
        tool = load_toml(project)["tool"]
        self.assertNotIn("clean-arch", tool)
        for config in (tool["pylint"], load_toml(root / ".quard/pylint-tests.toml")[
                "tool"]["pylint"]):
            self.assertNotIn("clean_architecture_linter", config["main"]["load-plugins"])
            self.assertFalse(set(config["messages_control"]["disable"]) & {"W9001", "W9201"})

    def test_real_pylint_profiles_in_generated_and_reviewed_full_checks(self):
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
                plugins, catalog = discover_plugins(root, logs)
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

                report = NativeCheckRunner().run(root)
                self.assertEqual(report["exit_code"], 1, report["diagnostics"])
                self.assertEqual(report["summary"]["infrastructure_errors"], 0,
                                 report["diagnostics"])
                jobs = {job["name"]: job for job in report["tools"]}
                for name in ("pylint-production", "pylint-tests"):
                    self.assertEqual(jobs[name]["status"], "violations", jobs[name])
                    self.assertIsInstance(jobs[name]["exit_code"], int)
                    self.assertGreater(jobs[name]["exit_code"], 0)
                for name in ("tach", "tach-external"):
                    self.assertIn(jobs[name]["status"], {"passed", "violations"})
                    self.assertIn(jobs[name]["exit_code"], {0, 1})
                rules = {(item["tool"], item["rule"], item["path"])
                         for item in report["diagnostics"]}
                self.assertIn(("pylint", "W0611", "src/demo/domain/api.py"), rules)
                self.assertIn(("pylint", "W0611", "tests/test_value.py"), rules)
                self.assertIn(("pylint", "R0903", "src/demo/domain/small.py"), rules)
                self.assertEqual(any(item["rule"] == "W9004" for item in report["diagnostics"]),
                                 not managed)
                self.assertFalse(any(item["rule"] in {"E1111", "R0903"}
                                     and item["path"] == "src/demo/domain/api.py"
                                     for item in report["diagnostics"]))
                if reviewed_tach is not None:
                    self.assertEqual((root / "tach.toml").read_bytes(), reviewed_tach)
                self.assertEqual(read_json(root / ".quard/reports/latest.json")["exit_code"], 1)

    def install_generated_fixture(self, root: Path) -> dict[str, Any]:
        policy = import_module("quard.consumer_policy.configured_consumer_policy")

        def fake_uv(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            if "_audit.py" in " ".join(argv):
                policy.ConfiguredConsumerPolicy(cwd).apply(
                    initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
                write_json(cwd / ".quard/logs/install-probes/result.json",
                           {"versions": {}, "smoke_tests": {"native": "mocked"}})
            return native_result(argv)

        with patch.object(entry, "capture", fake_uv):
            return TransactionalInstallation(entry).install(root, QUARD, progress=False)

    def generated_report(self, root: Path) -> dict[str, Any]:
        fake, _ = fake_tools()

        def real_tach(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            if "_tach_worker.py" in " ".join(argv):
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        def versions(_root: Path) -> tuple[dict[str, str], list[Any]]:
            return {}, []

        return NativeCheckRunner().run(root, capture_fn=real_tach, versions_fn=versions)

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
        report = self.generated_report(root)
        self.assertEqual(path.read_bytes(), expected)
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "tach")
                         ["status"], "passed", report["diagnostics"])
        self.assertEqual(report["exit_code"], 0, report["diagnostics"])
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
        logs = root / ".quard/logs/install-probes"
        plugins, catalog = discover_plugins(root, logs)
        write_json(root / ".quard/pylint-catalog.json", catalog)
        configure(root, initial=True, plugins=plugins, catalog=catalog)
        return utility

    def test_generated_directory_groups_full_consumer_check(self):
        with TemporaryDirectory(prefix="quard-multi-package-") as directory:
            root = Path(directory)
            utility = self.multi_package_fixture(root)
            report = NativeCheckRunner().run(root)
            self.assertEqual((report["exit_code"], report["summary"]["infrastructure_errors"]),
                             (0, 0), report["diagnostics"])
            self.assertTrue(all(job["status"] in {"passed", "skipped"} for job in report["tools"]))
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
            self.assertEqual(NativeCheckRunner().run(root)["exit_code"], 0)
            self.assertEqual((root / "tach.toml").read_bytes(), expected)
            execution = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); " +
                                 "from other.group.deep.operations_utils import inspect; " +
                                 "from demo.arbitrary.sender import Sender; " +
                                 "assert inspect(Sender()) == 'ready:ok'"], root, 10)
            self.assertEqual(execution["exit_code"], 0, execution)

            for suffix in (".py", ".pyi"):
                direct = root / f"src/root{suffix}"
                direct.write_text("from typing import Protocol\nclass Root(Protocol): ...\n")
                rejected = NativeCheckRunner().run(root)
                self.assertEqual((rejected["exit_code"],
                                  rejected["summary"]["infrastructure_errors"]), (1, 0),
                                 rejected["diagnostics"])
                self.assertTrue(any(d["rule"] == "SOURCE_ROOT_LAYOUT" and
                                    d["path"] == f"src/root{suffix}" and
                                    d["line"] == d["column"] == 1
                                    for d in rejected["diagnostics"]))
                self.assertEqual(next(job for job in rejected["tools"] if job["name"] ==
                                      "contract-folders")["status"], "violations")
                direct.unlink()

            (root / "src/root.txt").write_text("unrelated")
            self.assertEqual(NativeCheckRunner().run(root)["exit_code"], 0)
            (root / "src/root.txt").unlink()
            utility.write_text(utility.read_text() +
                               "from demo.domain import api\n")
            rejected = NativeCheckRunner().run(root)
            self.assertEqual((rejected["exit_code"],
                              rejected["summary"]["infrastructure_errors"]), (1, 0),
                             rejected["diagnostics"])
            self.assertEqual((next(job for job in rejected["tools"] if job["name"] == "tach")
                              ["status"], next(job for job in rejected["tools"] if job["name"] ==
                              "tach")["exit_code"]), ("violations", 1))
            self.assertTrue(any(d["tool"] == "tach" and d["path"] ==
                                "src/other/group/deep/operations_utils.py"
                                for d in rejected["diagnostics"]))
            self.assertNotIn("demo.domain.api", next(item for item in
                             load_toml(root / "tach.toml")["modules"] if item["path"] ==
                             "other.group.deep.operations_utils")["depends_on"])

    def test_generated_package_facades_full_consumer_check(self):
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
            assembly = root / "src/other/group/deep/main"
            assembly.mkdir()
            (assembly / "__init__.py").write_text("")
            wire = assembly / "wire.py"
            wire.write_text("from demo.arbitrary.sender import Sender\n"
                            "result = Sender().first('ok')\n")

            def check(expected: int, rule: str | None = None, path: str | None = None):
                report = NativeCheckRunner().run(root)
                self.assertEqual((report["exit_code"], report["summary"]["infrastructure_errors"]),
                                 (expected, 0), report["diagnostics"])
                job = next(item for item in report["tools"] if item["name"] ==
                           "contract-folders")
                self.assertEqual((job["status"], job["exit_code"]),
                                 ("passed", 0) if rule is None else ("violations", 1),
                                 report["diagnostics"])
                if rule is not None:
                    self.assertTrue(any(item["rule"] == rule and item["path"] == path and
                                        item["line"] > 0 and item["column"] > 0 for item in
                                        report["diagnostics"]), report["diagnostics"])
                return report

            clean = check(0)
            self.assertTrue(all(item["status"] in {"passed", "skipped"} for item in
                                clean["tools"]))
            config = load_toml(root / "tach.toml")
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
                                           load_toml(root / "tach.toml")["interfaces"] if
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
            rejected = NativeCheckRunner().run(root)
            self.assertEqual((rejected["exit_code"], rejected["summary"]["infrastructure_errors"]),
                             (1, 0), rejected["diagnostics"])
            self.assertEqual(next(item for item in rejected["tools"] if item["name"] == "tach")
                             ["status"], "violations")
            self.assertTrue(any(item["job"] == "tach" and item["path"] ==
                                str(wire.relative_to(root)) for item in rejected["diagnostics"]))

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

            def check() -> dict[str, Any]:
                report = NativeCheckRunner().run(root)
                self.assertEqual(report["summary"]["infrastructure_errors"], 0,
                                 report["diagnostics"])
                self.assertEqual(next(job for job in report["tools"] if job["name"] ==
                                      "contract-folders")["exit_code"],
                                 1 if any(item["tool"] == "contract-folders" for item in
                                          report["diagnostics"]) else 0)
                return report

            clean = check()
            self.assertEqual(clean["exit_code"], 0, clean["diagnostics"])
            self.assertEqual(next(item for item in load_toml(root / "tach.toml")["modules"]
                                  if item["path"] == "other.group.deep.combined")["layer"],
                             "Contract")
            self.assertEqual(next(item for item in load_toml(root / "tach.toml")["modules"]
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
            failed = check()
            self.assertEqual(failed["exit_code"], 1)
            self.assertTrue(any(item["rule"] == "CROSS_GROUP_CONTRACT" and
                                item["path"] == "src/other/group/deep/bridge.py" and
                                item["line"] == 3 and item["column"] == 1
                                for item in failed["diagnostics"]), failed["diagnostics"])
            bridge.write_text(before)

            # Source and stubs are checked separately, even when the source passes.
            stub = bridge.with_suffix(".pyi")
            before_stub = stub.read_text()
            stub.write_text(before_stub.replace("class Bridge(Combined):", "class Bridge(Service):")
                            .replace("from other.group.deep.combined import Combined",
                                     "from demo.arbitrary.service import Service"))
            failed = check()
            self.assertEqual(failed["exit_code"], 1)
            self.assertTrue(any(item["rule"] == "CROSS_GROUP_CONTRACT" and
                                item["path"] == "src/other/group/deep/bridge.pyi"
                                for item in failed["diagnostics"]), failed["diagnostics"])
            stub.write_text(before_stub)

            # A concrete class in the same module must not acquire a Contract role.
            extra.write_text(extra.read_text() + "\nclass Accidental(Extra):\n"
                             "    def third(self) -> str: return 'ok'\n")
            failed = check()
            self.assertEqual(failed["exit_code"], 1)
            self.assertTrue(any(item["rule"] == "MIXED_MODULE_ROLES" and
                                item["path"] == "src/demo/arbitrary/extra.py" and
                                item["line"] > 0 and item["column"] > 0
                                for item in failed["diagnostics"]), failed["diagnostics"])
            self.assertEqual(next(item for item in load_toml(root / "tach.toml")["modules"]
                                  if item["path"] == "demo.arbitrary.extra")["layer"],
                             "Infrastructure")
            extra.write_text(extra.read_text().split("\nclass Accidental")[0] + "\n")

            # A companion stub cannot redefine the source contract as concrete.
            extra_stub = extra.with_suffix(".pyi")
            original_stub = extra_stub.read_text()
            extra_stub.write_text("class Extra(object):\n    def third(self) -> str: ...\n")
            failed = check()
            self.assertEqual(failed["exit_code"], 1)
            self.assertTrue(any(item["rule"] == "CONFLICTING_MODULE_ROLE" and
                                item["path"] == "src/demo/arbitrary/extra.pyi" and
                                item["line"] == item["column"] == 1
                                for item in failed["diagnostics"]), failed["diagnostics"])
            extra_stub.write_text(original_stub)

            # An unresolved customer base must not select a group by guesswork.
            bridge.write_text(before.replace("class Bridge(Combined):", "class Bridge(Combined, Ghost):")
                              .replace("from other.group.deep.combined import Combined",
                                       "from other.group.deep.combined import Combined\n"
                                       "from demo.absent import Ghost"))
            failed = check()
            self.assertEqual(failed["exit_code"], 1)
            self.assertTrue(any(item["rule"] == "UNRESOLVED_GROUP_OWNER" and
                                item["path"] == "src/other/group/deep/bridge.py"
                                for item in failed["diagnostics"]), failed["diagnostics"])

    def test_generated_exact_external_inherited_contracts_full_check(self):
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
            facade.write_text("from collections import UserList as PublicItems\n"
                              "from builtins import Exception as PublicError\n"
                              "__all__ = ['PublicItems', 'PublicError']\n")
            use = root / "src/other/group/deep/external_utils.py"
            use.write_text("from other.group.deep.external_api import PublicItems\n\n"
                           "def read(items: PublicItems[str]) -> str:\n"
                           "    return items[0]\n")

            def check() -> dict[str, Any]:
                report = NativeCheckRunner().run(root)
                self.assertEqual(report["summary"]["infrastructure_errors"], 0,
                                 report["diagnostics"])
                return report

            clean = check()
            self.assertEqual(clean["exit_code"], 0, clean["diagnostics"])
            self.assertTrue(all(job["status"] in {"passed", "skipped"} for job in clean["tools"]))
            modules = {item["path"]: item for item in load_toml(root / "tach.toml")["modules"]}
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
                failed = check()
                self.assertEqual(failed["exit_code"], 1, failed["diagnostics"])
                self.assertEqual(next(job for job in failed["tools"] if job["name"] ==
                                      "tach")["status"], "violations")
                self.assertTrue(any(item["job"] == "tach" and item["path"] ==
                                    "src/other/group/deep/external_utils.py" and
                                    "Unrelated" in item["message"] for item in
                                    failed["diagnostics"]), failed["diagnostics"])
                interfaces = [item for item in load_toml(root / "tach.toml")["interfaces"]
                              if item["from"] == [r"^other\.group\.deep\.external_api$"]]
                self.assertFalse(any(r"^Unrelated$" in item["expose"] for item in interfaces))
            facade.write_text(original)
            use.write_text(original_use)

            original_stub = implementation.with_suffix(".pyi").read_text()
            implementation.with_suffix(".pyi").write_text(original_stub.replace(
                "UserList[str]", "UserDict[str, str]").replace(
                    "from collections import UserList", "from collections import UserDict"))
            failed = check()
            self.assertEqual(failed["exit_code"], 1, failed["diagnostics"])
            self.assertTrue(any(item["rule"] == "UNRESOLVED_EXTERNAL_CONTRACT" and
                                item["path"] == "src/demo/arbitrary/external_items.pyi" and
                                item["line"] > 0 and item["column"] > 0
                                for item in failed["diagnostics"]), failed["diagnostics"])
            implementation.with_suffix(".pyi").write_text(original_stub)

            implementation.with_suffix(".pyi").write_text(original_stub.replace(
                "UserList[str]", "UserList[int]"))
            failed = check()
            self.assertEqual(failed["exit_code"], 1, failed["diagnostics"])
            self.assertTrue(any(item["rule"] == "UNRESOLVED_EXTERNAL_CONTRACT" and
                                item["path"] == "src/demo/arbitrary/external_items.pyi"
                                for item in failed["diagnostics"]), failed["diagnostics"])
            implementation.with_suffix(".pyi").write_text(original_stub)

            implementation.write_text(implementation.read_text().replace(
                "class ExternalItems", "UserList = object\n\nclass ExternalItems"))
            failed = check()
            self.assertEqual(failed["exit_code"], 1, failed["diagnostics"])
            self.assertTrue(any(item["rule"] in {"UNRESOLVED_GROUP_OWNER",
                                                 "UNRESOLVED_EXTERNAL_CONTRACT"} and
                                item["path"] == "src/demo/arbitrary/external_items.py"
                                for item in failed["diagnostics"]), failed["diagnostics"])
            implementation.write_text(implementation.read_text().replace("UserList = object\n\n", ""))

            original_source = implementation.read_text()
            for replacement in ("from collections import UserDict as UserList\n",
                                "UserList = object\n"):
                implementation.write_text(original_source.replace(
                    "class ExternalItems", replacement + "\nclass ExternalItems"))
                failed = check()
                self.assertEqual(failed["exit_code"], 1, failed["diagnostics"])
                self.assertTrue(any(item["rule"] == "UNRESOLVED_EXTERNAL_CONTRACT" and
                                    item["path"] == "src/demo/arbitrary/external_items.py" and
                                    item["line"] > 0 and item["column"] > 0
                                    for item in failed["diagnostics"]), failed["diagnostics"])
                self.assertEqual(next(job for job in failed["tools"] if job["name"] ==
                                      "contract-folders")["status"], "violations")
            implementation.write_text(original_source)

            unproved = group / "unproved.py"
            unproved.write_text("class Unproved(UnknownBase):\n    pass\n")
            failed = check()
            self.assertEqual(failed["exit_code"], 1, failed["diagnostics"])
            self.assertTrue(any(item["rule"] == "UNRESOLVED_GROUP_OWNER" and
                                item["path"] == "src/demo/arbitrary/unproved.py" and
                                item["line"] == item["column"] == 1 for item in
                                failed["diagnostics"]), failed["diagnostics"])
            unproved.unlink()

            use.write_text(use.read_text() + "import tomlkit\n")
            failed = check()
            self.assertEqual(failed["exit_code"], 1, failed["diagnostics"])
            self.assertEqual(next(job for job in failed["tools"] if job["name"] ==
                                  "tach-external")["status"], "violations")
            self.assertTrue(any(item["job"] == "tach-external" and
                                item["rule"] == "UndeclaredExternalDependency" for item in
                                failed["diagnostics"]), failed["diagnostics"])

    def test_reviewed_folder_rules_survive_generated_role_changes(self):
        with TemporaryDirectory(prefix="quard-reviewed-roles-") as directory:
            root = Path(directory)
            create_repo(root)
            config = (root / "tach.toml").read_bytes()
            source = root / "src/demo/domain/api.py"
            source.write_text("from typing import Protocol\n"
                              "class AnyName(Protocol):\n    def read(self) -> str: ...\n")
            (root / "src/demo/client.py").write_text("from demo.domain.api import AnyName\n")
            logs = root / ".quard/logs/install-probes"
            logs.mkdir(parents=True, exist_ok=True)
            plugins, catalog = discover_plugins(root, logs)
            write_json(root / ".quard/pylint-catalog.json", catalog)
            configure(root, initial=True, plugins=plugins, catalog=catalog)
            report = NativeCheckRunner().run(root)
            self.assertEqual(report["exit_code"], 1, report["diagnostics"])
            self.assertEqual(report["summary"]["infrastructure_errors"], 0,
                             report["diagnostics"])
            self.assertTrue(any(item["rule"] == "CONTRACT_FOLDER" and
                                item["path"] == "src/demo/domain/api.py"
                                for item in report["diagnostics"]), report["diagnostics"])
            self.assertFalse(any(item["rule"] == "MIXED_MODULE_ROLES"
                                 for item in report["diagnostics"]))
            self.assertEqual((root / "tach.toml").read_bytes(), config)

    def test_generated_forbidden_imports_keep_real_tach_failure(self):
        root = self.generated_fixture()
        self.install_generated_fixture(root)
        path = root / "tach.toml"
        client = root / "src/demo/client.py"
        client.write_text(client.read_text() + "from demo.port.impl import Impl\n",
                          encoding="utf-8")
        path.write_text(path.read_text() + "\n# tampered\n")
        report = self.generated_report(root)
        module = next(item for item in load_toml(path)["modules"]
                      if item["path"] == "demo.client")
        self.assertEqual(module["depends_on"], ["demo.port.port"])
        self.assertNotIn("tampered", path.read_text())
        self.assertEqual(report["exit_code"], 1, report["diagnostics"])
        tach = next(job for job in report["tools"] if job["name"] == "tach")
        self.assertEqual((tach["status"], tach["exit_code"]), ("violations", 1))
        self.assertTrue(any(d["tool"] == "tach" and d["path"] == "src/demo/client.py"
                            for d in report["diagnostics"]), report["diagnostics"])
        self.assertEqual(read_json(root / ".quard/reports/latest.json")["exit_code"], 1)

    def test_generated_exact_role_matrix_uses_real_tach_on_full_consumer(self):
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

            def check() -> dict[str, Any]:
                report = NativeCheckRunner().run(root)
                self.assertEqual(report["summary"]["infrastructure_errors"], 0,
                                 report["diagnostics"])
                return report

            clean = check()
            self.assertEqual(clean["exit_code"], 0, clean["diagnostics"])
            execution = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                                 + "from other.group.deep.main.wire import result; "
                                 + "assert result == 'ready:ok'"], root, 10)
            self.assertEqual(execution["exit_code"], 0, execution)
            self.assertEqual({job["name"]: job["status"] for job in clean["tools"]}[
                "tach-external"], "passed")
            config = load_toml(root / "tach.toml")
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
            module_import = check()
            self.assertEqual(module_import["exit_code"], 0, module_import["diagnostics"])
            self.assertEqual(next(item["depends_on"] for item in load_toml(
                root / "tach.toml")["modules"] if item["path"] ==
                "other.group.deep.operations_utils"), ["demo.arbitrary.service"])
            utility.write_text(original_utility)
            utility_stub.write_text(original_utility_stub)
            self.assertEqual(check()["exit_code"], 0)

            for file, addition in (
                (utility, "\nfrom demo.arbitrary.sender import Sender\n"),
                (utility, "\nimport demo.arbitrary.sender\n"),
                (implementation, "\nfrom demo.arbitrary.sender_peer import Peer\n"),
                (contract, "\nfrom demo.arbitrary.sender import Sender\n"),
                (utility, "\nfrom typing import TYPE_CHECKING\n"
                 "if TYPE_CHECKING:\n    from demo.arbitrary.sender import Sender\n"),
                (bootstrap, "\nfrom other.group.deep.main.other import Other\n"),
            ):
                with self.subTest(file=file, addition=addition):
                    if file == implementation:
                        peer = file.with_name("sender_peer.py")
                        peer.write_text("class Peer(object):\n    pass\n")
                    elif file == bootstrap:
                        peer = file.with_name("other.py")
                        peer.write_text("class Other(object):\n    pass\n")
                    else:
                        peer = None
                    before = file.read_text()
                    file.write_text(before + addition)
                    try:
                        failed = check()
                        self.assertEqual(failed["exit_code"], 1, failed["diagnostics"])
                        tach = next(job for job in failed["tools"] if job["name"] == "tach")
                        self.assertEqual((tach["status"], tach["exit_code"]),
                                         ("violations", 1), failed["diagnostics"])
                        self.assertTrue(any(item["job"] == "tach" and item["path"] ==
                                            str(file.relative_to(root)) and item["line"] > 0
                                            for item in failed["diagnostics"]),
                                        failed["diagnostics"])
                        changed = {item["path"]: item for item in
                                   load_toml(root / "tach.toml")["modules"]}
                        self.assertEqual(changed[file.relative_to(root / "src").with_suffix(
                            "").as_posix().replace("/", ".")]["depends_on"],
                                         modules[file.relative_to(root / "src").with_suffix(
                                             "").as_posix().replace("/", ".")]["depends_on"])
                    finally:
                        file.write_text(before)
                        if peer is not None:
                            peer.unlink()

            self.assertEqual(check()["exit_code"], 0)
            original_contract = contract.read_text()
            contract.write_text(original_contract +
                                "\nfrom other.group.deep.aliases import PublicService\n")
            cyclic = check()
            self.assertEqual(cyclic["exit_code"], 1, cyclic["diagnostics"])
            self.assertEqual(next(job for job in cyclic["tools"] if job["name"] == "tach")[
                "status"], "violations")
            self.assertTrue(any(item["job"] == "tach" and item["rule"] ==
                                "cyclic-dependency" for item in cyclic["diagnostics"]),
                            cyclic["diagnostics"])
            contract.write_text(original_contract)
            self.assertEqual(check()["exit_code"], 0)
            root_file = root / "src/direct.py"
            root_file.write_text("from demo.arbitrary.service import Service\n")
            root_failure = check()
            self.assertEqual(root_failure["exit_code"], 1, root_failure["diagnostics"])
            self.assertTrue(any(item["rule"] == "SOURCE_ROOT_LAYOUT" and item["path"] ==
                                "src/direct.py" for item in root_failure["diagnostics"]),
                            root_failure["diagnostics"])
            root_file.unlink()
            original_utility = utility.read_text()
            utility.write_text(original_utility + "\nimport tomlkit\n")
            external = check()
            self.assertEqual(external["exit_code"], 1, external["diagnostics"])
            self.assertEqual(next(job for job in external["tools"] if job["name"] ==
                                  "tach-external")["status"], "violations")
            self.assertTrue(any(item["job"] == "tach-external" and item["path"] ==
                                str(utility.relative_to(root)) for item in
                                external["diagnostics"]), external["diagnostics"])
            utility.write_text(original_utility)
            self.assertEqual(check()["exit_code"], 0)
            # Exact/unused is checked by the pinned native adapter; regeneration
            # deliberately removes this manually inserted unused declaration.
            config = load_toml(root / "tach.toml")
            next(item for item in config["modules"] if item["path"] ==
                 "other.group.deep.operations_utils")["depends_on"].append(
                     "demo.arbitrary.service")
            (root / "tach.toml").write_text(tomlkit.dumps(config))
            native = capture([sys.executable, str(QUARD / "_tach_worker.py"),
                              "check"], root)
            self.assertEqual(native["exit_code"], 1, native)
            self.assertIn("unused-dependency", {item["rule"] for item in
                          json.loads(native["stdout"])["diagnostics"]})
            self.assertEqual(check()["exit_code"], 0)
            self.assertEqual((root / "tach.toml").read_bytes(), expected)

    def test_generated_full_static_and_runtime_dependency_types(self):
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
        report = NativeCheckRunner().run(root)
        self.assertEqual((report["exit_code"], report["summary"]["infrastructure_errors"]), (1, 0))
        dependency = next(job for job in report["tools"] if job["name"] == "dependency-types")
        tach = next(job for job in report["tools"] if job["name"] == "tach")
        pyright = next(job for job in report["tools"] if job["name"] == "pyright")
        self.assertEqual((dependency["status"], dependency["exit_code"]), ("violations", 1))
        self.assertEqual((tach["status"], tach["exit_code"]), ("violations", 1))
        self.assertEqual((pyright["status"], pyright["exit_code"]), ("violations", 1))
        self.assertIn("CONCRETE_DEPENDENCY_TYPE", {d["rule"] for d in report["diagnostics"]})
        executed = capture([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); "
                            "from demo.client import Client; from demo.port.impl import Impl; "
                            "assert isinstance(Client().build(Impl()), Impl)"], root, 10)
        self.assertEqual(executed["exit_code"], 0, executed)

    def test_generated_native_error_is_not_mistaken_for_success(self):
        root = self.generated_fixture()
        self.install_generated_fixture(root)
        fake, _ = fake_tools({"tach": {"stdout": "native Tach failed", "code": 2},
                              "pydoclint": {"stderr": (
                                  "src/demo/domain/api.py:1: DOC101: Native issue\n"), "code": 1}})
        def versions(_root: Path) -> tuple[dict[str, str], list[Any]]:
            return {}, []

        report = NativeCheckRunner().run(root, capture_fn=fake, versions_fn=versions)
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "tach")
                         ["status"], "error")
        self.assertIn("DOC101", {item["rule"] for item in report["diagnostics"]})
        self.assertTrue(any(item["is_infrastructure_error"] for item in report["diagnostics"]))

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
