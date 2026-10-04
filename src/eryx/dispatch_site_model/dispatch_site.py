"""Resolved implicit-call evidence kept separate from call traversal."""
from __future__ import annotations

import ast
from dataclasses import dataclass

from eryx._instance_fields_worker import Binding
from eryx.dispatch_site_model.dispatch_site_model import DispatchSiteModel


@dataclass(frozen=True)
class DispatchSite(DispatchSiteModel):
    """One implicit invocation with its original receiver and possible targets.

    Attributes:
        operation (ast.expr): Source expression being checked.
        receiver (ast.expr): Expression supplying the receiver.
        origins (frozenset[Binding]): Statically known receiver origins.
        candidates (tuple[str, ...]): Possible dispatched methods.
    """

    operation: ast.expr
    receiver: ast.expr
    origins: frozenset[Binding]
    candidates: tuple[str, ...]
