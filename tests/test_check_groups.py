"""Check-group and directory-boundary regressions with independent consumers."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _checks import covered_files, read_checks
from _common import EryxError
from _policy import configure, read_scope
from check import run_checks
from install import dependency_specs, validate_project
from test_infrastructure import ERYX, create_repo, native_result


class CheckGroupsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="eryx-check-groups-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_repo(self.root)
        (self.root / "review").mkdir()
        (self.root / "review/a.txt").write_text("text", encoding="utf-8")
        (self.root / "review/clean.ipynb").write_text("{}", encoding="utf-8")
        (self.root / "other").mkdir()
        (self.root / "other/ignored.ipynb").write_text("{}", encoding="utf-8")

    def configure(self, groups, dirs):
        (self.root / ".eryx/checks.toml").write_text(
            f"groups = {json.dumps(groups)}\nworking_directories = {json.dumps(dirs)}\n", encoding="utf-8")

    def run_mocked(self):
        def fake(argv, cwd, timeout):
            if "--output" in argv:
                Path(argv[argv.index("--output") + 1]).write_text('{"messages": []}', encoding="utf-8")
            return native_result(argv, stdout='{"generalDiagnostics": []}' if "pyright" in argv else "")
        return run_checks(self.root, capture_fn=fake, versions_fn=lambda root: ({}, []))

    def test_anonymity_only_needs_no_source_architecture_or_codestyle(self):
        self.configure(["anonymity"], ["review"])
        (self.root / "src/demo/domain/api.py").unlink()
        (self.root / ".eryx/architecture.ready.json").unlink()
        self.assertEqual(validate_project(self.root)["project"]["name"], "demo")
        report = self.run_mocked()
        self.assertEqual(report["exit_code"], 0, report)
        self.assertEqual([job["name"] for job in report["tools"]], ["pylint-anonymity"])
        self.assertEqual(report["coverage"]["notebooks"], ["review/clean.ipynb"])
        self.assertNotIn("ARCHITECTURE_NOT_INITIALIZED", [item["rule"] for item in report["diagnostics"]])
        specs = dependency_specs(ERYX, ["anonymity"])
        self.assertTrue(any(spec.startswith("nbstripout==") for spec in specs))
        self.assertFalse(any(spec.startswith("tach==") for spec in specs))

    def test_codestyle_and_both_keep_native_jobs_and_do_not_duplicate_notebooks(self):
        for groups in (["codestyle"], ["codestyle", "anonymity"]):
            with self.subTest(groups=groups):
                self.configure(groups, ["src", "tests", "review"])
                report = self.run_mocked()
                self.assertEqual(report["exit_code"], 0, report)
                self.assertEqual(len(report["tools"]), 6)
                self.assertEqual(report["coverage"]["notebooks"], ["review/clean.ipynb"])
                self.assertEqual(sum("--notebooks" in job["argv"] for job in report["tools"]), 1)

    def test_project_root_selects_existing_codestyle_layout(self):
        self.configure(["codestyle"], ["."])
        report = self.run_mocked()
        self.assertEqual(report["exit_code"], 0, report)
        self.assertEqual(report["coverage"]["notebooks"], ["other/ignored.ipynb", "review/clean.ipynb"])

    def test_exclusions_and_overlapping_roots_deduplicate_coverage(self):
        (self.root / "review/generated").mkdir()
        (self.root / "review/generated/skip.ipynb").write_text("{}")
        self.configure(["anonymity"], ["review", "review/generated"])
        covered = covered_files(self.root, read_checks(self.root), read_scope(self.root)["exclude_directory_names"])
        self.assertEqual(covered, ["review/a.txt", "review/clean.ipynb"])
        report = self.run_mocked()
        self.assertEqual(report["coverage"]["notebooks"], ["review/clean.ipynb"])

    def test_empty_and_invalid_configuration_fail_explicitly(self):
        invalid = [(["anonymity", "unknown"], ["review"]), ([], ["review"]),
                   (["anonymity"], ["../other"]), (["anonymity"], ["missing"]),
                   (["anonymity"], []), (["codestyle"], ["review"])]
        for groups, dirs in invalid:
            with self.subTest(groups=groups, dirs=dirs):
                self.configure(groups, dirs)
                with self.assertRaises(EryxError):
                    read_checks(self.root)
                self.assertEqual(self.run_mocked()["exit_code"], 2)
        (self.root / "empty").mkdir()
        self.configure(["anonymity"], ["empty"])
        self.assertIn("EMPTY_CHECK_SCOPE", [d["rule"] for d in self.run_mocked()["diagnostics"]])

    def test_no_src_layout_is_valid_for_anonymity(self):
        self.configure(["anonymity"], ["review"])
        import shutil
        shutil.rmtree(self.root / "src")
        self.assertEqual(validate_project(self.root)["project"]["name"], "demo")
        self.assertEqual(self.run_mocked()["exit_code"], 0)

    def test_symlink_and_excluded_working_directory_are_not_silent_success(self):
        (self.root / "linked").symlink_to(self.root / "review", target_is_directory=True)
        self.configure(["anonymity"], ["linked"])
        self.assertIn("INVALID_CHECKS", [d["rule"] for d in self.run_mocked()["diagnostics"]])
        (self.root / "generated").mkdir()
        (self.root / "generated/input.ipynb").write_text("{}")
        self.configure(["anonymity"], ["generated"])
        self.assertIn("EMPTY_CHECK_SCOPE", [d["rule"] for d in self.run_mocked()["diagnostics"]])

    def test_broken_configuration_symlink_is_not_treated_as_legacy(self):
        (self.root / ".eryx/checks.toml").symlink_to(self.root / "missing.toml")
        self.assertIn("INVALID_CHECKS", [d["rule"] for d in self.run_mocked()["diagnostics"]])

    def test_unapplied_codestyle_scope_cannot_check_outside_selected_directories(self):
        self.configure(["codestyle"], ["src"])
        stale = self.run_mocked()
        self.assertEqual(stale["exit_code"], 2)
        self.assertIn("SCOPE_NOT_APPLIED", [d["rule"] for d in stale["diagnostics"]])
        self.assertEqual(stale["tools"], [])
        configure(self.root)
        report = self.run_mocked()
        self.assertEqual(report["exit_code"], 0)
        self.assertNotIn("pylint-tests", [job["name"] for job in report["tools"]])

    def test_nested_test_directory_does_not_widen_native_analyzer_scope(self):
        (self.root / "tests/selected").mkdir()
        (self.root / "tests/selected/test_selected.py").write_text("pass\n")
        self.configure(["codestyle"], ["src", "tests/selected"])
        configure(self.root)
        report = self.run_mocked()
        self.assertEqual(report["exit_code"], 0, report)
        self.assertEqual(report["coverage"]["tests"], ["tests/selected/test_selected.py"])
        pyright = json.loads((self.root / "pyrightconfig.json").read_text())
        self.assertEqual(pyright["include"], ["src", "tests/selected"])
        pydoclint = next(job for job in report["tools"] if job["name"] == "pydoclint")
        self.assertEqual(pydoclint["argv"][-2:], ["src", "tests/selected"])


if __name__ == "__main__":
    unittest.main()
