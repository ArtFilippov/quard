"""Real-wheel contract declaration and executable body regressions."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
from typing import Any
import unittest

from rust_clone_engine_utils import report
from quard._common import read_json

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
        test.assertEqual(checked["summary"]["infrastructure_errors"], 0,
                         checked["diagnostics"])
        jobs = {job["name"]: job for job in checked["tools"]}
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

    verify_contract_exclusions_and_bodies(test, root, inspect, prefix, methods)


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
    properties = (prefix + "class First(Port):\n" +
                  "    @property\n    def alpha(self) -> str:\n        ...\n" +
                  "    @property\n    def beta(self) -> str:\n        ...\n" +
                  "    @property\n    def gamma(self) -> str:\n        ...\n")
    for source in (special, properties, "class Ordinary:\n" + methods):
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
