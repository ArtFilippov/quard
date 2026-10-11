"""Autonomous naming, selection and normalized-report regressions."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import validate

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from quard._common import capture
from quard._policy import discover_files, read_scope
from quard.check import execute_job, make_jobs, run_checks
from quard.check_selection_utils import validate_checks
from quard.leading_underscores_utils import analyze
from offline_fixtures import create_repo, fake_tools, native_result


class TestLeadingUnderscores(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        create_repo(self.root)
        self.run_dir = self.root / ".quard/reports/run"
        self.run_dir.mkdir(parents=True)

    def put(self, path: str, code: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")

    def job(self):
        files = discover_files(self.root, read_scope(self.root))
        return next(job for job in make_jobs(self.root, self.run_dir, files, True, [])
                    if job["name"] == "leading-underscores")

    def test_nested_bindings_stubs_tests_paths_and_exclusions(self):
        self.put("src/_pkg/_inner/_module.pyi", """
from external import _original, something as _local
class _Outer:
    __slots__ = ("_slot", "__dict__")
    class _Nested:
        def _method(self, _arg, *, __custom__):
            self._field = _arg
            setattr(self, "_via_setattr", 1)
            external._attribute = 1
            def _inner(_param): ...
            return _inner
def __init__(): ...
def __custom__(): ...
value, _binding = 1, 2
""")
        self.put("tests/_helpers/test__case.py", "async def _test(_parameter):\n"
                 "    for _loop in (): pass\n    match 1:\n        case _capture: pass\n")
        self.put("src/demo/generated/_ignored.py", "class _Ignored: pass\n")
        self.put("src/demo/unicode.py", 'label = "é"; _after = 1\n'
                 'from external import (\n    item as _across,\n)\n')
        files = discover_files(self.root, read_scope(self.root))
        paths = sorted(files["production"] + files["tests"])
        self.assertNotIn("src/demo/generated/_ignored.py", paths)
        findings, error = analyze(self.root, paths)
        self.assertIsNone(error)
        names = {d["symbol"] for d in findings if d["rule"] == "LEADING_UNDERSCORE_NAME"}
        self.assertTrue({"_Outer", "_Nested", "_method", "_arg", "__custom__", "_field",
                         "_via_setattr", "_slot", "_inner", "_param", "_binding", "_local",
                         "_test", "_parameter", "_loop", "_capture"} <= names)
        self.assertFalse({"_original", "_attribute", "__dict__", "__init__", "_Ignored", "_"} & names)
        path_names = {d["symbol"] for d in findings if d["rule"] == "LEADING_UNDERSCORE_PATH"}
        self.assertEqual(path_names, {"_pkg", "_inner", "_module", "_helpers"})
        self.assertTrue(all(d.get("line", 0) >= 1 and d.get("column", 0) >= 1
                            for d in findings if d["rule"] == "LEADING_UNDERSCORE_NAME"))
        self.assertTrue(all("line" not in d for d in findings
                            if d["rule"] == "LEADING_UNDERSCORE_PATH"))
        self.assertIn((1, 15), {(d["line"], d["column"]) for d in findings
                                 if d.get("symbol") == "_after"})
        self.assertIn((3, 13), {(d["line"], d["column"]) for d in findings
                                 if d.get("symbol") == "_across"})

    def test_python_special_names_and_placeholders_are_allowed(self):
        self.put("src/demo/__main__.py", """
__all__ = []
_ = 1
class Model:
    __slots__ = ("__dict__",)
    def __init__(self, _):
        self.__dict__ = {}
        self.__custom__ = 1
    def __post_init__(self): pass
    def __str__(self): return "ok"
