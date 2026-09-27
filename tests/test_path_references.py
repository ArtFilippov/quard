"""End-to-end synthetic reference reporting without leaking candidate text."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _path_references import correlate, references
from _reports import normalize_presidio
from check import run_checks
from test_infrastructure import create_repo


class ReferenceTests(unittest.TestCase):
    def test_longest_component_suffix_is_case_sensitive_and_not_a_substring(self):
        text = ('/old/assets/file.pth ./assets/file.pth assets/file.pth '
                'wrong/file.pth ./assets/File.pth prefix/efile.pth')
        tree = {'review/assets/file.pth': 'a' * 24,
                'review/other/file.pth': 'b' * 24,
                'review/assets/File.pth': 'c' * 24,
                'review/prefixfile.pth': 'd' * 24}
        records = list(correlate(text, [0], tree))
        self.assertEqual([(r['suffix_components'], r['candidate_object_ids']) for r in records], [
            (2, ['a' * 24]), (2, ['a' * 24]), (2, ['a' * 24]),
            (1, ['a' * 24, 'b' * 24]), (2, ['c' * 24]), (0, [])])

    def test_end_to_end_suffix_groups_and_value_free_archives(self):
        import jsonschema

        with tempfile.TemporaryDirectory(prefix="eryx-correlation-") as directory:
            root = Path(directory)
            create_repo(root)
            (root / '.eryx/checks.toml').write_text(
                'groups = ["anonymity"]\nworking_directories = ["review"]\n', encoding='utf-8')
            (root / '.eryx/presidio.toml').write_text(
                'seeds = ["SampleMarker"]\n', encoding='utf-8')
            for folder in ('assets', 'alternate', 'SampleMarker', 'models'):
                (root / 'review' / folder).mkdir(parents=True, exist_ok=True)
            for path in ('assets/shared.pth', 'alternate/shared.pth',
                         'SampleMarker/shared.pth', 'models/Unique.pth'):
                (root / 'review' / path).write_bytes(b'/secret/unreadable')
            text = ('default="/old/models/Unique.pth"\n'
                    'out="assets/shared.pth"; again="assets/shared.pth"\n'
                    'model="org/shared.pth"; not_found="missing/piece.pth"\n'
                    'case="models/unique.pth"; relative="SampleMarker/shared.pth"\n')
            source = root / 'review/config.txt'
            source.write_text(text, encoding='utf-8')
            before = {path: path.read_bytes() for path in (source, *(
                root / 'review' / name for name in ('assets/shared.pth',
                'alternate/shared.pth', 'SampleMarker/shared.pth', 'models/Unique.pth')))}

            report = run_checks(root)
            self.assertEqual(report['exit_code'], 1)
            self.assertEqual(next(t['status'] for t in report['tools'] if t['name'] == 'presidio'),
                             'violations')
            coverage = report['coverage']
            refs = coverage['path_references']
            self.assertEqual([ref['outcome'] for ref in refs],
                             ['unique', 'unique', 'unique', 'ambiguous', 'missing', 'missing', 'unique'])
            self.assertEqual([ref['suffix_components'] for ref in refs], [2, 2, 2, 1, 0, 0, 2])
            self.assertEqual([ref['line'] for ref in refs], [1, 2, 2, 3, 3, 4, 4])
            self.assertEqual(len({ref['reference_id'] for ref in refs}), 7)
            objects = {item['path']: item for item in coverage['tree_objects']}
            self.assertEqual(refs[0]['candidate_object_ids'], [objects['review/models/Unique.pth']['object_id']])
            self.assertEqual(refs[3]['candidate_object_ids'], sorted(
                objects['review/' + path]['object_id'] for path in
                ('assets/shared.pth', 'alternate/shared.pth', '[REDACTED]/shared.pth')))
            self.assertEqual(refs[1]['candidate_object_ids'], refs[2]['candidate_object_ids'])
            self.assertEqual(refs[6]['candidate_object_ids'],
                             [objects['review/[REDACTED]/shared.pth']['object_id']])
            diagnostics = report['diagnostics']
            self.assertEqual(len([d for d in diagnostics if d['rule'] == 'AMBIGUOUS_PATH_REFERENCE']), 1)
            self.assertEqual(len([d for d in diagnostics if d['rule'] == 'POSSIBLE_MISSING_PATH_REFERENCE']), 2)
            self.assertEqual(len([d for d in diagnostics if d['rule'] == 'ABSOLUTE_PATH_REFERENCE']), 1)
            self.assertEqual(len(coverage['path_groups']), coverage['tree_count'])
            for group in coverage['path_groups']:
                self.assertEqual(group['path'], next(obj['path'] for obj in coverage['tree_objects']
                                                      if obj['object_id'] == group['object_id']))
                self.assertTrue(set(group['reference_ids']) <= {ref['reference_id'] for ref in refs})
                self.assertTrue(set(group['diagnostic_fingerprints']) <=
                                {d['fingerprint'] for d in diagnostics})
            target_group = next(group for group in coverage['path_groups'] if group['object_id'] ==
                                objects['review/assets/shared.pth']['object_id'])
            self.assertEqual(set(target_group['reference_ids']),
                             {refs[1]['reference_id'], refs[2]['reference_id'], refs[3]['reference_id']})
            self.assertTrue(any(d['rule'] == 'KNOWN_IDENTIFIER_SEED' and d.get('object_id') ==
                                objects['review/[REDACTED]/shared.pth']['object_id'] for d in diagnostics))
            jsonschema.validate(report, json.loads((Path(__file__).resolve().parents[1] /
                                                    'report.schema.json').read_text()))
            archive = root / Path(report['report_path']).parent
            for artifact in [*archive.rglob('*.json'), *archive.rglob('*.log')]:
                body = artifact.read_text(encoding='utf-8')
                for value in ('SampleMarker', '/old/models/Unique.pth', 'org/shared.pth',
                              'missing/piece.pth', '/secret/unreadable'):
                    self.assertNotIn(value, body, artifact)
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_partial_scan_keeps_confirmed_reference_and_its_diagnostic_link(self):
        with tempfile.TemporaryDirectory(prefix="eryx-partial-reference-") as directory:
            root = Path(directory)
            create_repo(root)
            (root / ".eryx/checks.toml").write_text(
                'groups = ["anonymity"]\nworking_directories = ["review"]\n', encoding="utf-8")
            (root / "review").mkdir()
            (root / "review/ordinary.txt").write_text('path="/some/private/file"\n', encoding="utf-8")
            (root / "review/oversize.txt").write_bytes(b"x" * (512 * 1024 + 1))
            report = run_checks(root)
            self.assertEqual(report["exit_code"], 2)
            self.assertEqual(next(t["status"] for t in report["tools"] if t["name"] == "presidio"),
                             "error")
            self.assertEqual(report["coverage"]["presidio_unconfirmed"], ["review/oversize.txt"])
            self.assertEqual(report["coverage"]["path_reference_count"], 1)
            candidate = report["coverage"]["path_references"][0]
            absolute = [d for d in report["diagnostics"] if d["rule"] == "ABSOLUTE_PATH_REFERENCE"]
            self.assertEqual(len(absolute), 1)
            self.assertEqual(absolute[0]["reference_id"], candidate["reference_id"])
            self.assertEqual(absolute[0]["object_id"], candidate["source_object_id"])

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

    def test_invalid_correlation_ids_and_shapes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'worker.json'
            identity = 'a' * 24
            base = {"scanned": ["review/input.txt"], "skipped": [], "excluded": [],
                    "failed": [], "findings": []}
            reference = {"path": "review/input.txt", "line": 1, "column": 1,
                         "span_length": 15, "absolute": False, "suffix_components": 1,
                         "candidate_object_ids": [identity]}
            for changed in (reference | {"candidate_object_ids": ['b' * 24]},
                            reference | {"candidate_object_ids": [identity, identity]},
                            reference | {"suffix_components": 0},
                            reference | {"candidate_object_ids": []},
                            reference | {"suffix_components": True},
                            reference | {"matched_text": 'private'},
                            reference | {"candidate_object_ids": ['private']}):
                with self.subTest(changed=changed):
                    output.write_text(json.dumps(base | {"references": [changed]}), encoding='utf-8')
                    normalized = normalize_presidio({"exit_code": 0}, root, output,
                                                    base['scanned'], {identity})
                    self.assertEqual(normalized['status'], 'error')
                    self.assertEqual(normalized['references'], [])
                    self.assertNotIn('private', json.dumps(normalized))

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
