"""Standalone LCOM4 fixtures and real worker/report integration."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX))

import _policy
from check import execute_job, make_jobs


class Lcom4Test(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="eryx-lcom4-")
        self.root = Path(self.temp.name)
        (self.root / "src" / "demo" / "tests").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / ".eryx").mkdir()
        (self.root / ".eryx" / "scope.toml").write_text(
            'exclude_directory_names = ["generated"]\n'
            'test_directory_names = ["tests"]\n', encoding="utf-8")
        self.run_dir = self.root / ".eryx" / "reports" / "run"
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, path, code):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(code, encoding="utf-8")

    def run_worker(self):
        files = _policy.discover_files(self.root, _policy.read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False)
                   if job["name"] == "lcom4")
        result = execute_job(job, self.root, self.run_dir, timeout=30)
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        return result, raw, files

    def test_scope_and_connected_components(self):
        self.put("src/demo/core.py", '''
class Connected:
    def first(self):
        self.second()
    def second(self):
        return 1

class Shared:
    def first(self):
        return self.value
    def second(self):
        self.value = 1

class Split:
    def first(self):
        return self.a
    async def second(self):
        return self.b
    def third(self):
        return self.b

class Empty:
    value = 1

class Nested:
    def one(self):
        def deferred():
            self.two()
        return deferred
    def two(self):
        pass
''')
        self.put("src/demo/tests/fixture.py", "class TestFixture:\n    pass\n")
        self.put("tests/test_example.py", "class TestExample:\n    def a(self): pass\n    def b(self): pass\n")
        self.put("src/demo/generated/bad.py", "class Excluded:\n    pass\n")
        self.put("src/demo/stub.pyi", "class Stub: ...\n")
        result, raw, files = self.run_worker()
        self.assertEqual(result["status"], "violations")
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("src/demo/tests/fixture.py", files["tests"])
        self.assertIn("src/demo/generated/", files["excluded_directories"])
        scores = {d["symbol"]: d["score"] for d in result["diagnostics"]}
        self.assertEqual(scores, {"Split": 2, "Empty": 0, "Nested": 2,
                                  "TestFixture": 0, "TestExample": 2})
        split = next(d for d in result["diagnostics"] if d["symbol"] == "Split")
        self.assertEqual(split["groups"], [["first"], ["second", "third"]])
        self.assertEqual((split["path"], split["line"], split["column"]), ("src/demo/core.py", 14, 1))
        self.assertEqual(len(raw["diagnostics"]), 5)
        self.assertIsNone(raw["execution_error"])

    def test_accessors_are_distinct_vertices_without_implicit_property_edges(self):
        self.put("src/demo/accessors.py", '''
class Accessors:
    @property
    def value(self):
        return 1
    @value.setter
    def value(self, new):
        pass
    @value.deleter
    def value(self):
        pass
    def read(self):
        return self.value

class SharedBackingField:
    @property
    def value(self):
        return self._value
    @value.setter
    def value(self, new):
        self._value = new
    @value.deleter
    def value(self):
        del self._value
''')
        result, _, _ = self.run_worker()
        self.assertEqual(result["status"], "violations")
        self.assertEqual({d["symbol"]: d["groups"] for d in result["diagnostics"]},
                         {"Accessors": [["value"], ["value"], ["value"], ["read"]]})

    def test_receiver_calls_field_kinds_scopes_and_inheritance(self):
        self.put("src/demo/relationships.py", '''
class Renamed:
    def first(this):
        this.second()
    def second(this):
        this.field = 1
    async def third(other):
        return other.field

class ClassAndStatic:
    shared = 1
    @classmethod
    def first(klass):
        return klass.shared
    @staticmethod
    def second(arg):
        return ClassAndStatic.shared

class ClassCalls:
    @classmethod
    def first(klass):
        klass.second()
    @staticmethod
    def second():
        pass

class NamedClassCalls:
    @staticmethod
    def first(arg):
        NamedClassCalls.second()
    @staticmethod
    def second():
        pass

class DistinctFields:
    shared = 1
    def first(self):
        return self.shared
    @classmethod
    def second(cls):
        return cls.shared

class StaticArgument:
    @staticmethod
    def first(self):
        return self.shared
    def second(instance):
        return instance.shared

class StaticCallArgument:
    @staticmethod
    def first(cls):
        cls.second()
    @staticmethod
    def second():
        pass

class Parent:
    def inherited(self):
        pass

class Child(Parent):
    def first(self):
        alias = self
        alias.second()
        self.inherited()
        getattr(self, "second")()
    def second(self):
        self.inherited()

class UnknownCall:
    def first(self):
        self.external()
    def second(self):
        self.external()

class Scopes:
    def first(receiver):
        def deferred():
            receiver.third()
        class Nested:
            def a(self): pass
            def b(self): pass
        return [receiver.field for _ in range(1)]
    def second(receiver):
        return receiver.field
    def third(receiver):
        pass
''')
        result, _, _ = self.run_worker()
        self.assertEqual({d["symbol"]: d["groups"] for d in result["diagnostics"]},
                         {"DistinctFields": [["first"], ["second"]],
                          "StaticArgument": [["first"], ["second"]],
                          "StaticCallArgument": [["first"], ["second"]],
                          "Child": [["first"], ["second"]],
                          "UnknownCall": [["first"], ["second"]],
                          "Scopes": [["first", "second"], ["third"]],
                          "Scopes.Nested": [["a"], ["b"]]})

    def test_syntax_failure_is_infrastructure_error_and_preserves_other_results(self):
        self.put("src/demo/bad.py", "class Broken(\n")
        self.put("tests/test_fixture.py", "class StillChecked: pass\n")
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
        self.assertEqual(raw["execution_error"], "LCOM4_PARSE_ERROR")
        self.assertEqual({d["rule"] for d in result["diagnostics"]}, {"LCOM4_PARSE_ERROR", "LCOM4"})
        self.assertTrue(next(d for d in result["diagnostics"] if d["rule"] == "LCOM4_PARSE_ERROR")["is_infrastructure_error"])

    def test_missing_manifest_and_malformed_output_fail_closed(self):
        self.put("src/demo/core.py", "class Valid: pass\n")
        files = _policy.discover_files(self.root, _policy.read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False)
                   if job["name"] == "lcom4")
        Path(job["argv"][-1]).unlink()
        result = execute_job(job, self.root, self.run_dir, timeout=30)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
        self.assertEqual(result["error"], "LCOM4_EXECUTION_ERROR")

        def bad_capture(argv, root, timeout):
            return {"argv": argv, "stdout": "not JSON", "stderr": "", "exit_code": 0,
                    "duration_seconds": 0, "error": None}

        malformed = execute_job(job, self.root, self.run_dir, 30, capture_fn=bad_capture)
        self.assertEqual(malformed["status"], "error")
        self.assertEqual(malformed["error"], "UNREADABLE_TOOL_OUTPUT")


if __name__ == "__main__":
    unittest.main()
