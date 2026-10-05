"""Real worker and report regressions for the constructor syntax boundary."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import override
import unittest

from jsonschema import validate

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from quard._common import capture
from quard._policy import discover_files, read_scope
from quard.check import execute_job, make_jobs, run_checks
from offline_fixtures import create_repo, fake_tools, native_result


class TestConstructorChecker(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.temp: TemporaryDirectory[str] = TemporaryDirectory(prefix="quard-constructor-")
        self.addCleanup(self.temp.cleanup)
        self.root: Path = Path(self.temp.name)
        self.run_dir: Path = self.root / ".quard/reports/run"

    @override
    def setUp(self):
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / ".quard").mkdir()
        (self.root / ".quard/config.toml").write_text(
            (QUARD / "defaults/config.toml").read_text().split("[scope]")[0]
            + '[scope]\nexclude_directory_names = ["generated"]\n'
            'test_directory_names = ["tests"]\n', encoding="utf-8")
        self.run_dir.mkdir(parents=True)

    def put(self, path, source):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    def run_worker(self):
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [], True)
                   if job["name"] == "constructor-purity")
        result = execute_job(job, self.root, self.run_dir, 30)
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        return result, raw, job

    def test_allowed_syntax_and_direct_calls_in_nested_and_test_classes(self):
        self.put("src/good.py", '''\
class Value:
    def __init__(self, value):
        self.value = value

class Base:
    def __init__(self, value):
        self.value = value

class Child(Base):
    def __init__(receiver, value, /, *, flag=True):
        """An initial docstring."""
        super().__init__(value)
        super(Child, receiver).__init__(value)
        Base.__init__(receiver, value)
        receiver.value = Value(value)
        other = int(value)
        text = f"{other!s}"
        packed = [value, (other,)]
        first, *rest = packed
        if isinstance(value, str) and callable(value):
            receiver.value: Value = Value(value)
        elif flag:
            raise ValueError(value)
        else:
            assert hasattr(receiver, "value"), text
        match value:
            case {"key": item} if issubclass(Value, object):
                receiver.value = item
            case _:
                pass
        deferred = lambda item: unknown(item)
        def later():
            return unknown()
        class_like = Value
        receiver.more = class_like

class Outer:
    class Nested:
        def __init__(this):
            this.numbers = {1, 2, 3}
''')
        self.put("tests/test_value.py", 'class Fixture:\n    def __init__(instance):\n        instance.x = 1\n')
        self.put("src/generated/skip.py", 'class Ignored:\n    def __init__(self):\n        while True: pass\n')
        self.put("src/only.pyi", 'class Stub:\n    def __init__(self): ...\n')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["diagnostics"]),
                         ("passed", 0, []))
        self.assertEqual(json.loads((self.run_dir / "constructor-purity-files.json").read_text()),
                         ["src/good.py", "tests/test_value.py"])

    def test_forbidden_statements_targets_and_nested_expression_positions(self):
        self.put("src/bad.py", '''\
class Base: pass
class Outer(Base):
    class Inner:
        def __init__(instance):
            instance = 1
            alias = instance
            alias.x = 1
            other[0] = 2
            instance.x, (local, *rest) = 1, (2, 3)
            local = unknown(bad())
            instance.x = [allowed for allowed in things]
            if unknown():
                return None
            match x:
                case instance:
                    pass
            write()
            Base.__init__(instance)
            for item in things:
                instance.y = item
            with manager():
                pass
            try:
                pass
            except Exception:
                pass
            import os
            del instance.x
            global x
            class Nested: pass
            local = (x := 1)
            local = await task
            local = (yield item)
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        positions = {(d["line"], d["column"]) for d in result["diagnostics"]}
        self.assertEqual(len(result["diagnostics"]), len(positions))
        for line in (5, 7, 8, 10, 11, 12, 13, 15, 17, 18, 19, 21, 23, 27, 28, 29, 30, 31, 32, 33):
            self.assertIn(line, {d["line"] for d in result["diagnostics"]})
        self.assertEqual(sorted(d["column"] for d in result["diagnostics"] if d["line"] == 10),
                         [21, 29])
        self.assertTrue(all(d["symbol"] == "Outer.Inner.__init__" and
                            not d["is_infrastructure_error"] for d in result["diagnostics"]))

    def test_shadowed_builtins_unknown_calls_and_bad_parent_forms(self):
        self.put("src/shadows.py", '''\
class Parent: pass
class Child(Parent):
    def __init__(self, super, int, /):
        self.a = int(3)
        super().__init__()
        Parent.__init__(other)
        self.b = Parent(unknown())
        self.c = my_factory()

def isinstance(*args): pass
class Also:
    def __init__(self):
        assert isinstance(1, int)
class super: pass
class Another:
    def __init__(self):
        super().__init__()
''')
        result, _, _ = self.run_worker()
        self.assertEqual([d["line"] for d in result["diagnostics"]], [4, 5, 5, 6, 7, 8, 13, 17])

    def test_imports_aliases_annotations_and_branch_joins(self):
        self.put("src/demo/model.py", '''\
class Model:
    def __init__(self):
        self.value = 1

class Outer:
    class Nested:
        def __init__(self):
            self.value = 1

def factory():
    return Model()
''')
        self.put("src/demo/public.py", '''\
from .model import Model as Exported
from .model import factory
''')
        self.put("src/demo/usage.py", '''\
import builtins as b
import pathlib as external_module
from external_library import Client as External, service
from .public import Exported, factory
from .model import Outer
from . import model

Alias = Exported
Scalar = b.int
Unknown = model.factory

class Owner:
    def __init__(self, client: External, other):
        self.a = Alias()
        self.nested = Outer.Nested()
        self.b = model.Model()
        self.c = Scalar(1)
        self.d = external_module.Path("x")
        self.e = service("x")
        self.f = client.request(1)
        self.client: External = client
        self.g = self.client.request(2)
        service_alias = service
        self.h = service_alias(3)
        local = Alias
        self.i = local()
        if other:
            choice = Alias
        else:
            choice = service
        self.j = choice()
        if other:
            mixed = Alias
        else:
            mixed = factory
        self.k = mixed()
        if other:
            maybe = External
        self.l = maybe()
        self.m = factory()
        self.n = Unknown()
        self.o = other.request()
        self.p = external_module()
        self.q = b.print("no")
        self.r = missing.request()
''')
        result, _, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual([d["line"] for d in result["diagnostics"]],
                         [36, 39, 40, 41, 42, 43, 44, 45])

    def test_shadowed_aliases_and_external_receiver_proof(self):
        self.put("src/checks.py", '''\
from external_library import Client
from builtins import int as integer, super as parent
class Base:
    def __init__(self):
        self.x = 1
class Child(Base):
    def __init__(self, client: Client, unknown):
        parent().__init__()
        self.ok = integer(1)
        peer: Client = client
        self.ok = peer.run()
        if unknown:
            peer = unknown
        self.no = peer.run()
        integer = unknown
        self.no = integer(2)
        parent = unknown
        parent().__init__()
        self.no = open("x")
''')
        result, _, _ = self.run_worker()
        self.assertEqual([d["line"] for d in result["diagnostics"]],
                         [8, 8, 9, 14, 16, 18, 18, 19])

    def test_external_calls_check_arguments_and_unresolved_imports_stay_unknown(self):
        self.put("src/app/known.py", 'def helper():\n    return 1\n')
        self.put("src/app/public.py", 'from .known import helper as exported\n')
        self.put("src/app/usage.py", '''\
import outside
from outside import operation, Base
from .public import exported
from .unknown import mystery
class Child(Base):
    def __init__(self, dependency: outside.Client, uncertain):
        Base.__init__(self)
        self.valid = dependency.method()
        self.a = operation(exported())
        self.b = operation(uncertain())
        self.c = mystery()
        self.d = uncertain.method()
        self.e = outside.Client().method()
''')
        result, _, _ = self.run_worker()
        # External callable kind is opaque; nested eager expressions are not.
        self.assertEqual([d["line"] for d in result["diagnostics"]],
                         [9, 10, 11, 12, 13])

    def test_rebound_class_name_cannot_prove_explicit_super(self):
        self.put("src/rebound.py", '''\
class Parent:
    def __init__(self):
        self.x = 1
class Child(Parent):
    def __init__(self):
        super(Child, self).__init__()
Child = None
''')
        result, _, _ = self.run_worker()
        self.assertEqual([d["line"] for d in result["diagnostics"]], [6, 6])

    def test_exhaustive_match_joins_all_constructor_targets(self):
        self.put("src/matching.py", '''\
from external_lib import make
class Value:
    def __init__(self):
        self.x = 1
def helper():
    return Value()
class User:
    def __init__(self, option):
        match option:
            case 1:
                target = Value
            case _:
                target = make
        self.good = target()
        match option:
            case 1:
                target = helper
            case _:
                target = Value
        self.bad = target()
''')
        result, _, _ = self.run_worker()
        self.assertEqual([d["line"] for d in result["diagnostics"]], [20])

    def test_nested_class_constructor_is_checked_even_when_declaration_fails(self):
        self.put("src/local.py", '''\
class Outer:
    def __init__(self):
        class Inner:
            def __init__(this):
                return None
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["symbol"], d["line"]) for d in result["diagnostics"]],
                         [("Outer.__init__", 3), ("Outer.Inner.__init__", 5)])

    def test_deferred_bodies_and_proven_decorator_application_stages(self):
        self.put("src/pkg/decorators.py", '''\
def decorate(fn):
    def wrapper(*args, **kwargs):
        return forbidden_deferred(*args, **kwargs)
    marker = int(1)
    return wrapper

def factory(value):
    prepared = str(value)
    def apply(fn):
        def wrapper():
            return forbidden_deferred()
        return wrapper
    return apply

class CallableDecorator:
    def __init__(self):
        self.value = 1
    def __call__(self, fn):
        def wrapper():
            return forbidden_deferred()
        return wrapper

class ClassDecorator:
    def __init__(self, fn):
        self.fn = fn

async def async_decorator(fn):
    forbidden_deferred()
''')
        self.put("src/pkg/public.py", 'from .decorators import decorate, factory, CallableDecorator, '
                 'ClassDecorator, async_decorator\n')
        self.put("src/pkg/model.py", '''\
from __future__ import annotations
from external_lib import external_decorator, external_factory
from .public import decorate as exported, factory, CallableDecorator, ClassDecorator, async_decorator

class Model:
    def __init__(self):
        @external_decorator
        @external_factory(1)
        @exported
        @factory(2)
        @CallableDecorator()
        @ClassDecorator
        @async_decorator
        def later(value: unknown_annotation = int(1)) -> unknown_return:
            forbidden_deferred()
        async def asynchronous():
            await forbidden_deferred()
        self.value = lambda x=int(2): forbidden_deferred(x)
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["diagnostics"]),
                         ("passed", 0, []))

    def test_decorator_eager_failures_and_offending_source_locations(self):
        self.put("src/pkg/decorators.py", '''\
from external_lib import opaque
def bad_factory(value):
    nested = int(forbidden())
    def apply(fn):
        def wrapper():
            return forbidden_deferred()
        eager = opaque(forbidden())
        return wrapper
    return apply

def bad_decorator(fn):
    eager = forbidden()
    return fn

class BadCallable:
    def __init__(self):
        self.value = 1
    def __call__(self, fn):
        eager = forbidden()
        return fn

def no_decorator(value):
    return value

def lambda_factory():
    return lambda fn: forbidden()
''')
        self.put("src/pkg/model.py", '''\
from __future__ import annotations
from .decorators import bad_factory, bad_decorator, BadCallable, no_decorator, lambda_factory
class Model:
    def __init__(self):
        @bad_factory(1)
        @bad_decorator
        @BadCallable()
        @no_decorator(1)
        @unresolved
        @lambda_factory()
        def nested(x: forbidden_annotation = forbidden_default()) -> forbidden_return:
            return forbidden_deferred()
        lam = lambda x=forbidden_lambda(): forbidden_deferred()
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        locations = {(d["path"], d["line"], d["column"]) for d in result["diagnostics"]}
        self.assertEqual(locations, {
            ("src/pkg/decorators.py", 3, 18),
            ("src/pkg/decorators.py", 7, 24),
            ("src/pkg/decorators.py", 12, 13),
            ("src/pkg/decorators.py", 19, 17),
            ("src/pkg/decorators.py", 26, 23),
            ("src/pkg/model.py", 8, 10),
            ("src/pkg/model.py", 9, 10),
            ("src/pkg/model.py", 10, 10),
            ("src/pkg/model.py", 11, 46),
            ("src/pkg/model.py", 13, 24),
        })
        self.assertTrue(all(d["symbol"] == "Model.__init__" for d in result["diagnostics"]))

    def test_postponed_annotations_and_unknown_callable_instance(self):
        self.put("src/annotations.py", '''\
from __future__ import annotations
class Uncallable:
    def __init__(self):
        self.value = 1
class Model:
    def __init__(self):
        @Uncallable()
        def later(x: forbidden_annotation() = forbidden_default()) -> forbidden_return():
            forbidden_deferred()
        async def deferred(x: forbidden_async_annotation()):
            forbidden_deferred()
''')
        result, _, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual([(d["line"], d["column"]) for d in result["diagnostics"]],
                         [(8, 47), (8, 22), (8, 71), (7, 10), (10, 31)])

    def test_mixed_decorator_origins_still_check_proven_project_branch(self):
        self.put("src/choices.py", '''\
def project(fn):
    eager = forbidden()
    return fn
class Model:
    def __init__(self, option, unknown):
        if option:
            choice = project
        else:
            choice = unknown
        @choice
        def later():
            forbidden_deferred()
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["column"]) for d in result["diagnostics"]],
                         [(10, 10), (2, 13)])

    def test_project_decorator_must_return_a_function(self):
        self.put("src/returns.py", '''\
def invalid(fn):
    return 1
class Model:
    def __init__(self):
        @invalid
        def later():
            pass
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["column"]) for d in result["diagnostics"]],
                         [(2, 5), (5, 10)])

    def test_parse_and_report_failures(self):
        self.put("src/good.py", 'class A:\n    def __init__(self):\n        return None\n')
        self.put("src/bad.py", "class Broken(\n")
        result, raw, job = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "CONSTRUCTOR_PURITY_PARSE_ERROR"))
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"CONSTRUCTOR_PURITY", "CONSTRUCTOR_PURITY_PARSE_ERROR"})
        (self.root / "src/good.py").unlink()
        self.assertEqual(execute_job(job, self.root, self.run_dir, 30)["error"],
                         "CONSTRUCTOR_PURITY_PARSE_ERROR")

        def captured(payload, code=0):
            return execute_job(job, self.root, self.run_dir, 30, capture_fn=lambda *args: native_result(
                args[0], stdout=payload, code=code))

        self.assertEqual(captured("not JSON")["error"], "UNREADABLE_TOOL_OUTPUT")
        self.assertEqual(captured(json.dumps({"quard_constructor_purity_adapter": True,
                          "diagnostics": [], "execution_error": None}))["error"],
                         "UNREADABLE_TOOL_OUTPUT")
        self.assertEqual(captured(json.dumps({"quard_constructor_purity_adapter": 1,
                          "diagnostics": [{"rule": "CONSTRUCTOR_PURITY", "message": "bad",
                                           "path": "src/good.py", "symbol": "A.__init__",
                                           "line": True, "column": 1}], "execution_error": None}))
                         ["error"], "UNREADABLE_TOOL_OUTPUT")
        self.assertEqual(captured(json.dumps({"quard_constructor_purity_adapter": 1,
                           "diagnostics": [], "execution_error": None}), 1)["error"],
                         "CONSTRUCTOR_PURITY_EXECUTION_ERROR")

    def test_old_install_requires_migration_and_invalid_opt_in_fails(self):
        with TemporaryDirectory(prefix="quard-constructor-config-", dir=self.root) as directory:
            root = Path(directory)
            create_repo(root)
            setting = root / ".quard/config.toml"
            setting.unlink()
            fake, seen = fake_tools({"pydoclint": {
                "stderr": "src/demo/domain/api.py:1: DOC101: Missing argument\n", "code": 1}})
            report = run_checks(root, capture_fn=fake, versions_fn=lambda _: ({}, []))
            self.assertEqual((report["status"], report["exit_code"]), ("error", 2))
            self.assertIn("CONFIG_MIGRATION_REQUIRED", {d["rule"] for d in report["diagnostics"]})
            self.assertNotIn("constructor-purity", seen)
            self.assertEqual(report["tools"], [])
            self.assertFalse((root / report["report_path"]).parent.joinpath("constructor-purity-files.json").exists())

            original = (QUARD / "defaults/config.toml").read_text(encoding="utf-8")
            for content in ("constructor_purity = 1\n", "constructor_purity = 'true'\n",
                             "constructor_purity = true\nunknown = false\n", "[constructor_purity]\nenabled = true\n",
                             "constructor_purity = [\n"):
                with self.subTest(content=content):
                    setting.write_text(original.replace("constructor_purity = false", content.rstrip()), encoding="utf-8")
                    seen.clear()
                    report = run_checks(root, capture_fn=fake, versions_fn=lambda _: ({}, []))
                    validate(report, json.loads((QUARD / "report.schema.json").read_text(encoding="utf-8")))
                    self.assertEqual((report["status"], report["exit_code"]), ("error", 2))
                    if not content.endswith("[\n") and not content.startswith("[constructor_purity]"):
                        self.assertIn("DOC101", {d["rule"] for d in report["diagnostics"]})
                    self.assertIn("INVALID_CONFIG" if content.endswith("[\n") or
                                  content.startswith("[constructor_purity]") else "INVALID_CHECKS",
                                  {d["rule"] for d in report["diagnostics"]})
                    self.assertTrue(any(d["is_infrastructure_error"] for d in report["diagnostics"]))
                    if not content.endswith("[\n") and not content.startswith("[constructor_purity]"):
                        tool = next(t for t in report["tools"] if t["name"] == "constructor-purity")
                        self.assertEqual((tool["status"], tool["exit_code"]), ("error", None))
                    self.assertNotIn("constructor-purity", seen)
                    self.assertFalse((root / report["report_path"]).parent.joinpath("constructor-purity-files.json").exists())

    def test_project_report_keeps_full_scope_and_native_diagnostics(self):
        with TemporaryDirectory(prefix="quard-constructor-runner-", dir=self.root) as directory:
            root = Path(directory)
            create_repo(root)
            setting = root / ".quard/config.toml"
            setting.write_text(setting.read_text().replace("constructor_purity = false", "constructor_purity = true"), encoding="utf-8")
            (root / "src/demo/domain/api.py").write_text(
                'from .decorators import project_decorator\n'
                'class A:\n    def __init__(self):\n        return None\n'
                '        @project_decorator\n'
                '        def later():\n            pass\n', encoding="utf-8")
            (root / "src/demo/domain/decorators.py").write_text(
                'def project_decorator(fn):\n    result = forbidden()\n    return fn\n',
                encoding="utf-8")
            (root / "tests/test_value.py").write_text(
                'class B:\n    def __init__(self):\n        unknown()\n', encoding="utf-8")
            fake, _ = fake_tools()

            def mixed(argv, cwd, timeout):
                if str(QUARD / "_constructor_purity_worker.py") in argv:
                    return capture(argv, cwd, timeout)
                if "pyright" in argv:
                    return native_result(argv, code=1, stdout=json.dumps({"generalDiagnostics": [{
                        "file": "src/demo/domain/api.py", "message": "Native issue",
                        "rule": "reportArgumentType", "severity": "error",
                        "range": {"start": {"line": 0, "character": 0}}}]}))
                return fake(argv, cwd, timeout)

            for selected in (None, "", "src/demo/domain/api.py"):
                report = run_checks(root, lcom_files=selected, capture_fn=mixed,
                                    versions_fn=lambda _: ({}, []))
                validate(report, json.loads((QUARD / "report.schema.json").read_text(encoding="utf-8")))
                self.assertEqual((report["status"], report["exit_code"]), ("violations", 1))
                self.assertEqual({(d["path"], d["rule"]) for d in report["diagnostics"]}, {
                    ("src/demo/domain/api.py", "CONSTRUCTOR_PURITY"),
                    ("src/demo/domain/decorators.py", "CONSTRUCTOR_PURITY"),
                    ("tests/test_value.py", "CONSTRUCTOR_PURITY"),
                    ("src/demo/domain/api.py", "reportArgumentType")})
                self.assertIn(("src/demo/domain/decorators.py", 2, "A.__init__"),
                              {(d["path"], d["line"], d["symbol"])
                               for d in report["diagnostics"] if d["rule"] == "CONSTRUCTOR_PURITY"})
                tool = next(item for item in report["tools"] if item["name"] == "constructor-purity")
                self.assertEqual((tool["status"], tool["exit_code"]), ("violations", 1))
                self.assertTrue((root / tool["raw_output"]["stdout"]).is_file())

            def malformed(argv, cwd, timeout):
                if str(QUARD / "_constructor_purity_worker.py") in argv:
                    return native_result(argv, stdout="not JSON", code=0)
                return mixed(argv, cwd, timeout)

            report = run_checks(root, lcom_files="", capture_fn=malformed,
                                versions_fn=lambda _: ({}, []))
            self.assertEqual((report["status"], report["exit_code"]), ("error", 2))
            self.assertEqual({d["rule"] for d in report["diagnostics"]},
                             {"UNREADABLE_TOOL_OUTPUT", "reportArgumentType"})
            self.assertEqual(report["summary"]["infrastructure_errors"], 1)


if __name__ == "__main__":
    unittest.main()
