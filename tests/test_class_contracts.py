"""End-to-end scope and report regressions for explicit class contracts."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX / "src"))

from eryx._policy import discover_files, read_scope
from eryx.check import execute_job, make_jobs


class ClassContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="eryx-contracts-")
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
                   if job["name"] == "class-contracts")
        result = execute_job(job, self.root, self.run_dir, 30)
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        return result, raw, job

    def test_local_contracts_external_bases_and_full_scope(self):
        self.put("src/contracts.py", '''\
from typing import Protocol as P
from abc import ABC as Abstract, ABCMeta as Meta, abstractmethod as required

class Port(P):
    def run(self): ...
class Interface(Abstract):
    @required
    def run(self): ...
class NoContract:
    def run(self): ...
class Implementation(Port):
    def run(self): return 1
class Derived(Implementation): pass
class AbstractImpl(Interface):
    def run(self): return 2
class BareObject: pass
class ExplicitObject(object): pass
class External(list): pass
class PretendProtocol: pass
class Other(PretendProtocol): pass
class MetaInterface(metaclass=Meta):
    @required
    def run(self): ...
class Outer(object):
    class Inner: pass
def factory():
    class Local: pass
    return Local
''')
        self.put("src/consumer.py", '''\
from contracts import Port, NoContract, Interface
from pathlib import Path as ExternalPath
class UsesPort(Port): pass
class UsesABC(Interface): pass
class UsesConcrete(NoContract): pass
class UsesExternal(ExternalPath): pass
''')
        self.put("src/defs.pyi", "class Stub: ...\nclass Explicit(object): ...\n")
        self.put("tests/test_customer.py", "class TestCase: pass\n")
        self.put("src/generated/ignored.py", "class Ignored: pass\n")
        result, raw, job = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual({(d["path"], d["symbol"]) for d in result["diagnostics"]}, {
            ("src/contracts.py", "NoContract"), ("src/contracts.py", "BareObject"),
            ("src/contracts.py", "PretendProtocol"), ("src/contracts.py", "Other"),
            ("src/contracts.py", "Outer.Inner"), ("src/contracts.py", "factory.Local"),
            ("src/consumer.py", "UsesConcrete"), ("src/defs.pyi", "Stub"),
            ("tests/test_customer.py", "TestCase")})
        self.assertTrue(all(d["line"] > 0 and d["column"] > 0 and
                            not d["is_infrastructure_error"] for d in result["diagnostics"]))
        self.assertEqual(job["tool"], "class-contracts")
        self.assertIn("src/defs.pyi", json.loads((self.run_dir / "class-contracts-files.json").read_text()))

    def test_shadowed_protocol_and_object_are_not_proven_contracts(self):
        self.put("src/shadow.py", '''\
from typing import Protocol
Protocol = object
object = lambda: None
class Pretend(Protocol): pass
class ShadowObject(object): pass
''')
        result, _, _ = self.run_worker()
        self.assertEqual({d["symbol"] for d in result["diagnostics"]},
                         {"Pretend", "ShadowObject"})

    def test_cross_module_aliases_ancestors_nested_classes_and_stubs(self):
        self.put("src/pkg/__init__.py", "from .contracts import Port as PublicPort\n")
        self.put("src/pkg/contracts.pyi", '''\
from typing import Protocol as P
class Port(P):
    def run(self) -> None: ...
class NotAContract: ...
''')
        self.put("src/pkg/reexport.py", "from . import PublicPort as Alias\n")
        self.put("src/pkg/middle.py", '''\
from .reexport import Alias as Parent
from .contracts import NotAContract as Plain
class Middle(Parent): pass
class Unproven(Plain): pass
class Outer(object):
    class Nested(Middle): pass
    class Broken(Unproven): pass
''')
        self.put("src/pkg/usage.py", '''\
from .middle import Middle as Imported, Unproven as Bad, Outer
import pkg.middle as module_alias
class Good(Imported): pass
class Qualified(module_alias.Outer.Nested): pass
class NestedGood(Outer.Nested): pass
class Missing(Bad): pass
class MissingNested(Outer.Broken): pass
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual({(d["path"], d["symbol"]) for d in result["diagnostics"]}, {
            ("src/pkg/contracts.pyi", "NotAContract"),
            ("src/pkg/middle.py", "Unproven"), ("src/pkg/middle.py", "Outer.Broken"),
            ("src/pkg/usage.py", "Missing"), ("src/pkg/usage.py", "MissingNested")})

    def test_unresolved_customer_bases_do_not_count_as_external(self):
        self.put("src/pkg/known.py", "class Plain: pass\n")
        self.put("src/pkg/consumer.py", '''\
from pkg.known import Absent as Missing
from pkg.unscanned import Parent as Unscanned
import pkg.known as known
from pathlib import Path as External
class MissingName(Missing): pass
class MissingModule(Unscanned): pass
class MissingAttribute(known.Absent): pass
class QualifiedMissing(pkg.known.Absent): pass
class ProvenExternal(External): pass
class UnknownBare(Unknown): pass
''')
        result, _, _ = self.run_worker()
        self.assertEqual({d["symbol"] for d in result["diagnostics"]}, {
            "Plain", "MissingName", "MissingModule", "MissingAttribute",
            "QualifiedMissing", "UnknownBare"})

    def test_external_source_is_not_scanned(self):
        self.put("src/usage.py", "from pathlib import Path as Base\nclass Local(Base): pass\n")
        result, _, _ = self.run_worker()
        self.assertEqual(result["diagnostics"], [])

    def test_python_source_and_stub_both_receive_diagnostics(self):
        self.put("src/pkg.py", "class Both: pass\n")
        self.put("src/pkg.pyi", "class Both: ...\n")
        result, _, _ = self.run_worker()
        self.assertEqual({d["path"] for d in result["diagnostics"]},
                         {"src/pkg.py", "src/pkg.pyi"})

    def test_source_and_stub_declarations_have_independent_contracts(self):
        self.put("src/pkg.py", "class Both(object): pass\n")
        self.put("src/pkg.pyi", "class Both: ...\n")
        result, _, _ = self.run_worker()
        self.assertEqual({d["path"] for d in result["diagnostics"]}, {"src/pkg.pyi"})

    def test_parse_error_retains_violations_and_fails_as_infrastructure(self):
        self.put("src/good.py", "class Missing: pass\n")
        self.put("src/bad.pyi", "class Broken(\n")
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "CLASS_CONTRACTS_PARSE_ERROR"))
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"MISSING_CLASS_CONTRACT", "CLASS_CONTRACTS_PARSE_ERROR"})

    def test_malformed_output_fails_closed(self):
        self.put("src/one.py", "class One(object): pass\n")
        _, _, job = self.run_worker()
        result = execute_job(job, self.root, self.run_dir, 30, capture_fn=lambda *args: {
            "stdout": '{"eryx_class_contracts_adapter": 1, "diagnostics": [], "execution_error": null}',
            "stderr": "", "exit_code": 1, "duration_seconds": 0, "error": None})
        self.assertEqual((result["status"], result["error"]),
                         ("error", "CLASS_CONTRACTS_EXECUTION_ERROR"))


if __name__ == "__main__":
    unittest.main()
