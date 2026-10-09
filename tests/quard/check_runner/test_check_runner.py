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
from quard.check_selection_utils import DUPLICATE_JOBS, OPTIONAL_JOBS
from quard.architecture_draft.generated_architecture_utils import IDENTITY
from offline_fixtures import create_repo, fake_tools
from contract_clone_utils import verify_contract_methods
from rust_clone_engine_utils import (verify_prepared_scope, verify_corrupt_report,
                                       verify_mismatched_engine, verify_missing_wheel,
                                       verify_real_full_check, verify_same_file_and_mapping,
                                       verify_prepared_renamed, verify_renamed_failure,
                                       verify_prepared_similar, verify_similar_boundaries,
                                       verify_similar_failure,
                                       prepared_native, report as prepared_report, versions, wheel)
from typed_dict_clone_utils import verify_functional_typed_dict_fields, verify_typed_dict_fields

PLACEHOLDERS = ("pass", "...", "raise NotImplementedError",
                "raise NotImplementedError()", 'raise NotImplementedError("pending")')


class TestCheckRunner(unittest.TestCase):
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

    def test_real_wheel_accepts_supported_python_syntax(self) -> None:
        python, _ = wheel()
        syntax = ('type Alias[T] = list[T]\n'
                  'def identity[T](value: T) -> T:\n'
                  '    match value:\n'
                  '        case [first, *rest]:\n'
                  '            return f"{first!s} {rest!r}"\n'
                  '    return value\n'
                  'try:\n    raise ExceptionGroup("sample", [ValueError()])\n'
                  'except* ValueError as errors:\n    result = str(errors)\n')
        script = ('import quard_clones, sys; '
                  'quard_clones.SourceSession([(sys.argv[1], sys.argv[2])], 3)')
        checked = capture([str(python), "-c", script, str(self.root / "src/demo/syntax.py"),
                           syntax], self.root, 30)
        self.assertEqual(checked["exit_code"], 0, checked["stderr"])

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
        self.assertTrue({str(first), str(second)} <= set(native["eligibleLines"]))
        (self.root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
        # Three imports at file scope are ineligible even though they are long
        # enough for an unprepared native scan.
        for path in (first, second):
            path.write_text('"""Common module documentation."""\n' +
                            "import json\nimport pathlib\nimport collections\n",
                            encoding="utf-8")
        excluded = prepared_report(self.root)
        for name in ("jscpd", "jscpd-renamed", "jscpd-similar"):
            job = next(item for item in excluded["tools"] if item["name"] == name)
            self.assertEqual((job["status"], job["exit_code"]), ("skipped", None))
            self.assertIn("prepared unit", job["error"])
        # Attribute documentation and method docstrings do not turn two
        # assignments into a clone; the same assignments plus a third do.
        for path in (first, second):
            path.write_text('class Component:\n    """Class documentation."""\n' +
                            '    field = "assigned value"\n    """Attribute documentation."""\n' +
                            '    def action(self):\n        """Method documentation."""\n' +
                            '        first = 1\n        second = first + 2\n', encoding="utf-8")
        short = prepared_report(self.root)
        self.assertFalse([item for item in short["diagnostics"] if item["tool"] == "jscpd"],
                         short["diagnostics"])
        for path in (first, second):
            with path.open("a", encoding="utf-8") as source:
                _ = source.write("        third = second * 3\n")
        long = prepared_report(self.root)
        self.assertEqual({item["path"] for item in long["diagnostics"]
                          if item["rule"] == "EXACT_CLONE"},
                         {"src/demo/first.py", "tests/second.py"})
        for path in (first, second):
            path.write_text('class Holder:\n    first = "assigned value"\n' +
                            '    """Attribute documentation."""\n' +
                            '    second = first + " more"\n    third = second.upper()\n',
                            encoding="utf-8")
        mapped = prepared_report(self.root)
        exact = next(item for item in mapped["tools"] if item["name"] == "jscpd")
        self.assertEqual(exact["status"], "violations", mapped["diagnostics"])
        payload = read_json(self.root / exact["raw_output"]["native_json"])
        self.assertTrue(any('"""Attribute documentation."""' in clone["fragment"]
                            for clone in payload["duplicates"]))
        self.assertTrue(any('"assigned value"' in clone["fragment"]
                            for clone in payload["duplicates"]))
        for path in (first, second):
            path.write_text('class Holder:\n    value = "café"\n' +
                            '    """Attribute documentation."""\n' +
                            '    output = value + "!"\n    result = output.upper()\n',
                            encoding="utf-8")
        unicode_result = prepared_report(self.root)
        self.assertEqual(next(item for item in unicode_result["tools"]
                              if item["name"] == "jscpd")["status"], "violations",
                         unicode_result["diagnostics"])
        for path in (first, second):
            path.write_text("def left():\n    first = 1\n    second = first + 2\n\n" +
                            "def right():\n    third = 3\n    fourth = third + 4\n",
                            encoding="utf-8")
        boundaries = prepared_report(self.root)
        self.assertEqual(next(item for item in boundaries["tools"]
                              if item["name"] == "jscpd")["status"], "skipped")
        first.write_text("def broken(:\n", encoding="utf-8")
        broken = prepared_report(self.root)
        self.assertEqual(broken["exit_code"], 2)
        self.assertEqual({item["name"]: item["status"] for item in broken["tools"]
                          if item["name"] in {"jscpd", "jscpd-renamed", "jscpd-similar"}},
                         {"jscpd": "error", "jscpd-renamed": "error", "jscpd-similar": "error"})
        self.assertTrue(any(item["is_infrastructure_error"] for item in broken["diagnostics"]))

    def test_real_wheel_behavioral_declarations(self) -> None:
        self._verify_excluded_declarations()
        self._verify_mixed_declarations()
        self._verify_alias_shadowing()
        self._verify_behavioral_bodies()
        verify_typed_dict_fields(self, self.root)

    def test_real_wheel_functional_typed_dict_fields(self) -> None:
        verify_functional_typed_dict_fields(self, self.root)

    def test_real_wheel_contract_methods(self) -> None:
        verify_contract_methods(self, self.root)

    def _inspect_placeholder_source(self, source: str, eligible: int,
                                    clone: bool, exact_eligible: int | None = None
                                    ) -> dict[str, Any]:
        first = self.root / "src/demo/first.py"
        second = self.root / "tests/second.py"
        (self.root / "src/demo/domain/api.py").write_text("", encoding="utf-8")
        names = {"exact": "jscpd", "renamed": "jscpd-renamed",
                 "similar": "jscpd-similar"}
        for path in (first, second):
            path.write_text(source, encoding="utf-8")
        result = prepared_report(self.root)
        self.assertEqual(result["exit_code"], int(clone), result["diagnostics"])
        self.assertFalse([item for item in result["diagnostics"]
                          if item.get("is_infrastructure_error", False)])
        manifest = read_json((self.root / result["report_path"]).parent /
                             "clone-engine-files.json")
        for kind, name in names.items():
            job = next(item for item in result["tools"] if item["name"] == name)
            native = read_json(self.root / job["raw_output"]["native_json"])
            count = exact_eligible if kind == "exact" and exact_eligible is not None else eligible
            self.assertEqual(native["minimum"], 3)
            self.assertEqual(native["eligibleLines"][str(first)], count)
            self.assertEqual(native["unitLines"][str(first)], [count] if count else [])
            self.assertEqual(native["sourcePaths"],
                             [str(self.root / path) for path in manifest["paths"]])
            if count >= 3:
                self.assertEqual(native["statistics"]["total"]["sources"],
                                 len(native["sourcePaths"]))
            if kind == "exact" and clone:
                self.assertEqual((job["status"], job["exit_code"]), ("violations", 1))
                self.assertTrue(native["duplicates"])
                self.assertEqual({d["path"] for d in result["diagnostics"]
                                  if d["rule"] == "EXACT_CLONE"},
                                 {"src/demo/first.py", "tests/second.py"})
            else:
                self.assertEqual((job["status"], job["exit_code"]),
                                 ("passed", 0) if count >= 3 else ("skipped", None))
                self.assertFalse(native.get("duplicates", []))
        return result

    def test_real_wheel_behavioral_placeholders(self) -> None:
        for form in PLACEHOLDERS:
            with self.subTest(form=form, material="stub with documentation"):
                result = self._inspect_placeholder_source(
                    'def action():\n    """Shared documentation."""\n' + f"    {form}\n",
                    0, False)
                names = ("jscpd", "jscpd-renamed", "jscpd-similar")
                for name in names:
                    job = next(item for item in result["tools"] if item["name"] == name)
                    self.assertIn("skipped", read_json(self.root /
                                  job["raw_output"]["native_json"]))
            for body in (f"    first = 1\n    second = first + 2\n    {form}\n",
                         f"    first = 1\n    {form}\n    second = first + 2\n"):
                with self.subTest(form=form, material="two executable lines"):
                    self._inspect_placeholder_source(
                        'def action():\n    """Shared documentation."""\n' + body, 2, False)
            with self.subTest(form=form, material="three executable lines"):
                result = self._inspect_placeholder_source(
                    'def action():\n    first = 1\n    second = first + 2\n' +
                    f"    {form}\n    third = second * 3\n", 3, True)
                native = prepared_native(self, self.root, result, 3)
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
                    "        second = first + 2\n        third = second * 3\n", 3, True)

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
                    f"    {real}\n", 3, True)

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
                result = prepared_report(self.root)
                for name in ("jscpd", "jscpd-renamed", "jscpd-similar"):
                    job = next(item for item in result["tools"] if item["name"] == name)
                    self.assertEqual((job["status"], job["exit_code"]), ("skipped", None))

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
        short = prepared_report(self.root)
        for kind in ("exact", "renamed", "similar"):
            with self.subTest(kind=kind, material="declarations plus two assignments"):
                job = next(job for job in short["tools"] if job["name"] ==
                           {"exact": "jscpd", "renamed": "jscpd-renamed",
                            "similar": "jscpd-similar"}[kind])
                native = read_json(self.root / job["raw_output"]["native_json"])
                self.assertEqual((job["status"], job["exit_code"]), ("skipped", None))
                self.assertIn("skipped", native)
                self.assertEqual(native["unitLines"][str(first)][-1], 2)
        for path in (first, second):
            path.write_text(declarations + "        third = second * 3\n", encoding="utf-8")
        long = prepared_report(self.root)
        exact = prepared_native(self, self.root, long, 3)
        self.assertTrue(exact["duplicates"])
        self.assertTrue(any("first = argument + [1]" in item["fragment"]
                            for item in exact["duplicates"]))
        self.assertEqual({item["path"] for item in long["diagnostics"]
                          if item["rule"] == "EXACT_CLONE"},
                         {"src/demo/first.py", "tests/second.py"})
        second.write_text(declarations.replace("first = argument + [1]",
                                               "start = argument + [7]")
                          .replace("second: list[int] = first + [2]",
                                   "middle: list[int] = start + [8]")
                          + "        third = middle * 9\n", encoding="utf-8")
        changed = prepared_report(self.root)
        renamed = prepared_native(self, self.root, changed, 3, "renamed")
        self.assertTrue(renamed["duplicates"])
        self.assertFalse(prepared_native(self, self.root, changed, 3)["duplicates"])

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
        rebound = prepared_report(self.root)
        self.assertTrue(prepared_native(self, self.root, rebound, 3)["duplicates"])
        relative = ("from .typing import TypeAlias as TA\n"
                    "first: TA = 1\n"
                    "second: TA = first + 2\n"
                    "third: TA = second * 3\n")
        for path in (first, second):
            path.write_text(relative, encoding="utf-8")
        local = prepared_report(self.root)
        self.assertTrue(prepared_native(self, self.root, local, 3)["duplicates"])

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
        checked = prepared_report(self.root)
        native = prepared_native(self, self.root, checked, 3)
        self.assertTrue(native["duplicates"])
        self.assertTrue(any("if func:" in item["fragment"]
                            for item in native["duplicates"]))
        for path in (first, second):
            path.write_text("".join(("from typing import Protocol\n",
                                     "class Contract(Protocol):\n    def __init__(self):\n",
                                     "        self.value = 1\n        current = self.value + 2\n",
                                     "        self.value = current * 3\n")), encoding="utf-8")
        special = prepared_report(self.root)
        self.assertTrue(prepared_native(self, self.root, special, 3)["duplicates"])
        for path in (first, second):
            path.write_text("".join(("class First:\n    one: int\n    two: str\n",
                                     "    three: bytes\nclass Second:\n",
                                     "    one: int\n    two: str\n    three: bytes\n")),
                            encoding="utf-8")
        excluded = prepared_report(self.root)
        for name in ("jscpd", "jscpd-renamed", "jscpd-similar"):
            job = next(item for item in excluded["tools"] if item["name"] == name)
            self.assertEqual((job["status"], job["exit_code"]), ("skipped", None))

    def test_real_wheel_similar_prepared(self) -> None:
        verify_prepared_similar(self, self.root)
        verify_similar_boundaries(self, self.root)

    def test_real_wheel_similar_failure_keeps_peers(self) -> None:
        verify_similar_failure(self, self.root)

    def test_real_wheel_renamed_prepared(self) -> None:
        verify_prepared_renamed(self, self.root)

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

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path(mkdtemp(prefix="quard-switches-", dir="/tmp/opencode"))
        self.addCleanup(shutil.rmtree, self.root)
        create_repo(self.root)

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
                fake, seen = fake_tools()
                report = self.run_report(fake)
                self.assertIn(name, seen)
                self.assertEqual(next(job for job in report["tools"] if job["name"] == name)
                                 ["status"], "passed")
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
            return "0.0.0" if name == "jscpd" else actual_version(name)

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
