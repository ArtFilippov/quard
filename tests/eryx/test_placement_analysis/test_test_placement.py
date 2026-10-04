"""Autonomous test-to-production mapping and independent diagnostic regressions."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, override
import unittest

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))

from eryx._policy import discover_files, read_scope
from eryx.check import execute_job, make_jobs


class TestTestPlacement(unittest.TestCase):
    def test_migrated_analyzer_suites_map_to_production_contracts(self) -> None:
        from eryx.contract_analysis.test_placement import analyze
        from eryx._outer_classes_worker import analyze as outer_analyze

        files = discover_files(ERYX, read_scope(ERYX))
        migrated = [
            "tests/eryx/contract_analysis/test_contracts.py",
            "tests/eryx/folder_analysis/test_folder_ownership.py",
            "tests/eryx/test_placement_analysis/test_test_placement.py",
            "tests/eryx/method_analysis/test_declarations.py",
            "tests/eryx/cohesion_analysis/test_classes.py",
            "tests/eryx/field_ownership/test_ownership.py",
            "tests/eryx/constructor_analysis/test_constructor_checker.py",
            "tests/eryx/restricted_calls/test_calls.py",
        ]
        self.assertTrue(set(migrated) <= set(files["tests"]))
        diagnostics, error = analyze(ERYX, files["production"], migrated, ["tests"])
        self.assertIsNone(error)
        self.assertEqual(diagnostics, [])
        diagnostics, error = outer_analyze(ERYX, migrated)
        self.assertIsNone(error)
        self.assertEqual(diagnostics, [])

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.temp = TemporaryDirectory(prefix="eryx-test-placement-")
        self.root = Path(self.temp.name)
        self.run_dir = self.root / ".eryx/reports/run"

    @override
    def setUp(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / ".eryx").mkdir()
        (self.root / ".eryx/scope.toml").write_text(
            'exclude_directory_names = ["generated"]\n'
            + 'test_directory_names = ["tests", "specs"]\n', encoding="utf-8")
        self.run_dir.mkdir(parents=True)

    @override
    def tearDown(self) -> None:
        self.temp.cleanup()

    def put(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def run_worker(self, name: str = "test-placement") -> tuple[dict[str, Any], dict[str, Any]]:
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [],
                    test_directory_names=["tests", "specs"]) if job["name"] == name)
        result = execute_job(job, self.root, self.run_dir, 30)
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        return result, raw

    def test_contract_implementation_envelope_and_configured_test_root(self):
        self.put("src/pkg/http_port/http_port.py", "from typing import Protocol\n"
                 + "class HTTPPort(Protocol):\n    def fetch(self): ...\n")
        self.put("src/pkg/http_port/adapter.py", "from .http_port import HTTPPort\n"
                 + "class Adapter(HTTPPort):\n    def fetch(self): return 1\n")
        self.put("src/pkg/http_port/envelopes/http_port_envelope.py",
                 "from pkg.http_port.http_port import HTTPPort\n"
                 + "class HTTPPortEnvelope(HTTPPort):\n    def fetch(self): return 1\n")
        self.put("src/pkg/http_port/envelopes/logging_http_port_envelope.py",
                 "from .http_port_envelope import HTTPPortEnvelope\n"
                 + "class LoggingHTTPPortEnvelope(HTTPPortEnvelope): pass\n")
        self.put("src/pkg/http_port/adapter.pyi", "from .http_port import HTTPPort\n"
                 + "class Adapter(HTTPPort): ...\n")
        self.put("tests/pkg/http_port/test_http_port.py",
                 "from unittest import TestCase\nclass TestHTTPPort(TestCase):\n"
                 + "    def test_contract(self): pass\n")
        self.put("tests/pkg/http_port/test_adapter.py",
                 "from unittest import TestCase\nclass TestAdapter(TestCase):\n"
                 + "    def test_fetch(self): pass\n")
        self.put("tests/pkg/http_port/envelopes/test_http_port_envelope.py",
                 "class TestHTTPPortEnvelope(object):\n    def test_wrap(self): pass\n")
        self.put("tests/pkg/http_port/envelopes/test_logging_http_port_envelope.py",
                 "class TestLoggingHTTPPortEnvelope(object): pass\n")
        self.put("src/pkg/specs/http_port/test_adapter.py",
                 "class TestAdapter(object):\n    def test_fetch(self): pass\n")
        self.put("tests/pkg/http_port/helpers.py", "def prepare(): return 1\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("passed", 0, None), result["diagnostics"])
        self.assertEqual(result["diagnostics"], [])
        self.assertIn("src/pkg/specs/http_port/test_adapter.py", json.loads(
            (self.run_dir / "test-placement-files.json").read_text(encoding="utf-8"))["tests"])

    def test_missing_ambiguous_misnamed_misplaced_and_contractless_targets(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\nclass Port(Protocol): pass\n")
        self.put("src/pkg/port/adapter.py", "from .port import Port\nclass Adapter(Port): pass\n")
        self.put("src/pkg/port/other.py", "from .port import Port\nclass Adapter(Port): pass\n")
        self.put("src/pkg/port/loose.py", "class Loose(object): pass\n")
        self.put("src/pkg/port/envelopes/port_envelope.py", "from pkg.port.port import Port\n"
                 + "class PortEnvelope(Port): pass\n")
        self.put("tests/pkg/port/test_adapter.py", "class TestAdapter(object): pass\n")
        self.put("tests/pkg/port/test_port_envelope.py", "class TestPortEnvelope(object): pass\n")
        self.put("tests/pkg/wrong/test_port.py", "class TestPort(object): pass\n")
        self.put("tests/pkg/port/test_missing.py", "class TestMissing(object): pass\n")
        self.put("tests/pkg/port/test_loose.py", "class TestLoose(object): pass\n")
        self.put("tests/pkg/port/test_bad.py", "class TestPort(object): pass\n")
        self.put("tests/pkg/port/test_broken.py", "class PortTests(object): pass\n")
        self.put("tests/pkg/port/test_functions.py", "def test_independent(): pass\n"
                 + "async def test_async(): pass\n"
                 + "if True:\n    def test_conditional(): pass\n"
                 + "def helper():\n    def test_nested(): pass\n")
        self.put("tests/pkg/generated/test_excluded.py", "def test_ignored(): pass\n")
        result, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual({(item["symbol"], item["rule"]) for item in result["diagnostics"]}, {
            ("TestAdapter", "AMBIGUOUS_TEST_TARGET"),
            ("TestPortEnvelope", "TEST_CLASS_PLACEMENT"),
            ("TestPort", "TEST_CLASS_PLACEMENT"),
            ("TestMissing", "MISSING_TEST_TARGET"),
            ("TestLoose", "TEST_TARGET_CONTRACT"),
            ("TestPort", "TEST_MODULE_NAME"),
            ("PortTests", "TEST_CLASS_NAME"),
            ("test_independent", "TOP_LEVEL_TEST_FUNCTION"),
            ("test_async", "TOP_LEVEL_TEST_FUNCTION"),
            ("test_conditional", "TOP_LEVEL_TEST_FUNCTION")})
        self.assertTrue(all(item["line"] > 0 and item["column"] > 0 and
                            not item["is_infrastructure_error"] for item in result["diagnostics"]))

    def test_stub_target_and_independent_class_contract_diagnostics(self):
        self.put("src/pkg/port/port.pyi", "from typing import Protocol\nclass Port(Protocol): ...\n")
        self.put("src/pkg/port/broken.py", "class Broken: pass\n")
        self.put("tests/pkg/port/test_port.pyi", "class TestPort(object): ...\n")
        self.put("tests/pkg/port/test_broken.py", "class TestBroken(object): pass\n")
        placement, _ = self.run_worker()
        contracts, _ = self.run_worker("class-contracts")
        self.assertEqual([(d["symbol"], d["rule"]) for d in placement["diagnostics"]],
                         [("TestBroken", "TEST_TARGET_CONTRACT")])
        self.assertEqual({(d["path"], d["rule"]) for d in contracts["diagnostics"]}, {
            ("src/pkg/port/broken.py", "MISSING_CLASS_CONTRACT")})

    def test_native_test_diagnostics_are_independent(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\nclass Port(Protocol): pass\n")
        self.put("tests/pkg/port/test_port.py", "def test_independent():\n    return unknown_name\n")
        self.put(".eryx/pylint-tests.toml", '[tool.pylint.messages_control]\n'
                 + 'disable = ["all"]\nenable = ["undefined-variable"]\n')
        placement, _ = self.run_worker()
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [],
                    test_directory_names=["tests", "specs"]) if job["name"] == "pylint-tests")
        native = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual([d["rule"] for d in placement["diagnostics"]],
                         ["TOP_LEVEL_TEST_FUNCTION"])
        self.assertEqual((native["status"], native["exit_code"]), ("violations", 2), native)
        self.assertEqual([d["rule"] for d in native["diagnostics"]], ["E0602"])

    def test_parse_failure_and_malformed_worker_report(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\nclass Port(Protocol): pass\n")
        self.put("src/bad.py", "class Broken(\n")
        self.put("tests/pkg/port/test_port.py", "class TestPort(object): pass\n"
                 + "def test_independent(): pass\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "TEST_PLACEMENT_PARSE_ERROR"))
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"TEST_PLACEMENT_PARSE_ERROR", "TOP_LEVEL_TEST_FUNCTION"})
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [],
                    test_directory_names=["tests", "specs"]) if job["name"] == "test-placement")
        broken = execute_job(job, self.root, self.run_dir, 30, capture_fn=lambda *args: {
            "stdout": '{"eryx_test_placement_adapter": 1, "diagnostics": [], "execution_error": null}',
            "stderr": "", "exit_code": 1, "duration_seconds": 0, "error": None})
        self.assertEqual((broken["status"], broken["error"]),
                         ("error", "TEST_PLACEMENT_EXECUTION_ERROR"))


if __name__ == "__main__":
    unittest.main()
