"""Isolated consumer fixtures for field ownership and report failures."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import override
import unittest

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))

from eryx._policy import discover_files, read_scope
from eryx._field_initialization import _block
from eryx.check import execute_job, make_jobs


class TestOwnership(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.temp: TemporaryDirectory[str] = TemporaryDirectory(prefix="eryx-fields-")
        self.addCleanup(self.temp.cleanup)
        self.root: Path = Path(self.temp.name)

    @override
    def setUp(self):
        (self.root / "src/pkg").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / ".eryx/reports/run").mkdir(parents=True)
        (self.root / ".eryx/config.toml").write_text(
            (ERYX / "defaults/config.toml").read_text().split("[scope]")[0]
            + '[scope]\nexclude_directory_names = ["generated"]\n'
            'test_directory_names = ["tests"]\n', encoding="utf-8")

    def put(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def job(self):
        files = discover_files(self.root, read_scope(self.root))
        return next(job for job in make_jobs(self.root, self.root / ".eryx/reports/run", files, False, [])
                    if job["name"] == "instance-fields")

    def run_worker(self):
        job = self.job()
        result = execute_job(job, self.root, self.root / ".eryx/reports/run", 30)
        self.assertTrue((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"),
                        (self.root / result["raw_output"]["stderr"]).read_text(encoding="utf-8"))
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        return result, raw

    def boundary_diagnostics(self, result):
        """Keep older boundary fixtures focused on their original rules."""
        return [item for item in result["diagnostics"] if item["rule"] not in {
            "UNKNOWN_FIELD_TYPE", "NON_PRIMITIVE_FIELD_REBINDING"}]

    def test_direct_owner_constructor_initialization(self):
        self.put("src/pkg/initialized.py", '''\
class Ready:
    def __init__(this):
        this.value = 1
        this.other: int = 2
        this.left, this.right = 3, 4
    def change(this):
        this.value = 5

class Missing:
    def change(self):
        self.late = 1
        self.late = 2
    def __init__(self):
        pass

class NoConstructor:
    def change(self):
        self.absent = 1

class Conditional:
    def __init__(self):
        if condition:
            self.maybe = 1

class Nested:
    def __init__(self):
        def deferred():
            self.later = 1
    def change(self):
        self.later = 2
''')
        self.put("src/pkg/declared.pyi", "class Stub:\n    value: int\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["line"], d["column"], d["symbol"])
                          for d in self.boundary_diagnostics(result)], [
            (11, 9, "pkg.initialized.Missing.late"),
            (18, 9, "pkg.initialized.NoConstructor.absent"),
            (23, 13, "pkg.initialized.Conditional.maybe"),
            (30, 9, "pkg.initialized.Nested.later"),
        ])
        self.assertTrue(all(d["rule"] == "MISSING_FIELD_INITIALIZATION" and
                            not d["is_infrastructure_error"] for d in self.boundary_diagnostics(result)))

    def test_initialized_direct_fields_pass(self):
        self.put("src/pkg/ready.py", '''\
class Ready:
    def __init__(self):
        self.value = 1
        self.name: str = "ready"
    def change(self):
        self.value = 2
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["diagnostics"]),
                         ("passed", 0, []))

    def test_exact_types_and_path_sensitive_rebinding(self):
        self.put("src/pkg/types.py", '''\
from dataclasses import dataclass
class Small(int): pass
def number() -> int: return 1
@dataclass
class Data:
    entries: list[int]
    def change(self): self.entries.append(1)
class Model:
    def __init__(self, flag):
        self.scalar: int = Small(1)
        self.inferred = number()
        self.none = None
        self.union: int | str = 1
        self.child: Small = Small(1)
        self.items = []
        if flag:
            self.alternative = []
        else:
            self.alternative = []
        self.items = []
        self.none = None
    def update(self):
        self.scalar = 2
        self.inferred = 2
        self.items.append(1)
        self.items = []
        self.union = 2
        self.child = Small(2)
        self.alternative = []
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["line"], d["rule"], d["column"]) for d in result["diagnostics"]], [
            (20, "NON_PRIMITIVE_FIELD_REBINDING", 9),
            (21, "NON_PRIMITIVE_FIELD_REBINDING", 9),
            (26, "NON_PRIMITIVE_FIELD_REBINDING", 9),
            (27, "NON_PRIMITIVE_FIELD_REBINDING", 9),
            (28, "NON_PRIMITIVE_FIELD_REBINDING", 9),
            (29, "NON_PRIMITIVE_FIELD_REBINDING", 9),
        ])
        self.assertTrue(all(not d["is_infrastructure_error"] for d in result["diagnostics"]))

    def test_unknown_evidence_stub_conflicts_and_late_primitive(self):
        self.put("src/pkg/model.py", '''\
class Model:
    known: int
    def __init__(self):
        self.known = 1
        self.unknown = compute()
        self.conflict = 1
        self.conflict = "text"
        self.stubbed = 1
    def later(self):
        self.late = 1
''')
        self.put("src/pkg/model.pyi", '''\
class Model:
    known: str
    stubbed: str
''')
        result, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"]), ("violations", 1))
        self.assertEqual([(d["rule"], d["path"], d["line"], d["symbol"])
                          for d in result["diagnostics"]], [
            ("MISSING_FIELD_INITIALIZATION", "src/pkg/model.py", 10, "pkg.model.Model.late"),
            *[("UNKNOWN_FIELD_TYPE", "src/pkg/model.py", line, f"pkg.model.Model.{field}")
              for line, field in [(2, "known"), (5, "unknown"), (6, "conflict"), (8, "stubbed")]],
        ])

    def test_aliases_resolved_returns_and_loop_rebindings(self):
        self.put("src/pkg/factory.py", "def number() -> int: return 1\n")
        self.put("src/pkg/values.py", '''\
from pkg.factory import number as make
from builtins import int as integer
type Number = integer
class Values:
    def __init__(self, flag):
        self.number: Number = make()
        self.inferred = make()
        self.pieces: list[int] = []
        for _ in range(2):
            self.pieces = []
    def change(self):
        self.number = make()
        self.inferred = 2
        self.pieces.append(1)
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["rule"], d["line"], d["symbol"]) for d in result["diagnostics"]], [
            ("NON_PRIMITIVE_FIELD_REBINDING", 10, "pkg.values.Values.pieces")])

    def test_conflicting_generic_source_and_stub_annotations(self):
        self.put("src/pkg/generic.py", "class Generic:\n    values: list[int]\n"
                 "    def __init__(self): self.values = []\n")
        self.put("src/pkg/generic.pyi", "class Generic:\n    values: list[str]\n")
        result, _ = self.run_worker()
        self.assertEqual([(d["rule"], d["path"], d["line"])
                          for d in result["diagnostics"]], [
            ("UNKNOWN_FIELD_TYPE", "src/pkg/generic.py", 2)])

    def test_each_exact_primitive_may_be_reassigned(self):
        self.put("src/pkg/primitives.py", '''\
class Primitives:
    def make(self) -> str: return "made"
    def __init__(self):
        self.truth: bool = True
        self.count: int = 1
        self.measure: float = 1.0
        self.imaginary: complex = 1j
        self.text = self.make()
        self.data: bytes = b"one"
    def update(self):
        self.truth = False
        self.count = 2
        self.measure = 2.0
        self.imaginary = 2j
        self.text = "two"
        self.data = b"two"
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["diagnostics"]),
                         ("passed", 0, []))

    def test_indirect_constructor_bindings_and_proven_aliases(self):
        self.put("src/pkg/indirect.py", '''\
from builtins import setattr as store, vars as dictionary
import builtins as bi
class Ready:
    def __init__(self, flag):
        alias = self
        if flag:
            store(alias, "count", 1)
        else:
            bi.setattr(self, "count", 2)
        dictionary(alias)["items"] = []
        alias.__dict__.update({"name": "ready"})
    def change(self):
        alias = self
        store(alias, "count", 3)
        alias.__dict__["items"].append(1)
class Conditional:
    value: int
    def __init__(self, flag):
        if flag:
            alias = self
        store(alias, "value", 1)
class Late:
    def __init__(self): pass
    def change(self):
        alias = self
        dictionary(alias)["value"] = 1
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["rule"], d["line"], d["symbol"]) for d in result["diagnostics"]], [
            ("MISSING_FIELD_INITIALIZATION", 17, "pkg.indirect.Conditional.value"),
            ("MISSING_FIELD_INITIALIZATION", 26, "pkg.indirect.Late.value"),
        ])

    def test_indirect_rebinding_deletion_and_augmentation(self):
        self.put("src/pkg/lifecycle.py", '''\
from builtins import delattr as remove
class Values:
    def __init__(self):
        self.items: list[int] = []
        self.tuple_value: tuple[int] = (1,)
        self.custom: object = object()
        setattr(self, "unknown", factory())
    def change(self):
        alias = self
        alias.items.append(1)
        alias.tuple_value += (2,)
        alias.custom += other
        vars(alias)["items"] = []
        setattr(alias, "tuple_value", (3,))
        alias.__dict__.update({"custom": object()})
        setattr(alias, "unknown", factory())
        del alias.items
        remove(alias, "tuple_value")
        del alias.__dict__["custom"]
        vars(alias).pop("unknown")
class NotInitialized:
    value: tuple[int]
    def __init__(self):
        self.value += (1,)
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["rule"], d["line"], d["symbol"]) for d in result["diagnostics"]], [
            ("MISSING_FIELD_INITIALIZATION", 22, "pkg.lifecycle.NotInitialized.value"),
            ("UNKNOWN_FIELD_TYPE", 7, "pkg.lifecycle.Values.unknown"),
            *[("NON_PRIMITIVE_FIELD_REBINDING", line, f"pkg.lifecycle.Values.{field}")
              for line, field in [(13, "items"), (14, "tuple_value"), (15, "custom")]],
            *[("DELETED_INSTANCE_FIELD", line, f"pkg.lifecycle.Values.{field}")
              for line, field in [(17, "items"), (18, "tuple_value"),
                                  (19, "custom"), (20, "unknown")]],
        ])

    def test_unknown_names_and_reassigned_aliases_do_not_initialize(self):
        self.put("src/pkg/unproven.py", '''\
class Model:
    value: int
    other: int
    third: int
    def __init__(self, unknown, field):
        alias = self
        alias = unknown
        setattr(alias, "value", 1)
        setattr(self, field, 1)
        self = unknown
        self.other = 1
        vars(self)["third"] = 1
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["rule"], d["line"], d["symbol"]) for d in result["diagnostics"]
                          if d["rule"] == "MISSING_FIELD_INITIALIZATION"], [
            ("MISSING_FIELD_INITIALIZATION", 2, "pkg.unproven.Model.value"),
            ("MISSING_FIELD_INITIALIZATION", 3, "pkg.unproven.Model.other"),
            ("MISSING_FIELD_INITIALIZATION", 4, "pkg.unproven.Model.third"),
        ])

    def test_dictionary_setdefault_and_constructor_deletion(self):
        self.put("src/pkg/dictionary.py", '''\
class Defaults:
    def __init__(self):
        alias = self
        vars(alias).setdefault("value", [])
        alias.__dict__.setdefault("count", 1)
    def change(self):
        self.value.append(1)
        vars(self).setdefault("value", [])
        vars(self).setdefault("count", 2)
class Deleted:
    value: int
    def __init__(self):
        setattr(self, "value", 1)
        delattr(self, "value")
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["rule"], d["line"], d["symbol"]) for d in result["diagnostics"]], [
            ("MISSING_FIELD_INITIALIZATION", 11, "pkg.dictionary.Deleted.value"),
            ("DELETED_INSTANCE_FIELD", 14, "pkg.dictionary.Deleted.value"),
        ])

    def test_local_builtin_import_is_proven_but_shadowing_is_not(self):
        self.put("src/pkg/builtins.py", '''\
class Local:
    def __init__(self):
        from builtins import setattr as store
        import builtins as bi
        store(self, "count", 1)
        bi.vars(self)["items"] = []
class Shadowed:
    count: int
    def __init__(self, store):
        from builtins import setattr as set_field
        set_field = store
        set_field(self, "count", 1)
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["rule"], d["symbol"]) for d in result["diagnostics"]], [
            ("MISSING_FIELD_INITIALIZATION", "pkg.builtins.Shadowed.count"),
        ])

    def test_annotated_alias_write_supplies_exact_field_type(self):
        self.put("src/pkg/alias.py", '''\
class Annotated:
    def __init__(self):
        alias = self
        alias.value: int = compute()
    def change(self):
        alias = self
        alias.value = 1
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["diagnostics"]),
                         ("passed", 0, []))

    def test_deferred_or_empty_comprehensions_cannot_initialize(self):
        self.put("src/pkg/deferred.py", '''\
class Deferred:
    value: int
    other: int
    def __init__(self):
        generator = (setattr(self, "value", 1) for _ in [1])
        empty = [setattr(self, "other", 1) for _ in []]
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["rule"], d["symbol"]) for d in result["diagnostics"]], [
            ("MISSING_FIELD_INITIALIZATION", "pkg.deferred.Deferred.value"),
            ("MISSING_FIELD_INITIALIZATION", "pkg.deferred.Deferred.other"),
        ])

    def test_augmentation_alone_and_conditional_indirect_writes(self):
        self.put("src/pkg/control.py", '''\
class Augmented:
    def __init__(self):
        self.value += 1
class Conditional:
    def __init__(self, flag):
        if setattr(self, "value", 1) or flag:
            vars(self)["other"] = 1
        else:
            vars(self)["other"] = 2
class WithBody:
    def __init__(self, context):
        with context as self.value:
            setattr(self, "other", 1)
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["rule"], d["line"], d["symbol"]) for d in result["diagnostics"]], [
            ("MISSING_FIELD_INITIALIZATION", 3, "pkg.control.Augmented.value"),
            ("MISSING_FIELD_INITIALIZATION", 13, "pkg.control.WithBody.other"),
            ("UNKNOWN_FIELD_TYPE", 3, "pkg.control.Augmented.value"),
            ("UNKNOWN_FIELD_TYPE", 12, "pkg.control.WithBody.value"),
        ])

    def test_malformed_new_rule_report_is_an_error(self):
        self.put("src/pkg/model.py", "class Model:\n    value: list[int]\n")
        job = self.job()
        for rule in ("UNKNOWN_FIELD_TYPE", "NON_PRIMITIVE_FIELD_REBINDING",
                     "DELETED_INSTANCE_FIELD"):
            payload = json.dumps({"eryx_instance_fields_adapter": 1, "execution_error": None,
                                  "diagnostics": [{"rule": rule, "message": "missing location"}]})
            result = execute_job(job, self.root, self.root / ".eryx/reports/run", 30,
                                 capture_fn=lambda *args: {"stdout": payload, "stderr": "",
                                                           "exit_code": 1, "duration_seconds": 0,
                                                           "error": None})
            self.assertEqual(result["status"], "error")

    def test_declared_source_fields_need_own_constructor(self):
        self.put("src/pkg/declarations.py", '''\
from abc import ABC, abstractmethod
from typing import Protocol

class Interface(Protocol):
    value: int

class Abstract(ABC):
    __slots__ = ("slot",)
    @abstractmethod
    def work(self): ...

class Mixed:
    declared: int
    __slots__ = ("slot", "ready")
    def __init__(self, flag):
        self.ready = 1
        if flag:
            self.declared = 2
        self.slot = 3

class Ready:
    value: int
    __slots__ = ("slot",)
    def __init__(self):
        self.value = 1
        self.slot = 2

class DictionarySlots:
    __slots__ = ("__dict__", "__weakref__")
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["line"], d["column"], d["symbol"])
                          for d in self.boundary_diagnostics(result)], [
            (5, 5, "pkg.declarations.Interface.value"),
            (8, 18, "pkg.declarations.Abstract.slot"),
            (13, 5, "pkg.declarations.Mixed.declared"),
        ])

    def test_dataclass_generated_and_custom_constructor_assignments(self):
        self.put("src/pkg/data.py", '''\
from dataclasses import dataclass as data, field as field_of, InitVar, KW_ONLY
import dataclasses as dc
from typing import ClassVar

@data
class Generated:
    required: int
    defaulted: int = 1
    factory: list[int] = field_of(default_factory=list)
    skipped: int = field_of(init=False)
    shared_default: int = field_of(init=False, default=1)
    built: list[int] = field_of(init=False, default_factory=list)
    temp: InitVar[int] = 1
    _: KW_ONLY
    shared: ClassVar[int] = 2

@dc.dataclass(slots=True)
class Slotted:
    initialized: int = dc.field(init=False, default=1)
    missing: int = dc.field(init=False)

@data(init=False)
class NoGeneration:
    value: int

@data
class Custom:
    value: int
    excluded: int = field_of(init=False)
    def __init__(self):
        self.excluded = 1

@data
class Complete:
    value: int
    excluded: int = field_of(init=False)
    def __init__(self):
        self.value = 1
        self.excluded = 2
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["line"], d["column"], d["symbol"]) for d in result["diagnostics"]
                          if d["rule"] == "MISSING_FIELD_INITIALIZATION"], [
            (10, 5, "pkg.data.Generated.skipped"),
            (11, 5, "pkg.data.Generated.shared_default"),
            (20, 5, "pkg.data.Slotted.missing"),
            (24, 5, "pkg.data.NoGeneration.value"),
            (28, 5, "pkg.data.Custom.value"),
        ])
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]
                          if d["rule"] == "NO_CLASS_STATE"], [(15, "pkg.data.Generated.shared")])

    def test_stub_protocol_and_abstract_fields_have_ownership_without_initialization(self):
        self.put("src/pkg/interfaces.pyi", '''\
from typing import Protocol
from abc import ABC, abstractmethod
class ProtocolFields(Protocol):
    value: int
class AbstractFields(ABC):
    __slots__ = ("slot",)
    @abstractmethod
    def work(self) -> None: ...
''')
        self.put("tests/test_interfaces.py", '''\
from pkg.interfaces import ProtocolFields, AbstractFields
def use(protocol: ProtocolFields, abstract: AbstractFields):
    return protocol.value, abstract.slot
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["path"], d["line"], d["symbol"])
                          for d in self.boundary_diagnostics(result)], [
            ("tests/test_interfaces.py", 3, "pkg.interfaces.ProtocolFields.value"),
            ("tests/test_interfaces.py", 3, "pkg.interfaces.AbstractFields.slot"),
        ])

    def test_source_still_requires_initialization_with_companion_stub(self):
        self.put("src/pkg/model.py", "class Model:\n    value: int\n")
        self.put("src/pkg/model.pyi", "class Model:\n    value: int\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["path"], d["line"], d["symbol"]) for d in result["diagnostics"]], [
            ("src/pkg/model.py", 2, "pkg.model.Model.value"),
        ])

    def test_constructor_paths_returns_and_deferred_writes(self):
        self.put("src/pkg/paths.py", '''\
class Alternatives:
    def __init__(self, flag):
        if flag:
            self.value = 1
        else:
            self.value = 2

class MissingElse:
    def __init__(self, flag):
        if flag:
            self.value = 1

class EarlyReturn:
    def __init__(self, flag):
        if flag:
            return
        self.value = 1

class RaisingBranch:
    def __init__(self, flag):
        if flag:
            raise ValueError()
        self.value = 1

class AlwaysRaises:
    def __init__(self):
        raise ValueError()
    def change(self):
        self.value = 1

class Deferred:
    def __init__(self):
        def later():
            self.value = 1
        callback = lambda: setattr(self, "other", 1)
        self.fill()
    def fill(self):
        self.value = 2

class LoopOnly:
    def __init__(self, items):
        for item in items:
            self.value = item

class WhileOnly:
    def __init__(self, flag):
        while flag:
            self.value = 1

class LoopElse:
    def __init__(self, items):
        for item in items:
            self.value = item
        else:
            self.value = 0

class BreakElse:
    def __init__(self, items):
        for item in items:
            if item:
                break
        else:
            self.value = 1
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["line"], d["symbol"]) for d in self.boundary_diagnostics(result)], [
            (11, "pkg.paths.MissingElse.value"),
            (17, "pkg.paths.EarlyReturn.value"),
            (38, "pkg.paths.Deferred.value"),
            (43, "pkg.paths.LoopOnly.value"),
            (48, "pkg.paths.WhileOnly.value"),
            (63, "pkg.paths.BreakElse.value"),
        ])
        self.assertTrue(all(d["rule"] == "MISSING_FIELD_INITIALIZATION" and
                            not d["is_infrastructure_error"] for d in self.boundary_diagnostics(result)))

    def test_match_try_finally_and_loop_write_counts(self):
        self.put("src/pkg/flow.py", '''\
class Matched:
    def __init__(self, choice):
        match choice:
            case 0:
                self.value = 1
            case _:
                self.value = 2

class Unmatched:
    def __init__(self, choice):
        match choice:
            case 0:
                self.value = 1

class Finally:
    def __init__(self, flag):
        try:
            if flag:
                return
        finally:
            self.value = 1

class Handler:
    def __init__(self):
        try:
            self.value = compute()
        except ValueError:
            pass

class Alternatives:
    def __init__(self, flag):
        if flag:
            self.value = 1
        else:
            self.value = 2

class Repeated:
    def __init__(self, values):
        for value in values:
            self.value = value

class LoopRaises:
    def __init__(self):
        while True:
            raise ValueError()
    def change(self):
        self.value = 1

class WithTarget:
    def __init__(self, context):
        with context as self.value:
            pass

class WithBody:
    def __init__(self, context):
        with context:
            self.value = 1
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["symbol"], d["rule"]) for d in self.boundary_diagnostics(result)], [
            ("pkg.flow.Unmatched.value", "MISSING_FIELD_INITIALIZATION"),
            ("pkg.flow.Handler.value", "MISSING_FIELD_INITIALIZATION"),
            ("pkg.flow.Repeated.value", "MISSING_FIELD_INITIALIZATION"),
            ("pkg.flow.WithBody.value", "MISSING_FIELD_INITIALIZATION"),
        ])
        # The later rebinding rule needs successive loop writes, not two
        # mutually exclusive branch assignments, to reach count two.
        tree = ast.parse((self.root / "src/pkg/flow.py").read_text(encoding="utf-8"))
        alternatives, repeated = tree.body[4:6]
        self.assertIsInstance(alternatives, ast.ClassDef)
        self.assertIsInstance(repeated, ast.ClassDef)
        self.assertIsInstance(alternatives.body[0], ast.FunctionDef)
        self.assertIsInstance(repeated.body[0], ast.FunctionDef)
        self.assertEqual(_block(alternatives.body[0].body, {0}, "value", "self")["normal"], {1})
        self.assertEqual(_block(repeated.body[0].body, {0}, "value", "self")["normal"],
                         {0, 1, 2})

    def test_external_reads_writes_deletes_and_known_owners_in_both_profiles(self):
        self.put("src/pkg/model.py", '''\
class Record:
    declared: int
    plain = 1
    def __init__(self):
        self.value = 1
    def change(self, other: Record):
        self.value = other.value
        other.value = 2
    @property
    def public(self):
        return self.value
    def call(self):
        return self.value
''')
        self.put("src/pkg/use.py", '''\
from pkg.model import Record as Item
import pkg.model as model

def use(item: Item):
    a = item.value
    item.value = 2
    del item.value
    return item.public, item.call()

copy = Item()
copy.declared = 1
copy.plain += 1
model.Record().value
''')
        self.put("tests/test_use.py", '''\
from pkg.model import Record
def test_field():
    instance = Record()
    return instance.value
''')
        self.put("src/pkg/generated/ignored.py", "from pkg.model import Record\nx = Record().value\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(item["path"], item["line"], item["symbol"]) for item in self.boundary_diagnostics(result)
                           if item["rule"] != "NO_CLASS_STATE"], [
            ("src/pkg/model.py", 2, "pkg.model.Record.declared"),
            *(("src/pkg/use.py", line, symbol) for line, symbol in (
                (5, "pkg.model.Record.value"), (6, "pkg.model.Record.value"),
                (7, "pkg.model.Record.value"), (11, "pkg.model.Record.declared"),
                (12, "pkg.model.Record.plain"), (13, "pkg.model.Record.value"))),
        ] + [("tests/test_use.py", 4, "pkg.model.Record.value")])
        self.assertEqual([(item["path"], item["line"], item["symbol"])
                          for item in result["diagnostics"] if item["rule"] == "NO_CLASS_STATE"],
                         [("src/pkg/model.py", 3, "pkg.model.Record.plain")])
        self.assertEqual(raw["diagnostics"][0]["rule"], "MISSING_FIELD_INITIALIZATION")
        self.assertEqual(result["diagnostics"][1]["column"], 5)
        self.assertTrue(all(not item["is_infrastructure_error"] for item in result["diagnostics"]))

    def test_unknown_third_party_properties_methods_and_rebinding(self):
        self.put("src/pkg/model.py", '''\
class Model:
    def __init__(self): self.field = 1
    @property
    def property(self): return self.field
    def method(self): return self.field
    def peer(self, other: Model):
        return other.field
    public_via_builtin = property(lambda self: self.field)
''')
        self.put("tests/test_negative.py", '''\
from pkg.model import Model
from library import Foreign
def use(unknown, external: Foreign):
    unknown.field
    external.field
    Model().property
    Model().method()
    Model().public_via_builtin
    known = Model()
    known = unknown
    known.field
    known = Model()
    for known in objects:
        pass
    known.field
    known = Model()
    if condition:
        known = unknown
    known.field
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["diagnostics"], raw["diagnostics"]),
                         ("passed", [], []))

    def test_stub_fields_and_properties(self):
        self.put("src/pkg/record.pyi", '''\
class Record:
    value: int
    @property
    def public(self) -> int: ...
    def use(self, other: Record) -> int: ...
''')
        self.put("tests/test_stub.py", '''\
from pkg.record import Record
def use(record: Record):
    return record.value, record.public
''')
        result, _ = self.run_worker()
        self.assertEqual((result["status"], [(d["path"], d["line"], d["symbol"])
                                              for d in result["diagnostics"]]),
                          ("violations", [("tests/test_stub.py", 3, "pkg.record.Record.value")]))

    def test_first_writes_and_static_aliases(self):
        self.put("src/pkg/model.py", '''\
class Model:
    def __init__(self, label: str = ""): pass
    def own(self, peer: Model):
        self.new = 1
        peer.other = 2
    def public(self): pass
    @property
    def prop(self): return 1

class Child(Model):
    def own(self):
        self.child_field = 1

Alias = Model
type TypeAlias = Model
''')
        self.put("src/pkg/use.py", '''\
from pkg.model import Model
def use(value: Model):
    value.from_production = 1
''')
        self.put("tests/test_first.py", '''\
from pkg.model import Model as Renamed, Child, Alias, TypeAlias
from library import Foreign

def use(value: Renamed, child: Child, foreign: Foreign, unknown):
    value.fresh = 1
    value.fresh += 1
    del value.new
    alias = value
    alias.created: int = 1
    child.new = 1
    child.own_field = 1
    value.public = 1
    value.prop = 1
    foreign.fresh = 1
    unknown.fresh = 1
    Alias().other_new = 1
    TypeAlias().via_type_alias = 1
    Renamed(label="test").from_keyword_call = 1
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["path"], d["line"], d["symbol"])
                          for d in result["diagnostics"] if d["rule"] == "MISSING_FIELD_INITIALIZATION"], [
            ("src/pkg/model.py", 4, "pkg.model.Model.new"),
            ("src/pkg/model.py", 12, "pkg.model.Child.child_field"),
        ])
        self.assertEqual([(d["path"], d["line"], d["symbol"])
                          for d in result["diagnostics"] if d["rule"] != "MISSING_FIELD_INITIALIZATION"], [
            ("src/pkg/use.py", 3, "pkg.model.Model.from_production")
        ] + [
            ("tests/test_first.py", line, f"pkg.model.{owner}.{field}")
            for line, owner, field in [(5, "Model", "fresh"), (6, "Model", "fresh"),
                                       (7, "Model", "new"), (9, "Model", "created"),
                                       (10, "Model", "new"), (11, "Child", "own_field"),
                                       (16, "Model", "other_new"),
                                       (17, "Model", "via_type_alias"),
                                       (18, "Model", "from_keyword_call")]
        ])

    def test_annotation_fallback_and_ambiguous_owners(self):
        self.put("src/pkg/model.pyi", '''\
class Model:
    stored: int
    def public(self) -> int: ...
class Other:
    stored: int
class Mixed(ForeignBase): ...
''')
        self.put("tests/test_types.py", '''\
from pkg.model import Model, Other, Mixed
from library import Foreign
Alias = Model

def use(value: Alias, different: Model | Other, mixed: Model | Foreign,
        foreign: Foreign, unknown):
    value.stored
    value.fresh = 1
    different.stored
    different.fresh = 1
    mixed.stored
    mixed.fresh = 1
    foreign.stored = 1
    unknown.stored = 1
    typed: Alias = unknown
    typed.fresh = 1
    conflict: Model = Other()
    conflict.stored
    Model().public
    explicit: Model | Other = Model()
    explicit.stored
    Mixed().first = 1
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]], [
            (7, "pkg.model.Model.stored"), (8, "pkg.model.Model.fresh"),
            (16, "pkg.model.Model.fresh")
        ])

    def test_dataclass_annotations_slots_and_inherited_owners_across_modules(self):
        self.put("src/pkg/model.py", '''\
from dataclasses import dataclass as data

@data
class Parent:
    item: int
    def own(self, peer: Parent):
        return peer.item

class Slotted:
    __slots__ = ("slot",)
    def own(self, peer: Slotted):
        return peer.slot

class Annotated:
    value: int
    def own(self, peer: Annotated):
        return peer.value
''')
        self.put("src/pkg/child.py", '''\
from pkg.model import Parent, Slotted, Annotated
from unknown import Foreign

class Child(Parent):
    own_field: int
    def access(self, peer: Child):
        peer.own_field
        self.item
        peer.item = 2

class Overridden(Parent):
    item: int
    def access(self, peer: Overridden):
        return peer.item

class Mixed(Foreign, Parent):
    def access(self):
        return self.item

class Container:
    item: int
    class Nested:
        def access(self, container: Container):
            return container.item
    def access(self, nested: Nested, peer: Container):
        return peer.item
''')
        self.put("tests/test_models.py", '''\
from pkg.model import Parent, Slotted, Annotated
from pkg.child import Child, Overridden, Container
import pkg.model as models
from unknown import Foreign

def use(parent: Parent, slot: Slotted, annotated: Annotated, child: Child,
        overridden: Overridden, unknown: Foreign):
    parent.item
    slot.slot
    annotated.value
    child.own_field
    child.item
    overridden.item
    Container.Nested().access(Container())
    unknown.item
    models.Parent(1).item
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(d["path"], d["line"], d["symbol"])
                           for d in self.boundary_diagnostics(result)], [
            ("src/pkg/child.py", 5, "pkg.child.Child.own_field"),
            ("src/pkg/child.py", 12, "pkg.child.Overridden.item"),
            ("src/pkg/child.py", 21, "pkg.child.Container.item"),
            ("src/pkg/model.py", 10, "pkg.model.Slotted.slot"),
            ("src/pkg/model.py", 15, "pkg.model.Annotated.value"),
            ("src/pkg/child.py", 8, "pkg.model.Parent.item"),
            ("src/pkg/child.py", 9, "pkg.model.Parent.item"),
            ("src/pkg/child.py", 24, "pkg.child.Container.item"),
            ("tests/test_models.py", 8, "pkg.model.Parent.item"),
            ("tests/test_models.py", 9, "pkg.model.Slotted.slot"),
            ("tests/test_models.py", 10, "pkg.model.Annotated.value"),
            ("tests/test_models.py", 11, "pkg.child.Child.own_field"),
            ("tests/test_models.py", 12, "pkg.model.Parent.item"),
            ("tests/test_models.py", 13, "pkg.child.Overridden.item"),
            ("tests/test_models.py", 16, "pkg.model.Parent.item"),
        ])

    def test_qualified_dataclass_and_literal_slot_forms_only(self):
        self.put("src/pkg/models.py", '''\
import dataclasses as dc
from unknown import transform

@dc.dataclass(slots=True)
class Data:
    value: int

class Slots:
    __slots__: dict[str, str] = {"field": "documentation"}

@transform
class Unresolved:
    value: int

class Dynamic:
    __slots__ = make_slots()

dc = transform
@dc.dataclass
class Shadowed:
    value: int
''')
        self.put("tests/test_data.py", '''\
from pkg.models import Data, Slots, Unresolved, Dynamic, Shadowed
Data(1).value
Slots().field
Unresolved().value
Dynamic().field
Shadowed().value
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["path"], d["line"], d["symbol"])
                           for d in self.boundary_diagnostics(result)], [
            ("src/pkg/models.py", 9, "pkg.models.Slots.field"),
            ("src/pkg/models.py", 13, "pkg.models.Unresolved.value"),
            ("src/pkg/models.py", 21, "pkg.models.Shadowed.value"),
            ("tests/test_data.py", 2, "pkg.models.Data.value"),
            ("tests/test_data.py", 3, "pkg.models.Slots.field"),
        ])

    def test_class_state_final_values_and_public_interfaces(self):
        self.put("src/pkg/state.py", '''\
from typing import Final as Fixed
import typing as t
from functools import cached_property

class State:
    MUTABLE = []
    lower = 1
    stable: Fixed[tuple[int, ...]] = (1, 2)
    nested: t.Final[tuple[int, ...]] = (1, 2)
    mutable_final: Fixed[list[int]] = []
    uncertain: Fixed[int] = compute()
    instance_final: Fixed[int]
    callback = lambda: 1
    descriptor = CustomDescriptor()
    cached_via_call = cached_property(lambda self: self.lower)
    @custom_descriptor
    def decorated(self): return 1

    def __init__(self):
        self.instance_final = 1
    @property
    def public(self): return self.lower
    @cached_property
    def cached(self): return self.lower
    def method(self): return self.lower
    def within(self, peer: State):
        State.MUTABLE
        peer.MUTABLE = []
        del peer.lower
        return State.stable, peer.instance_final, peer.descriptor

class Child(State):
    def outside(self):
        return self.MUTABLE
''')
        self.put("src/pkg/consumer.py", '''\
from pkg.state import State as S
S.MUTABLE
S.MUTABLE = []
del S.MUTABLE
S().MUTABLE
S().MUTABLE = []
del S().MUTABLE
S.lower
S().lower
S.stable
S().stable
S.stable = ()
del S().stable
S.nested
S.mutable_final
S().mutable_final
S.uncertain
S().instance_final
S.callback()
S().callback()
S.descriptor
S().descriptor
S.decorated
S().decorated
S().public
S().cached
S().method()
S().cached_via_call
''')
        self.put("tests/test_state.py", '''\
from pkg.state import State
State.MUTABLE
State.stable
State.stable = (1,)
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], raw["execution_error"]), ("violations", None))
        self.assertEqual([(d["path"], d["line"], d["rule"], d["symbol"])
                          for d in result["diagnostics"] if d["rule"] != "NO_CLASS_STATE"], [
            *[("src/pkg/consumer.py", line, "EXTERNAL_CLASS_FIELD", f"pkg.state.State.{field}")
              for line, field in [(2, "MUTABLE"), (3, "MUTABLE"), (4, "MUTABLE"),
                                  (5, "MUTABLE"), (6, "MUTABLE"), (7, "MUTABLE"),
                                   (8, "lower"), (9, "lower"), (10, "stable"),
                                   (11, "stable"), (12, "stable"),
                                   (13, "stable"), (14, "nested"), (15, "mutable_final"),
                                  (16, "mutable_final"), (17, "uncertain")]],
            ("src/pkg/consumer.py", 18, "EXTERNAL_INSTANCE_FIELD", "pkg.state.State.instance_final"),
            *[("src/pkg/consumer.py", line, "EXTERNAL_CLASS_FIELD", f"pkg.state.State.{field}")
              for line, field in [(19, "callback"), (20, "callback"),
                                  (21, "descriptor"), (22, "descriptor"),
                                  (23, "decorated"), (24, "decorated")]],
            ("src/pkg/state.py", 34, "EXTERNAL_CLASS_FIELD", "pkg.state.State.MUTABLE"),
            ("tests/test_state.py", 2, "EXTERNAL_CLASS_FIELD", "pkg.state.State.MUTABLE"),
            ("tests/test_state.py", 3, "EXTERNAL_CLASS_FIELD", "pkg.state.State.stable"),
            ("tests/test_state.py", 4, "EXTERNAL_CLASS_FIELD", "pkg.state.State.stable"),
        ])
        self.assertEqual({d["symbol"] for d in result["diagnostics"] if d["rule"] == "NO_CLASS_STATE"},
                         {f"pkg.state.State.{field}" for field in (
                             "MUTABLE", "lower", "stable", "nested", "mutable_final", "uncertain",
                             "callback", "descriptor", "decorated")})

    def test_final_annotations_do_not_exempt_class_state(self):
        self.put("src/pkg/constants.py", '''\
from typing_extensions import Final as Fixed
from typing import Final

class Config:
    good: Fixed[int] = 3
    empty: Final[tuple[int, ...]] = ()
    mutable: Final[tuple[list[int], ...]] = ([],)
    named: Final[tuple[int, ...]] = SOME_TUPLE
    repeated: Final[int] = 1
    repeated = []

class Shadowed:
    Final = fake
    shadowed: Final[int] = 2

class Regular:
    value: Final[int] = 7

class Derived(Config):
    good = []

class InstanceShadow(Config):
    def __init__(self): self.empty = 1
''')
        self.put("tests/test_constants.py", '''\
from pkg.constants import Config, Shadowed, Regular, Derived, InstanceShadow
Config.good
Config.empty
Config.mutable
Config.named
Config.repeated
Shadowed.shadowed
Regular.value
Derived.good
Derived().good
Config.good = 4
InstanceShadow.empty
InstanceShadow().empty
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]
                          if d["rule"] == "NO_CLASS_STATE"], [
            (line, f"pkg.constants.{owner}.{field}") for line, owner, field in [
                (5, "Config", "good"), (6, "Config", "empty"),
                (7, "Config", "mutable"), (8, "Config", "named"),
                (9, "Config", "repeated"), (10, "Config", "repeated"),
                (13, "Shadowed", "Final"), (14, "Shadowed", "shadowed"),
                (17, "Regular", "value"), (20, "Derived", "good")]])
        self.assertEqual([(d["line"], d["rule"], d["symbol"])
                          for d in result["diagnostics"] if d["rule"] != "NO_CLASS_STATE"], [
            (2, "EXTERNAL_CLASS_FIELD", "pkg.constants.Config.good"),
            (3, "EXTERNAL_CLASS_FIELD", "pkg.constants.Config.empty"),
            (4, "EXTERNAL_CLASS_FIELD", "pkg.constants.Config.mutable"),
            (5, "EXTERNAL_CLASS_FIELD", "pkg.constants.Config.named"),
            (6, "EXTERNAL_CLASS_FIELD", "pkg.constants.Config.repeated"),
            (7, "EXTERNAL_CLASS_FIELD", "pkg.constants.Shadowed.shadowed"),
            (8, "EXTERNAL_CLASS_FIELD", "pkg.constants.Regular.value"),
            (9, "EXTERNAL_CLASS_FIELD", "pkg.constants.Derived.good"),
            (10, "EXTERNAL_CLASS_FIELD", "pkg.constants.Derived.good"),
            (11, "EXTERNAL_CLASS_FIELD", "pkg.constants.Config.good"),
            (12, "EXTERNAL_CLASS_FIELD", "pkg.constants.Config.empty"),
            (13, "EXTERNAL_INSTANCE_FIELD", "pkg.constants.InstanceShadow.empty"),
        ])

    def test_class_values_are_forbidden_at_declaration_and_on_later_writes(self):
        self.put("src/pkg/state.py", '''\
from typing import Final
class State:
    IMMUTABLE: Final[int] = 3
    mutable = []
    callback = lambda: 1
    annotated: int
    __slots__ = ("field",)
    @property
    def public(self): return self.field
    def method(self):
        State.later = 1
        return State.IMMUTABLE
State.new = 2
''')
        self.put("tests/test_state.py", '''\
from pkg.state import State
State.IMMUTABLE
State.IMMUTABLE = 4
State.another = 5
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["path"], d["line"], d["rule"], d["symbol"])
                           for d in self.boundary_diagnostics(result)], [
            ("src/pkg/state.py", 6, "MISSING_FIELD_INITIALIZATION", "pkg.state.State.annotated"),
            ("src/pkg/state.py", 7, "MISSING_FIELD_INITIALIZATION", "pkg.state.State.field"),
            ("src/pkg/state.py", 3, "NO_CLASS_STATE", "pkg.state.State.IMMUTABLE"),
            ("src/pkg/state.py", 4, "NO_CLASS_STATE", "pkg.state.State.mutable"),
            ("src/pkg/state.py", 5, "NO_CLASS_STATE", "pkg.state.State.callback"),
            ("src/pkg/state.py", 11, "NO_CLASS_STATE", "pkg.state.State.later"),
            ("src/pkg/state.py", 13, "NO_CLASS_STATE", "pkg.state.State.new"),
            ("tests/test_state.py", 2, "EXTERNAL_CLASS_FIELD", "pkg.state.State.IMMUTABLE"),
            ("tests/test_state.py", 3, "EXTERNAL_CLASS_FIELD", "pkg.state.State.IMMUTABLE"),
            ("tests/test_state.py", 4, "NO_CLASS_STATE", "pkg.state.State.another"),
        ])

    def test_dataclass_defaults_enum_members_and_slots_are_not_class_state(self):
        self.put("src/pkg/model.py", '''\
from dataclasses import dataclass as data
from enum import Enum as Choice
import enum as e
from typing import ClassVar as CV
@data(slots=True)
class Item:
    quantity: int
    name: str = "default"
    shared: CV[int] = 1
class Slotted:
    __slots__ = ("name", "quantity")
class Color(Choice):
    RED = 1
class Direction(e.StrEnum):
    NORTH = "north"
    _cache = []
class Ordinary:
    name: str = "default"
''')
        self.put("tests/test_model.py", '''\
from pkg.model import Item, Color, Direction
Item(1).name
Color.RED
Direction.NORTH
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["path"], d["line"], d["rule"], d["symbol"])
                           for d in self.boundary_diagnostics(result)], [
            ("src/pkg/model.py", 9, "NO_CLASS_STATE", "pkg.model.Item.shared"),
            ("src/pkg/model.py", 11, "MISSING_FIELD_INITIALIZATION", "pkg.model.Slotted.name"),
            ("src/pkg/model.py", 11, "MISSING_FIELD_INITIALIZATION", "pkg.model.Slotted.quantity"),
            ("src/pkg/model.py", 16, "NO_CLASS_STATE", "pkg.model.Direction._cache"),
            ("src/pkg/model.py", 18, "NO_CLASS_STATE", "pkg.model.Ordinary.name"),
            ("tests/test_model.py", 2, "EXTERNAL_INSTANCE_FIELD", "pkg.model.Item.name"),
        ])

    def test_conditional_and_reassigned_class_values_are_prohibited(self):
        self.put("src/pkg/choices.py", '''\
class Choices:
    public = property(lambda self: 1)
    public = 2
    if enabled:
        extra, another = 1, 2
    def update(self):
        Choices.extra = 3
Alias = Choices
Alias.later = 4
setattr(Alias, "dynamic", 5)
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["line"], d["rule"], d["symbol"])
                          for d in result["diagnostics"]], [
            (3, "NO_CLASS_STATE", "pkg.choices.Choices.public"),
            (5, "NO_CLASS_STATE", "pkg.choices.Choices.extra"),
            (5, "NO_CLASS_STATE", "pkg.choices.Choices.another"),
            (7, "NO_CLASS_STATE", "pkg.choices.Choices.extra"),
            (9, "NO_CLASS_STATE", "pkg.choices.Choices.later"),
            (10, "NO_CLASS_STATE", "pkg.choices.Choices.dynamic"),
        ])

    def test_indirect_access_in_production_and_tests(self):
        self.put("src/pkg/model.py", '''\
class Model:
    value: int
    shared = 1
    def __init__(self):
        self.value = 1
        setattr(self, "indirect", 1)
        vars(self)["via_dict"] = 1
    @property
    def public(self): return []
    def own(self, peer: Model):
        getattr(peer, "value")
        setattr(peer, "value", 2)
        delattr(peer, "value")
        peer.__dict__["value"] = 3
        return vars(peer)["value"]
class Child(Model):
    def outside(self): return getattr(self, "value")
''')
        self.put("src/pkg/use.py", '''\
from pkg.model import Model
import builtins as bi
def use(obj: Model, unknown, field):
    getattr(obj, "value")
    getattr(obj, "value", None)
    setattr(obj, "value", 2)
    delattr(obj, "value")
    obj.__dict__["value"]
    obj.__dict__["value"] = 3
    del obj.__dict__["value"]
    vars(obj)["value"]
    vars(obj)["value"] = 4
    del vars(obj)["value"]
    setattr(obj, "fresh", 1)
    obj.__dict__["new"] = 1
    vars(obj)["later"] = 1
    bi.getattr(obj, "value")
    getattr(obj, "indirect")
    getattr(obj, "via_dict")
    vars(obj).get("value")
    vars(obj).pop("value")
    vars(obj).setdefault("value", 1)
    vars(obj).update({"value": 2})
    obj.__dict__["shared"] = 1
    vars(obj)["shared"]
    Model.shared
    getattr(Model, "shared")
    setattr(Model, "shared", 2)
    delattr(Model, "shared")
    vars(Model)["shared"]
    vars(Model)["shared"] = 3
    del Model.__dict__["shared"]
    setattr(Model, "new", 1)
    delattr(Model, "missing")
    getattr(obj, "public").append(1)
    vars(obj)["public"].append(1)
    getattr(obj, "value" + field)
    vars(obj)[field]
    getattr(unknown, "value")
    unknown.__dict__["value"]
    obj.build().value
    Model().make().value
''')
        self.put("tests/test_use.py", '''\
from pkg.model import Model
from builtins import getattr as fetch, vars as dictionary
def use(value: Model):
    fetch(value, "value")
    dictionary(value)["value"]
    getattr(value, "public").append(2)
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        actual = [(d["path"], d["line"], d["rule"], d["symbol"], d["column"])
                  for d in result["diagnostics"] if d["rule"] != "NO_CLASS_STATE"]
        expected = [
            ("src/pkg/model.py", 17, "EXTERNAL_INSTANCE_FIELD", "pkg.model.Model.value", 31),
            *[("src/pkg/use.py", line, "EXTERNAL_INSTANCE_FIELD", f"pkg.model.Model.{field}", column)
              for line, field, column in [
                  (4, "value", 5), (5, "value", 5), (6, "value", 5), (7, "value", 5),
                  (8, "value", 5), (9, "value", 5), (10, "value", 9),
                  (11, "value", 5), (12, "value", 5), (13, "value", 9),
                  (14, "fresh", 5), (15, "new", 5), (16, "later", 5),
                  (17, "value", 5), (18, "indirect", 5), (19, "via_dict", 5),
                  (20, "value", 5), (21, "value", 5), (22, "value", 5),
                  (23, "value", 5)]],
            ("src/pkg/use.py", 24, "EXTERNAL_CLASS_FIELD", "pkg.model.Model.shared", 5),
            ("src/pkg/use.py", 26, "EXTERNAL_CLASS_FIELD", "pkg.model.Model.shared", 5),
            *[("src/pkg/use.py", line, "EXTERNAL_CLASS_FIELD", "pkg.model.Model.shared", column)
              for line, column in [(27, 5), (28, 5), (29, 5), (30, 5), (31, 5), (32, 9)]],
            ("tests/test_use.py", 4, "EXTERNAL_INSTANCE_FIELD", "pkg.model.Model.value", 5),
            ("tests/test_use.py", 5, "EXTERNAL_INSTANCE_FIELD", "pkg.model.Model.value", 5),
        ]
        self.assertEqual(actual, expected)
        self.assertEqual([(d["line"], d["symbol"]) for d in result["diagnostics"]
                          if d["rule"] == "NO_CLASS_STATE"], [
            (3, "pkg.model.Model.shared"),
            (33, "pkg.model.Model.new")])
        self.assertEqual(len(raw["diagnostics"]), len(result["diagnostics"]))
        self.assertTrue(all(not d["is_infrastructure_error"] for d in result["diagnostics"]))

    def test_indirect_unknown_names_owners_and_shadowed_builtins(self):
        self.put("src/pkg/model.py", '''\
class Model:
    def __init__(self): self.value = 1
    @property
    def public(self): return []
''')
        self.put("tests/test_safe.py", '''\
from pkg.model import Model
from library import Foreign
def use(value: Model, other: Foreign, unknown, field):
    getattr(value, field)
    setattr(value, field, 2)
    delattr(value, field)
    vars(value)[field]
    value.__dict__[field] = 3
    getattr(other, "value")
    vars(other)["value"] = 2
    unknown.__dict__["value"]
    getattr(value, "public").append(1)
    value.public.append(1)
    value.method().value
    Model().method().value
def shadow(getattr, setattr, delattr, vars):
    value = Model()
    getattr(value, "value")
    setattr(value, "value", 2)
    delattr(value, "value")
    vars(value)["value"]
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["diagnostics"], raw["diagnostics"]),
                         ("passed", [], []))

    def test_local_classes_are_checked_without_treating_methods_as_state(self):
        self.put("src/pkg/local.py", '''\
def factory():
    class Local:
        value = 1
        def method(self):
            return 2
    return Local
''')
        result, _ = self.run_worker()
        self.assertEqual([(d["line"], d["rule"], d["symbol"])
                          for d in result["diagnostics"]], [
            (3, "NO_CLASS_STATE", "pkg.local.factory.Local.value"),
        ])

    def test_source_and_output_failures_are_infrastructure_errors(self):
        self.put("src/pkg/good.py", "class Good:\n    def __init__(self): self.value = 1\n"
                 "    def later(self): self.extra = 2\nx = getattr(Good(), 'value')\n")
        self.put("tests/broken.py", "class Broken(\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "INSTANCE_FIELDS_PARSE_ERROR"))
        self.assertEqual({item["rule"] for item in result["diagnostics"]},
                         {"INSTANCE_FIELDS_PARSE_ERROR", "EXTERNAL_INSTANCE_FIELD",
                          "MISSING_FIELD_INITIALIZATION"})
        self.assertEqual(raw["diagnostics"][-1]["symbol"], "pkg.good.Good.value")
        job = self.job()
        (self.root / "tests/broken.py").unlink()
        missing = execute_job(job, self.root, self.root / ".eryx/reports/run", 30)
        self.assertEqual((missing["status"], missing["error"]), ("error", "INSTANCE_FIELDS_PARSE_ERROR"))
        for payload, code in [("not json", 0), (json.dumps({
                "eryx_instance_fields_adapter": 1, "diagnostics": [], "execution_error": None}), 1),
                (json.dumps({"eryx_instance_fields_adapter": 1, "diagnostics": [
                    {"rule": "EXTERNAL_INSTANCE_FIELD", "message": "bad"}], "execution_error": None}), 1),
                (json.dumps({"eryx_instance_fields_adapter": 1, "diagnostics": [
                    {"rule": "MISSING_FIELD_INITIALIZATION", "message": "bad"}],
                    "execution_error": None}), 1)]:
            with self.subTest(payload=payload):
                corrupt = execute_job(job, self.root, self.root / ".eryx/reports/run", 30,
                                      capture_fn=lambda *args: {"stdout": payload, "stderr": "",
                                                                "exit_code": code, "duration_seconds": 0,
                                                                "error": None})
                self.assertEqual(corrupt["status"], "error")


if __name__ == "__main__":
    unittest.main()
