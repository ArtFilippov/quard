"""Installation, migration and lifecycle through the public transaction contract."""

from __future__ import annotations

import difflib
import io
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

import tomlkit

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from installation_fixture_utils import fake_uv, run_command
from offline_fixtures import create_repo, managed_bytes, native_result
from quard import install as entry
from quard import _policy
from quard.duplication_utils import read_duplication_min_lines
from quard.check_selection_utils import CHECK_DEFAULTS, OPTIONAL_JOBS
from quard.architecture_draft.generated_architecture_utils import IDENTITY
from quard._audit import smoke_tests, tool_binary
from quard._common import (QuardError, load_toml, read_json, repo_lock, safe_target,
                          write_json)
from quard.installation.installation import Installation
from quard.installation.transactional_installation import TransactionalInstallation


class TestTransactionalInstallation(unittest.TestCase):
    def test_source_build_failure_rolls_back_and_logs(self):
        root = self.configured_project()
        before = managed_bytes(root)
        commands: list[list[str]] = []
        specs = entry.dependency_specs(QUARD)

        def fail_sync(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            commands.append(argv)
            return native_result(argv, stderr="cargo is required", code=int(argv[1] == "sync"))

        with patch.object(entry, "dependency_specs", return_value=specs), patch.object(
                entry, "capture", fail_sync), self.assertRaises(QuardError):
            self.installation().install(root, QUARD, progress=False)
        self.assertEqual(len(commands), 2)
        for argv in commands:
            self.assertNotIn("--no-build-package", argv)
        self.assertIn("quard-clones==0.1.0", commands[0])
        source = load_toml(root / "pyproject.toml").get("tool", {}).get("uv", {}).get("sources", {})
        self.assertNotIn("quard-clones", source)
        self.assertEqual(managed_bytes(root), before)
        self.assertTrue(read_json(root / ".quard/logs/install-error.json")
                        ["managed_files_rolled_back"])
        self.assertIn("cargo is required", (root / ".quard/logs/install.log").read_text())

    def test_real_non_clone_installer_smoke_probes(self):
        root = self.configured_project()
        self.disable_native_checks(root)
        (root / "pyproject.toml").write_bytes((QUARD / "pyproject.toml").read_bytes())
        logs = root / ".quard/logs/install-probes"
        logs.mkdir(parents=True)
        with patch("quard.audit_utils.read_json", wraps=read_json) as manifest:
            # The standalone CLI smoke fixture runs without installing a wheel.
            manifest.side_effect = lambda path: ({"dependencies": {"jscpd": "5.4.0"}}
                                                if Path(path).name == "tools.json"
                                                else read_json(path))
            result = smoke_tests(root, logs)
        self.assertEqual(result["pylint_config_and_fixture"], "passed")
        self.assertEqual(result["pyright_type_error"], "detected")
        self.assertEqual(result["pydoclint_signature_error"], "detected")
        self.assertNotIn("jscpd_exact_clone", result)
        self.assertEqual(result["tach_undeclared_dependency"], "detected")
        self.assertEqual(tool_binary("pydoclint"),
                         str(Path(sys.executable).parent / "pydoclint"))
        with self.assertRaises(QuardError) as error:
            tool_binary("nonexistent-quard-tool")
        self.assertEqual(error.exception.code, "MISSING_TOOL")

    def test_repo_lock_refuses_concurrent_operations(self):
        root = self.configured_project()
        with repo_lock(root):
            with self.assertRaises(QuardError):
                with repo_lock(root):
                    pass

    def test_safe_target_rejects_path_traversal(self):
        root = self.configured_project()
        with self.assertRaises(QuardError):
            safe_target(root / "../escape", root)

    def configured_project(self) -> Path:
        root = Path(self.enterContext(TemporaryDirectory(prefix="quard-migration-")))
        create_repo(root)
        return root

    def disable_native_checks(self, root: Path) -> None:
        setting = root / ".quard/config.toml"
        text = setting.read_text(encoding="utf-8")
        for key in ("pyright", "pydoclint", "duplicate_detection"):
            text = text.replace(f"{key} = true", f"{key} = false")
        setting.write_text(text, encoding="utf-8")

    def installation(self) -> Installation:
        return TransactionalInstallation(entry)

    def project(self, directory: str) -> Path:
        root = Path(directory)
        (root / "src/example").mkdir(parents=True)
        (root / "src/example/__init__.py").write_text("", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            '[project]\nname = "example"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n',
            encoding="utf-8",
        )
        return root

    def test_dry_run_does_not_write_consumer_files(self):
        with TemporaryDirectory(prefix="quard-installation-") as directory:
            root = self.project(directory)
            before = (root / "pyproject.toml").read_bytes()
            installation = self.installation()
            plan = installation.plan(root, QUARD)
            result = installation.install(root, QUARD, dry_run=True)
            self.assertEqual((result["status"], result["exit_code"]), ("planned", 0))
            self.assertEqual({key: result[key] for key in plan}, plan)
            self.assertEqual((root / "pyproject.toml").read_bytes(), before)
            self.assertFalse((root / ".quard").exists())

    def test_failed_tool_step_restores_managed_files_and_logs_failure(self):
        def fail(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            return {"argv": argv, "stdout": "", "stderr": "offline failure",
                    "exit_code": 1, "error": None}

        with TemporaryDirectory(prefix="quard-installation-") as directory:
            root = self.project(directory)
            before = (root / "pyproject.toml").read_bytes()
            with patch.object(entry, "capture", fail):
                with self.assertRaises(QuardError):
                    self.installation().install(root, QUARD, progress=False)
            self.assertEqual((root / "pyproject.toml").read_bytes(), before)
            self.assertTrue(read_json(root / ".quard/logs/install-error.json")
                            ["managed_files_rolled_back"])

    def test_plan_does_not_change_configured_consumer(self):
        root = self.configured_project()
        before = managed_bytes(root)
        result = self.installation().install(root, QUARD, dry_run=True, progress=False)
        self.assertEqual(result["status"], "planned")
        self.assertIn("pyright[nodejs]==1.1.414", result["dependencies"])
        self.assertNotIn("jscpd==5.4.0", result["dependencies"])
        self.assertTrue(result["clone_source"]["git"].startswith(("ssh://", "https://")))
        self.assertEqual(managed_bytes(root), before)

    def test_rollback_on_uv_failure_leaves_failure_log(self):
        root = self.configured_project()
        (root / ".quard/config.toml").unlink()
        before = managed_bytes(root)

        def fail(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            return native_result(argv, stderr="intentional offline test failure", code=1)

        with patch.object(entry, "capture", fail), self.assertRaises(QuardError):
            self.installation().install(root, QUARD, progress=False)
        self.assertEqual(managed_bytes(root), before)
        self.assertFalse((root / ".quard/config.toml").exists())
        self.assertTrue(read_json(root / ".quard/logs/install-error.json")
                        ["managed_files_rolled_back"])

    def test_prepare_project_idempotent(self):
        root = self.configured_project()
        entry.prepare_project(root)
        first = (root / "pyproject.toml").read_bytes()
        entry.prepare_project(root)
        self.assertEqual(first, (root / "pyproject.toml").read_bytes())
        data = load_toml(root / "pyproject.toml")
        self.assertEqual(data["project"]["requires-python"], ">=3.12,<3.15")
        self.assertEqual(data["tool"]["uv"]["dependency-groups"]["dev"]["requires-python"],
                         ">=3.12")

    def test_full_install_reinstall_mocked(self):
        root = self.configured_project()
        with patch.object(entry, "capture", fake_uv):
            self.installation().install(root, QUARD, progress=False)
            first = managed_bytes(root)
            self.installation().install(root, QUARD, progress=False)
            self.assertEqual(managed_bytes(root), first)

    def test_upgrade_adds_new_enabled_switches_without_overwriting_choices(self):
        root = self.configured_project()
        setting = root / ".quard/config.toml"
        text = setting.read_text(encoding="utf-8")
        for key in CHECK_DEFAULTS:
            if key == "constructor_purity":
                continue
            text = text.replace(f"{key} = true\n", "")
        text = text.replace("constructor_purity = false",
                            "# Retain this choice\nconstructor_purity = true\n"
                            + "instance_fields = false")
        setting.write_text(text, encoding="utf-8")
        with patch.object(entry, "capture", fake_uv):
            self.installation().install(root, QUARD, progress=False)
            updated = setting.read_text(encoding="utf-8")
            self.installation().install(root, QUARD, progress=False)
        self.assertEqual(setting.read_text(encoding="utf-8"), updated)
        self.assertIn("# Retain this choice", updated)
        self.assertTrue(_policy.read_checks(root)["constructor_purity"])
        self.assertFalse(_policy.read_checks(root)["instance_fields"])
        self.assertTrue(all(_policy.read_checks(root)[key] for key in OPTIONAL_JOBS
                            if key != "instance_fields"))
        self.assertTrue(_policy.read_checks(root)["duplicate_detection"])

    def test_duplication_setting_survives_install_reinstall_and_apply_entry_point(self):
        root = self.configured_project()
        setting = root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "min_lines = 3", "min_lines = 6\n# Consumer clone preference"), encoding="utf-8")
        expected = setting.read_bytes()
        original_pylint = load_toml(root / "pyproject.toml")["tool"]["pylint"][
            "similarities"]["min-similarity-lines"]
        with patch.object(entry, "capture", fake_uv), patch.object(sys, "argv", [
                str(QUARD / "install.py"), "--project", str(root), "--json"]):
            self.assertEqual(entry.main(), 0)
            self.assertEqual(entry.main(), 0)
        self.assertEqual(json.loads(run_command([sys.executable, str(QUARD / "apply.py"),
                                                 "--project", str(root), "--json"], root))[
            "status"], "applied")
        self.assertEqual((setting.read_bytes(), read_duplication_min_lines(root)),
                         (expected, 6))
        native = load_toml(root / "pyproject.toml")
        self.assertEqual(native["tool"]["pylint"]["similarities"]["min-similarity-lines"],
                         original_pylint)
        self.assertEqual(native["tool"]["unrelated"]["value"], "retain")

    def test_legacy_duplication_settings_default_without_migration(self):
        root = self.configured_project()
        setting = root / ".quard/config.toml"
        for old in (setting.read_text(encoding="utf-8").replace(
                "[duplication]\nmin_lines = 3\n", ""),
                setting.read_text(encoding="utf-8").replace("min_lines = 3\n", "")):
            with self.subTest(old=old):
                setting.write_text(old, encoding="utf-8")
                self.assertEqual(read_duplication_min_lines(root), 3)
                with patch.object(entry, "capture", fake_uv), patch.object(sys, "argv", [
                        str(QUARD / "install.py"), "--project", str(root), "--json"]):
                    self.assertEqual(entry.main(), 0)
                self.assertEqual(read_duplication_min_lines(root), 3)

    def test_invalid_duplication_settings_are_configuration_errors_at_public_commands(self):
        root = self.configured_project()
        setting = root / ".quard/config.toml"
        original = setting.read_text(encoding="utf-8")
        for replacement, rule in (("min_lines = true", "INVALID_DUPLICATION"),
                                  ("min_lines = 0", "INVALID_DUPLICATION"),
                                  ("min_lines = -1", "INVALID_DUPLICATION"),
                                  ("min_lines = 2.5", "INVALID_DUPLICATION"),
                                  ('min_lines = "3"', "INVALID_DUPLICATION"),
                                  ("other = 3", "INVALID_DUPLICATION"),
                                  ("[duplication", "INVALID_CONFIG")):
            with self.subTest(value=replacement):
                setting.write_text(original.replace("[duplication]\nmin_lines = 3",
                                                     "[duplication]\n" + replacement),
                                   encoding="utf-8")
                with self.assertRaises(QuardError) as invalid:
                    read_duplication_min_lines(root)
                self.assertEqual(invalid.exception.code, rule)
                if replacement not in {"min_lines = true", "[duplication"}:
                    continue
                for command in ("apply.py", "check.py"):
                    output = run_command([sys.executable, str(QUARD / command),
                                          "--project", str(root), "--json"], root, expected=2)
                    response = json.loads(output)
                    self.assertEqual(response["exit_code"], 2)
                    if command == "apply.py":
                        self.assertEqual(response["code"], rule)
                    else:
                        self.assertIn(rule, {d["rule"] for d in response["diagnostics"]})
                        self.assertTrue(all(d["is_infrastructure_error"]
                                            for d in response["diagnostics"] if d["rule"] == rule))
                with patch.object(entry, "capture", wraps=fake_uv) as captured, patch.object(
                        sys, "argv", [str(QUARD / "install.py"), "--project", str(root),
                                      "--json"]), patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    self.assertEqual(entry.main(), 2)
                    self.assertEqual(json.loads(stdout.getvalue())["code"], rule)
                captured.assert_not_called()
        setting.write_text("duplication = 3\n" + original.replace(
            "[duplication]\nmin_lines = 3\n", ""), encoding="utf-8")
        output = json.loads(run_command([sys.executable, str(QUARD / "apply.py"),
                                         "--project", str(root), "--json"], root, expected=2))
        self.assertEqual(output["code"], "INVALID_DUPLICATION")

    def test_architecture_switch_upgrade_defaults_follow_selected_mode(self):
        for generated_policy in (False, True):
            with self.subTest(generated=generated_policy):
                root = self.configured_project()
                if generated_policy:
                    write_json(root / ".quard/architecture.generated.json", IDENTITY)
                setting = root / ".quard/config.toml"
                setting.write_text(setting.read_text(encoding="utf-8")
                                   .replace("dependency_types = false\n", "")
                                   .replace("tach = true\n", ""), encoding="utf-8")
                with patch.object(entry, "capture", fake_uv):
                    self.installation().install(root, QUARD, progress=False)
                self.assertEqual(_policy.read_checks(root)["dependency_types"], generated_policy)
                self.assertTrue(_policy.read_checks(root)["tach"])
                first = setting.read_bytes()
                with patch.object(entry, "capture", fake_uv):
                    self.installation().install(root, QUARD, progress=False)
                self.assertEqual(setting.read_bytes(), first)

    def test_reviewed_dependency_opt_in_is_rejected_before_install_steps(self):
        root = self.configured_project()
        setting = root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "dependency_types = false", "dependency_types = true"), encoding="utf-8")
        before = managed_bytes(root)
        with patch.object(entry, "capture", side_effect=fake_uv) as captured:
            with self.assertRaisesRegex(QuardError, "requires generated architecture"):
                self.installation().install(root, QUARD, progress=False)
        captured.assert_not_called()
        self.assertEqual(managed_bytes(root), before)

    def test_legacy_checks_migration_enables_new_switches(self):
        root = self.configured_project()
        (root / ".quard/config.toml").unlink()
        (root / ".quard/checks.toml").write_text("constructor_purity = true\n")
        with patch.object(entry, "capture", fake_uv):
            self.installation().install(root, QUARD, progress=False)
        self.assertTrue(_policy.read_checks(root)["constructor_purity"])
        self.assertTrue(all(_policy.read_checks(root)[key] for key in OPTIONAL_JOBS))
        self.assertTrue(_policy.read_checks(root)["duplicate_detection"])
        self.assertFalse((root / ".quard/checks.toml").exists())

    def test_reinstall_retains_disabled_native_checks_and_still_runs_audit(self):
        root = self.configured_project()
        self.disable_native_checks(root)
        with patch.object(entry, "capture", side_effect=fake_uv) as captured:
            self.installation().install(root, QUARD, progress=False)
        self.assertTrue(all(not _policy.read_checks(root)[key] for key in
                            ("pyright", "pydoclint", "duplicate_detection")))
        commands = [call.args[0] for call in captured.call_args_list]
        self.assertEqual(len(commands), 3)
        self.assertIn("pyright[nodejs]==1.1.414", commands[0])
        self.assertIn("pydoclint==0.9.1", commands[0])
        self.assertNotIn("jscpd==5.4.0", commands[0])
        self.assertIn(str(QUARD / "_audit.py"), commands[2])

    def test_migrate_legacy_policy_and_multi_package_architecture(self):
        root = self.configured_project()
        path = root / "pyproject.toml"
        document = load_toml(path)
        document["tool"]["pylint"]["design"]["ignored-parents"].append("foreign.Framework")
        document["tool"]["pyright"]["stubPath"] = "src/stubs"
        document["tool"]["uv"] = {
            "sources": {"foreign": {"git": "https://example.org/repo", "rev": "abc"}},
            "index": [{"name": "custom", "url": "https://example.org/simple",
                       "explicit": True}],
            "dependency-metadata": [{"name": "foreign", "requires-dist": []}],
        }
        path.write_text(tomlkit.dumps(document))
        write_json(root / "pyrightconfig.json", document["tool"]["pyright"])
        (root / "src/other").mkdir()
        (root / "src/other/__init__.py").write_text("")
        limits = root / ".quard/config.toml"
        limits.write_text(limits.read_text().replace("max_complexity = 8", "max_complexity = 6"))
        _policy.configure(root)
        for name in ("readability.toml", "scope.toml", "checks.toml"):
            (root / ".quard" / name).write_bytes((QUARD / "defaults" / name).read_bytes())
        legacy_limits = root / ".quard/readability.toml"
        legacy_limits.write_text(legacy_limits.read_text().replace("max_complexity = 8", "max_complexity = 6"))
        limits.unlink()
        original_tach = (root / "tach.toml").read_bytes()
        original_marker = (root / ".quard/architecture.ready.json").read_bytes()
        (root / ".quard").rename(root / "quality")
        with patch.object(entry, "capture", fake_uv):
            self.installation().install(root, QUARD, progress=False)
        actual = load_toml(path)
        self.assertIn("foreign.Framework", actual["tool"]["pylint"]["design"]["ignored-parents"])
        self.assertEqual(actual["tool"]["pyright"]["stubPath"], "src/stubs")
        for key in ("index", "dependency-metadata"):
            self.assertEqual(actual["tool"]["uv"][key], document["tool"]["uv"][key])
        self.assertEqual(actual["tool"]["uv"]["sources"]["foreign"],
                         document["tool"]["uv"]["sources"]["foreign"])
        self.assertEqual(actual["tool"]["uv"]["sources"]["quard-clones"]["rev"],
                         self.installation().plan(root, QUARD)["clone_source"]["rev"])
        self.assertEqual(_policy.read_limits(root)["production"]["max_complexity"], 6)
        self.assertTrue((root / ".quard/config.toml").exists())
        self.assertTrue(all(not (root / ".quard" / name).exists() for name in
                            ("readability.toml", "scope.toml", "checks.toml")))
        self.assertEqual((root / "tach.toml").read_bytes(), original_tach)
        self.assertEqual((root / ".quard/architecture.ready.json").read_bytes(), original_marker)
        tests = load_toml(root / ".quard/pylint-tests.toml")
        self.assertIn("foreign.Framework", tests["tool"]["pylint"]["design"]["ignored-parents"])
        self.assertNotIn("garak.probes.base.Probe", _policy.IGNORED_FRAMEWORK_PARENTS)

    def test_existing_state_wins_without_installation_marker(self):
        root = self.configured_project()
        legacy = root / "quality"
        legacy.mkdir()
        (legacy / "readability.toml").write_text("invalid legacy limits")
        before = (root / ".quard/config.toml").read_bytes()
        entry.initialize_state(root, QUARD)
        self.assertEqual((root / ".quard/config.toml").read_bytes(), before)

    def test_late_failure_restores_all_managed_files_and_modes(self):
        root = self.configured_project()
        setting = root / ".quard/config.toml"
        setting.write_text(setting.read_text().replace("constructor_purity = false",
                                               "constructor_purity = true"))
        for name in ("readability.toml", "scope.toml", "checks.toml"):
            (root / ".quard" / name).write_bytes((QUARD / "defaults" / name).read_bytes())
        original = managed_bytes(root)
        path = root / "pyproject.toml"
        path.chmod(0o640)

        def failing_audit(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            fake_uv(argv, cwd, timeout)
            if str(QUARD / "_audit.py") in argv:
                path.write_text("partially written config")
                write_json(cwd / ".quard/policy-notes.json", {"partial": True})
                return native_result(argv, code=2, stderr="intentional probe failure")
            return native_result(argv)

        with patch.object(entry, "capture", failing_audit), self.assertRaises(QuardError):
            self.installation().install(root, QUARD, progress=False)
        self.assertEqual(managed_bytes(root), original)
        self.assertTrue(_policy.read_checks(root)["constructor_purity"])
        self.assertTrue(all((root / ".quard" / name).exists() for name in
                            ("readability.toml", "scope.toml", "checks.toml")))
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_constructor_opt_in_survives_reinstall_and_invalid_setting_fails(self):
        root = self.configured_project()
        setting = root / ".quard/config.toml"
        setting.write_text(setting.read_text().replace("constructor_purity = false",
                                               "constructor_purity = true"), encoding="utf-8")
        before_setting = setting.read_text(encoding="utf-8")
        with patch.object(entry, "capture", fake_uv):
            self.installation().install(root, QUARD, progress=False)
            self.installation().install(root, QUARD, progress=False)
        self.assertEqual(setting.read_text(encoding="utf-8"), before_setting)
        setting.write_text(before_setting.replace("constructor_purity = true",
                                                  "constructor_purity = 1"), encoding="utf-8")
        before = managed_bytes(root)
        with patch.object(entry, "capture", side_effect=fake_uv) as captured:
            with self.assertRaisesRegex(QuardError, r"\[checks\] expects"):
                self.installation().install(root, QUARD, progress=False)
        captured.assert_not_called()
        self.assertEqual(managed_bytes(root), before)

    def test_missing_constructor_setting_created_only_on_install(self):
        root = self.configured_project()
        setting = root / ".quard/config.toml"
        setting.unlink()
        with patch.object(entry, "capture", fake_uv):
            self.installation().install(root, QUARD, progress=False)
        self.assertFalse(_policy.read_checks(root)["dependency_types"])
        self.assertTrue(_policy.read_checks(root)["tach"])

    def test_migration_keeps_all_settings_and_rollback_restores_old_files(self):
        root = self.configured_project()
        old = root / ".quard"
        config = old / "config.toml"
        config.unlink()
        for name in ("readability.toml", "scope.toml", "checks.toml"):
            (old / name).write_bytes((QUARD / "defaults" / name).read_bytes())
        readability = old / "readability.toml"
        readability.write_text(readability.read_text().replace("max_complexity = 8",
                                                            "max_complexity = 7")
                               .replace("max_module_lines = 800", "max_module_lines = 900"))
        scope = old / "scope.toml"
        scope.write_text(scope.read_text().replace('"generated", "_generated"',
                                                  '"generated", "custom", "_generated"'))
        (old / "checks.toml").write_text("constructor_purity = true\n")
        before = managed_bytes(root)

        def fail(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            return native_result(argv, code=1, stderr="offline failure")

        with patch.object(entry, "capture", fail), self.assertRaises(QuardError):
            self.installation().install(root, QUARD, progress=False)
        self.assertEqual(managed_bytes(root), before)
        self.assertFalse(config.exists())
        with patch.object(entry, "capture", fake_uv):
            self.installation().install(root, QUARD, progress=False)
        self.assertTrue(config.exists())
        self.assertTrue(all(not (old / name).exists() for name in
                            ("readability.toml", "scope.toml", "checks.toml")))
        self.assertEqual(_policy.read_limits(root)["production"]["max_complexity"], 7)
        self.assertEqual(_policy.read_limits(root)["tests"]["max_module_lines"], 900)
        self.assertIn("custom", _policy.read_scope(root)["exclude_directory_names"])
        self.assertTrue(_policy.read_checks(root))
        with patch.object(entry, "capture", fake_uv):
            self.installation().install(root, QUARD, progress=False)
        self.assertTrue(_policy.read_checks(root))

    def test_selected_extras_are_retained_on_reinstall(self):
        root = self.configured_project()
        path = root / "pyproject.toml"
        doc = load_toml(path)
        doc["project"]["optional-dependencies"] = {"keep": ["six==1.17.0"]}
        path.write_text(tomlkit.dumps(doc))
        self.assertEqual(self.installation().plan(root, QUARD, extras=["keep"])["extras"],
                         ["keep"])
        with patch.object(entry, "capture", side_effect=fake_uv) as capture:
            self.installation().install(root, QUARD, extras=["keep"], progress=False)
            self.assertEqual(self.installation().plan(root, QUARD)["extras"], ["keep"])
            self.installation().install(root, QUARD, progress=False)
        commands = [call.args[0] for call in capture.call_args_list]
        for command in commands:
            if command[1] in {"sync", "run"}:
                self.assertEqual(command[command.index("--extra") + 1], "keep")
        self.assertEqual(read_json(root / ".quard/installation.json")["extras"], ["keep"])

    def test_unknown_extra_is_rejected_without_writes(self):
        root = self.configured_project()
        before = managed_bytes(root)
        with self.assertRaisesRegex(QuardError, "Unknown project extra"):
            self.installation().plan(root, QUARD, extras=["missing"])
        with self.assertRaisesRegex(QuardError, "Unknown project extra"):
            self.installation().install(root, QUARD, extras=["missing"], progress=False)
        self.assertEqual(managed_bytes(root), before)

    def test_real_install_separate_clone_and_nonstandard_submodule(self):
        if self._testMethodName.startswith("test_real_install_"):
            self.skipTest("LifecycleTests are always skipped")
        with TemporaryDirectory(prefix="quard lifecycle with spaces ") as tmp:
            base = Path(tmp)
            tool = base / "separate tool clone"
            shutil.copytree(QUARD, tool, ignore=shutil.ignore_patterns(
                ".git", ".venv", "__pycache__", ".quard", ".tach"))
            run_command(["git", "init", "-b", "main"], tool)
            run_command(["git", "add", "."], tool)
            run_command(["git", "commit", "-m", "fixture"], tool)
            project = base / "consumer project"
            (project / "src/sample/domain").mkdir(parents=True)
            (project / "src/sample/__init__.py").write_text(
                'from sample.domain.api import double\n\n__all__ = ["double"]\n')
            (project / "src/sample/domain/__init__.py").write_text("")
            (project / "src/sample/domain/api.py").write_text(
                "def double(value: int) -> int:\n    return value * 2\n")
            (project / "pyproject.toml").write_text(
                '[project]\nname = "sample"\nversion = "0.1.0"\nrequires-python = ">=3.12,<3.13"\n'
                + 'dependencies = []\n[project.optional-dependencies]\nkeep = ["six==1.17.0"]\n'
                + '[tool.uv]\npackage = false\n')
            run_command(["git", "init", "-b", "main"], project)
            (project / ".gitignore").write_text(".venv/\n__pycache__/\n")
            original_tool = managed_bytes(tool)
            before = managed_bytes(project)
            self.assertEqual(self.installation().install(
                project, tool, dry_run=True, extras=["keep"])["status"], "planned")
            plan = run_command(["bash", str(tool / "install.sh"), "--project", str(project),
                                "--dry-run", "--json", "--extra", "keep"], base)
            self.assertEqual(json.loads(plan)["status"], "planned")
            self.assertEqual(managed_bytes(project), before)
            installed = run_command(["bash", str(tool / "install.sh"), "--project", str(project),
                                     "--json", "--extra", "keep"], base)
            self.assertEqual(len(json.loads(installed)["smoke_tests"]), 6)
            uv = ["uv", "run", "--locked", "--group", "dev", "--extra", "keep", "python"]
            missing = run_command(uv + [str(tool / "check.py"), "--json"], project, expected=2)
            self.assertIn("ARCHITECTURE_NOT_INITIALIZED",
                          [d["rule"] for d in json.loads(missing)["diagnostics"]])
            run_command(uv + [str(tool / "init_architecture.py"), "--json"], project)
            run_command(uv + [str(tool / "init_architecture.py"), "--accept", "--json"], project)
            report = json.loads(run_command(uv + [str(tool / "check.py"), "--json"], project))
            self.assertEqual(len(report["tools"]), 15)
            self.assertEqual(report["schema_version"], "quard-report/v1")
            self.assertEqual(len(report["quard"]["commit"]), 40)
            self.assertFalse(report["quard"]["dirty"])
            before = managed_bytes(project)
            reinstalled = run_command(
                ["bash", str(tool / "install.sh"), "--project", str(project), "--json"], base)
            after = managed_bytes(project)
            differences = "\n".join("\n".join(difflib.unified_diff(
                before[name].decode().splitlines(), after[name].decode().splitlines(),
                fromfile=name, tofile=name))
                for name in before if before[name] != after.get(name))
            self.assertEqual(json.loads(reinstalled)["modified_files"], [], differences)
            self.assertEqual(managed_bytes(project), before)
            run_command(uv + ["-c", "import six; assert six.__version__ == '1.17.0'"], project)
            self.assertEqual(managed_bytes(tool), original_tool)
            self.assertEqual(run_command(["git", "status", "--porcelain", "--ignored"], tool), "")
            relative = "tools with spaces/analyzer"
            run_command(["git", "-c", "protocol.file.allow=always", "submodule", "add",
                         str(tool), relative], project)
            submodule = project / relative
            run_command(uv + [str(submodule / "apply.py"), "--project", str(project), "--json"],
                        project)
            run_command(uv + [str(submodule / "init_architecture.py"), "--accept", "--json"],
                        project)
            run_command(["bash", str(submodule / "install.sh"), "--project", str(project),
                         "--json"], base)
            run_command(uv + [str(submodule / "check.py"), "--json"], project)
            self.assertEqual(run_command(["git", "status", "--porcelain", "--ignored"],
                                         submodule), "")
            config_path = project / "pyrightconfig.json"
            config = json.loads(config_path.read_text())
            config["notAnQuardSetting"] = True
            config_path.write_text(json.dumps(config))
            before = managed_bytes(project)
            run_command(["bash", str(submodule / "install.sh"), "--project", str(project),
                         "--json"], base, expected=2)
            self.assertEqual(managed_bytes(project), before)
            error = json.loads((project / ".quard/logs/install-error.json").read_text())
            self.assertTrue(error["managed_files_rolled_back"])
            config.pop("notAnQuardSetting")
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
            with (submodule / "check.py").open("a") as stream:
                stream.write("\n# local regression fixture\n")
            dirty = json.loads(run_command(uv + [str(submodule / "check.py"), "--json"], project))
            self.assertTrue(dirty["quard"]["dirty"])
