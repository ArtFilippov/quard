"""Contract and legacy imports for restricted-call traversal."""
from __future__ import annotations

from pathlib import Path
import unittest

from quard._class_contracts_worker import analyze as contracts_analyze
from quard._contract_folders_worker import analyze as folders_analyze
from quard._outer_classes_worker import analyze as outer_analyze
from quard._restricted_calls import Ancestry as LegacyAncestry, Calls as LegacyCalls
from quard.ancestry_traversal.ancestry import Ancestry
from quard.ancestry_traversal.ancestry_traversal import AncestryTraversal
from quard.restricted_calls.ancestry import Ancestry as PreviousAncestry
from quard.restricted_calls.calls import Calls
from quard.restricted_calls.restricted_calls import RestrictedCalls

QUARD = Path(__file__).resolve().parents[3]

class TestRestrictedCalls(unittest.TestCase):
    def test_restricted_calls_contract_derived_visitor_and_legacy_entry_point(self):
        self.assertIs(LegacyCalls, Calls)
        self.assertIs(LegacyAncestry, Ancestry)
        self.assertIs(PreviousAncestry, Ancestry)
        self.assertIn(RestrictedCalls, Calls.__bases__)
        self.assertIn(Calls, Ancestry.__bases__)
        self.assertIn(AncestryTraversal, Ancestry.__bases__)
        self.assertIn(RestrictedCalls, AncestryTraversal.__bases__)
        paths = sorted(str(path.relative_to(QUARD)) for folder in (
            "ancestry_traversal", "restricted_calls")
            for path in (QUARD / "src/quard" / folder).glob("*.py"))
        paths.append("src/quard/_restricted_calls.py")
        for analyze in (contracts_analyze, folders_analyze, outer_analyze):
            diagnostics, error = analyze(QUARD, paths)
            self.assertIsNone(error)
            self.assertEqual([], diagnostics)
