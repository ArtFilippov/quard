"""Independent job selection on isolated offline consumers."""
from __future__ import annotations

from pathlib import Path
import json
import shutil
import sys
from tempfile import mkdtemp
from typing import Any
import unittest
from collections.abc import Callable
from importlib import metadata
from unittest.mock import patch

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from jsonschema import validate
from quard._common import capture, read_json, write_json
from quard.check_runner.native_check_runner import NativeCheckRunner
from quard.check_runner.runner_utils import installed_versions
from quard.install_utils import dependency_specs
from quard.check_selection_utils import DUPLICATE_JOBS, OPTIONAL_JOBS
from quard.architecture_draft.generated_architecture_utils import IDENTITY
from offline_fixtures import create_repo, fake_tools
from contract_clone_utils import verify_contract_methods, verify_contract_properties
from rust_clone_engine_utils import (verify_prepared_scope, verify_corrupt_report,
                                        verify_mismatched_engine, verify_missing_wheel,
                                         verify_real_full_check, verify_real_wheel_install,
                                         verify_same_file_and_mapping,
                                        verify_prepared_renamed, verify_renamed_failure,
                                        verify_prepared_similar, verify_similar_boundaries,
                                        verify_similar_failure,
                                        focused_scan, native_wheel, prepared_native,
                                        report as prepared_report, versions, wheel)
from typed_dict_clone_utils import verify_functional_typed_dict_fields, verify_typed_dict_fields
from named_tuple_clone_utils import verify_named_tuple_schemas

PLACEHOLDERS = ("pass", "...", "raise NotImplementedError",
                "raise NotImplementedError()", 'raise NotImplementedError("pending")')


