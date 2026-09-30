"""Isolated consumer fixtures for field ownership and report failures."""
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


class InstanceFieldTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="eryx-fields-")
        self.root = Path(self.temp.name)
        (self.root / "src/pkg").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / ".eryx/reports/run").mkdir(parents=True)
        (self.root / ".eryx/scope.toml").write_text(
            'exclude_directory_names = ["generated"]\n'
            'test_directory_names = ["tests"]\n', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

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
        self.assertEqual([(item["path"], item["line"], item["symbol"]) for item in result["diagnostics"]
                          if item["rule"] != "NO_CLASS_STATE"], [
            ("src/pkg/use.py", line, symbol) for line, symbol in (
                (5, "pkg.model.Record.value"), (6, "pkg.model.Record.value"),
                (7, "pkg.model.Record.value"), (11, "pkg.model.Record.declared"),
                (12, "pkg.model.Record.plain"), (13, "pkg.model.Record.value"))
        ] + [("tests/test_use.py", 4, "pkg.model.Record.value")])
        self.assertEqual([(item["path"], item["line"], item["symbol"])
                          for item in result["diagnostics"] if item["rule"] == "NO_CLASS_STATE"],
                         [("src/pkg/model.py", 3, "pkg.model.Record.plain")])
        self.assertEqual(raw["diagnostics"][0]["rule"], "NO_CLASS_STATE")
        self.assertEqual(result["diagnostics"][1]["column"], 9)
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
        self.assertEqual([(d["path"], d["line"], d["symbol"]) for d in result["diagnostics"]], [
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
                          for d in result["diagnostics"]], [
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
                          for d in result["diagnostics"]], [
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
                          for d in result["diagnostics"]], [
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
                          for d in result["diagnostics"]], [
            ("src/pkg/model.py", 9, "NO_CLASS_STATE", "pkg.model.Item.shared"),
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
        self.put("src/pkg/good.py", "class Good:\n    def __init__(self): self.value = 1\nx = Good().value\n")
        self.put("tests/broken.py", "class Broken(\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "INSTANCE_FIELDS_PARSE_ERROR"))
        self.assertEqual({item["rule"] for item in result["diagnostics"]},
                         {"INSTANCE_FIELDS_PARSE_ERROR", "EXTERNAL_INSTANCE_FIELD"})
        job = self.job()
        (self.root / "tests/broken.py").unlink()
        missing = execute_job(job, self.root, self.root / ".eryx/reports/run", 30)
        self.assertEqual((missing["status"], missing["error"]), ("error", "INSTANCE_FIELDS_PARSE_ERROR"))
        for payload, code in [("not json", 0), (json.dumps({
                "eryx_instance_fields_adapter": 1, "diagnostics": [], "execution_error": None}), 1),
                (json.dumps({"eryx_instance_fields_adapter": 1, "diagnostics": [
                    {"rule": "EXTERNAL_INSTANCE_FIELD", "message": "bad"}], "execution_error": None}), 1)]:
            with self.subTest(payload=payload):
                corrupt = execute_job(job, self.root, self.root / ".eryx/reports/run", 30,
                                      capture_fn=lambda *args: {"stdout": payload, "stderr": "",
                                                                "exit_code": code, "duration_seconds": 0,
                                                                "error": None})
                self.assertEqual(corrupt["status"], "error")


if __name__ == "__main__":
    unittest.main()
