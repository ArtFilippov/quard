"""Offline whole-run orchestration regressions on isolated consumers."""
from __future__ import annotations

import io
import json
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, override
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
from eryx.check_runner.runner_utils import binary, installed_versions
from eryx.report_utils import normalize
from offline_fixtures import create_repo, fake_tools
from offline_fixture_utils import native_result


class TestNativeCheckRunner(unittest.TestCase):
    def test_tool_checkout_is_a_valid_consumer(self):
        self.assertEqual(project_dir(ERYX), ERYX)
        self.assertEqual(project_dir(self.root), self.root)

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path(self.enterContext(
            TemporaryDirectory(prefix="eryx-runner with spaces-")))
        create_repo(self.root)
        self.runner: CheckRunner = NativeCheckRunner()

    def run_report(self, fake: Callable[[list[str], Path, int], dict[str, Any]],
                   selection: str | None = None, serial: bool = False) -> dict[str, Any]:
        def versions(_root: Path) -> tuple[dict[str, str], list[str]]:
            return {}, []

        return self.runner.run(self.root, lcom_files=selection, serial=serial, timeout=30,
                               capture_fn=fake, versions_fn=versions)

    def test_full_run_archives_reports_and_each_job(self):
        fake, seen = fake_tools()
        report = self.run_report(fake)
        self.assertEqual((report["status"], report["exit_code"]), ("passed", 0))
        self.assertEqual(len(seen), 17)
        self.assertEqual(len({job["name"] for job in report["tools"]}), 18)
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
        report = self.run_report(
            fake, "tests/test_value.py src/demo/domain/api.py tests/test_value.py")
        coverage = report["coverage"]["lcom4"]
        self.assertEqual((report["exit_code"], coverage["mode"], len(seen)), (0, "selected", 17))
        self.assertEqual(coverage["selected_files"],
                         ["tests/test_value.py", "src/demo/domain/api.py"])
        self.assertEqual(coverage["analyzed_files"], coverage["selected_files"])
        run_dir = self.root / report["report_path"].replace("/report.json", "")
        self.assertEqual(read_json(run_dir / "lcom4-files.json"), coverage["selected_files"])
        full_scope = sorted(report["coverage"]["production"] + report["coverage"]["tests"])
        for name in ("method-decorators", "instance-fields", "class-contracts",
                     "contract-folders", "outer-classes", "free-function-placement",
                     "encapsulated-only"):
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
        self.assertEqual((report["exit_code"], len(seen)), (0, 16))
        self.assertNotIn("lcom4", seen)
        self.assertEqual(report["coverage"]["lcom4"], {"mode": "skipped", "requested": "",
                         "selected_files": [], "analyzed_files": []})
        job = next(job for job in report["tools"] if job["name"] == "lcom4")
        self.assertEqual((job["status"], job["exit_code"]), ("skipped", None))
        self.assertIn("encapsulated-only", seen)
        self.assertIn("free-function-placement", seen)
        validate(report, read_json(ERYX / "report.schema.json"))

    def test_malformed_lcom4_never_claims_analyzed_files(self):
        fake, seen = fake_tools({"lcom4": {"stdout": "not JSON", "code": 1}})
        report = self.run_report(fake, "src/demo/domain/api.py")
        self.assertEqual((report["status"], report["exit_code"], len(seen)), ("error", 2, 17))
        self.assertEqual(report["coverage"]["lcom4"]["selected_files"],
                         ["src/demo/domain/api.py"])
        self.assertEqual(report["coverage"]["lcom4"]["analyzed_files"], [])
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "lcom4")
                         ["status"], "error")

    def test_error_over_violation_and_no_fail_fast(self):
        message = "src/demo/domain/api.py:1: DOC101: Missing argument\n"
        fake, seen = fake_tools({"pyright": {"stdout": "broken", "code": 2},
                                 "pydoclint": {"stderr": message, "code": 1}})
        report = self.run_report(fake, serial=True)
        self.assertEqual((report["status"], report["exit_code"], len(seen)), ("error", 2, 17))
        self.assertIn("DOC101", {item["rule"] for item in report["diagnostics"]})
        self.assertTrue(any(item["is_infrastructure_error"] for item in report["diagnostics"]))
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "pydoclint")
                         ["status"], "violations")
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "pyright")
                         ["status"], "error")
        self.assertTrue((self.root / report["report_path"]).is_file())

    def test_unexpected_job_failure_does_not_cancel_or_hide_native_results(self):
        fake, seen = fake_tools({"pydoclint": {
            "stderr": "src/demo/domain/api.py:1: DOC101: Native issue\n", "code": 1}})

        def failing_capture(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            if str(ERYX / "_lcom4_worker.py") in argv:
                raise RuntimeError("worker crashed")
            return fake(argv, cwd, timeout)

        report = self.run_report(failing_capture, serial=True)
        self.assertEqual((report["status"], report["exit_code"], len(seen)),
                         ("error", 2, 16))
        self.assertEqual({item["rule"] for item in report["diagnostics"]},
                         {"RUNNER_SUBPROCESS_ERROR", "DOC101"})
        jobs = {job["name"]: job for job in report["tools"]}
        self.assertEqual((jobs["lcom4"]["status"], jobs["lcom4"]["exit_code"]),
                         ("error", None))
        self.assertEqual((jobs["pydoclint"]["status"], jobs["pydoclint"]["exit_code"]),
                         ("violations", 1))
        log = self.root / jobs["lcom4"]["raw_output"]["stderr"]
        self.assertIn("worker crashed", log.read_text(encoding="utf-8"))
        self.assertEqual(report["coverage"]["lcom4"]["analyzed_files"], [])
        validate(report, read_json(ERYX / "report.schema.json"))

    def test_native_violation_returns_one_and_retains_diagnostic(self):
        fake, seen = fake_tools({"pydoclint": {
            "stderr": "src/demo/domain/api.py:1: DOC101: Missing argument\n",
                                 "code": 1}})
        report = self.run_report(fake)
        self.assertEqual((report["status"], report["exit_code"], len(seen)), ("violations", 1, 17))
        self.assertEqual([(item["rule"], item["path"], item["line"])
                          for item in report["diagnostics"]],
                         [("DOC101", "src/demo/domain/api.py", 1)])

    def test_malformed_native_output_preserves_other_diagnostics(self):
        message = "src/demo/domain/api.py:1: DOC101: Native issue\n"
        for failure in ({"stdout": "not JSON", "code": 0},
                        {"stdout": "", "code": None, "error": "TOOL_TIMEOUT"}):
            with self.subTest(failure=failure):
                fake, seen = fake_tools({"encapsulated-only": failure,
                                         "pydoclint": {"stderr": message, "code": 1}})
                report = self.run_report(fake, "")
                self.assertEqual((report["status"], report["exit_code"], len(seen)),
                                 ("error", 2, 16))
                self.assertIn("DOC101", {item["rule"] for item in report["diagnostics"]})
                self.assertTrue(any(
                    item["is_infrastructure_error"] for item in report["diagnostics"]
                    if item["tool"] == "encapsulated-only"))
                self.assertEqual(next(
                    job for job in report["tools"] if job["name"] == "encapsulated-only")
                                 ["status"], "error")
                validate(report, read_json(ERYX / "report.schema.json"))

    def test_invalid_selection_and_missing_architecture_still_run_independent_jobs(self):
        for selection in ("src/missing.py", "src/demo/domain/api.pyi", "../outside.py",
                          "/tmp/outside.py", "src/demo/generated/skip.py",
                          "src/demo/domain/../domain/api.py", "tests/missing.py", " "):
            with self.subTest(selection=selection):
                fake, seen = fake_tools()
                report = self.run_report(fake, selection)
                self.assertEqual((report["exit_code"], len(seen)), (2, 16))
                self.assertIn("INVALID_LCOM_FILES", {d["rule"] for d in report["diagnostics"]})
        (self.root / ".eryx/architecture.ready.json").unlink()
        fake, seen = fake_tools()
        report = self.run_report(fake)
        self.assertEqual((report["exit_code"], len(seen)), (2, 15))
        self.assertEqual(sum(job["status"] == "blocked" for job in report["tools"]), 2)

    def test_native_worker_violation_and_pyright_survive_skipped_lcom4(self):
        source = self.root / "src/demo/domain/api.py"
        source.write_text(
            "class Example:\n    @staticmethod\n    def make(): pass\n", encoding="utf-8")
        fake, seen = fake_tools({"pyright": {"stdout": json.dumps({
            "generalDiagnostics": [{"file": str(source), "message": "Native issue",
                                    "severity": "error", "rule": "reportReturnType",
                                     "range": {"start": {"line": 0, "character": 0}}}]
        }), "code": 1}})

        def mixed(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
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

        def mixed(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
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
                                     "range": {"start": {"line": 0, "character": 0}}}]
        }), "code": 1}})

        def mixed(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
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
        (self.root / "tests/test_value.py").write_text(
            "class TestFixture: pass\n", encoding="utf-8")
        (self.root / "src/demo/domain/stubs.pyi").write_text("class Stub: ...\n", encoding="utf-8")
        fake, _ = fake_tools({"pyright": {"stdout": json.dumps({
            "generalDiagnostics": [{"file": str(source), "message": "Native issue",
                                    "severity": "error", "rule": "reportReturnType",
                                     "range": {"start": {"line": 0, "character": 0}}}]
        }), "code": 1}})

        def mixed(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
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

        def mixed(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
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
        path.write_text(path.read_text().replace(
            "max_module_lines = 400", "max_module_lines = 410"))
        fake, seen = fake_tools()
        report = self.run_report(fake)
        self.assertEqual((report["exit_code"], len(seen)), (2, 17))
        self.assertIn("READABILITY_NOT_APPLIED", {item["rule"] for item in report["diagnostics"]})

    def test_display_limit_does_not_truncate_archived_diagnostics(self):
        fake, _ = fake_tools({"pydoclint": {
            "stderr": "src/demo/domain/api.py:1: DOC101: Missing argument\n", "code": 1}})
        report = self.run_report(fake)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            display(report, False, 0)
        self.assertIn("1 more diagnostics", output.getvalue())
        self.assertEqual(len(read_json(self.root / report["report_path"])["diagnostics"]), 1)

    def test_real_jscpd_clones_and_clean_consumer_with_lcom_skipped(self) -> None:
        (self.root / "src/demo/domain/api.py").write_text(
            "from typing import Protocol\n\nclass API(Protocol):\n"
            "    def read(self) -> str: ...\n",
            encoding="utf-8")
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        text = ("def calculate(value):\n    alpha = value + 17\n    beta = alpha * 3\n" +
                "    gamma = beta - 4\n    delta = gamma / 2\n    return delta + 10\n")
        first.write_text(text, encoding="utf-8")
        second.write_text(text, encoding="utf-8")
        excluded = self.root / "src/demo/generated"
        excluded.mkdir()
        (excluded / "hidden.py").write_text(text, encoding="utf-8")
        (self.root / "src/demo/companion.pyi").write_text(text, encoding="utf-8")
        fake, seen = fake_tools()

        def dispatch(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            if Path(argv[0]).name == "jscpd":
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        report = self.run_report(dispatch, "")
        job = next(item for item in report["tools"] if item["name"] == "jscpd")
        self.assertEqual((report["exit_code"], job["status"], job["exit_code"]),
                         (1, "violations", 1), report["diagnostics"])
        clones = [item for item in report["diagnostics"] if item["tool"] == "jscpd"]
        self.assertEqual({item["path"] for item in clones},
                         {"src/demo/first.py", "tests/second.py"})
        self.assertEqual({item["rule"] for item in clones}, {"EXACT_CLONE"})
        self.assertEqual({item["native_details"]["kind"] for item in clones}, {"exact"})
        self.assertTrue(all(item["line"] >= 1 and item["column"] >= 1 for item in clones))
        self.assertEqual(len(clones), 2)
        self.assertEqual(set(job["raw_output"]), {"native_json", "stdout", "stderr"})
        self.assertNotIn("src/demo/generated/hidden.py", job["argv"])
        self.assertNotIn("src/demo/companion.pyi", job["argv"])
        self.assertEqual(job["argv"][job["argv"].index("--kind") + 1], "exact")
        self.assertNotIn("--ignore-identifiers", job["argv"])
        renamed = next(item for item in report["tools"] if item["name"] == "jscpd-renamed")
        self.assertEqual(renamed["argv"][renamed["argv"].index("--kind") + 1], "renamed")
        self.assertIn("--ignore-identifiers", renamed["argv"])
        self.assertIn("--ignore-literals", renamed["argv"])
        self.assertEqual(renamed["status"], "passed")
        self.assertEqual(job["argv"][job["argv"].index("--exit-code") + 2:],
                         renamed["argv"][renamed["argv"].index("--exit-code") + 2:])
        self.assertNotIn("lcom4", seen)
        second.unlink()
        clean = self.run_report(dispatch, "")
        self.assertEqual(next(item for item in clean["tools"] if item["name"] == "jscpd")[
            "status"], "passed")
        self.assertEqual(next(item for item in clean["tools"] if item["name"] == "jscpd-renamed")[
            "status"], "passed")

    def test_real_jscpd_renamed_identifiers_and_literals_full_scope(self) -> None:
        (self.root / "src/demo/domain/api.py").write_text(
            "from typing import Protocol\n\nclass API(Protocol):\n"
            "    def read(self) -> str: ...\n",
            encoding="utf-8")
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        original = ("def calculate(value):\n    alpha = value + 17\n    beta = alpha * 3\n" +
                    "    gamma = beta - 4\n    delta = gamma / 2\n    return delta + 10\n")
        first.write_text(original, encoding="utf-8")
        fake, _ = fake_tools()

        def dispatch(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            if Path(argv[0]).name == "jscpd":
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        for variant in (original.replace("value", "amount").replace("alpha", "start")
                        .replace("beta", "middle").replace("gamma", "later")
                        .replace("delta", "answer").replace("calculate", "compute"),
                        original.replace("17", "71").replace("3", "8")
                        .replace("4", "6").replace("2", "9").replace("10", "22")):
            with self.subTest(variant=variant):
                second.write_text(variant, encoding="utf-8")
                report = self.run_report(dispatch)
                job = next(item for item in report["tools"] if item["name"] == "jscpd-renamed")
                clones = [item for item in report["diagnostics"] if item["tool"] == "jscpd"]
                self.assertEqual((report["exit_code"], job["status"], job["exit_code"]),
                                 (1, "violations", 1), report["diagnostics"])
                self.assertEqual({item["rule"] for item in clones}, {"RENAMED_CLONE"})
                self.assertEqual({item["native_details"]["kind"] for item in clones},
                                 {"renamed"})
                self.assertEqual({item["path"] for item in clones},
                                 {"src/demo/first.py", "tests/second.py"})
                self.assertTrue(all(item["line"] >= 1 and item["column"] >= 1
                                    and "clone of " in item["message"] for item in clones))
                native = read_json(self.root / job["raw_output"]["native_json"])
                self.assertTrue(any(entry["kind"] == "renamed" for entry in native["duplicates"]))
                self.assertEqual(next(item for item in report["tools"] if item["name"] ==
                                      "jscpd")["status"], "passed")

    def test_renamed_native_failure_does_not_hide_exact_clones(self) -> None:
        text = ("def calculate(value):\n    alpha = value + 17\n    beta = alpha * 3\n" +
                "    gamma = beta - 4\n    delta = gamma / 2\n    return delta + 10\n")
        (self.root / "src/demo/first.py").write_text(text, encoding="utf-8")
        (self.root / "tests/second.py").write_text(text, encoding="utf-8")
        fake, _ = fake_tools()

        def dispatch(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            if Path(argv[0]).name == "jscpd":
                result = capture(argv, cwd, timeout)
                if argv[argv.index("--kind") + 1] == "renamed":
                    (Path(argv[argv.index("--output") + 1]) / "jscpd-report.json").unlink()
                return result
            return fake(argv, cwd, timeout)

        report = self.run_report(dispatch, "")
        jobs = {job["name"]: job for job in report["tools"]}
        self.assertEqual((report["exit_code"], jobs["jscpd"]["status"],
                          jobs["jscpd-renamed"]["status"]), (2, "violations", "error"))
        self.assertIn("EXACT_CLONE", {d["rule"] for d in report["diagnostics"]})
        self.assertTrue(any(d.get("is_infrastructure_error") for d in report["diagnostics"]))

    def test_combined_native_invocation_reports_both_kinds(self) -> None:
        original = ("def calculate(value):\n    alpha = value + 17\n    beta = alpha * 3\n" +
                    "    gamma = beta - 4\n    delta = gamma / 2\n    return delta + 10\n")
        paths = ["src/demo/first.py", "tests/second.py", "tests/third.py"]
        for path, text in zip(paths, (original, original,
                                      original.replace("value", "amount").replace(
                                          "alpha", "start").replace("beta", "middle")
                                      .replace("gamma", "later").replace("delta", "answer")),
                              strict=True):
            (self.root / path).write_text(text, encoding="utf-8")
        output = self.root / ".eryx/combined"
        output.mkdir()
        config = output / "config.json"
        config.write_text("{}", encoding="utf-8")
        command = [binary("jscpd"), "--config", str(config), "--no-gitignore",
                   "--absolute", "--kind", "exact,renamed", "--ignore-identifiers",
                   "--ignore-literals", "--mode", "strict", "--min-lines", "5",
                   "--min-tokens", "1", "--fail-on-empty", "--reporters", "json",
                   "--output", str(output), "--exit-code", "1", *paths]
        result = normalize("jscpd", capture(command, self.root, 30), self.root,
                           output / "jscpd-report.json", paths)
        self.assertEqual(result["status"], "violations", result["diagnostics"])
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"EXACT_CLONE", "RENAMED_CLONE"})
        self.assertEqual({d["native_details"]["kind"] for d in result["diagnostics"]},
                         {"exact", "renamed"})

    def test_jscpd_parses_both_kinds_and_rejects_unknown_kind(self) -> None:
        native = self.root / "jscpd-report.json"
        fragment = {"name": str(self.root / "one.py"),
                    "startLoc": {"line": 2, "column": 3}}
        other = {"name": str(self.root / "two.py"),
                 "startLoc": {"line": 5, "column": 7}}
        clones = [{"kind": kind, "lines": 6, "tokens": 20,
                   "firstFile": fragment, "secondFile": other}
                  for kind in ("exact", "renamed")]
        native.write_text(json.dumps({"duplicates": clones, "statistics": {
            "total": {"sources": 2, "clones": 2}}}), encoding="utf-8")
        result = normalize("jscpd", native_result([], code=1), self.root,
                           native, ["one.py", "two.py"])
        self.assertEqual(result["status"], "violations")
        self.assertEqual({item["rule"] for item in result["diagnostics"]},
                         {"EXACT_CLONE", "RENAMED_CLONE"})
        self.assertEqual({(item["path"], item["line"], item["column"])
                          for item in result["diagnostics"]},
                         {("one.py", 2, 4), ("two.py", 5, 8)})
        clones[1]["kind"] = "similar"
        native.write_text(json.dumps({"duplicates": clones, "statistics": {
            "total": {"sources": 2, "clones": 2}}}), encoding="utf-8")
        invalid = normalize("jscpd", native_result([], code=1), self.root,
                            native, ["one.py", "two.py"])
        self.assertEqual(invalid["status"], "error")
        self.assertTrue(any(d["is_infrastructure_error"] for d in invalid["diagnostics"]))

    def test_jscpd_missing_malformed_and_incomplete_output_fails_closed(self) -> None:
        native = self.root / "jscpd-report.json"
        for payload in (None, "{oops", '{"duplicates":[],"statistics":{"total":' +
                        '{"sources":0,"clones":0}}}',
                        '{"duplicates":[],"statistics":{"total":' +
                        '{"sources":2,"clones":0}}}'):
            with self.subTest(payload=payload):
                if payload is None:
                    native.unlink(missing_ok=True)
                else:
                    native.write_text(payload, encoding="utf-8")
                result = normalize("jscpd", native_result([], code=0), self.root,
                                   native, ["one.py"])
                self.assertEqual(result["status"], "error")
                self.assertTrue(any(d["is_infrastructure_error"] for d in result["diagnostics"]))
        native.write_text('{"duplicates":[],"statistics":{"total":' +
                          '{"sources":1,"clones":0}}}', encoding="utf-8")
        failed = normalize("jscpd", native_result([], code=3), self.root, native, ["one.py"])
        self.assertEqual((failed["status"], failed["error"]), ("error", "JSCPD_EXECUTION_ERROR"))

    def test_jscpd_version_audit_requires_pin(self) -> None:
        versions, errors = installed_versions(self.root)
        self.assertEqual(versions["jscpd"], "5.4.0")
        self.assertFalse(errors)
        actual_version = metadata.version

        def wrong_version(name: str) -> str:
            return "0.0.0" if name == "jscpd" else actual_version(name)

        with patch("eryx.check_runner.runner_utils.metadata.version",
                   side_effect=wrong_version):
            _, errors = installed_versions(self.root)
        self.assertEqual([(item["rule"], item["is_infrastructure_error"]) for item in errors],
                         [("TOOL_VERSION_MISMATCH", True)])


if __name__ == "__main__":
    unittest.main()
