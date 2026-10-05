"""Placement and outer-class checks for migrated analyzer suites."""
from __future__ import annotations

from pathlib import Path
import unittest

from quard._outer_classes_worker import analyze as outer_analyze
from quard._policy import discover_files, read_scope
from quard.contract_analysis.test_placement import analyze

QUARD = Path(__file__).resolve().parents[3]

class TestTestPlacementAnalysis(unittest.TestCase):
    def test_migrated_analyzer_suites_map_to_production_contracts(self) -> None:
        files = discover_files(QUARD, read_scope(QUARD))
        migrated = [
            "tests/quard/contract_analysis/test_contracts.py",
            "tests/quard/folder_analysis/test_folder_ownership.py",
            "tests/quard/test_placement_analysis/test_test_placement.py",
            "tests/quard/method_analysis/test_declarations.py",
            "tests/quard/cohesion_analysis/test_classes.py",
            "tests/quard/field_ownership/test_ownership.py",
            "tests/quard/field_ownership/test_field_ownership.py",
            "tests/quard/constructor_analysis/test_constructor_checker.py",
            "tests/quard/restricted_calls/test_calls.py",
            "tests/quard/test_placement_analysis/test_test_placement_analysis.py",
        ]
        self.assertTrue(set(migrated) <= set(files["tests"]))
        diagnostics, error = analyze(QUARD, files["production"], migrated, ["tests"])
        self.assertIsNone(error)
        self.assertEqual(diagnostics, [])
        diagnostics, error = outer_analyze(QUARD, migrated)
        self.assertIsNone(error)
        self.assertEqual(diagnostics, [])
