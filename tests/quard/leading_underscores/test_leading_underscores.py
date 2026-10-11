"""Autonomous naming, selection and normalized-report regressions."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import validate

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from quard._common import capture
from quard._policy import discover_files, read_scope
from quard.check import execute_job, make_jobs, run_checks
from quard.check_selection_utils import validate_checks
from quard.leading_underscores_utils import analyze
from offline_fixtures import create_repo, fake_tools, native_result


class TestLeadingUnderscores(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        create_repo(self.root)
        self.run_dir = self.root / ".quard/reports/run"
        self.run_dir.mkdir(parents=True)

    def put(self, path: str, code: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")

    def job(self):
        files = discover_files(self.root, read_scope(self.root))
        return next(job for job in make_jobs(self.root, self.run_dir, files, True, [])
                    if job["name"] == "leading-underscores")

    def test_nested_bindings_stubs_tests_paths_and_exclusions(self):
        self.put("src/_pkg/_inner/_module.pyi", """
from external import _original, something as _local
class _Outer:
    __slots__ = ("_slot", "__dict__")
    class _Nested:
        def _method(self, _arg, *, __custom__):
            self._field = _arg
            setattr(self, "_via_setattr", 1)
            external._attribute = 1
            def _inner(_param): ...
            return _inner
def __init__(): ...
def __custom__(): ...
value, _binding = 1, 2
""")
        self.put("tests/_helpers/test__case.py", "async def _test(_parameter):\n"
                 "    for _loop in (): pass\n    match 1:\n        case _capture: pass\n")
        self.put("src/demo/generated/_ignored.py", "class _Ignored: pass\n")
        self.put("src/demo/unicode.py", 'label = "é"; _after = 1\n'
                 'from external import (\n    item as _across,\n)\n')
        files = discover_files(self.root, read_scope(self.root))
        paths = sorted(files["production"] + files["tests"])
        self.assertNotIn("src/demo/generated/_ignored.py", paths)
        findings, error = analyze(self.root, paths)
        self.assertIsNone(error)
        names = {d["symbol"] for d in findings if d["rule"] == "LEADING_UNDERSCORE_NAME"}
        self.assertTrue({"_Outer", "_Nested", "_method", "_arg", "__custom__", "_field",
                         "_via_setattr", "_slot", "_inner", "_param", "_binding", "_local",
                         "_test", "_parameter", "_loop", "_capture"} <= names)
        self.assertFalse({"_original", "_attribute", "__dict__", "__init__", "_Ignored", "_"} & names)
        path_names = {d["symbol"] for d in findings if d["rule"] == "LEADING_UNDERSCORE_PATH"}
        self.assertEqual(path_names, {"_pkg", "_inner", "_module", "_helpers"})
        self.assertTrue(all(d.get("line", 0) >= 1 and d.get("column", 0) >= 1
                            for d in findings if d["rule"] == "LEADING_UNDERSCORE_NAME"))
        self.assertTrue(all("line" not in d for d in findings
                            if d["rule"] == "LEADING_UNDERSCORE_PATH"))
        self.assertIn((1, 15), {(d["line"], d["column"]) for d in findings
                                 if d.get("symbol") == "_after"})
        self.assertIn((3, 13), {(d["line"], d["column"]) for d in findings
                                 if d.get("symbol") == "_across"})

    def test_python_special_names_and_placeholders_are_allowed(self):
        self.put("src/demo/__main__.py", """
__all__ = []
_ = 1
class Model:
    __slots__ = ("__dict__",)
    def __init__(self, _):
        self.__dict__ = {}
        self.__custom__ = 1
    def __post_init__(self): pass
    def __str__(self): return "ok"
