"""Compatibility and structural coverage for the field ownership seam."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

from quard._instance_fields_worker import Accesses as LegacyAccesses, Ownership as LegacyOwnership
from quard.field_access.accesses import Accesses
from quard.field_access.field_access import FieldAccess
from quard.field_ownership.accesses import Accesses as PreviousAccesses
from quard.field_ownership.field_ownership import FieldOwnership
from quard.field_ownership.ownership import Ownership
from quard.free_function_placement_utils import analyze as placement

QUARD = Path(__file__).resolve().parents[3]

class TestFieldOwnership(unittest.TestCase):
    def test_field_contracts_and_compatibility_imports(self):
        self.assertIs(LegacyOwnership, Ownership)
        self.assertIs(LegacyAccesses, Accesses)
        self.assertIs(PreviousAccesses, Accesses)
        self.assertIn(FieldOwnership, Ownership.__bases__)
        self.assertIn(FieldAccess, Accesses.__bases__)
        for folder, path, name in (("field_ownership", "field_ownership.py", "FieldOwnership"),
                                   ("field_ownership", "ownership.py", "Ownership"),
                                   ("field_ownership", "class_stores.py", "ClassStores"),
                                   ("field_access", "field_access.py", "FieldAccess"),
                                   ("field_access", "accesses.py", "Accesses")):
            source = QUARD / "src/quard" / folder / path
            outer = [item.name for item in ast.parse(source.read_text(encoding="utf-8")).body
                     if isinstance(item, ast.ClassDef)]
            self.assertEqual(outer, [name])
        paths = [str(path.relative_to(QUARD)) for folder in (
            "field_ownership", "field_operation_model", "field_type_analysis",
            "constructor_analysis", "constructor_origins")
            for path in (QUARD / "src/quard" / folder).glob("*.py")]
        paths.extend(("src/quard/field_initialization_utils.py",
                      "src/quard/_field_initialization.py"))
        diagnostics, error = placement(QUARD, paths)
        self.assertIsNone(error)
        self.assertEqual(diagnostics, [])
