"""Offline whole-run orchestration regressions on isolated consumers."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import validate

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))
sys.path.insert(0, str(ERYX / "tests"))

from eryx._common import capture, project_dir, read_json
from eryx.check import display
from eryx.check_runner.check_runner import CheckRunner
from eryx.check_runner.native_check_runner import NativeCheckRunner
from offline_fixtures import create_repo, fake_tools


class TestNativeCheckRunner(unittest.TestCase):
    def test_tool_checkout_is_a_valid_consumer(self):
        self.assertEqual(project_dir(ERYX), ERYX)

    def setUp(self):
        self.temp = TemporaryDirectory(prefix="eryx-runner with spaces-")
        self.root = Path(self.temp.name)
        create_repo(self.root)
        self.runner: CheckRunner = NativeCheckRunner()

    def tearDown(self):
        self.temp.cleanup()

    def run_report(self, fake, selection=None, serial=False):
        return self.runner.run(self.root, lcom_files=selection, serial=serial, timeout=30,
                               capture_fn=fake, versions_fn=lambda _: ({}, []))

    def test_full_run_archives_reports_and_each_job(self):
        fake, seen = fake_tools()
        report = self.run_report(fake)
        self.assertEqual((report["status"], report["exit_code"]), ("passed", 0))
        self.assertEqual(len(seen), 14)
        self.assertEqual(len({job["name"] for job in report["tools"]}), 15)
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "constructor-purity")
                         ["status"], "skipped")
        self.assertEqual(report["coverage"]["lcom4"]["mode"], "full")
        self.assertEqual(report["coverage"]["lcom4"]["analyzed_files"],
                         report["coverage"]["lcom4"]["selected_files"])
        self.assertEqual(read_json(self.root / report["report_path"]),
                         read_json(self.root / report["latest_report_path"]))
        validate(report, read_json(ERYX / "report.schema.json"))
        for job in report["tools"]:
            if job["status"] != "skipped":
                self.assertTrue((self.root / job["raw_output"]["stdout"]).is_file())
                self.assertTrue((self.root / job["raw_output"]["stderr"]).is_file())

    def test_selected_lcom4_keeps_other_manifests_full(self):
        fake, seen = fake_tools()
        report = self.run_report(fake, "tests/test_value.py src/demo/domain/api.py tests/test_value.py")
        coverage = report["coverage"]["lcom4"]
        self.assertEqual((report["exit_code"], coverage["mode"], len(seen)), (0, "selected", 14))
        self.assertEqual(coverage["selected_files"],
                         ["tests/test_value.py", "src/demo/domain/api.py"])
        self.assertEqual(coverage["analyzed_files"], coverage["selected_files"])
        run_dir = self.root / report["report_path"].replace("/report.json", "")
        self.assertEqual(read_json(run_dir / "lcom4-files.json"), coverage["selected_files"])
        full_scope = sorted(report["coverage"]["production"] + report["coverage"]["tests"])
        for name in ("method-decorators", "instance-fields", "class-contracts",
                     "contract-folders", "outer-classes", "encapsulated-only"):
            self.assertEqual(read_json(run_dir / f"{name}-files.json"), full_scope)
        self.assertEqual(read_json(run_dir / "test-placement-files.json")["production"],
                         report["coverage"]["production"])
        self.assertEqual(read_json(run_dir / "test-placement-files.json")["tests"],
                         report["coverage"]["tests"])
        self.assertIn("pyright", seen)
        self.assertIn("pylint-production", seen)
        self.assertIn("pylint-tests", seen)

    def test_skipped_lcom4_still_runs_every_other_job(self):
        fake, seen = fake_tools()
        report = self.run_report(fake, "")
        self.assertEqual((report["exit_code"], len(seen)), (0, 13))
        self.assertNotIn("lcom4", seen)
        self.assertEqual(report["coverage"]["lcom4"], {"mode": "skipped", "requested": "",
                         "selected_files": [], "analyzed_files": []})
        job = next(job for job in report["tools"] if job["name"] == "lcom4")
        self.assertEqual((job["status"], job["exit_code"]), ("skipped", None))
        self.assertIn("encapsulated-only", seen)
        validate(report, read_json(ERYX / "report.schema.json"))

    def test_malformed_lcom4_never_claims_analyzed_files(self):
        fake, seen = fake_tools({"lcom4": {"stdout": "not JSON", "code": 1}})
        report = self.run_report(fake, "src/demo/domain/api.py")
        self.assertEqual((report["status"], report["exit_code"], len(seen)), ("error", 2, 14))
        self.assertEqual(report["coverage"]["lcom4"]["selected_files"],
                         ["src/demo/domain/api.py"])
        self.assertEqual(report["coverage"]["lcom4"]["analyzed_files"], [])
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "lcom4")
                         ["status"], "error")

    def test_error_over_violation_and_no_fail_fast(self):
        fake, seen = fake_tools({"pyright": {"stdout": "broken", "code": 2},
                                 "pydoclint": {"stderr": "src/demo/domain/api.py:1: DOC101: Missing argument\n",
                                               "code": 1}})
        report = self.run_report(fake, serial=True)
        self.assertEqual((report["status"], report["exit_code"], len(seen)), ("error", 2, 14))
        self.assertIn("DOC101", {item["rule"] for item in report["diagnostics"]})
        self.assertTrue(any(item["is_infrastructure_error"] for item in report["diagnostics"]))
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "pydoclint")
                         ["status"], "violations")
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "pyright")
                         ["status"], "error")
        self.assertTrue((self.root / report["report_path"]).is_file())

    def test_native_violation_returns_one_and_retains_diagnostic(self):
        fake, seen = fake_tools({"pydoclint": {"stderr": "src/demo/domain/api.py:1: DOC101: Missing argument\n",
                                 "code": 1}})
        report = self.run_report(fake)
        self.assertEqual((report["status"], report["exit_code"], len(seen)), ("violations", 1, 14))
        self.assertEqual([(item["rule"], item["path"], item["line"])
                          for item in report["diagnostics"]],
                         [("DOC101", "src/demo/domain/api.py", 1)])

    def test_malformed_native_output_preserves_other_diagnostics(self):
        for failure in ({"stdout": "not JSON", "code": 0},
                        {"stdout": "", "code": None, "error": "TOOL_TIMEOUT"}):
            with self.subTest(failure=failure):
                fake, seen = fake_tools({"encapsulated-only": failure,
                                         "pydoclint": {"stderr": "src/demo/domain/api.py:1: DOC101: Native issue\n",
                                                       "code": 1}})
                report = self.run_report(fake, "")
                self.assertEqual((report["status"], report["exit_code"], len(seen)),
                                 ("error", 2, 13))
                self.assertIn("DOC101", {item["rule"] for item in report["diagnostics"]})
                self.assertTrue(any(item["is_infrastructure_error"] for item in report["diagnostics"]
                                    if item["tool"] == "encapsulated-only"))
                self.assertEqual(next(job for job in report["tools"] if job["name"] == "encapsulated-only")
                                 ["status"], "error")
                validate(report, read_json(ERYX / "report.schema.json"))

    def test_invalid_selection_and_missing_architecture_still_run_independent_jobs(self):
        for selection in ("src/missing.py", "src/demo/domain/api.pyi", "../outside.py",
                          "/tmp/outside.py", "src/demo/generated/skip.py",
                          "src/demo/domain/../domain/api.py", "tests/missing.py", " "):
            with self.subTest(selection=selection):
                fake, seen = fake_tools()
                report = self.run_report(fake, selection)
                self.assertEqual((report["exit_code"], len(seen)), (2, 13))
                self.assertIn("INVALID_LCOM_FILES", {d["rule"] for d in report["diagnostics"]})
        (self.root / ".eryx/architecture.ready.json").unlink()
        fake, seen = fake_tools()
        report = self.run_report(fake)
        self.assertEqual((report["exit_code"], len(seen)), (2, 12))
        self.assertEqual(sum(job["status"] == "blocked" for job in report["tools"]), 2)

    def test_native_worker_violation_and_pyright_survive_skipped_lcom4(self):
        source = self.root / "src/demo/domain/api.py"
        source.write_text("class Example:\n    @staticmethod\n    def make(): pass\n", encoding="utf-8")
        fake, seen = fake_tools({"pyright": {"stdout": json.dumps({
            "generalDiagnostics": [{"file": str(source), "message": "Native issue",
                                    "severity": "error", "rule": "reportReturnType",
                                    "range": {"start": {"line": 0, "character": 0}}}]}), "code": 1}})

        def mixed(argv, cwd, timeout):
            if str(ERYX / "_method_decorators_worker.py") in argv:
                seen.append("method-decorators")
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        report = self.run_report(mixed, "")
        self.assertEqual((report["status"], report["exit_code"]), ("violations", 1))
        self.assertEqual({item["rule"] for item in report["diagnostics"]},
                         {"NON_INSTANCE_METHOD", "reportReturnType"})
        self.assertEqual(report["coverage"]["lcom4"]["mode"], "skipped")
        self.assertIn("pyright", seen)
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "method-decorators")
                         ["status"], "violations")

    def test_receiver_violations_keep_full_stub_scope_with_selected_lcom4(self):
        (self.root / "src/demo/domain/api.py").write_text(
            "class Example:\n    def missing(): pass\n", encoding="utf-8")
        (self.root / "src/demo/domain/stubs.pyi").write_text(
            "class Stub:\n    async def defaulted(self = ...) -> None: ...\n", encoding="utf-8")
        fake, _ = fake_tools()

        def mixed(argv, cwd, timeout):
            if str(ERYX / "_method_decorators_worker.py") in argv:
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        for selection in ("", "tests/test_value.py"):
            with self.subTest(selection=selection):
                report = self.run_report(mixed, selection)
                self.assertEqual((report["status"], report["exit_code"]), ("violations", 1))
                self.assertEqual([(item["path"], item["symbol"]) for item in report["diagnostics"]
                                  if item["rule"] == "MISSING_INSTANCE_RECEIVER"],
                                 [("src/demo/domain/api.py", "Example.missing"),
                                  ("src/demo/domain/stubs.pyi", "Stub.defaulted")])

    def test_instance_fields_and_native_results_survive_skipped_lcom4(self):
        source = self.root / "src/demo/domain/api.py"
        source.write_text("class Example:\n    def __init__(self): self.field = 1\n"
                          "def use(value: Example):\n    return value.field\n", encoding="utf-8")
        (self.root / "tests/test_value.py").write_text(
            "from demo.domain.api import Example\n"
            "def test_field():\n    return Example().field\n", encoding="utf-8")
        fake, _ = fake_tools({"pyright": {"stdout": json.dumps({
            "generalDiagnostics": [{"file": str(source), "message": "Native issue",
                                    "severity": "error", "rule": "reportReturnType",
                                    "range": {"start": {"line": 0, "character": 0}}}]}), "code": 1}})

        def mixed(argv, cwd, timeout):
            if str(ERYX / "_instance_fields_worker.py") in argv:
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        report = self.run_report(mixed, "")
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual([(item["path"], item["line"], item["rule"])
                          for item in report["diagnostics"] if item["tool"] == "instance-fields"],
                         [("src/demo/domain/api.py", 4, "EXTERNAL_INSTANCE_FIELD"),
                          ("tests/test_value.py", 3, "EXTERNAL_INSTANCE_FIELD")])
        self.assertIn("reportReturnType", {item["rule"] for item in report["diagnostics"]})

    def test_class_contracts_keep_full_source_test_and_stub_scope(self):
        source = self.root / "src/demo/domain/api.py"
        source.write_text("class Missing: pass\n", encoding="utf-8")
        (self.root / "tests/test_value.py").write_text("class TestFixture: pass\n", encoding="utf-8")
        (self.root / "src/demo/domain/stubs.pyi").write_text("class Stub: ...\n", encoding="utf-8")
        fake, _ = fake_tools({"pyright": {"stdout": json.dumps({
            "generalDiagnostics": [{"file": str(source), "message": "Native issue",
                                    "severity": "error", "rule": "reportReturnType",
                                    "range": {"start": {"line": 0, "character": 0}}}]}), "code": 1}})

        def mixed(argv, cwd, timeout):
            if str(ERYX / "_class_contracts_worker.py") in argv:
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        report = self.run_report(mixed, "")
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual({item["path"] for item in report["diagnostics"]
                          if item["rule"] == "MISSING_CLASS_CONTRACT"},
                         {"src/demo/domain/api.py", "src/demo/domain/stubs.pyi",
                          "tests/test_value.py"})
        self.assertIn("reportReturnType", {item["rule"] for item in report["diagnostics"]})

    def test_method_conversions_in_source_and_stub_keep_diagnostics(self):
        (self.root / "src/demo/domain/api.py").write_text(
            "class Example:\n    def make(self): pass\n"
            "Example.make = staticmethod(Example.make)\n", encoding="utf-8")
        (self.root / "src/demo/domain/stubs.pyi").write_text(
            "class Stub:\n    def build(self): ...\n    build = classmethod(build)\n",
            encoding="utf-8")
        fake, _ = fake_tools()

        def mixed(argv, cwd, timeout):
            if str(ERYX / "_method_decorators_worker.py") in argv:
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        report = self.run_report(mixed, "")
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual([(item["path"], item["symbol"], item["rule"])
                          for item in report["diagnostics"]],
                         [("src/demo/domain/api.py", "Example.make", "NON_INSTANCE_METHOD"),
                          ("src/demo/domain/stubs.pyi", "Stub.build", "NON_INSTANCE_METHOD")])
        self.assertTrue(all(not item["is_infrastructure_error"] for item in report["diagnostics"]))

    def test_invalid_readability_still_completes_jobs(self):
        path = self.root / ".eryx/readability.toml"
        path.write_text(path.read_text().replace("max_module_lines = 400", "max_module_lines = 410"))
        fake, seen = fake_tools()
        report = self.run_report(fake)
        self.assertEqual((report["exit_code"], len(seen)), (2, 14))
        self.assertIn("READABILITY_NOT_APPLIED", {item["rule"] for item in report["diagnostics"]})

    def test_display_limit_does_not_truncate_archived_diagnostics(self):
        fake, _ = fake_tools({"pydoclint": {
            "stderr": "src/demo/domain/api.py:1: DOC101: Missing argument\n", "code": 1}})
        report = self.run_report(fake)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            display(report, False, 0)
        self.assertIn("1 more diagnostics", output.getvalue())
        self.assertEqual(len(read_json(self.root / report["report_path"])["diagnostics"]), 1)


if __name__ == "__main__":
    unittest.main()
