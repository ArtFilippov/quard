"""End-to-end fixtures for the non-instance-method worker and its report adapter."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX))

from _policy import discover_files, read_scope
from check import execute_job, make_jobs


class MethodDecoratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="eryx-methods-")
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / ".eryx").mkdir()
        (self.root / ".eryx/scope.toml").write_text(
            'exclude_directory_names = ["generated"]\n'
            'test_directory_names = ["tests"]\n', encoding="utf-8")
        self.run_dir = self.root / ".eryx/reports/run"
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

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

        for text in ('not JSON', '{"eryx_method_decorators_adapter": 1, "diagnostics": [{"rule": "NON_INSTANCE_METHOD", "message": "bad"}], "execution_error": null}'):
            with self.subTest(text=text):
                self.assertEqual(capture(text, 0)["error"], "UNREADABLE_TOOL_OUTPUT")
        self.assertEqual(capture(json.dumps({"eryx_method_decorators_adapter": 1,
                         "diagnostics": [], "execution_error": None}), 1)["status"], "error")


if __name__ == "__main__":
    unittest.main()
