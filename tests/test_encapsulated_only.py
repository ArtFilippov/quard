"""Consumer-facing restricted-call worker and runner regressions."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from jsonschema import validate

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX / "src"))

from eryx._policy import discover_files, read_scope
from eryx.check import execute_job, make_jobs


class EncapsulatedOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="eryx-restricted-")
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
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
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
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
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]],
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
        from test_infrastructure import create_repo, native_result, RunnerTests

        with TemporaryDirectory(prefix="eryx-restricted-runner-") as directory:
            root = Path(directory)
            create_repo(root)
            (root / "src/demo/domain/api.py").write_text('''\
def encapsulated_only[T](value: T) -> T: return value
class Service(object):
    @encapsulated_only
    def run(this): pass
Service().run()
''', encoding="utf-8")
            fake, _ = RunnerTests().fake_tools()

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
