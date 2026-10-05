"""Independent job selection on isolated offline consumers."""
from __future__ import annotations

from pathlib import Path
import json
import shutil
import sys
from tempfile import mkdtemp
from typing import Any
import unittest
from collections.abc import Callable
from importlib import metadata
from unittest.mock import patch

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))
sys.path.insert(0, str(ERYX / "tests"))

from jsonschema import validate
from eryx._common import read_json
from eryx.check_runner.native_check_runner import NativeCheckRunner
from eryx.check_runner.runner_utils import installed_versions
from eryx.check_selection_utils import DUPLICATE_JOBS, OPTIONAL_JOBS
from offline_fixtures import create_repo, fake_tools


class TestCheckRunner(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path(mkdtemp(prefix="eryx-switches-"))
        self.addCleanup(shutil.rmtree, self.root)
        create_repo(self.root)

    def set_switch(self, key: str, enabled: bool) -> None:
        path = self.root / ".eryx/config.toml"
        text = path.read_text(encoding="utf-8")
        previous, updated = ("false", "true") if enabled else ("true", "false")
        path.write_text(text.replace(f"{key} = {previous}", f"{key} = {updated}"),
                        encoding="utf-8")

    def run_report(self, fake: Callable[[list[str], Path, int], dict[str, Any]],
                   selection: str | None = None) -> dict[str, Any]:
        def versions(_root: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
            return {}, []

        return NativeCheckRunner().run(self.root, lcom_files=selection, capture_fn=fake,
                                       versions_fn=versions)

    def test_each_independent_switch_skips_only_its_job_without_manifest(self):
        for key, name in OPTIONAL_JOBS.items():
            with self.subTest(key=key):
                self.set_switch(key, False)
                fake, seen = fake_tools()
                report = self.run_report(fake)
                jobs = {job["name"]: job for job in report["tools"]}
                run_dir = (self.root / report["report_path"]).parent
                self.assertEqual((report["exit_code"], jobs[name]["status"],
                                  jobs[name]["exit_code"]), (0, "skipped", None))
                self.assertNotIn(name, seen)
                self.assertFalse((run_dir / f"{name}-files.json").exists())
                self.assertEqual({other for other in OPTIONAL_JOBS.values()
                                  if jobs[other]["status"] == "skipped"}, {name})
                self.assertIn("pylint-production", seen)
                self.assertIn("pylint-tests", seen)
                if key == "lcom4":
                    self.assertEqual(report["coverage"]["lcom4"], {
                        "mode": "skipped", "requested": None,
                        "selected_files": [], "analyzed_files": []})
                self.set_switch(key, True)
                fake, seen = fake_tools()
                report = self.run_report(fake)
                self.assertIn(name, seen)
                self.assertEqual(next(job for job in report["tools"] if job["name"] == name)
                                 ["status"], "passed")
                validate(report, read_json(ERYX / "report.schema.json"))

    def test_disabled_lcom_rejects_explicit_paths_and_empty_argument(self):
        self.set_switch("lcom4", False)
        for selection in ("", "src/demo/domain/api.py"):
            with self.subTest(selection=selection):
                fake, seen = fake_tools({"pydoclint": {
                    "stderr": "src/demo/domain/api.py:1: DOC101: Native issue\n", "code": 1}})
                report = self.run_report(fake, selection)
                job = next(item for item in report["tools"] if item["name"] == "lcom4")
                self.assertEqual((report["exit_code"], job["status"], job["exit_code"]),
                                 (2, "error", None))
                self.assertEqual(report["coverage"]["lcom4"], {
                    "mode": "invalid", "requested": selection,
                    "selected_files": [], "analyzed_files": []})
                self.assertEqual({item["rule"] for item in report["diagnostics"]},
                                 {"LCOM4_DISABLED", "DOC101"})
                self.assertNotIn("lcom4", seen)
                self.assertFalse(((self.root / report["report_path"]).parent /
                                  "lcom4-files.json").exists())
                validate(report, read_json(ERYX / "report.schema.json"))

    def test_selected_job_failure_and_disabled_peer_keep_native_results(self):
        self.set_switch("class_contracts", False)
        fake, seen = fake_tools({
            "method-decorators": {"stdout": "not JSON", "code": 1},
            "pydoclint": {"stderr": "src/demo/domain/api.py:1: DOC101: Native issue\n",
                          "code": 1}})
        report = self.run_report(fake, "src/demo/domain/api.py")
        jobs = {job["name"]: job for job in report["tools"]}
        self.assertEqual((report["exit_code"], jobs["method-decorators"]["status"],
                          jobs["class-contracts"]["status"]), (2, "error", "skipped"))
        self.assertNotIn("class-contracts", seen)
        self.assertIn("DOC101", {item["rule"] for item in report["diagnostics"]})
        self.assertTrue(any(item["is_infrastructure_error"]
                            for item in report["diagnostics"]))
        self.assertEqual(report["coverage"]["lcom4"]["mode"], "selected")

    def test_old_unified_config_defaults_new_checks_on_without_upgrading(self):
        path = self.root / ".eryx/config.toml"
        text = path.read_text(encoding="utf-8")
        for key in (*OPTIONAL_JOBS, "duplicate_detection"):
            text = text.replace(f"{key} = true\n", "")
        path.write_text(text, encoding="utf-8")
        fake, seen = fake_tools()
        report = self.run_report(fake)
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(set(OPTIONAL_JOBS.values()) <= set(seen))
        self.assertTrue(set(DUPLICATE_JOBS) <= set(seen))

    def test_native_switches_skip_jobs_manifests_and_raw_output(self):
        native = {"pyright": ("pyright",), "pydoclint": ("pydoclint",),
                  "duplicate_detection": DUPLICATE_JOBS}
        for key, names in native.items():
            with self.subTest(key=key):
                self.set_switch(key, False)
                fake, seen = fake_tools()
                report = self.run_report(fake, "")
                jobs = {job["name"]: job for job in report["tools"]}
                run_dir = (self.root / report["report_path"]).parent
                self.assertEqual(report["exit_code"], 0, report["diagnostics"])
                for name in names:
                    self.assertNotIn(name, seen)
                    self.assertEqual((jobs[name]["status"], jobs[name]["exit_code"],
                                      jobs[name]["raw_output"]), ("skipped", None, {}))
                    self.assertFalse((run_dir / f"{name}-files.json").exists())
                    self.assertFalse((run_dir / "raw" / f"{name}.stdout.log").exists())
                if key == "duplicate_detection":
                    self.assertFalse((run_dir / "jscpd-config.json").exists())
                    self.assertFalse((run_dir / "raw/jscpd-report.json").exists())
                    self.assertFalse((run_dir / "raw/jscpd-renamed").exists())
                    self.assertFalse((run_dir / "raw/jscpd-similar").exists())
                self.assertIn("pylint-production", seen)
                self.assertIn("pylint-tests", seen)
                validate(report, read_json(ERYX / "report.schema.json"))
                self.set_switch(key, True)

    def test_mixed_disabled_and_enabled_native_failures_keep_diagnostics_and_versions(self):
        self.set_switch("pydoclint", False)
        self.set_switch("duplicate_detection", False)
        source = self.root / "src/demo/domain/api.py"
        fake, seen = fake_tools({"pyright": {"stdout": json.dumps({
            "generalDiagnostics": [{"file": str(source), "message": "Native issue",
                                    "severity": "error", "rule": "reportReturnType",
                                    "range": {"start": {"line": 0, "character": 0}}}]
        }), "code": 1}})
        report = self.run_report(fake)
        self.assertEqual((report["exit_code"], {d["rule"] for d in report["diagnostics"]}),
                         (1, {"reportReturnType"}))
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "pyright")
                         ["exit_code"], 1)
        self.assertNotIn("pydoclint", seen)
        self.assertTrue(all(name not in seen for name in DUPLICATE_JOBS))

        actual_version = metadata.version

        def version(name: str) -> str:
            return "0.0.0" if name == "jscpd" else actual_version(name)

        with patch("eryx.check_runner.runner_utils.metadata.version", side_effect=version):
            report = NativeCheckRunner().run(self.root, serial=True, timeout=30,
                                             lcom_files=None, capture_fn=fake,
                                             versions_fn=installed_versions)
        self.assertEqual(report["exit_code"], 2)
        self.assertIn("TOOL_VERSION_MISMATCH", {d["rule"] for d in report["diagnostics"]})
        self.assertIn("reportReturnType", {d["rule"] for d in report["diagnostics"]})

    def test_disabling_all_native_switches_keeps_mandatory_pylint(self):
        for key in ("pyright", "pydoclint", "duplicate_detection"):
            self.set_switch(key, False)
        fake, seen = fake_tools()
        report = self.run_report(fake)
        skipped = ("pyright", "pydoclint", *DUPLICATE_JOBS)
        jobs = {job["name"]: job for job in report["tools"]}
        self.assertEqual(report["exit_code"], 0, report["diagnostics"])
        self.assertTrue(all(jobs[name]["status"] == "skipped" and
                            jobs[name]["exit_code"] is None for name in skipped))
        self.assertTrue(all(name not in seen for name in skipped))
        self.assertIn("pylint-production", seen)
        self.assertIn("pylint-tests", seen)
        validate(report, read_json(ERYX / "report.schema.json"))
