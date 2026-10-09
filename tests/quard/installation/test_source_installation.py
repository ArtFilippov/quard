"""Real uv source-build lifecycle on an isolated public consumer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
import unittest

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "check_runner"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from installation_fixture_utils import run_command
from offline_fixtures import managed_bytes
from rust_clone_engine_utils import adoption_consumer
from quard._common import QuardError
from quard.install_utils import clone_source

QUARD = Path(__file__).resolve().parents[3]


class TestSourceInstallation(unittest.TestCase):
    def test_real_uv_source_install_reinstall_locked_sync_and_submodule(self) -> None:
        cargo = shutil.which("cargo") or "/tmp/opencode/cargo/bin/cargo"
        self.assertTrue(Path(cargo).is_file(), "Rust is required for source installation")
        with TemporaryDirectory(prefix="quard-source-", dir="/tmp/opencode") as tmp:
            base = Path(tmp)
            tool = base / "separate tool"
            shutil.copytree(QUARD, tool, ignore=shutil.ignore_patterns(
                ".git", ".venv", ".quard", ".tach", "tach.toml", "__pycache__", "target"))
            run_command(["git", "init", "-b", "main"], tool)
            run_command(["git", "add", "."], tool)
            run_command(["git", "commit", "-m", "pinned fixture"], tool)
            run_command(["git", "remote", "add", "origin", tool.as_uri()], tool)
            revision = run_command(["git", "rev-parse", "HEAD"], tool).strip()
            original = managed_bytes(tool)
            consumer = base / "public consumer"
            consumer.mkdir()
            adoption_consumer(consumer)
            project = consumer / "pyproject.toml"
            doc = tomlkit.parse(project.read_text(encoding="utf-8"))
            doc["project"]["requires-python"] = ">=3.12,<3.13"
            doc["project"]["optional-dependencies"] = {"keep": ["six==1.17.0"]}
            uv = doc.setdefault("tool", {}).setdefault("uv", {})
            uv["index"] = [{"name": "private", "url": "https://example.org/simple",
                            "explicit": True}]
            uv["sources"] = {"unrelated": {"git": "https://example.org/other"}}
            project.write_text(tomlkit.dumps(doc), encoding="utf-8")
            env = os.environ.copy()
            env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
            if cargo.startswith("/tmp/opencode/"):
                env["CARGO_HOME"] = "/tmp/opencode/cargo"
                env["RUSTUP_HOME"] = "/tmp/opencode/rustup"
            env["CARGO_TARGET_DIR"] = "/tmp/opencode/quard-clones-target"
            env["CARGO_BUILD_JOBS"] = "2"
            env["PYTHONDONTWRITEBYTECODE"] = "1"

            def invoke(script: Path, *args: str) -> dict:
                interpreter = (consumer / ".venv/bin/python" if script.name == "check.py"
                               else Path(sys.executable))
                result = subprocess.run([str(interpreter), str(script), "--project", str(consumer),
                                         "--json", *args], cwd=base, env=env,
                                        capture_output=True, text=True, timeout=1200, check=False)
                log = consumer / ".quard/logs/install.log"
                details = log.read_text(encoding="utf-8")[-4000:] if log.exists() else ""
                self.assertEqual(result.returncode, 0,
                                 result.stderr + "\n" + result.stdout + "\n" + details)
                return json.loads(result.stdout)

            first = invoke(tool / "install.py", "--extra", "keep")
            self.assertEqual(first["smoke_tests"]["clone_wheel_similar"], "detected")
            self.assertEqual(first["versions"]["cpd-core"], "0.1.20")
            lock = (consumer / "uv.lock").read_text(encoding="utf-8")
            self.assertIn(revision, lock)
            package = next(package for package in tomllib.loads(lock)["package"]
                           if package["name"] == "quard-clones")
            self.assertIn(revision, str(package["source"]))
            self.assertEqual(tomlkit.parse(project.read_text())["tool"]["uv"]["sources"][
                "quard-clones"]["subdirectory"], "rust/quard-clones")
            self.assertEqual(tomlkit.parse(project.read_text())["tool"]["uv"]["index"],
                             doc["tool"]["uv"]["index"])
            self.assertEqual(tomlkit.parse(project.read_text())["tool"]["uv"]["sources"][
                "unrelated"], doc["tool"]["uv"]["sources"]["unrelated"])
            self.assertEqual(invoke(tool / "install.py")["exit_code"], 0)
            synced = subprocess.run(["uv", "sync", "--locked", "--group", "dev", "--extra", "keep"],
                                    cwd=consumer, env=env, capture_output=True, text=True,
                                    timeout=1200, check=False)
            self.assertEqual(synced.returncode, 0, synced.stderr)
            self.assertEqual(invoke(tool / "check.py")["exit_code"], 0)
            self.assertEqual(managed_bytes(tool), original)
            self.assertEqual(run_command(["git", "status", "--porcelain", "--ignored"], tool), "")
            run_command(["git", "init", "-b", "main"], consumer)
            run_command(["git", "-c", "protocol.file.allow=always", "submodule", "add",
                         str(tool), "tools/clone engine"], consumer)
            nested = consumer / "tools/clone engine"
            self.assertEqual(invoke(nested / "install.py")["exit_code"], 0)
            self.assertEqual(invoke(nested / "check.py")["exit_code"], 0)
            self.assertEqual(run_command(["git", "status", "--porcelain", "--ignored"], nested), "")
            run_command(["git", "update-index", "--cacheinfo",
                         "160000," + "1" * 40 + ",tools/clone engine"], consumer)
            with self.assertRaises(QuardError) as mismatch:
                clone_source(consumer, nested)
            self.assertEqual(mismatch.exception.code, "CLONE_GITLINK_MISMATCH")
            (nested / "rust/quard-clones/smoke.py").write_text(
                "# Uncommitted source\n", encoding="utf-8")
            with self.assertRaises(QuardError) as dirty:
                clone_source(consumer, nested)
            self.assertEqual(dirty.exception.code, "CLONE_SOURCE_DIRTY")
