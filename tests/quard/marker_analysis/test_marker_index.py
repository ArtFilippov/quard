"""Migrated state and marker classes retain their entry points and placement."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

from quard._class_contracts_worker import analyze as contracts_analyze
from quard._constructor_origins import Origins as LegacyOrigins
from quard._constructor_purity_worker import ConstructorChecker as LegacyChecker
from quard._contract_folders_worker import analyze as folders_analyze
from quard._field_operations import FieldOperation as LegacyOperation
from quard._field_types import TypeEvidence as LegacyEvidence
from quard._markers import MarkerBindings as LegacyBindings, MarkerIndex as LegacyIndex
from quard._outer_classes_worker import analyze as outer_analyze
from quard._restricted_calls import DispatchSite as LegacySite
from quard.constructor_analysis.constructor_checker import ConstructorChecker
from quard.constructor_origins.origins import Origins
from quard.dispatch_site_model.dispatch_site import DispatchSite
from quard.field_operation_model.field_operation import FieldOperation
from quard.field_type_analysis.type_evidence import TypeEvidence
from quard.marker_analysis.marker_index import MarkerIndex
from quard.marker_binding_analysis.marker_bindings import MarkerBindings
from quard.contract_analysis.contracts import Contracts
from quard.field_ownership.ownership import Ownership
from quard.folder_analysis.generated_external_contracts import GeneratedOwnership
from quard.restricted_targets.resolution import Restrictions

QUARD = Path(__file__).resolve().parents[3]

class TestMarkerIndex(unittest.TestCase):
    """Verify marker provenance and public descriptor classification."""

    def test_cached_aliases_reexports_stubs_and_shadowing(self):
        trees = {
            "src/pkg/facade.py": ast.parse("from qraft import cached_property as cached\n"),
            "src/pkg/port/port.py": ast.parse('''\
from typing import Protocol
class Port(Protocol):
    @property
    def value(self) -> int: ...
'''),
            "src/pkg/port/value.py": ast.parse('''\
from pkg.facade import cached as cache
from pkg.port.port import Port
class Value(Port):
    @cache
    def value(self) -> int: return 1
'''),
            "src/pkg/port/value.pyi": ast.parse('''\
from pkg.facade import cached as cache
from pkg.port.port import Port
class Value(Port):
    @cache
    def value(self) -> int: ...
'''),
            "src/pkg/port/assigned.py": ast.parse('''\
import qraft as runtime
from pkg.port.port import Port
class Assigned(Port):
    def calculate(self) -> int: return 1
    value = runtime.cached_property(calculate)
'''),
            "src/pkg/port/other.py": ast.parse('''\
from pkg.port.port import Port
from qraft import cached_property as cache
def cache(value): return value
class Other(Port):
    @cache
    def value(self) -> int: return 1
'''),
        }
        self.assertEqual({(owner, name) for owner, name in Restrictions(Ownership(trees)).properties
                          if owner.startswith("pkg.port.")},
                         {("pkg.port.port.Port", "value"), ("pkg.port.value.Value", "value"),
                          ("pkg.port.assigned.Assigned", "value")},
                         "unrelated or shadowed decorators became properties")

    def test_shadowed_qraft_module_does_not_impersonate_runtime(self):
        trees = {"src/qraft/__init__.py": ast.parse('''\
def cached_property(value): return value
'''), "src/pkg/example.py": ast.parse('''\
from qraft import cached_property as cache
class Example(object):
    @cache
    def value(self): return 1
''')}
        self.assertEqual(Ownership(trees).markers.property_declarations(next(
            item for item in trees["src/pkg/example.py"].body
            if isinstance(item, ast.ClassDef))), {},
            "a consumer package impersonated the external runtime")

    def test_cached_constructor_is_not_class_state(self):
        trees = {"src/pkg/example.py": ast.parse('''\
from qraft import cached_property as cache
class Example(object):
    def calculate(self) -> int: return 1
    value = cache(calculate)
''')}
        self.assertNotIn("pkg.example.Example.value",
                         {item["symbol"] for item in Ownership(trees).declaration_diagnostics()},
                         "a proven cached property was classified as stored state")

    def test_cached_constructor_keeps_concrete_ancestor_ban(self):
        trees = {"src/pkg/example.py": ast.parse('''\
from qraft import cached_property as cache
class Parent(object):
    def calculate(self): return 1
    value: object = cache(calculate)
class Child(Parent):
    @cache
    def value(self): return 2
''')}
        self.assertEqual({item["symbol"] for item in Contracts(trees).diagnostics()
                          if item["rule"] == "CUSTOMER_METHOD_OVERRIDE"},
                         {"Child.value"}, "cached properties lost the concrete ancestor ban")

    def test_cached_envelope_source_and_stub_agree(self):
        trees = {"src/pkg/port/port.py": ast.parse('''\
from typing import Protocol
class Port(Protocol):
    @property
    def value(self) -> int: ...
'''), "src/pkg/port/envelopes/value.py": ast.parse('''\
from qraft import cached_property as cache
from pkg.port.port import Port
class Value(Port):
    def __init__(self, delegate: Port) -> None:
        self.delegate = delegate
    @cache
    def value(self) -> int:
        return self.delegate.value
'''), "src/pkg/port/envelopes/value.pyi": ast.parse('''\
from pkg.port.port import Port
class Value(Port):
    @property
    def value(self) -> int: ...
''')}
        folder = GeneratedOwnership(trees)
        name = "pkg.port.envelopes.value.Value"
        self.assertEqual((folder.member_kind(name, next(
                             item for item in folder.nodes[name].body
                             if isinstance(item, ast.FunctionDef)
                             and item.name == "value"),
                                             "src/pkg/port/envelopes/value.py"),
                          folder.stub_agreement(name, next(
                              item for item in trees["src/pkg/port/envelopes/value.pyi"].body
                              if isinstance(item, ast.ClassDef)),
                                                "src/pkg/port/envelopes/value.pyi",
                                                original=folder.nodes[name],
                                                source="src/pkg/port/envelopes/value.py")),
                         ("get", True), "a cached read did not forward as a property")

    def test_migrated_state_and_marker_classes_keep_entry_points_and_placement(self):
        for old, current in ((LegacyOrigins, Origins), (LegacyChecker, ConstructorChecker),
                             (LegacyOperation, FieldOperation), (LegacyEvidence, TypeEvidence),
                             (LegacyIndex, MarkerIndex), (LegacyBindings, MarkerBindings),
                             (LegacySite, DispatchSite)):
            self.assertIs(old, current)

        paths = sorted(str(path.relative_to(QUARD)) for folder in (
            "constructor_analysis", "constructor_origins", "dispatch_site_model",
            "field_operation_model", "field_type_analysis", "marker_analysis",
            "marker_binding_analysis")
            for path in (QUARD / "src/quard" / folder).glob("*.py"))
        paths.extend(f"src/quard/{module}.py" for module in (
            "_constructor_origins", "_constructor_purity_worker", "_field_operations",
            "_field_types", "_markers"))
        for analyze in (contracts_analyze, folders_analyze, outer_analyze):
            diagnostics, error = analyze(QUARD, paths)
            self.assertIsNone(error)
            self.assertEqual([], diagnostics)
