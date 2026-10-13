"""Real-wheel contract declaration and executable body regressions."""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
import json
from pathlib import Path
import re
from typing import Any
import unittest

from rust_clone_engine_utils import focused_scan, report
from quard._common import read_json
from quard.report_utils import normalize

Inspect = Callable[[str, str, int], dict[str, Any]]


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

    def inspect(left: str, right: str, minimum: int = 3) -> dict[str, Any]:
        config = root / ".quard/config.toml"
        config.write_text(re.sub(r"(?m)^min_lines = \d+$", f"min_lines = {minimum}",
                                 config.read_text(encoding="utf-8")), encoding="utf-8")
        first.write_text(left, encoding="utf-8")
        second.write_text(right, encoding="utf-8")
        scans = focused_scan(root)
        for kind in ("renamed", "similar"):
            test.assertIn("skipped", scans[kind])
        return scans["exact"]

    left = prefix + 'class First(Port):\n    """Documentation."""\n' + methods
    right = prefix + "class Second(Port):\n" + methods
    native = inspect(left, right)
    test.assertTrue(native["duplicates"])
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
    # Keep one real worker/report assertion for native diagnostic delivery.
    report_result = report(root)
    exact = [d for d in report_result["diagnostics"] if d["rule"] == "EXACT_CLONE"]
    test.assertEqual(report_result["exit_code"], 1, report_result["diagnostics"])
    test.assertEqual({d["path"] for d in exact},
                     {"src/demo/first.py", "tests/second.py"})
    test.assertTrue(all(d["native_details"] in native["duplicates"] and
                        not d.get("is_infrastructure_error", False) for d in exact))

    # A different class name and import spelling do not change the interface.
    abc = ("from abc import ABC as Base, abstractmethod as required\n" +
           "class Interface(Base):\n" +
           methods.replace("    def alpha", "    @required\n    def alpha"))
    native = inspect(left, abc)
    test.assertTrue(native["duplicates"])

    # A single changed signature cannot turn identical placeholders into a clone
    # at the six-line minimum; neither can a reorder or renamed method.
    for changed in (methods.replace("alpha(self, value: int)", "other(self, value: int)"),
                    methods.replace("value: int", "value: bytes"),
                    methods.replace("-> str", "-> bytes"),
                    methods[methods.index("    async def beta"):]
                    + methods[:methods.index("    async def beta")]):
        with test.subTest(changed=changed):
            native = inspect(left, prefix + "class Second(Port):\n" + changed, 6)
            test.assertFalse(native["duplicates"])

    changed_all = (methods.replace("alpha", "delta").replace("beta", "epsilon")
                   .replace("gamma", "zeta"))
    native = inspect(left, prefix + "class Second(Port):\n" + changed_all)
    test.assertFalse(native["duplicates"])

    metaclass = ("import abc as abstract\nclass Interface(metaclass=abstract.ABCMeta):\n" +
                 "    @abstract.abstractmethod\n" + methods)
    native = inspect(left, metaclass)
    test.assertTrue(native["duplicates"])
    aliased = metaclass.replace("import abc as abstract",
                                "from abc import ABCMeta as Meta, abstractmethod as required")
    aliased = aliased.replace("abstract.ABCMeta", "Meta")
    aliased = aliased.replace("abstract.abstractmethod", "required")
    native = inspect(left, aliased)
    test.assertTrue(native["duplicates"])

    shadow = prefix + "Port = object\nclass Ordinary(Port):\n" + methods
    native = inspect(shadow, shadow)
    test.assertIn("skipped", native)

    native = inspect(left + "\n" + right, "")
    test.assertTrue(native["duplicates"])
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
        inspect (Inspect): Focused native scan on the discovered sources.
        prefix (str): Proven Protocol import.
    """
    first = root / "src/demo/first.py"
    inline = ("    def alpha(self, value: int) -> str: ...\n" +
              "    async def beta(self, *, key: str) -> bool: pass\n" +
              "    def gamma(self) -> None: raise NotImplementedError\n")
    native = inspect(prefix + "class First(Port):\n" + inline,
                     prefix + "class Second(Port):\n" + inline, 3)
    test.assertEqual(native["unitLines"][str(first)], [3])
    test.assertTrue(native["duplicates"])
    test.assertTrue(any(d["fragment"] == inline.rstrip("\n") and
                        d["firstFile"]["start"] == 3 and
                        d["firstFile"]["end"] == 5 for d in native["duplicates"]))
    multiline = ("    def alpha(\n        self, value: int,\n"
                 "    ) -> str:\n        ...\n"
                 "    async def beta(\n        self, *, key: str,\n"
                 "    ) -> bool: pass\n")
    native = inspect(prefix + "class First(Port):\n" + multiline,
                     prefix + "class Second(Port):\n" + multiline, 6)
    test.assertTrue(native["duplicates"])
    test.assertEqual(native["unitLines"][str(first)], [7])
    test.assertTrue(any("\n        self, value: int,\n" in d["fragment"] and
                        "async def beta" in d["fragment"] for d in native["duplicates"]), native)
    long_signature = ("    def alpha(\n        self,\n        value: int,\n"
                      "        key: str,\n        extra: bytes,\n    ) -> str:\n"
                      "        \"\"\"Contract documentation.\"\"\"\n        ...\n")
    changed = long_signature.replace("extra: bytes", "extra: bool")
    native = inspect(prefix + "class First(Port):\n" + long_signature,
                     prefix + "class Second(Port):\n" + changed, 3)
    test.assertFalse(native["duplicates"], native)
    native = inspect(prefix + "class First(Port):\n" + long_signature,
                     prefix + "class Second(Port):\n" + long_signature, 7)
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
            native = inspect(source, source, 3)
            test.assertIn("skipped", native)

    # Stub-only contracts do not make two executable lines meet the minimum.
    short = prefix + "class First(Port):\n" + methods[:methods.index("    async def beta")]
    native = inspect(short, short, 3)
    test.assertIn("skipped", native)
    split = prefix + "class First(Port):\n" + short.split("class First(Port):\n", 1)[1]
    split += "class Second(Port):\n" + short.split("class First(Port):\n", 1)[1]
    native = inspect(split, split, 3)
    test.assertIn("skipped", native)

    # Real bodies in contracts, including special methods, remain behavioral.
    real = (prefix + "class First(Port):\n" +
            "    def __len__(self) -> int:\n" +
            "        one = 1\n        two = one + 2\n" +
            "        three = two * 3\n        four = three - 4\n        return four\n")
    first.write_text(real, encoding="utf-8")
    second.write_text(real.replace("First", "Second"), encoding="utf-8")
    scans = focused_scan(root)
    test.assertTrue(scans["exact"]["duplicates"])
    test.assertFalse(scans["renamed"].get("duplicates"))
    test.assertFalse(scans["similar"]["duplicates"])
    test.assertTrue(any(d["firstFile"]["start"] >= 3
                        for d in scans["exact"]["duplicates"]))

    # Only behavior participates in the normalized pass, even inside contracts.
    second.write_text(real.replace("First", "Second").replace("one", "a")
                      .replace("two", "b").replace("three", "c"), encoding="utf-8")
    test.assertTrue(focused_scan(root)["renamed"]["duplicates"])


def inspect_property_declarations(test: unittest.TestCase, root: Path, left: str,
                                  right: str, minimum: int = 7) -> dict[str, Any]:
    """Run real native passes on a property fixture and return the exact result.

    Args:
        test (unittest.TestCase): Native report assertion owner.
        root (Path): Isolated public fixture root.
        left (str): First contract source.
        right (str): Second contract source.
        minimum (int, default=7): Shared prepared minimum.

    Returns:
        dict[str, Any]: Real native exact scan of the discovered source scope.
    """
    first = root / "src/demo/first.py"
    second = root / "tests/second.py"
    config = root / ".quard/config.toml"
    config.write_text(re.sub(r"(?m)^min_lines = \d+$", f"min_lines = {minimum}",
                              config.read_text(encoding="utf-8")), encoding="utf-8")
    first.write_text(left, encoding="utf-8")
    second.write_text(right, encoding="utf-8")
    scans = focused_scan(root)
    for kind in ("renamed", "similar"):
        test.assertIn("skipped", scans[kind])
    return scans["exact"]


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
    native = inspect(left, right)
    test.assertTrue(native["duplicates"])
    test.assertEqual(native["unitLines"][str(first)], [10])
    test.assertTrue(any(d["firstFile"]["startLoc"]["position"] ==
                        left.index("def value") and d["firstFile"]["endLoc"]["position"] ==
                        left.index("raise NotImplementedError") +
                        len("raise NotImplementedError") and "\n        self,\n" in d["fragment"]
                        and "@value.setter" in d["fragment"] and
                        d["firstFile"]["name"] == str(first) for d in native["duplicates"]),
                    native)
    report_result = report(root)
    test.assertEqual(report_result["exit_code"], 1, report_result["diagnostics"])
    test.assertTrue(all(d["native_details"] in native["duplicates"]
                        for d in report_result["diagnostics"] if d["rule"] == "EXACT_CLONE"))
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
    native = inspect(prefix + "class First(Protocol):\n" + lengthy,
                     prefix + "class Second(Protocol):\n" +
                     lengthy.replace("second: bytes", "second: bool"), 3)
    test.assertFalse(native["duplicates"], native)

    for changed in (declarations.replace("@value.setter", "@other.setter"),
                    declarations.replace("item: str", "item: bytes"),
                    declarations.replace("value", "other"),
                    declarations.replace("-> str", "-> bytes")):
        with test.subTest(changed=changed):
            native = inspect(left, prefix + "class Second(Protocol):\n" + changed, 8)
            test.assertFalse(native["duplicates"], native)

    plain = declarations.replace("    @property\n", "").replace(
        "    @value.setter\n", "").replace("    @value.deleter\n", "")
    native = inspect(left, prefix + "class Second(Protocol):\n" + plain, 8)
    test.assertFalse(native["duplicates"], native)

    special = prefix + "class First(Protocol):\n" + (
        "    def __len__(self) -> int:\n        ...\n") * 4
    native = inspect(special, special.replace("First", "Second"), 3)
    test.assertIn("skipped", native)

    # A long original span containing fewer eligible lines never clears the minimum.
    brief = (prefix + "class First(Protocol):\n    @property\n" +
             "    def value(\n        self,\n    ) -> str:\n        ...\n")
    native = inspect(brief, brief.replace("First", "Second"), 5)
    test.assertIn("skipped", native)

    native = inspect(left + "\n" + right, "", 7)
    test.assertTrue(native["duplicates"])
    test.assertTrue(all(d["firstFile"]["name"] == d["secondFile"]["name"] == str(first)
                        for d in native["duplicates"]))

    # Normalization needs a genuine archived runner report at this boundary.
    verify_property_mapping(test, root, report(root), native)
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
            "        c = b + 'z'\n        d = c + 'q'\n        return d\n    def __len__(self) -> int:\n" +
            "        a = 1\n        b = a + 2\n        c = b * 3\n        return c\n")
    first.write_text(real, encoding="utf-8")
    second.write_text(real.replace("First", "Second"), encoding="utf-8")
    behavioral = focused_scan(root)
    test.assertTrue(behavioral["exact"]["duplicates"])
    test.assertFalse(behavioral["renamed"].get("duplicates"))
    test.assertFalse(behavioral["similar"]["duplicates"])
    second.write_text(real.replace("First", "Second").replace("a = 'x'", "first = 'x'")
                      .replace("b = a + 'y'", "second = first + 'y'")
                      .replace("c = b + 'z'", "third = second + 'z'")
                      .replace("d = c + 'q'", "fourth = third + 'q'")
                      .replace("return d\n    def", "return fourth\n    def"), encoding="utf-8")
    test.assertTrue(focused_scan(root)["renamed"]["duplicates"])

    # One contract can have both declaration-only and implemented accessors.
    mixed = (prefix + "class First(Protocol):\n    @property\n" +
             "    def value(self) -> str:\n        ...\n" +
             "    @value.setter\n    def value(self, item: str) -> None:\n" +
             "        first = item + 'a'\n        second = first + 'b'\n" +
             "        return None\n    @value.deleter\n" +
             "    def value(self) -> None:\n        pass\n")
    first.write_text(mixed, encoding="utf-8")
    second.write_text(mixed.replace("First", "Second"), encoding="utf-8")
    payload = focused_scan(root)["exact"]
    test.assertTrue(payload["duplicates"])
    test.assertEqual(payload["unitLines"][str(first)], [3, 4])
    test.assertTrue(any("first = item + 'a'" in d["fragment"] for d in payload["duplicates"]))
    test.assertTrue(any("@value.deleter" in d["fragment"] for d in payload["duplicates"]))
