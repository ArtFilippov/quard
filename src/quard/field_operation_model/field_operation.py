"""Recognize literal receiver-field operations and conservative receiver aliases."""
from __future__ import annotations

import ast
from dataclasses import dataclass

from quard.field_operation_model.field_operation_model import FieldOperationModel


@dataclass(frozen=True)
class FieldOperation(FieldOperationModel):
    name: str
    kind: str  # write, delete, augment, or setdefault
    location: ast.expr
    value: ast.expr | None = None


from quard.field_operation_model.operation_utils import _builtins, _literal, field_operations
