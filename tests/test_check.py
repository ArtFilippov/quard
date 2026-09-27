"""Regression tests for native Pylint worker execution."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX))

from check import execute_job, make_jobs
from _policy import discover_notebooks


class PylintOutputTest(unittest.TestCase):
    def run_pylint(self, code, enabled):
        with TemporaryDirectory(prefix="eryx-pylint-") as directory:
            root = Path(directory)
            source = root / "src" / "sample.py"
            source.parent.mkdir()
            source.write_text(code, encoding="utf-8")
            (root / "pyproject.toml").write_text(
                '[tool.pylint.messages_control]\n'
                'disable = ["all"]\n'
                f'enable = {json.dumps(enabled)}\n',
                encoding="utf-8",
            )
            run_dir = root / ".eryx" / "reports" / "first-run"
            run_dir.mkdir(parents=True)
            files = {"production": ["src/sample.py"], "tests": []}
            job = make_jobs(root, run_dir, files, architecture_ready=False)[0]
            # Run the real installed Pylint adapter against the temporary project.
            self.assertEqual(job["argv"][1], str(ERYX / "_pylint_worker.py"))
            result = execute_job(job, root, run_dir, timeout=30)

            self.assertIsNone(result["error"])
            native_path = root / result["raw_output"]["native_json"]
            native = json.loads(native_path.read_text(encoding="utf-8"))
            return result, native["messages"]

    def test_first_job_writes_native_json_in_a_fresh_report_directory(self):
        result, messages = self.run_pylint(
            "print(undefined_name)\n", ["undefined-variable"]
        )
        self.assertEqual(result["status"], "violations", result)
        self.assertEqual([item["symbol"] for item in messages], ["undefined-variable"])

    def test_pathlib_aliases_keep_encoding_and_mode_diagnostics(self):
        result, messages = self.run_pylint(
            'from pathlib import Path\n'
            'read_text = Path("example.txt").read_text\n'
            'read_text()\n'
            'read_text(encoding="utf-8")\n'
            'open = Path("example.txt").open\n'
            'open("invalid", encoding="utf-8")\n'
            'write_text = Path.write_text\n'
            'write_text(Path("example.txt"), "text", encoding="utf-8")\n',
            ["unspecified-encoding", "bad-open-mode"],
        )
        self.assertEqual(result["status"], "violations", result)
        self.assertEqual(
            [(item["symbol"], item["line"]) for item in messages],
            [("unspecified-encoding", 3), ("bad-open-mode", 6)],
        )


class NotebookPylintTest(unittest.TestCase):
    def test_project_wide_notebooks_are_checked_once_without_modification(self):
        with TemporaryDirectory(prefix="eryx-notebooks-") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src/sample.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests/test_sample.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "experiments").mkdir()
            (root / "build").mkdir()
            notebook = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
                {"cell_type": "code", "id": "cell-one", "metadata": {}, "source": "1\n",
                 "execution_count": None, "outputs": []}]}
            clean = root / "experiments/clean.ipynb"
            dirty = root / "experiments/dirty.ipynb"
            excluded = root / "build/ignored.ipynb"
            clean.write_text(json.dumps(notebook), encoding="utf-8")
            stripped = subprocess.run([sys.executable, "-m", "nbstripout", "--textconv", str(clean)],
                                      capture_output=True, text=True, check=True)
            clean.write_text(stripped.stdout, encoding="utf-8")
            notebook["cells"][0]["outputs"] = [{"output_type": "stream", "name": "stdout", "text": "secret\n"}]
            notebook["cells"][0]["execution_count"] = 1
            dirty.write_text(json.dumps(notebook), encoding="utf-8")
            excluded.write_text(json.dumps(notebook), encoding="utf-8")
            before = {path: path.read_bytes() for path in (clean, dirty, excluded)}
            scope = {"exclude_directory_names": ["build"], "test_directory_names": ["tests"]}
            paths, excluded_dirs = discover_notebooks(root, scope)
            self.assertEqual(paths, ["experiments/clean.ipynb", "experiments/dirty.ipynb"])
            self.assertIn("build/", excluded_dirs)
            config = ('[tool.pylint.main]\nload-plugins = ["_notebook_checker"]\n'
                      '[tool.pylint.messages_control]\ndisable = ["all"]\n'
                      'enable = ["unstripped-notebook"]\n')
            (root / "pyproject.toml").write_text(config, encoding="utf-8")
            (root / ".eryx").mkdir()
            (root / ".eryx/pylint-tests.toml").write_text(config, encoding="utf-8")
            run_dir = root / ".eryx/reports/first-run"
            run_dir.mkdir(parents=True)
            jobs = make_jobs(root, run_dir, {"production": ["src/sample.py"],
                                            "tests": ["tests/test_sample.py"], "notebooks": paths}, False)
            self.assertEqual(len([job for job in jobs if job["tool"] == "pylint"]), 2)
            results = [execute_job(job, root, run_dir, timeout=30) for job in jobs if job["tool"] == "pylint"]
            self.assertEqual([result["status"] for result in results], ["violations", "passed"],
                             [(result, (root / result["raw_output"]["stderr"]).read_text(encoding="utf-8"))
                              for result in results])
            native = json.loads((root / results[0]["raw_output"]["native_json"]).read_text(encoding="utf-8"))
            self.assertEqual([(item["symbol"], item["path"]) for item in native["messages"]],
                             [("unstripped-notebook", "experiments/dirty.ipynb")])
            self.assertEqual(results[0]["diagnostics"][0]["path"], "experiments/dirty.ipynb")
            self.assertEqual({path: path.read_bytes() for path in before}, before)


if __name__ == "__main__":
    unittest.main()
