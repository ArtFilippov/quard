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
