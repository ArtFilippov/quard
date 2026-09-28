"""Synthetic-consumer coverage for path-only archives and checkpoints."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _content_policy import content_excluded
from _presidio_worker import MAX_TEXT_BYTES, read_stable_file, scan
from _reports import normalize_presidio
from _secret_checker import SecretChecker, configure_secrets, secret_coverage
from check import run_checks
from test_infrastructure import create_repo, native_result


class ContentPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="eryx-content-policy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_repo(self.root)
        (self.root / "review").mkdir()
        (self.root / ".eryx/checks.toml").write_text(
            'groups = ["anonymity"]\nworking_directories = ["review"]\n', encoding="utf-8")

    def test_real_jobs_record_path_checked_exclusions_without_content_findings(self):
        import jsonschema

        (self.root / ".eryx/presidio.toml").write_text('seeds = ["SampleMarker"]\n', encoding="utf-8")
        names = ["assets/packed.TXT.GZ", "assets/oversize.zip", "assets/broken.tar.xz",
                 "assets/weights.pth", "assets/SampleMarker.7z"]
        (self.root / "review/assets").mkdir()
        for name in names:
            path = self.root / "review" / name
            path.write_bytes(b"AKIAIOSFODNN7EXAMPLE SampleMarker\n" if "packed" in name else
                             b"x" * (MAX_TEXT_BYTES + 1) if "oversize" in name else b"\xff\x00invalid")
        text = self.root / "review/ordinary.txt"
        text.write_text("Plain ordinary text.\n", encoding="utf-8")
        original = {path: path.read_bytes() for path in [text, *(self.root / "review" / n for n in names)]}

        report = run_checks(self.root)
        coverage = report["coverage"]
        excluded = sorted("review/" + name.replace("SampleMarker", "[REDACTED]") for name in names)
        for job in ("presidio", "secret"):
            self.assertEqual(coverage[job + "_inputs"], [*excluded, "review/ordinary.txt"])
            self.assertEqual(coverage[job + "_excluded"], excluded)
            self.assertEqual(coverage[job + "_excluded_count"], len(names))
            self.assertEqual(coverage[job + "_scanned"], ["review/ordinary.txt"])
            self.assertEqual(coverage[job + "_unconfirmed"], [])
        self.assertEqual(coverage["notebook_count"], 0)
        self.assertEqual(report["exit_code"], 1, report)
        self.assertEqual({tool["name"]: (tool["status"], tool["exit_code"]) for tool in report["tools"]},
                         {"presidio": ("passed", 0), "pylint-anonymity": ("passed", 0)})
        objects = [obj for obj in coverage["tree_objects"] if obj["kind"] == "file"]
        self.assertEqual(len(objects), len(names) + 1)
        for obj in objects:
            self.assertTrue(obj["path_checked"])
            if obj["path"] != "review/ordinary.txt":
                self.assertEqual(obj["content"], {"presidio": "excluded_by_policy",
                                                   "secret": "excluded_by_policy"})
        self.assertTrue(any(d["rule"] == "KNOWN_IDENTIFIER_SEED" and d["tool"] == "tree"
                            for d in report["diagnostics"]))
        self.assertFalse(any(d["tool"] in {"presidio", "pylint"} for d in report["diagnostics"]))
        jsonschema.validate(report, json.loads((Path(__file__).resolve().parents[1] / "report.schema.json").read_text()))
        archived = self.root / Path(report["report_path"]).parent
        for artifact in [archived / "report.json", *archived.rglob("*.json"), *archived.rglob("*.log")]:
            body = artifact.read_text(encoding="utf-8")
            self.assertNotIn("SampleMarker", body, artifact)
            self.assertNotIn("AKIAIOSFODNN7EXAMPLE", body, artifact)
        self.assertEqual(original, {path: path.read_bytes() for path in original})

    def test_workers_never_open_even_missing_excluded_files(self):
        excluded = [str(self.root / "review" / name) for name in
                    ("missing.pth", "oversized.tar.gz", "invalid.zip")]
        ordinary = self.root / "review/ordinary.txt"
        ordinary.write_text("plain text\n", encoding="utf-8")
        with (patch("_presidio_worker.analyzer") as engine,
              patch("_presidio_worker.read_stable_file", wraps=read_stable_file) as read):
            engine.return_value.analyze.return_value = []
            result, code = scan([*excluded, str(ordinary)])
        self.assertEqual((code, result["excluded"], result["scanned"]),
                         (0, excluded, [str(ordinary)]))
        read.assert_called_once_with(str(ordinary))

        configure_secrets([*excluded, str(ordinary)])
        checker = SecretChecker(MagicMock())
        with (patch("_secret_checker.Path.read_text", wraps=Path.read_text, autospec=True) as read_text,
              patch.object(checker, "add_message") as message):
            checker.open()
        self.assertEqual(secret_coverage()["excluded"], excluded)
        self.assertEqual(secret_coverage()["scanned"], [str(ordinary)])
        read_text.assert_called_once()
        message.assert_not_called()

    def test_only_excluded_inputs_are_truthful_zero_scan_not_incomplete(self):
        (self.root / "review/data.gz").write_bytes(b"invalid archive\n")
        report = run_checks(self.root)
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual({tool["name"]: tool["status"] for tool in report["tools"]},
                         {"presidio": "passed", "pylint-anonymity": "passed"})
        self.assertEqual(report["coverage"]["presidio_scanned"], [])
        self.assertEqual(report["coverage"]["secret_scanned"], [])
        self.assertEqual(report["coverage"]["presidio_unconfirmed"], [])
        self.assertEqual(report["coverage"]["secret_unconfirmed"], [])
        self.assertEqual(report["coverage"]["presidio_excluded"], ["review/data.gz"])
        self.assertEqual(report["coverage"]["secret_excluded"], ["review/data.gz"])
        self.assertEqual({d["rule"] for d in report["diagnostics"]},
                         {"EMPTY_PRESIDIO_SCAN_SCOPE", "EMPTY_SECRET_SCAN_SCOPE"})

    def test_invalid_configuration_stays_explicit_with_excluded_inputs(self):
        (self.root / "review/data.gz").write_bytes(b"invalid archive\n")
        (self.root / "review/ordinary.txt").write_text("plain text\n", encoding="utf-8")
        (self.root / ".eryx/presidio.toml").write_text('patterns = ["["]\n', encoding="utf-8")
        report = run_checks(self.root)
        self.assertIn("PRESIDIO_IDENTIFIER_CONFIG_ERROR", {d["rule"] for d in report["diagnostics"]})
        self.assertEqual(report["coverage"]["presidio_excluded"], ["[REDACTED]/[REDACTED]"])
        self.assertEqual(len(report["coverage"]["presidio_unconfirmed"]), 1)

    def test_misclassified_exclusions_and_real_text_failures_remain_errors(self):
        selected = ["review/file.txt.gz", "review/ordinary.txt"]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "worker.json"
            for label, scanned, excluded in (("missing", [selected[1]], []),
                                             ("false-scan", selected, []),
                                             ("false-exclude", [], selected)):
                with self.subTest(label=label):
                    output.write_text(json.dumps({"scanned": scanned, "skipped": [], "excluded": excluded,
                                                   "failed": [], "findings": [], "references": []}), encoding="utf-8")
                    self.assertEqual(normalize_presidio({"exit_code": 0}, self.root, output, selected)["status"],
                                     "error")
        self.assertTrue(content_excluded("x/asset.TXT.GZ"))
        self.assertFalse(content_excluded("x/ordinary.txt"))
        ordinary = self.root / selected[1]
        with patch("_presidio_worker.analyzer"), patch("_presidio_worker.read_stable_file", side_effect=OSError):
            result, code = scan([selected[0], str(ordinary)])
        self.assertEqual((code, result["excluded"], result["failed"]),
                         (2, [selected[0]], [str(ordinary)]))

    def test_broken_worker_keeps_policy_exclusions_but_not_false_text_coverage(self):
        (self.root / "review/file.txt.gz").write_bytes(b"invalid\n")
        (self.root / "review/ordinary.txt").write_text("plain text\n", encoding="utf-8")

        def broken(argv, cwd, timeout):
            if "_presidio_worker.py" in argv[1]:
                Path(argv[argv.index("--output") + 1]).write_text(
                    json.dumps({"scanned": ["review/ordinary.txt"], "skipped": [], "excluded": [],
                                 "failed": [], "findings": [], "references": []}), encoding="utf-8")
                return native_result(argv)
            Path(argv[argv.index("--output") + 1]).write_text('{"messages": []}', encoding="utf-8")
            Path(argv[argv.index("--secret-coverage") + 1]).write_text(
                json.dumps({"scanned": ["review/ordinary.txt"], "skipped": [], "excluded": []}),
                encoding="utf-8")
            return native_result(argv)

        report = run_checks(self.root, capture_fn=broken, versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["coverage"]["presidio_excluded"], ["review/file.txt.gz"])
        self.assertEqual(report["coverage"]["secret_excluded"], ["review/file.txt.gz"])
        self.assertEqual(report["coverage"]["presidio_unconfirmed"], ["review/ordinary.txt"])
        self.assertEqual(report["coverage"]["secret_unconfirmed"], ["review/ordinary.txt"])
        self.assertIn("SECRET_SCAN_INCOMPLETE", {d["rule"] for d in report["diagnostics"]})
        self.assertIn("PRESIDIO_SCAN_INCOMPLETE", {d["rule"] for d in report["diagnostics"]})


if __name__ == "__main__":
    unittest.main()
