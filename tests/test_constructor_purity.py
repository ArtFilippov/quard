"""Real worker and report regressions for the constructor syntax boundary."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from jsonschema import validate

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX / "src"))

from eryx._common import capture
from eryx._policy import discover_files, read_scope
from eryx.check import execute_job, make_jobs, run_checks
from test_infrastructure import RunnerTests, create_repo, native_result


class ConstructorPurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="eryx-constructor-")
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

    def put(self, path, source):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    def run_worker(self):
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [])
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
        self.assertEqual(captured(json.dumps({"eryx_constructor_purity_adapter": True,
                          "diagnostics": [], "execution_error": None}))["error"],
                         "UNREADABLE_TOOL_OUTPUT")
        self.assertEqual(captured(json.dumps({"eryx_constructor_purity_adapter": 1,
                          "diagnostics": [{"rule": "CONSTRUCTOR_PURITY", "message": "bad",
                                           "path": "src/good.py", "symbol": "A.__init__",
                                           "line": True, "column": 1}], "execution_error": None}))
                         ["error"], "UNREADABLE_TOOL_OUTPUT")
        self.assertEqual(captured(json.dumps({"eryx_constructor_purity_adapter": 1,
                          "diagnostics": [], "execution_error": None}), 1)["error"],
                         "CONSTRUCTOR_PURITY_EXECUTION_ERROR")

    def test_project_report_keeps_full_scope_and_native_diagnostics(self):
        with TemporaryDirectory(prefix="eryx-constructor-runner-") as directory:
            root = Path(directory)
            create_repo(root)
            (root / "src/demo/domain/api.py").write_text(
                'class A:\n    def __init__(self):\n        return None\n', encoding="utf-8")
            (root / "tests/test_value.py").write_text(
                'class B:\n    def __init__(self):\n        unknown()\n', encoding="utf-8")
            fake, _ = RunnerTests().fake_tools()

            def mixed(argv, cwd, timeout):
                if str(ERYX / "_constructor_purity_worker.py") in argv:
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
                validate(report, json.loads((ERYX / "report.schema.json").read_text(encoding="utf-8")))
                self.assertEqual((report["status"], report["exit_code"]), ("violations", 1))
                self.assertEqual({(d["path"], d["rule"]) for d in report["diagnostics"]}, {
                    ("src/demo/domain/api.py", "CONSTRUCTOR_PURITY"),
                    ("tests/test_value.py", "CONSTRUCTOR_PURITY"),
                    ("src/demo/domain/api.py", "reportArgumentType")})
                tool = next(item for item in report["tools"] if item["name"] == "constructor-purity")
                self.assertEqual((tool["status"], tool["exit_code"]), ("violations", 1))
                self.assertTrue((root / tool["raw_output"]["stdout"]).is_file())

            def malformed(argv, cwd, timeout):
                if str(ERYX / "_constructor_purity_worker.py") in argv:
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
