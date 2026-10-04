"""Installation, migration and lifecycle through the public transaction contract."""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

import tomlkit

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))
sys.path.insert(0, str(ERYX / "tests"))

from eryx import install as entry
from eryx import _policy
from eryx._audit import smoke_tests, tool_binary
from eryx._common import (EryxError, load_toml, read_json, repo_lock, safe_target,
                          write_json)
from eryx.installation.installation import Installation
from eryx.installation.transactional_installation import TransactionalInstallation
from offline_fixtures import CATALOG, create_repo, managed_bytes, native_result


def fake_uv(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    if str(ERYX / "_audit.py") in argv:
        _policy.configure(cwd, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
        write_json(cwd / ".eryx/logs/install-probes/result.json",
                   {"versions": {}, "smoke_tests": {"native": "mocked"}})
    return native_result(argv)


def run_command(argv: list[str], cwd: Path, expected: int = 0) -> str:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "UV_NO_PROGRESS": "1",
                "GIT_AUTHOR_NAME": "Eryx test", "GIT_AUTHOR_EMAIL": "test@example.org",
                "GIT_COMMITTER_NAME": "Eryx test", "GIT_COMMITTER_EMAIL": "test@example.org"})
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                            timeout=240, check=False)
    if result.returncode != expected:
        root = Path(argv[argv.index("--project") + 1]) if "--project" in argv else cwd
        details = "\n".join(f"{path.relative_to(root)}:\n{path.read_text()[-8000:]}"
                            for path in (root / ".eryx/logs").rglob("*.log"))
        raise AssertionError(
            f"{argv}\nexit={result.returncode}\n{result.stdout}\n{result.stderr}\n{details}")
    return result.stdout


