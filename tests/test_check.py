"""Regression tests for native Pylint worker execution."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX / "src"))

from eryx.check import execute_job, make_jobs


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


if __name__ == "__main__":
    unittest.main()
