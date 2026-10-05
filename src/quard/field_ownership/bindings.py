"""Shared names and builtin bindings for field owners and access sites."""
from __future__ import annotations

from quard.field_ownership.bindings_utils import assigned_names, module_name

Binding = tuple[str, str]  # (class | instance | module | builtins | builtin, name)
PROTOCOL_BUILTINS = frozenset({"len", "str", "repr", "ascii", "format", "int", "float",
                               "complex", "bin", "oct", "hex", "abs", "round", "hash",
                               "iter", "next", "list", "tuple", "any", "all"})
