"""Migrated state and marker classes retain their entry points and placement."""
from __future__ import annotations

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

QUARD = Path(__file__).resolve().parents[3]

class TestMarkerIndex(unittest.TestCase):
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
