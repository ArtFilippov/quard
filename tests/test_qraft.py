"""Public runtime and typing contracts for the optional qraft distribution."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
from types import MappingProxyType
from typing import override
import unittest

from qraft import cached_property
from qraft_fixture_utils import consumer

DICTIONARY = "__dict__"

class TestQraft(unittest.TestCase):
    """Exercise the installed API and the native type checker's contract."""

    def test_runtime_installs_from_pinned_revision_without_analyzers(self) -> None:
        with TemporaryDirectory() as tmp:
            project, revision = consumer(Path(tmp))
            result = subprocess.run([str(project / ".venv/bin/python"), "-B", "-c",
                                     "from qraft import cached_property; "
                                     + "print(cached_property.__name__)"],
                                    cwd=project, capture_output=True, text=True, check=True)
            packages = tomllib.loads((project / "uv.lock").read_text(encoding="utf-8"))[
                "package"]
            self.assertEqual((result.stdout.strip(), {item["name"] for item in packages},
                              revision in str(packages)),
                             ("CachedProperty", {"consumer", "qraft"}, True),
                             "the Git-pinned runtime pulled in analyzers or could not be imported")

    def test_values_are_cached_separately_per_instance(self) -> None:
        class Value:
            def __init__(self, number: int) -> None:
                super().__init__()
                self.number = number
                self.calls = 0

            @cached_property
            def result(self) -> int:
                self.calls += 1
                return self.number

        first = Value(3)
        second = Value(7)
        self.assertEqual((first.result, first.result, first.calls,
                          second.result, second.calls), (3, 3, 1, 7, 1),
                         "cached values were recomputed or shared between instances")

    def test_assignment_is_rejected(self) -> None:
        class Value:
            @cached_property
            def result(self) -> int:
                return 3

        value = Value()
        with self.assertRaises(AttributeError) as failure:
            setattr(value, "result", 4)
        self.assertIn("read-only", str(failure.exception), "assignment was allowed")

    def test_deletion_is_rejected_after_caching(self) -> None:
        class Value:
            @cached_property
            def result(self) -> int:
                return 3

        value = Value()
        _ = value.result
        with self.assertRaises(AttributeError) as failure:
            delattr(value, "result")
        self.assertIn("read-only", str(failure.exception), "deletion was allowed")

    def test_instance_without_dictionary_fails_clearly(self) -> None:
        class Value:
            __slots__ = ()

            @cached_property
            def result(self) -> int:
                return 3

        with self.assertRaises(TypeError) as failure:
            _ = Value().result
        self.assertIn("mutable __dict__", str(failure.exception),
                      "slot-only instances did not explain the cache requirement")

    def test_read_only_dictionary_fails_clearly(self) -> None:
        class Value:
            @override
            def __getattribute__(self, name: str) -> object:
                if name == DICTIONARY:
                    return MappingProxyType[str, int]({})
                return super().__getattribute__(name)

            @cached_property
            def result(self) -> int:
                return 3

        with self.assertRaises(TypeError) as failure:
            _ = Value().result
        self.assertIn("mutable __dict__", str(failure.exception),
                      "immutable dictionaries did not explain the cache requirement")

    def test_native_override_diagnostics(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, _ = consumer(root)
            (root / "pyrightconfig.json").write_text(json.dumps({
                "include": ["example.py"],
                "typeCheckingMode": "strict", "pythonVersion": "3.12",
                "reportImplicitOverride": "error"}), encoding="utf-8")
            (root / "example.py").write_text('''\
from typing import Protocol, override
from qraft import cached_property

class Contract(Protocol):
    @property
    def value(self) -> int: ...

class Good(Contract):
    @cached_property
    @override
    def value(self) -> int:
        return 3

class WrongReturn(Contract):
    @cached_property
    @override
    def value(self) -> str:
        return "no"

class MissingOverride(Contract):
    @cached_property
    def value(self) -> int:
        return 3

class NoBase:
    @cached_property
    @override
    def value(self) -> int:
        return 3
''', encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "pyright", "--project", str(root),
                                     "--pythonpath", str(project / ".venv/bin/python"),
                                     "--outputjson"], cwd=root, capture_output=True,
                                    text=True, check=False)
            findings = json.loads(result.stdout)["generalDiagnostics"]
            self.assertEqual({(item["range"]["start"]["line"] + 1, item["rule"])
                              for item in findings},
                             {(17, "reportIncompatibleMethodOverride"),
                              (22, "reportImplicitOverride"),
                              (28, "reportGeneralTypeIssues")},
                             "native override diagnostics were lost or the valid override failed: "
                             + result.stdout)


if __name__ == "__main__":
    unittest.main()
