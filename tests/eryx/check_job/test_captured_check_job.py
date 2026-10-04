"""Regression tests for native Pylint worker execution."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))

from eryx.check import make_jobs
from eryx.check_job.captured_check_job import CapturedCheckJob
from eryx._common import capture


class TestCapturedCheckJob(unittest.TestCase):
    def run_job(self, job: dict[str, Any], root: Path, run_dir: Path,
                timeout: int = 30,
                capture_fn: Callable[[list[str], Path, int], dict[str, Any]] = capture
                ) -> dict[str, Any]:
        return CapturedCheckJob().execute(job, root, run_dir, timeout=timeout,
                                          capture_fn=capture_fn)

    def run_pylint(
        self, code: str, enabled: list[str]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
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
            result = self.run_job(job, root, run_dir)

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

    def test_capture_timeout_terminates_the_real_process(self):
        with TemporaryDirectory(prefix="eryx-timeout-") as directory:
            root = Path(directory)
            run_dir = root / ".eryx" / "reports" / "first-run"
            result = self.run_job({"name": "pydoclint", "tool": "pydoclint",
                                   "argv": [sys.executable, "-c", "import time; time.sleep(10)"]},
                                  root, run_dir, timeout=1)
            self.assertEqual(result["error"], "TOOL_TIMEOUT")
            self.assertEqual(result["status"], "error")
            self.assertNotEqual(result["exit_code"], 0)
            self.assertTrue((root / result["raw_output"]["stderr"]).is_file())

    def test_capture_start_failure_is_reported(self):
        with TemporaryDirectory(prefix="eryx-start-failure-") as directory:
            root = Path(directory)
            run_dir = root / ".eryx" / "reports" / "first-run"
            result = self.run_job({"name": "pydoclint", "tool": "pydoclint",
                                   "argv": ["/definitely/nonexistent-tool"]}, root, run_dir)
            self.assertEqual(result["error"], "TOOL_START_FAILED")
            self.assertEqual(result["status"], "error")
            self.assertTrue((root / result["raw_output"]["stderr"]).is_file())

    def test_capture_exception_archives_failure_and_keeps_exit_code_unknown(self):
        with TemporaryDirectory(prefix="eryx-capture-exception-") as directory:
            root = Path(directory)
            run_dir = root / ".eryx" / "reports" / "first-run"
            job = {"name": "pydoclint", "tool": "pydoclint", "argv": ["missing"]}

            def broken_capture(_argv: list[str], _root: Path, _timeout: int
                               ) -> dict[str, Any]:
                raise OSError("subprocess failed")

            result = self.run_job(job, root, run_dir, capture_fn=broken_capture)
            self.assertEqual((result["status"], result["exit_code"]), ("error", None))
            self.assertEqual(result["error"], "RUNNER_SUBPROCESS_ERROR")
            self.assertEqual((root / result["raw_output"]["stderr"]).read_text(encoding="utf-8"),
                             "subprocess failed")
            self.assertTrue((root / result["raw_output"]["stdout"]).is_file())


if __name__ == "__main__":
    unittest.main()
