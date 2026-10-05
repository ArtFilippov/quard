"""Contract and compatibility behavior for restricted target resolution."""
from __future__ import annotations

from pathlib import Path
import unittest

from quard._class_contracts_worker import analyze as contracts_analyze
from quard._contract_folders_worker import analyze as folders_analyze
from quard._outer_classes_worker import analyze as outer_analyze
from quard._restricted_calls import Restrictions as LegacyRestrictions
from quard.restricted_targets.resolution import Restrictions
from quard.restricted_targets.restricted_targets import RestrictedTargets

QUARD = Path(__file__).resolve().parents[3]

class TestRestrictedTargets(unittest.TestCase):
    def test_restricted_targets_contract_and_legacy_entry_point(self):
        self.assertIs(LegacyRestrictions, Restrictions)
        self.assertIn(RestrictedTargets, Restrictions.__bases__)
        paths = sorted(str(path.relative_to(QUARD)) for path in (
            QUARD / "src/quard/restricted_targets").glob("*.py"))
        for analyze in (contracts_analyze, folders_analyze, outer_analyze):
            diagnostics, error = analyze(QUARD, paths)
            self.assertIsNone(error)
            self.assertEqual([], diagnostics)
