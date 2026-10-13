"""Real-wheel exact schema clone fixtures at the check-runner seam."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import unittest

from rust_clone_engine_utils import focused_scan, report, wheel
from quard._common import capture


def _verify_standalone_renamed(test: unittest.TestCase, root: Path,
                               prefix: str, fields: str) -> None:
    """The normalized wheel session excludes schemas outside the batch runner.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated consumer root.
        prefix (str): Proven TypedDict import.
        fields (str): Schema fields to change under normalization.
    """
    python, _ = wheel()
    source_a = prefix + "class Alpha(TD):\n" + fields
    source_b = prefix + "class Beta(TD):\n" + (
        "    alpha: int\n    beta: str\n    gamma: list[int]\n    delta: bytes\n")
    script = ("import json, quard_clones, sys; " +
              "session = quard_clones.SourceSession([(sys.argv[1], sys.argv[2]), " +
              "(sys.argv[3], sys.argv[4])], 3, True); " +
              "print(json.dumps({'behavior': session.eligible_lines(), " +
              "'duplicates': json.loads(session.renamed())['duplicates']}))")
    first = root / "src/demo/first.py"
    second = root / "tests/second.py"
    standalone = capture([str(python), "-c", script, str(first), source_a,
                          str(second), source_b], root, 30)
    test.assertEqual(standalone["exit_code"], 0, standalone["stderr"])
    standalone_data = json.loads(standalone["stdout"])
    test.assertEqual(standalone_data["behavior"], {str(first): 0, str(second): 0})
    test.assertFalse(standalone_data["duplicates"])


def _inspect_schema(test: unittest.TestCase, root: Path, left: str, right: str,
                    minimum: int, skip_behavior: bool = True) -> dict[str, dict[str, Any]]:
    config = root / ".quard/config.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        "min_units = 3", f"min_units = {minimum}").replace(
        "min_units = 4", f"min_units = {minimum}"), encoding="utf-8")
    (root / "src/demo/first.py").write_text(left, encoding="utf-8")
    (root / "tests/second.py").write_text(right, encoding="utf-8")
    scans = focused_scan(root)
    for kind in ("renamed", "similar"):
        if skip_behavior:
            test.assertIn("skipped", scans[kind])
        else:
            test.assertFalse(scans[kind].get("duplicates", []))
    return scans


def verify_typed_dict_fields(test: unittest.TestCase, root: Path) -> None:
    """Verify class-based schemas, original mappings and independent views.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated public consumer root.
    """
    first = root / "src/demo/first.py"
    (root / "src/demo/domain/api.py").write_text("", encoding="utf-8")

    def inspect(left: str, right: str, minimum: int = 3) -> dict[str, Any]:
        result = _inspect_schema(test, root, left, right, minimum)
        exact = result["exact"]
        for kind in ("renamed", "similar"):
            behavior = result[kind]
            test.assertEqual(behavior["eligibleUnits"][str(first)],
                                int(kind == "similar" and
                                    ("TD = object" in left or "value = 1" in left)))
        test.assertEqual(exact["minimumUnits"], minimum)
        test.assertEqual(exact["sourcePaths"], [str(root / path) for path in (
            "src/demo/__init__.py", "src/demo/domain/api.py", "src/demo/first.py",
            "tests/second.py", "tests/test_value.py")])
        return exact

    prefix = "from typing import TypedDict as TD\n"
    fields = "    one: int\n    two: str\n    three: list[int]\n    four: bytes\n"
    left = prefix + "class GroupCounts(TD):\n    distinct: float\n" + fields
    right = prefix + "class MicroCounts(TD):\n    different: bool\n" + fields
    native = inspect(left, right)
    test.assertEqual(native["unitSizes"][str(first)], [5])
    test.assertTrue(native["duplicates"], native)
    test.assertTrue(any(item["firstFile"]["start"] == 4 and
                        item["secondFile"]["start"] == 4 and
                        item["fragment"].startswith("    one: int") and
                        "four: bytes" in item["fragment"]
                        for item in native["duplicates"]), native["duplicates"])
    # The report-path regression also checks normalized paths and coordinates.
    report_result = report(root)
    test.assertEqual(report_result["exit_code"], 1, report_result["diagnostics"])
    test.assertEqual({item["path"] for item in report_result["diagnostics"] if
                       item["rule"] == "EXACT_CLONE"},
                     {"src/demo/first.py", "tests/second.py"})
    test.assertTrue(all(item["native_details"] in native["duplicates"] and
                     item["line"] >= 4 and item["column"] >= 1
                     for item in report_result["diagnostics"] if item["rule"] == "EXACT_CLONE"))

    # Same-file schemas match; adjacent shorter schemas cannot concatenate.
    native = inspect(left + "\n" + right, "")
    test.assertTrue(native["duplicates"])
    test.assertTrue(all(item["firstFile"]["name"] == item["secondFile"]["name"]
                        == str(first) for item in native["duplicates"]))
    short = "    one: int\n    two: str\n"
    split = prefix + "class First(TD):\n" + short + "class Second(TD):\n    three: bytes\n"
    native = inspect(split, split)
    test.assertIn("skipped", native)
    test.assertEqual(native["unitSizes"][str(first)], [2, 1])
    mixed = prefix + "class First(TD):\n" + short + "def calculate():\n    value = 1\n"
    native = inspect(mixed, mixed)
    test.assertIn("skipped", native)
    test.assertEqual(native["unitSizes"][str(first)], [1, 2])

    # Exactly three matching trailing fields clear the configured minimum.
    native = inspect(prefix + "class First(TD):\n    initial: float\n" + fields,
                     prefix + "class Second(TD):\n    other: bool\n" + fields[13:])
    test.assertTrue(native["duplicates"])

    # Differently named or typed fields cannot turn into Type-2/3 clones.
    for changed in (fields.replace("two: str", "other: str"),
                    fields.replace("two: str", "two: bytes")):
        with test.subTest(changed=changed):
            native = inspect(prefix + "class First(TD):\n" + fields,
                             prefix + "class Second(TD):\n" + changed, 4)
            test.assertFalse(native["duplicates"])
            test.assertEqual(native["unitSizes"][str(first)], [4])

    _verify_standalone_renamed(test, root, prefix, fields)

    native = inspect("import typing as tp\nclass Alpha(tp.TypedDict):\n" + fields,
                     "import typing_extensions as tp\n" +
                     "class Beta(tp.TypedDict):\n" + fields)
    test.assertTrue(native["duplicates"])
    for declaration in ("class TypedDict:\n    pass\nclass Alpha(TypedDict):\n",
                        prefix + "TD = object\nclass Alpha(TD):\n"):
        with test.subTest(declaration=declaration):
            native = inspect(declaration + fields, declaration + fields)
            test.assertIn("skipped", native)


def verify_functional_typed_dict_fields(test: unittest.TestCase, root: Path) -> None:
    """Exercise native cross-form field matching and original field coordinates.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated public consumer root.
    """
    first = root / "src/demo/first.py"
    (root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
    prefix = "from typing import TypedDict as TD\n"
    fields = "    'one': int,\n    'two': str,\n    'three': list[int],\n"
    functional = prefix + "Counts = TD(\n    'Counts',\n    {\n" + fields + "    },\n)\n"
    other = prefix + "class Other(TD):\n    one: int\n    two: str\n" + "    three: list[int]\n"

    def inspect(left: str, right: str, minimum: int = 3,
                skip_behavior: bool = True) -> dict[str, Any]:
        return _inspect_schema(test, root, left, right, minimum, skip_behavior)["exact"]

    for right in (functional.replace("Counts", "Different"), other):
        with test.subTest(right=right):
            native = inspect(functional, right)
            test.assertTrue(native["duplicates"], native)
            test.assertEqual(native["minimumUnits"], 3)
            test.assertEqual(native["eligibleUnits"][str(first)], 3)
            test.assertEqual(native["unitSizes"][str(first)], [3])
            test.assertTrue(any(item["firstFile"]["start"] == 5 and
                                item["firstFile"]["startLoc"]["column"] == 4 and
                                item["firstFile"]["startLoc"]["position"] ==
                                functional.index("'one': int") and
                                item["firstFile"]["endLoc"]["position"] ==
                                functional.index("list[int]") + len("list[int]") and
                                item["fragment"] == fields.rstrip("\n") and
                                item["secondFile"]["start"] == (3 if right == other else 5)
                                for item in native["duplicates"]), native["duplicates"])
            # One real report proves native findings reach the archived report.
            reported = report(root) if right == other else None
            exact = ([d for d in reported["diagnostics"] if d["rule"] == "EXACT_CLONE"]
                     if reported is not None else [])
            if reported is not None:
                test.assertEqual(reported["exit_code"], 1, reported["diagnostics"])
                test.assertEqual({d["path"] for d in exact},
                                 {"src/demo/first.py", "tests/second.py"})
            test.assertTrue(all(d["native_details"] in native["duplicates"] and
                                d["line"] == (5 if d["path"] == "src/demo/first.py" else
                                              3 if right == other else 5)
                                for d in exact), exact)

    # A different leading field does not prevent a three-field trailing match.
    native = inspect(functional.replace(fields, "    'initial': bytes,\n" + fields),
                     other.replace("    one: int", "    other: bool\n    one: int"))
    test.assertTrue(any(item["firstFile"]["start"] == 6 and
                        item["secondFile"]["start"] == 4 for item in native["duplicates"]))

    native = inspect(functional + "\n" + functional.replace("Counts", "Other"), "")
    test.assertTrue(native["duplicates"])
    test.assertTrue(all(item["firstFile"]["name"] == item["secondFile"]["name"] ==
                        str(first) for item in native["duplicates"]))

    keyword = (prefix + "Kw = TD(\n    'Kw',\n    one=int,\n    two=str,\n" +
               "    three=list[int],\n)\n")
    native = inspect(keyword, other)
    test.assertTrue(native["duplicates"])
    native = inspect(other, functional)
    test.assertTrue(any(item["secondFile"]["startLoc"]["position"] ==
                        functional.index("'one': int") for item in native["duplicates"]))
    native = inspect(functional.replace("from typing import TypedDict as TD",
                                        "import typing_extensions as tp").replace(
                                            " = TD(", " = tp.TypedDict("), other)
    test.assertTrue(native["duplicates"])
    special = functional.replace("'one'", "'first-name'").replace(
        "'two'", "'привет'").replace("'three'", "'class'")
    native = inspect(special, special.replace("'first-name'", '"first-name"')
                     .replace("'привет'", '"привет"').replace("'class'", '"class"'))
    test.assertTrue(native["duplicates"])

    for right in (functional.replace("'two': str", "'other': str"),
                  functional.replace("'two': str", "'two': bytes")):
        with test.subTest(right=right):
            native = inspect(functional, right, 3)
            test.assertFalse(native["duplicates"])
    native = inspect(functional, other, 4)
    test.assertIn("skipped", native)

    # A one-line dictionary, shared field line, or opening-line field is not
    # silently given invented lines or promoted into the behavior view.
    compact = prefix + "Short = TD('Short', {'one': int, 'two': str, " + "'three': list[int]})\n"
    mixed = functional.replace("    'one': int,\n    'two': str,\n",
                               "    'one': int, 'two': str,\n")
    opening = functional.replace("    {\n    'one': int,", "    {'one': int,")
    for left in (compact, mixed, opening):
        with test.subTest(left=left):
            native = inspect(left, other, 3)
            test.assertEqual(native["eligibleUnits"][str(first)], 0)
            test.assertFalse(native["duplicates"])
    shadowed = prefix + "TD = object\n" + functional[len(prefix):]
    native = inspect(shadowed, other, skip_behavior=False)
    test.assertFalse(native["duplicates"])