""")
        self.put("src/demo/__custom__/__init__.py", "\n")
        items, error = analyze(self.root, ["src/demo/__init__.py", "src/demo/__main__.py"])
        self.assertIsNone(error)
        self.assertEqual({d["symbol"] for d in items}, {"__custom__"})
        paths, error = analyze(self.root, ["src/demo/__custom__/__init__.py"])
        self.assertIsNone(error)
        self.assertEqual([(d["rule"], d["symbol"]) for d in paths],
                         [("LEADING_UNDERSCORE_PATH", "__custom__")])

    def test_worker_normalization_and_error_semantics(self):
        self.put("src/demo/_bad.py", "class _Bad:\n    pass\n")
        job = self.job()
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"LEADING_UNDERSCORE_NAME", "LEADING_UNDERSCORE_PATH"})
        self.assertTrue(all(not d["is_infrastructure_error"] for d in result["diagnostics"]))
        self.put("src/demo/_broken.pyi", "def bad(\n")
        job = self.job()
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
        self.assertIn("LEADING_UNDERSCORES_PARSE_ERROR", {d["rule"] for d in result["diagnostics"]})
        self.assertIn("LEADING_UNDERSCORE_NAME", {d["rule"] for d in result["diagnostics"]})
        for payload, code in (("bad JSON", 0), (json.dumps({
                "quard_leading_underscores_adapter": 1, "diagnostics": [{
                    "rule": "LEADING_UNDERSCORE_NAME", "path": "src/demo/_bad.py",
                    "line": True, "column": 1, "symbol": "_Bad", "message": "bad"}],
                "execution_error": None}), 1), (json.dumps({
                    "quard_leading_underscores_adapter": 1, "diagnostics": [],
                    "execution_error": None}), 1)):
            with self.subTest(payload=payload):
                result = execute_job(job, self.root, self.run_dir, 30,
                                     capture_fn=lambda argv, cwd, timeout: native_result(
                                         argv, stdout=payload, code=code))
                self.assertEqual(result["status"], "error")
                self.assertTrue(any(d["is_infrastructure_error"] for d in result["diagnostics"]))
        Path(job["argv"][-1]).write_text('["../escape.py"]', encoding="utf-8")
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))

    def test_default_on_upgrade_disable_and_full_report(self):
        self.put("src/demo/_bad.py", "_value = 1\n")
        config = self.root / ".quard/config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace(
            "duplicate_detection = true", "duplicate_detection = false"), encoding="utf-8")
        fake, seen = fake_tools()

        def mixed(argv, cwd, timeout):
            if str(QUARD / "_leading_underscores_worker.py") in argv:
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        for lcom in (None, "", "src/demo/__init__.py"):
            with self.subTest(lcom=lcom):
                with patch("quard.check_runner.native_check_runner.require_source_pin"):
                    report = run_checks(self.root, lcom_files=lcom, capture_fn=mixed,
                                        versions_fn=lambda _: ({}, []))
                validate(report, json.loads((QUARD / "report.schema.json").read_text(encoding="utf-8")))
                self.assertEqual((report["status"], report["exit_code"]), ("violations", 1),
                                 report["diagnostics"])
                self.assertEqual(next(t for t in report["tools"] if t["name"] == "leading-underscores")
                                 ["status"], "violations")
                self.assertEqual({d["rule"] for d in report["diagnostics"]},
                                 {"LEADING_UNDERSCORE_NAME", "LEADING_UNDERSCORE_PATH"})
        self.assertTrue(seen)
        config.write_text(config.read_text(encoding="utf-8").replace(
            "leading_underscores = true", "leading_underscores = false"), encoding="utf-8")
        self.assertTrue(validate_checks({"constructor_purity": False})["leading_underscores"])
        with patch("quard.check_runner.native_check_runner.require_source_pin"):
            report = run_checks(self.root, capture_fn=mixed, versions_fn=lambda _: ({}, []))
        self.assertEqual((report["status"], report["exit_code"]), ("passed", 0),
                         report["diagnostics"])
        job = next(t for t in report["tools"] if t["name"] == "leading-underscores")
        self.assertEqual((job["status"], job["exit_code"], job["raw_output"]),
                         ("skipped", None, {}))
        self.assertFalse((self.root / report["report_path"]).parent.joinpath(
            "leading-underscores-files.json").exists())

    def test_checkout_findings_are_real_violations(self):
        files = discover_files(QUARD, read_scope(QUARD))
        self.assertIn("src/quard/_common.py", files["production"])
        self.assertIn("tests/quard/leading_underscores/test_leading_underscores.py", files["tests"])
        findings, error = analyze(QUARD, sorted(files["production"] + files["tests"]))
        self.assertIsNone(error)
        self.assertIn(("LEADING_UNDERSCORE_PATH", "src/quard/_common.py"),
                      {(d["rule"], d["path"]) for d in findings})
        self.assertTrue(any(d["rule"] == "LEADING_UNDERSCORE_NAME" for d in findings))


if __name__ == "__main__":
    unittest.main()
