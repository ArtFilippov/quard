"""Implicit dispatch keeps receiver permission distinct from possible targets."""
from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))

from quard.dispatch_site_model.dispatch_site import DispatchSite


class TestDispatchSite(unittest.TestCase):
    def test_receiver_permission_and_class_origins(self) -> None:
        statement = ast.parse("value + other").body[0]
        assert isinstance(statement, ast.Expr)
        expression = statement.value
        assert isinstance(expression, ast.BinOp)
        receiver = expression.left
        def allowed(node: ast.expr) -> bool:
            return node is receiver
        current = DispatchSite(expression, receiver, frozenset({("current", "pkg.Owner")}),
                               ("pkg.Owner.__add__",))
        self.assertEqual(current.forbidden_targets(allowed), ())
        self.assertEqual(current.forbidden_targets(allowed, intermediate=True),
                         (("pkg.Owner.__add__", 1, 1),))
        class_site = DispatchSite(expression, receiver, frozenset({("class", "pkg.Owner")}),
                                  ("pkg.Owner.__add__",))
        self.assertEqual(class_site.forbidden_targets(allowed), (("pkg.Owner.__add__", 1, 1),))
