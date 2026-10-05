"""End-to-end fixtures for the non-instance-method worker and its report adapter."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import override
import unittest

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))

from quard._policy import discover_files, read_scope
from quard.check import execute_job, make_jobs


class TestDeclarations(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.temp: TemporaryDirectory[str] = TemporaryDirectory(prefix="quard-methods-")
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

    def put(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def run_worker(self):
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [])
                   if job["name"] == "method-decorators")
        result = execute_job(job, self.root, self.run_dir, 30)
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        return result, raw, job

    def test_scope_aliases_shadowing_and_nested_declarations(self):
        self.put("src/pkg.py", '''\
from builtins import staticmethod as sm, classmethod as cm
import builtins as bi
from typing import Protocol
from abc import ABC, abstractmethod

class Outer:
    @staticmethod
    def direct(): pass
    @cm
    async def alias(cls): pass
    class Nested(Protocol):
        @bi.staticmethod
        def stub() -> None: ...
    @property
    def value(self): return 1
    @abstractmethod
    def pending(self): ...
    def __new__(cls): return super().__new__(cls)

class Abstract(ABC):
    @sm
    async def factory(): pass
alias = bi.classmethod
class AssignedAlias:
    @alias
    def create(cls): pass
''')
        self.put("tests/test_methods.py", '''\
import builtins
class Case:
    @builtins.classmethod
    def from_test(cls): pass
''')
        self.put("src/defs.pyi", '''\
from builtins import classmethod as cm
class Stub:
    @cm
    def build(cls) -> Stub: ...
''')
        self.put("src/generated/ignored.py", "class Ignored:\n    @staticmethod\n    def f(): pass\n")
        self.put("src/shadow.py", '''\
staticmethod = lambda fn: fn
class Other:
    @staticmethod
    def allowed(self): pass
class Pretend:
    @other.staticmethod
    def allowed(self): pass
def enclosing(classmethod):
    class Nested:
        @classmethod
        def allowed(cls): pass
import builtins as b
b = object()
class AlsoOther:
    @b.classmethod
    def allowed(cls): pass
class HasNestedFunction:
    def method(self):
        @staticmethod
        def local(): pass
        return local()
if False:
    from builtins import staticmethod as maybe
class ConditionalImport:
    @maybe
    def allowed(self): pass
def forward_shadow():
    class Nested:
        @staticmethod
        def allowed(): pass
    staticmethod = lambda fn: fn
''')
        result, raw, job = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertIsNone(raw["execution_error"])
        self.assertEqual([(d["path"], d["symbol"], d["rule"]) for d in result["diagnostics"]], [
            ("src/defs.pyi", "Stub.build", "NON_INSTANCE_METHOD"),
            ("src/pkg.py", "Outer.direct", "NON_INSTANCE_METHOD"),
            ("src/pkg.py", "Outer.alias", "NON_INSTANCE_METHOD"),
            ("src/pkg.py", "Outer.Nested.stub", "NON_INSTANCE_METHOD"),
            ("src/pkg.py", "Abstract.factory", "NON_INSTANCE_METHOD"),
            ("src/pkg.py", "AssignedAlias.create", "NON_INSTANCE_METHOD"),
            ("tests/test_methods.py", "Case.from_test", "NON_INSTANCE_METHOD"),
        ])
        self.assertEqual((result["diagnostics"][0]["line"], result["diagnostics"][0]["column"]), (3, 5))
        self.assertEqual(set(json.loads((self.run_dir / "method-decorators-files.json").read_text())),
                         {"src/defs.pyi", "src/pkg.py", "src/shadow.py", "tests/test_methods.py"})
        self.assertEqual(job["tool"], "method-decorators")

    def test_function_conversions_in_classes_and_after_creation(self):
        self.maxDiff = None
        self.put("src/conversions.py", '''\
from builtins import staticmethod as sm, classmethod as cm, setattr as set_attr
import builtins as bi

def helper(self): pass
class Outer:
    def direct(self): pass
    direct = staticmethod(direct)
    def alias(cls): pass
    alias = cm(alias)
    class Inner:
        def nested(self): pass
        nested = bi.staticmethod(nested)
    from builtins import staticmethod as local_sm
    def local(self): pass
    local = local_sm(local)

class Later:
    def method(self): pass
Later.method = sm(Later.method)
Later.other = bi.classmethod(helper)
Alias = Later
set_attr(Alias, "factory", cm(helper))
bi.setattr(Outer.Inner, "new", sm(helper))
static_alias = bi.staticmethod
Later.lambda_method = static_alias(lambda: None)
saved = bi.setattr(Later, "saved", cm(helper))
''')
        self.put("tests/test_convert.py", '''\
class Case:
    def test(self): pass
Case.test = staticmethod(Case.test)
''')
        self.put("src/defs.pyi", '''\
from builtins import classmethod as cm
class Stub:
    def build(cls) -> Stub: ...
    build = cm(build)
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["path"], d["symbol"], d["line"], d["column"])
                          for d in result["diagnostics"]], [
            ("src/conversions.py", "Outer.direct", 7, 13),
            ("src/conversions.py", "Outer.alias", 9, 12),
            ("src/conversions.py", "Outer.Inner.nested", 12, 17),
            ("src/conversions.py", "Outer.local", 15, 12),
            ("src/conversions.py", "Later.method", 19, 15),
            ("src/conversions.py", "Later.other", 20, 14),
            ("src/conversions.py", "Later.factory", 22, 27),
            ("src/conversions.py", "Outer.Inner.new", 23, 31),
            ("src/conversions.py", "Later.lambda_method", 25, 22),
            ("src/conversions.py", "Later.saved", 26, 35),
            ("src/defs.pyi", "Stub.build", 4, 12),
            ("tests/test_convert.py", "Case.test", 3, 12),
        ])
        self.assertTrue(all(d["rule"] == "NON_INSTANCE_METHOD" and
                            not d["is_infrastructure_error"] for d in result["diagnostics"]))

    def test_class_body_methods_require_mandatory_positional_receiver(self):
        self.put("src/receivers.py", '''\
from abc import ABC, abstractmethod
from typing import Protocol

def free_function(): pass

class Outer:
    def missing(): pass
    async def asynchronous(*args): pass
    def defaulted(receiver=None): pass
    def keyword_only(*, receiver): pass
    def kwargs_only(**kwargs): pass
    def positional_only(receiver=None, /): pass
    def valid(receiver): return receiver
    def unused(instance): pass
    def positional_only_valid(instance, /): pass
    def optional_second(instance, other=None): pass
    def __new__(cls): return super().__new__(cls)
    def __init_subclass__(cls): pass
    def __class_getitem__(cls, item): return item
    @property
    def value(self): return 1
    @value.setter
    def value(self, new_value): pass
    class Nested:
        def missing(): pass

class Contract(Protocol):
    def missing() -> None: ...
    async def valid(self) -> None: ...

class Abstract(ABC):
    @abstractmethod
    def missing() -> None: ...
    @abstractmethod
    def valid(self) -> None: ...

def factory():
    class Local:
        def missing(): pass
    return Local

class Inherited(list):
    def own(self): pass
''')
        self.put("src/stubs.pyi", '''\
from typing import Protocol
class Stub:
    def missing() -> None: ...
    async def defaulted(self = ...) -> None: ...
    def valid(this) -> None: ...
class Contract(Protocol):
    def missing(*args) -> None: ...
''')
        self.put("tests/test_receivers.py", "class TestCase:\n    def missing(): pass\n")
        self.put("src/generated/ignored.py", "class Ignored:\n    def missing(): pass\n")
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["path"], d["symbol"]) for d in result["diagnostics"]], [
            ("src/receivers.py", f"Outer.{name}") for name in (
                "missing", "asynchronous", "defaulted", "keyword_only", "kwargs_only", "positional_only")
        ] + [
            ("src/receivers.py", "Outer.Nested.missing"),
            ("src/receivers.py", "Contract.missing"),
            ("src/receivers.py", "Abstract.missing"),
            ("src/receivers.py", "Local.missing"),
            ("src/stubs.pyi", "Stub.missing"),
            ("src/stubs.pyi", "Stub.defaulted"),
            ("src/stubs.pyi", "Contract.missing"),
            ("tests/test_receivers.py", "TestCase.missing"),
        ])
        self.assertTrue(all(d["rule"] == "MISSING_INSTANCE_RECEIVER" and
                            d["line"] > 0 and d["column"] > 0 and
                            not d["is_infrastructure_error"] for d in result["diagnostics"]))

    def test_unrelated_conversions_and_shadowed_builtins_are_ignored(self):
        self.put("src/negative.py", '''\
import builtins as bi
def helper(self): pass
def staticmethod(fn): return fn
def setattr(obj, name, value): pass
class Shadow:
    def method(self): pass
    method = staticmethod(method)
class Unrelated:
    def method(self): pass
    method = other.classmethod(method)
class Real:
    def method(self): pass
    method = bi.classmethod(method)
    method = bi.staticmethod(method)
bi.setattr(Real, "method", bi.staticmethod(Real.method))
setattr(Real, "other", bi.staticmethod(helper))
bi.setattr(Real, dynamic_name, bi.staticmethod(helper))
bi.setattr(library_class, "method", bi.staticmethod(helper))
library_class.method = bi.staticmethod(helper)
Real.unknown = bi.staticmethod(unknown_function)
bi = object()
Real.fake = bi.classmethod(helper)
def replace(cls): return library_class
from builtins import staticmethod as real_sm
@replace
class Decorated:
    def method(self): pass
Decorated.method = real_sm(Decorated.method)
''')
        self.put("src/conditional.py", '''\
if flag:
    from builtins import staticmethod as maybe
class Candidate:
    def method(self): pass
    method = maybe(method)
def enclosing(staticmethod):
    class Local:
        def method(self): pass
        method = staticmethod(method)
def forward():
    class Inner:
        def method(self): pass
        method = staticmethod(method)
    staticmethod = lambda fn: fn
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual([(d["symbol"], d["line"]) for d in result["diagnostics"]],
                         [("Real.method", 13)])

    def test_parse_and_read_failures_do_not_hide_other_violations(self):
        self.put("src/good.py", "class Good:\n    @staticmethod\n    def bad(): pass\n")
        self.put("src/bad.pyi", "class Broken(\n")
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
        self.assertEqual(raw["execution_error"], "METHOD_DECORATORS_PARSE_ERROR")
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"NON_INSTANCE_METHOD", "METHOD_DECORATORS_PARSE_ERROR"})
        self.assertTrue(next(d for d in result["diagnostics"] if d["rule"] != "NON_INSTANCE_METHOD")
                        ["is_infrastructure_error"])

    def test_missing_source_after_discovery_is_infrastructure_error(self):
        self.put("src/missing.py", "class Missing: pass\n")
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [])
                   if job["name"] == "method-decorators")
        (self.root / "src/missing.py").unlink()
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"], result["error"]),
                         ("error", 2, "METHOD_DECORATORS_PARSE_ERROR"))
        self.assertTrue(result["diagnostics"][0]["is_infrastructure_error"])

    def test_malformed_output_and_exit_mismatch_fail_closed(self):
        self.put("src/one.py", "x = 1\n")
        _, _, job = self.run_worker()

        def capture(text, code):
            return execute_job(job, self.root, self.run_dir, 30, capture_fn=lambda *args: {
                "stdout": text, "stderr": "", "exit_code": code, "duration_seconds": 0, "error": None})

        for text in ('not JSON', '{"quard_method_decorators_adapter": 1, "diagnostics": [{"rule": "NON_INSTANCE_METHOD", "message": "bad"}], "execution_error": null}'):
            with self.subTest(text=text):
                self.assertEqual(capture(text, 0)["error"], "UNREADABLE_TOOL_OUTPUT")
        self.assertEqual(capture(json.dumps({"quard_method_decorators_adapter": 1,
                         "diagnostics": [], "execution_error": None}), 1)["status"], "error")


if __name__ == "__main__":
    unittest.main()
