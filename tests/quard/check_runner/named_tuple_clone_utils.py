"""Real-engine NamedTuple schema and behavior regression fixtures."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import unittest

from rust_clone_engine_utils import focused_scan, prepared_native, report
from quard._common import read_json


def verify_named_tuple_schemas(test: unittest.TestCase, root: Path) -> None:
    """Check all native passes, report normalization and original schema locations.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated public consumer.
    """
    first = root / "src/demo/first.py"
    second = root / "tests/second.py"
    (root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
    left = ("from typing import NamedTuple as NT\n"
            "class Alpha(NT):\n    leading: bytes\n    one: int\n"
            "    two: str = 'ready'\n    three: list[int] = []\n")
    right = ("import typing as tp\nBeta = tp.NamedTuple('Beta', [\n"
             "    ('one', int),\n    ('two', str),\n    ('three', list[int]),\n])\n")

    def scan(a: str, b: str) -> dict[str, dict[str, Any]]:
        first.write_text(a, encoding="utf-8")
        second.write_text(b, encoding="utf-8")
        return focused_scan(root)

    # Defaults are part of the signature: no match with a required field.
    result = scan(left, right)
    test.assertFalse(result["exact"].get("duplicates"))
    for kind in ("renamed", "similar"):
        test.assertFalse(result[kind].get("duplicates"), result[kind])

    plain = left.replace(" = 'ready'", "").replace(" = []", "")
    result = scan(plain, right)
    native = result["exact"]
    test.assertTrue(native["duplicates"], native)
    test.assertEqual(native["eligibleUnits"][str(first)], 4)
    test.assertEqual(native["eligibleUnits"][str(second)], 3)
    test.assertTrue(any(item["firstFile"]["start"] == 4 and
                        item["firstFile"]["startLoc"]["position"] ==
                        plain.index("one: int") and
                        item["firstFile"]["startLoc"]["column"] == 4 and
                        item["fragment"] == "    one: int\n    two: str\n    three: list[int]" and
                        item["secondFile"]["startLoc"]["position"] ==
                        right.index("'one', int") and
                        item["secondFile"]["startLoc"]["column"] == 5
                        for item in native["duplicates"]), native["duplicates"])
    checked = report(root)
    for kind in ("exact", "renamed", "similar"):
        if kind == "exact":
            test.assertTrue(prepared_native(test, root, checked, 3, kind)["duplicates"])
        else:
            name = "jscpd-" + kind
            job = next(item for item in checked["tools"] if item["name"] == name)
            test.assertIn("minimum", read_json(root / job["raw_output"]["native_json"])["skipped"])
    test.assertEqual(checked["exit_code"], 1)
    test.assertEqual(checked["summary"]["infrastructure_errors"], 0)
    jobs = {job["name"]: job for job in checked["tools"]}
    test.assertEqual([(jobs[name]["status"], jobs[name]["exit_code"]) for name in
                      ("jscpd", "jscpd-renamed", "jscpd-similar")],
                     [("violations", 1), ("skipped", None), ("skipped", None)])
    diagnostics = [d for d in checked["diagnostics"] if d["tool"] == "jscpd"]
    test.assertEqual({d["path"] for d in diagnostics},
                     {"src/demo/first.py", "tests/second.py"})
    test.assertTrue(all(d["rule"] == "EXACT_CLONE" and
                        d["native_details"] in native["duplicates"] and
                        not d.get("is_infrastructure_error", False) for d in diagnostics))
    test.assertEqual(read_json(root / jobs["jscpd"]["raw_output"]["native_json"])[
        "duplicates"], native["duplicates"])

    # One changed member in a minimum-length schema destroys the exact match.
    for replacement in ("other: str", "two: bytes", "two: str = 'changed'"):
        with test.subTest(replacement=replacement):
            changed = plain.replace("two: str", replacement)
            scans = scan(changed, plain.replace("Alpha", "Gamma"))
            for kind in ("exact", "renamed", "similar"):
                test.assertFalse(scans[kind].get("duplicates"), scans[kind])

    # Class defaults and collections.namedtuple defaults compare exactly.
    class_default = left.replace("    leading: bytes\n", "")
    for changed in (class_default.replace("Alpha", "Gamma"),
                    class_default.replace("'ready'", "'other'")):
        scans = scan(class_default, changed)
        test.assertEqual(bool(scans["exact"].get("duplicates")),
                         "'other'" not in changed)
        test.assertFalse(scans["renamed"].get("duplicates"))
        test.assertFalse(scans["similar"].get("duplicates"))

    collection = ("from collections import namedtuple as NT\n"
                  "A = NT('A', [\n    'one',\n    'two',\n    'three',\n], "
                  "defaults=[\n    4,\n    5,\n])\n")
    scans = scan(collection, collection.replace("A = NT('A'", "B = NT('B'"))
    test.assertTrue(scans["exact"]["duplicates"], scans["exact"])
    test.assertFalse(scans["renamed"].get("duplicates"))
    test.assertFalse(scans["similar"].get("duplicates"))
    scans = scan(collection, collection.replace("    5,", "    6,"))
    test.assertFalse(scans["exact"].get("duplicates"))

    # Repeated declarations in one source are independent schema units.
    scans = scan(plain + "\n" + plain.replace("Alpha", "Another"), "")
    test.assertTrue(scans["exact"]["duplicates"])
    test.assertTrue(all(item["firstFile"]["name"] == item["secondFile"]["name"]
                        == str(first) for item in scans["exact"]["duplicates"]))

    # Methods remain executable, and ordinary expressions around a declaration
    # still participate in all existing behavior views.
    method = ("    def calculate(self, value):\n"
              "        first = value + 17\n        second = first * 3\n"
              "        return second - 4\n")
    scans = scan(plain + method, plain.replace("Alpha", "Gamma") + method)
    test.assertTrue(scans["exact"]["duplicates"])
    test.assertTrue(any("first = value + 17" in item["fragment"]
                        for item in scans["exact"]["duplicates"]))
    surrounding = "before = make(1)\n" + plain + "after = make(2)\nlater = make(3)\n"
    scans = scan(surrounding, surrounding.replace("Alpha", "Gamma"))
    test.assertGreaterEqual(scans["similar"]["eligibleUnits"][str(first)], 3)
    test.assertGreaterEqual(scans["renamed"]["eligibleUnits"][str(first)], 3)

    # Compact declarations are excluded without invented field locations.
    for compact in (
        "from typing import NamedTuple\n" +
        "C = NamedTuple('C', [('one', int), ('two', str), ('three', bytes)])\n",
        "from typing import NamedTuple\n" +
        "class C(NamedTuple):\n    one: int; two: str; three: bytes\n",
    ):
        scans = scan(compact, compact.replace("C", "D"))
        for kind in ("exact", "renamed", "similar"):
            test.assertFalse(scans[kind].get("duplicates"), scans[kind])

    # A compact and an expanded unrelated field list reproduce #136's
    # cross-declaration and shifted/overlapping same-declaration false matches.
    provenance = ("from typing import NamedTuple\nProvenance = NamedTuple('Provenance', [\n"
                  "    ('labels_source_row', float | None), ('byte_identity_status', str | None),\n"
                  "    ('source_locator_status', str | None), ('source_links_json', str | None),\n"
                  "    ('vlm_caption_pixelprose', str | None), ('generation_prompt', str | None),\n"
                  "    ('stats_evidence_id', str | None), ('stats_source', str | None),\n"
                  "    ('stats_source_row', float | None),\n])\n")
    context = (
        "from typing import NamedTuple\nSourceContext = NamedTuple('SourceContext', [\n"
        "    ('tag_list', tuple[str, ...] | None),\n"
        "    ('width', float | None), ('height', float | None),\n"
        "    ('topiq_nr', float | None), ('spectral_slope', float | None),\n"
        "    ('hf_energy_ratio', float | None), ('photorealism_score', float | None),\n"
        "    ('scene_complexity_score', float | None), ('brightness', float | None),\n"
        "    ('si', float | None), ('cf', float | None), ('noise_var_mean', float | None),\n"
        "    ('technical_quality_score', float | None), ('num_faces', float | None),\n"
        "    ('total_text_area_share', float | None), ('is_multipanel', float | None),\n])\n"
    )
    scans = scan(provenance, context)
    for kind in ("exact", "renamed", "similar"):
        test.assertFalse(scans[kind].get("duplicates"), scans[kind])
    test.assertEqual(scans["renamed"]["eligibleUnits"][str(first)], 0)
    test.assertEqual(scans["similar"]["eligibleUnits"][str(second)], 0)

    # The same batch must still detect actual executable renamed behavior.
    behavior = ("def work(value):\n    first = make(value)\n"
                "    second = first[0]\n    return second + 17\n")
    other = behavior.replace("value", "item").replace("first", "initial").replace(
        "second", "result").replace("17", "23")
    scans = scan(provenance + behavior, context + other)
    test.assertTrue(scans["renamed"]["duplicates"], scans["renamed"])
    test.assertFalse(scans["exact"].get("duplicates"))
    test.assertFalse(scans["similar"].get("duplicates"))
    checked = report(root)
    jobs = {job["name"]: job for job in checked["tools"]}
    test.assertEqual(checked["exit_code"], 1)
    test.assertEqual(checked["summary"]["infrastructure_errors"], 0)
    for kind in ("exact", "renamed", "similar"):
        archived = prepared_native(test, root, checked, 3, kind)
        test.assertEqual(bool(archived["duplicates"]), kind == "renamed")
    test.assertEqual([(jobs[name]["status"], jobs[name]["exit_code"]) for name in
                      ("jscpd", "jscpd-renamed", "jscpd-similar")],
                     [("passed", 0), ("violations", 1), ("passed", 0)])
    test.assertEqual({d["rule"] for d in checked["diagnostics"] if d["tool"] == "jscpd"},
                     {"RENAMED_CLONE"})
