"""Autonomous worker and report regressions for assembly-site lambda injection."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from quard._common import capture, write_json
from quard._policy import discover_files, read_scope
from quard.check import execute_job, make_jobs, run_checks
from quard.check_selection_utils import validate_checks
from offline_fixtures import create_repo, fake_tools, native_result
from legacy_clone_fixture_utils import historical_cli


class TestLambdaInjection(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="quard-lambda-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        historical_cli(self)
        create_repo(self.root)
        self.run_dir = self.root / ".quard/reports/work"
        self.run_dir.mkdir(parents=True)

    def put(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def job(self):
        files = discover_files(self.root, read_scope(self.root))
        return next(job for job in make_jobs(self.root, self.run_dir, files, True, [], False)
                    if job["name"] == "lambda-injection")

    def test_declarative_bodies_and_external_provenance(self):
        self.put("src/demo/domain/api.py", "class Builder: pass\n")
        self.put("src/demo/main/wire.py", '''\
from demo.domain.api import Builder as Build
from external_library import provide as outside
from external_library import module as remote

def assembly(port):
    alias = Build
    alias(lambda arg: arg)
    Build(lambda arg: 42)
    Build(factory=lambda arg: arg.value)
    outside(lambda arg: {'one': remote.make(arg), 'two': (Build(arg), [arg])})
    remote.provide(lambda arg: outside(remote.make(arg)))
    unknown(lambda arg: arg if arg else None)
    Build(lambda arg: outside(arg if arg else None))
    Build(lambda arg: outside([arg + 1]))
''')
        self.put("src/demo/domain/ordinary.py", "from demo.domain.api import Builder\n"
                 "Builder(lambda x: x if x else None)\n")
        self.put("src/demo/domain/envelopes/wire.py", "from demo.domain.api import Builder\n"
                 "Builder(lambda x: x[0])\n")
        self.put("src/demo/domain/envelopes/nested/wire.py", "from demo.domain.api import Builder\n"
                 "Builder(lambda x: x if x else None)\n")
        self.put("tests/test_lambda.py", "from demo.domain.api import Builder\n"
                 "Builder(lambda x: x if x else None)\n")
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual({(d["path"], d["line"], d["column"]) for d in result["diagnostics"]},
                         {("src/demo/main/wire.py", 13, 31),
                          ("src/demo/main/wire.py", 14, 32),
                          ("src/demo/domain/envelopes/wire.py", 2, 19)})
        self.assertTrue(all("wrapper class" in d["message"] for d in result["diagnostics"]))
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text())
        self.assertEqual(raw["execution_error"], None)
        self.assertEqual(len(raw["diagnostics"]), 3)

    def test_forbidden_forms_and_lexical_shadowing(self):
        self.put("src/demo/main/wire.py", '''\
from third_party import create
from demo.domain.api import Builder
def build(value):
    Builder(lambda x: x if x else value)
    create(lambda x: x[0])
    Builder(lambda x: x + 1)
    Builder(lambda x: (y := x))
    Builder(lambda x: [x for _ in x])
    Builder(lambda x: {**x})
    Builder(lambda x: (lambda y: y))
    create = value
    create(lambda x: x[0])
    unresolved(lambda x: x[0])
''')
        self.put("src/demo/domain/api.py", "class Builder: pass\n")
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual([d["line"] for d in result["diagnostics"]], [4, 5, 6, 7, 8, 9, 10])
        self.assertTrue(all(d["rule"] == "LAMBDA_INJECTION" for d in result["diagnostics"]))

    def test_root_main_and_conditional_origins(self):
        self.put("src/main/wire.py", '''\
from demo.domain.api import Builder
from outside_package import provide
def build(value, condition):
    if condition:
        provider = Builder
    else:
        provider = provide
    provider(lambda x: x[0])
    if condition:
        provider = value
    provider(lambda x: x[0])
    match value:
        case {'provider': provider}:
            provider(lambda x: x[0])
''')
        self.put("src/demo/domain/api.py", "class Builder: pass\n")
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual([(d["path"], d["line"]) for d in result["diagnostics"]],
                         [("src/main/wire.py", 8)])

    def test_argument_origins_and_external_fields(self):
        self.put("src/demo/domain/api.py", "class Builder: pass\n"
                 "def provide(): return Builder()\n"
                 "ready = Builder()\n")
        self.put("src/demo/main/wire.py", '''\
from demo.domain.api import Builder, provide, ready
from outside_package import settings as external_settings
import outside_package as outside

def wire(port, selected):
    created = provide()
    alias = created
    Builder(port, value=[alias, {'field': external_settings.field}], other=42)
    outside.connect(alias, config=external_settings.field)
    Builder(provide(), external_settings.field, outside.make(port))
    Builder(ready)
    Builder(missing)
    Builder(lambda arg: (arg, alias, external_settings.field))
    Builder(lambda arg: missing)
    outside.connect(lambda arg: ready)
    unknown(ready)

assembled = Builder()
Builder(assembled)
Builder(ready)
''')
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(11, "argument"), (12, "argument"), (14, "argument"),
                          (15, "argument"), (20, "argument")])
        self.assertTrue(all(d["column"] > 0 for d in result["diagnostics"]))

    def test_branch_captures_and_scope_boundaries(self):
        self.put("src/demo/domain/api.py", "class Builder: pass\n"
                 "ready = Builder()\n")
        self.put("src/demo/main/wire.py", '''\
from demo.domain.api import Builder, ready
from outside_package import value as external_value

module_value = Builder()
Builder(module_value)

def outer(input_value, choose):
    good = Builder()
    if choose:
        chosen = good
    else:
        chosen = ready
    Builder(chosen)
    if choose:
        safe = input_value
    else:
        safe = external_value.field
    Builder(safe)
    def inner():
        Builder(good)
        Builder(chosen)
        Builder(module_value)
        Builder(lambda arg: (good, chosen, module_value))
    return inner

def reads_global():
    Builder(module_value)
    Builder(external_value.field)
''')
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(13, "argument"), (21, "argument"), (22, "argument"),
                          (23, "argument"), (23, "argument"), (27, "argument")])

    def test_nested_calls_and_unproved_container_elements(self):
        self.put("src/demo/domain/api.py", "class Builder: pass\n"
                 "def provide(value): return value\n")
        self.put("src/demo/main/wire.py", '''\
from demo.domain.api import Builder, provide
from outside_package import connect
def wire(value):
    def local_factory():
        return value
    Builder(local_factory())
    Builder(connect(provide(value)))
    Builder(connect(missing))
    Builder(items={'bad': missing})
    Builder(items=[value if value else 0])
    Builder(lambda arg: connect(missing))
    connect(value, key=missing)
    unknown(missing)
''')
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(8, "argument"), (9, "argument"), (10, "argument"),
                          (11, "argument"), (12, "argument")])

    def test_later_rebinding_invalidates_a_nested_capture(self):
        self.put("src/demo/domain/api.py", "class Builder: pass\nready = Builder()\n")
        self.put("src/demo/main/wire.py", '''\
from demo.domain.api import Builder, ready
def outer():
    captured = Builder()
    def inner():
        Builder(captured)
    captured = ready
    return inner
''')
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(5, "argument")])

    def test_later_rebinding_invalidates_a_direct_lambda_capture(self):
        self.put("src/demo/domain/api.py", "class Builder: pass\nready = Builder()\n")
        self.put("src/demo/main/wire.py", '''\
from demo.domain.api import Builder, ready
def outer():
    captured = Builder()
    Builder(lambda arg: captured)
    captured = ready
''')
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(4, "argument")])

    def test_unsupported_lambda_still_checks_referenced_values(self):
        self.put("src/demo/domain/api.py", "class Builder: pass\nready = Builder()\n")
        self.put("src/demo/main/wire.py", '''\
from demo.domain.api import Builder, ready
Builder(lambda arg: ready if arg else arg)
''')
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(2, "lambda"), (2, "argument")])

    def test_generated_and_reviewed_reporting_independent_of_lcom(self):
        self.put("src/demo/main/wire.py", "from demo.domain.api import Builder\n"
                 "Builder(lambda x: x[0])\n")
        self.put("src/demo/domain/api.py", "class Builder: pass\n")
        fake, seen = fake_tools()

        def mixed(argv, cwd, timeout):
            if str(QUARD / "_lambda_injection_worker.py") in argv:
                seen.append("lambda-injection")
                return capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        for generated in (False, True):
            with self.subTest(generated=generated):
                if generated:
                    write_json(self.root / ".quard/architecture.generated.json",
                               {"policy": "contract-only", "version": 1})
                report = run_checks(self.root, lcom_files="", capture_fn=mixed,
                                    versions_fn=lambda _: ({}, []))
                tool = next(t for t in report["tools"] if t["name"] == "lambda-injection")
                self.assertEqual((tool["status"], tool["exit_code"]), ("violations", 1))
                self.assertIn("lambda-injection", seen)
                self.assertTrue((self.root / tool["raw_output"]["stdout"]).is_file())
                self.assertEqual({d["rule"] for d in report["diagnostics"]}, {"LAMBDA_INJECTION"})

    def test_selection_and_infrastructure_errors(self):
        self.put("src/demo/main/wire.py", "from demo.domain.api import Builder\n"
                 "Builder(lambda x: x[0])\n")
        self.put("src/demo/domain/api.py", "class Builder: pass\n")
        job = self.job()
        self.put("src/demo/main/broken.py", "x = (\n")
        result = execute_job(self.job(), self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"LAMBDA_INJECTION", "LAMBDA_INJECTION_PARSE_ERROR"})
        for payload, code in (("not JSON", 1),
                              (json.dumps({"quard_lambda_injection_adapter": 1,
                                           "diagnostics": [], "execution_error": None}), 1),
                              (json.dumps({"quard_lambda_injection_adapter": 1,
                                           "diagnostics": [{"rule": "LAMBDA_INJECTION",
                                                            "path": "src/a.py", "line": True,
                                                            "column": 1, "symbol": "lambda",
                                                            "message": "bad"}],
                                           "execution_error": None}), 0)):
            with self.subTest(payload=payload):
                result = execute_job(job, self.root, self.run_dir, 30,
                                     capture_fn=lambda argv, cwd, timeout: native_result(
                                         argv, stdout=payload, code=code))
                self.assertEqual(result["status"], "error")
                self.assertTrue(any(d["is_infrastructure_error"] for d in result["diagnostics"]))
        setting = self.root / ".quard/config.toml"
        setting.write_text(setting.read_text().replace("lambda_injection = true",
                                                      "lambda_injection = false"))
        fake, seen = fake_tools()
        report = run_checks(self.root, capture_fn=fake, versions_fn=lambda _: ({}, []))
        tool = next(t for t in report["tools"] if t["name"] == "lambda-injection")
        self.assertEqual((tool["status"], tool["exit_code"], tool["raw_output"]),
                         ("skipped", None, {}))
        self.assertNotIn("lambda-injection", seen)
        self.assertFalse((self.root / report["report_path"]).parent.joinpath(
            "lambda-injection-files.json").exists())
        self.assertTrue(validate_checks({})["lambda_injection"])
        self.put("src/demo/main/broken.py", "from demo.domain.api import Builder\n"
                 "Builder(lambda x: x[0])\n")
        (self.root / "src/demo/domain/api.py").unlink()
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
        self.assertIn("LAMBDA_INJECTION_PARSE_ERROR", {d["rule"] for d in result["diagnostics"]})


if __name__ == "__main__":
    unittest.main()
