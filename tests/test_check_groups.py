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
            if "_presidio_worker.py" in " ".join(argv):
                selected = json.loads(Path(argv[argv.index("--manifest") + 1]).read_text())
                Path(argv[argv.index("--output") + 1]).write_text(
                     json.dumps({"scanned": selected, "skipped": [], "excluded": [], "failed": [],
                                 "findings": [], "references": []}))
                return native_result(argv)
            if "--output" in argv:
                Path(argv[argv.index("--output") + 1]).write_text('{"messages": []}', encoding="utf-8")
            if "--secret-coverage" in argv:
                selected = json.loads(Path(argv[argv.index("--secrets") + 1]).read_text())
                Path(argv[argv.index("--secret-coverage") + 1]).write_text(
                    json.dumps({"scanned": selected, "skipped": [], "excluded": []}), encoding="utf-8")
            return native_result(argv, stdout='{"generalDiagnostics": []}' if "pyright" in argv else "")
        return run_checks(self.root, capture_fn=fake, versions_fn=lambda root: ({}, []))

    def test_anonymity_only_needs_no_source_architecture_or_codestyle(self):
        self.configure(["anonymity"], ["review"])
        (self.root / "src/demo/domain/api.py").unlink()
        (self.root / ".eryx/architecture.ready.json").unlink()
        self.assertEqual(validate_project(self.root)["project"]["name"], "demo")
        report = self.run_mocked()
        self.assertEqual(report["exit_code"], 0, report)
        self.assertEqual([job["name"] for job in report["tools"]], ["presidio", "pylint-anonymity"])
        self.assertEqual(report["coverage"]["notebooks"], ["review/clean.ipynb"])
        self.assertEqual(report["coverage"]["secret_scanned"], ["review/a.txt", "review/clean.ipynb"])
        self.assertEqual(report["coverage"]["secret_count"], 2)
        self.assertEqual(report["coverage"]["presidio_count"], 2)
        self.assertNotIn("ARCHITECTURE_NOT_INITIALIZED", [item["rule"] for item in report["diagnostics"]])
        specs = dependency_specs(ERYX, ["anonymity"])
        self.assertTrue(any(spec.startswith("nbstripout==") for spec in specs))
        self.assertIn("detect-secrets==1.5.0", specs)
        self.assertTrue(any(spec.startswith("presidio-analyzer==") for spec in specs))
        self.assertTrue(any(spec.startswith("en-core-web-sm @ https://") for spec in specs))
        self.assertTrue(any(spec.startswith("ru-core-news-sm @ https://") for spec in specs))
        self.assertFalse(any(spec.startswith("tach==") for spec in specs))
        self.assertFalse(any(spec.startswith("presidio-analyzer==") or spec.startswith("en-core-web-sm @") or
                             spec.startswith("ru-core-news-sm @")
                             for spec in dependency_specs(ERYX, ["codestyle"])))

    def test_codestyle_and_both_keep_native_jobs_and_do_not_duplicate_notebooks(self):
        for groups in (["codestyle"], ["codestyle", "anonymity"]):
            with self.subTest(groups=groups):
                self.configure(groups, ["src", "tests", "review"])
                report = self.run_mocked()
                self.assertEqual(report["exit_code"], 0, report)
                self.assertEqual(len(report["tools"]), 6 + int("anonymity" in groups))
                self.assertEqual(report["coverage"]["notebooks"], ["review/clean.ipynb"])
                self.assertEqual(sum("--notebooks" in job["argv"] for job in report["tools"]), 1)
                self.assertEqual(sum("--secrets" in job["argv"] for job in report["tools"]),
                                 int("anonymity" in groups))

    def test_real_secret_scanner_reports_location_without_value_or_writing_input(self):
        self.configure(["anonymity"], ["review"])
        (self.root / "review/clean.ipynb").unlink()
        source = self.root / "review/settings.py"
        source.write_text("label = 'safe'\n", encoding="utf-8")
        before = source.read_bytes()
        clean = run_checks(self.root)
        self.assertEqual(clean["exit_code"], 0, clean)
        self.assertEqual(next(t for t in clean["tools"] if t["name"] == "pylint-anonymity")["status"], "passed", clean)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(clean["coverage"]["secret_scanned"], ["review/a.txt", "review/settings.py"])

        secret = "AKIAIOSFODNN7EXAMPLE"
        source.write_text(f"label = 'safe'\naws_access_key_id = '{secret}'\n", encoding="utf-8")
        before = source.read_bytes()
        report = run_checks(self.root)
        self.assertEqual(report["exit_code"], 1, report)
        self.assertEqual(next(t for t in report["tools"] if t["name"] == "pylint-anonymity")["status"], "violations", report)
        self.assertEqual([(item["rule"], item["path"], item["line"])
                          for item in report["diagnostics"] if item["tool"] == "pylint"],
                         [("W9803", "review/settings.py", 2)])
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(report["coverage"]["secret_scanned"], ["review/a.txt", "review/settings.py"])

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
        self.assertEqual(report["coverage"]["secret_scanned"], ["review/a.txt", "review/clean.ipynb"])

    def test_binary_inputs_are_visible_as_skipped_not_scanned(self):
        self.configure(["anonymity"], ["review"])
        (self.root / "review/clean.ipynb").unlink()
        (self.root / "review/photo.png").write_bytes(b"\x89PNG\r\n")
        before = (self.root / "review/photo.png").read_bytes()
        report = run_checks(self.root)
        self.assertEqual(report["exit_code"], 0, report)
        self.assertEqual(report["coverage"]["secret_skipped"], ["review/photo.png"])
        self.assertNotIn("review/photo.png", report["coverage"]["secret_scanned"])
        self.assertEqual((self.root / "review/photo.png").read_bytes(), before)

    def test_text_with_upstream_ignored_extension_is_scanned(self):
        self.configure(["anonymity"], ["review"])
        (self.root / "review/clean.ipynb").unlink()
        secret = "AKIAIOSFODNN7EXAMPLE"
        path = self.root / "review/style.css"
        path.write_text(f"/* aws_access_key_id = '{secret}' */\n", encoding="utf-8")
        original = path.read_bytes()
        report = run_checks(self.root)
        self.assertEqual(report["exit_code"], 1, report)
        self.assertEqual(report["coverage"]["secret_scanned"], ["review/a.txt", "review/style.css"])
        self.assertEqual([(d["rule"], d["path"]) for d in report["diagnostics"] if d["tool"] == "pylint"],
                          [("W9803", "review/style.css")])
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(path.read_bytes(), original)

    def test_zero_text_coverage_is_an_explicit_failure(self):
        self.configure(["anonymity"], ["review"])
        (self.root / "review/a.txt").unlink()
        (self.root / "review/clean.ipynb").unlink()
        (self.root / "review/photo.png").write_bytes(b"\x89PNG\r\n")
        report = run_checks(self.root)
        self.assertEqual(report["exit_code"], 2, report)
        self.assertEqual(report["coverage"]["secret_inputs"], ["review/photo.png"])
        self.assertEqual(report["coverage"]["secret_count"], 0)
        self.assertEqual(report["coverage"]["secret_skipped"], ["review/photo.png"])
        self.assertIn("EMPTY_SECRET_SCAN_SCOPE", [item["rule"] for item in report["diagnostics"]])

    def test_missing_partial_and_unexpected_scanner_results_fail_closed(self):
        self.configure(["anonymity"], ["review"])
        for mode in ("missing", "partial", "malformed", "unexpected", "nonzero"):
            with self.subTest(mode=mode):
                def fake(argv, cwd, timeout):
                    output = Path(argv[argv.index("--output") + 1])
                    output.write_text('{"messages": []}', encoding="utf-8")
                    coverage = Path(argv[argv.index("--secret-coverage") + 1])
                    if mode == "partial":
                        coverage.write_text('{"scanned": ["review/a.txt"], "skipped": [], "excluded": []}')
                    elif mode == "malformed":
                        coverage.write_text('{"scanned": "all", "skipped": [], "excluded": []}')
                    elif mode != "missing":
                        coverage.write_text(json.dumps({"scanned": ["review/a.txt", "review/clean.ipynb"],
                                                         "skipped": [], "excluded": []}))
                    result = native_result(argv, stdout="unexpected AKIAIOSFODNN7EXAMPLE" if mode == "unexpected" else "")
                    if mode == "nonzero":
                        result["exit_code"] = 4
                    return result
                report = run_checks(self.root, capture_fn=fake, versions_fn=lambda root: ({}, []))
                self.assertEqual(report["exit_code"], 2, report)
                self.assertEqual(next(t for t in report["tools"] if t["name"] == "pylint-anonymity")["status"], "error")
                self.assertNotIn("AKIAIOSFODNN7EXAMPLE", json.dumps(report))
                if mode in ("missing", "malformed"):
                    self.assertEqual(report["coverage"]["secret_count"], 0)
                if mode == "partial":
                    self.assertEqual(report["coverage"]["secret_scanned"], ["review/a.txt"])

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

    def test_combined_group_uses_manifest_for_private_doc_roots(self):
        (self.root / "tests/12a_lia").mkdir()
        (self.root / "tests/12a_lia/test_example.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / ".eryx/presidio.toml").write_text(
            'patterns = ["[0-9]{2}[a-z]_[a-z]{3}"]\n', encoding="utf-8")
        self.configure(["codestyle", "anonymity"], ["src", "tests/12a_lia"])
        configure(self.root)
        report = self.run_mocked()
        job = next(job for job in report["tools"] if job["name"] == "pydoclint")
        self.assertNotIn("12a_lia", " ".join(job["argv"]))
        self.assertTrue(job["argv"][1].endswith("_pydoclint_worker.py"))
        # The worker receives raw roots only through a consumer-owned manifest.
        run_dir = self.root / Path(report["report_path"]).parent
        self.assertEqual(json.loads((run_dir / "pydoclint-roots.json").read_text()),
                         ["src", "tests/[REDACTED]"])
        from _common import capture
        import sys

        raw = run_dir / "worker-input.json"
        raw.write_text(json.dumps(["src", "tests/12a_lia"]), encoding="utf-8")
        result = capture([sys.executable, str(ERYX / "_pydoclint_worker.py"),
                          "--manifest", str(raw), "--config", str(self.root / "pyproject.toml")], self.root, 30)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertNotIn("12a_lia", " ".join(result["argv"]))


if __name__ == "__main__":
    unittest.main()
