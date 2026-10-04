"""Autonomous scope, worker and full-report regressions for outer classes."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from jsonschema import validate

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))
sys.path.insert(0, str(ERYX / "tests"))

from eryx._common import capture
from eryx._policy import discover_files, read_scope
from eryx.check import execute_job, make_jobs, run_checks
from eryx.outer_classes.parsed_outer_classes import ParsedOuterClasses
from offline_fixtures import create_repo, fake_tools, native_result


class TestParsedOuterClasses(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="eryx-outer-classes-")
        self.root = Path(self.temp.name)
        create_repo(self.root)
        self.run_dir = self.root / ".eryx/reports/run"
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, path: str, code: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")

    def worker(self):
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [])
                   if job["name"] == "outer-classes")
        result = execute_job(job, self.root, self.run_dir, 30)
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        return result, raw, job

    def test_each_file_counts_only_classes_without_enclosing_class(self):
        self.assertEqual(ParsedOuterClasses().collect(ast.parse("class One: pass\n"))[0].name,
                         "One")
        self.put("src/demo/one.py", """\
class Outer:
    class Inner: pass
    def method(self):
        class InMethod: pass
def factory():
    class Local: pass
if True:
    class Conditional: pass
""")
        self.put("src/demo/one.pyi", "class Stub:\n    class Nested: ...\n")
        self.put("src/demo/pair.py", "class Source: pass\n")
        self.put("src/demo/pair.pyi", "class Source: ...\n")
        self.put("src/demo/empty.py", "def function(): pass\n")
        self.put("tests/test_classes.py", "class First: pass\nclass Second: pass\n")
        self.put("src/demo/tests/fixture.pyi", "class A: ...\nclass B: ...\n")
        self.put("src/demo/generated/ignored.py", "class A: pass\nclass B: pass\n")
        self.put("src/demo/generated_models/included.py", "class A: pass\nclass B: pass\n")
        result, raw, job = self.worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual({(d["path"], d["line"], d["column"], d["symbol"])
                          for d in result["diagnostics"]}, {
            ("src/demo/one.py", 1, 1, "Outer"),
            ("src/demo/one.py", 6, 5, "Local"),
            ("src/demo/one.py", 8, 5, "Conditional"),
            ("tests/test_classes.py", 1, 1, "First"),
            ("tests/test_classes.py", 2, 1, "Second"),
            ("src/demo/tests/fixture.pyi", 1, 1, "A"),
            ("src/demo/tests/fixture.pyi", 2, 1, "B"),
            ("src/demo/generated_models/included.py", 1, 1, "A"),
            ("src/demo/generated_models/included.py", 2, 1, "B")})
        self.assertTrue(all(d["rule"] == "MULTIPLE_OUTER_CLASSES" and
                            not d["is_infrastructure_error"] for d in result["diagnostics"]))
        self.assertEqual(len(raw["diagnostics"]), 9)
        paths = json.loads(Path(job["argv"][-1]).read_text(encoding="utf-8"))
        self.assertIn("src/demo/pair.pyi", paths)
        self.assertNotIn("src/demo/generated/ignored.py", paths)

    def test_parse_and_worker_failures_keep_other_file_diagnostics(self):
        self.put("src/demo/broken.pyi", "class Broken(\n")
        self.put("tests/test_classes.py", "class A: pass\nclass B: pass\n")
        result, raw, job = self.worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "OUTER_CLASSES_PARSE_ERROR"))
        self.assertEqual([d["rule"] for d in result["diagnostics"]],
                         ["OUTER_CLASSES_PARSE_ERROR", "MULTIPLE_OUTER_CLASSES",
                          "MULTIPLE_OUTER_CLASSES", "OUTER_CLASSES_PARSE_ERROR"])
        self.assertEqual(sum(d["is_infrastructure_error"] for d in result["diagnostics"]), 2)

        Path(job["argv"][-1]).unlink()
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"], result["error"]),
                         ("error", 2, "OUTER_CLASSES_EXECUTION_ERROR"))

    def test_full_check_records_job_and_raw_output_independently_of_lcom4(self):
        self.put("src/demo/domain/api.py", "class First: pass\nclass Second: pass\n")
        self.put("src/demo/domain/api.pyi", "class Stub: ...\n")
        self.put("tests/test_value.py", "class TestOne: pass\nclass TestTwo: pass\n")
        fake, _ = fake_tools({"pydoclint": {
            "stderr": "tests/test_value.py:1: DOC101: Native issue\n", "code": 1}})

        def mixed(argv, cwd, timeout):
            if str(ERYX / "_outer_classes_worker.py") in argv:
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        for selection in (None, "", "src/demo/__init__.py"):
            with self.subTest(selection=selection):
                report = run_checks(self.root, lcom_files=selection, capture_fn=mixed,
                                    versions_fn=lambda _: ({}, []))
                validate(report, json.loads((ERYX / "report.schema.json").read_text(encoding="utf-8")))
                self.assertEqual((report["status"], report["exit_code"]), ("violations", 1))
                self.assertEqual({(d["path"], d["symbol"]) for d in report["diagnostics"]
                                  if d["rule"] == "MULTIPLE_OUTER_CLASSES"}, {
                    ("src/demo/domain/api.py", "First"),
                    ("src/demo/domain/api.py", "Second"),
                    ("tests/test_value.py", "TestOne"), ("tests/test_value.py", "TestTwo")})
                self.assertIn("DOC101", {d["rule"] for d in report["diagnostics"]})
                job = next(t for t in report["tools"] if t["name"] == "outer-classes")
                self.assertEqual((job["status"], job["exit_code"]), ("violations", 1))
                self.assertTrue((self.root / job["raw_output"]["stdout"]).is_file())
                manifest = self.root / report["report_path"].replace("report.json", "outer-classes-files.json")
                self.assertIn("src/demo/domain/api.pyi", json.loads(manifest.read_text(encoding="utf-8")))

        def malformed(argv, cwd, timeout):
            if str(ERYX / "_outer_classes_worker.py") in argv:
                return native_result(argv, stdout="not JSON")
            return mixed(argv, cwd, timeout)

        report = run_checks(self.root, lcom_files="", capture_fn=malformed,
                            versions_fn=lambda _: ({}, []))
        self.assertEqual((report["status"], report["exit_code"]), ("error", 2))
        self.assertEqual({d["rule"] for d in report["diagnostics"]},
                         {"UNREADABLE_TOOL_OUTPUT", "DOC101"})
        self.assertEqual(report["summary"]["infrastructure_errors"], 1)

    def test_malformed_diagnostics_and_exit_mismatch_fail_closed(self):
        self.put("src/demo/domain/api.py", "class First: pass\nclass Second: pass\n")
        _, _, job = self.worker()

        for payload in ("not JSON", json.dumps({"eryx_outer_classes_adapter": True,
                        "diagnostics": [], "execution_error": None}),
                        json.dumps({"eryx_outer_classes_adapter": 1, "diagnostics": [{
                            "rule": "MULTIPLE_OUTER_CLASSES", "path": "src/demo/domain/api.py",
                            "line": True, "column": 1, "symbol": "First", "message": "bad"}],
                            "execution_error": None})):
            with self.subTest(payload=payload):
                result = execute_job(job, self.root, self.run_dir, 30,
                                     capture_fn=lambda argv, cwd, timeout: native_result(argv, stdout=payload))
                self.assertEqual(result["error"], "UNREADABLE_TOOL_OUTPUT")
        mismatch = execute_job(job, self.root, self.run_dir, 30,
                               capture_fn=lambda argv, cwd, timeout: native_result(argv, stdout=json.dumps({
                                   "eryx_outer_classes_adapter": 1, "diagnostics": [],
                                   "execution_error": None}), code=1))
        self.assertEqual(mismatch["error"], "OUTER_CLASSES_EXECUTION_ERROR")


if __name__ == "__main__":
    unittest.main()
