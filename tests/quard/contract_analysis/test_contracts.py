"""End-to-end scope and report regressions for explicit class contracts."""
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
from quard.check_selection_utils import CHECK_DEFAULTS


class TestContracts(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.temp: TemporaryDirectory[str] = TemporaryDirectory(prefix="quard-contracts-")
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
                   if job["name"] == "class-contracts")
        result = execute_job(job, self.root, self.run_dir, 30)
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        return result, raw, job

    def test_generated_dependency_types_in_source_stubs_and_composition(self):
        self.put(".quard/architecture.generated.json", '{"schema_version": 1}\n')
        self.put("src/pkg/port/port.py", "from typing import Protocol\nclass Port(Protocol): ...\n")
        self.put("src/pkg/port/concrete.py", "from .port import Port\nclass Concrete(Port): ...\n")
        self.put("src/pkg/port/__init__.py", "from .concrete import Concrete as Exported\n")
        self.put("src/pkg/user/consumer.py", '''\
from __future__ import annotations
from typing import TYPE_CHECKING, Self
from pkg.port.port import Port
if TYPE_CHECKING:
    from pkg.port import Exported as Imported
Alias = Imported
class Consumer(Port):
    field: Alias
    no_import: "pkg.port.concrete.Concrete"
    own: Consumer
    port: Port
    def __init__(self, value: Alias, port: Port) -> None:
        self.stored: Alias = value
    def build(self, value: "list[Alias]", missing: "pkg.port.absent.Missing") -> "Alias | Self": ...
''')
        self.put("src/pkg/user/consumer.pyi", '''\
from pkg.port import Exported as Imported
class Consumer:
    field: Imported
    def build(self, value: Imported) -> Imported: ...
''')
        self.put("src/pkg/main/wiring.py", "from pkg.port import Exported\n"
                 "class Wiring:\n    def get(self) -> Exported: ...\n")
        self.put("src/pkg/main/wiring.pyi", "from pkg.port import Exported\n"
                 "class Wiring:\n    def get(self) -> Exported: ...\n")
        self.put("src/pkg/port/envelopes/wiring.py", "from pkg.port import Exported\n"
                 "class Wiring:\n    def get(self) -> Exported: ...\n")
        self.put("src/pkg/port/envelopes/wiring.pyi", "from pkg.port import Exported\n"
                 "class Wiring:\n    def get(self) -> Exported: ...\n")

        def run_dependency():
            files = discover_files(self.root, read_scope(self.root))
            job = next(job for job in make_jobs(self.root, self.run_dir, files, True, [],
                                                checks={**CHECK_DEFAULTS, "dependency_types": True})
                       if job["name"] == "dependency-types")
            return execute_job(job, self.root, self.run_dir, 30)

        result = run_dependency()
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1), result)
        self.assertEqual({(d["path"], d["symbol"], d["rule"]) for d in result["diagnostics"]}, {
            ("src/pkg/user/consumer.py", "Consumer.field", "CONCRETE_DEPENDENCY_TYPE"),
            ("src/pkg/user/consumer.py", "Consumer.no_import", "CONCRETE_DEPENDENCY_TYPE"),
            ("src/pkg/user/consumer.py", "Consumer.__init__.value", "CONCRETE_DEPENDENCY_TYPE"),
            ("src/pkg/user/consumer.py", "Consumer.stored", "CONCRETE_DEPENDENCY_TYPE"),
            ("src/pkg/user/consumer.py", "Consumer.build.value", "CONCRETE_DEPENDENCY_TYPE"),
            ("src/pkg/user/consumer.py", "Consumer.build", "CONCRETE_DEPENDENCY_TYPE"),
            ("src/pkg/user/consumer.py", "Consumer.build.missing", "UNRESOLVED_DEPENDENCY_TYPE"),
            ("src/pkg/user/consumer.pyi", "Consumer.field", "CONCRETE_DEPENDENCY_TYPE"),
            ("src/pkg/user/consumer.pyi", "Consumer.build.value", "CONCRETE_DEPENDENCY_TYPE"),
            ("src/pkg/user/consumer.pyi", "Consumer.build", "CONCRETE_DEPENDENCY_TYPE"),
        })
        self.assertTrue(all(d["line"] > 0 and d["column"] > 0
                            and not d["is_infrastructure_error"] for d in result["diagnostics"]))
        manifest = self.run_dir / "dependency-types-files.json"
        self.assertIn("src/pkg/user/consumer.pyi", json.loads(manifest.read_text()))
        self.put("src/pkg/broken.py", "class Broken(\n")
        result = run_dependency()
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
        self.assertIn("CONCRETE_DEPENDENCY_TYPE", {d["rule"] for d in result["diagnostics"]})

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
class MetaImpl(MetaInterface):
    def run(self): return 3