""")
        self.put("src/demo/__custom__/__init__.py", "\n")
        items, error = analyze(self.root, ["src/demo/__init__.py", "src/demo/__main__.py"])
        self.assertIsNone(error)
        self.assertEqual({d["symbol"] for d in items}, {"__custom__"})
        paths, error = analyze(self.root, ["src/demo/__custom__/__init__.py"])
        self.assertIsNone(error)
        self.assertEqual([(d["rule"], d["symbol"]) for d in paths],
                         [("LEADING_UNDERSCORE_PATH", "__custom__")])

    def test_exception_data_names_are_special_but_invented_dunders_are_not(self):
        self.put("src/demo/exception.py", '''__traceback__ = None
__cause__ = None
__context__ = None
__suppress_context__ = False
__notes__ = []
__annotate__ = None
__base__ = None
__value__ = None
__invented__ = 1
class Error(Exception):
    def __init__(self):
        self.__traceback__ = None
        self.__cause__ = None
        self.__context__ = None
        self.__suppress_context__ = False
        self.__notes__ = []
        self.__invented__ = 1
''')
        findings, error = analyze(self.root, ["src/demo/exception.py"])
        self.assertIsNone(error)
        self.assertEqual([(d["symbol"], d["line"]) for d in findings],
                         [("__invented__", 9), ("__invented__", 17)])

    def test_unaliased_imports_bind_local_names_only_for_first_party_modules(self):
        self.put("src/pkg/__init__.py", "\n")
        self.put("src/pkg/_local.py", "_thing = 1\n")
        self.put("src/_pkg/__init__.py", "\n")
        self.put("src/pkg/client.py", '''import _pkg
import _pkg.component
from ._local import _thing
from pkg._local import _thing
from . import _local
from pkg import _local
import pkg._local
import _external
from external import _remote
from external.module import _remote
from pkg._local import _thing as _renamed
''')
        self.put("src/pkg/client.pyi", "from ._local import _thing\nfrom vendor import _external\n")
        self.put("tests/test_imports.py", "from pkg import _local\nfrom vendor import _outside\n")
        paths = ["src/pkg/__init__.py", "src/pkg/_local.py", "src/_pkg/__init__.py",
                 "src/pkg/client.py", "src/pkg/client.pyi", "tests/test_imports.py"]
        findings, error = analyze(self.root, paths)
        self.assertIsNone(error)
        bindings = [(d["symbol"], d["line"], d["column"]) for d in findings
                    if d["path"] == "src/pkg/client.py" and d["rule"] == "LEADING_UNDERSCORE_NAME"]
        self.assertEqual(bindings, [("_pkg", 1, 8), ("_pkg", 2, 8),
                                    ("_thing", 3, 21), ("_thing", 4, 24),
                                    ("_local", 5, 15), ("_local", 6, 17),
                                    ("_renamed", 11, 34)])
        self.assertEqual({(d["path"], d["symbol"]) for d in findings
                          if d["rule"] == "LEADING_UNDERSCORE_NAME" and d["path"] in paths[4:]},
                         {("src/pkg/client.pyi", "_thing"), ("tests/test_imports.py", "_local")})
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual({(d["symbol"], d["line"]) for d in result["diagnostics"]
                          if d["path"] == "src/pkg/client.py" and d["rule"] == "LEADING_UNDERSCORE_NAME"},
                         {(name, line) for name, line, _ in bindings})
        self.assertFalse(any(d["is_infrastructure_error"] for d in result["diagnostics"]))

    def test_proven_attribute_writes_and_dictionary_forms_in_source_and_stub(self):
        code = '''import builtins as bi
from builtins import setattr as put, vars as mapping
class Model:
    __slots__ = "_slot"
    def __init__(self):
        alias = self
        alias._direct = 1
        bi.setattr(alias, "_builtin", 2)
        put(self, "_imported", 3)
        mapping(self)["_mapped"] = 4
        self.__dict__["_dictionary"] = 5
        mapping(self).__setitem__("_method", 6)
        self.__dict__.update({"_updated": 7})
        self.__dict__.setdefault("_defaulted", 8)
        dictionary = vars(self)
        dictionary["_alias_dict"] = 1
        dictionary.update(_keyword=1)
        dictionary.setdefault("_implicit")
model = Model()
model._outside = 9
setattr(Model, "_class_attribute", 1)
'''
        self.put("src/demo/model.py", code)
        self.put("src/demo/model.pyi", 'class Model:\n    __slots__ = ("_stub_slot",)\n'
                 '    def accept(self, other: "Model") -> None:\n'
                 '        other.__dict__["_stub_dict"] = 1\n'
                 '        other._stub_attr = 2\n')
        paths = ["src/demo/model.py", "src/demo/model.pyi"]
        findings, error = analyze(self.root, paths)
        self.assertIsNone(error)
        locations = {(d["path"], d["symbol"]): (d["line"], d["column"])
                     for d in findings if d["rule"] == "LEADING_UNDERSCORE_NAME"}
        for name in ("_slot", "_direct", "_builtin", "_imported", "_mapped",
                     "_dictionary", "_method", "_updated", "_defaulted", "_alias_dict",
                     "_keyword", "_implicit", "_outside",
                     "_class_attribute"):
            self.assertIn((paths[0], name), locations)
        for name in ("_stub_slot", "_stub_dict", "_stub_attr"):
            self.assertIn((paths[1], name), locations)
        self.assertEqual(locations[(paths[0], "_direct")], (7, 15))
        self.assertEqual(locations[(paths[0], "_builtin")], (8, 27))
        self.assertEqual(locations[(paths[0], "_alias_dict")], (16, 20))
        self.assertTrue(all(line > 0 and column > 0 for line, column in locations.values()))
        job = self.job()
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertIn("_stub_dict", {d["symbol"] for d in result["diagnostics"]})
        self.assertFalse(any(d["is_infrastructure_error"] for d in result["diagnostics"]))

    def test_external_unknown_and_ambiguous_owners_are_not_declarations(self):
        self.put("src/demo/owners.py", '''from external import Client
class Local:
    def __init__(self, peer: Client):
        peer._external = 1
        setattr(peer, "_external_set", 1)
        peer.__dict__["_external_dict"] = 1
        self._local = 1
        alias = self
        alias = peer
        alias._ambiguous = 1
        if peer:
            maybe = self
        else:
            maybe = peer
        maybe._branch = 1
        setattr(maybe, "_branch_set", 1)
        vars(maybe)["_branch_dict"] = 1
    def no_proof(self, unknown):
        unknown._unknown = 1
        unknown.__dict__["_unknown_dict"] = 1
        setattr(unknown, "_unknown_set", 1)
    @staticmethod
    def static(peer):
        peer._static = 1
text = "_ordinary_string"
data = {"_ordinary_key": 1}
name = "_computed"
setattr(Local(), name, 1)
setattr(Local(), "__init__", 1)
setattr(Local(), "_", 1)
''')
        findings, error = analyze(self.root, ["src/demo/owners.py"])
        self.assertIsNone(error)
        self.assertEqual({d["symbol"] for d in findings if d["rule"] == "LEADING_UNDERSCORE_NAME"},
                         {"_local"})

    def test_disabled_naming_keeps_other_jobs_and_never_starts_worker(self):
        self.put("src/demo/_bad.py", "class _Bad: pass\n")
        config = self.root / ".quard/config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace(
            "leading_underscores = true", "leading_underscores = false").replace(
            "duplicate_detection = true", "duplicate_detection = false"), encoding="utf-8")
        fake, seen = fake_tools()

        def guarded(argv, cwd, timeout):
            self.assertNotIn("_leading_underscores_worker.py", " ".join(argv))
            return fake(argv, cwd, timeout)

        with patch("quard.check_runner.native_check_runner.require_source_pin"):
            report = run_checks(self.root, capture_fn=guarded, versions_fn=lambda _: ({}, []))
        self.assertEqual((report["status"], report["exit_code"]), ("passed", 0))
        self.assertTrue(seen)
        jobs = {job["name"]: job for job in report["tools"]}
        self.assertEqual((jobs["leading-underscores"]["status"],
                          jobs["leading-underscores"]["exit_code"]), ("skipped", None))
        self.assertIn("instance-fields", jobs)
        self.assertIn("encapsulated-only", jobs)
        self.assertIn("class-contracts", jobs)

    def test_worker_normalization_and_error_semantics(self):
        self.put("src/demo/_bad.py", "class _Bad:\n    pass\n")
        job = self.job()
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"LEADING_UNDERSCORE_NAME", "LEADING_UNDERSCORE_PATH"})
        self.assertTrue(all(not d["is_infrastructure_error"] for d in result["diagnostics"]))
        self.put("src/demo/_broken.pyi", "def bad(\n")
        job = self.job()
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
        self.assertIn("LEADING_UNDERSCORES_PARSE_ERROR", {d["rule"] for d in result["diagnostics"]})
        self.assertIn("LEADING_UNDERSCORE_NAME", {d["rule"] for d in result["diagnostics"]})
        for payload, code in (("bad JSON", 0), (json.dumps({
                "quard_leading_underscores_adapter": 1, "diagnostics": [{
                    "rule": "LEADING_UNDERSCORE_NAME", "path": "src/demo/_bad.py",
                    "line": True, "column": 1, "symbol": "_Bad", "message": "bad"}],
                "execution_error": None}), 1), (json.dumps({
                    "quard_leading_underscores_adapter": 1, "diagnostics": [],
                    "execution_error": None}), 1)):
            with self.subTest(payload=payload):
                result = execute_job(job, self.root, self.run_dir, 30,
                                     capture_fn=lambda argv, cwd, timeout: native_result(
                                         argv, stdout=payload, code=code))
                self.assertEqual(result["status"], "error")
                self.assertTrue(any(d["is_infrastructure_error"] for d in result["diagnostics"]))
        Path(job["argv"][-1]).write_text('["../escape.py"]', encoding="utf-8")
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))

    def test_default_on_upgrade_disable_and_full_report(self):
        self.put("src/demo/_bad.py", "_value = 1\n")
        config = self.root / ".quard/config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace(
            "duplicate_detection = true", "duplicate_detection = false"), encoding="utf-8")
        fake, seen = fake_tools()

        def mixed(argv, cwd, timeout):
            if str(QUARD / "_leading_underscores_worker.py") in argv:
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        for lcom in (None, "", "src/demo/__init__.py"):
            with self.subTest(lcom=lcom):
                with patch("quard.check_runner.native_check_runner.require_source_pin"):
                    report = run_checks(self.root, lcom_files=lcom, capture_fn=mixed,
                                        versions_fn=lambda _: ({}, []))
                validate(report, json.loads((QUARD / "report.schema.json").read_text(encoding="utf-8")))
                self.assertEqual((report["status"], report["exit_code"]), ("violations", 1),
                                 report["diagnostics"])
                self.assertEqual(next(t for t in report["tools"] if t["name"] == "leading-underscores")
                                 ["status"], "violations")
                self.assertEqual({d["rule"] for d in report["diagnostics"]},
                                 {"LEADING_UNDERSCORE_NAME", "LEADING_UNDERSCORE_PATH"})
        self.assertTrue(seen)
        config.write_text(config.read_text(encoding="utf-8").replace(
            "leading_underscores = true", "leading_underscores = false"), encoding="utf-8")
        self.assertTrue(validate_checks({"constructor_purity": False})["leading_underscores"])
        with patch("quard.check_runner.native_check_runner.require_source_pin"):
            report = run_checks(self.root, capture_fn=mixed, versions_fn=lambda _: ({}, []))
        self.assertEqual((report["status"], report["exit_code"]), ("passed", 0),
                         report["diagnostics"])
        job = next(t for t in report["tools"] if t["name"] == "leading-underscores")
        self.assertEqual((job["status"], job["exit_code"], job["raw_output"]),
                         ("skipped", None, {}))
        self.assertFalse((self.root / report["report_path"]).parent.joinpath(
            "leading-underscores-files.json").exists())

    def test_checkout_findings_are_real_violations(self):
        files = discover_files(QUARD, read_scope(QUARD))
        self.assertIn("src/quard/_common.py", files["production"])
        self.assertIn("tests/quard/leading_underscores/test_leading_underscores.py", files["tests"])
        findings, error = analyze(QUARD, sorted(files["production"] + files["tests"]))
        self.assertIsNone(error)
        self.assertIn(("LEADING_UNDERSCORE_PATH", "src/quard/_common.py"),
                      {(d["rule"], d["path"]) for d in findings})
        self.assertTrue(any(d["rule"] == "LEADING_UNDERSCORE_NAME" for d in findings))


if __name__ == "__main__":
    unittest.main()
