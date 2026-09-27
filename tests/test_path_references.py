"""End-to-end synthetic reference reporting without leaking candidate text."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _path_references import references
from _reports import normalize_presidio
from check import run_checks
from test_infrastructure import create_repo


class ReferenceTests(unittest.TestCase):
    def test_missing_or_malformed_reference_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "worker.json"
            base = {"scanned": ["review/input.txt"], "skipped": [], "excluded": [],
                    "failed": [], "findings": []}
            for records in (None, [{"path": "review/input.txt", "line": 1, "column": 1,
                                    "span_length": 9, "absolute": True, "raw": "/private/a"}],
                            [{"path": "review/missing.txt", "line": 1, "column": 1,
                              "span_length": 9, "absolute": True}]):
                with self.subTest(records=records):
                    payload = base if records is None else base | {"references": records}
                    output.write_text(json.dumps(payload), encoding="utf-8")
                    normalized = normalize_presidio({"exit_code": 0}, root, output, base["scanned"])
                    self.assertEqual(normalized["status"], "error")
                    self.assertEqual(normalized["references"], [])
                    self.assertNotIn("/private/a", json.dumps(normalized))

    def test_heuristic_positions_and_boundaries(self):
        text = ('default="/home/SampleMarker/output.pth"\n'
                '# ./models/output.pth and ../assets/file.txt\n'
                'model="org/family"; https://site.example/docs/file; ssh user@host:/tmp/file\n')
        starts = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
        self.assertEqual(list(references(text, starts)), [
            {"line": 1, "column": 10, "span_length": 29, "absolute": True},
            {"line": 2, "column": 3, "span_length": 19, "absolute": False},
            {"line": 2, "column": 27, "span_length": 18, "absolute": False},
            {"line": 3, "column": 8, "span_length": 10, "absolute": False},
        ])

    def test_report_positions_identities_exclusions_and_safe_archives(self):
        import jsonschema

        with tempfile.TemporaryDirectory(prefix="eryx-reference-") as directory:
            root = Path(directory)
            create_repo(root)
            (root / ".eryx/checks.toml").write_text(
                'groups = ["anonymity"]\nworking_directories = ["review"]\n', encoding="utf-8")
            (root / ".eryx/presidio.toml").write_text('seeds = ["SampleMarker"]\n', encoding="utf-8")
            (root / "review").mkdir()
            content = ('default="/home/SampleMarker/output.pth"\n'
                       '# ./models/output.pth and ../assets/file.txt\n'
                       'model="org/family"; out="org/family"\n')
            source = root / "review/SampleMarker.txt"
            source.write_text(content, encoding="utf-8")
            auxiliary = {"guide.md": 'See /docs/manual and ./local/page\n',
                         "run.sh": 'output=results/log.txt\n',
                         "settings.toml": 'weights = "org/family"\n'}
            for filename, text in auxiliary.items():
                (root / "review" / filename).write_text(text, encoding="utf-8")
            (root / "review/excluded.pth").write_text('/home/SampleMarker/private\n', encoding="utf-8")
            original = {p: p.read_bytes() for p in (source, root / "review/excluded.pth",
                                                    *(root / "review" / name for name in auxiliary))}

            report = run_checks(root)
            candidates = report["coverage"]["path_references"]
            self.assertEqual(report["coverage"]["path_reference_count"], 9)
            source_candidates = [r for r in candidates if r["path"] == "review/[REDACTED].txt"]
            self.assertEqual([r["absolute"] for r in source_candidates],
                             [True, False, False, False, False])
            self.assertEqual([(r["line"], r["column"]) for r in source_candidates],
                             [(1, 10), (2, 3), (2, 27), (3, 8), (3, 26)])
            self.assertEqual(len({r["reference_id"] for r in candidates}), 9)
            self.assertEqual({r["path"] for r in candidates},
                             {"review/[REDACTED].txt", "review/guide.md", "review/run.sh",
                              "review/settings.toml"})
            obj = next(o for o in report["coverage"]["tree_objects"]
                       if o["path"] == "review/[REDACTED].txt")
            self.assertTrue(all(r["source_object_id"] == obj["object_id"] and
                                r["path"] == obj["path"] for r in source_candidates))
            absolute = [d for d in report["diagnostics"] if d["rule"] == "ABSOLUTE_PATH_REFERENCE"]
            self.assertEqual(len(absolute), 2)
            self.assertEqual((absolute[0]["reference_id"], absolute[0]["object_id"],
                              absolute[0]["line"], absolute[0]["column"]),
                             (source_candidates[0]["reference_id"], obj["object_id"], 1, 10))
            self.assertIn("KNOWN_IDENTIFIER_SEED", {d["rule"] for d in report["diagnostics"]})
            self.assertEqual(report["coverage"]["presidio_excluded"], ["review/excluded.pth"])
            self.assertEqual(next(t["status"] for t in report["tools"] if t["name"] == "presidio"),
                             "violations")
            jsonschema.validate(report, json.loads((Path(__file__).resolve().parents[1] /
                                                    "report.schema.json").read_text()))
            archive = root / Path(report["report_path"]).parent
            for artifact in [*archive.rglob("*.json"), *archive.rglob("*.log")]:
                body = artifact.read_text(encoding="utf-8")
                for value in ("SampleMarker", "/home/", "org/family", "../assets/file.txt",
                              "/docs/manual", "results/log.txt"):
                    self.assertNotIn(value, body, artifact)
            self.assertEqual(original, {p: p.read_bytes() for p in original})


if __name__ == "__main__":
    unittest.main()