class Outer(object):
    class Inner: pass
def factory():
    class Local: pass
    return Local
''')
        self.put("src/consumer.py", '''\
from contracts import Port, NoContract, Interface, MetaInterface
from pathlib import Path as ExternalPath
class UsesPort(Port): pass
class UsesABC(Interface): pass
class UsesMeta(MetaInterface): pass
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

    def test_extra_methods_and_properties_need_no_coverage(self):
        self.put("src/pkg/ports.pyi", '''\
from typing import Protocol
class Read(Protocol):
    def read(self) -> str: ...
    @property
    def value(self) -> str: ...
class Write(Protocol):
    def write(self, value: str) -> None: ...
''')
        self.put("src/pkg/reexports.py", "from .ports import Read as Reader, Write as Writer\n")
        self.put("src/pkg/implementation.py", '''\
from .reexports import Reader, Writer
class Middle(Reader): pass
class Complete(Middle, Writer):
    def read(self): return "ok"
    def write(self, value): pass
    @property
    def value(self): return "ok"
    @value.setter
    def value(self, new_value): pass
    def __str__(self): return "complete"
class Extra(Complete):
    def extra(self): return 1
    @property
    def other(self): return 2
    @other.setter
    def other(self, new_value): pass
    def _helper(self): pass
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("passed", 0, None))
        self.assertEqual(result["diagnostics"], [])

    def test_extra_members_are_private_to_implementation_in_both_modes(self):
        for generated in (False, True):
            with self.subTest(generated=generated):
                if generated:
                    self.put(".quard/architecture.generated.json",
                             '{"policy": "contract-only", "version": 2}\n')
                self.put("src/pkg/contracts.py", '''\
from typing import Protocol
class Port(Protocol):
    def run(self): ...
class Unrelated(Protocol):
    def extra(self): ...
''')
                self.put("src/pkg/usage.py", '''\
from .contracts import Port
class Base(Port):
    def run(self): pass
    def extra(self): pass
    @property
    def value(self): return 1
class Child(Base):
    def extra(self): pass
class Missing:
    def extra(self): pass
''')
                self.put("src/pkg/usage.pyi", '''\
from .contracts import Port
class Base(Port):
    def run(self): ...
    def extra(self): ...
    @property
    def value(self): ...
''')
                self.put("tests/test_usage.py", '''\
from pkg.contracts import Port
class TestImplementation(Port):
    def run(self): pass
    def extra(self): pass
''')
                result, raw, job = self.run_worker()
                self.assertEqual((job["name"], result["status"], result["exit_code"],
                                  raw["execution_error"]),
                                 ("class-contracts", "violations", 1, None))
                self.assertEqual({(d["path"], d["symbol"], d["rule"])
                                  for d in result["diagnostics"]}, {
                    ("src/pkg/usage.py", "Child.extra", "CUSTOMER_METHOD_OVERRIDE"),
                    ("src/pkg/usage.py", "Missing", "MISSING_CLASS_CONTRACT"),
                })

    def test_abstract_contract_and_external_base_tradeoff(self):
        self.put("src/ports.py", '''\
from abc import ABC, abstractmethod
class Port(ABC):
    @abstractmethod
    def run(self): ...
    @property
    @abstractmethod
    def status(self): ...
class Plain: pass
''')
        self.put("src/usage.py", '''\
from ports import Port, Plain
from pathlib import Path as External
class Local(Port):
    def run(self): pass
    @property
    def status(self): return "ok"
    def extra(self): pass
class Mixed(Port, External):
    def run(self): pass
    def extra(self): pass
class Descendant(Mixed):
    def another(self): pass
class ObjectOnly(object):
    def extra(self): pass
class Unproven(Plain):
    def extra(self): pass
''')
        self.put("tests/test_usage.py", '''\
from ports import Port
class TestImpl(Port):
    def run(self): pass
    def unlisted(self): pass
''')
        result, _, _ = self.run_worker()
        self.assertEqual({(d["path"], d["symbol"], d["rule"]) for d in result["diagnostics"]}, {
            ("src/ports.py", "Plain", "MISSING_CLASS_CONTRACT"),
            ("src/usage.py", "Unproven", "MISSING_CLASS_CONTRACT")})

    def test_stub_implementation_members_and_unknown_external_provenance(self):
        self.put("src/pkg/ports.py", "from typing import Protocol\nclass Port(Protocol):\n    def run(self): ...\n")
        self.put("src/pkg/usage.pyi", '''\
from .ports import Port
from pkg.unscanned import Missing
class Impl(Port):
    def run(self): ...
    def unlisted(self): ...
class Unknown(Port, Missing):
    def unlisted(self): ...
''')
        result, _, _ = self.run_worker()
        self.assertEqual(result["diagnostics"], [])

    def test_source_and_stub_resolve_their_own_ancestry(self):
        self.put("src/ports.py", "from typing import Protocol\nclass Port(Protocol):\n    def run(self): ...\n")
        self.put("src/usage.py", "class Both(object):\n    def extra(self): pass\n")
        self.put("src/usage.pyi", '''\
from ports import Port
class Both(Port):
    def run(self): ...
''')
        result, _, _ = self.run_worker()
        self.assertEqual(result["diagnostics"], [])

    def test_assigned_properties_and_non_instance_decorators(self):
        self.put("src/usage.py", '''\
from typing import Protocol
from functools import cached_property as cache
class Port(Protocol):
    @property
    def value(self): ...
class Impl(Port):
    def _value(self): return 1
    value = property(_value)
    extra = cache(_value)
    @staticmethod
    def factory(): pass
    @classmethod
    def create(cls): pass
class Covered(Port):
    @cache
    def value(self): return 1
''')
        result, _, _ = self.run_worker()
        self.assertEqual(result["diagnostics"], [])

    def test_customer_method_redeclaration_across_source_test_and_stub(self):
        self.put("src/pkg/ports.py", '''\
from typing import Protocol
class Port(Protocol):
    def run(self): return 1
class Base(Port):
    def run(self): return 2
    def _helper(self): return 3
    @property
    def value(self): return 4
    @staticmethod
    def create(): return 5
''')
        self.put("src/pkg/public.py", "from .ports import Base as Parent\n")
        self.put("src/pkg/usage.py", '''\
from .public import Parent
from .ports import Port
class Middle(Parent): pass
class Impl(Middle, Port):
    def run(self): return 1
    def _helper(self): return 2
    @property
    def value(self): return 3
    @classmethod
    def create(cls): return cls
''')
        self.put("tests/test_usage.py", '''\
from pkg.ports import Base
class TestImpl(Base):
    async def run(self): return 1
''')
        self.put("src/pkg/stubs.pyi", '''\
from .ports import Base
class Stub(Base):
    def _helper(self) -> None: ...
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        overrides = [d for d in result["diagnostics"] if d["rule"] == "CUSTOMER_METHOD_OVERRIDE"]
        self.assertEqual({(d["path"], d["symbol"], d["line"], d["column"])
                          for d in overrides}, {
                              ("src/pkg/usage.py", "Impl.run", 5, 5),
                              ("src/pkg/usage.py", "Impl._helper", 6, 5),
                              ("src/pkg/usage.py", "Impl.value", 8, 5),
                              ("src/pkg/usage.py", "Impl.create", 10, 5),
                              ("tests/test_usage.py", "TestImpl.run", 3, 5),
                              ("src/pkg/stubs.pyi", "Stub._helper", 3, 5)})
        self.assertTrue(all(not d["is_infrastructure_error"] and "Base" in d["message"]
                            for d in overrides))

    def test_contract_bodies_magic_methods_and_unproven_bases_are_not_overrides(self):
        self.put("src/pkg/usage.py", '''\
from typing import Protocol
from abc import ABC, abstractmethod
from pathlib import Path
class Port(Protocol):
    def run(self): return 1
class Interface(ABC):
    @abstractmethod
    def work(self): return 2
class Base(Port):
    def run(self): return 3
    def __str__(self): return "base"
    def __hidden(self): return 4
    def __custom__(self): return 5
    def assigned(self): return 6
class FromContracts(Port, Interface):
    def run(self): return 1
    def work(self): return 2
class FromExternal(Path):
    def run(self): return 1
class FromUnknown(Unknown):
    def run(self): return 1
class Child(Base):
    def __str__(self): return "child"
    def __hidden(self): return 1
    def __custom__(self): return 1
    assigned = lambda self: None
class ContractChild(Port):
    def run(self): return 1
''')
        result, _, _ = self.run_worker()
        self.assertEqual({(d["symbol"], d["rule"]) for d in result["diagnostics"]
                          if d["rule"] == "CUSTOMER_METHOD_OVERRIDE"},
                         {("Child.__custom__", "CUSTOMER_METHOD_OVERRIDE")})

    def test_override_from_stub_base_in_both_source_and_stub_and_parse_failure(self):
        self.put("src/pkg/parent.pyi", '''\
from typing import Protocol
class Port(Protocol):
    def run(self) -> None: ...
class Parent(Port):
    def run(self) -> None: ...
''')
        self.put("src/pkg/usage.py", "from .parent import Parent\nclass Impl(Parent):\n"
                 "    def run(self): pass\n")
        self.put("src/pkg/usage.pyi", "from .parent import Parent\nclass Impl(Parent):\n"
                 "    def run(self) -> None: ...\n")
        result, _, _ = self.run_worker()
        self.assertEqual({(d["path"], d["symbol"]) for d in result["diagnostics"]
                          if d["rule"] == "CUSTOMER_METHOD_OVERRIDE"},
                         {("src/pkg/usage.py", "Impl.run"),
                          ("src/pkg/usage.pyi", "Impl.run")})
        self.put("src/bad.py", "class Broken(\n")
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "CLASS_CONTRACTS_PARSE_ERROR"))
        self.assertIn("CUSTOMER_METHOD_OVERRIDE", {d["rule"] for d in result["diagnostics"]})

    def test_redeclared_property_accessors_have_one_member_location(self):
        self.put("src/usage.py", '''\
class Base(object):
    @property
    def value(self): return 1
    @value.setter
    def value(self, new_value): pass
class Child(Base):
    @property
    def value(self): return 2
    @value.setter
    def value(self, new_value): pass
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]
                           if d["rule"] == "CUSTOMER_METHOD_OVERRIDE"],
                         [(8, "Child.value")])

    def test_properties_match_methods_setters_and_assigned_properties(self):
        self.put("src/properties.py", '''\
from functools import cached_property as cache
class Base(object):
    @property
    def value(self): return 1
    def method(self): return 2
    def _private(self): return 3
    def source(self): return 4
    assigned = property(source)
    cached = cache(source)
class Middle(Base): pass
class Child(Middle):
    @value.setter
    def value(self, new_value): pass
    @property
    def method(self): return 1
    def assigned(self): return 1
    def cached(self): return 1
    _private = property(lambda self: 1)
class Sibling(Middle):
    method = property(lambda self: 2)
    @method.setter
    def method(self, new_value): pass
''')
        result, _, _ = self.run_worker()
        self.assertEqual({(d["symbol"], d["line"], d["column"])
                          for d in result["diagnostics"]
                          if d["rule"] == "CUSTOMER_METHOD_OVERRIDE"}, {
                              ("Child.value", 13, 5), ("Child.method", 15, 5),
                              ("Child.assigned", 16, 5), ("Child.cached", 17, 5),
                              ("Child._private", 18, 5), ("Sibling.method", 20, 5)})
        self.assertEqual(len([d for d in result["diagnostics"]
                              if d["rule"] == "CUSTOMER_METHOD_OVERRIDE"]), 6)

    def test_all_ancestors_aliases_mro_and_source_stub_precedence(self):
        self.put("src/pkg/ports.pyi", '''\
from typing import Protocol
class Port(Protocol):
    def run(self): ...
    @property
    def state(self): ...
''')
        self.put("src/pkg/bases.pyi", '''\
from .ports import Port
class StubBase(Port):
    def run(self): ...
    @property
    def state(self): ...
class Dual(StubBase):
    def stub_only(self): ...
    def shared(self): ...
''')
        self.put("src/pkg/bases.py", '''\
from .ports import Port
class Dual(Port):
    def shared(self): pass
    def source_only(self): pass
class Outer(object):
    class Nested(Port):
        def nested(self): pass
''')
        self.put("src/pkg/__init__.py", "from .bases import StubBase as PublicStub, "
                 "Dual as PublicDual, Outer as PublicOuter\n")
        self.put("src/pkg/relay.py", "from . import PublicStub as Stub, PublicDual as Dual, PublicOuter as Outer\n")
        self.put("src/pkg/usage.py", '''\
from .relay import Stub, Dual, Outer
from .ports import Port
from pathlib import Path as External
from pkg.absent import Unknown
class First(Port, Stub):
    def run(self): pass
    @state.setter
    def state(self, value): pass
class Middle(External, Outer.Nested, Dual): pass
class Last(Port, Middle, Unknown):
    def nested(self): pass
    def shared(self): pass
    def source_only(self): pass
    def stub_only(self): pass
    def run(self): pass
class UnknownOnly(Unknown, External):
    def missing(self): pass
''')
        self.put("src/pkg/usage.pyi", '''\
from .relay import Stub, Dual
class First(Stub):
    def run(self): ...
class Last(Dual):
    def shared(self): ...
    def source_only(self): ...
    def stub_only(self): ...
    def run(self): ...
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual({(d["path"], d["symbol"], d["line"])
                          for d in result["diagnostics"]
                          if d["rule"] == "CUSTOMER_METHOD_OVERRIDE"}, {
                              ("src/pkg/usage.py", "First.run", 6),
                              ("src/pkg/usage.py", "First.state", 8),
                              ("src/pkg/usage.py", "Last.nested", 11),
                              ("src/pkg/usage.py", "Last.shared", 12),
                              ("src/pkg/usage.py", "Last.source_only", 13),
                              ("src/pkg/usage.pyi", "First.run", 3),
                              ("src/pkg/usage.pyi", "Last.shared", 5),
                              ("src/pkg/usage.pyi", "Last.source_only", 6)})

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
            "stdout": '{"quard_class_contracts_adapter": 1, "diagnostics": [], "execution_error": null}',
            "stderr": "", "exit_code": 1, "duration_seconds": 0, "error": None})
        self.assertEqual((result["status"], result["error"]),
                         ("error", "CLASS_CONTRACTS_EXECUTION_ERROR"))


if __name__ == "__main__":
    unittest.main()
