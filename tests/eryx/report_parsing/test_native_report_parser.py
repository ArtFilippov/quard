"""Native report parser regressions on isolated consumer paths."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))
sys.path.insert(0, str(ERYX / "tests"))

from offline_fixtures import native_result
from eryx._reports import diagnostic, sorted_diagnostics
from eryx.report_parsing.native_report_parser import NativeReportParser
from eryx.report_parsing.report_parsing import ReportParsing


class TestNativeReportParser(unittest.TestCase):
    def parser(self) -> ReportParsing:
        return NativeReportParser(Path("/tmp/demo-repo"))

    def test_pylint_json2(self):
        text = json.dumps({"messages": [{"messageId": "C0301", "message": "Line too long",
                                         "path": "/tmp/demo-repo/src/demo/a.py", "line": 4,
                                         "column": 0, "type": "convention", "symbol": "line-too-long"}]})
        items, err = self.parser().parse_pylint(text)
        self.assertIsNone(err)
        self.assertEqual(items[0]["column"], 1)
        self.assertEqual(items[0]["path"], "src/demo/a.py")

    def test_pylint_bad_plugin_is_infra(self):
        text = json.dumps({"messages": [{"messageId": "E0013", "message": "Bad plugin",
                                         "path": "config", "line": 1, "column": 0}]})
        result = self.parser().normalize("pylint", native_result([], stdout=text, code=2))
        self.assertEqual(result["status"], "error")

    def test_pyright_one_based_and_banner(self):
        text = "optional banner\n" + json.dumps({"generalDiagnostics": [{
            "file": "/tmp/demo-repo/src/a.py", "message": "Invalid type", "severity": "error",
            "rule": "reportReturnType", "range": {"start": {"line": 0, "character": 3}}}]})
        result = self.parser().parse_pyright(text)[0]
        self.assertEqual((result["line"], result["column"]), (1, 4))

    def test_pydoclint_stderr(self):
        text = "\x1b[31msrc/demo/a.py:8: DOC101: Missing args\x1b[0m\n"
        result = self.parser().normalize("pydoclint", native_result([], stderr=text, code=1))
        self.assertEqual(result["status"], "violations")
        self.assertEqual(result["diagnostics"][0]["rule"], "DOC101")

    def test_tach_positioned(self):
        result = self.parser().normalize("tach", native_result(
            [], stdout="❌ src/demo/a.py[L8]: Cannot import 'demo.b'\n", code=1))
        self.assertEqual(result["status"], "violations")
        item = result["diagnostics"][0]
        self.assertEqual(item["path"], "src/demo/a.py")
        self.assertEqual(item["line"], 8)
        self.assertTrue(item["rule_is_adapter_category"])

    def test_unparsed_failure_never_passes(self):
        result = self.parser().normalize("tach", native_result(
            [], stdout="new unknown error format", code=1))
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["diagnostics"][-1]["is_infrastructure_error"])

    def test_timeout_is_error(self):
        result = self.parser().normalize("pyright", native_result([], error="TOOL_TIMEOUT", code=-15))
        self.assertEqual(result["error"], "TOOL_TIMEOUT")

    def test_config_warning_zero_exit_is_error(self):
        result = self.parser().normalize("pyright", native_result(
            [], stdout='{"generalDiagnostics": []}',
            stderr='Config contains unrecognized setting "bad".'))
        self.assertEqual(result["status"], "error")

    def test_tach_native_adapter_exact_unused(self):
        payload = {"eryx_tach_adapter": 1, "execution_error": None, "diagnostics": [{
            "rule": "unused-dependency", "message": "Unused declaration", "path": "tach.toml",
            "line": None, "severity": "error", "rule_is_adapter_category": True}]}
        result = self.parser().normalize("tach", native_result(
            [], stdout=json.dumps(payload), code=1))
        self.assertEqual(result["status"], "violations")
        self.assertEqual(result["diagnostics"][0]["rule"], "unused-dependency")

    def test_pyright_message_about_config_not_execution_error(self):
        payload = {"generalDiagnostics": [{"file": "src/a.py",
                                          "message": "Function config returns error incorrectly",
                                          "severity": "error", "rule": "reportReturnType",
                                          "range": {"start": {"line": 1, "character": 0}}}]}
        result = self.parser().normalize("pyright", native_result(
            [], stdout=json.dumps(payload), code=1))
        self.assertEqual(result["status"], "violations")

    def test_malformed_native_output_retains_runner_error(self):
        result = self.parser().normalize("pyright", native_result([], stdout="broken", code=2,
                                                                error="TOOL_TIMEOUT"))
        self.assertEqual((result["status"], result["error"]), ("error", "TOOL_TIMEOUT"))
        self.assertEqual(result["diagnostics"][-1]["rule"], "TOOL_TIMEOUT")

    def test_native_diagnostic_survives_tool_failure(self):
        payload = {"generalDiagnostics": [{"file": "src/a.py", "message": "Bad type",
                                          "rule": "reportReturnType",
                                          "range": {"start": {"line": 0, "character": 0}}}]}
        result = self.parser().normalize("pyright", native_result(
            [], stdout=json.dumps(payload), code=2))
        self.assertEqual((result["status"], result["error"]),
                         ("error", "PYRIGHT_EXECUTION_ERROR"))
        self.assertEqual([(item["rule"], item["line"], item["column"])
                          for item in result["diagnostics"]],
                         [("reportReturnType", 1, 1), ("PYRIGHT_EXECUTION_ERROR", None, None)])
        first = diagnostic("pylint", "W0611", "Unused", path="a.py", line=3)
        second = diagnostic("pyright", "reportUnusedImport", "Unused", path="a.py", line=3)
        sorted_items = sorted_diagnostics([second, first, dict(first)])
        self.assertEqual(len(sorted_items), 2)
        self.assertEqual(sorted_items[0]["tool"], "pylint")
