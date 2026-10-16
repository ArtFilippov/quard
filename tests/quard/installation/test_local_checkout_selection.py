"""A local development source cannot replace a selected consumer's Git pin."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "check_runner"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from rust_clone_engine_utils import adoption_consumer

ROOT = Path(__file__).resolve().parents[3]


class TestLocalCheckoutSelection(unittest.TestCase):
    def test_public_submodule_keeps_its_pinned_git_source(self) -> None:
        with TemporaryDirectory(prefix="quard-pinned-consumer-", dir="/tmp/opencode") as temp:
            base = Path(temp)
            origin = base / "pinned origin"
            consumer = base / "public consumer"
            consumer.mkdir()
            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["GIT_ALLOW_PROTOCOL"] = "file"

            def command(argv: list[str], cwd: Path, *, expected: int = 0) -> str:
                result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                        text=True, timeout=1200, check=False)
                self.assertEqual(result.returncode, expected,
                                 f"{argv}: {result.stdout}\n{result.stderr}")
                return result.stdout.strip()

            command(["git", "clone", "--shared", str(ROOT), str(origin)], base)
            revision = command(["git", "rev-parse", "HEAD"], origin)
            adoption_consumer(consumer)
            # The pinned older checkout cannot parse switches introduced after its revision.
            setting = consumer / ".quard/config.toml"
            setting.write_text(setting.read_text(encoding="utf-8").replace(
                "cached_property_migration = true\n", "").replace(
                "tuple_signatures = false\n", "").replace(
                "stairfmt = true\n", ""), encoding="utf-8")
            command(["git", "init", "-b", "main"], consumer)
            command(["git", "submodule", "add", origin.as_uri(), "tools/analyzer"], consumer)
            tool = consumer / "tools/analyzer"
            self.assertEqual(command(["git", "status", "--porcelain"], tool), "")

            installed = json.loads(command([sys.executable, str(tool / "install.py"),
                                            "--project", str(consumer), "--json"], consumer))
            self.assertEqual(installed["exit_code"], 0)
            project = tomllib.loads((consumer / "pyproject.toml").read_text(encoding="utf-8"))
            source = project["tool"]["uv"]["sources"]["quard-clones"]
            self.assertEqual(source["rev"], revision)
            self.assertEqual(source["subdirectory"], "rust/quard-clones")
            self.assertEqual(source["git"], origin.as_uri())
            locked = tomllib.loads((consumer / "uv.lock").read_text(encoding="utf-8"))
            component = next(item for item in locked["package"] if item["name"] == "quard-clones")
            self.assertIn(revision, str(component["source"]))
            self.assertNotIn("directory", component["source"])
            command(["uv", "sync", "--locked", "--group", "dev"], consumer)
            command(["git", "add", "-A"], consumer)
            baseline = command(["git", "status", "--porcelain"], consumer)
            python = str(consumer / ".venv/bin/python")
            applied = json.loads(command([python, str(tool / "apply.py"),
                                          "--project", str(consumer), "--json"], consumer))
            self.assertEqual(applied["exit_code"], 0)
            checked = json.loads(command([python, str(tool / "check.py"),
                                          "--project", str(consumer), "--json"], consumer))
            report = json.loads((consumer / ".quard/reports/latest.json").read_text(
                encoding="utf-8"))
            self.assertEqual((checked["exit_code"], report["exit_code"]), (0, 0))
            self.assertEqual(report["summary"]["infrastructure_errors"], 0)
            for kind, name in (("exact", "jscpd"), ("renamed", "jscpd-renamed"),
                               ("similar", "jscpd-similar")):
                job = next(item for item in report["tools"] if item["name"] == name)
                self.assertIn((job["status"], job["exit_code"]),
                              {("passed", 0), ("skipped", None)})
                self.assertIn("_clone_engine_worker.py", job["argv"][1])
                native = json.loads((consumer / job["raw_output"]["native_json"]).read_text(
                    encoding="utf-8"))
                self.assertEqual(native["pass"], kind)
                self.assertEqual(native["engine"]["quard-clones"], "0.1.0")
                self.assertEqual(native["minimumUnits"], 3)
            env["PYTHONPATH"] = str(consumer / "src")
            command([python, "-B", "-m", "unittest", "discover", "-s",
                     "tests/demo/records", "-v"],
                    consumer)
            self.assertEqual(command(["git", "status", "--porcelain"], tool), "")
            self.assertEqual(command(["git", "status", "--porcelain"], consumer), baseline)


if __name__ == "__main__":
    unittest.main()
