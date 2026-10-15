"""Real migration worker and report checks on a disposable consumer."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from quard._common import capture
from quard.check import execute_job, make_jobs, run_checks
from quard._policy import discover_files, read_scope
from quard.check_selection_utils import validate_checks
from quard.cached_property_migration_utils import analyze
from offline_fixtures import create_repo, fake_tools, native_result


class TestCachedPropertyMigration(unittest.TestCase):
    """Check provenance, isolation, selection and infrastructure failures."""

    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        create_repo(self.root)
        self.run_dir = self.root / ".quard/reports/run"
        self.run_dir.mkdir(parents=True)

    def put(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def job(self):
        files = discover_files(self.root, read_scope(self.root))
        return next(job for job in make_jobs(self.root, self.run_dir, files, True, [])
                    if job["name"] == "cached-property-migration")

    def test_real_pipeline_finds_proven_uses_without_crossing_stub_evidence(self) -> None:
        self.put("src/demo/bridge.py", "from functools import cached_property as original\n"
                 "renamed = original\n")
        self.put("src/demo/bridge.pyi", "from qraft import cached_property as renamed\n")
        self.put("src/demo/value.py", "from demo.bridge import renamed as cache\n"
                 "import functools as fn\n"
                 "from functools import cached_property as unused\n"
                 "class Value:\n"
                 "    @cache\n    def one(self): return 1\n"
                 "    @fn.cached_property\n    def two(self): return 2\n"
                 "    three = fn.cached_property(lambda self: 3)\n")
        self.put("src/demo/value.pyi", "from demo.bridge import renamed as cache\n"
                 "class Value:\n    @cache\n    def one(self) -> int: ...\n")
        self.put("tests/test_cache.py", "from functools import cached_property as cache\n"
                 "class TestValue:\n    @cache\n    def number(self): return 1\n")
        self.put("src/demo/generated/ignored.py", "from functools import cached_property\n"
                 "class Ignored:\n    @cached_property\n    def value(self): return 1\n")
        self.put("src/demo/shadow.py", "from qraft import cached_property\n"
                 "class Other:\n    @cached_property\n    def one(self): return 1\n"
                 "    def cached_property(self, fn): return fn\n"
                 "    @cached_property\n    def two(self): return 2\n"
                 "def local(cached_property):\n    @cached_property\n"
                 "    def value(self): return 1\n    return value\n")
        setting = self.root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "duplicate_detection = true", "duplicate_detection = false"), encoding="utf-8")
        fake, _ = fake_tools()

        def mixed(argv, cwd, timeout):
            if str(QUARD / "_cached_property_migration_worker.py") in argv:
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        with patch("quard.check_runner.native_check_runner.require_source_pin"):
            report = run_checks(self.root, lcom_files="", capture_fn=mixed,
                                versions_fn=lambda _: ({}, []))
        findings = [item for item in report["diagnostics"] if item["tool"] == "cached-property-migration"]
        self.assertEqual((report["exit_code"],
                          {(item["path"], item["line"]) for item in findings},
                          all(item["column"] >= 1 and "qraft.cached_property" in item["message"]
                              for item in findings),
                          next(job["status"] for job in report["tools"]
                               if job["name"] == "cached-property-migration")),
                         (1, {("src/demo/value.py", 5), ("src/demo/value.py", 7),
                              ("src/demo/value.py", 9), ("tests/test_cache.py", 3)},
                          True, "violations"), report["diagnostics"])

    def test_disabled_job_has_no_manifest_or_worker(self) -> None:
        setting = self.root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "cached_property_migration = true", "cached_property_migration = false").replace(
            "duplicate_detection = true", "duplicate_detection = false"), encoding="utf-8")
        self.put("src/demo/bad.py", "from functools import cached_property\n"
                 "class Bad:\n    @cached_property\n    def value(self): return 1\n")
        fake, seen = fake_tools()
        with patch("quard.check_runner.native_check_runner.require_source_pin"):
            report = run_checks(self.root, capture_fn=fake, versions_fn=lambda _: ({}, []))
        job = next(item for item in report["tools"] if item["name"] == "cached-property-migration")
        self.assertEqual((job["status"], job["exit_code"], job["raw_output"],
                          (self.root / report["report_path"]).parent.joinpath(
                              "cached-property-migration-files.json").exists(),
                          "cached-property-migration" in seen, "class-contracts" in seen,
                          validate_checks({})["cached_property_migration"]),
                         ("skipped", None, {}, False, False, True, True))

    def test_stub_reexports_and_calls_in_returns_are_independent(self) -> None:
        self.put("src/demo/facade.py", "from qraft import cached_property as saved\n")
        self.put("src/demo/facade.pyi", "from functools import cached_property as saved\n")
        self.put("src/demo/consumer.py", "from demo.facade import saved\n"
                 "def descriptor(fn):\n    return saved(fn)\n")
        self.put("src/demo/consumer.pyi", "from demo.facade import saved\n"
                 "class Value:\n    @saved\n    def item(self) -> int: ...\n")
        self.put("tests/unrelated.py", "import other as functools\n"
                 "class Other:\n    @functools.cached_property\n"
                 "    def item(self): return 1\n")
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual({(item["path"], item["line"]) for item in result["diagnostics"]},
                         {("src/demo/consumer.pyi", 3)})

    def test_proven_return_call_is_positioned(self) -> None:
        self.put("src/demo/consumer.py", "from functools import cached_property as saved\n"
                 "def descriptor(fn):\n    return saved(fn)\n")
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual({(item["path"], item["line"], item["column"])
                          for item in result["diagnostics"]}, {("src/demo/consumer.py", 3, 12)})

    def test_deferred_function_sees_later_module_import(self) -> None:
        self.put("src/demo/consumer.py", "def descriptor(fn):\n    return saved(fn)\n"
                 "from functools import cached_property as saved\n")
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual({(item["path"], item["line"]) for item in result["diagnostics"]},
                         {("src/demo/consumer.py", 2)})

    def test_lambda_and_comprehension_shadowed_names_are_not_proven(self) -> None:
        self.put("src/demo/consumer.py", "from functools import cached_property as saved\n"
                 "lambda saved: saved(fn)\n"
                 "[saved(fn) for saved in values]\n"
                 "[saved(fn) for fn in values for saved in others]\n")
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["diagnostics"]), ("passed", []))

    def test_local_module_named_functools_is_not_stdlib(self) -> None:
        self.put("src/functools.py", "def cached_property(fn):\n    return fn\n")
        self.put("src/demo/consumer.py", "from functools import cached_property\n"
                 "class Value:\n    @cached_property\n    def item(self): return 1\n")
        files = discover_files(self.root, read_scope(self.root))
        self.assertEqual(analyze(self.root, files["production"]), ([], None))

    def test_parse_failure_keeps_other_file_findings(self) -> None:
        self.put("src/demo/proven.py", "from functools import cached_property\n"
                 "class Value:\n    @cached_property\n    def value(self): return 1\n")
        self.put("tests/broken.pyi", "def broken(\n")
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"],
                          {item["rule"] for item in result["diagnostics"]}),
                         ("error", 2, {"CACHED_PROPERTY_MIGRATION_PARSE_ERROR",
                                       "FUNCTOOLS_CACHED_PROPERTY"}))

    def test_malformed_reports_are_errors(self) -> None:
        job = self.job()
        statuses = []
        for text, code in (("bad JSON", 0), (json.dumps({
                "quard_cached_property_migration_adapter": 1, "diagnostics": [],
                "execution_error": None}), 1)):
            result = execute_job(job, self.root, self.run_dir, 30,
                                 capture_fn=lambda argv, cwd, timeout: native_result(
                                     argv, stdout=text, code=code))
            statuses.append(result["status"])
        self.assertEqual(statuses, ["error", "error"])

    def test_invalid_manifest_is_error(self) -> None:
        job = self.job()
        Path(job["argv"][-1]).write_text('["../escape.py"]', encoding="utf-8")
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
