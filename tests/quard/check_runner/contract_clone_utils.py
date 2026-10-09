"""Real-wheel contract declaration and executable body regressions."""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
import json
from pathlib import Path
import re
from typing import Any
import unittest

from rust_clone_engine_utils import report
from quard._common import read_json
from quard.report_utils import normalize

Inspect = Callable[[str, str, int], tuple[dict[str, Any], dict[str, Any]]]


def verify_contract_methods(test: unittest.TestCase, root: Path) -> None:
    """Check exact declarations, native positions and independent behavior passes.

    Args:
        test (unittest.TestCase): Native output assertion owner.
        root (Path): Temporary discovered contract project.
    """
    first = root / "src/demo/first.py"
    second = root / "tests/second.py"
    (root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
    prefix = "from typing import Protocol as Port\n"
    methods = ("    def alpha(self, value: int) -> str:\n        ...\n"
               "    async def beta(self, *, key: str) -> bool:\n        pass\n"
               "    def gamma(self) -> None:\n        raise NotImplementedError\n")

    def inspect(left: str, right: str, minimum: int = 3
                ) -> tuple[dict[str, Any], dict[str, Any]]:
        config = root / ".quard/config.toml"
        config.write_text(re.sub(r"(?m)^min_lines = \d+$", f"min_lines = {minimum}",
                                 config.read_text(encoding="utf-8")), encoding="utf-8")
        first.write_text(left, encoding="utf-8")
        second.write_text(right, encoding="utf-8")
        checked = report(root)
        jobs = {job["name"]: job for job in checked["tools"]}
        test.assertEqual(checked["summary"]["infrastructure_errors"], 0,
                         (checked["diagnostics"], (root / jobs["jscpd"]["raw_output"][
                             "batch_stderr"]).read_text(encoding="utf-8")))
        for name in ("jscpd-renamed", "jscpd-similar"):
            test.assertEqual((jobs[name]["status"], jobs[name]["exit_code"]),
                             ("skipped", None), checked["diagnostics"])
            test.assertIn("skipped", read_json(root / jobs[name]["raw_output"]["native_json"]))
        return checked, read_json(root / jobs["jscpd"]["raw_output"]["native_json"])

    left = prefix + 'class First(Port):\n    """Documentation."""\n' + methods
    right = prefix + "class Second(Port):\n" + methods
    checked, native = inspect(left, right)
    test.assertEqual(checked["exit_code"], 1, checked["diagnostics"])
    test.assertEqual(native["eligibleLines"][str(first)], 6)
    test.assertEqual(native["unitLines"][str(first)], [6])
    test.assertEqual(set(native["sourcePaths"]),
                     {str(root / p) for p in ("src/demo/__init__.py",
                                              "src/demo/domain/api.py", "src/demo/first.py",
                                              "tests/second.py", "tests/test_value.py")})
    test.assertTrue(any(d["fragment"].startswith("    def alpha") and
                        "raise NotImplementedError" in d["fragment"] and
                        d["firstFile"]["name"] == str(first) and
                        d["firstFile"]["start"] == 4 and
                        d["secondFile"]["start"] == 3 and
                        d["firstFile"]["startLoc"]["position"] ==
                        left.index("def alpha") and
                        d["firstFile"]["endLoc"]["position"] ==
                        left.index("raise NotImplementedError") +
                        len("raise NotImplementedError") for d in native["duplicates"]), native)
    exact = [d for d in checked["diagnostics"] if d["rule"] == "EXACT_CLONE"]
    test.assertEqual({d["path"] for d in exact},
                     {"src/demo/first.py", "tests/second.py"})
    test.assertTrue(all(d["native_details"] in native["duplicates"] and
                        not d.get("is_infrastructure_error", False) for d in exact))

    # A different class name and import spelling do not change the interface.
    abc = ("from abc import ABC as Base, abstractmethod as required\n" +
           "class Interface(Base):\n" +
           methods.replace("    def alpha", "    @required\n    def alpha"))
    checked, native = inspect(left, abc)
    test.assertEqual(checked["exit_code"], 1, checked["diagnostics"])
    test.assertTrue(native["duplicates"])

    # A single changed signature cannot turn identical placeholders into a clone
    # at the six-line minimum; neither can a reorder or renamed method.
    for changed in (methods.replace("alpha(self, value: int)", "other(self, value: int)"),
                    methods.replace("value: int", "value: bytes"),
                    methods.replace("-> str", "-> bytes"),
                    methods[methods.index("    async def beta"):]
                    + methods[:methods.index("    async def beta")]):
        with test.subTest(changed=changed):
            checked, native = inspect(left, prefix + "class Second(Port):\n" + changed, 6)
            test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
            test.assertFalse(native["duplicates"])

    changed_all = (methods.replace("alpha", "delta").replace("beta", "epsilon")
                   .replace("gamma", "zeta"))
    checked, native = inspect(left, prefix + "class Second(Port):\n" + changed_all)
    test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
    test.assertFalse(native["duplicates"])

    metaclass = ("import abc as abstract\nclass Interface(metaclass=abstract.ABCMeta):\n" +
                 "    @abstract.abstractmethod\n" + methods)
    checked, native = inspect(left, metaclass)
    test.assertEqual(checked["exit_code"], 1, checked["diagnostics"])
    test.assertTrue(native["duplicates"])
    aliased = metaclass.replace("import abc as abstract",
                                "from abc import ABCMeta as Meta, abstractmethod as required")
    aliased = aliased.replace("abstract.ABCMeta", "Meta")
    aliased = aliased.replace("abstract.abstractmethod", "required")
    checked, native = inspect(left, aliased)
    test.assertEqual(checked["exit_code"], 1, checked["diagnostics"])
    test.assertTrue(native["duplicates"])

    shadow = prefix + "Port = object\nclass Ordinary(Port):\n" + methods
    checked, native = inspect(shadow, shadow)
    test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
    test.assertIn("skipped", native)

    checked, native = inspect(left + "\n" + right, "")
    test.assertEqual(checked["exit_code"], 1, checked["diagnostics"])
    test.assertTrue(all(d["firstFile"]["name"] == d["secondFile"]["name"] ==
                         str(first) for d in native["duplicates"]))

    verify_multiline_methods(test, root, inspect, prefix)
    verify_contract_exclusions_and_bodies(test, root, inspect, prefix, methods)


def verify_multiline_methods(test: unittest.TestCase, root: Path, inspect: Inspect,
                             prefix: str) -> None:
    """Match original multiline signatures and reject distinct full signatures.

    Args:
        test (unittest.TestCase): Native output assertion owner.
        root (Path): Isolated consumer root.
        inspect (Inspect): Full prepared check on the selected sources.
        prefix (str): Proven Protocol import.
    """
    first = root / "src/demo/first.py"
    inline = ("    def alpha(self, value: int) -> str: ...\n" +
              "    async def beta(self, *, key: str) -> bool: pass\n" +
              "    def gamma(self) -> None: raise NotImplementedError\n")
    checked, native = inspect(prefix + "class First(Port):\n" + inline,
                              prefix + "class Second(Port):\n" + inline, 3)
    test.assertEqual((checked["exit_code"], native["unitLines"][str(first)]),
                     (1, [3]), checked["diagnostics"])
    test.assertTrue(any(d["fragment"] == inline.rstrip("\n") and
                        d["firstFile"]["start"] == 3 and
                        d["firstFile"]["end"] == 5 for d in native["duplicates"]))
    multiline = ("    def alpha(\n        self, value: int,\n"
                 "    ) -> str:\n        ...\n"
                 "    async def beta(\n        self, *, key: str,\n"
                 "    ) -> bool: pass\n")
    checked, native = inspect(prefix + "class First(Port):\n" + multiline,
                              prefix + "class Second(Port):\n" + multiline, 6)
    test.assertEqual(checked["exit_code"], 1, checked["diagnostics"])
    test.assertEqual(native["unitLines"][str(first)], [7])
    test.assertTrue(any("\n        self, value: int,\n" in d["fragment"] and
                        "async def beta" in d["fragment"] for d in native["duplicates"]), native)
    long_signature = ("    def alpha(\n        self,\n        value: int,\n"
                      "        key: str,\n        extra: bytes,\n    ) -> str:\n"
                      "        \"\"\"Contract documentation.\"\"\"\n        ...\n")
    changed = long_signature.replace("extra: bytes", "extra: bool")
    checked, native = inspect(prefix + "class First(Port):\n" + long_signature,
                              prefix + "class Second(Port):\n" + changed, 3)
    test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
    test.assertFalse(native["duplicates"], native)
    checked, native = inspect(prefix + "class First(Port):\n" + long_signature,
                              prefix + "class Second(Port):\n" + long_signature, 7)
    test.assertEqual(checked["exit_code"], 1, checked["diagnostics"])
    test.assertTrue(any('"""Contract documentation."""' in d["fragment"] and
                        d["firstFile"]["startLoc"]["position"] ==
                        (prefix + "class First(Port):\n" + long_signature).index("def alpha")
                        for d in native["duplicates"]), native)


def verify_contract_exclusions_and_bodies(
        test: unittest.TestCase, root: Path,
        inspect: Inspect,
        prefix: str, methods: str) -> None:
    """Verify structural boundaries and real executable contract implementations.

    Args:
        test (unittest.TestCase): Checks the independent native job results.
        root (Path): Fixture with the source and test profiles.
        inspect (Inspect): Runs a declaration-only scan at the chosen minimum.
        prefix (str): Proven local Protocol import.
        methods (str): Ordered placeholder declarations.
    """
    first = root / "src/demo/first.py"
    second = root / "tests/second.py"

    # Special declarations and ordinary classes have no structural representation.
    special = prefix + "class First(Port):\n" + (
        "    def __len__(self) -> int:\n        ...\n") * 3
    for source in (special, "class Ordinary:\n" + methods):
        with test.subTest(source=source):
            checked, native = inspect(source, source, 3)
            test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
            test.assertIn("skipped", native)

    # Stub-only contracts do not make two executable lines meet the minimum.
    short = prefix + "class First(Port):\n" + methods[:methods.index("    async def beta")]
    checked, native = inspect(short, short, 3)
    test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
    test.assertIn("skipped", native)
    split = prefix + "class First(Port):\n" + short.split("class First(Port):\n", 1)[1]
    split += "class Second(Port):\n" + short.split("class First(Port):\n", 1)[1]
    checked, native = inspect(split, split, 3)
    test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
    test.assertIn("skipped", native)

    # Real bodies in contracts, including special methods, remain behavioral.
    real = (prefix + "class First(Port):\n" +
            "    def __len__(self) -> int:\n" +
            "        one = 1\n        two = one + 2\n" +
            "        three = two * 3\n        return three\n")
    first.write_text(real, encoding="utf-8")
    second.write_text(real.replace("First", "Second"), encoding="utf-8")
    checked = report(root)
    jobs = {job["name"]: job for job in checked["tools"]}
    test.assertEqual(checked["summary"]["infrastructure_errors"], 0)
    test.assertEqual(jobs["jscpd"]["status"], "violations")
    test.assertEqual(jobs["jscpd-renamed"]["status"], "passed")
    test.assertEqual(jobs["jscpd-similar"]["status"], "passed")
    test.assertTrue(any(d["rule"] == "EXACT_CLONE" and d["line"] >= 3
                        for d in checked["diagnostics"]))

    # Only behavior participates in the normalized pass, even inside contracts.
    second.write_text(real.replace("First", "Second").replace("one", "a")
                      .replace("two", "b").replace("three", "c"), encoding="utf-8")
    checked = report(root)
    jobs = {job["name"]: job for job in checked["tools"]}
    test.assertEqual(checked["summary"]["infrastructure_errors"], 0)
    test.assertEqual(jobs["jscpd-renamed"]["status"], "violations",
                     checked["diagnostics"])
    test.assertTrue(any(d["rule"] == "RENAMED_CLONE" for d in checked["diagnostics"]))


def inspect_property_declarations(test: unittest.TestCase, root: Path, left: str,
                                  right: str, minimum: int = 7
                                  ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run all passes on a real property fixture and read the exact native archive.

    Args:
        test (unittest.TestCase): Native report assertion owner.
        root (Path): Isolated public fixture root.
        left (str): First contract source.
        right (str): Second contract source.
        minimum (int, default=7): Shared prepared minimum.

    Returns:
        tuple[dict[str, Any], dict[str, Any]]: Full report and exact native archive.
    """
    first = root / "src/demo/first.py"
    second = root / "tests/second.py"
    config = root / ".quard/config.toml"
    config.write_text(re.sub(r"(?m)^min_lines = \d+$", f"min_lines = {minimum}",
                              config.read_text(encoding="utf-8")), encoding="utf-8")
    first.write_text(left, encoding="utf-8")
    second.write_text(right, encoding="utf-8")
    checked = report(root)
    jobs = {job["name"]: job for job in checked["tools"]}
    test.assertEqual(checked["summary"]["infrastructure_errors"], 0,
                     (checked["diagnostics"], (root / jobs["jscpd"]["raw_output"][
                         "batch_stderr"]).read_text(encoding="utf-8")))
    for name in ("jscpd-renamed", "jscpd-similar"):
        test.assertEqual(jobs[name]["status"], "skipped", checked["diagnostics"])
    return checked, read_json(root / jobs["jscpd"]["raw_output"]["native_json"])


def verify_contract_properties(test: unittest.TestCase, root: Path) -> None:
    """Check ordered accessors and multiline ranges through the packaged engine.

    Args:
        test (unittest.TestCase): Native report assertion owner.
        root (Path): Isolated public fixture root.
    """
    first = root / "src/demo/first.py"
    (root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
    config = root / ".quard/config.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        "min_lines = 3", "min_lines = 7"), encoding="utf-8")
    prefix = "from typing import Protocol\n"
    declarations = ("    @property\n    def value(\n        self,\n    ) -> str:\n        ...\n"
                    "    @value.setter\n    def value(\n        self, item: str,\n"
                    "    ) -> None:\n        pass\n"
                    "    @value.deleter\n    def value(self) -> None:\n"
                    "        raise NotImplementedError\n")

    inspect: Inspect = partial(inspect_property_declarations, test, root)
    left = prefix + "class First(Protocol):\n" + declarations
    right = prefix + "class Second(Protocol):\n" + declarations
    checked, native = inspect(left, right)
    test.assertEqual(checked["exit_code"], 1, checked["diagnostics"])
    test.assertEqual(native["unitLines"][str(first)], [10])
    test.assertTrue(any(d["firstFile"]["startLoc"]["position"] ==
                        left.index("def value") and d["firstFile"]["endLoc"]["position"] ==
                        left.index("raise NotImplementedError") +
                        len("raise NotImplementedError") and "\n        self,\n" in d["fragment"]
                        and "@value.setter" in d["fragment"] and
                        d["firstFile"]["name"] == str(first) for d in native["duplicates"]),
                    native)
    test.assertTrue(all(d["native_details"] in native["duplicates"]
                        for d in checked["diagnostics"] if d["rule"] == "EXACT_CLONE"))
    test.assertTrue(any(d["firstFile"]["startLoc"]["column"] == 4 and
                        d["firstFile"]["endLoc"]["column"] == 33
                        for d in native["duplicates"]), native)
    verify_property_boundaries(test, root, inspect, prefix, declarations, left)


def verify_property_boundaries(test: unittest.TestCase, root: Path, inspect: Inspect,
                               prefix: str, declarations: str, left: str) -> None:
    """Exercise distinct declarations, empty views and bad original mappings.

    Args:
        test (unittest.TestCase): Native report assertion owner.
        root (Path): Isolated public fixture root.
        inspect (Inspect): Full prepared check on the selected sources.
        prefix (str): Proven Protocol import.
        declarations (str): Getter, setter and deleter declarations.
        left (str): Original first class source.
    """
    first = root / "src/demo/first.py"
    right = prefix + "class Second(Protocol):\n" + declarations

    lengthy = ("    @property\n    def value(\n        self,\n"
               "        first: str,\n        second: bytes,\n"
               "    ) -> str:\n        ...\n")
    checked, native = inspect(prefix + "class First(Protocol):\n" + lengthy,
                              prefix + "class Second(Protocol):\n" +
                              lengthy.replace("second: bytes", "second: bool"), 3)
    test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
    test.assertFalse(native["duplicates"], native)

    for changed in (declarations.replace("@value.setter", "@other.setter"),
                    declarations.replace("item: str", "item: bytes"),
                    declarations.replace("value", "other"),
                    declarations.replace("-> str", "-> bytes")):
        with test.subTest(changed=changed):
            checked, native = inspect(left, prefix + "class Second(Protocol):\n" + changed,
                                      8)
            test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
            test.assertFalse(native["duplicates"], native)

    plain = declarations.replace("    @property\n", "").replace(
        "    @value.setter\n", "").replace("    @value.deleter\n", "")
    checked, native = inspect(left, prefix + "class Second(Protocol):\n" + plain, 8)
    test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
    test.assertFalse(native["duplicates"], native)

    special = prefix + "class First(Protocol):\n" + (
        "    def __len__(self) -> int:\n        ...\n") * 4
    checked, native = inspect(special, special.replace("First", "Second"), 3)
    test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
    test.assertIn("skipped", native)

    # A long original span containing fewer eligible lines never clears the minimum.
    brief = (prefix + "class First(Protocol):\n    @property\n" +
             "    def value(\n        self,\n    ) -> str:\n        ...\n")
    checked, native = inspect(brief, brief.replace("First", "Second"), 5)
    test.assertEqual(checked["exit_code"], 0, checked["diagnostics"])
    test.assertIn("skipped", native)

    checked, native = inspect(left + "\n" + right, "", 7)
    test.assertEqual(checked["exit_code"], 1, checked["diagnostics"])
    test.assertTrue(all(d["firstFile"]["name"] == d["secondFile"]["name"] == str(first)
                        for d in native["duplicates"]))

    verify_property_mapping(test, root, checked, native)
    verify_property_behavior(test, root, prefix)


def verify_property_mapping(test: unittest.TestCase, root: Path,
                            checked: dict[str, Any], native: dict[str, Any]) -> None:
    """Fail a real full check on inconsistent original source positions.

    Args:
        test (unittest.TestCase): Native report assertion owner.
        root (Path): Isolated public fixture root.
        checked (dict[str, Any]): Completed same-file check report.
        native (dict[str, Any]): Exact engine archive from that check.
    """
    # Corrupt real multiline mapping payloads: report normalization fails closed.
    paths = read_json((root / checked["report_path"]).parent /
                      "clone-engine-files.json")["paths"]
    bad = root / "invalid-contract.json"
    for field, value in (("position", -1), ("column", 999)):
        altered = json.loads(json.dumps(native))
        altered["duplicates"][0]["firstFile"]["endLoc"][field] = value
        bad.write_text(json.dumps(altered), encoding="utf-8")
        parsed = normalize("jscpd", {"exit_code": 1, "stdout": "", "stderr": "",
                                     "error": None}, root, bad, paths, True)
        test.assertEqual(parsed["status"], "error")
        test.assertTrue(any(d["is_infrastructure_error"] for d in parsed["diagnostics"]))

    def corrupt_mapping(output: Path) -> None:
        payload = read_json(output)
        payload["duplicates"][0]["firstFile"]["startLoc"]["position"] += 1
        output.write_text(json.dumps(payload), encoding="utf-8")

    failed = report(root, corrupt_native=corrupt_mapping)
    jobs = {job["name"]: job for job in failed["tools"]}
    test.assertEqual((failed["exit_code"], jobs["jscpd"]["status"],
                      jobs["jscpd"]["exit_code"]), (2, "error", 1))
    test.assertTrue(any(d["job"] == "jscpd" and d["is_infrastructure_error"]
                        for d in failed["diagnostics"]))


def verify_property_behavior(test: unittest.TestCase, root: Path, prefix: str) -> None:
    """Keep real accessor and special-method bodies in behavior passes.

    Args:
        test (unittest.TestCase): Native report assertion owner.
        root (Path): Isolated public fixture root.
        prefix (str): Proven Protocol import.
    """
    # Implemented accessor and special-method bodies remain behavioral.
    config = root / ".quard/config.toml"
    first = root / "src/demo/first.py"
    second = root / "tests/second.py"
    config.write_text(config.read_text(encoding="utf-8").replace(
        "min_lines = 7", "min_lines = 3"), encoding="utf-8")
    real = (prefix + "class First(Protocol):\n    @property\n" +
            "    def value(self) -> str:\n        a = 'x'\n        b = a + 'y'\n" +
            "        return b\n    def __len__(self) -> int:\n" +
            "        a = 1\n        b = a + 2\n        return b\n")
    first.write_text(real, encoding="utf-8")
    second.write_text(real.replace("First", "Second"), encoding="utf-8")
    behavioral = report(root)
    jobs = {job["name"]: job for job in behavioral["tools"]}
    test.assertEqual(behavioral["summary"]["infrastructure_errors"], 0)
    test.assertEqual(jobs["jscpd"]["status"], "violations")
    test.assertEqual(jobs["jscpd-renamed"]["status"], "passed")
    test.assertEqual(jobs["jscpd-similar"]["status"], "passed")
    second.write_text(real.replace("First", "Second").replace("a = 'x'", "first = 'x'")
                      .replace("b = a + 'y'", "second = first + 'y'")
                      .replace("return b\n    def", "return second\n    def"), encoding="utf-8")
    renamed = report(root)
    jobs = {job["name"]: job for job in renamed["tools"]}
    test.assertEqual(renamed["summary"]["infrastructure_errors"], 0)
    test.assertEqual(jobs["jscpd-renamed"]["status"], "violations")

    # One contract can have both declaration-only and implemented accessors.
    mixed = (prefix + "class First(Protocol):\n    @property\n" +
             "    def value(self) -> str:\n        ...\n" +
             "    @value.setter\n    def value(self, item: str) -> None:\n" +
             "        first = item + 'a'\n        second = first + 'b'\n" +
             "        return None\n    @value.deleter\n" +
             "    def value(self) -> None:\n        pass\n")
    first.write_text(mixed, encoding="utf-8")
    second.write_text(mixed.replace("First", "Second"), encoding="utf-8")
    combined = report(root)
    job = next(job for job in combined["tools"] if job["name"] == "jscpd")
    payload = read_json(root / job["raw_output"]["native_json"])
    test.assertEqual(combined["summary"]["infrastructure_errors"], 0)
    test.assertEqual(job["status"], "violations")
    test.assertEqual(payload["unitLines"][str(first)], [3, 4])
    test.assertTrue(any("first = item + 'a'" in d["fragment"] for d in payload["duplicates"]))
    test.assertTrue(any("@value.deleter" in d["fragment"] for d in payload["duplicates"]))
