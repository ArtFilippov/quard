"""Independent-consumer regressions for complete, value-free path coverage."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _tree_inventory import inventory
from check import run_checks
from test_infrastructure import create_repo


class TreeInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="12a_lia-checkout-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_repo(self.root)
        (self.root / "review").mkdir()
        (self.root / ".eryx/checks.toml").write_text(
            'groups = ["anonymity"]\nworking_directories = ["review"]\n', encoding="utf-8")
        (self.root / ".eryx/presidio.toml").write_text(
            'seeds = ["СидОр", "a.b"]\npatterns = ["[0-9]{2}[a-z]_[a-z]{3}"]\n', encoding="utf-8")
        (self.root / "review/clean.txt").write_text("ordinary text\n", encoding="utf-8")

    def test_complete_tree_and_content_coverage_are_distinct_and_safe(self):
        review = self.root / "review"
        (review / "СИДОР").mkdir()
        (review / "СИДОР/12A_LIA.txt").write_text("ordinary text\n", encoding="utf-8")
        (review / ".ignored").mkdir()
        (review / ".ignored/a.b.bin").write_bytes(b"\x00\xff")
        (review / "generated").mkdir()
        (review / "generated/12a_lia.txt").write_text("outside text scope\n", encoding="utf-8")
        for name in ("12a_lia.txt", "12A_LIA.txt"):
            (review / name).write_text("mail: ivy@example.com\n", encoding="utf-8")
        for folder in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                       ".ipynb_checkpoints"):
            (review / folder).mkdir()
        (review / "stray.pyc").write_bytes(b"\0")
        (review / "stray.pyo").write_bytes(b"\0")
        (review / ".DS_Store").write_bytes(b"\0")
        (review / "outside").symlink_to(self.root / ".eryx", target_is_directory=True)
        (review / "loop").symlink_to(review, target_is_directory=True)
        originals = {path: path.read_bytes() for path in review.rglob("*") if path.is_file()}

        report = run_checks(self.root)
        self.assertEqual(report["exit_code"], 1, report)
        coverage = report["coverage"]
        objects = {obj["path"]: obj for obj in coverage["tree_objects"]}
        self.assertEqual(coverage["tree_count"], len(coverage["tree_objects"]))
        self.assertEqual(coverage["tree_incomplete_count"], 0)
        self.assertEqual(coverage["tree_count"], 19)
        self.assertEqual(len(objects), 18)  # two differently cased names share one display
        collision = [obj for obj in coverage["tree_objects"] if obj["path"] == "review/[REDACTED].txt"]
        self.assertEqual(len({obj["object_id"] for obj in collision}), 2)
        self.assertEqual({obj["content"]["presidio"] for obj in collision}, {"scanned"})
        mail = [d for d in report["diagnostics"] if d["rule"] == "EMAIL_ADDRESS" and
                d["path"] == "review/[REDACTED].txt"]
        self.assertEqual(len({d["object_id"] for d in mail}), 2)
        self.assertEqual(len({d["fingerprint"] for d in mail}), 2)
        self.assertEqual(objects["review/loop"]["kind"], "symlink")
        self.assertEqual(objects["review/outside"]["kind"], "symlink")
        self.assertEqual(objects["review/__pycache__"]["kind"], "directory")
        self.assertIn("review/[REDACTED]/[REDACTED].txt", objects)
        self.assertIn("review/.ignored/[REDACTED].bin", objects)
        self.assertEqual(objects["review/generated/[REDACTED].txt"]["content"],
                         {"presidio": "not_selected", "secret": "not_selected"})
        rules = {(d["rule"], d["path"]) for d in report["diagnostics"] if d["tool"] == "tree"}
        for folder in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                       ".ipynb_checkpoints"):
            self.assertIn(("CACHE_DIRECTORY", "review/" + folder), rules)
        for name in ("stray.pyc", "stray.pyo"):
            self.assertIn(("BYTECODE_FILE", "review/" + name), rules)
        self.assertIn(("DS_STORE", "review/.DS_Store"), rules)
        self.assertTrue({"review/.ignored/[REDACTED].bin", "review/[REDACTED]/[REDACTED].txt"} <=
                        set(coverage["presidio_inputs"]))
        tree_findings = [d for d in report["diagnostics"] if d["tool"] == "tree" and
                         d["rule"].startswith("KNOWN_")]
        self.assertEqual({(d["rule"], d["path"], d["column"], d["span_length"]) for d in tree_findings},
                         {("KNOWN_IDENTIFIER_SEED", "review/[REDACTED]", 8, 5),
                          ("KNOWN_IDENTIFIER_SEED", "review/[REDACTED]/[REDACTED].txt", 8, 5),
                          ("KNOWN_IDENTIFIER_PATTERN", "review/[REDACTED]/[REDACTED].txt", 14, 7),
                          ("KNOWN_IDENTIFIER_SEED", "review/.ignored/[REDACTED].bin", 17, 3),
                          ("KNOWN_IDENTIFIER_PATTERN", "review/generated/[REDACTED].txt", 18, 7),
                          ("KNOWN_IDENTIFIER_PATTERN", "review/[REDACTED].txt", 8, 7)})
        self.assertTrue(all(d["object_id"] in {obj["object_id"] for obj in coverage["tree_objects"]}
                            for d in tree_findings))
        self.assertTrue(all(d["object_id"] in {obj["object_id"] for obj in coverage["tree_objects"]}
                            for d in report["diagnostics"] if d["tool"] == "presidio" and d["path"]))
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        import jsonschema
        jsonschema.validate(report, json.loads((Path(__file__).resolve().parents[1] /
                                                "report.schema.json").read_text()))
        archive = self.root / Path(report["report_path"]).parent
        for artifact in [archive / "report.json", *archive.rglob("*.json"), *archive.rglob("*.log")]:
            text = artifact.read_text(encoding="utf-8")
            for private in ("СИДОР", "12A_LIA", "12a_lia", "a.b"):
                self.assertNotIn(private, text, artifact)
            self.assertNotIn(str(self.root), text, artifact)
        self.assertFalse(any(d["rule"] == "KNOWN_IDENTIFIER_PATTERN" and
                             d["path"] == "review/clean.txt" for d in report["diagnostics"]))

    def test_enclosing_checkout_is_not_inspected_and_empty_excluded_dir_is(self):
        (self.root / "review/__pycache__").mkdir()
        public, findings, _, incomplete = inventory(self.root, ["review"], "test")
        self.assertEqual((len(public), incomplete), (2, 0))
        self.assertEqual([d["rule"] for d in findings], ["CACHE_DIRECTORY"])

    def test_traversal_failure_is_explicit_without_exception_text(self):
        (self.root / "review/blocked").mkdir()
        from _tree_inventory import os as tree_os
        original = tree_os.open

        def broken(path, *args, **kwargs):
            if path == "blocked":
                raise OSError("private path /secret/СИДОР")
            return original(path, *args, **kwargs)

        with patch("_tree_inventory.os.open", side_effect=broken):
            public, findings, _, incomplete = inventory(self.root, ["review"], "test")
        self.assertEqual(incomplete, 1)
        self.assertTrue(any(d["rule"] == "TREE_INVENTORY_INCOMPLETE" and
                            d["is_infrastructure_error"] for d in findings))
        self.assertNotIn("private path", json.dumps([public, findings]))


if __name__ == "__main__":
    unittest.main()
