"""Placement and outer-class checks for migrated analyzer suites."""
from __future__ import annotations

from pathlib import Path
import unittest

from eryx._outer_classes_worker import analyze as outer_analyze
from eryx._policy import discover_files, read_scope
from eryx.contract_analysis.test_placement import analyze

ERYX = Path(__file__).resolve().parents[3]

class TestTestPlacementAnalysis(unittest.TestCase):
    def test_migrated_analyzer_suites_map_to_production_contracts(self) -> None:
        files = discover_files(ERYX, read_scope(ERYX))
        migrated = [
            "tests/eryx/contract_analysis/test_contracts.py",
            "tests/eryx/folder_analysis/test_folder_ownership.py",
            "tests/eryx/test_placement_analysis/test_test_placement.py",
            "tests/eryx/method_analysis/test_declarations.py",
            "tests/eryx/cohesion_analysis/test_classes.py",
            "tests/eryx/field_ownership/test_ownership.py",
            "tests/eryx/field_ownership/test_field_ownership.py",
            "tests/eryx/constructor_analysis/test_constructor_checker.py",
            "tests/eryx/restricted_calls/test_calls.py",
            "tests/eryx/test_placement_analysis/test_test_placement_analysis.py",
        ]
        self.assertTrue(set(migrated) <= set(files["tests"]))
        diagnostics, error = analyze(ERYX, files["production"], migrated, ["tests"])
        self.assertIsNone(error)
        self.assertEqual(diagnostics, [])
        diagnostics, error = outer_analyze(ERYX, migrated)
        self.assertIsNone(error)
        self.assertEqual(diagnostics, [])
