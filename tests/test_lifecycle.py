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

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX))
from test_migration import managed_bytes


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
            (project / "src/sample/__init__.py").write_text('from sample.domain.api import double\n\n__all__ = ["double"]\n')
            (project / "src/sample/domain/__init__.py").write_text("")
            (project / "src/sample/domain/api.py").write_text("def double(value: int) -> int:\n    return value * 2\n")
            (project / "pyproject.toml").write_text(
                '[project]\nname = "sample"\nversion = "0.1.0"\nrequires-python = ">=3.12,<3.13"\n'
                'dependencies = []\n[project.optional-dependencies]\nkeep = ["six==1.17.0"]\n'
                '[tool.uv]\npackage = false\n')
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
            uv = ["uv", "run", "--locked", "--group", "dev", "--extra", "keep", "python"]
            missing = self.run_command(uv + [str(tool / "check.py"), "--json"], project, expected=2)
            self.assertIn("ARCHITECTURE_NOT_INITIALIZED", [d["rule"] for d in json.loads(missing)["diagnostics"]])
            self.run_command(uv + [str(tool / "init_architecture.py"), "--json"], project)
            self.run_command(uv + [str(tool / "init_architecture.py"), "--accept", "--json"], project)
            report = json.loads(self.run_command(uv + [str(tool / "check.py"), "--json"], project))
            self.assertEqual(len(report["tools"]), 5)
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
