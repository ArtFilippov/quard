"""Join syntactic origins and resolve builtins and module paths."""
from __future__ import annotations

import builtins
from pathlib import Path

from eryx.constructor_origins.constructor_origins import Environment, Origin

UNKNOWN = frozenset({"unknown"})


def merge(*environments: Environment) -> Environment:
    """Missing bindings at a join are unknown, not evidence of a builtin."""
    return {name: frozenset().union(*(env.get(name, UNKNOWN) for env in environments))
            for name in set().union(*(set(env) for env in environments))}


def builtin(name: str) -> Origin:
    value = vars(builtins).get(name)
    if isinstance(value, type) and name != "super":
        return frozenset({f"type:{name}"})
    if name in {"isinstance", "issubclass", "hasattr", "callable", "super"}:
        return frozenset({f"builtin:{name}"})
    return UNKNOWN


def module_name(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)
