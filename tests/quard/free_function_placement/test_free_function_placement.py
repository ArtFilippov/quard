"""Autonomous free-function placement worker and report regressions."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import override
import unittest
from unittest.mock import patch

from jsonschema import validate

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from quard._common import capture
from quard._policy import discover_files, read_scope
from quard import (_markers, _method_decorators_worker, _restricted_agreement,
                  marker_utils, method_declaration_utils, restriction_agreement_utils)
from quard.check import execute_job, make_jobs, run_checks
from quard.check_runner import native_check_runner, runner_utils
from quard.free_function_placement_utils import analyze as placement
from quard.marker_analysis import marker_index
from quard.method_analysis import declarations
from offline_fixtures import create_repo, fake_tools, native_result
from legacy_clone_fixture_utils import historical_cli


class TestFreeFunctionPlacement(unittest.TestCase):
    def test_migrated_test_support_passes_full_discovered_test_scope(self):
        files = discover_files(QUARD, read_scope(QUARD))
        paths = files["tests"]
        for path in ("tests/test_core_analyzers.py", "tests/discovery_utils.py",
                     "tests/offline_fixtures.py", "tests/offline_fixture_utils.py",
                     "tests/quard/installation/installation_fixture_utils.py",
                     "tests/quard/free_function_placement/test_free_function_placement.py"):
            self.assertIn(path, paths)
        self.assertEqual(placement(QUARD, paths), ([], None))
        manifest = self.root / "test-scope.json"
        manifest.write_text(json.dumps(paths), encoding="utf-8")
        result = capture([sys.executable, str(QUARD / "_free_function_placement_worker.py"),
                          "--manifest", str(manifest)], QUARD, timeout=30)
        self.assertEqual((result["exit_code"], result["error"]), (0, None))
        self.assertEqual(json.loads(result["stdout"])["diagnostics"], [])

    def test_checkout_placement_worker_passes_entire_discovered_scope(self):
        files = discover_files(QUARD, read_scope(QUARD))
        paths = sorted(files["production"] + files["tests"])
        self.assertIn("src/quard/check_runner/native_check_runner.py", paths)
        self.assertIn("src/quard/check_runner/runner_utils.py", paths)
        self.assertEqual(placement(QUARD, paths), ([], None))
        manifest = self.root / "checkout-scope.json"
        manifest.write_text(json.dumps(paths), encoding="utf-8")
        result = capture([sys.executable, str(QUARD / "_free_function_placement_worker.py"),
                          "--manifest", str(manifest)], QUARD, timeout=30)
        self.assertEqual((result["exit_code"], result["error"]), (0, None))
        self.assertEqual(json.loads(result["stdout"])["diagnostics"], [])
        self.assertIs(native_check_runner.make_jobs, runner_utils.make_jobs)
        self.assertIs(native_check_runner.execute_job, runner_utils.execute_job)

    def test_discovery_hook_and_fixture_imports_survive_migration(self):
        import discovery_utils
        import offline_fixture_utils
        import offline_fixtures
        import test_core_analyzers

        self.assertIs(test_core_analyzers.load_tests, discovery_utils.load_tests)
        for name in offline_fixtures.__all__:
            self.assertIs(getattr(offline_fixtures, name),
                          getattr(offline_fixture_utils, name))
        loader = unittest.TestLoader()
        with patch.object(loader, "discover", return_value=unittest.TestSuite()) as discover:
            suite = discovery_utils.load_tests(loader, unittest.TestSuite(), None)
        self.assertEqual(suite.countTestCases(), 0)
        groups = {Path(call.args[0]).name for call in discover.call_args_list}
        self.assertTrue({"installation", "free_function_placement"}
                        <= groups)
        self.assertTrue(all(call.kwargs["pattern"] == "test*.py" and
                            call.kwargs["top_level_dir"] == call.args[0]
                            for call in discover.call_args_list))

    def test_shared_runtime_and_policy_compatibility_and_placement(self):
        paths = [f"src/quard/{name}.py" for name in
                 ("_common", "runtime_error", "runtime_utils", "_policy", "policy_utils",
                  "consumer_policy/consumer_policy", "consumer_policy/configured_consumer_policy")]
        self.assertEqual(placement(QUARD, paths), ([], None))
        manifest = self.root / "shared-functions.json"
        manifest.write_text(json.dumps(paths), encoding="utf-8")
        result = capture([sys.executable, str(QUARD / "_free_function_placement_worker.py"),
                          "--manifest", str(manifest)], QUARD, timeout=30)
        self.assertEqual((result["exit_code"], result["error"]), (0, None))
        self.assertEqual(json.loads(result["stdout"])["diagnostics"], [])

        from quard import _common, _policy, policy_utils, runtime_utils
        from quard.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
        self.assertIs(_common.capture, runtime_utils.capture)
        self.assertIs(_common.QuardError, runtime_utils.QuardError)
        self.assertIs(_policy.configure, policy_utils.configure)
        self.assertIs(_policy.discover_files, policy_utils.discover_files)
        self.assertIs(_policy.write_json, _common.write_json)
        self.assertEqual(_common.tool_dir(), QUARD)
        self.assertEqual(_common.project_dir(self.root), self.root)
        self.assertEqual(ConfiguredConsumerPolicy(self.root).scope(),
                         _policy.read_scope(self.root))
        self.assertEqual(ConfiguredConsumerPolicy(self.root).limit_drift(), [])

    def test_command_and_worker_entry_points_have_class_free_utilities(self):
        entry_points = ("check", "apply", "install", "init_architecture", "_audit",
                        "_pylint_worker", "_tach_worker", "_instance_fields_worker",
                        "_outer_classes_worker", "_encapsulated_only_worker")
        utilities = ("check_utils", "apply_utils", "install_utils",
                     "init_architecture_utils", "audit_utils", "pylint_worker_utils",
                     "tach_worker_utils", "instance_fields_worker_utils",
                     "outer_classes_worker_utils", "encapsulated_only_worker_utils")
        paths = [f"src/quard/{name}.py" for name in (*entry_points, *utilities)]
        self.assertEqual(placement(QUARD, paths), ([], None))
        manifest = self.root / "entry-points.json"
        manifest.write_text(json.dumps(paths), encoding="utf-8")
        placement_result = capture(
            [sys.executable, str(QUARD / "_free_function_placement_worker.py"),
             "--manifest", str(manifest)], QUARD, timeout=30)
        self.assertEqual((placement_result["exit_code"], placement_result["error"]),
                         (0, None))
        self.assertEqual(json.loads(placement_result["stdout"])["diagnostics"], [])

        from quard import check, check_utils, install, install_utils
        from quard import _outer_classes_worker, outer_classes_worker_utils
        self.assertIs(check.main, check_utils.main)
        self.assertIs(install.install, install_utils.install)
        self.assertIs(_outer_classes_worker.analyze, outer_classes_worker_utils.analyze)

        self.put("src/demo/two.py", "class First: pass\nclass Second: pass\n")
        worker_manifest = self.root / "paths.json"
        worker_manifest.write_text('["src/demo/two.py"]', encoding="utf-8")
        result = capture([sys.executable, str(QUARD / "_outer_classes_worker.py"),
                          "--manifest", str(worker_manifest)], self.root, timeout=30)
        self.assertEqual((result["exit_code"], result["error"]), (1, None))
        report = json.loads(result["stdout"])
        self.assertEqual(report["execution_error"], None)
        self.assertEqual({item["rule"] for item in report["diagnostics"]},
                         {"MULTIPLE_OUTER_CLASSES"})

    def test_method_marker_and_restriction_helpers_keep_imports_and_placement(self):
        paths = [f"src/quard/{name}.py" for name in (
            "_method_decorators_worker", "method_analysis/declarations", "method_declaration_utils",
            "_markers", "marker_analysis/marker_index", "marker_utils",
            "_restricted_agreement", "restriction_agreement_utils")]
        self.assertEqual(placement(QUARD, paths), ([], None))
        manifest = self.root / "migrated-helpers.json"
        manifest.write_text(json.dumps(paths), encoding="utf-8")
        result = capture([sys.executable, str(QUARD / "_free_function_placement_worker.py"),
                          "--manifest", str(manifest)], QUARD, timeout=30)
        self.assertEqual((result["exit_code"], result["error"]), (0, None))
        self.assertEqual(json.loads(result["stdout"])["diagnostics"], [])

        self.assertIs(_markers.identity_definition, marker_utils.identity_definition)
        self.assertIs(marker_index.local_names, marker_utils.local_names)
        self.assertIs(_method_decorators_worker.analyze, method_declaration_utils.analyze)
        self.assertIs(declarations.needs_receiver, method_declaration_utils.needs_receiver)
        self.assertIs(declarations.Declarations, _method_decorators_worker.Declarations)
        self.assertIs(_restricted_agreement.agreement, restriction_agreement_utils.agreement)

    @override
    def setUp(self) -> None:
        historical_cli(self)
        self.temp: TemporaryDirectory[str] = TemporaryDirectory(prefix="quard-free-functions-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        create_repo(self.root)
        (self.root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
        (self.root / "tests/test_value.py").write_text("", encoding="utf-8")
        self.run_dir = self.root / ".quard/reports/run"
        self.run_dir.mkdir(parents=True)

    def put(self, path: str, code: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")

    def worker(self):
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [])
                   if job["name"] == "free-function-placement")
        result = execute_job(job, self.root, self.run_dir, 30)
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        return result, raw, job

    def test_related_functions_and_stubs_in_both_profiles_pass(self):
        self.put("src/demo/math_utils.py", "def add(x, y): return x + y\nasync def fetch(): pass\n")
        self.put("src/demo/math_utils.pyi", "def add(x: int, y: int) -> int: ...\n"
                 "async def fetch() -> None: ...\n")
        self.put("tests/test_math_utils.py", "if True:\n    def helper(): pass\n")
        self.put("tests/test_math_utils.pyi", "async def helper() -> None: ...\n")
        self.put("src/demo/model.py", "class Model:\n    def method(self):\n"
                 "        def local(): pass\n        return local()\n")
        self.put("src/demo/generated/bad.py", "def excluded(): pass\n")
        result, raw, job = self.worker()
        self.assertEqual((result["status"], result["exit_code"], raw["diagnostics"]),
                         ("passed", 0, []))
        manifest = json.loads(Path(job["argv"][-1]).read_text(encoding="utf-8"))
        self.assertIn("src/demo/math_utils.pyi", manifest)
        self.assertIn("tests/test_math_utils.pyi", manifest)
        self.assertNotIn("src/demo/generated/bad.py", manifest)

    def test_misplaced_functions_and_classes_have_individual_positions(self):
        self.put("src/demo/ordinary.py", "def first(): pass\nasync def second(): pass\n"
                 "if True:\n    def conditional(): pass\n"
                 "try:\n    async def guarded(): pass\nexcept Exception:\n"
                 "    def fallback(): pass\n"
                 "class Container:\n    def method(self):\n        def nested(): pass\n"
                 "def factory():\n    def inner(): pass\n    return inner\n")
        self.put("src/demo/ordinary.pyi", "def stub() -> None: ...\n")
        self.put("tests/test_api.py", "async def helper(): pass\n")
        self.put("src/demo/format_utils.py", "def format_item(): pass\n"
                 "class First:\n    class Nested: pass\n"
                 "def build():\n    class Local: pass\n    return Local\n")
        result, raw, _ = self.worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual({(d["rule"], d["path"], d["line"], d["column"], d["symbol"])
                          for d in result["diagnostics"]}, {
            ("FREE_FUNCTION_PLACEMENT", "src/demo/ordinary.py", 1, 1, "first"),
            ("FREE_FUNCTION_PLACEMENT", "src/demo/ordinary.py", 2, 1, "second"),
            ("FREE_FUNCTION_PLACEMENT", "src/demo/ordinary.py", 4, 5, "conditional"),
            ("FREE_FUNCTION_PLACEMENT", "src/demo/ordinary.py", 6, 5, "guarded"),
            ("FREE_FUNCTION_PLACEMENT", "src/demo/ordinary.py", 8, 5, "fallback"),
            ("FREE_FUNCTION_PLACEMENT", "src/demo/ordinary.py", 12, 1, "factory"),
            ("FREE_FUNCTION_PLACEMENT", "src/demo/ordinary.pyi", 1, 1, "stub"),
            ("FREE_FUNCTION_PLACEMENT", "tests/test_api.py", 1, 1, "helper"),
            ("FREE_FUNCTION_PLACEMENT", "src/demo/format_utils.py", 1, 1, "format_item"),
            ("FREE_FUNCTION_PLACEMENT", "src/demo/format_utils.py", 4, 1, "build"),
            ("UTILITIES_MODULE_CLASS", "src/demo/format_utils.py", 2, 1, "First"),
            ("UTILITIES_MODULE_CLASS", "src/demo/format_utils.py", 3, 5, "Nested"),
            ("UTILITIES_MODULE_CLASS", "src/demo/format_utils.py", 5, 5, "Local"),
        })
        self.assertTrue(all(not d["is_infrastructure_error"] for d in result["diagnostics"]))
        message = ("first: design this code around object-oriented classes rather than "
                   "free functions. If a free function is unavoidable, declare it in a "
                   "class-free <group_name>_utils.py "
                   "module (or companion stub).")
        self.assertEqual(next(d["message"] for d in result["diagnostics"]
                              if d["symbol"] == "first"), message)

    def test_parse_read_and_manifest_errors_do_not_pass(self):
        self.put("src/demo/broken.pyi", "def broken(\n")
        self.put("src/demo/ordinary.py", "def misplaced(): pass\n")
        result, raw, job = self.worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "FREE_FUNCTION_PLACEMENT_PARSE_ERROR"))
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"FREE_FUNCTION_PLACEMENT_PARSE_ERROR", "FREE_FUNCTION_PLACEMENT"})
        (self.root / "src/demo/broken.pyi").unlink()
        result = execute_job(job, self.root, self.run_dir, 30)
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "FREE_FUNCTION_PLACEMENT_PARSE_ERROR"))
        self.assertIn("FREE_FUNCTION_PLACEMENT", {d["rule"] for d in result["diagnostics"]})
        (self.root / "src/demo/broken.pyi").write_bytes(b"\xff")
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
        self.assertIn("FREE_FUNCTION_PLACEMENT", {d["rule"] for d in result["diagnostics"]})
        Path(job["argv"][-1]).write_text("[1]", encoding="utf-8")
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"], result["error"]),
                         ("error", 2, "FREE_FUNCTION_PLACEMENT_EXECUTION_ERROR"))
        Path(job["argv"][-1]).write_text(
            json.dumps(["../outside.py", "src/demo/ordinary.py"]), encoding="utf-8")
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"FREE_FUNCTION_PLACEMENT_EXECUTION_ERROR", "FREE_FUNCTION_PLACEMENT"})
        Path(job["argv"][-1]).unlink()
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"], result["error"]),
                         ("error", 2, "FREE_FUNCTION_PLACEMENT_EXECUTION_ERROR"))

    def test_full_report_passes_with_real_placement_worker_and_discovered_scope(self):
        self.put("src/demo/example_utils.py", "def helper(): pass\n")
        self.put("src/demo/example_utils.pyi", "def helper() -> None: ...\n")
        self.put("tests/test_example_utils.py", "async def helper(): pass\n")
        self.put("src/demo/generated/ignored.py", "def misplaced(): pass\n")
        fake, _ = fake_tools()

        def mixed(argv, cwd, timeout):
            if str(QUARD / "_free_function_placement_worker.py") in argv:
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        report = run_checks(self.root, lcom_files="", capture_fn=mixed,
                            versions_fn=lambda _: ({}, []))
        validate(report, json.loads((QUARD / "report.schema.json").read_text(encoding="utf-8")))
        self.assertEqual((report["status"], report["exit_code"], report["diagnostics"]),
                         ("passed", 0, []))
        job = next(t for t in report["tools"] if t["name"] == "free-function-placement")
        self.assertEqual((job["status"], job["exit_code"]), ("passed", 0))
        self.assertEqual(json.loads((self.root / job["raw_output"]["stdout"]).read_text(
            encoding="utf-8"))["diagnostics"], [])
        manifest = (self.root / report["report_path"]).parent / "free-function-placement-files.json"
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")),
                         sorted(report["coverage"]["production"] + report["coverage"]["tests"]))
        self.assertNotIn("src/demo/generated/ignored.py", manifest.read_text(encoding="utf-8"))

    def test_full_check_retains_independent_job_and_rejects_bad_worker_output(self):
        self.put("src/demo/bad.py", "def misplaced(): pass\n")
        self.put("src/demo/bad.pyi", "def stub() -> None: ...\n")
        self.put("tests/test_bad.py", "async def helper(): pass\n")
        fake, _ = fake_tools({"pydoclint": {
            "stderr": "src/demo/bad.py:1: DOC101: Native issue\n", "code": 1}})

        def mixed(argv, cwd, timeout):
            if str(QUARD / "_free_function_placement_worker.py") in argv:
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        for selection in (None, "", "src/demo/__init__.py"):
            with self.subTest(selection=selection):
                report = run_checks(self.root, lcom_files=selection, capture_fn=mixed,
                                    versions_fn=lambda _: ({}, []))
                validate(report, json.loads((QUARD / "report.schema.json").read_text(encoding="utf-8")))
                self.assertEqual((report["status"], report["exit_code"]),
                                 ("violations", 1))
                self.assertIn("DOC101", {d["rule"] for d in report["diagnostics"]})
                self.assertIn("FREE_FUNCTION_PLACEMENT", {d["rule"] for d in report["diagnostics"]})
                self.assertEqual({d["path"] for d in report["diagnostics"]
                                  if d["tool"] == "free-function-placement"},
                                 {"src/demo/bad.py", "src/demo/bad.pyi", "tests/test_bad.py"})
                job = next(t for t in report["tools"] if t["name"] == "free-function-placement")
                self.assertEqual((job["status"], job["exit_code"]), ("violations", 1))
                self.assertTrue((self.root / job["raw_output"]["stdout"]).is_file())
                self.assertTrue((self.root / job["raw_output"]["stderr"]).is_file())
                self.assertEqual(json.loads((self.root / report["report_path"]).read_text(
                    encoding="utf-8"))["diagnostics"], report["diagnostics"])

        def malformed(argv, cwd, timeout):
            if str(QUARD / "_free_function_placement_worker.py") in argv:
                return native_result(argv, stdout="not JSON")
            return mixed(argv, cwd, timeout)

        report = run_checks(self.root, lcom_files="", capture_fn=malformed,
                            versions_fn=lambda _: ({}, []))
        self.assertEqual((report["status"], report["exit_code"]), ("error", 2))
        self.assertEqual({d["rule"] for d in report["diagnostics"]},
                         {"UNREADABLE_TOOL_OUTPUT", "DOC101"})
        job = next(t for t in report["tools"] if t["name"] == "free-function-placement")
        self.assertEqual((job["status"], job["exit_code"]), ("error", 0))
        self.assertTrue(any(d["is_infrastructure_error"] for d in report["diagnostics"]))

    def test_malformed_diagnostics_and_exit_mismatch_fail_closed(self):
        _, _, job = self.worker()
        for payload in ("not JSON", json.dumps({"quard_free_function_placement_adapter": True,
                        "diagnostics": [], "execution_error": None}),
                        json.dumps({"quard_free_function_placement_adapter": 1,
                                    "diagnostics": [{"rule": "FREE_FUNCTION_PLACEMENT",
                                                     "path": "src/demo/api.py", "line": True,
                                                     "column": 1, "symbol": "a", "message": "bad"}],
                                    "execution_error": None})):
            with self.subTest(payload=payload):
                result = execute_job(job, self.root, self.run_dir, 30,
                                     capture_fn=lambda argv, cwd, timeout: native_result(argv, stdout=payload))
                self.assertEqual(result["error"], "UNREADABLE_TOOL_OUTPUT")
        result = execute_job(job, self.root, self.run_dir, 30,
                             capture_fn=lambda argv, cwd, timeout: native_result(argv, stdout=json.dumps({
                                 "quard_free_function_placement_adapter": 1,
                                 "diagnostics": [], "execution_error": None}), code=1))
        self.assertEqual(result["error"], "FREE_FUNCTION_PLACEMENT_EXECUTION_ERROR")


if __name__ == "__main__":
    unittest.main()