class TestCheckRunner(unittest.TestCase):
    def test_real_wheel_mixed_schema_and_behavior_accounting_order(self) -> None:
        source = ('from typing import TypedDict\nclass Item(TypedDict):\n'
                  '    a: int\n    b: str\n    c: bool\n'
                  'def work():\n    one = 1\n    two = one + 2\n'
                  '    three = two * 3\n    four = three / 4\n')
        path = "/tmp/schema-and-behavior.py"
        session = native_wheel().SourceSession([(path, source)], 3)
        sizes = session.exact_unit_lines()[path]
        self.assertEqual(session.exact_scanned_unit_lines()[path],
                         [size for size in sizes if size >= 3])

    def test_real_wheel_exact_rejects_overlapping_prepared_constructs(self) -> None:
        source = ('def check(self, root, value):\n'
                  '    if value is not None:\n'
                  '        self.assertEqual((root / "tach.toml").read_bytes(), value)\n'
                  '    if value is not None:\n'
                  '        self.assertEqual((root / "tach.toml").read_bytes(), value)\n')
        session = native_wheel().SourceSession([("/tmp/exact-overlap.py", source)], 3)
        self.assertEqual(json.loads(session.exact())["duplicates"], [])
        # The native detector can report a disjoint 3/1 match when the
        # intervening constructs are different; neither side may be short.
        first, last = source.rsplit('    if value is not None:\n', 1)
        source = (first + '    middle = compute(root)\n    other = middle / 2\n'
                  '    if value is not None:\n' + last)
        session = native_wheel().SourceSession([("/tmp/exact-short-side.py", source)], 3)
        self.assertEqual(json.loads(session.exact())["duplicates"], [])

    def test_real_wheel_renamed_folded_context_maps_forward(self) -> None:
        path = QUARD / "src/quard/folder_analysis/folder_ownership.py"
        session = native_wheel().SourceSession([(str(path), path.read_text(encoding="utf-8"))], 3)
        clones = json.loads(session.renamed())["duplicates"]
        self.assertTrue(clones)
        self.assertTrue(all(side["start"] <= side["end"] for clone in clones
                            for side in (clone["firstFile"], clone["secondFile"])))

    def test_source_pin_activates_component(self) -> None:
        manifest = read_json(QUARD / "tools.json")
        tool = self.root / "candidate-tool"
        tool.mkdir()
        write_json(tool / "tools.json", manifest)
        self.assertIn("quard-clones==0.1.0", dependency_specs(tool))
        manifest["native_source"] = False
        write_json(tool / "tools.json", manifest)
        self.assertNotIn("quard-clones==0.1.0", dependency_specs(tool))

    def test_real_wheel_rejects_incompatible_abi_tag(self) -> None:
        python, _ = wheel()
        script = ("import sys\nsys.path.insert(0, sys.argv[1])\n"
                  "from quard import clone_engine_utils as engine\n"
                  "from unittest.mock import patch\n"
                  "from importlib import metadata\n"
                  "wheel = metadata.distribution('quard-clones')\n"
                  "read = wheel.read_text\n"
                  "with patch.object(type(wheel), 'read_text',\n"
                  "    lambda self, name: 'Tag: cp311-cp311-win_amd64\\n'\n"
                  "    if name == 'WHEEL' else read(name)):\n"
                  "    engine.installed_engine()\n")
        # Verify the same distribution through a real interpreter, changing only
        # its claimed WHEEL tag. Importable code alone cannot prove wheel ABI.
        result = capture([str(python), "-c", script, str(QUARD / "src")], self.root, 30)
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("wheel ABI/platform does not match", result["stderr"])

    def test_real_wheel_read_failure_keeps_peer_findings(self) -> None:
        first = self.root / "src/demo/domain/api.py"
        fake, _ = fake_tools({"pydoclint": {"stderr": "tests/test_value.py:1: DOC101: Issue\n",
                                            "code": 1}})
        python, _ = wheel()

        def dispatch(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            if Path(argv[1]).name == "_clone_engine_worker.py":
                first.unlink()
                return capture([str(python), *argv[1:]], cwd, timeout)
            return fake(argv, cwd, timeout)

        with patch.dict("os.environ", {"QUARD_CLONE_ENGINE": "rust"}):
            result = NativeCheckRunner().run(self.root, lcom_files="", serial=True,
                                              capture_fn=dispatch, versions_fn=versions)
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual({job["name"]: job["status"] for job in result["tools"]
                          if job["name"] in {"jscpd", "jscpd-renamed", "jscpd-similar"}},
                         {name: "error" for name in ("jscpd", "jscpd-renamed", "jscpd-similar")})
        self.assertIn("DOC101", {item["rule"] for item in result["diagnostics"]})

    def test_real_wheel_stub_only_view_is_skipped(self) -> None:
        for path in ("src/demo/__init__.py", "src/demo/domain/api.py", "tests/test_value.py"):
            (self.root / path).unlink()
        (self.root / "src/demo/domain/api.pyi").write_text("class Port: ...\n",
                                                           encoding="utf-8")
        checked = prepared_report(self.root)
        for name in ("jscpd", "jscpd-renamed", "jscpd-similar"):
            job = next(item for item in checked["tools"] if item["name"] == name)
            self.assertEqual((job["status"], job["exit_code"]), ("skipped", None))

    def test_real_wheel_prunes_trivial_renamed_control_before_detection(self) -> None:
        first = self.root / "src/demo/domain/api.py"
        second = self.root / "tests/test_value.py"
        pure = ("def work(value):\n"
                "    left = value\n    if probe(value):\n"
                "        middle = left\n    else:\n        middle = 4\n"
                "    for item in value:\n        middle = item\n"
                "    with context(value) as handle:\n        middle = handle\n"
                "    match value:\n        case 1:\n            middle = 3\n"
                "    return middle\n")
        first.write_text(pure, encoding="utf-8")
        second.write_text(pure, encoding="utf-8")
        views = focused_scan(self.root)
        self.assertFalse(views["renamed"].get("duplicates"))
        self.assertEqual(views["renamed"]["eligibleUnits"][str(first)], 0)
        self.assertTrue(views["exact"]["duplicates"])

        left = ("def work(value):\n    if probe(value):\n"
                "        temp = value\n        result = build(value)\n"
                "        item = result[0]\n        return item + 1\n")
        right = left.replace("temp", "unused").replace("result", "output").replace(
            "item", "element").replace("value", "source")
        first.write_text(left, encoding="utf-8")
        second.write_text(right, encoding="utf-8")
        results = focused_scan(self.root)
        self.assertTrue(results["renamed"]["duplicates"], results["renamed"])
        self.assertEqual(results["renamed"]["eligibleUnits"][str(first)], 3)
        self.assertEqual(results["renamed"]["eligibleUnits"][str(second)], 3)
        self.assertTrue(any({entry["firstFile"]["startLoc"]["line"],
                             entry["secondFile"]["startLoc"]["line"]} == {2}
                            and entry["firstFile"]["endLoc"]["line"] == 6
                            and entry["fragment"] == "\n".join(left.splitlines()[1:6])
                            for entry in results["renamed"]["duplicates"]))
        checked = prepared_report(self.root)
        jobs = {job["name"]: job for job in checked["tools"]}
        self.assertEqual(jobs["jscpd-renamed"]["status"], "violations")
        self.assertEqual(jobs["jscpd-similar"]["status"], "passed")
        self.assertFalse(any(d.get("is_infrastructure_error") for d in checked["diagnostics"]))

        for skeleton in ("if probe(value):\n        alias = value\n",
                         "while probe(value):\n        alias = value\n",
                         "for alias in iterate(value):\n        alias = value\n",
                         "with context(value) as alias:\n        alias = value\n",
                         "match value:\n        case 1:\n            alias = value\n"):
            with self.subTest(empty=skeleton):
                source = ("def work(value):\n    first = build(value)\n    " + skeleton +
                          "    second = first[0]\n    return value\n")
                first.write_text(source, encoding="utf-8")
                second.write_text(source.replace("value", "source"), encoding="utf-8")
                view = focused_scan(self.root)["renamed"]
                self.assertEqual(view["eligibleUnits"][str(first)], 2)
                self.assertFalse(view.get("duplicates"), view)

        left = ("def work(value):\n    if probe(value):\n"
                "        one = build(value)\n        two = one[0]\n"
                "        return two + 1\n")
        first.write_text(left, encoding="utf-8")
        second.write_text(left.replace("if probe(value)", "while probe(value)")
                          .replace("value", "source"), encoding="utf-8")
        self.assertFalse(focused_scan(self.root)["renamed"].get("duplicates"))

    def test_real_wheel_prunes_only_proven_field_bindings(self) -> None:
        first = self.root / "src/demo/domain/api.py"
        second = self.root / "tests/test_value.py"
        source = ("class Holder:\n    def __init__(self, value):\n"
                  "        self.field = value\n    @property\n"
                  "    def property(self):\n        return self.field\n"
                  "    def work(self, value):\n        local = self.field\n"
                  "        other = self.property\n        unknown = self.unknown\n"
                  "        total = value + 1\n        result = total[0]\n"
                  "        return result * 2\n")
        first.write_text(source, encoding="utf-8")
        second.write_text(source, encoding="utf-8")
        view = focused_scan(self.root)["renamed"]
        # Constructor write and decorated property body keep their own units;
        # the proven field extraction disappears from work.
        self.assertEqual(view["unitSizes"][str(first)], [1, 1, 5])
        self.assertFalse(view.get("duplicates"))  # Identical copies are Type 1.

    def test_real_wheel_accepts_supported_python_syntax(self) -> None:
        syntax = ('type Alias[T] = list[T]\n'
                  'def identity[T](value: T) -> T:\n'
                  '    match value:\n'
                  '        case [first, *rest]:\n'
                  '            return f"{first!s} {rest!r}"\n'
                  '    return value\n'
                  'try:\n    raise ExceptionGroup("sample", [ValueError()])\n'
                  'except* ValueError as errors:\n    result = str(errors)\n')
        source = self.root / "src/demo/syntax.py"
        source.write_text(syntax, encoding="utf-8")
        scans = focused_scan(self.root)
        self.assertTrue(all(str(source) in payload["sourcePaths"]
                            and payload["eligibleUnits"][str(source)] >= 3
                            for payload in scans.values()))

    def test_real_wheel_prepared_imports_and_docstrings(self) -> None:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        imports = "    import json\n    import pathlib\n    import collections\n"
        for path in (first, second):
            path.write_text('"""Shared module documentation."""\n' +
                            "import json\nimport pathlib\nimport collections\n" +
                            'def action():\n    """Repeated documentation."""\n' + imports,
                            encoding="utf-8")
        result = prepared_report(self.root)
        jobs = {job["name"]: job for job in result["tools"]}
        self.assertEqual((jobs["jscpd"]["status"], jobs["jscpd"]["exit_code"]),
                         ("violations", 1), result["diagnostics"])
        native = read_json(self.root / jobs["jscpd"]["raw_output"]["native_json"])
        self.assertTrue(native["duplicates"])
        self.assertTrue(all(item["firstFile"]["start"] >= 7 for item in native["duplicates"]))
        self.assertTrue({str(first), str(second)} <= set(native["eligibleUnits"]))
        (self.root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
        # Three imports at file scope are ineligible even though they are long
        # enough for an unprepared native scan.
        for path in (first, second):
            path.write_text('"""Common module documentation."""\n' +
                            "import json\nimport pathlib\nimport collections\n",
                            encoding="utf-8")
        excluded = focused_scan(self.root)
        self.assertTrue(all("skipped" in native for native in excluded.values()))
        # Attribute documentation and method docstrings do not turn two
        # assignments into a clone; the same assignments plus a third do.
        for path in (first, second):
            path.write_text('class Component:\n    """Class documentation."""\n' +
                            '    field = "assigned value"\n    """Attribute documentation."""\n' +
                            '    def action(self):\n        """Method documentation."""\n' +
                            '        first = 1\n        second = first + 2\n', encoding="utf-8")
        short = focused_scan(self.root)
        self.assertTrue(all("skipped" in native or not native["duplicates"]
                            for native in short.values()))
        for path in (first, second):
            with path.open("a", encoding="utf-8") as source:
                _ = source.write("        third = second * 3\n")
        long = focused_scan(self.root)["exact"]
        self.assertEqual({part["name"] for clone in long["duplicates"]
                          for part in (clone["firstFile"], clone["secondFile"])},
                         {str(first), str(second)})
        for path in (first, second):
            path.write_text('class Holder:\n    first = "assigned value"\n' +
                            '    """Attribute documentation."""\n' +
                            '    second = first + " more"\n    third = second.upper()\n',
                            encoding="utf-8")
        payload = focused_scan(self.root)["exact"]
        self.assertTrue(payload["duplicates"])
        self.assertTrue(any('"""Attribute documentation."""' in clone["fragment"]
                            for clone in payload["duplicates"]))
        self.assertTrue(any('"assigned value"' in clone["fragment"]
                            for clone in payload["duplicates"]))
        for path in (first, second):
            path.write_text('class Holder:\n    value = "café"\n' +
                            '    """Attribute documentation."""\n' +
                            '    output = value + "!"\n    result = output.upper()\n',
                            encoding="utf-8")
        self.assertTrue(focused_scan(self.root)["exact"]["duplicates"])
        for path in (first, second):
            path.write_text("def left():\n    first = 1\n    second = first + 2\n\n" +
                            "def right():\n    third = 3\n    fourth = third + 4\n",
                            encoding="utf-8")
        self.assertIn("skipped", focused_scan(self.root)["exact"])
        first.write_text("def broken(:\n", encoding="utf-8")
        broken = prepared_report(self.root)
        self.assertEqual(broken["exit_code"], 2)
        self.assertEqual({item["name"]: item["status"] for item in broken["tools"]
                          if item["name"] in {"jscpd", "jscpd-renamed", "jscpd-similar"}},
                         {"jscpd": "error", "jscpd-renamed": "error", "jscpd-similar": "error"})
        self.assertTrue(any(item["is_infrastructure_error"] for item in broken["diagnostics"]))

    def test_real_wheel_same_file_class_docstrings_from_91(self) -> None:
        """Repeated class openings are inert; copied bodies still fail in one file."""
        (self.root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
        first = self.root / "src/demo/first.py"
        module = '''"""Shared module description."""
import json
import pathlib
import collections

'''
        scaffolding = '''    """Shared class description, first paragraph.
    Second paragraph.
    Third paragraph.
    """
    def build(self, value):
        """Shared method description."""
        pass
'''
        first.write_text(module + "\n".join(
            f"class {name}:\n{scaffolding}" for name in ("Records", "Profiles")),
            encoding="utf-8")
        empty = prepared_report(self.root)
        self.assertEqual(empty["exit_code"], 0, empty["diagnostics"])
        for name in ("jscpd", "jscpd-renamed", "jscpd-similar"):
            job = next(item for item in empty["tools"] if item["name"] == name)
            self.assertEqual((job["status"], job["exit_code"]), ("skipped", None))
        body = ("        initial = value + 17\n"
                "        result = initial * 3\n"
                "        return result - 4\n")
        first.write_text(module + "\n".join(
            f"class {name}:\n{scaffolding.replace('        pass', body.rstrip())}"
            for name in ("Records", "Profiles")), encoding="utf-8")
        found = prepared_report(self.root)
        native = prepared_native(self, self.root, found, 3)
        self.assertEqual(next(j for j in found["tools"] if j["name"] == "jscpd")["status"],
                         "violations", found["diagnostics"])
        self.assertTrue(any(clone["firstFile"]["name"] == clone["secondFile"]["name"]
                            == str(first) and body.strip() in clone["fragment"]
                            for clone in native["duplicates"]))
        self.assertEqual({d["path"] for d in found["diagnostics"]
                          if d["rule"] == "EXACT_CLONE"}, {"src/demo/first.py"})

    def test_real_wheel_behavioral_declarations(self) -> None:
        self._verify_excluded_declarations()
        self._verify_mixed_declarations()
        self._verify_alias_shadowing()
        self._verify_behavioral_bodies()
        verify_typed_dict_fields(self, self.root)

    def test_real_wheel_functional_typed_dict_fields(self) -> None:
        verify_functional_typed_dict_fields(self, self.root)

    def test_real_wheel_named_tuple_schemas(self) -> None:
        verify_named_tuple_schemas(self, self.root)

    def test_real_wheel_contract_methods(self) -> None:
        verify_contract_methods(self, self.root)
        verify_contract_properties(self, self.root)

    def _inspect_placeholder_source(self, source: str, eligible: int,
                                    clone: bool, exact_eligible: int | None = None,
                                    renamed_eligible: int | None = None
                                    ) -> dict[str, dict[str, Any]]:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        (self.root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
        for path in (first, second):
            path.write_text(source, encoding="utf-8")
        result = focused_scan(self.root)
        for kind, native in result.items():
            count = (exact_eligible if kind == "exact" and exact_eligible is not None else
                     renamed_eligible if kind == "renamed" and renamed_eligible is not None else
                     eligible)
            self.assertEqual(native["minimumUnits"], 3)
            self.assertEqual(native["eligibleUnits"][str(first)], count)
            self.assertEqual(native["unitSizes"][str(first)], [count] if count else [])
            self.assertEqual(native["sourcePaths"],
                             [str(self.root / path) for path in sorted((
                                 "src/demo/__init__.py", "src/demo/domain/api.py",
                                 "src/demo/first.py", "tests/second.py", "tests/test_value.py"))])
            if count >= 3:
                self.assertEqual(native["statistics"]["total"]["sources"],
                                 len(native["sourcePaths"]))
            if kind == "exact" and clone:
                self.assertTrue(native["duplicates"])
                self.assertEqual({part["name"] for item in native["duplicates"]
                                  for part in (item["firstFile"], item["secondFile"])},
                                 {str(first), str(second)})
            else:
                self.assertEqual("skipped" in native, count < 3)
                self.assertFalse(native.get("duplicates", []))
        return result

    def test_real_wheel_behavioral_placeholders(self) -> None:
        for form in PLACEHOLDERS:
            with self.subTest(form=form, material="stub with documentation"):
                result = self._inspect_placeholder_source(
                    'def action():\n    """Shared documentation."""\n' + f"    {form}\n",
                    0, False)
                self.assertTrue(all("skipped" in native for native in result.values()))
            for body in (f"    first = 1\n    second = first + 2\n    {form}\n",
                         f"    first = 1\n    {form}\n    second = first + 2\n"):
                with self.subTest(form=form, material="two executable lines"):
                    self._inspect_placeholder_source(
                        'def action():\n    """Shared documentation."""\n' + body,
                        2, False, renamed_eligible=1)
            with self.subTest(form=form, material="three executable lines"):
                result = self._inspect_placeholder_source(
                    'def action():\n    first = 1\n    second = first + 2\n' +
                    f"    {form}\n    third = second * 3\n", 3, True,
                    renamed_eligible=2)
                native = result["exact"]
                self.assertTrue(any(form in item["fragment"] and
                                    "third = second * 3" in item["fragment"]
                                    for item in native["duplicates"]))
                self.assertTrue(any(item["firstFile"]["start"] == 2 and
                                    item["firstFile"]["end"] == 5
                                    for item in native["duplicates"]))

    def test_real_wheel_method_placeholders(self) -> None:
        for wrapper in ("class Component:\n    def action(self):\n",
                        "from typing import Protocol\nclass Component(Protocol):\n" +
                        "    def action(self):\n",
                        "class Component:\n    def __call__(self):\n"):
            for form in PLACEHOLDERS:
                with self.subTest(wrapper=wrapper, form=form):
                    self._inspect_placeholder_source(
                        wrapper + '        """A method stub."""\n' +
                        f"        {form}\n", 0, False,
                        2 if "(Protocol)" in wrapper else None)
            with self.subTest(wrapper=wrapper, material="real body"):
                self._inspect_placeholder_source(
                    wrapper + "        first = 1\n        ...\n" +
                    "        second = first + 2\n        third = second * 3\n", 3, True,
                    renamed_eligible=2)

        for form in PLACEHOLDERS:
            with self.subTest(form=form, material="one-line stub"):
                self._inspect_placeholder_source(f"def action(): {form}\n", 0, False)

    def test_real_wheel_other_statements_remain_behavioral(self) -> None:
        for real in ("return None", "raise ValueError()",
                     "raise NotImplementedError(compute())",
                     "raise NotImplementedError from cause", "return ...",
                     "value = ..."):
            with self.subTest(real=real):
                self._inspect_placeholder_source(
                    "def action():\n    first = 1\n    second = first + 2\n" +
                    f"    {real}\n", 3, True,
                    renamed_eligible=1 if real in ("return None", "return ...", "value = ...")
                    else 2)

    def _verify_excluded_declarations(self) -> None:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        (self.root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
        excluded_forms = {
            "multiline class and decorators": (
                "@decorate(\n    mode=1,\n    enabled=True,\n)\n"
                "class First(\n    Parent,\n    metaclass=Meta,\n):\n"
                "    field: list[int]\n    other: dict[str, int]\n    last: bytes\n"),
            "multiline function annotations": (
                "@decorate\nasync def action(\n    first: list[int],\n"
                "    second: dict[str, int],\n    third: str,\n) -> bytes:\n"
                "    result: bytes\n    return b'yes'\n"),
            "pep 695 aliases": (
                "type First[T] = list[T]\n"
                "type Second[T] = dict[str, T]\n"
                "type Third = tuple[int, str]\n"),
            "explicit annotated aliases": (
                "from typing import TypeAlias as TA\n"
                "import typing as tp\n"
                "from typing_extensions import TypeAlias as TE\n"
                "First: TA = list[int]\n"
                "Second: tp.TypeAlias = dict[str, int]\n"
                "Third: TE = tuple[int, str]\n"),
        }
        for form, source in excluded_forms.items():
            with self.subTest(form=form):
                for path in (first, second):
                    path.write_text(source, encoding="utf-8")
                scans = focused_scan(self.root)
                self.assertTrue(all("skipped" in native for native in scans.values()))

    def _verify_mixed_declarations(self) -> None:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        (self.root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
        declarations = (
            "from typing import TypeAlias as TA\n"
            "import typing as tp\n"
            "from typing_extensions import TypeAlias as TE\n"
            "type Modern[T] = dict[str, list[T]]\n"
            "Legacy: TA = dict[str, list[int]]\n"
            "Qualified: tp.TypeAlias = list[int]\n"
            "Extended: TE = tuple[int, str]\n"
            "@decorate(\n    mode=1,\n    enabled=True,\n)\n"
            "class Widget(\n    Base,\n    metaclass=Meta,\n):\n"
            "    field: list[int]\n"
            "    @property\n"
            "    def value(\n        self,\n        argument: list[int],\n"
            "        another: str,\n    ) -> list[int]:\n"
            "        first = argument + [1]\n"
            "        second: list[int] = first + [2]\n"
        )
        for path in (first, second):
            path.write_text(declarations, encoding="utf-8")
        short = focused_scan(self.root)
        for kind in ("exact", "renamed", "similar"):
            with self.subTest(kind=kind, material="declarations plus two assignments"):
                native = short[kind]
                self.assertIn("skipped", native)
                self.assertEqual(native["unitSizes"][str(first)][-1], 2)
        for path in (first, second):
            path.write_text(declarations + "        third = second * 3\n", encoding="utf-8")
        long = focused_scan(self.root)
        exact = long["exact"]
        self.assertTrue(exact["duplicates"])
        self.assertTrue(any("first = argument + [1]" in item["fragment"]
                            for item in exact["duplicates"]))
        self.assertEqual({part["name"] for clone in exact["duplicates"]
                          for part in (clone["firstFile"], clone["secondFile"])},
                         {str(first), str(second)})
        second.write_text(declarations.replace("first = argument + [1]",
                                               "start = argument + [7]")
                          .replace("second: list[int] = first + [2]",
                                   "middle: list[int] = start + [8]")
                          + "        third = middle * 9\n", encoding="utf-8")
        changed = focused_scan(self.root)
        renamed = changed["renamed"]
        self.assertTrue(renamed["duplicates"])
        self.assertFalse(changed["exact"]["duplicates"])

    def _verify_alias_shadowing(self) -> None:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        (self.root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
        shadowed = ("from typing import TypeAlias as TA\n"
                    "def run(TA):\n"
                    "    first: TA = 1\n"
                    "    second: TA = first + 2\n"
                    "    third: TA = second * 3\n")
        for path in (first, second):
            path.write_text(shadowed, encoding="utf-8")
        self.assertTrue(focused_scan(self.root)["exact"]["duplicates"])
        relative = ("from .typing import TypeAlias as TA\n"
                    "first: TA = 1\n"
                    "second: TA = first + 2\n"
                    "third: TA = second * 3\n")
        for path in (first, second):
            path.write_text(relative, encoding="utf-8")
        self.assertTrue(focused_scan(self.root)["exact"]["duplicates"])

    def _verify_behavioral_bodies(self) -> None:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        (self.root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
        for path in (first, second):
            path.write_text("".join(("def decorate(func):\n    if func:\n",
                                     "        initial = func(1)\n",
                                     "        result: int = initial + 2\n",
                                     "        return result\n",
                                     "    return func\n")), encoding="utf-8")
        native = focused_scan(self.root)["exact"]
        self.assertTrue(native["duplicates"])
        self.assertTrue(any("if func:" in item["fragment"]
                            for item in native["duplicates"]))
        for path in (first, second):
            path.write_text("".join(("from typing import Protocol\n",
                                     "class Contract(Protocol):\n    def __init__(self):\n",
                                     "        self.value = 1\n        current = self.value + 2\n",
                                     "        self.value = current * 3\n")), encoding="utf-8")
        self.assertTrue(focused_scan(self.root)["exact"]["duplicates"])
        for path in (first, second):
            path.write_text("".join(("class First:\n    one: int\n    two: str\n",
                                     "    three: bytes\nclass Second:\n",
                                     "    one: int\n    two: str\n    three: bytes\n")),
                            encoding="utf-8")
        self.assertTrue(all("skipped" in native for native in
                            focused_scan(self.root).values()))

    def test_real_wheel_similar_prepared(self) -> None:
        verify_prepared_similar(self, self.root)
        verify_similar_boundaries(self, self.root)

    def test_real_wheel_similar_counts_complete_constructs(self) -> None:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        prefix = ("def calculate(value):\n    first = value + 17\n"
                  "    second = first * 3\n    third = second - 4\n")
        suffix = ("    fourth = value + 71\n    fifth = fourth * 8\n"
                  "    return fifth - 6\n")
        left_gap = ("    extra = (\n        value + 1\n    )\n"
                    "    another = value - 1\n")
        right_gap = ("    extra = (\n        value + 2\n    )\n"
                     "    another = value - 2\n")
        first.write_text(prefix + left_gap + suffix, encoding="utf-8")
        second.write_text(prefix + "    ignored: int\n    # excluded comment\n" +
                          right_gap + suffix, encoding="utf-8")
        scans = focused_scan(self.root)
        self.assertEqual(scans["similar"]["unitSizes"][str(first)], [8])
        self.assertEqual(scans["similar"]["unitSizes"][str(second)], [8])
        self.assertEqual(scans["similar"]["scannedUnitSizes"][str(second)], [8])
        self.assertTrue(scans["similar"]["duplicates"])
        self.assertTrue(all(item["kind"] == "similar" and item["method"] == "gap" and
                            item["unmatchedLines"] == [2, 2] and
                            0 < item["similarity"] < 1
                            for item in scans["similar"]["duplicates"]))
        checked = prepared_report(self.root)
        native = prepared_native(self, self.root, checked, 3, "similar")
        self.assertEqual(next(job for job in checked["tools"] if job["name"] ==
                              "jscpd-similar")["status"], "violations")
        self.assertTrue(any(item["rule"] == "SIMILAR_CLONE" and
                            item["native_details"]["method"] == "gap"
                            for item in checked["diagnostics"]))
        for item in native["duplicates"]:
            self.assertIn("value + 1" if item["firstFile"]["name"] == str(first)
                          else "value + 2", item["fragment"])
            for side in ("firstFile", "secondFile"):
                part = item[side]
                text = Path(part["name"]).read_text(encoding="utf-8")
                self.assertEqual(part["startLoc"]["position"],
                                 sum(len(line) for line in text.splitlines(keepends=True)[
                                     :part["start"] - 1]) + part["startLoc"]["column"])
                self.assertEqual(part["endLoc"]["position"],
                                 sum(len(line) for line in text.splitlines(keepends=True)[
                                     :part["end"] - 1]) + part["endLoc"]["column"])
        second.write_text(prefix + "    ignored: int\n" + right_gap +
                          "    third_gap = value / 9\n" + suffix, encoding="utf-8")
        self.assertFalse(focused_scan(self.root)["similar"]["duplicates"])
        for path in (first, second):
            path.write_text(prefix.replace("    third = second - 4\n", "") +
                            left_gap + suffix.replace("    return fifth - 6\n", ""),
                            encoding="utf-8")
        self.assertFalse(focused_scan(self.root)["similar"]["duplicates"])
        # Continuations alone cannot provide a third matching construct.
        two = ("def calculate(value):\n    first = (\n        value + 17\n    )\n"
               "    second = (\n        first * 3\n    )\n")
        first.write_text(two, encoding="utf-8")
        second.write_text(two, encoding="utf-8")
        self.assertEqual(focused_scan(self.root)["similar"]["unitSizes"][str(first)], [2])
        self.assertFalse(focused_scan(self.root)["similar"]["duplicates"])
        second.write_text(two.replace("17", "18"), encoding="utf-8")
        self.assertFalse(focused_scan(self.root)["similar"]["duplicates"])

    def test_real_wheel_similar_same_file_fragments_are_disjoint(self) -> None:
        first = self.root / "src/demo/first.py"
        prefix = "    one = 1\n    two = one + 2\n    three = two * 3\n"
        suffix = "    four = 4\n    five = four + 5\n    return five * 6\n"
        first.write_text("def repeated():\n" + prefix + "    gap = 7\n" + suffix +
                         "\ndef again():\n" + prefix + "    gap = 8\n" + suffix,
                         encoding="utf-8")
        similar = focused_scan(self.root)["similar"]
        self.assertTrue(similar["duplicates"])
        for clone in similar["duplicates"]:
            left, right = clone["firstFile"], clone["secondFile"]
            if left["name"] == right["name"]:
                self.assertTrue(left["end"] < right["start"] or
                                right["end"] < left["start"], clone)

    def test_real_wheel_similar_failure_keeps_peers(self) -> None:
        verify_similar_failure(self, self.root)

    def test_real_wheel_renamed_prepared(self) -> None:
        verify_prepared_renamed(self, self.root)

    def test_real_wheel_nested_composition_is_a_complete_renamed_unit(self) -> None:
        left = ("value = Outside(\n    Middle(\n        Branch(\n"
                "            Leaf(local, 17)\n        )\n    )\n)\n")
        right = ("value = Different(\n    Middle(\n        Branch(\n"
                 "            Leaf(other, 23)\n        )\n    )\n)\n")
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        first.write_text(left, encoding="utf-8")
        second.write_text(right, encoding="utf-8")
        result = prepared_report(self.root)
        exact = prepared_native(self, self.root, result, 3)
        renamed = prepared_native(self, self.root, result, 3, "renamed")
        self.assertEqual(exact["statistics"]["total"]["sources"],
                         renamed["statistics"]["total"]["sources"])
        matches = [item for item in renamed["duplicates"]
                   if {item["firstFile"]["name"], item["secondFile"]["name"]}
                   == {str(first), str(second)}]
        self.assertTrue(matches, renamed["duplicates"])
        self.assertEqual(renamed["candidateUnitSizes"][str(first)], [3])
        self.assertEqual(renamed["candidateUnitSizes"][str(second)], [3])
        self.assertTrue(all(item["kind"] == "renamed" for item in matches))
        self.assertTrue(any(item["firstFile"]["start"] == 2 and
                            item["firstFile"]["end"] == 6 and
                            "Leaf(local, 17)" in item["fragment"] for item in matches), matches)
        self.assertTrue(any(item["firstFile"]["startLoc"]["position"] ==
                            left.index("Middle(") and
                            item["firstFile"]["endLoc"]["position"] ==
                            left.index("    )\n)") + len("    )") and
                            item["fragment"] == "\n".join(left.splitlines()[1:6])
                            for item in matches), matches)
        jobs = {job["name"]: job for job in result["tools"]}
        self.assertEqual((jobs["jscpd"]["status"], jobs["jscpd-renamed"]["status"],
                          result["exit_code"]), ("passed", "violations", 1))
        self.assertTrue(any(d["rule"] == "RENAMED_CLONE" and
                            d["path"] == "src/demo/first.py" and d["line"] == 2
                            and d["native_details"] in matches
                            for d in result["diagnostics"]), result["diagnostics"])
        second.write_text(right.replace("other, 23", "local, 17"), encoding="utf-8")
        identical_inner = focused_scan(self.root)["renamed"]
        self.assertFalse([item for item in identical_inner.get("duplicates", [])
                          if item["firstFile"]["start"] == 2 and
                          item["firstFile"]["end"] == 6 and
                          item["secondFile"]["start"] == 2 and
                          item["secondFile"]["end"] == 6], identical_inner)
        second.write_text(right, encoding="utf-8")
        setting = self.root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "min_units = 3", "min_units = 6"), encoding="utf-8")
        larger_minimum = focused_scan(self.root)["renamed"]
        self.assertEqual(larger_minimum["minimumUnits"], 6)
        self.assertFalse(larger_minimum.get("duplicates", []), larger_minimum)

    def test_real_wheel_exact_requires_complete_constructs(self) -> None:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        left = ('def load(root):\n    records = (\n'
                '        arrow.dataset(root / "private_images.parquet", '
                'format="parquet").to_table().to_pylist()\n'
                '    )\n    if not records or len(records) != 7:\n'
                '        raise ValueError("private catalogue")\n')
        right = ('def load(samples):\n    rows = tuple(\n'
                 '        row for row in arrow.dataset(samples, format="ipc").to_table(\n'
                 '            columns=["path", "generator"]\n'
                 '        ).to_pylist()\n    )\n'
                 '    if any(row != "real" for row in rows):\n'
                 '        raise ValueError("wrong labels")\n')
        first.write_text(left, encoding="utf-8")
        second.write_text(right, encoding="utf-8")
        short = focused_scan(self.root)
        self.assertEqual(short["exact"]["minimumUnits"], 3)
        self.assertFalse(short["exact"].get("duplicates", []), short["exact"])
        self.assertEqual(short["exact"]["unitSizes"][str(first)], [3])
        self.assertEqual(short["similar"]["unitSizes"][str(first)], [3])

        assignments = ('def calculate():\n    first = 1\n'
                       '    second = first + 2\n    return second * 3\n')
        first.write_text(assignments, encoding="utf-8")
        second.write_text(assignments, encoding="utf-8")
        result = prepared_report(self.root)
        native = prepared_native(self, self.root, result, 3)
        matches = [item for item in native["duplicates"] if
                   {item["firstFile"]["name"], item["secondFile"]["name"]}
                   == {str(first), str(second)}]
        self.assertTrue(matches, native)
        self.assertTrue(any(item["kind"] == "exact" and
                            item["firstFile"]["start"] == 2 and
                            item["firstFile"]["end"] == 4 and
                            item["fragment"] == '\n'.join(assignments.splitlines()[1:])
                            for item in matches), matches)
        self.assertEqual(native["eligibleUnits"][str(first)], 3)
        self.assertTrue(any(d["rule"] == "EXACT_CLONE" and d["native_details"] in matches
                            for d in result["diagnostics"]), result["diagnostics"])

        three_assignments = ('def calculate():\n    first = 1\n'
                             '    second = first + 2\n    third = second * 3\n')
        first.write_text(three_assignments, encoding="utf-8")
        second.write_text(three_assignments, encoding="utf-8")
        self.assertTrue(focused_scan(self.root)["exact"]["duplicates"])
        setting = self.root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "min_units = 3", "min_units = 4"), encoding="utf-8")
        self.assertFalse(focused_scan(self.root)["exact"].get("duplicates", []))
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "min_units = 4", "min_units = 3"), encoding="utf-8")

        nested = ('def build(item):\n    value = Different(\n'
                  '        Middle(\n            Branch(\n'
                  '                Leaf(item, 17)\n            )\n        )\n    )\n')
        first.write_text(nested.replace("Different", "Outside"), encoding="utf-8")
        second.write_text(nested, encoding="utf-8")
        nested_report = prepared_report(self.root)
        exact_calls = prepared_native(self, self.root, nested_report, 3)
        calls = [item for item in exact_calls["duplicates"] if
                 {item["firstFile"]["name"], item["secondFile"]["name"]}
                 == {str(first), str(second)}]
        self.assertTrue(any(item["kind"] == "exact" and
                            item["firstFile"]["startLoc"]["position"] ==
                            first.read_text(encoding="utf-8").index("Middle(") and
                            item["fragment"] == '\n'.join(nested.splitlines()[2:7])
                            for item in calls), calls)

        two = ('def build():\n    first = wrap(\n        value,\n'
               '    )\n    second = wrap(\n        value,\n    )\n')
        first.write_text(two, encoding="utf-8")
        second.write_text(two, encoding="utf-8")
        short_multiline = focused_scan(self.root)["exact"]
        self.assertEqual(short_multiline["unitSizes"][str(first)], [2])
        self.assertFalse(short_multiline.get("duplicates", []), short_multiline)

    def test_real_wheel_renamed_respects_callable_identity_and_local_relations(self) -> None:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"

        def compare(left: str, right: str) -> list[dict[str, Any]]:
            first.write_text(left, encoding="utf-8")
            second.write_text(right, encoding="utf-8")
            return focused_scan(self.root)["renamed"].get("duplicates", [])

        left = ("class Actor: pass\n"
                "def build(value):\n"
                "    first = wire(Actor, value, 17)\n"
                "    second = wire(Actor, first, 18)\n"
                "    return wire(Actor, second, 19)\n")
        different = left.replace("Actor", "Other").replace("17", "21")
        self.assertFalse(compare(left, different))
        aliased = ("from demo.first import Actor as Alias\n"
                   "def build(other):\n"
                   "    start = wire(Alias, other, 21)\n"
                   "    middle = wire(Alias, start, 22)\n"
                   "    return wire(Alias, middle, 23)\n")
        matches = compare(left, aliased)
        self.assertTrue(matches, matches)
        self.assertEqual({item["kind"] for item in matches}, {"renamed"})
        self.assertTrue(any(item["fragment"] == "\n".join(left.splitlines()[2:5])
                            and item["firstFile"]["start"] == 3
                            and item["firstFile"]["end"] == 5 for item in matches), matches)

        constructed = ("class Actor: pass\n"
                       "def build(value):\n    a = Actor(value, 17)\n"
                       "    b = Actor(a, 18)\n    return Actor(b, 19)\n")
        self.assertFalse(compare(constructed, constructed.replace("Actor", "Other")
                                 .replace("17", "21")))
        alias_constructor = ("from demo.first import Actor as Renamed\n"
                             "def build(other):\n    x = Renamed(other, 21)\n"
                             "    y = Renamed(x, 22)\n    return Renamed(y, 23)\n")
        self.assertTrue(compare(constructed, alias_constructor))
        bound_constructor = ("from demo.first import Actor\n"
                             "Renamed = Actor\n"
                             "def build(other):\n    x = Renamed(other, 21)\n"
                             "    y = Renamed(x, 22)\n    return Renamed(y, 23)\n")
        self.assertTrue(compare(constructed, bound_constructor))
        module_constructor = ("import demo.first as source\n"
                              "def build(other):\n    x = source.Actor(other, 21)\n"
                              "    y = source.Actor(x, 22)\n    return source.Actor(y, 23)\n")
        self.assertTrue(compare(module_constructor, module_constructor.replace(
            "source", "origin").replace("21", "31").replace("22", "32")
            .replace("23", "33").replace("x =", "a =").replace("(x,", "(a,")
            .replace("y =", "b =").replace("(y,", "(b,")))
        qualified_value = ("import demo.first as source\n"
                           "def build(other):\n    x = wire(source.Actor, other, 21)\n"
                           "    y = wire(source.Actor, x, 22)\n"
                           "    return wire(source.Actor, y, 23)\n")
        self.assertTrue(compare(qualified_value, qualified_value.replace(
            "source", "origin").replace("21", "31").replace("22", "32")
            .replace("23", "33").replace("x =", "a =").replace(", x,", ", a,")
            .replace("y =", "b =").replace(", y,", ", b,")))
        self.assertFalse(compare(qualified_value,
                                 qualified_value.replace("source.Actor", "source.Other")
                                 .replace("21", "31")))

        function = ("def transform(value):\n    return value\n"
                    "def build(value):\n    a = transform(value, 17)\n"
                    "    b = transform(a, 18)\n    return transform(b, 19)\n")
        function_alias = ("from demo.first import transform as change\n"
                          "def build(other):\n    x = change(other, 21)\n"
                          "    y = change(x, 22)\n    return change(y, 23)\n")
        self.assertTrue(compare(function, function_alias))
        self.assertFalse(compare(function, function_alias.replace("transform", "different")))

        call = ("def run(value):\n    a = value.read(17)\n"
                "    b = value.read(a)\n    return value.read(b)\n")
        self.assertFalse(compare(call, call.replace("read", "write").replace("17", "23")))
        changed_call = ("def run(other):\n    x = other.read(23)\n"
                        "    y = other.read(x)\n    return other.read(y)\n")
        self.assertTrue(compare(call, changed_call))

        repeated = ("def run(value):\n    a = apply(value, value, 17)\n"
                    "    b = apply(a, a, 18)\n    return apply(b, b, 19)\n")
        distinct = ("def run(value, peer):\n    a = apply(value, peer, 21)\n"
                    "    b = apply(a, peer, 22)\n    return apply(b, peer, 23)\n")
        self.assertFalse(compare(repeated, distinct))
        unknown = ("def run(value):\n    a = mystery(value, 17)\n"
                   "    b = mystery(a, 18)\n    return mystery(b, 19)\n")
        self.assertFalse(compare(unknown, unknown.replace("mystery", "unresolved")))
        nested = ("value = Outer(\n    Middle(\n        Branch(\n"
                  "            Leaf(local, 17)\n        )\n    )\n)\n")
        self.assertFalse(compare(nested, nested.replace("Leaf", "Twig")
                                 .replace("17", "23")))

    def test_real_wheel_nested_calls_do_not_compare_overlapping_subtrees(self) -> None:
        source = self.root / "src/demo/first.py"
        source.write_text("value = Wrap(\n    First(\n        Second(\n"
                          "            Third(one, 17)\n        )),\n"
                          "    First(\n        Second(\n            Third(two, 23)\n"
                          "        )),\n)\n", encoding="utf-8")
        native = focused_scan(self.root)["renamed"]
        self.assertEqual(native["minimumUnits"], 3)
        self.assertTrue(any(item["firstFile"]["name"] == str(source) and
                            item["secondFile"]["name"] == str(source) and
                            item["firstFile"]["end"] < item["secondFile"]["start"]
                            for item in native["duplicates"]), native["duplicates"])
        source.write_text("value = Wrap(\n    Same(\n        Same(\n"
                          "            Same(\n                Same(item, 17)\n"
                          "            )\n        )\n    )\n)\n", encoding="utf-8")
        overlapping = focused_scan(self.root)["renamed"]
        self.assertFalse(overlapping.get("duplicates", []), overlapping)

    def test_real_wheel_renamed_field_and_keyword_roles(self) -> None:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"

        def compare(left: str, right: str) -> list[dict[str, Any]]:
            first.write_text(left, encoding="utf-8")
            second.write_text(right, encoding="utf-8")
            return focused_scan(self.root)["renamed"].get("duplicates", [])

        owner = ("class Record:\n"
                 "    def __init__(self):\n"
                 "        self.first = 1\n"
                 "        self.second = 2\n")
        field = ("def run(item: Record):\n"
                 "    a = item.first + 17\n"
                 "    b = item.first + a\n"
                 "    return item.first + b\n")
        imported = ("from demo.first import Record as Entry\n"
                    "def run(item: Entry):\n"
                    "    x = item.second + 23\n"
                    "    y = item.second + x\n"
                    "    return item.second + y\n")
        matches = compare(owner + field, imported)
        self.assertTrue(any(d["firstFile"]["start"] == 6 and
                            d["secondFile"]["start"] == 3 and
                            d["kind"] == "renamed" and
                            "item.first" in d["fragment"] for d in matches), matches)
        result = prepared_report(self.root)
        jobs = {job["name"]: job for job in result["tools"]}
        self.assertEqual((jobs["jscpd"]["status"], jobs["jscpd-renamed"]["status"],
                          jobs["jscpd-similar"]["status"]),
                         ("passed", "violations", "passed"))
        archived = prepared_native(self, self.root, result, 3, "renamed")
        self.assertTrue(any(d["fragment"] == "\n".join((owner + field).splitlines()[5:8])
                            and d["firstFile"]["startLoc"]["line"] == 6
                            and d["secondFile"]["startLoc"]["line"] == 3
                            for d in archived["duplicates"]), archived)
        self.assertFalse([d for d in result["diagnostics"] if d.get("is_infrastructure_error")])
        self.assertFalse(compare(owner + field, imported.replace(
            "y = item.second", "y = item.first").replace(
            "return item.second", "return item.first")))
        self.assertFalse(compare(owner + field, imported.replace("item.second", "item.unknown")))

        property_owner = ("class Record:\n"
                          "    @property\n"
                          "    def first(self):\n"
                          "        return 1\n"
                          "    @property\n"
                          "    def second(self):\n"
                          "        return 2\n")
        self.assertFalse(compare(property_owner + field, imported))
        aliased_property = property_owner.replace("class Record:",
                                                   "from builtins import property as view\n"
                                                   "class Record:").replace("@property", "@view")
        self.assertFalse(compare(aliased_property + field, imported))
        inherited_property = ("class Base:\n"
                              "    @property\n"
                              "    def first(self):\n"
                              "        return 1\n"
                              "class Record(Base):\n"
                              "    def __init__(self):\n"
                              "        self.first = 1\n"
                              "        self.second = 2\n")
        self.assertFalse(compare(inherited_property + field, imported))
        hooked = owner.replace("class Record:\n", "class Record:\n"
                               "    def __getattribute__(self, name):\n"
                               "        return object.__getattribute__(self, name)\n")
        self.assertFalse(compare(hooked + field, imported))
        rebound_owner = owner.replace("        self.first = 1\n",
                                      "        self = unknown\n        self.first = 1\n")
        self.assertFalse(compare(rebound_owner + field, imported))
        aliased_field = ("def run(item: Record):\n"
                         "    alias = item\n"
                         "    a = alias.first + 17\n"
                         "    b = alias.first + a\n"
                         "    return alias.first + b\n")
        imported_alias = ("from demo.first import Record as Entry\n"
                          "def run(item: Entry):\n"
                          "    alias = item\n"
                          "    x = alias.second + 23\n"
                          "    y = alias.second + x\n"
                          "    return alias.second + y\n")
        self.assertTrue(compare(owner + aliased_field, imported_alias))
        self.assertFalse(compare(owner + aliased_field,
                                 imported_alias.replace("alias = item", "alias = unknown")))
        self.assertFalse(compare(owner + aliased_field,
                                 imported_alias.replace("alias = item",
                                                        "alias = item\n    from external import value as alias")))
        self.assertFalse(compare(owner + field,
                                 imported.replace("item.second", "item.read")))
        constructed = ("def run():\n"
                       "    item = Record()\n"
                       "    a = item.first + 17\n"
                       "    b = item.first + a\n"
                       "    return item.first + b\n")
        imported_ctor = ("from demo.first import Record as Entry\n"
                         "def run():\n"
                         "    item = Entry()\n"
                         "    x = item.second + 23\n"
                         "    y = item.second + x\n"
                         "    return item.second + y\n")
        self.assertTrue(compare(owner + constructed, imported_ctor))

        keywords = ("def run(value):\n"
                    "    a = send(value, first=17, first_extra=18)\n"
                    "    b = send(a, first=19, first_extra=20)\n"
                    "    return send(b, first=21, first_extra=22)\n")
        renamed = keywords.replace("first_extra", "later").replace("first=", "second=")
        self.assertTrue(compare(keywords, renamed.replace("17", "23")))
        self.assertFalse(compare(keywords, renamed.replace(
            "second=19", "third=19").replace("second=21", "third=21")
                                 .replace("17", "23")))
        self.assertFalse(compare(keywords, renamed.replace("send(", "deliver(")
                                 .replace("17", "23")))

    def test_real_wheel_renamed_does_not_count_literal_or_punctuation_windows(self) -> None:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        first.write_text("values = {\n    'alpha',\n    'beta',\n    'gamma',\n"
                         "    'delta',\n    'epsilon',\n    'zeta',\n}\n", encoding="utf-8")
        second.write_text("unique = 1\n", encoding="utf-8")
        literal = focused_scan(self.root)["renamed"]
        self.assertEqual(literal["minimumUnits"], 3)
        self.assertFalse(literal.get("duplicates", []), literal)
        first.write_text("value = one(\n    x + y,\n)\nif flag:\n"
                         "    raise ValueError('left')\n", encoding="utf-8")
        second.write_text("value = two(\n    a / b,\n)\nif not condition:\n"
                          "    result = b * a\n", encoding="utf-8")
        continuation = focused_scan(self.root)["renamed"]
        self.assertFalse(continuation.get("duplicates", []), continuation)

    def test_real_wheel_renamed_failure_keeps_exact(self) -> None:
        verify_renamed_failure(self, self.root)

    def test_real_wheel_prepared_scope(self) -> None:
        verify_prepared_scope(self, self.root)

    def test_real_wheel_invalid_output_retains_independent_diagnostics(self) -> None:
        verify_corrupt_report(self, self.root)

    def test_real_wheel_missing_is_infrastructure_error(self) -> None:
        verify_missing_wheel(self, self.root)

    def test_real_wheel_engine_version_mismatch_is_error(self) -> None:
        verify_mismatched_engine(self, self.root)

    def test_real_wheel_same_file_and_mapping(self) -> None:
        verify_same_file_and_mapping(self, self.root)

    def test_real_wheel_full_consumer_without_rust(self) -> None:
        verify_real_full_check(self, self.root)

    @unittest.skip("Real uv wheel-install lifecycle disabled by project decision")
    def test_real_wheel_uv_install_separate_tool_without_rust(self) -> None:
        verify_real_wheel_install(self, self.root)

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path(mkdtemp(prefix="quard-switches-", dir="/tmp/opencode"))
        self.addCleanup(shutil.rmtree, self.root)
        create_repo(self.root)

    def setUp(self) -> None:
        pin_guard = patch("quard.check_runner.native_check_runner.require_source_pin")
        pin_guard.start()
        self.addCleanup(pin_guard.stop)

    def set_switch(self, key: str, enabled: bool) -> None:
        path = self.root / ".quard/config.toml"
        text = path.read_text(encoding="utf-8")
        previous, updated = ("false", "true") if enabled else ("true", "false")
        path.write_text(text.replace(f"{key} = {previous}", f"{key} = {updated}"),
                        encoding="utf-8")

    def run_report(self, fake: Callable[[list[str], Path, int], dict[str, Any]],
                   selection: str | None = None) -> dict[str, Any]:
        def versions(_root: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
            return {}, []

        with patch("quard.check_runner.native_check_runner.require_source_pin"), patch(
                "quard.check_runner.runner_utils.clone_engine_enabled", return_value=False):
            return NativeCheckRunner().run(self.root, lcom_files=selection, capture_fn=fake,
                                           versions_fn=versions)

    def test_each_independent_switch_skips_only_its_job_without_manifest(self):
        for key, name in OPTIONAL_JOBS.items():
            with self.subTest(key=key):
                self.set_switch(key, False)
                fake, seen = fake_tools()
                report = self.run_report(fake)
                jobs = {job["name"]: job for job in report["tools"]}
                run_dir = (self.root / report["report_path"]).parent
                self.assertEqual((report["exit_code"], jobs[name]["status"],
                                  jobs[name]["exit_code"]), (0, "skipped", None))
                self.assertNotIn(name, seen)
                self.assertFalse((run_dir / f"{name}-files.json").exists())
                self.assertEqual({other for other in OPTIONAL_JOBS.values()
                                  if jobs[other]["status"] == "skipped"}, {name})
                self.assertIn("pylint-production", seen)
                self.assertIn("pylint-tests", seen)
                if key == "lcom4":
                    self.assertEqual(report["coverage"]["lcom4"], {
                        "mode": "skipped", "requested": None,
                        "selected_files": [], "analyzed_files": []})
                self.set_switch(key, True)
                validate(report, read_json(QUARD / "report.schema.json"))
        # A single fully re-enabled report checks all workers after the switch matrix.
        fake, seen = fake_tools()
        report = self.run_report(fake)
        self.assertTrue(set(OPTIONAL_JOBS.values()) <= set(seen))
        self.assertTrue(all(job["status"] == "passed" for job in report["tools"]
                            if job["name"] in OPTIONAL_JOBS.values()))
        validate(report, read_json(QUARD / "report.schema.json"))

    def test_disabled_lcom_rejects_explicit_paths_and_empty_argument(self):
        self.set_switch("lcom4", False)
        for selection in ("", "src/demo/domain/api.py"):
            with self.subTest(selection=selection):
                fake, seen = fake_tools({"pydoclint": {
                    "stderr": "src/demo/domain/api.py:1: DOC101: Native issue\n", "code": 1}})
                report = self.run_report(fake, selection)
                job = next(item for item in report["tools"] if item["name"] == "lcom4")
                self.assertEqual((report["exit_code"], job["status"], job["exit_code"]),
                                 (2, "error", None))
                self.assertEqual(report["coverage"]["lcom4"], {
                    "mode": "invalid", "requested": selection,
                    "selected_files": [], "analyzed_files": []})
                self.assertEqual({item["rule"] for item in report["diagnostics"]},
                                 {"LCOM4_DISABLED", "DOC101"})
                self.assertNotIn("lcom4", seen)
                self.assertFalse(((self.root / report["report_path"]).parent /
                                  "lcom4-files.json").exists())
                validate(report, read_json(QUARD / "report.schema.json"))

    def test_selected_job_failure_and_disabled_peer_keep_native_results(self):
        self.set_switch("class_contracts", False)
        fake, seen = fake_tools({
            "method-decorators": {"stdout": "not JSON", "code": 1},
            "pydoclint": {"stderr": "src/demo/domain/api.py:1: DOC101: Native issue\n",
                          "code": 1}})
        report = self.run_report(fake, "src/demo/domain/api.py")
        jobs = {job["name"]: job for job in report["tools"]}
        self.assertEqual((report["exit_code"], jobs["method-decorators"]["status"],
                          jobs["class-contracts"]["status"]), (2, "error", "skipped"))
        self.assertNotIn("class-contracts", seen)
        self.assertIn("DOC101", {item["rule"] for item in report["diagnostics"]})
        self.assertTrue(any(item["is_infrastructure_error"]
                            for item in report["diagnostics"]))
        self.assertEqual(report["coverage"]["lcom4"]["mode"], "selected")

    def test_old_unified_config_defaults_new_checks_on_without_upgrading(self):
        path = self.root / ".quard/config.toml"
        text = path.read_text(encoding="utf-8")
        for key in (*OPTIONAL_JOBS, "duplicate_detection"):
            text = text.replace(f"{key} = true\n", "")
        path.write_text(text, encoding="utf-8")
        fake, seen = fake_tools()
        report = self.run_report(fake)
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(set(OPTIONAL_JOBS.values()) <= set(seen))
        self.assertTrue(set(DUPLICATE_JOBS) <= set(seen))

    def test_native_switches_skip_jobs_manifests_and_raw_output(self):
        native = {"pyright": ("pyright",), "pydoclint": ("pydoclint",),
                  "duplicate_detection": DUPLICATE_JOBS}
        for key, names in native.items():
            with self.subTest(key=key):
                self.set_switch(key, False)
                fake, seen = fake_tools()
                report = self.run_report(fake, "")
                jobs = {job["name"]: job for job in report["tools"]}
                run_dir = (self.root / report["report_path"]).parent
                self.assertEqual(report["exit_code"], 0, report["diagnostics"])
                for name in names:
                    self.assertNotIn(name, seen)
                    self.assertEqual((jobs[name]["status"], jobs[name]["exit_code"],
                                      jobs[name]["raw_output"]), ("skipped", None, {}))
                    self.assertFalse((run_dir / f"{name}-files.json").exists())
                    self.assertFalse((run_dir / "raw" / f"{name}.stdout.log").exists())
                if key == "duplicate_detection":
                    self.assertFalse((run_dir / "jscpd-config.json").exists())
                    self.assertFalse((run_dir / "raw/jscpd-report.json").exists())
                    self.assertFalse((run_dir / "raw/jscpd-renamed").exists())
                    self.assertFalse((run_dir / "raw/jscpd-similar").exists())
                self.assertIn("pylint-production", seen)
                self.assertIn("pylint-tests", seen)
                validate(report, read_json(QUARD / "report.schema.json"))
                self.set_switch(key, True)

    def test_mixed_disabled_and_enabled_native_failures_keep_diagnostics_and_versions(self):
        self.set_switch("pydoclint", False)
        self.set_switch("duplicate_detection", False)
        source = self.root / "src/demo/domain/api.py"
        fake, seen = fake_tools({"pyright": {"stdout": json.dumps({
            "generalDiagnostics": [{"file": str(source), "message": "Native issue",
                                    "severity": "error", "rule": "reportReturnType",
                                    "range": {"start": {"line": 0, "character": 0}}}]
        }), "code": 1}})
        report = self.run_report(fake)
        self.assertEqual((report["exit_code"], {d["rule"] for d in report["diagnostics"]}),
                         (1, {"reportReturnType"}))
        self.assertEqual(next(job for job in report["tools"] if job["name"] == "pyright")
                         ["exit_code"], 1)
        self.assertNotIn("pydoclint", seen)
        self.assertTrue(all(name not in seen for name in DUPLICATE_JOBS))

        actual_version = metadata.version

        def version(name: str) -> str:
            return "0.0.0" if name == "quard-clones" else actual_version(name)

        with patch("quard.check_runner.runner_utils.metadata.version", side_effect=version):
            report = NativeCheckRunner().run(self.root, serial=True, timeout=30,
                                             lcom_files=None, capture_fn=fake,
                                             versions_fn=installed_versions)
        self.assertEqual(report["exit_code"], 2)
        self.assertIn("TOOL_VERSION_MISMATCH", {d["rule"] for d in report["diagnostics"]})
        self.assertIn("reportReturnType", {d["rule"] for d in report["diagnostics"]})

    def test_disabling_all_native_switches_keeps_mandatory_pylint(self):
        for key in ("pyright", "pydoclint", "duplicate_detection"):
            self.set_switch(key, False)
        fake, seen = fake_tools()
        report = self.run_report(fake)
        skipped = ("pyright", "pydoclint", *DUPLICATE_JOBS)
        jobs = {job["name"]: job for job in report["tools"]}
        self.assertEqual(report["exit_code"], 0, report["diagnostics"])
        self.assertTrue(all(jobs[name]["status"] == "skipped" and
                            jobs[name]["exit_code"] is None for name in skipped))
        self.assertTrue(all(name not in seen for name in skipped))
        self.assertIn("pylint-production", seen)
        self.assertIn("pylint-tests", seen)
        validate(report, read_json(QUARD / "report.schema.json"))

    def test_reviewed_tach_selection_and_dependency_mode(self):
        for tach in (False, True):
            for dependency in (False, True):
                with self.subTest(tach=tach, dependency=dependency):
                    self.set_switch("tach", tach)
                    self.set_switch("dependency_types", dependency)
                    fake, seen = fake_tools({"pydoclint": {
                        "stderr": "src/demo/domain/api.py:1: DOC101: Native issue\n", "code": 1}})
                    report = self.run_report(fake)
                    jobs = {job["name"]: job for job in report["tools"]}
                    run_dir = (self.root / report["report_path"]).parent
                    self.assertEqual(report["exit_code"], 2 if dependency else 1)
                    self.assertIn("DOC101", {d["rule"] for d in report["diagnostics"]})
                    self.assertEqual(jobs["dependency-types"]["status"],
                                     "error" if dependency else "skipped")
                    for name in ("tach", "tach-external"):
                        self.assertEqual(jobs[name]["status"], "passed" if tach else "skipped")
                        self.assertEqual(name in seen, tach)
                        if not tach:
                            self.assertIsNone(jobs[name]["exit_code"])
                            self.assertFalse((run_dir / "raw" / f"{name}.stdout.log").exists())
                    self.assertNotIn("dependency-types", seen)
                    self.assertFalse(((self.root / report["report_path"]).parent /
                                      "dependency-types-files.json").exists())
                    self.assertFalse((run_dir / "raw/dependency-types.stdout.log").exists())
                    validate(report, read_json(QUARD / "report.schema.json"))

    def test_unaccepted_reviewed_policy_blocks_only_enabled_tach(self):
        (self.root / ".quard/architecture.ready.json").unlink()
        for tach in (False, True):
            with self.subTest(tach=tach):
                self.set_switch("tach", tach)
                fake, seen = fake_tools({"pydoclint": {
                    "stderr": "src/demo/domain/api.py:1: DOC101: Native issue\n", "code": 1}})
                report = self.run_report(fake)
                jobs = {job["name"]: job for job in report["tools"]}
                self.assertEqual(report["exit_code"], 2 if tach else 1)
                self.assertEqual({jobs[name]["status"] for name in ("tach", "tach-external")},
                                 {"blocked" if tach else "skipped"})
                self.assertEqual(jobs["dependency-types"]["status"], "skipped")
                self.assertNotIn("tach", seen)
                self.assertIn("pylint-production", seen)
                self.assertIn("DOC101", {d["rule"] for d in report["diagnostics"]})

    def test_generated_switches_regenerate_tach_even_without_tach_job(self):
        write_json(self.root / ".quard/architecture.generated.json", IDENTITY)
        for tach in (False, True):
            for dependency in (False, True):
                with self.subTest(tach=tach, dependency=dependency):
                    self.set_switch("tach", tach)
                    self.set_switch("dependency_types", dependency)
                    (self.root / "tach.toml").unlink(missing_ok=True)
                    fake, seen = fake_tools()
                    report = self.run_report(fake)
                    jobs = {job["name"]: job for job in report["tools"]}
                    self.assertEqual(report["exit_code"], 0, report["diagnostics"])
                    self.assertTrue((self.root / "tach.toml").is_file())
                    self.assertEqual(jobs["dependency-types"]["status"],
                                     "passed" if dependency else "skipped")
                    self.assertEqual("dependency-types" in seen, dependency)
                    self.assertEqual(jobs["tach"]["status"], "passed" if tach else "skipped")
                    self.assertEqual(jobs["tach-external"]["status"],
                                     "passed" if tach else "skipped")
                    if not dependency:
                        self.assertFalse(((self.root / report["report_path"]).parent /
                                          "dependency-types-files.json").exists())

    def test_generation_failure_blocks_selected_architecture_jobs_not_native_diagnostics(self):
        write_json(self.root / ".quard/architecture.generated.json", IDENTITY)
        (self.root / "src/demo/domain/api.py").write_text("def broken(:\n", encoding="utf-8")
        for tach in (False, True):
            for dependency in (False, True):
                with self.subTest(tach=tach, dependency=dependency):
                    self.set_switch("tach", tach)
                    self.set_switch("dependency_types", dependency)
                    fake, seen = fake_tools({"pydoclint": {
                        "stderr": "src/demo/domain/api.py:1: DOC101: Native issue\n", "code": 1}})
                    report = self.run_report(fake)
                    jobs = {job["name"]: job for job in report["tools"]}
                    self.assertEqual(report["exit_code"], 2)
                    self.assertEqual(jobs["dependency-types"]["status"],
                                     "blocked" if dependency else "skipped")
                    for name in ("tach", "tach-external"):
                        self.assertEqual(jobs[name]["status"], "blocked" if tach else "skipped")
                        self.assertNotIn(name, seen)
                    self.assertNotIn("dependency-types", seen)
                    self.assertIn("pylint-production", seen)
                    self.assertIn("DOC101", {d["rule"] for d in report["diagnostics"]})
                    self.assertTrue(any(d["is_infrastructure_error"]
                                        for d in report["diagnostics"]))

    def test_unexpected_generation_failure_still_runs_unrelated_jobs(self):
        write_json(self.root / ".quard/architecture.generated.json", IDENTITY)
        self.set_switch("dependency_types", True)
        fake, seen = fake_tools({"pydoclint": {
            "stderr": "src/demo/domain/api.py:1: DOC101: Native issue\n", "code": 1}})
        with patch("quard.check_runner.native_check_runner.regenerate",
                   side_effect=ValueError("unexpected generation failure")):
            report = self.run_report(fake)
        jobs = {job["name"]: job for job in report["tools"]}
        self.assertEqual(report["exit_code"], 2)
        self.assertTrue(all(jobs[name]["status"] == "blocked" for name in
                            ("tach", "tach-external", "dependency-types")))
        self.assertNotIn("tach", seen)
        self.assertNotIn("dependency-types", seen)
        self.assertIn("pylint-production", seen)
        self.assertEqual({"DOC101", "ARCHITECTURE_VALIDATION_FAILED"},
                         {d["rule"] for d in report["diagnostics"]})
