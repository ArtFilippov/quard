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
from _presidio_seeds import latin_ranges, seed_spans
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

    def test_real_contextual_identifiers_report_only_account_positions(self):
        lines = [
            'root = "/home/mira_k/work"',
            'cache = "/scratch/Соколова/results"',
            'input = "/Users/Alex-7/image.png"',
            'ssh -p 2222 build_8@compute.example',
            'scp ./file.py otto@cluster.example:/work/file.py',
            'scp nina@cluster.example:results.txt ./results.txt',
            'sftp -P 2222 ivan@host.example',
            'ordinary = "/usr/local/share"',
            'temporary = "/tmp/build_8/file"',
            'shared = "/home/shared/data"',
            'site = "https://host.example/home/person/page"',
            'echo dev@host.example',
            'ssh host.example',
            'scp ./local.txt ./copy.txt',
            'sftp host.example',
            'ssh host.example && echo visitor@host.example',
        ]
        source = self.root / "review/context.txt"
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")
        binary = self.root / "review/image.png"
        binary.write_bytes(b"\x89PNG\0")
        before = source.read_bytes()
        report = run_checks(self.root)
        presidio = next(tool for tool in report["tools"] if tool["name"] == "presidio")
        self.assertEqual((report["exit_code"], presidio["status"], presidio["exit_code"]),
                         (1, "violations", 1), report)
        expected = [("LOCAL_PATH_IDENTIFIER", 1, "mira_k"),
                    ("LOCAL_PATH_IDENTIFIER", 2, "Соколова"),
                    ("LOCAL_PATH_IDENTIFIER", 3, "Alex-7"),
                    ("REMOTE_ACCOUNT", 4, "build_8"),
                    ("REMOTE_ACCOUNT", 5, "otto"),
                    ("REMOTE_ACCOUNT", 6, "nina"),
                    ("REMOTE_ACCOUNT", 7, "ivan")]
        actual = {(d["rule"], d["line"], d["column"], d["span_length"])
                  for d in report["diagnostics"] if d["tool"] == "presidio" and
                  d["rule"] in {"LOCAL_PATH_IDENTIFIER", "REMOTE_ACCOUNT"}}
        self.assertEqual(actual, {(entity, line, lines[line - 1].index(value) + 1, len(value))
                                  for entity, line, value in expected})
        self.assertEqual(report["coverage"]["presidio_scanned"],
                         ["review/clean.txt", "review/context.txt"])
        self.assertEqual(report["coverage"]["presidio_skipped"], ["review/image.png"])
        self.assertEqual(report["coverage"]["presidio_unconfirmed"], [])
        self.assertEqual(source.read_bytes(), before)
        archived = self.root / Path(report["report_path"]).parent
        for path in [archived / "report.json", *archived.rglob("*.log"), archived / "raw/presidio.json"]:
            content = path.read_text(encoding="utf-8")
            for _, _, value in expected:
                self.assertNotIn(value, content, path)

    def test_real_consumer_seeds_find_every_literal_occurrence_without_leaking_values(self):
        seeds = ("СидОр", "İd", "Secret-branch")
        (self.root / ".eryx/presidio.toml").write_text(
            'seeds = ["СидОр", "İd", "Secret-branch"]\n', encoding="utf-8")
        lines = ['# сидор and СИДОР', 'tag = "preİDpost"',
                 'path = "/unusual/secret-BRANCH/file"',
                 'ssh secret-branch@host.example', 'other = "not identified"']
        source = self.root / "review/seeds.txt"
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")
        before = source.read_bytes()
        report = run_checks(self.root)
        presidio = next(tool for tool in report["tools"] if tool["name"] == "presidio")
        self.assertEqual((report["exit_code"], presidio["status"], presidio["exit_code"]),
                         (1, "violations", 1), report)
        actual = {(d["line"], d["column"], d["span_length"])
                  for d in report["diagnostics"] if d["rule"] == "KNOWN_IDENTIFIER_SEED"}
        self.assertEqual(actual, {(1, 3, 5), (1, 13, 5), (2, 11, 2),
                                  (3, 18, 13), (4, 5, 13)})
        self.assertEqual(report["coverage"]["presidio_scanned"],
                         ["review/clean.txt", "review/seeds.txt"])
        self.assertEqual(report["coverage"]["presidio_unconfirmed"], [])
        self.assertEqual(source.read_bytes(), before)
        archived = self.root / Path(report["report_path"]).parent
        for path in [archived / "report.json", *archived.rglob("*.log"), archived / "raw/presidio.json"]:
            content = path.read_text(encoding="utf-8")
            for value in ("СидОр", "сидор", "СИДОР", "İd", "İD", "Secret-branch", "secret-BRANCH"):
                self.assertNotIn(value, content, path)
        self.assertFalse(any(seed in " ".join(tool["argv"]) for seed in seeds for tool in report["tools"]))

    def test_seed_offsets_survive_casefold_expansion_and_overlap(self):
        self.assertEqual(seed_spans("aİb ß SS", ("İ", "i", "ss")),
                         [(1, 2), (4, 5), (6, 8)])

    def test_real_patterns_and_literals_are_distinct_and_value_free(self):
        config = self.root / ".eryx/presidio.toml"
        expression = r"[0-9]{2}[a-z]_[a-z]{3}"
        config.write_text('seeds = ["a.b"]\npatterns = ["' + expression + '", "сокол", '
                          '"\\\\u0441\\\\u043e\\\\u043a\\\\u043e\\\\u043b"]\n',
                          encoding="utf-8")
        lines = ['# 12a_lia and 12A_LIA', 'default = "/models/12a_lia.pth"',
                 'tag = "pre12A_LIApost a.b a-b"', 'other = "9a_lia 12a_li 12K_lia 12İ_lia"',
                 'alias = "СОКОЛ"']
        source = self.root / "review/known.txt"
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")
        before = source.read_bytes()
        report = run_checks(self.root)
        self.assertEqual(report["exit_code"], 1, report)
        actual = {(d["rule"], d["line"], d["column"], d["span_length"])
                  for d in report["diagnostics"] if d["path"] == "review/known.txt" and
                  d["rule"] in {"KNOWN_IDENTIFIER_SEED", "KNOWN_IDENTIFIER_PATTERN"}}
        expected = {("KNOWN_IDENTIFIER_PATTERN", line, text.index(value) + 1, len(value))
                    for line, text, value in ((1, lines[0], "12a_lia"), (2, lines[1], "12a_lia"),
                                              (3, lines[2], "12A_LIA"))}
        expected.add(("KNOWN_IDENTIFIER_PATTERN", 1, lines[0].index("12A_LIA") + 1, 7))
        expected.add(("KNOWN_IDENTIFIER_PATTERN", 5, lines[4].index("СОКОЛ") + 1, 5))
        expected.add(("KNOWN_IDENTIFIER_SEED", 3, lines[2].index("a.b") + 1, 3))
        self.assertEqual(actual, expected)
        self.assertEqual(source.read_bytes(), before)
        archived = self.root / Path(report["report_path"]).parent
        for path in [archived / "report.json", *archived.rglob("*.log"), archived / "raw/presidio.json"]:
            contents = path.read_text(encoding="utf-8")
            for value in (expression, "12a_lia", "12A_LIA", "a.b", "сокол", "СОКОЛ"):
                self.assertNotIn(value, contents, path)
        self.assertFalse(any(expression in " ".join(tool["argv"]) for tool in report["tools"]))

    def test_pattern_only_empty_and_missing_configuration_preserve_context(self):
        config = self.root / ".eryx/presidio.toml"
        config.write_text('patterns = ["[0-9]{2}[a-z]_[a-z]{3}"]\n', encoding="utf-8")
        source = self.root / "review/known.txt"
        source.write_text('tag = "12a_lia"\npath = "/home/marker/work"\n', encoding="utf-8")
        report = run_checks(self.root)
        self.assertTrue({"KNOWN_IDENTIFIER_PATTERN", "LOCAL_PATH_IDENTIFIER"} <=
                        {d["rule"] for d in report["diagnostics"]})
        config.write_text("seeds = []\npatterns = []\n", encoding="utf-8")
        report = run_checks(self.root)
        self.assertNotIn("KNOWN_IDENTIFIER_PATTERN", {d["rule"] for d in report["diagnostics"]})
        self.assertIn("LOCAL_PATH_IDENTIFIER", {d["rule"] for d in report["diagnostics"]})

    def test_latin_range_rewrite_does_not_change_escaped_literals(self):
        self.assertEqual(latin_ranges(r"\[a-z] [a-z] \\ (?# [a-z]) [A-Z]"),
                         r"\[a-z] (?a:[a-z]) \\ (?# [a-z]) (?a:[A-Z])")
        config = self.root / ".eryx/presidio.toml"
        config.write_text('patterns = ["(?# [a-z])x"]\n', encoding="utf-8")
        (self.root / "review/known.txt").write_text("x\n", encoding="utf-8")
        report = run_checks(self.root)
        self.assertIn("KNOWN_IDENTIFIER_PATTERN", {d["rule"] for d in report["diagnostics"]})

    def test_empty_and_missing_seeds_keep_general_policy_active(self):
        self.assertEqual(run_checks(self.root)["exit_code"], 0)
        (self.root / ".eryx/presidio.toml").write_text("seeds = []\n", encoding="utf-8")
        (self.root / "review/known.txt").write_text('path = "/home/marker/work"\n', encoding="utf-8")
        report = run_checks(self.root)
        self.assertEqual(next(tool for tool in report["tools"] if tool["name"] == "presidio")["status"],
                         "violations", report)
        self.assertIn("LOCAL_PATH_IDENTIFIER", {d["rule"] for d in report["diagnostics"]})

    def test_invalid_identifier_config_is_value_free_and_preserves_independent_jobs(self):
        config = self.root / ".eryx/presidio.toml"
        for invalid in ('seeds = ["private-marker", 5]\n',
                        'seeds = ["private-marker"\n',
                        'seeds = ["private-marker"]\nextra = true\n',
                        'seeds = [""]\n',
                        'patterns = ["private-marker", 5]\n',
                        'patterns = ["private-marker", "["]\n',
                        'patterns = ["private-marker", "' + '(' * 600 + '"]\n',
                        'patterns = ["private-marker"]\nother = true\n',
                        'patterns = [" "]\n'):
            with self.subTest(invalid=invalid):
                config.write_text(invalid, encoding="utf-8")
                report = run_checks(self.root)
                presidio = next(tool for tool in report["tools"] if tool["name"] == "presidio")
                secret = next(tool for tool in report["tools"] if tool["name"] == "pylint-anonymity")
                self.assertEqual((report["exit_code"], presidio["status"], presidio["exit_code"]),
                                 (2, "error", 2), report)
                self.assertEqual(secret["status"], "passed", report)
                # Without a valid identifier configuration, path display fails
                # closed, but coverage still records the unconfirmed object.
                self.assertEqual(report["coverage"]["presidio_unconfirmed"],
                                 ["[REDACTED]/[REDACTED]"])
                expected_error = ("PRESIDIO_IDENTIFIER_CONFIG_ERROR" if invalid.startswith("patterns") else
                                  "PRESIDIO_SEED_CONFIG_ERROR")
                self.assertIn(expected_error, {d["rule"] for d in report["diagnostics"]})
                archived = self.root / Path(report["report_path"]).parent
                for path in [archived / "report.json", *archived.rglob("*.log"), archived / "raw/presidio.json"]:
                    self.assertNotIn("private-marker", path.read_text(encoding="utf-8"))

    def test_contextual_inventory_is_active_in_each_language(self):
        engine = analyzer()
        for language in ("en", "ru"):
            for name in ("LocalPathRecognizer", "RemoteAccountRecognizer", "SeedRecognizer",
                         "IdentifierPatternRecognizer"):
                recognizer = next(r for r in engine.registry.recognizers
                                  if r.name == name and r.supported_language == language)
                engine.registry.recognizers.remove(recognizer)
                with self.assertRaises(PresidioPolicyError):
                    verify_inventory(engine)
                engine.registry.recognizers.append(recognizer)
        with patch.object(engine.registry, "get_recognizers", return_value=[]):
            with self.assertRaises(PresidioPolicyError):
                verify_inventory(engine)

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
            output.write_text(json.dumps({"scanned": ["review/clean.txt"], "skipped": [], "excluded": [], "failed": [],
                                           "references": [], "findings": [{"path": "review/clean.txt", "line": 1,
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
                    json.dumps({"scanned": ["review/clean.txt"], "skipped": [], "excluded": [], "failed": [],
                                "findings": [], "extra": secret}), encoding="utf-8")
                return native_result(argv, code=2, stderr=secret)
            if "--output" in argv:
                Path(argv[argv.index("--output") + 1]).write_text('{"messages": []}', encoding="utf-8")
                selected = json.loads(Path(argv[argv.index("--secrets") + 1]).read_text())
                Path(argv[argv.index("--secret-coverage") + 1]).write_text(
                    json.dumps({"scanned": selected, "skipped": [], "excluded": []}), encoding="utf-8")
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
            output.write_text(json.dumps({"scanned": ["review/clean.txt"], "skipped": [], "excluded": [],
                                           "failed": ["review/broken.txt"], "findings": [],
                                           "references": []}), encoding="utf-8")
            expected = ["review/clean.txt", "review/broken.txt"]
            partial = normalize_presidio({"exit_code": 2}, self.root, output, expected)
            self.assertEqual(partial["status"], "error")
            self.assertEqual(partial["coverage"], {"scanned": ["review/clean.txt"], "skipped": [], "excluded": []})
            timed_out = normalize_presidio({"exit_code": -15, "error": "TOOL_TIMEOUT"}, self.root,
                                           output, expected)
            self.assertEqual(timed_out["status"], "error")
            self.assertEqual(timed_out["coverage"], {"scanned": [], "skipped": [], "excluded": []})

    def test_binary_headers_are_skipped_without_claiming_text_coverage(self):
        (self.root / "review/photo.png").write_bytes(b"\x89PNG\r\n\0")
        (self.root / "review/.DS_Store").write_bytes(b"\0\0\0\x01Bud1")
        (self.root / "review/weights.pth").write_bytes(b"PK\x03\x04\xff\xfe")
        from unittest.mock import patch
        paths = [str(self.root / "review" / name) for name in (".DS_Store", "photo.png", "weights.pth")]
        with patch("_presidio_worker.analyzer"):
            report, status = scan(paths)
        self.assertEqual(status, 2)
        self.assertEqual(report["skipped"], paths[:2])
        self.assertEqual(report["excluded"], paths[2:])
        self.assertEqual(report["scanned"], [])

    def test_unconfirmed_presidio_does_not_cancel_other_jobs(self):
        def fake(argv, cwd, timeout):
            if "_presidio_worker.py" in " ".join(argv):
                Path(argv[argv.index("--output") + 1]).write_text(
                     '{"scanned": [], "skipped": [], "excluded": [], "failed": [], "findings": [], "references": []}', encoding="utf-8")
            elif "--output" in argv:
                Path(argv[argv.index("--output") + 1]).write_text('{"messages": []}', encoding="utf-8")
                selected = json.loads(Path(argv[argv.index("--secrets") + 1]).read_text())
                Path(argv[argv.index("--secret-coverage") + 1]).write_text(
                    json.dumps({"scanned": selected, "skipped": [], "excluded": []}), encoding="utf-8")
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
            for payload in ('{"scanned": ["review/clean.txt"], "skipped": [], "excluded": [], "failed": [], '
                            '"findings": [{"path": "review/clean.txt", "line": 1, '
                            '"entity": "PERSON", "score": 0.7, "value": "private"}]}',
                            '{"scanned": [], "skipped": [], "excluded": [], "failed": [], "findings": [], '
                             '"error": "PRESIDIO_MODEL_UNAVAILABLE"}',
                             '{"scanned": [], "skipped": [], "excluded": [], "failed": [], "findings": [], '
                             '"error": "PRESIDIO_POLICY_MISMATCH"}'):
                output.write_text(payload, encoding="utf-8")
                result = normalize_presidio({"exit_code": 2, "stderr": "private", "stdout": ""},
                                            self.root, output, ["review/clean.txt"])
                self.assertEqual(result["status"], "error")
                self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
