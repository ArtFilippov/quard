"""Real uv installation and native analyzer acceptance, independent of any consumer."""
from __future__ import annotations

import json
import difflib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import tomlkit

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX))
from test_migration import managed_bytes


@unittest.skipUnless(os.environ.get("ERYX_RUN_LIFECYCLE") == "1",
                     "Real installation tests are opt-in: set ERYX_RUN_LIFECYCLE=1")
class LifecycleTests(unittest.TestCase):
    def run_command(self, argv, cwd, expected=0):
        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env.pop("UV_PROJECT_ENVIRONMENT", None)
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "UV_NO_PROGRESS": "1",
                    "GIT_AUTHOR_NAME": "Eryx test", "GIT_AUTHOR_EMAIL": "test@example.org",
                    "GIT_COMMITTER_NAME": "Eryx test", "GIT_COMMITTER_EMAIL": "test@example.org"})
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=240)
        details = ""
        if result.returncode != expected:
            root = Path(argv[argv.index("--project") + 1]) if "--project" in argv else cwd
            details = "\n".join(f"{path.relative_to(root)}:\n{path.read_text()[-8000:]}"
                                for path in (root / ".eryx/logs").rglob("*.log"))
        self.assertEqual(result.returncode, expected,
                         f"{argv}\nexit={result.returncode}\n{result.stdout}\n{result.stderr}\n{details}")
        return result.stdout

    def test_real_install_separate_clone_and_nonstandard_submodule(self):
        with tempfile.TemporaryDirectory(prefix="eryx lifecycle with spaces ") as tmp:
            base = Path(tmp)
            tool = base / "separate tool clone"
            shutil.copytree(ERYX, tool, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".eryx", ".tach"))
            self.run_command(["git", "init", "-b", "main"], tool)
            self.run_command(["git", "add", "."], tool)
            self.run_command(["git", "commit", "-m", "fixture"], tool)
            project = base / "consumer project"
            (project / "src/sample/domain").mkdir(parents=True)
            (project / "src/sample/__init__.py").write_text('from .domain.api import double\n\n__all__ = ["double"]\n')
            (project / "src/sample/domain/__init__.py").write_text("")
            (project / "src/sample/domain/api.py").write_text("def double(value: int) -> int:\n    return value * 2\n")
            (project / "pyproject.toml").write_text(
                '[project]\nname = "sample"\nversion = "0.1.0"\nrequires-python = ">=3.12,<3.13"\n'
                'dependencies = []\n[project.optional-dependencies]\nkeep = ["six==1.17.0"]\n'
                '[tool.uv]\npackage = false\n')
            (project / ".eryx").mkdir()
            (project / ".eryx/checks.toml").write_text(
                'groups = ["codestyle", "anonymity"]\nworking_directories = ["src"]\n')
            self.run_command(["git", "init", "-b", "main"], project)
            (project / ".gitignore").write_text(".venv/\n__pycache__/\n")
            original_tool = managed_bytes(tool)
            before = managed_bytes(project)
            plan = self.run_command(["bash", str(tool / "install.sh"), "--project", str(project),
                                     "--dry-run", "--json", "--extra", "keep"], base)
            self.assertEqual(json.loads(plan)["status"], "planned")
            self.assertEqual(managed_bytes(project), before)
            installed = self.run_command(["bash", str(tool / "install.sh"), "--project", str(project),
                                          "--json", "--extra", "keep"], base)
            self.assertEqual(json.loads(installed)["smoke_tests"]["pylint_notebook"], "detected")
            self.assertEqual(json.loads(installed)["smoke_tests"]["pylint_secret"], "detected")
            self.assertEqual(json.loads(installed)["smoke_tests"]["presidio_person"], "detected")
            uv = ["uv", "run", "--locked", "--group", "dev", "--extra", "keep", "python"]
            missing = self.run_command(uv + [str(tool / "check.py"), "--json"], project, expected=2)
            self.assertIn("ARCHITECTURE_NOT_INITIALIZED", [d["rule"] for d in json.loads(missing)["diagnostics"]])
            self.run_command(uv + [str(tool / "init_architecture.py"), "--json"], project)
            self.run_command(uv + [str(tool / "init_architecture.py"), "--accept", "--json"], project)
            report = json.loads(self.run_command(uv + [str(tool / "check.py"), "--json"], project))
            self.assertEqual(len(report["tools"]), 6)
            self.assertEqual(report["schema_version"], "eryx-report/v1")
            self.assertEqual(len(report["eryx"]["commit"]), 40)
            self.assertFalse(report["eryx"]["dirty"])
            before = managed_bytes(project)
            reinstalled = self.run_command(["bash", str(tool / "install.sh"), "--project", str(project), "--json"], base)
            after = managed_bytes(project)
            differences = "\n".join("\n".join(difflib.unified_diff(
                before[name].decode().splitlines(), after[name].decode().splitlines(), fromfile=name, tofile=name))
                for name in before if before[name] != after.get(name))
            self.assertEqual(json.loads(reinstalled)["modified_files"], [], differences)
            self.assertEqual(managed_bytes(project), before)
            self.run_command(uv + ["-c", "import six; assert six.__version__ == '1.17.0'"], project)
            self.assertEqual(managed_bytes(tool), original_tool)
            self.assertEqual(self.run_command(["git", "status", "--porcelain", "--ignored"], tool), "")
            # A real gitlink at an arbitrary path, without network or access to tai.
            relative = "tools with spaces/analyzer"
            self.run_command(["git", "-c", "protocol.file.allow=always", "submodule", "add", str(tool), relative], project)
            submodule = project / relative
            self.run_command(uv + [str(submodule / "apply.py"), "--project", str(project), "--json"], project)
            self.run_command(uv + [str(submodule / "init_architecture.py"), "--accept", "--json"], project)
            self.run_command(["bash", str(submodule / "install.sh"), "--project", str(project), "--json"], base)
            self.run_command(uv + [str(submodule / "check.py"), "--json"], project)
            self.assertEqual(self.run_command(["git", "status", "--porcelain", "--ignored"], submodule), "")
            # Late *real* native probe failure restores managed files.
            config_path = project / "pyrightconfig.json"
            config = json.loads(config_path.read_text())
            config["notAnEryxSetting"] = True
            config_path.write_text(json.dumps(config))
            before = managed_bytes(project)
            self.run_command(["bash", str(submodule / "install.sh"), "--project", str(project), "--json"], base, expected=2)
            self.assertEqual(managed_bytes(project), before)
            error = json.loads((project / ".eryx/logs/install-error.json").read_text())
            self.assertTrue(error["managed_files_rolled_back"])
            config.pop("notAnEryxSetting")
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
            # Dirty-source reporting comes from the actual tool repository.
            with (submodule / "check.py").open("a") as stream:
                stream.write("\n# local regression fixture\n")
            dirty = json.loads(self.run_command(uv + [str(submodule / "check.py"), "--json"], project))
            self.assertTrue(dirty["eryx"]["dirty"])
            # Installation must test the effective combined-group configuration.
            config_path = project / "pyproject.toml"
            config = tomlkit.parse(config_path.read_text(encoding="utf-8"))
            config["tool"]["pylint"]["messages_control"]["disable"].append("potential-secret")
            config_path.write_text(tomlkit.dumps(config), encoding="utf-8")
            failed = json.loads(self.run_command(
                ["bash", str(submodule / "install.sh"), "--project", str(project), "--json"],
                base, expected=2))
            self.assertEqual(failed["code"], "INSTALL_STEP_FAILED")
            self.assertIn("PYLINT_SECRET_SMOKE_FAILED", (project / ".eryx/logs/install.log").read_text())
            self.assertEqual(json.loads((project / ".eryx/logs/install-error.json").read_text())
                             ["managed_files_rolled_back"], True)

    def test_anonymity_only_install_and_check_without_src_or_architecture(self):
        with tempfile.TemporaryDirectory(prefix="eryx-anonymity-install-") as tmp:
            project = Path(tmp)
            (project / "review").mkdir()
            (project / "review/notes.txt").write_text("No notebook", encoding="utf-8")
            (project / "pyproject.toml").write_text(
                '[project]\nname = "anonymity-fixture"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12,<3.13"\ndependencies = []\n'
                '[tool.uv]\npackage = false\n', encoding="utf-8")
            (project / ".eryx").mkdir()
            (project / ".eryx/checks.toml").write_text(
                'groups = ["anonymity"]\nworking_directories = ["review"]\n', encoding="utf-8")
            installed = json.loads(self.run_command(["bash", str(ERYX / "install.sh"),
                                                      "--project", str(project), "--json"], project))
            self.assertEqual(installed["smoke_tests"]["pylint_notebook"], "detected")
            self.assertEqual(installed["smoke_tests"]["pylint_secret"], "detected")
            self.assertEqual(installed["smoke_tests"]["presidio_person"], "detected")
            self.assertEqual(installed["smoke_tests"]["presidio_email"], "detected")
            self.assertEqual(installed["smoke_tests"]["presidio_russian"], "detected")
            self.assertEqual(set(installed["versions"]), {"pylint", "nbstripout", "detect-secrets", "tomlkit", "packaging",
                                                           "presidio-analyzer", "en-core-web-sm", "ru-core-news-sm"})
            self.assertFalse(installed["architecture_initialization_required"])
            self.assertFalse((project / "pyrightconfig.json").exists())
            self.assertFalse((project / "tach.toml").exists())
            self.assertFalse((project / ".eryx/readability.toml").exists())
            uv = ["uv", "run", "--locked", "--group", "dev", "python", str(ERYX / "check.py"), "--json"]
            clean = json.loads(self.run_command(uv, project))
            self.assertEqual([tool["name"] for tool in clean["tools"]], ["presidio", "pylint-anonymity"])
            self.assertEqual(clean["coverage"]["notebook_count"], 0)
            self.assertEqual(clean["coverage"]["secret_scanned"], ["review/notes.txt"])
            self.assertEqual(clean["coverage"]["presidio_scanned"], ["review/notes.txt"])
            (project / "outside").mkdir()
            (project / "outside/ignored.ipynb").write_text("invalid notebook", encoding="utf-8")
            notebook = project / "review/dirty.ipynb"
            notebook.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {},
                                            "cells": [{"cell_type": "code", "id": "fixture", "metadata": {},
                                                       "source": "print(1)", "execution_count": 1,
                                                       "outputs": [{"output_type": "stream", "name": "stdout",
                                                                    "text": "1\n"}]}]}), encoding="utf-8")
            before = notebook.read_bytes()
            report = json.loads(self.run_command(uv, project, expected=1))
            self.assertEqual([(d["rule"], d["path"]) for d in report["diagnostics"]],
                             [("W9801", "review/dirty.ipynb")])
            self.assertEqual(notebook.read_bytes(), before)
