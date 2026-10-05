"""Consumer-facing restricted-call worker and runner regressions."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import override
import unittest

from jsonschema import validate

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))
sys.path.insert(0, str(ERYX / "tests"))

from eryx._encapsulated_only_worker import analyze
from eryx._policy import discover_files, read_scope
from eryx.check import execute_job, make_jobs


class TestCalls(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.temp: TemporaryDirectory[str] = TemporaryDirectory(prefix="eryx-restricted-")
        self.addCleanup(self.temp.cleanup)
        self.root: Path = Path(self.temp.name)
        self.run_dir: Path = self.root / ".eryx/reports/run"

    @override
    def setUp(self):
        (self.root / "src").mkdir()
        (self.root / ".eryx").mkdir()
        (self.root / ".eryx/config.toml").write_text(
            (ERYX / "defaults/config.toml").read_text().split("[scope]")[0]
            + '[scope]\nexclude_directory_names = ["generated"]\n'
            'test_directory_names = ["tests"]\n', encoding="utf-8")
        self.run_dir.mkdir(parents=True)

    def put(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def run_worker(self, name="encapsulated-only", selected=None):
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, selected)
                   if job["name"] == name)
        result = execute_job(job, self.root, self.run_dir, 30)
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        return result, raw, job

    def assert_calls(self, source, forbidden):
        self.put("src/example.py", source)
        result, raw, _ = self.run_worker(selected=[])
        self.assertEqual(raw["execution_error"], None)
        self.assertEqual(result["status"], "violations" if forbidden else "passed")
        self.assertEqual(result["exit_code"], 1 if forbidden else 0)
        self.assertEqual([d["line"] for d in result["diagnostics"]], forbidden)
        self.assertTrue(all(d["rule"] == "ENCAPSULATED_ONLY_CALL" and
                            not d["is_infrastructure_error"] for d in result["diagnostics"]))

    def test_migrated_traversal_retains_class_and_member_evidence(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
@encapsulated_only
class Base:
    def run(self): pass
    @property
    def secret(self): return 1
    @encapsulated_only
    def __len__(self): return 1
class Child(Base): pass
class Client:
    def check(self, peer: Child):
        peer.run()
        peer.secret
        len(peer)
''')
        result, raw, _ = self.run_worker(selected=[])
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["rule"], d["line"], d["column"], d["symbol"])
                          for d in result["diagnostics"]], [
            ("ENCAPSULATED_ONLY_CALL", 12, 9, "example.Base.run"),
            ("ENCAPSULATED_ONLY_PROPERTY", 13, 9, "example.Base.secret"),
            ("ENCAPSULATED_ONLY_CALL", 14, 9, "example.Base.__len__")])
        self.assertTrue(all(not d["is_infrastructure_error"] for d in result["diagnostics"]))
        self.assertIn({"owner": "example.Base", "path": "src/example.py", "line": 3,
                       "column": 1, "marked": True}, raw["classes"])
        self.assertTrue(any(d["member"] == "secret" and d["accessor"] == "getter"
                            for d in raw["declarations"]))
        self.assertEqual(raw["effective_targets"], {
            "example.Base.run": "example.Base",
            "example.Base.secret": "example.Base",
            "example.Base.__len__": "example.Base.__len__"})

    def test_migrated_ancestry_retains_alias_dispatch_and_field_diagnostics(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Base:
    def __init__(self): self.data = 1
    @encapsulated_only
    def run(self): pass
Alias = Base
class Child(Alias): pass
class Client:
    def check(self, peer: Child):
        peer.run()
        peer.data
''')
        calls, raw, _ = self.run_worker(selected=[])
        self.assertEqual((calls["status"], calls["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["rule"], d["line"], d["column"], d["symbol"])
                          for d in calls["diagnostics"]],
                         [("ENCAPSULATED_ONLY_CALL", 10, 9, "example.Base.run")])
        self.assertEqual(raw["effective_targets"], {"example.Base.run": "example.Base.run"})
        fields, field_raw, _ = self.run_worker(name="instance-fields", selected=[])
        self.assertEqual((fields["status"], fields["exit_code"], field_raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["rule"], d["line"], d["column"], d["symbol"])
                          for d in fields["diagnostics"]],
                         [("EXTERNAL_INSTANCE_FIELD", 11, 9, "example.Base.data")])
        _, error, restrictions = analyze(self.root, ["src/example.py"])
        self.assertIsNone(error)
        self.assertEqual(restrictions.parents["example.Child"], ("example.Base",))

    def test_builtin_protocol_helpers_match_direct_receivers_and_shadowing(self):
        self.put("src/example.py", '''\
import builtins as native
from builtins import len as length
def encapsulated_only(value): return value
class Item:
    @encapsulated_only
    def __len__(self): return 1
    @encapsulated_only
    def __str__(self): return "item"
    @encapsulated_only
    def __repr__(self): return "item"
    @encapsulated_only
    def __format__(self, spec): return "item"
    @encapsulated_only
    def __index__(self): return 1
class Owner:
    def __init__(self, field: Item): self.field = field
    def run(self, peer: Item):
        len(self.field)
        length(self.field)
        native.str(self.field)
        len(peer)
        length(peer)
        native.str(peer)
        repr(peer)
        ascii(peer)
        format(peer, "")
        int(peer)
        hex(peer)
        peer.__len__()
        alias = native.len
        alias(peer)
        saved = native.len
        alias = lambda obj: 0
        alias(peer)
        def shadow():
            def len(obj): return 0
            len(peer)
        native.len = lambda obj: 0
        native.len(peer)
        saved(peer)
''')
        result, _, _ = self.run_worker(selected=[])
        calls = [(d["line"], d["symbol"], d["column"]) for d in result["diagnostics"]
                 if d["rule"] == "ENCAPSULATED_ONLY_CALL"]
        self.assertEqual(calls, [
            (21, "example.Item.__len__", 9), (22, "example.Item.__len__", 9),
            (23, "example.Item.__str__", 9), (24, "example.Item.__repr__", 9),
            (25, "example.Item.__repr__", 9), (26, "example.Item.__format__", 9),
            (27, "example.Item.__index__", 9), (28, "example.Item.__index__", 9),
            (29, "example.Item.__len__", 9), (31, "example.Item.__len__", 9),
            (40, "example.Item.__len__", 9)])
        self.assertEqual(result["status"], "violations")
        self.assertTrue(all(not d["is_infrastructure_error"] for d in result["diagnostics"]))

    def test_builtin_iterator_helpers_check_generated_and_self_iterators(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Generated:
    @encapsulated_only
    def __next__(self): return 1
class Stream:
    @encapsulated_only
    def __iter__(self) -> Generated: return Generated()
class SelfStream:
    @encapsulated_only
    def __iter__(self): return self
    @encapsulated_only
    def __next__(self): return 1
class Owner:
    def __init__(self, field: SelfStream): self.field = field
    def run(self, peer: SelfStream, stream: Stream):
        iter(self.field)
        next(iter(self.field))
        next(iter(peer))
        next(iter(stream))
        list(stream)
        tuple(stream)
        any(stream)
        all(stream)
        iter(peer)
        peer.__iter__()
        iterator = iter(self.field)
        next(iterator)
        maybe = stream if True else object()
        next(iter(maybe))
''')
        result, _, _ = self.run_worker(selected=[])
        calls = [(d["line"], d["symbol"]) for d in result["diagnostics"]
                 if d["rule"] == "ENCAPSULATED_ONLY_CALL"]
        self.assertEqual(calls, [
            (18, "example.SelfStream.__iter__"), (18, "example.SelfStream.__next__"),
            (19, "example.Stream.__iter__"), (19, "example.Generated.__next__"),
            (20, "example.Stream.__iter__"), (20, "example.Generated.__next__"),
            (21, "example.Stream.__iter__"), (21, "example.Generated.__next__"),
            (22, "example.Stream.__iter__"), (22, "example.Generated.__next__"),
            (23, "example.Stream.__iter__"), (23, "example.Generated.__next__"),
            (24, "example.SelfStream.__iter__"), (25, "example.SelfStream.__iter__"),
            (27, "example.SelfStream.__next__"),
            (29, "example.Stream.__iter__"), (29, "example.Generated.__next__")])

    def test_builtin_fallback_targets_mixed_evidence_and_field_aliases(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Base:
    @encapsulated_only
    def __index__(self): return 2
    @encapsulated_only
    def __repr__(self): return "base"
class Override(Base):
    def __int__(self): return 3
    def __str__(self): return "overridden"
class Owner:
    def __init__(self, field: Base): self.field = field
    def run(self, peer: Base, choose: bool):
        int(self.field)
        int(peer)
        int(Override())
        str(peer)
        str(Override())
        value = self.field
        int(value)
        option = peer if choose else object()
        int(option)
        float(peer)
        complex(peer)
        float(Override())
        from builtins import int as convert
        convert(peer)
        convert = lambda x: 0
        convert(peer)
        format(peer)
        format(peer, "x")
''')
        result, _, _ = self.run_worker(selected=[])
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]
                          if d["rule"] == "ENCAPSULATED_ONLY_CALL"], [
            (14, "example.Base.__index__"), (16, "example.Base.__repr__"),
            (19, "example.Base.__index__"), (21, "example.Base.__index__"),
            (22, "example.Base.__index__"), (23, "example.Base.__index__"),
            (24, "example.Base.__index__"), (26, "example.Base.__index__"),
            (29, "example.Base.__repr__")])

    def test_agreement_contracts_conflicts_and_access_survive_mismatch(self):
        self.put("src/example.py", '''\
from typing import Protocol
from abc import ABC, abstractmethod
def encapsulated_only(value): return value
alias = encapsulated_only
class Marked(Protocol):
    @alias
    def run(self): ...
class Plain(Protocol):
    def run(self): ...
class Abstract(ABC):
    @abstractmethod
    @alias
    def other(self): ...
class Missing(Marked):
    def run(self): pass
class Extra(Plain):
    @alias
    def run(self): pass
class Equivalent(Marked, Abstract):
    @alias
    def run(self): pass
    @alias
    def other(self): pass
class Conflict(Marked, Plain):
    @alias
    def run(self): pass
class Base(Marked):
    @alias
    def run(self): pass
class Descendant(Base): pass
class Resembles:
    def run(self): pass
Missing().run()
Extra().run()
''')
        result, _, _ = self.run_worker()
        agreements = [d for d in result["diagnostics"]
                      if d["rule"] == "ENCAPSULATED_ONLY_DECLARATION"]
        self.assertEqual([(d["line"], d["symbol"]) for d in agreements], [
            (15, "example.Missing.run"), (18, "example.Extra.run"),
            (24, "example.Conflict.run")])
        self.assertTrue(all("example." in d["message"] and not d["is_infrastructure_error"]
                            for d in agreements))
        self.assertIn((33, "example.Missing.run"),
                      [(d["line"], d["symbol"]) for d in result["diagnostics"]
                       if d["rule"] == "ENCAPSULATED_ONLY_CALL"])

    def test_agreement_source_stub_overloads_properties_and_class_equivalence(self):
        self.put("src/marker.py", "def encapsulated_only(value): return value\n")
        self.put("src/example.py", '''\
from marker import encapsulated_only as mark
from typing import overload
class Service:
    @mark
    def run(self): pass
    @mark
    @property
    def item(self): return 1
    @overload
    @mark
    def choose(self, value: int) -> int: ...
    @overload
    def choose(self, value: str) -> str: ...
    def choose(self, value): return value
@mark
class Equivalent:
    def run(self): pass
    @property
    def item(self): return 1
Service().run()
Service().item
''')
        self.put("src/example.pyi", '''\
from marker import encapsulated_only as alias
class Service:
    def run(self) -> None: ...
    @property
    def item(self) -> int: ...
    def choose(self, value: object) -> object: ...
@alias
class Equivalent:
    @alias
    def run(self) -> None: ...
    @alias
    @property
    def item(self) -> int: ...
''')
        result, raw, _ = self.run_worker()
        agreements = [d for d in result["diagnostics"]
                      if d["rule"] == "ENCAPSULATED_ONLY_DECLARATION"]
        self.assertEqual({(d["path"], d["line"], d["symbol"]) for d in agreements}, {
            ("src/example.py", 13, "example.Service.choose"),
            ("src/example.py", 14, "example.Service.choose"),
            ("src/example.pyi", 3, "example.Service.run"),
            ("src/example.pyi", 5, "example.Service.item"),
            ("src/example.pyi", 6, "example.Service.choose")})
        self.assertEqual([(d["line"], d["rule"]) for d in result["diagnostics"]
                          if d["rule"] in {"ENCAPSULATED_ONLY_CALL", "ENCAPSULATED_ONLY_PROPERTY"}],
                         [(20, "ENCAPSULATED_ONLY_CALL"), (21, "ENCAPSULATED_ONLY_PROPERTY")])
        self.assertIn("example.Service.run", raw["effective_targets"])

    def test_agreement_class_marker_and_whole_property_inherited(self):
        self.put("src/example.py", '''\
from typing import Protocol
from functools import cached_property
def encapsulated_only(value): return value
class Contract(Protocol):
    @property
    def item(self) -> int: ...
    @item.setter
    @encapsulated_only
    def item(self, value: int): ...
    @encapsulated_only
    @cached_property
    def cached(self) -> int: ...
@encapsulated_only
class Matching(Contract):
    @property
    def item(self) -> int: return 1
    @cached_property
    def cached(self) -> int: return 1
class Mismatch(Contract):
    @property
    def item(self) -> int: return 1
    @cached_property
    def cached(self) -> int: return 1
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (21, "example.Mismatch.item"), (23, "example.Mismatch.cached")])

    def test_agreement_transitive_contracts_stubs_and_conflict_without_implementation(self):
        self.put("src/marker.py", "def encapsulated_only(value): return value\n")
        self.put("src/example.pyi", '''\
from typing import Protocol
from marker import encapsulated_only as mark
class Restricted(Protocol):
    @mark
    def run(self) -> None: ...
class Unrestricted(Protocol):
    def run(self) -> None: ...
class Middle(Restricted): ...
class Missing(Middle):
    def run(self) -> None: ...
class Conflict(Middle, Unrestricted): ...
class StubOnly(Protocol):
    @mark
    def act(self) -> None: ...
class StubImpl(StubOnly):
    def act(self) -> None: ...
''')
        result, raw, _ = self.run_worker(selected=[])
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (10, "example.Missing.run"), (11, "example.Conflict.run"),
            (16, "example.StubImpl.act")])
        self.assertEqual(raw["execution_error"], None)

    def test_agreement_companion_class_marks_on_inherited_members(self):
        self.put("src/marker.py", "def encapsulated_only(value): return value\n")
        self.put("src/example.py", '''\
from marker import encapsulated_only as mark
class Base:
    def run(self): pass
@mark
class Child(Base): pass
class Peer(Base): pass
@mark
class Equivalent(Base): pass
''')
        self.put("src/example.pyi", '''\
from marker import encapsulated_only as alias
class Base:
    def run(self) -> None: ...
class Child(Base): ...
@alias
class Peer(Base): ...
class Equivalent(Base):
    @alias
    def run(self) -> None: ...
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["path"], d["line"], d["symbol"]) for d in result["diagnostics"]], [
            ("src/example.pyi", 4, "example.Child.run"),
            ("src/example.pyi", 6, "example.Peer.run")])

    def test_agreement_real_runner_report_keeps_independent_diagnostics(self):
        from eryx._common import capture
        from eryx.check import run_checks
        from offline_fixtures import create_repo, fake_tools, native_result

        with TemporaryDirectory(prefix="eryx-agreement-report-", dir=self.root) as directory:
            root = Path(directory)
            create_repo(root)
            (root / "src/demo/domain/api.py").write_text('''\
from typing import Protocol
def encapsulated_only(value): return value
class Contract(Protocol):
    @encapsulated_only
    def run(self): ...
class Implementation(Contract):
    def run(self): pass
Implementation().run()
''', encoding="utf-8")
            fake, _ = fake_tools()

            def mixed(argv, cwd, timeout):
                if str(ERYX / "_encapsulated_only_worker.py") in argv:
                    return capture(argv, cwd, timeout)
                if "pyright" in argv:
                    return native_result(argv, code=1, stdout=json.dumps({"generalDiagnostics": [{
                        "file": "src/demo/domain/api.py", "message": "Independent type error",
                        "rule": "reportArgumentType", "severity": "error",
                        "range": {"start": {"line": 0, "character": 0}}}]}))
                return fake(argv, cwd, timeout)

            report = run_checks(root, lcom_files="", capture_fn=mixed,
                                versions_fn=lambda _: ({}, []))
            validate(report, json.loads((ERYX / "report.schema.json").read_text(encoding="utf-8")))
            self.assertEqual((report["status"], report["exit_code"]), ("violations", 1))
            self.assertEqual({d["rule"] for d in report["diagnostics"]}, {
                "ENCAPSULATED_ONLY_DECLARATION", "ENCAPSULATED_ONLY_CALL", "reportArgumentType"})
            job = next(tool for tool in report["tools"] if tool["name"] == "encapsulated-only")
            self.assertEqual((job["status"], job["exit_code"]), ("violations", 1))
            self.assertTrue((root / job["raw_output"]["stdout"]).is_file())

    def test_property_accessors_and_cached_values_follow_receiver_boundary(self):
        self.put("src/example.py", '''\
from functools import cached_property as cached
import builtins as bi
def encapsulated_only(value): return value
class Service:
    @property
    def item(self) -> int: return 1
    @item.setter
    @encapsulated_only
    def item(self, value: int): pass
    @item.deleter
    def item(self): pass
    @encapsulated_only
    @cached
    def result(self) -> int: return 1
    def work(self, peer: Service):
        alias = self
        alias.item
        alias.item = 2
        del alias.item
        self.result
        self.result = 2
        del self.result
        peer.item
        peer.item = 2
        del peer.item
        peer.result
        peer.result = 3
        del peer.result
        bi.getattr(peer, "item")
        bi.setattr(peer, "item", 2)
        bi.delattr(peer, "item")
        g = bi.getattr
        g(peer, "result")
        g = lambda obj, name: None
        g(peer, "item")
        def getattr(obj, name): return None
        getattr(peer, "item")
''')
        result, raw, _ = self.run_worker(selected=[])
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (23, "example.Service.item"), (24, "example.Service.item"),
            (25, "example.Service.item"), (26, "example.Service.result"),
            (27, "example.Service.result"), (28, "example.Service.result"),
            (29, "example.Service.item"), (30, "example.Service.item"),
            (31, "example.Service.item"), (33, "example.Service.result")])
        self.assertTrue(all(d["rule"] == "ENCAPSULATED_ONLY_PROPERTY" for d in result["diagnostics"]))
        self.assertEqual([(d["accessor"], d["marked"]) for d in raw["declarations"]
                          if d["member"] == "item"],
                         [("getter", False), ("setter", True), ("deleter", False)])

    def test_accessors_keep_current_instance_receiver(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Service:
    @encapsulated_only
    @property
    def item(self) -> int: return 1
    @item.setter
    def item(self, value: int):
        self.item
        peer: Service = Service()
        peer.item
    @item.deleter
    def item(self): self.item
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(10, "example.Service.item")])

    def test_inherited_property_and_property_result_are_not_stored_fields(self):
        self.put("src/example.py", '''\
from typing import Protocol
def encapsulated_only(value): return value
class Service:
    @encapsulated_only
    def run(self): pass
class Base:
    @property
    def payload(self) -> Service: return Service()
class Contract(Protocol):
    @encapsulated_only
    @property
    def guarded(self) -> Service: ...
@encapsulated_only
class Marked(Base):
    @encapsulated_only
    def use(self, peer: Marked):
        self.payload
        peer.payload
        self.payload.run()
class Child(Marked):
    @property
    def fresh(self) -> int: return 1
class Owner:
    def __init__(self, child: Child): self.child = child
    def use(self, peer: Owner, contract: Contract):
        self.child.payload
        peer.child.payload
        self.child.fresh
        contract.guarded
''')
        result, raw, _ = self.run_worker()
        self.assertEqual([(d["line"], d["rule"], d["symbol"]) for d in result["diagnostics"]], [
            (18, "ENCAPSULATED_ONLY_PROPERTY", "example.Base.payload"),
            (19, "ENCAPSULATED_ONLY_CALL", "example.Service.run"),
            (27, "ENCAPSULATED_ONLY_PROPERTY", "example.Base.payload"),
            (29, "ENCAPSULATED_ONLY_PROPERTY", "example.Contract.guarded")])
        self.assertEqual(raw["effective_targets"]["example.Base.payload"], "example.Marked")
        fields, _, _ = self.run_worker("instance-fields")
        self.assertFalse(any(d["symbol"] == "example.Base.payload" for d in fields["diagnostics"]))

    def test_static_descriptor_construction_aliases_shadowing_and_stub_evidence(self):
        self.put("src/example.py", '''\
from functools import cached_property as cached
from builtins import property as descriptor
def encapsulated_only(value): return value
class Service:
    @encapsulated_only
    def get(self) -> int: return 1
    def set(self, value: int): pass
    item = descriptor(get, set)
    result = cached(get)
Service().item
Service().result
def property(value): return value
class Fake:
    @encapsulated_only
    def get(self) -> int: return 1
    item = property(get)
Fake().item
''')
        self.put("src/example.pyi", '''\
class Service:
    @property
    def item(self) -> int: ...
''')
        result, raw, _ = self.run_worker()
        self.assertIn(("src/example.pyi", 3, "example.Service.item"),
                      {(d["path"], d["line"], d["symbol"]) for d in result["diagnostics"]
                       if d["rule"] == "ENCAPSULATED_ONLY_DECLARATION"})
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]
                          if d["rule"] == "ENCAPSULATED_ONLY_PROPERTY"],
                         [(10, "example.Service.item"), (11, "example.Service.result")])
        self.assertEqual([(d["path"], d["accessor"], d["marked"]) for d in raw["declarations"]
                          if d["member"] == "item" and "kind" in d],
                         [("src/example.py", "constructor", False),
                          ("src/example.py", "getter", True),
                          ("src/example.py", "setter", False),
                          ("src/example.pyi", "getter", False)])

    def test_property_super_stored_receiver_and_reflection_aliases(self):
        self.put("src/example.py", '''\
import builtins as bi
def encapsulated_only(value): return value
class Service:
    @encapsulated_only
    @property
    def item(self) -> int: return 1
class Child(Service):
    def work(self, peer: Child):
        super().item
        super(Child, self).item
        super(Child, peer).item
        bi.getattr(super(), "item")
class Owner:
    def __init__(self, service: Service): self.service = service
    def work(self, peer: Owner):
        self.service.item
        bi.getattr(self.service, "item")
        bi.setattr(self.service, "item", 2)
        bi.delattr(self.service, "item")
        peer.service.item
        obj = self.service
        obj.item
        read = bi.getattr
        read(peer.service, "item")
        read = lambda obj, key: None
        read(peer.service, "item")
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["rule"]) for d in result["diagnostics"]], [
            (11, "ENCAPSULATED_ONLY_PROPERTY"),
            (20, "ENCAPSULATED_ONLY_PROPERTY"),
            (22, "ENCAPSULATED_ONLY_PROPERTY"),
            (24, "ENCAPSULATED_ONLY_PROPERTY")])

    def test_property_diagnostics_share_full_report_with_independent_violations(self):
        from eryx._common import capture
        from eryx.check import run_checks
        from offline_fixtures import create_repo, fake_tools

        with TemporaryDirectory(prefix="eryx-property-report-", dir=self.root) as directory:
            root = Path(directory)
            create_repo(root)
            (root / "src/demo/domain/api.py").write_text('''\
def encapsulated_only(value): return value
class Service:
    @encapsulated_only
    @property
    def item(self) -> int: return 1
Service().item
''', encoding="utf-8")
            fake, _ = fake_tools()

            def mixed(argv, cwd, timeout):
                if any(str(ERYX / name) in argv for name in ("_encapsulated_only_worker.py",
                                                              "_class_contracts_worker.py")):
                    return capture(argv, cwd, timeout)
                return fake(argv, cwd, timeout)

            report = run_checks(root, lcom_files="", capture_fn=mixed,
                                versions_fn=lambda _: ({}, []))
            validate(report, json.loads((ERYX / "report.schema.json").read_text(encoding="utf-8")))
            self.assertEqual((report["status"], report["exit_code"]), ("violations", 1))
            self.assertIn("ENCAPSULATED_ONLY_PROPERTY", {d["rule"] for d in report["diagnostics"]})
            self.assertIn("MISSING_CLASS_CONTRACT", {d["rule"] for d in report["diagnostics"]})

    def test_callable_extraction_reflection_and_unbound_invocation(self):
        self.assert_calls('''\
import builtins as bi
def encapsulated_only(value): return value
class Service:
    @encapsulated_only
    def run(self): pass
    def use(self, peer: Service):
        ref = self.run
        ref()  # forbidden
        other = peer.run
        other()  # forbidden
        self.run()
        getattr(self, "run")()
        g = getattr
        g(peer, "run")()  # forbidden
        bi.getattr(peer, "run")()  # forbidden
        r = g(self, "run")
        r()  # forbidden
        Service.run(self)  # forbidden
        bi.getattr(Service, "run")(self)  # forbidden
        getattr(self, "unknown")()
        getattr(self, "r" + "un")()
    def shadow(self, peer: Service):
        def getattr(obj, name): return lambda: None
        getattr(peer, "run")()
class Child(Service):
    def use(self):
        super().run()
        ref = super().run
        ref()  # forbidden
        def nested():
            super(Child, self).run()
            unbound = super(Child, self).run
            unbound()  # forbidden
''', [8, 10, 14, 15, 17, 18, 19, 29, 33])

    def test_constructor_hooks_use_fresh_receiver_and_type_dispatch(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
@encapsulated_only
class Ordinary:
    def __new__(cls): return super().__new__(cls)
    def __init__(self): pass
    def __call__(self): pass
    def __getitem__(self, key): return 1
    def __getattribute__(self, name): return object.__getattribute__(self, name)
    def __getattr__(self, name): return 1
class Guarded:
    @encapsulated_only
    def __new__(cls): return super().__new__(cls)
    @encapsulated_only
    def __init__(self): pass
Alias = Guarded
Ordinary()
Ordinary()()
Ordinary()[0]
Ordinary().anything
Alias()
class Owner:
    def make(self): Guarded()
def uncertain(factory): factory()
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["column"], d["symbol"]) for d in result["diagnostics"]], [
            (20, 1, "example.Guarded.__new__"), (20, 1, "example.Guarded.__init__"),
            (22, 21, "example.Guarded.__new__"), (22, 21, "example.Guarded.__init__")])

    def test_callable_and_subscription_protocols_keep_receiver_origin(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Service:
    @encapsulated_only
    def __call__(self) -> Service: return self
    @encapsulated_only
    def __getitem__(self, key) -> Service: return self
    @encapsulated_only
    def __setitem__(self, key, value): pass
    @encapsulated_only
    def __delitem__(self, key): pass
    @property
    def result(self) -> Service: return self
class Owner:
    def __init__(self, service: Service): self.service = service
    def run(self, peer: Service, unknown, flag: bool):
        self.service()
        self.service[0]
        self.service[0] = 1
        del self.service[0]
        peer()
        peer[0]
        peer[0] = 1
        del peer[0]
        Service()()
        Service()[0]
        self.service[0]()
        self.service()()
        peer.result()
        maybe = peer if flag else unknown
        maybe[0]
        unknown[0]
        peer.__call__ = lambda: None
        peer()
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (20, "example.Service.__call__"), (21, "example.Service.__getitem__"),
            (22, "example.Service.__setitem__"), (23, "example.Service.__delitem__"),
            (24, "example.Service.__call__"), (25, "example.Service.__getitem__"),
            (26, "example.Service.__call__"), (27, "example.Service.__call__"),
            (28, "example.Service.__call__"), (30, "example.Service.__getitem__"),
            (33, "example.Service.__call__")])

    def test_operator_receiver_precedence_and_notimplemented_fallbacks(self):
        self.maxDiff = None
        source = '''\
def encapsulated_only(value): return value
class Base:
    @encapsulated_only
    def __add__(self, other): return NotImplemented
    @encapsulated_only
    def __iadd__(self, other): return NotImplemented
    @encapsulated_only
    def __neg__(self): return 1
class Right:
    @encapsulated_only
    def __radd__(self, other): return 1
class Child(Base):
    @encapsulated_only
    def __radd__(self, other): return 1
class Wins:
    def __add__(self, other): return 1
class Same:
    @encapsulated_only
    def __add__(self, other): return NotImplemented
    @encapsulated_only
    def __radd__(self, other): return 1
class Owner:
    def __init__(self, base: Base, right: Right):
        self.base, self.right = base, right
    def run(self, peer: Base, other: Right, unknown, child: Child, flag):
        -self.base
        -peer
        self.base + other
        peer + self.right
        self.base + self.right
        peer + unknown
        unknown + other
        peer += other
        self.base += other
        self.base + child
        Wins() + other
        Same() + Same()
'''
        self.put("src/example.py", source)
        result, raw, _ = self.run_worker()
        self.assertIsNone(raw["execution_error"])
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (27, "example.Base.__neg__"),
            (28, "example.Right.__radd__"),
            (29, "example.Base.__add__"),
            (31, "example.Base.__add__"),
            (33, "example.Base.__iadd__"), (33, "example.Base.__add__"),
            (33, "example.Right.__radd__"),
            (34, "example.Right.__radd__"),
            (35, "example.Child.__radd__"),
            (37, "example.Same.__add__")])
        self.assertTrue(all(d["column"] is not None and not d["is_infrastructure_error"]
                            for d in result["diagnostics"]))

    def test_truth_membership_comparisons_and_independent_origins(self):
        self.maxDiff = None
        source = '''\
def encapsulated_only(value): return value
class Truth:
    @encapsulated_only
    def __bool__(self): return True
    @encapsulated_only
    def __len__(self): return 1
class Length:
    @encapsulated_only
    def __len__(self): return 1
class Cursor:
    @encapsulated_only
    def __next__(self): return 1
class Iterable:
    def __iter__(self) -> Cursor: return Cursor()
class Sequence:
    @encapsulated_only
    def __getitem__(self, index): return 1
class Contains:
    @encapsulated_only
    def __contains__(self, item) -> Truth: return Truth()
    def __iter__(self) -> Cursor: return Cursor()
class Compared:
    @encapsulated_only
    def __lt__(self, other): return NotImplemented
    @encapsulated_only
    def __gt__(self, other): return True
class Owner:
    def __init__(self, value: Truth, container: Contains, compared: Compared):
        self.value, self.container, self.compared = value, container, compared
    def run(self, peer: Truth, collection: Contains, first: Compared, second: Compared,
            iterable: Iterable, sequence: Sequence, unknown, choice):
        if self.value: pass
        if peer: pass
        if Length(): pass
        if not peer: pass
        assert peer
        peer and self.value
        peer or unknown
        bool(peer)
        1 in self.container
        1 not in collection
        1 in iterable
        1 in sequence
        first < second
        self.compared < second
        first > self.compared
        second == unknown
        (peer if choice else unknown) and self.value
'''
        self.put("src/example.py", source)
        result, raw, _ = self.run_worker(selected=[])
        self.assertIsNone(raw["execution_error"])
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (33, "example.Truth.__bool__"), (34, "example.Length.__len__"),
            (35, "example.Truth.__bool__"), (36, "example.Truth.__bool__"),
            (37, "example.Truth.__bool__"), (38, "example.Truth.__bool__"),
            (39, "example.Truth.__bool__"), (40, "example.Truth.__bool__"),
            (41, "example.Contains.__contains__"), (41, "example.Truth.__bool__"),
            (42, "example.Cursor.__next__"),
            (43, "example.Sequence.__getitem__"),
            (44, "example.Compared.__lt__"),
            (46, "example.Compared.__gt__"),
            (48, "example.Truth.__bool__")])

    def test_operator_results_subtype_dispatch_and_conditional_evidence(self):
        self.maxDiff = None
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Truth:
    @encapsulated_only
    def __bool__(self): return True
class Base:
    @encapsulated_only
    def __add__(self, other) -> Truth: return Truth()
    @encapsulated_only
    def __iadd__(self, other): return 1
    @encapsulated_only
    def __lt__(self, other) -> Truth: return Truth()
class Sub(Base):
    @encapsulated_only
    def __radd__(self, other): return 1
    @encapsulated_only
    def __gt__(self, other): return 1
class Maybe:
    @encapsulated_only
    def __add__(self, other):
        if other: return NotImplemented
        return 1
class Right:
    @encapsulated_only
    def __radd__(self, other): return 1
class Owner:
    def __init__(self, base: Base, sub: Sub):
        self.base, self.sub = base, sub
    def use(self, peer: Base, other: Sub, right: Right, unknown, flag):
        peer + other
        self.base + other
        peer + self.sub
        if self.base + peer: pass
        if peer + self.base: pass
        if self.base < peer: pass
        peer < other
        maybe = peer if flag else unknown
        if maybe + right: pass
        Maybe() + right
        other + unknown
        peer += right
        (self.base if flag else unknown) + unknown
        (self.base if flag else other) + unknown
''')
        result, raw, _ = self.run_worker()
        self.assertIsNone(raw["execution_error"])
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (29, "example.Sub.__radd__"), (30, "example.Sub.__radd__"),
            (32, "example.Truth.__bool__"),
            (33, "example.Base.__add__"), (33, "example.Truth.__bool__"),
            (34, "example.Truth.__bool__"),
            (35, "example.Sub.__gt__"),
            (37, "example.Base.__add__"), (37, "example.Right.__radd__"),
            (37, "example.Truth.__bool__"),
            (38, "example.Maybe.__add__"), (38, "example.Right.__radd__"),
            (39, "example.Base.__add__"), (40, "example.Base.__iadd__"),
            (42, "example.Base.__add__")])

    def test_magic_marking_and_proven_fallback_exclusion(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
@encapsulated_only
class Ordinary:
    def __add__(self, other): return 1
    def __bool__(self): return True
class Explicit:
    @encapsulated_only
    def __add__(self, other): return 1
class NoFallback:
    @property
    def __bool__(self): return None
    @encapsulated_only
    def __len__(self): return 1
class NoMembershipFallback:
    @property
    def __contains__(self): return None
    @encapsulated_only
    def __getitem__(self, index): return 1
def use(ordinary: Ordinary, explicit: Explicit, truth: NoFallback,
        container: NoMembershipFallback, unknown):
    ordinary + ordinary
    if ordinary: pass
    explicit + unknown
    if truth: pass
    1 in container
''')
        result, raw, _ = self.run_worker()
        self.assertIsNone(raw["execution_error"])
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (23, "example.Explicit.__add__")])

    def test_membership_element_comparison_and_augmented_subscription(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Element:
    @encapsulated_only
    def __eq__(self, other): return True
    @encapsulated_only
    def __iadd__(self, other): return 1
class Cursor:
    def __next__(self) -> Element: return Element()
class Iterated:
    def __iter__(self) -> Cursor: return Cursor()
class Indexed:
    @encapsulated_only
    def __getitem__(self, index) -> Element: return Element()
    @encapsulated_only
    def __setitem__(self, index, value): pass
class Owner:
    def __init__(self, indexed: Indexed): self.indexed = indexed
    def run(self, iterated: Iterated, indexed: Indexed, unknown):
        unknown in iterated
        unknown in indexed
        self.indexed[0] += 1
        indexed[0] += 1
''')
        result, raw, _ = self.run_worker()
        self.assertIsNone(raw["execution_error"])
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (19, "example.Element.__eq__"),
            (20, "example.Indexed.__getitem__"), (20, "example.Element.__eq__"),
            (21, "example.Element.__iadd__"),
            (22, "example.Indexed.__getitem__"), (22, "example.Element.__iadd__"),
            (22, "example.Indexed.__setitem__")])

    def test_default_inequality_delegates_to_marked_equality(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Truth:
    @encapsulated_only
    def __bool__(self): return True
class Eq:
    @encapsulated_only
    def __eq__(self, other) -> Truth: return Truth()
class Ne(Eq):
    def __ne__(self, other): return False
def use(peer: Eq, overridden: Ne, unknown):
    peer != unknown
    overridden != unknown
    if peer != unknown: pass
''')
        result, raw, _ = self.run_worker()
        self.assertIsNone(raw["execution_error"])
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (11, "example.Eq.__eq__"), (11, "example.Truth.__bool__"),
            (13, "example.Eq.__eq__"), (13, "example.Truth.__bool__")])

    def test_context_protocols_use_manager_not_bound_result(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Manager:
    @encapsulated_only
    def __enter__(self) -> Manager: return self
    @encapsulated_only
    def __exit__(self, *args): pass
    @encapsulated_only
    async def __aenter__(self) -> Manager: return self
    @encapsulated_only
    async def __aexit__(self, *args): pass
class Owner:
    def __init__(self, manager: Manager): self.manager = manager
    async def work(self, peer: Manager, unknown, choice):
        with self.manager as bound: pass
        with peer as bound: pass
        with Manager() as bound: pass
        async with self.manager as bound: pass
        async with peer as bound: pass
        with (peer if choice else unknown): pass
        with unknown: pass
        def nested():
            with self.manager: pass
            with peer: pass
        self = peer
        with self: pass
''')
        result, raw, _ = self.run_worker()
        self.assertEqual(raw["execution_error"], None)
        self.assertEqual([(d["line"], d["column"], d["symbol"]) for d in result["diagnostics"]], [
            (15, 14, "example.Manager.__enter__"), (15, 14, "example.Manager.__exit__"),
            (16, 14, "example.Manager.__enter__"), (16, 14, "example.Manager.__exit__"),
            (18, 20, "example.Manager.__aenter__"), (18, 20, "example.Manager.__aexit__"),
            (19, 15, "example.Manager.__enter__"), (19, 15, "example.Manager.__exit__"),
            (23, 18, "example.Manager.__enter__"), (23, 18, "example.Manager.__exit__"),
            (25, 14, "example.Manager.__enter__"), (25, 14, "example.Manager.__exit__")])

    def test_iteration_comprehensions_generated_iterators_and_await(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Cursor:
    def __iter__(self): return self
    def __aiter__(self): return self
    @encapsulated_only
    def __next__(self) -> int: return 1
    @encapsulated_only
    async def __anext__(self) -> int: return 1
class Stream:
    @encapsulated_only
    def __iter__(self) -> Cursor: return Cursor()
    @encapsulated_only
    def __aiter__(self) -> Cursor: return Cursor()
    @encapsulated_only
    def __await__(self) -> Cursor: return Cursor()
class Owner:
    def __init__(self, stream: Stream): self.stream = stream
    async def work(self, peer: Stream, unknown, choice):
        for x in self.stream: pass
        for x in peer: pass
        async for x in self.stream: pass
        async for x in peer: pass
        [x for x in peer]
        (x for x in peer)
        [y for x in self.stream for y in peer]
        [x async for x in peer]
        await self.stream
        await peer
        await (peer if choice else unknown)
        await unknown
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (19, "example.Cursor.__next__"),
            (20, "example.Stream.__iter__"), (20, "example.Cursor.__next__"),
            (21, "example.Cursor.__anext__"),
            (22, "example.Stream.__aiter__"), (22, "example.Cursor.__anext__"),
            (23, "example.Stream.__iter__"), (23, "example.Cursor.__next__"),
            (24, "example.Stream.__iter__"), (24, "example.Cursor.__next__"),
            (25, "example.Cursor.__next__"), (25, "example.Stream.__iter__"),
            (25, "example.Cursor.__next__"),
            (26, "example.Stream.__aiter__"), (26, "example.Cursor.__anext__"),
            (27, "example.Cursor.__next__"),
            (28, "example.Stream.__await__"), (28, "example.Cursor.__next__"),
            (29, "example.Stream.__await__"), (29, "example.Cursor.__next__")])

    def test_protocol_origins_super_shadowing_and_unannotated_intermediate(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Cursor:
    @encapsulated_only
    def __next__(self): return 1
class Base:
    @encapsulated_only
    def __iter__(self): return Cursor()
class Child(Base):
    def work(self, peer: Child, unknown, choice):
        for item in self: pass
        for item in peer: pass
        me = self
        [item for item in me for me in (peer,)]
        for item in me: pass
        me = peer
        for item in me: pass
        maybe = peer if choice else unknown
        for item in maybe: pass
        for item in unknown: pass
class Override(Base):
    def __iter__(self): return Cursor()
for item in Override(): pass
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (10, "example.Cursor.__next__"),
            (11, "example.Base.__iter__"), (11, "example.Cursor.__next__"),
            (13, "example.Cursor.__next__"),
            (14, "example.Cursor.__next__"),
            (16, "example.Base.__iter__"), (16, "example.Cursor.__next__"),
            (18, "example.Base.__iter__"), (18, "example.Cursor.__next__"),
            (22, "example.Cursor.__next__")])

    def test_unmarked_acquisition_stubs_and_awaitable_results(self):
        self.put("src/marker.py", "def encapsulated_only(value): return value\n")
        self.put("src/protocols.pyi", '''\
from marker import encapsulated_only as mark
class Cursor:
    @mark
    def __next__(self) -> int: ...
class Stream:
    def __iter__(self) -> Cursor: ...
class Task:
    @mark
    def __await__(self) -> Cursor: ...
class Factory:
    def task(self) -> Task: ...
''')
        self.put("src/example.py", '''\
from protocols import Stream, Task, Factory
def work(stream: Stream, task: Task, factory: Factory, unknown):
    for value in stream: pass
    await task
    await factory.task()
    for value in unknown: pass
''')
        result, raw, _ = self.run_worker()
        self.assertEqual(raw["execution_error"], None)
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]
                          if d["path"] == "src/example.py"], [
            (3, "protocols.Cursor.__next__"),
            (4, "protocols.Task.__await__"), (4, "protocols.Cursor.__next__"),
            (5, "protocols.Task.__await__"), (5, "protocols.Cursor.__next__")])

    def test_context_bound_and_iterated_values_retain_type_not_permission(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Manager:
    @encapsulated_only
    def __enter__(self) -> Manager: return self
    @encapsulated_only
    def __exit__(self, *args): pass
class Cursor:
    def __next__(self) -> Manager: return Manager()
class Collection:
    def __iter__(self) -> Cursor: return Cursor()
class Owner:
    def __init__(self, manager: Manager, items: Collection):
        self.manager, self.items = manager, items
    def work(self):
        with self.manager as bound:
            with bound: pass
        for bound in self.items:
            with bound: pass
        [bound.__enter__() for bound in self.items]
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (16, "example.Manager.__enter__"), (16, "example.Manager.__exit__"),
            (18, "example.Manager.__enter__"), (18, "example.Manager.__exit__"),
            (19, "example.Manager.__enter__")])

    def test_proven_self_iterator_keeps_receiver_boundary(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Sequence:
    def __iter__(this): return this
    @encapsulated_only
    def __next__(this): return 1
    def work(this, peer: Sequence, unknown):
        for item in this: pass
        for item in peer: pass
        for item in unknown: pass
        alias = this
        for item in alias: pass
        alias = peer
        for item in alias: pass
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (8, "example.Sequence.__next__"), (13, "example.Sequence.__next__")])

    def test_attribute_hooks_default_lookup_and_literal_reflection(self):
        self.put("src/example.py", '''\
import builtins as bi
def encapsulated_only(value): return value
class Service:
    @encapsulated_only
    def __getattribute__(self, name): return object.__getattribute__(self, name)
    @encapsulated_only
    def __getattr__(self, name): return 1
    @encapsulated_only
    def __setattr__(self, name, value): pass
    @encapsulated_only
    def __delattr__(self, name): pass
    def known(self): pass
class Fallback:
    @encapsulated_only
    def __getattr__(self, name): return 1
    def known(self): pass
def use(peer: Service, fallback: Fallback, unknown):
    peer.known()
    peer.missing
    peer.field = 1
    del peer.field
    bi.getattr(peer, "missing")
    g = bi.getattr
    g(peer, "known")
    bi.setattr(peer, "field", 1)
    bi.delattr(peer, "field")
    fallback.known()
    fallback.missing
    unknown.missing
    def getattr(obj, key): return 1
    getattr(peer, "missing")
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (18, "example.Service.__getattr__"), (18, "example.Service.__getattribute__"),
            (19, "example.Service.__getattr__"), (19, "example.Service.__getattribute__"),
            (20, "example.Service.__setattr__"), (21, "example.Service.__delattr__"),
            (22, "example.Service.__getattr__"), (22, "example.Service.__getattribute__"),
            (24, "example.Service.__getattr__"), (24, "example.Service.__getattribute__"),
            (25, "example.Service.__setattr__"), (26, "example.Service.__delattr__"),
            (28, "example.Fallback.__getattr__")])

    def test_protocol_lookup_uses_type_over_instance_shadow_and_class_override(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Base:
    @encapsulated_only
    def __call__(self): pass
    @encapsulated_only
    def __getitem__(self, key): return 1
class Child(Base):
    def __call__(self): pass
    def __getitem__(self, key): return 2
class Other(Base): pass
def use(child: Child, other: Other, unknown):
    child()
    child[0]
    other.__call__ = lambda: None
    other()
    other[0]
    unknown()
    unknown[0]
class Fallback:
    @encapsulated_only
    def __getattr__(self, name): return 1
    def __getattribute__(self, name): return object.__getattribute__(self, name)
    def known(self): pass
def lookup(obj: Fallback):
    obj.known
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (15, "example.Base.__call__"), (16, "example.Base.__getitem__"),
            (25, "example.Fallback.__getattr__")])

    def test_stored_field_extraction_and_getattr_alias_reassignment(self):
        self.assert_calls('''\
import builtins as bi
def encapsulated_only(value): return value
class Service:
    @encapsulated_only
    def run(self): pass
class Owner:
    def __init__(self, service: Service): self.service = service
    def work(self):
        direct = self.service.run
        direct()  # forbidden
        indirect = bi.getattr(self.service, "run")
        indirect()  # forbidden
        bi.getattr(self.service, "run")()
        Service.run(self.service)  # forbidden
        g = bi.getattr
        g(self.service, "run")()
        g = lambda obj, name: lambda: None
        g(Service(), "run")()
''', [10, 12, 14])

    def test_receiver_aliases_stored_fields_mixed_targets_and_nested_scopes(self):
        self.assert_calls('''\
def encapsulated_only(value): return value
class Service:
    @encapsulated_only
    def run(self): pass
class Plain:
    def run(self): pass
class Owner:
    def __init__(this, service: Service): this.service = service
    async def work(this, peer: Owner, choice: bool, unknown):
        me = this
        me.service.run()
        dep = me.service
        dep.run()  # forbidden
        if choice:
            selected = this
        else:
            selected = peer
        selected.service.run()  # forbidden
        target = Service() if choice else Plain()
        target.run()  # forbidden
        maybe = unknown if choice else Service()
        maybe.run()  # forbidden
        def nested():
            me.service.run()
            def deeper(): this.service.run()
            deeper()
        def shadow(this: Owner):
            this.service.run()  # forbidden
        class Inner:
            def work(inner):
                inner.service.run()
                me.service.run()  # forbidden
        me = peer
        me.service.run()  # forbidden
        this = peer
        this.service.run()  # forbidden
''', [13, 18, 20, 22, 28, 32, 34, 36])

    def test_alternative_control_flow_invalidates_permission_and_retains_targets(self):
        self.assert_calls('''\
def encapsulated_only(value): return value
class Service:
    @encapsulated_only
    def run(self): pass
    def work(self, peer: Service, choice: bool):
        alias = self
        try:
            if choice: alias = peer
        except ValueError:
            alias = self
        alias.run()  # forbidden
        for item in (peer,):
            alias = peer
        alias.run()  # forbidden
        with open("file") as alias:
            alias.run()
        match choice:
            case True: maybe = peer
            case False: maybe = self
        maybe.run()  # forbidden
        if choice:
            fn = self.run
        else:
            fn = lambda: None
        fn()  # forbidden
''', [11, 14, 20, 25])

    def test_explicit_calls_use_receiver_identity_and_stored_field_shape(self):
        self.put("src/example.py", '''\
def encapsulated_only[T](value: T) -> T:
    return value
class Service(object):
    @encapsulated_only
    def run(this): pass
    def own(this, peer: Service):
        this.run()
        peer.run()
        Service().run()
class Owner(object):
    def __init__(this, service: Service):
        this.service = service
    def work(this, peer: Owner):
        this.service.run()
        peer.service.run()
Owner(Service()).service.run()
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["line"], d["column"], d["symbol"]) for d in result["diagnostics"]],
                         [(8, 9, "example.Service.run"), (9, 9, "example.Service.run"),
                          (15, 9, "example.Service.run"), (16, 1, "example.Service.run")])
        self.assertTrue(all(d["rule"] == "ENCAPSULATED_ONLY_CALL" and
                            not d["is_infrastructure_error"] for d in result["diagnostics"]))

    def test_slots_dataclasses_properties_chains_and_builtin_super(self):
        self.put("src/example.py", '''\
from dataclasses import dataclass
import builtins as bi
def encapsulated_only[T](value: T) -> T: return value
class Service(object):
    @encapsulated_only
    def run(this): pass
class Slotted(object):
    __slots__ = ("service",)
    service: Service
    def work(this): this.service.run()
@dataclass
class Data(object):
    service: Service
    def work(this): this.service.run()
class Owner(object):
    shared = Service()
    def __init__(this, service: Service, data: Data):
        this.service = service
        this.data = data
    @property
    def exposed(this) -> Service: return this.service
    def work(this):
        this.exposed.run()
        this.shared.run()
        this.data.service.run()
        this.service.run()
class Child(Service):
    def work(this, peer: Child):
        super().run()
        bi.super(Child, this).run()
        super(Child, peer).run()
def unknown(value):
    value.run()
''')
        result, _, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual([d["line"] for d in result["diagnostics"]], [23, 24, 25, 31])

    def test_imports_reexports_stubs_and_scope_independent_of_lcom_selection(self):
        self.put("src/pkg/marker.py", "def encapsulated_only[T](value: T) -> T: return value\n")
        self.put("src/pkg/__init__.py", "from .marker import encapsulated_only as restricted\n"
                 "from .service import Service as Public\n")
        self.put("src/pkg/relay.py", "from . import restricted as marker\n")
        self.put("src/pkg/service.pyi", '''\
from .relay import marker as boundary
class Service(object):
    @boundary
    def run(this) -> None: ...
''')
        self.put("src/pkg/usage.py", '''\
from pkg import Public
Alias = Public
Alias().run()
class Owner(object):
    def __init__(this, service: Public): this.service = service
    def work(this, peer: Owner):
        this.service.run()
        peer.service.run()
''')
        self.put("src/pkg/other.pyi", '''\
import pkg.relay as markers
class Other(object):
    @markers.marker
    def run(this) -> None: ...
def use(value: Other) -> None:
    value.run()
''')
        self.put("tests/test_usage.py", "from pkg.service import Service\nService().run()\n")
        self.put("src/generated/ignored.py", "from pkg.service import Service\nService().run()\n")
        for selected in (None, [], ["src/pkg/usage.py"]):
            with self.subTest(selected=selected):
                result, raw, _ = self.run_worker(selected=selected)
                self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
                self.assertEqual({(d["path"], d["symbol"]) for d in result["diagnostics"]}, {
                    ("src/pkg/usage.py", "pkg.service.Service.run"),
                    ("src/pkg/other.pyi", "pkg.other.Other.run"),
                    ("tests/test_usage.py", "pkg.service.Service.run")})
                self.assertEqual([d["line"] for d in result["diagnostics"]
                                  if d["path"] == "src/pkg/usage.py"], [3, 8])

    def test_identity_marker_preserves_existing_method_and_field_diagnostics(self):
        self.put("src/marker.py", "def encapsulated_only[T](value: T) -> T: return value\n")
        self.put("src/example.py", '''\
from marker import encapsulated_only as mark
@mark
class Service(object):
    @mark
    def missing(): pass
    @mark
    def run(this): pass
    def __init__(this): this.value = 1
    def broken(this):
        local = Service()
        local.value
Service().value
Service.run = staticmethod(Service.run)
''')
        methods, _, _ = self.run_worker("method-decorators")
        self.assertEqual([(d["rule"], d["symbol"]) for d in methods["diagnostics"]], [
            ("MISSING_INSTANCE_RECEIVER", "Service.missing"),
            ("NON_INSTANCE_METHOD", "Service.run")])
        fields, _, _ = self.run_worker("instance-fields")
        self.assertEqual([(d["rule"], d["symbol"]) for d in fields["diagnostics"]],
                         [("EXTERNAL_INSTANCE_FIELD", "example.Service.value"),
                          ("NO_CLASS_STATE", "example.Service.run")])

    def test_shadowed_reassigned_and_unrelated_marker_names_do_not_restrict(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
mark = encapsulated_only
class Real(object):
    @mark
    def run(this): pass
Real().run()
mark = lambda value: value
class Unmarked(object):
    @mark
    def run(this): pass
Unmarked().run()
def outer(encapsulated_only):
    class Local(object):
        @encapsulated_only
        def run(this): pass
    Local().run()
def forward():
    class Local(object):
        @encapsulated_only
        def run(this): pass
    encapsulated_only = lambda value: value
    Local().run()
def encapsulated_only(value): return object()
class Replaced(object):
    @encapsulated_only
    def run(this): pass
Replaced().run()
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(6, "example.Real.run")])

    def test_valid_clean_output_passes_and_source_stub_evidence_is_separate(self):
        self.put("src/example.py", '''\
def encapsulated_only[T](value: T) -> T: return value
class Service(object):
    @encapsulated_only
    def run(this): pass
    def work(this): this.run()
''')
        self.put("src/example.pyi", '''\
def encapsulated_only[T](value: T) -> T: ...
class Service(object):
    @encapsulated_only
    def run(this) -> None: ...
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], result["error"]), ("passed", 0, None))
        self.assertEqual([(d["path"], d["marked"]) for d in raw["declarations"]
                          if d["member"] == "run"], [("src/example.py", True), ("src/example.pyi", True)])

    def test_class_marking_inherited_methods_and_magic_exception(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Base:
    def run(self): pass
    def __custom__(self): pass
@encapsulated_only
class Marked(Base):
    @encapsulated_only
    def __explicit__(self): pass
    def extra(self): pass
class Child(Marked):
    def fresh(self): pass
Marked().run()
Child().run()
Child().extra()
Child().fresh()
Child().__custom__()
Child().__explicit__()
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(12, "example.Base.run"), (13, "example.Base.run"),
                          (14, "example.Marked.extra"), (17, "example.Marked.__explicit__")])
        self.assertEqual(raw["effective_targets"]["example.Base.run"], "example.Marked")

    def test_c3_dispatch_alias_reexports_nested_and_stub_ancestors(self):
        self.put("src/pkg/marker.py", "def encapsulated_only(value): return value\n")
        self.put("src/pkg/__init__.py", "from .marker import encapsulated_only as mark\n"
                 "from .base import Base as Exported\n")
        self.put("src/pkg/base.pyi", '''\
class Base:
    def inherited(self) -> None: ...
''')
        self.put("src/pkg/use.py", '''\
from pkg import mark, Exported as Parent
class Other:
    def inherited(self): pass
@mark
class Middle(Parent): pass
class First(Other, Middle): pass
class Second(Middle, Other): pass
class Child(Second): pass
class Container:
    @mark
    class Nested(Parent): pass
First().inherited()
Second().inherited()
Child().inherited()
Container.Nested().inherited()
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(13, "pkg.base.Base.inherited"), (14, "pkg.base.Base.inherited"),
                          (15, "pkg.base.Base.inherited")])
        self.assertIn(raw["effective_targets"]["pkg.base.Base.inherited"],
                      {"pkg.use.Middle", "pkg.use.Container.Nested"})

    def test_explicit_contracts_and_protocol_annotations_without_structural_marking(self):
        self.put("src/example.py", '''\
from typing import Protocol
def encapsulated_only(value): return value
class Contract(Protocol):
    @encapsulated_only
    def run(self): ...
class AbstractContract(Protocol):
    @encapsulated_only
    def other(self): ...
class Implements(Contract, AbstractContract):
    def run(self): pass
    def other(self): pass
class Resembles:
    def run(self): pass
def calls(contract: Contract, obj: Implements, lookalike: Resembles):
    contract.run()
    obj.run()
    obj.other()
    lookalike.run()
''')
        result, raw, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]
                          if d["rule"] == "ENCAPSULATED_ONLY_CALL"],
                         [(15, "example.Contract.run"), (16, "example.Implements.run"),
                          (17, "example.Implements.other")])
        self.assertEqual(raw["effective_targets"]["example.Implements.run"],
                         "example.Contract.run")

    def test_class_marker_alias_shadowing_and_source_stub_ownership(self):
        self.put("src/marker.py", "def encapsulated_only(value): return value\n")
        self.put("src/base.py", "class Base:\n    def run(self): pass\n")
        self.put("src/base.pyi", "class Base:\n    def stub_only(self) -> None: ...\n")
        self.put("src/use.py", '''\
from marker import encapsulated_only as mark
from base import Base as Parent
alias = mark
@alias
class Marked(Parent): pass
Marked().run()
Marked().stub_only()
alias = lambda value: value
@alias
class Unmarked(Parent): pass
Unmarked().run()
Parent = lambda: object()
@mark
class Unknown(Parent): pass
Unknown().run()
def local(mark):
    @mark
    class Shadow(Parent): pass
    Shadow().run()
''')
        result, raw, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(6, "base.Base.run")])
        self.assertEqual(raw["effective_targets"], {"base.Base.run": "use.Marked"})

    def test_inherited_permission_does_not_suppress_override_or_field_boundaries(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Base:
    def __init__(self): self.value = 1
    def run(self): pass
@encapsulated_only
class Marked(Base): pass
class Child(Marked):
    def run(self): pass
    def work(self):
        self.value
        self.run()
Child().run()
''')
        restricted, _, _ = self.run_worker()
        self.assertEqual(restricted["diagnostics"], [])
        contracts, _, _ = self.run_worker("class-contracts")
        self.assertIn("CUSTOMER_METHOD_OVERRIDE", {d["rule"] for d in contracts["diagnostics"]})
        fields, _, _ = self.run_worker("instance-fields")
        self.assertIn("EXTERNAL_INSTANCE_FIELD", {d["rule"] for d in fields["diagnostics"]})

    def test_marked_abstract_contract_constrains_implementation(self):
        self.put("src/example.py", '''\
from abc import ABC, abstractmethod
def encapsulated_only(value): return value
class Interface(ABC):
    @encapsulated_only
    @abstractmethod
    def run(self): ...
class Concrete(Interface):
    def run(self): pass
Concrete().run()
''')
        result, raw, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]
                          if d["rule"] == "ENCAPSULATED_ONLY_CALL"],
                         [(9, "example.Concrete.run")])
        self.assertEqual(raw["effective_targets"]["example.Concrete.run"], "example.Interface.run")

    def test_inherited_methods_keep_the_same_receiver_and_super_boundary(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Base:
    def run(self): pass
@encapsulated_only
class Marked(Base): pass
class Child(Marked):
    def work(this, peer: Child):
        this.run()
        super().run()
        super(Child, this).run()
        super(Child, peer).run()
        peer.run()
Child().run()
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(11, "example.Base.run"), (12, "example.Base.run"),
                          (13, "example.Base.run")])

    def test_calls_in_class_suites_defaults_decorators_and_assignment_targets(self):
        self.put("src/example.py", '''\
def encapsulated_only(value): return value
class Service(object):
    @encapsulated_only
    def run(this): pass
class User(object):
    Service().run()
    def work(this, value=Service().run()): pass
def user(value=Service().run()): pass
@Service().run()
def decorated(): pass
Service().run().value = 1
''')
        result, _, _ = self.run_worker()
        self.assertEqual([d["line"] for d in result["diagnostics"]], [6, 7, 8, 9, 11])

    def test_definition_annotation_calls_and_reimports_of_replaced_markers(self):
        self.put("src/marker.py", "def encapsulated_only(value): return value\n")
        self.put("src/example.py", '''\
import marker
class Service(object):
    @marker.encapsulated_only
    def run(this): pass
def use(value: Service().run()) -> Service().run(): pass
marker.encapsulated_only = lambda value: object()
from marker import encapsulated_only as changed
class Replaced(object):
    @changed
    def run(this): pass
Replaced().run()
''')
        result, _, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
                         [(5, "example.Service.run"), (5, "example.Service.run")])

    def test_class_values_are_forbidden_and_cached_getters_keep_their_receiver(self):
        self.put("src/example.py", '''\
from functools import cached_property as cached
def encapsulated_only(value): return value
class Service(object):
    @encapsulated_only
    def run(this): pass
class Owner(object):
    shared = Service()
    def __init__(this, service: Service): this.service = service
    @cached
    def result(this: Owner):
        this.service.run()
Owner.shared.run()
''')
        result, _, _ = self.run_worker()
        self.assertEqual([d["line"] for d in result["diagnostics"]], [12])

    def test_module_attribute_and_control_flow_shadowing_invalidate_markers(self):
        self.put("src/marker.py", "def encapsulated_only(value): return value\n")
        self.put("src/example.py", '''\
import marker
from marker import encapsulated_only
marker.encapsulated_only = lambda value: object()
class Replaced(object):
    @marker.encapsulated_only
    def run(this): pass
Replaced().run()
for encapsulated_only in [lambda value: value]: pass
class Looped(object):
    @encapsulated_only
    def run(this): pass
Looped().run()
from marker import encapsulated_only as original
with manager() as original: pass
class Managed(object):
    @original
    def run(this): pass
Managed().run()
''')
        result, _, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], result["diagnostics"]), ("passed", 0, []))

    def test_parse_read_and_manifest_errors_preserve_restricted_calls(self):
        self.put("src/good.py", '''\
def encapsulated_only(value): return value
class Service(object):
    @encapsulated_only
    def run(this): pass
Service().run()
''')
        self.put("src/bad.pyi", "class Broken(\n")
        result, raw, job = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "ENCAPSULATED_ONLY_PARSE_ERROR"))
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"ENCAPSULATED_ONLY_CALL", "ENCAPSULATED_ONLY_PARSE_ERROR"})
        (self.root / "src/bad.pyi").unlink()
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 2))
        Path(job["argv"][-1]).unlink()
        result = execute_job(job, self.root, self.run_dir, 30)
        self.assertEqual((result["status"], result["exit_code"], result["error"]),
                         ("error", 2, "ENCAPSULATED_ONLY_EXECUTION_ERROR"))

    def test_malformed_inconsistent_crashing_and_timed_out_reports_fail_closed(self):
        self.put("src/example.py", "value = 1\n")
        _, raw, job = self.run_worker()

        def captured(payload, code=0, error=None, stderr=""):
            return execute_job(job, self.root, self.run_dir, 30, capture_fn=lambda *args: {
                "stdout": payload, "stderr": stderr, "exit_code": code,
                "duration_seconds": 0, "error": error})

        invalid = ["not JSON", json.dumps({**raw, "eryx_encapsulated_only_adapter": True}),
                   json.dumps({**raw, "diagnostics": [{"rule": "ENCAPSULATED_ONLY_CALL",
                               "message": "bad", "path": "src/example.py", "symbol": "example.C.run",
                               "line": True, "column": 0}]}),
                   json.dumps({**raw, "execution_error": "ENCAPSULATED_ONLY_PARSE_ERROR"}),
                   json.dumps({**raw, "declarations": [{}]}),
                   json.dumps({**raw, "classes": [{}]}),
                   json.dumps({**raw, "effective_targets": {"missing.M.run": "missing.M"}}),
                   json.dumps({**raw, "diagnostics": [{"rule": "ENCAPSULATED_ONLY_DECLARATION",
                                "message": "bad", "path": "src/example.py", "symbol": "example.M.run",
                                "line": 1, "column": 1}]}),
                   json.dumps({**raw, "diagnostics": [{"rule": "ENCAPSULATED_ONLY_CALL",
                               "message": "bad", "path": "src/example.py", "symbol": "example.C.run",
                               "line": 1, "column": 1}]})]
        for text in invalid:
            with self.subTest(text=text):
                result = captured(text)
                self.assertEqual((result["status"], result["error"]), ("error", "UNREADABLE_TOOL_OUTPUT"))
        self.assertEqual(captured(json.dumps(raw), 1)["error"], "ENCAPSULATED_ONLY_EXECUTION_ERROR")
        self.assertEqual(captured("", None, "TOOL_TIMEOUT")["error"], "TOOL_TIMEOUT")
        self.assertEqual(captured(json.dumps(raw), 0, stderr="Traceback (most recent call last)")["error"],
                         "TOOL_CRASH")

    def test_full_runner_retains_other_diagnostics_and_validates_report_schema(self):
        from eryx._common import capture
        from eryx.check import run_checks
        from offline_fixtures import create_repo, fake_tools, native_result

        with TemporaryDirectory(prefix="eryx-restricted-runner-", dir=self.root) as directory:
            root = Path(directory)
            create_repo(root)
            (root / "src/demo/domain/api.py").write_text('''\
def encapsulated_only[T](value: T) -> T: return value
class Service(object):
    @encapsulated_only
    def run(this): pass
Service().run()
''', encoding="utf-8")
            fake, _ = fake_tools()

            def mixed(argv, cwd, timeout):
                if str(ERYX / "_encapsulated_only_worker.py") in argv:
                    return capture(argv, cwd, timeout)
                if "pyright" in argv:
                    return native_result(argv, code=1, stdout=json.dumps({"generalDiagnostics": [{
                        "file": "src/demo/domain/api.py", "message": "Independent type error",
                        "rule": "reportArgumentType", "severity": "error",
                        "range": {"start": {"line": 0, "character": 0}}}]}))
                return fake(argv, cwd, timeout)

            for selection in (None, "", "src/demo/domain/api.py"):
                report = run_checks(root, lcom_files=selection, capture_fn=mixed,
                                    versions_fn=lambda _: ({}, []))
                validate(report, json.loads((ERYX / "report.schema.json").read_text(encoding="utf-8")))
                self.assertEqual((report["status"], report["exit_code"]), ("violations", 1))
                self.assertEqual({d["rule"] for d in report["diagnostics"]},
                                 {"ENCAPSULATED_ONLY_CALL", "reportArgumentType"})
                tool = next(tool for tool in report["tools"] if tool["name"] == "encapsulated-only")
                self.assertEqual((tool["status"], tool["exit_code"]), ("violations", 1))
                self.assertTrue((root / tool["raw_output"]["stdout"]).is_file())


if __name__ == "__main__":
    unittest.main()
