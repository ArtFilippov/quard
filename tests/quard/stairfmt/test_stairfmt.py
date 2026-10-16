"""Autonomous regressions for checking and fixing Stairfmt compositions."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from typing import Any, override

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from quard._policy import discover_files, read_checks, read_scope
from quard.check import execute_job, make_jobs
from quard.check_runner.native_check_runner import NativeCheckRunner
from offline_fixtures import create_repo, fake_tools


class TestStairfmt(unittest.TestCase):
    """Exercise the consumer-facing check and its independent fix report.

    Args:
        methodName (str, default="runTest"): Test case name.
    """

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path.cwd()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        create_repo(self.root)

    def put(self, name: str, source: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def run_fix(self, *paths: str) -> tuple[int, dict[str, Any], str]:
        command = subprocess.run([sys.executable, "-B", str(QUARD / "stairfmt.py"), "--fix",
                                  "--project", str(self.root), *paths], cwd=self.root,
                                 capture_output=True, text=True, check=False)
        report = json.loads((self.root / ".quard/reports/stairfmt/latest.json").read_text(encoding="utf-8"))
        return command.returncode, report, command.stdout

    def test_enabled_job_reports_positions_and_fix_command(self):
        self.put("src/demo/domain/api.py", "x = f(g(1), h(2))\n")
        self.put("src/demo/domain/api.pyi", "x = f(g(1), h(2))\n")
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / ".quard/reports/run"
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item["name"] == "stairfmt")
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result["status"], result["exit_code"],
                          {item["path"] for item in result["diagnostics"]},
                          all("stairfmt.py --fix --project ." in item["message"]
                              for item in result["diagnostics"])),
                         ("violations", 1, {"src/demo/domain/api.py"}, True), result)

    def test_parse_failure_preserves_other_file_findings(self):
        self.put("src/demo/domain/api.py", "x = f(g(1), h(2))\n")
        self.put("src/demo/broken.py", "def x(:\n")
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / ".quard/reports/run"
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item["name"] == "stairfmt")
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result["status"], result["exit_code"],
                          {item["rule"] for item in result["diagnostics"]},
                          all(item["is_infrastructure_error"] for item in result["diagnostics"]
                              if item["rule"] == "STAIRFMT_PARSE_ERROR")),
                         ("error", 2, {"ST001", "ST002", "ST004", "STAIRFMT_PARSE_ERROR"}, True), result)

    def test_partial_fix_reports_failure_and_keeps_success(self):
        self.put("src/demo/working.py", "x = f(g(1), h(2))\n")
        self.put("src/demo/blocked.py", "x = f(g(1),  # note\n    h(2))\n")
        code, report, output = self.run_fix()
        self.assertEqual((code, report["status"], report["changed_files"],
                          report["warnings"][0]["line"], len(report["remaining"]),
                          "комментарий" in output,
                          "x = f(\n" in (self.root / "src/demo/working.py").read_text()),
                         (1, "violations", ["src/demo/working.py"], 1, 3, True, True))

    def test_successful_fix_is_clean_and_idempotent(self):
        self.put("src/demo/working.py", "x = f(g(h(1)), k(2))\n")
        code, report, _ = self.run_fix("src/demo/working.py")
        code_again, report_again, _ = self.run_fix("src/demo/working.py")
        self.assertEqual((code, report["status"], len(report["changed_files"]),
                          code_again, report_again["changed_files"], report_again["remaining"],
                          (self.root / report["report_path"]).is_file()),
                         (0, "passed", 1, 0, [], [], True))

    def test_explicit_paths_obey_discovered_scope(self):
        self.put("src/demo/generated/ignored.py", "x = f(g(1), h(2))\n")
        self.put("src/demo/working.py", "x = f(g(1), h(2))\n")
        code, report, _ = self.run_fix("src/demo")
        self.assertEqual((code, report["checked_files"], report["changed_files"],
                          (self.root / "src/demo/generated/ignored.py").read_text()),
                         (0, 3, ["src/demo/working.py"], "x = f(g(1), h(2))\n"))

    def test_checkout_opt_out_and_consumer_default(self):
        self.assertEqual((read_checks(QUARD)["stairfmt"], read_checks(self.root)["stairfmt"]),
                         (False, True))

    def test_missing_switch_keeps_consumer_default(self):
        setting = self.root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "stairfmt = true\n", ""), encoding="utf-8")
        self.assertTrue(read_checks(self.root)["stairfmt"])

    def test_command_entry_point_prints_archived_json(self):
        self.put("src/demo/working.py", "x = f(g(1), h(2))\n")
        command = subprocess.run([sys.executable, "-B", str(QUARD / "stairfmt.py"), "--fix",
                                  "--project", str(self.root), "--json", "src/demo/working.py"],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        report = json.loads(command.stdout)
        self.assertEqual((command.returncode, report["exit_code"],
                          (self.root / report["report_path"]).is_file()), (0, 0, True))

    def test_fix_reports_parse_failure_without_hiding_peer_edits(self):
        self.put("src/demo/working.py", "x = f(g(1), h(2))\n")
        self.put("src/demo/broken.py", "def x(:\n")
        code, report, _ = self.run_fix()
        self.assertEqual((code, report["status"], report["changed_files"],
                          report["errors"][0]["path"]),
                         (2, "error", ["src/demo/working.py"], "src/demo/broken.py"))

    def test_rejected_path_still_archives_a_fix_failure(self):
        code, report, _ = self.run_fix("src/demo/generated/ignored.py")
        self.assertEqual((code, report["status"], len(report["errors"]),
                          (self.root / report["report_path"]).is_file()),
                         (2, "error", 1, True))

    def test_corrupt_manifest_is_an_infrastructure_failure(self):
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / ".quard/reports/run"
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item["name"] == "stairfmt")
        (directory / "stairfmt-files.json").write_text("invalid", encoding="utf-8")
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result["status"], result["exit_code"],
                          result["diagnostics"][0]["rule"]),
                         ("error", 2, "STAIRFMT_EXECUTION_ERROR"))

    def test_disabled_job_has_no_manifest(self):
        setting = self.root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "stairfmt = true", "stairfmt = false"), encoding="utf-8")
        fake, seen = fake_tools()
        with patch("quard.check_runner.native_check_runner.require_source_pin"), patch(
                "quard.check_runner.runner_utils.clone_engine_enabled", return_value=False):
            report = NativeCheckRunner().run(self.root, capture_fn=fake, versions_fn=self.versions)
        job = next(item for item in report["tools"] if item["name"] == "stairfmt")
        directory = (self.root / report["report_path"]).parent
        self.assertEqual((job["status"], job["exit_code"], "stairfmt" in seen,
                          (directory / "stairfmt-files.json").exists()),
                         ("skipped", None, False, False))

    def versions(self, root: Path) -> tuple[dict[str, str], list[dict[str, object]]]:
        return {}, []
