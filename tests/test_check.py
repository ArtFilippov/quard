"""Regression tests for native Pylint worker execution."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX))

from check import execute_job, make_jobs
from _policy import discover_notebooks
from _notebook_checker import NotebookChecker, configure_notebooks
from _secret_checker import SecretChecker, configure_secrets, secret_coverage


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

    def test_combined_group_scans_secrets_in_production_worker(self):
        with TemporaryDirectory(prefix="eryx-combined-anonymity-") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            secret = "AKIAIOSFODNN7EXAMPLE"
            source = root / "src/sample.py"
            source.write_text(f"aws_access_key_id = '{secret}'\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                '[tool.pylint.main]\nload-plugins = ["_notebook_checker", "_secret_checker"]\n'
                '[tool.pylint.messages_control]\ndisable = ["all"]\n'
                'enable = ["potential-secret", "secret-scan-failed"]\n', encoding="utf-8")
            run_dir = root / ".eryx/reports/first-run"
            run_dir.mkdir(parents=True)
            jobs = make_jobs(root, run_dir, {"production": ["src/sample.py"], "tests": [],
                                            "notebooks": [], "secret_inputs": ["src/sample.py"]},
                             architecture_ready=False, groups=["codestyle", "anonymity"])
            result = execute_job(jobs[0], root, run_dir, timeout=30)
            self.assertEqual(result["status"], "violations", result)
            self.assertEqual([(d["rule"], d["path"], d["line"]) for d in result["diagnostics"]],
                             [("W9803", "src/sample.py", 1)])
            self.assertNotIn(secret, json.dumps(result))
            self.assertEqual(result["secret_coverage"], {"scanned": ["src/sample.py"], "skipped": []})

    def test_combined_group_rejects_disabled_secret_diagnostics(self):
        with TemporaryDirectory(prefix="eryx-disabled-secrets-") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src/sample.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                '[tool.pylint.main]\nload-plugins = ["_notebook_checker", "_secret_checker"]\n'
                '[tool.pylint.messages_control]\ndisable = ["all"]\n', encoding="utf-8")
            run_dir = root / ".eryx/reports/first-run"
            run_dir.mkdir(parents=True)
            job = make_jobs(root, run_dir, {"production": ["src/sample.py"], "tests": [],
                                            "notebooks": [], "secret_inputs": ["src/sample.py"]},
                            architecture_ready=False, groups=["codestyle", "anonymity"])[0]
            result = execute_job(job, root, run_dir, timeout=30)
            self.assertEqual(result["status"], "error", result)
            self.assertEqual(result["secret_coverage"], {"scanned": [], "skipped": []})
            self.assertIn("SECRET_SCAN_INCOMPLETE", [d["rule"] for d in result["diagnostics"]])


class SecretCheckerTest(unittest.TestCase):
    def test_unreadable_input_and_upstream_exception_do_not_claim_coverage_or_expose_value(self):
        with TemporaryDirectory(prefix="eryx-secret-failure-") as directory:
            root = Path(directory)
            source = root / "settings.txt"
            source.write_text("label = 'safe'\n", encoding="utf-8")
            original = source.read_bytes()
            linter = MagicMock()
            for failure in ("missing", "scanner"):
                with self.subTest(failure=failure):
                    configure_secrets([str(source)])
                    checker = SecretChecker(linter)
                    with patch.object(checker, "add_message") as message:
                        if failure == "missing":
                            source.unlink()
                            checker.open()
                            source.write_bytes(original)
                        else:
                            with patch("_secret_checker.Path.read_text", side_effect=OSError("AKIAIOSFODNN7EXAMPLE")):
                                checker.open()
                        message.assert_called_once_with("secret-scan-failed", line=1, args=(str(source),))
                    self.assertEqual(secret_coverage(), {"scanned": [], "skipped": []})
            self.assertEqual(source.read_bytes(), original)


class NotebookPylintTest(unittest.TestCase):
    def test_invalid_notebook_is_an_infrastructure_error_with_its_path(self):
        with TemporaryDirectory(prefix="eryx-invalid-notebook-") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src/sample.py").write_text("VALUE = 1\n", encoding="utf-8")
            notebook = root / "bad.ipynb"
            notebook.write_bytes(b"not json\n")
            (root / "pyproject.toml").write_text(
                '[tool.pylint.main]\nload-plugins = ["_notebook_checker"]\n'
                '[tool.pylint.messages_control]\ndisable = ["all"]\n'
                'enable = ["notebook-verification-failed"]\n', encoding="utf-8")
            run_dir = root / ".eryx/reports/first-run"
            run_dir.mkdir(parents=True)
            job = make_jobs(root, run_dir, {"production": ["src/sample.py"],
                                            "tests": [], "notebooks": [notebook.name]}, False)[0]
            result = execute_job(job, root, run_dir, timeout=30)
            self.assertEqual(result["status"], "error", result)
            self.assertEqual([(item["rule"], item["path"]) for item in result["diagnostics"]
                              if item.get("is_infrastructure_error")], [("F9802", "bad.ipynb"),
                                                                          ("PYLINT_ANALYSIS_ERROR", None)])
            self.assertEqual(notebook.read_bytes(), b"not json\n")

    def test_launch_and_unexpected_output_are_not_violations(self):
        linter = MagicMock()
        configure_notebooks(["bad.ipynb"])
        try:
            checker = NotebookChecker(linter)
            with patch.object(checker, "add_message") as message:
                with patch("_notebook_checker.subprocess.run", side_effect=FileNotFoundError("missing nbstripout")):
                    checker.open()
                message.assert_called_once_with("notebook-verification-failed", line=1,
                                                args=("bad.ipynb", "missing nbstripout"))
                message.reset_mock()
                for code, stdout, stderr in [(0, "surprise", ""), (1, "", "invalid notebook"),
                                             (2, "", ""), (1, "wrong notebook", "")]:
                    with self.subTest(code=code, stdout=stdout, stderr=stderr):
                        with patch("_notebook_checker.subprocess.run", return_value=subprocess.CompletedProcess(
                                [], code, stdout, stderr)):
                            checker.open()
                        self.assertEqual(message.call_args.args[0], "notebook-verification-failed")
                        message.reset_mock()
        finally:
            configure_notebooks([])

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
