"""Independent-consumer Presidio checks using the installed analyzer and model."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _reports import normalize_presidio
from _presidio_policy import ENGLISH_ENTITIES, PresidioPolicyError, verify_english_inventory
from _presidio_worker import analyzer, scan
from check import execute_job, run_checks
from test_infrastructure import create_repo, native_result


class PresidioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="eryx-presidio-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_repo(self.root)
        (self.root / "review").mkdir()
        (self.root / "review/generated").mkdir()
        (self.root / ".eryx/checks.toml").write_text(
            'groups = ["anonymity"]\nworking_directories = ["review", "review/generated"]\n', encoding="utf-8")
        (self.root / "review/clean.txt").write_text("A plain test file.\n", encoding="utf-8")

    def test_real_findings_clean_scope_and_archived_output(self):
        (self.root / "review/generated/hidden.txt").write_text("Dr. John Smith\n", encoding="utf-8")
        self.assertEqual(run_checks(self.root)["exit_code"], 0)
        person, email = "Dr. John Smith", "john.smith@example.com"
        source = self.root / "review/contact.md"
        source.write_text(f"Header\nReach {person} here\nEmail: {email}\n", encoding="utf-8")
        original = source.read_bytes()
        report = run_checks(self.root)
        self.assertEqual(report["exit_code"], 1, report)
        self.assertEqual(next(tool for tool in report["tools"] if tool["name"] == "presidio")["status"],
                         "violations")
        self.assertTrue({("PERSON", "review/contact.md", 2), ("EMAIL_ADDRESS", "review/contact.md", 3)} <=
                        {(d["rule"], d["path"], d["line"]) for d in report["diagnostics"]})
        self.assertEqual(report["coverage"]["presidio_scanned"],
                         ["review/clean.txt", "review/contact.md"])
        self.assertEqual(report["coverage"]["presidio_skipped"], [])
        self.assertEqual(report["coverage"]["presidio_unconfirmed"], [])
        self.assertEqual(source.read_bytes(), original)
        archived = self.root / Path(report["report_path"]).parent
        for path in [archived / "report.json", *archived.rglob("*.log"), archived / "raw/presidio.json"]:
            content = path.read_text(encoding="utf-8")
            self.assertNotIn(person, content, path)
            self.assertNotIn(email, content, path)

    def test_real_standard_english_entities_include_low_confidence_identifiers(self):
        samples = ["Dr. John Smith", "john.smith@example.com", "Born on 12 March 1992",
                   "See https://example.com/news", "Server 192.168.1.15",
                   "US SSN 219-09-9999", "UK NHS number 943 476 5919",
                   "Credit card 4111 1111 1111 1111", "IBAN GB82 WEST 1234 5698 7654 32",
                   "US passport number 123456789"]
        source = self.root / "review/identifiers.txt"
        source.write_text("\n".join(samples) + "\n", encoding="utf-8")
        original = source.read_bytes()
        report = run_checks(self.root)
        expected = {("PERSON", 1), ("EMAIL_ADDRESS", 2), ("DATE_TIME", 3), ("URL", 4),
                    ("IP_ADDRESS", 5), ("US_SSN", 6), ("UK_NHS", 7), ("CREDIT_CARD", 8),
                    ("IBAN_CODE", 9), ("US_PASSPORT", 10), ("US_BANK_NUMBER", 10),
                    ("US_DRIVER_LICENSE", 10)}
        presidio = next(tool for tool in report["tools"] if tool["name"] == "presidio")
        self.assertEqual(presidio["status"], "violations", report)
        self.assertEqual(report["exit_code"], 1, report)
        self.assertTrue(expected <= {(d["rule"], d["line"]) for d in report["diagnostics"]
                                     if d["tool"] == "presidio"}, report)
        self.assertEqual(report["coverage"]["presidio_unconfirmed"], [])
        self.assertEqual(source.read_bytes(), original)
        archived = self.root / Path(report["report_path"]).parent
        for path in [archived / "report.json", *archived.rglob("*.log"), archived / "raw/presidio.json"]:
            content = path.read_text(encoding="utf-8")
            for value in ("John Smith", "john.smith@example.com", "12 March 1992",
                          "https://example.com/news", "192.168.1.15", "219-09-9999",
                          "943 476 5919", "4111 1111 1111 1111", "GB82 WEST 1234 5698 7654 32",
                          "123456789"):
                self.assertNotIn(value, content, path)

    def test_real_inventory_rejects_missing_extra_and_disabled_recognizers(self):
        engine = analyzer()
        self.assertEqual(set(engine.get_supported_entities(language="en")), ENGLISH_ENTITIES)
        removed = engine.registry.recognizers.pop()
        with self.assertRaises(PresidioPolicyError):
            verify_english_inventory(engine)
        engine.registry.recognizers.append(removed)
        engine.registry.recognizers.append(removed)
        with self.assertRaises(PresidioPolicyError):
            verify_english_inventory(engine)
        engine.registry.recognizers.pop()
        with patch.object(engine.registry, "get_recognizers",
                          return_value=engine.registry.recognizers[:-1]):
            with self.assertRaises(PresidioPolicyError):
                verify_english_inventory(engine)
        with patch.object(engine.nlp_engine, "get_supported_entities", return_value=["PERSON"]):
            with self.assertRaises(PresidioPolicyError):
                verify_english_inventory(engine)
        engine.default_score_threshold = 0.5
        with self.assertRaises(PresidioPolicyError):
            verify_english_inventory(engine)

    def test_binary_headers_are_skipped_without_claiming_text_coverage(self):
        (self.root / "review/photo.png").write_bytes(b"\x89PNG\r\n\0")
        (self.root / "review/.DS_Store").write_bytes(b"\0\0\0\x01Bud1")
        (self.root / "review/weights.pth").write_bytes(b"PK\x03\x04\xff\xfe")
        from unittest.mock import patch
        paths = [str(self.root / "review" / name) for name in (".DS_Store", "photo.png", "weights.pth")]
        with patch("_presidio_worker.analyzer"):
            report, status = scan(paths)
        self.assertEqual(status, 2)
        self.assertEqual(report["skipped"], paths)
        self.assertEqual(report["scanned"], [])

    def test_unconfirmed_presidio_does_not_cancel_other_jobs(self):
        def fake(argv, cwd, timeout):
            if "_presidio_worker.py" in " ".join(argv):
                Path(argv[argv.index("--output") + 1]).write_text(
                    '{"scanned": [], "skipped": [], "failed": [], "findings": []}', encoding="utf-8")
            elif "--output" in argv:
                Path(argv[argv.index("--output") + 1]).write_text('{"messages": []}', encoding="utf-8")
                selected = json.loads(Path(argv[argv.index("--secrets") + 1]).read_text())
                Path(argv[argv.index("--secret-coverage") + 1]).write_text(
                    json.dumps({"scanned": selected, "skipped": []}), encoding="utf-8")
            return native_result(argv)

        report = run_checks(self.root, capture_fn=fake, versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual({tool["name"]: tool["status"] for tool in report["tools"]},
                         {"presidio": "error", "pylint-anonymity": "passed"})
        self.assertEqual(report["coverage"]["presidio_unconfirmed"], ["review/clean.txt"])

    def test_worker_exception_cannot_enter_report(self):
        from unittest.mock import patch

        def interrupted(job, root, run_dir, timeout, capture_fn):
            if job["name"] == "presidio":
                raise RuntimeError("john.smith@example.com")
            return execute_job(job, root, run_dir, timeout, capture_fn)

        with patch("check.execute_job", side_effect=interrupted):
            report = run_checks(self.root)
        self.assertEqual(report["exit_code"], 2)
        self.assertNotIn("john.smith@example.com", json.dumps(report))
        self.assertEqual(report["coverage"]["presidio_unconfirmed"], ["review/clean.txt"])
        self.assertEqual(next(tool for tool in report["tools"] if tool["name"] == "pylint-anonymity")["status"],
                         "passed")

    def test_malformed_output_and_missing_model_are_failures_without_raw_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "native.json"
            for payload in ('{"scanned": ["review/clean.txt"], "skipped": [], "failed": [], '
                            '"findings": [{"path": "review/clean.txt", "line": 1, '
                            '"entity": "PERSON", "score": 0.7, "value": "private"}]}',
                            '{"scanned": [], "skipped": [], "failed": [], "findings": [], '
                             '"error": "PRESIDIO_MODEL_UNAVAILABLE"}',
                             '{"scanned": [], "skipped": [], "failed": [], "findings": [], '
                             '"error": "PRESIDIO_POLICY_MISMATCH"}'):
                output.write_text(payload, encoding="utf-8")
                result = normalize_presidio({"exit_code": 2, "stderr": "private", "stdout": ""},
                                            self.root, output, ["review/clean.txt"])
                self.assertEqual(result["status"], "error")
                self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
