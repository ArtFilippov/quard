"""Real-wheel exact schema clone fixtures at the check-runner seam."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import unittest

from rust_clone_engine_utils import report, wheel
from quard._common import capture, read_json


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


def verify_typed_dict_fields(test: unittest.TestCase, root: Path) -> None:
    """Verify class-based schemas, original mappings and independent views.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated public consumer root.
    """
    first = root / "src/demo/first.py"
    second = root / "tests/second.py"
    (root / "src/demo/domain/api.py").write_text("", encoding="utf-8")

    def inspect(left: str, right: str, minimum: int = 3) -> tuple[dict[str, Any],
                                                                   dict[str, Any]]:
        config = root / ".quard/config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace(
            "min_lines = 3", f"min_lines = {minimum}").replace(
            "min_lines = 4", f"min_lines = {minimum}"), encoding="utf-8")
        first.write_text(left, encoding="utf-8")
        second.write_text(right, encoding="utf-8")
        result = report(root)
        test.assertEqual(result["summary"]["infrastructure_errors"], 0,
                         result["diagnostics"])
        jobs = {job["name"]: job for job in result["tools"]}
        for name in ("jscpd-renamed", "jscpd-similar"):
            test.assertEqual((jobs[name]["status"], jobs[name]["exit_code"]),
                             ("skipped", None), result["diagnostics"])
            behavior = read_json(root / jobs[name]["raw_output"]["native_json"])
            test.assertEqual(behavior["eligibleLines"][str(first)],
                             int("TD = object" in left or "value = 1" in left))
            test.assertIn("skipped", behavior)
        exact = read_json(root / jobs["jscpd"]["raw_output"]["native_json"])
        test.assertEqual(exact["minimum"], minimum)
        test.assertEqual(exact["sourcePaths"], [str(root / path) for path in read_json(
            (root / result["report_path"]).parent / "clone-engine-files.json")["paths"]])
        return result, exact

    prefix = "from typing import TypedDict as TD\n"
    fields = "    one: int\n    two: str\n    three: list[int]\n    four: bytes\n"
    left = prefix + "class GroupCounts(TD):\n    distinct: float\n" + fields
    right = prefix + "class MicroCounts(TD):\n    different: bool\n" + fields
    result, native = inspect(left, right)
    test.assertEqual((result["exit_code"], native["unitLines"][str(first)]), (1, [5]))
    test.assertTrue(native["duplicates"], native)
    test.assertTrue(any(item["firstFile"]["start"] == 4 and
                        item["secondFile"]["start"] == 4 and
                        item["fragment"].startswith("    one: int") and
                        "four: bytes" in item["fragment"]
                        for item in native["duplicates"]), native["duplicates"])
    test.assertEqual({item["path"] for item in result["diagnostics"] if
                      item["rule"] == "EXACT_CLONE"},
                     {"src/demo/first.py", "tests/second.py"})
    test.assertTrue(all(item["native_details"] in native["duplicates"] and
                        item["line"] >= 4 and item["column"] >= 1
                        for item in result["diagnostics"] if item["rule"] == "EXACT_CLONE"))

    # Same-file schemas match; adjacent shorter schemas cannot concatenate.
    same_file, native = inspect(left + "\n" + right, "")
    test.assertTrue(native["duplicates"], same_file["diagnostics"])
    test.assertTrue(all(item["firstFile"]["name"] == item["secondFile"]["name"]
                        == str(first) for item in native["duplicates"]))
    short = "    one: int\n    two: str\n"
    split = prefix + "class First(TD):\n" + short + "class Second(TD):\n    three: bytes\n"
    result, native = inspect(split, split)
    test.assertEqual(result["exit_code"], 0)
    test.assertIn("skipped", native)
    test.assertEqual(native["unitLines"][str(first)], [2, 1])
    mixed = prefix + "class First(TD):\n" + short + "def calculate():\n    value = 1\n"
    result, native = inspect(mixed, mixed)
    test.assertEqual(result["exit_code"], 0, result["diagnostics"])
    test.assertIn("skipped", native)
    test.assertEqual(native["unitLines"][str(first)], [1, 2])

    # Exactly three matching trailing fields clear the configured minimum.
    result, native = inspect(prefix + "class First(TD):\n    initial: float\n" + fields,
                             prefix + "class Second(TD):\n    other: bool\n" + fields[13:])
    test.assertEqual(result["exit_code"], 1, result["diagnostics"])
    test.assertTrue(native["duplicates"])

    # Differently named or typed fields cannot turn into Type-2/3 clones.
    for changed in (fields.replace("two: str", "other: str"),
                    fields.replace("two: str", "two: bytes")):
        with test.subTest(changed=changed):
            result, native = inspect(prefix + "class First(TD):\n" + fields,
                                     prefix + "class Second(TD):\n" + changed, 4)
            test.assertEqual(result["exit_code"], 0, result["diagnostics"])
            test.assertFalse(native["duplicates"])
            test.assertEqual(native["unitLines"][str(first)], [4])

    _verify_standalone_renamed(test, root, prefix, fields)

    result, native = inspect("import typing as tp\nclass Alpha(tp.TypedDict):\n" + fields,
                             "import typing_extensions as tp\n" +
                             "class Beta(tp.TypedDict):\n" + fields)
    test.assertEqual(result["exit_code"], 1)
    test.assertTrue(native["duplicates"])
    for declaration in ("class TypedDict:\n    pass\nclass Alpha(TypedDict):\n",
                        prefix + "TD = object\nclass Alpha(TD):\n"):
        with test.subTest(declaration=declaration):
            result, native = inspect(declaration + fields, declaration + fields)
            test.assertEqual(result["exit_code"], 0, result["diagnostics"])
            test.assertIn("skipped", native)
