"""Independent-consumer Presidio checks using the installed analyzer and model."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _reports import normalize_presidio, sorted_diagnostics
from _presidio_policy import ENGLISH_ENTITIES, RUSSIAN_ENTITIES, PresidioPolicyError, verify_inventory
from _presidio_worker import MAX_TEXT_BYTES, analyzer, scan
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
            verify_inventory(engine)
        engine.registry.recognizers.append(removed)
        engine.registry.recognizers.append(removed)
        with self.assertRaises(PresidioPolicyError):
            verify_inventory(engine)
        engine.registry.recognizers.pop()
        with patch.object(engine.registry, "get_recognizers",
                          return_value=engine.registry.recognizers[:-1]):
            with self.assertRaises(PresidioPolicyError):
                verify_inventory(engine)
        with patch.object(engine.nlp_engine, "get_supported_entities", return_value=["PERSON"]):
            with self.assertRaises(PresidioPolicyError):
                verify_inventory(engine)
        engine.default_score_threshold = 0.5
        with self.assertRaises(PresidioPolicyError):
            verify_inventory(engine)

    def test_real_russian_and_english_findings_share_selected_text(self):
        values = ["Иван Петров", "ivan.petrov@example.com", "7707083893",
                  "112-233-445 95", "4510 123456", "219-09-9999", "192.168.1.15",
                  "12 марта 1992 года", "+7 916 123-45-67"]
        source = self.root / "review/bilingual.txt"
        source.write_text("\n".join((
            f"Контакт: {values[0]}", f"Почта: {values[1]}", f"ИНН {values[2]}",
            f"СНИЛС {values[3]}", f"Паспорт {values[4]}",
            f"US SSN {values[5]}", f"Сервер {values[6]}",
            f"Родился {values[7]}", f"Телефон {values[8]}",
            "Contact: Dr. John Smith", "Email: john.smith@example.com")) + "\n", encoding="utf-8")
        before = source.read_bytes()
        report = run_checks(self.root)
        findings = {(d["rule"], d["line"]) for d in report["diagnostics"] if d["tool"] == "presidio"}
        self.assertEqual(report["exit_code"], 1, report)
        self.assertTrue({("PERSON", 1), ("EMAIL_ADDRESS", 2), ("RU_INN", 3),
                         ("RU_SNILS", 4), ("RU_PASSPORT", 5), ("US_SSN", 6),
                         ("IP_ADDRESS", 7), ("DATE_TIME", 8), ("PHONE_NUMBER", 9),
                         ("PERSON", 10), ("EMAIL_ADDRESS", 11)} <= findings, report)
        self.assertEqual(report["coverage"]["presidio_unconfirmed"], [])
        self.assertEqual(source.read_bytes(), before)
        archived = self.root / Path(report["report_path"]).parent
        for path in [archived / "report.json", *archived.rglob("*.log"), archived / "raw/presidio.json"]:
            content = path.read_text(encoding="utf-8")
            for value in values + ["John Smith", "john.smith@example.com"]:
                self.assertNotIn(value, content, path)

    def test_russian_inventory_and_missing_mapping_fail_closed(self):
        engine = analyzer()
        self.assertEqual(set(engine.get_supported_entities(language="ru")), RUSSIAN_ENTITIES)
        ru = next(r for r in engine.registry.recognizers if r.name == "RuInnRecognizer")
        engine.registry.recognizers.remove(ru)
        with self.assertRaises(PresidioPolicyError):
            verify_inventory(engine)
        engine.registry.recognizers.append(ru)
        mapping = engine.nlp_engine.ner_model_configuration.model_to_presidio_entity_mapping
        with patch.dict(mapping, {"PER": "LOCATION"}):
            with self.assertRaises(PresidioPolicyError):
                verify_inventory(engine)
        with patch("_presidio_worker.metadata.version", side_effect=lambda name: "0" if name == "ru-core-news-sm" else
                   {"presidio-analyzer": "2.2.360", "en-core-web-sm": "3.8.0"}[name]):
            result, code = scan([str(self.root / "review/clean.txt")])
        self.assertEqual((code, result["error"], result["scanned"]),
                         (2, "PRESIDIO_MODEL_UNAVAILABLE", []))

    def test_same_line_repeated_entities_keep_distinct_columns(self):
        source = self.root / "review/repeated.txt"
        source.write_text("ivan@example.com и anna@example.com\n", encoding="utf-8")
        report = run_checks(self.root)
        emails = [d for d in report["diagnostics"] if d["tool"] == "presidio" and
                  d["rule"] == "EMAIL_ADDRESS" and d["path"] == "review/repeated.txt"]
        self.assertEqual([d["column"] for d in emails], [1, 20], report)

    def test_same_start_different_spans_are_not_collapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "native.json"
            output.write_text(json.dumps({"scanned": ["review/clean.txt"], "skipped": [], "failed": [],
                                          "findings": [{"path": "review/clean.txt", "line": 1,
                                                        "column": 1, "span_length": length,
                                                        "entity": "PERSON", "score": 0.85}
                                                       for length in (4, 8)]}), encoding="utf-8")
            result = normalize_presidio({"exit_code": 1}, self.root, output, ["review/clean.txt"])
        findings = sorted_diagnostics(result["diagnostics"])
        self.assertEqual(result["status"], "violations")
        self.assertEqual([d["span_length"] for d in findings], [4, 8])

    def test_raw_structured_text_and_notebook_physical_lines(self):
        import jsonschema

        values = ["john.smith@example.com", "ivan.petrov@example.com"]
        (self.root / "review/data.json").write_text(json.dumps({"contact": values[0]}) + "\n", encoding="utf-8")
        notebook = self.root / "review/notes.ipynb"
        notebook.write_text('{"cells": [\n {"source": ["Contact: ' + values[1] + '"]}\n]}\n', encoding="utf-8")
        before = {path: path.read_bytes() for path in (self.root / "review").rglob("*") if path.is_file()}
        report = run_checks(self.root)
        emails = {(d["path"], d["line"]) for d in report["diagnostics"]
                  if d["tool"] == "presidio" and d["rule"] == "EMAIL_ADDRESS"}
        self.assertTrue({("review/data.json", 1), ("review/notes.ipynb", 2)} <= emails, report)
        self.assertEqual(report["coverage"]["presidio_unconfirmed"], [])
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        jsonschema.validate(report, json.loads((Path(__file__).resolve().parents[1] / "report.schema.json").read_text()))
        archived = self.root / Path(report["report_path"]).parent
        for path in [archived / "report.json", *archived.rglob("*.log"), archived / "raw/presidio.json"]:
            for value in values:
                self.assertNotIn(value, path.read_text(encoding="utf-8"))

    def test_oversize_and_changed_files_are_unconfirmed_without_partial_findings(self):
        from types import SimpleNamespace

        source = self.root / "review/contact.md"
        source.write_text("john.smith@example.com\n", encoding="utf-8")
        huge = self.root / "review/huge.ipynb"
        huge.write_bytes(b"x" * (MAX_TEXT_BYTES + 1))

        class Engine:
            def analyze(self, *, text, language):
                if language == "ru":
                    source.write_text("changed during scan\n", encoding="utf-8")
                return [SimpleNamespace(start=0, end=4, entity_type="PERSON", score=0.8)]

        with patch("_presidio_worker.analyzer", return_value=Engine()):
            result, code = scan([str(source), str(huge)])
        self.assertEqual(code, 2)
        self.assertEqual(result["failed"], [str(source), str(huge)])
        self.assertEqual(result["scanned"], [])
        self.assertEqual(result["findings"], [])

    def test_unreadable_file_is_unconfirmed_without_source_bearing_error(self):
        source = str(self.root / "review/clean.txt")
        with (patch("_presidio_worker.analyzer"),
              patch("_presidio_worker.read_stable_file", side_effect=OSError("private source text"))):
            result, code = scan([source])
        self.assertEqual(code, 2)
        self.assertEqual(result["failed"], [source])
        self.assertEqual(result["scanned"], [])
        self.assertNotIn("private source text", json.dumps(result))

    def test_partial_coverage_and_malformed_payload_do_not_archive_source(self):
        secret = "john.smith@example.com"
        source = self.root / "review/contact.txt"
        source.write_text(secret, encoding="utf-8")

        def fake(argv, cwd, timeout):
            if "_presidio_worker.py" in " ".join(argv):
                Path(argv[argv.index("--output") + 1]).write_text(
                    json.dumps({"scanned": ["review/clean.txt"], "skipped": [], "failed": [],
                                "findings": [], "extra": secret}), encoding="utf-8")
                return native_result(argv, code=2, stderr=secret)
            if "--output" in argv:
                Path(argv[argv.index("--output") + 1]).write_text('{"messages": []}', encoding="utf-8")
                selected = json.loads(Path(argv[argv.index("--secrets") + 1]).read_text())
                Path(argv[argv.index("--secret-coverage") + 1]).write_text(
                    json.dumps({"scanned": selected, "skipped": []}), encoding="utf-8")
            return native_result(argv)

        report = run_checks(self.root, capture_fn=fake, versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["coverage"]["presidio_scanned"], [])
        self.assertEqual(report["coverage"]["presidio_unconfirmed"],
                         ["review/clean.txt", "review/contact.txt"])
        archived = self.root / Path(report["report_path"]).parent
        for path in [archived / "report.json", *archived.rglob("*.log"), archived / "raw/presidio.json"]:
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))

    def test_partial_worker_coverage_and_timeout_are_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "native.json"
            output.write_text(json.dumps({"scanned": ["review/clean.txt"], "skipped": [],
                                          "failed": ["review/broken.txt"], "findings": []}), encoding="utf-8")
            expected = ["review/clean.txt", "review/broken.txt"]
            partial = normalize_presidio({"exit_code": 2}, self.root, output, expected)
            self.assertEqual(partial["status"], "error")
            self.assertEqual(partial["coverage"], {"scanned": ["review/clean.txt"], "skipped": []})
            timed_out = normalize_presidio({"exit_code": -15, "error": "TOOL_TIMEOUT"}, self.root,
                                           output, expected)
            self.assertEqual(timed_out["status"], "error")
            self.assertEqual(timed_out["coverage"], {"scanned": [], "skipped": []})

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