class TestTransactionalInstallation(unittest.TestCase):
    def test_real_native_installer_smoke_probes(self):
        with TemporaryDirectory(prefix="eryx-native-probes-") as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_bytes((ERYX / "pyproject.toml").read_bytes())
            logs = root / ".eryx/logs/install-probes"
            logs.mkdir(parents=True)
            result = smoke_tests(root, logs)
            self.assertEqual(result["pylint_config_and_fixture"], "passed")
            self.assertEqual(result["pyright_type_error"], "detected")
            self.assertEqual(result["pydoclint_signature_error"], "detected")
            self.assertEqual(result["tach_undeclared_dependency"], "detected")

    def test_installer_probe_resolves_native_binary_in_project_environment(self):
        self.assertEqual(tool_binary("pydoclint"),
                         str(Path(sys.executable).parent / "pydoclint"))
        with self.assertRaises(EryxError) as error:
            tool_binary("nonexistent-eryx-tool")
        self.assertEqual(error.exception.code, "MISSING_TOOL")

    def test_repo_lock_refuses_concurrent_operations(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with repo_lock(root):
                with self.assertRaises(EryxError):
                    with repo_lock(root):
                        pass

    def test_safe_target_rejects_path_traversal(self):
        root = Path("/tmp/example")
        with self.assertRaises(EryxError):
            safe_target(root / "../escape", root)

    def configured_project(self) -> Path:
        root = Path(self.enterContext(TemporaryDirectory(prefix="eryx-migration-")))
        create_repo(root)
        return root

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
        with TemporaryDirectory(prefix="eryx-installation-") as directory:
            root = self.project(directory)
            before = (root / "pyproject.toml").read_bytes()
            installation = self.installation()
            plan = installation.plan(root, ERYX)
            result = installation.install(root, ERYX, dry_run=True)
            self.assertEqual((result["status"], result["exit_code"]), ("planned", 0))
            self.assertEqual({key: result[key] for key in plan}, plan)
            self.assertEqual((root / "pyproject.toml").read_bytes(), before)
            self.assertFalse((root / ".eryx").exists())

    def test_failed_tool_step_restores_managed_files_and_logs_failure(self):
        def fail(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            return {"argv": argv, "stdout": "", "stderr": "offline failure",
                    "exit_code": 1, "error": None}

        with TemporaryDirectory(prefix="eryx-installation-") as directory:
            root = self.project(directory)
            before = (root / "pyproject.toml").read_bytes()
            with patch.object(entry, "capture", fail):
                with self.assertRaises(EryxError):
                    self.installation().install(root, ERYX, progress=False)
            self.assertEqual((root / "pyproject.toml").read_bytes(), before)
            self.assertTrue(read_json(root / ".eryx/logs/install-error.json")
                            ["managed_files_rolled_back"])

    def test_plan_does_not_change_configured_consumer(self):
        root = self.configured_project()
        before = managed_bytes(root)
        result = self.installation().install(root, ERYX, dry_run=True, progress=False)
        self.assertEqual(result["status"], "planned")
        self.assertIn("pyright[nodejs]==1.1.414", result["dependencies"])
        self.assertEqual(managed_bytes(root), before)

    def test_rollback_on_uv_failure_leaves_failure_log(self):
        root = self.configured_project()
        (root / ".eryx/checks.toml").unlink()
        before = managed_bytes(root)

        def fail(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            return native_result(argv, stderr="intentional offline test failure", code=1)

        with patch.object(entry, "capture", fail), self.assertRaises(EryxError):
            self.installation().install(root, ERYX, progress=False)
        self.assertEqual(managed_bytes(root), before)
        self.assertFalse((root / ".eryx/checks.toml").exists())
        self.assertTrue(read_json(root / ".eryx/logs/install-error.json")
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
            self.installation().install(root, ERYX, progress=False)
            first = managed_bytes(root)
            self.installation().install(root, ERYX, progress=False)
            self.assertEqual(managed_bytes(root), first)

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
        limits = root / ".eryx/readability.toml"
        limits.write_text(limits.read_text().replace("max_complexity = 8", "max_complexity = 6"))
        _policy.configure(root)
        original_tach = (root / "tach.toml").read_bytes()
        original_marker = (root / ".eryx/architecture.ready.json").read_bytes()
        (root / ".eryx").rename(root / "quality")
        with patch.object(entry, "capture", fake_uv):
            self.installation().install(root, ERYX, progress=False)
        actual = load_toml(path)
        self.assertIn("foreign.Framework", actual["tool"]["pylint"]["design"]["ignored-parents"])
        self.assertEqual(actual["tool"]["pyright"]["stubPath"], "src/stubs")
        for key in ("sources", "index", "dependency-metadata"):
            self.assertEqual(actual["tool"]["uv"][key], document["tool"]["uv"][key])
        self.assertEqual(_policy.read_limits(root)["production"]["max_complexity"], 6)
        self.assertEqual((root / "tach.toml").read_bytes(), original_tach)
        self.assertEqual((root / ".eryx/architecture.ready.json").read_bytes(), original_marker)
        tests = load_toml(root / ".eryx/pylint-tests.toml")
        self.assertIn("foreign.Framework", tests["tool"]["pylint"]["design"]["ignored-parents"])
        self.assertNotIn("garak.probes.base.Probe", _policy.IGNORED_FRAMEWORK_PARENTS)

    def test_existing_state_wins_without_installation_marker(self):
        root = self.configured_project()
        legacy = root / "quality"
        legacy.mkdir()
        (legacy / "readability.toml").write_text("invalid legacy limits")
        before = (root / ".eryx/readability.toml").read_bytes()
        entry.initialize_state(root, ERYX)
        self.assertEqual((root / ".eryx/readability.toml").read_bytes(), before)

    def test_late_failure_restores_all_managed_files_and_modes(self):
        root = self.configured_project()
        (root / ".eryx/checks.toml").unlink()
        original = managed_bytes(root)
        path = root / "pyproject.toml"
        path.chmod(0o640)

        def failing_audit(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            fake_uv(argv, cwd, timeout)
            if str(ERYX / "_audit.py") in argv:
                path.write_text("partially written config")
                write_json(cwd / ".eryx/policy-notes.json", {"partial": True})
                return native_result(argv, code=2, stderr="intentional probe failure")
            return native_result(argv)

        with patch.object(entry, "capture", failing_audit), self.assertRaises(EryxError):
            self.installation().install(root, ERYX, progress=False)
        self.assertEqual(managed_bytes(root), original)
        self.assertFalse((root / ".eryx/checks.toml").exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_constructor_opt_in_survives_reinstall_and_invalid_setting_fails(self):
        root = self.configured_project()
        setting = root / ".eryx/checks.toml"
        setting.write_text("constructor_purity = true\n", encoding="utf-8")
        with patch.object(entry, "capture", fake_uv):
            self.installation().install(root, ERYX, progress=False)
            self.installation().install(root, ERYX, progress=False)
        self.assertEqual(setting.read_text(encoding="utf-8"), "constructor_purity = true\n")
        setting.write_text("constructor_purity = 1\n", encoding="utf-8")
        before = managed_bytes(root)
        with patch.object(entry, "capture", side_effect=fake_uv) as captured:
            with self.assertRaisesRegex(EryxError, "checks.toml expects"):
                self.installation().install(root, ERYX, progress=False)
        captured.assert_not_called()
        self.assertEqual(managed_bytes(root), before)

    def test_missing_constructor_setting_created_only_on_install(self):
        root = self.configured_project()
        setting = root / ".eryx/checks.toml"
        setting.unlink()
        with patch.object(entry, "capture", fake_uv):
            self.installation().install(root, ERYX, progress=False)
        self.assertEqual(setting.read_text(encoding="utf-8"),
                         (ERYX / "defaults/checks.toml").read_text(encoding="utf-8"))

    def test_selected_extras_are_retained_on_reinstall(self):
        root = self.configured_project()
        path = root / "pyproject.toml"
        doc = load_toml(path)
        doc["project"]["optional-dependencies"] = {"keep": ["six==1.17.0"]}
        path.write_text(tomlkit.dumps(doc))
        self.assertEqual(self.installation().plan(root, ERYX, extras=["keep"])["extras"],
                         ["keep"])
        with patch.object(entry, "capture", side_effect=fake_uv) as capture:
            self.installation().install(root, ERYX, extras=["keep"], progress=False)
            self.assertEqual(self.installation().plan(root, ERYX)["extras"], ["keep"])
            self.installation().install(root, ERYX, progress=False)
        commands = [call.args[0] for call in capture.call_args_list]
        for command in commands:
            if command[1] in {"sync", "run"}:
                self.assertEqual(command[command.index("--extra") + 1], "keep")
        self.assertEqual(read_json(root / ".eryx/installation.json")["extras"], ["keep"])

    def test_unknown_extra_is_rejected_without_writes(self):
        root = self.configured_project()
        before = managed_bytes(root)
        with self.assertRaisesRegex(EryxError, "Unknown project extra"):
            self.installation().plan(root, ERYX, extras=["missing"])
        with self.assertRaisesRegex(EryxError, "Unknown project extra"):
            self.installation().install(root, ERYX, extras=["missing"], progress=False)
        self.assertEqual(managed_bytes(root), before)

    def test_real_install_separate_clone_and_nonstandard_submodule(self):
        if self._testMethodName.startswith("test_real_install_"):
            self.skipTest("LifecycleTests are always skipped")
        with TemporaryDirectory(prefix="eryx lifecycle with spaces ") as tmp:
            base = Path(tmp)
            tool = base / "separate tool clone"
            shutil.copytree(ERYX, tool, ignore=shutil.ignore_patterns(
                ".git", ".venv", "__pycache__", ".eryx", ".tach"))
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
            self.assertEqual(report["schema_version"], "eryx-report/v1")
            self.assertEqual(len(report["eryx"]["commit"]), 40)
            self.assertFalse(report["eryx"]["dirty"])
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
            config["notAnEryxSetting"] = True
            config_path.write_text(json.dumps(config))
            before = managed_bytes(project)
            run_command(["bash", str(submodule / "install.sh"), "--project", str(project),
                         "--json"], base, expected=2)
            self.assertEqual(managed_bytes(project), before)
            error = json.loads((project / ".eryx/logs/install-error.json").read_text())
            self.assertTrue(error["managed_files_rolled_back"])
            config.pop("notAnEryxSetting")
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
            with (submodule / "check.py").open("a") as stream:
                stream.write("\n# local regression fixture\n")
            dirty = json.loads(run_command(uv + [str(submodule / "check.py"), "--json"], project))
            self.assertTrue(dirty["eryx"]["dirty"])
