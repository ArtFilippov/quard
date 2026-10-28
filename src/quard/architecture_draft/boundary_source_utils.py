"""Shared source and import discovery for generated and reviewed Tach policies."""
from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path

from quard._common import QuardError

PACKAGE_FILE = "__init__"


def parse_trees(root: Path, files: list[str], code: str) -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for path in files:
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise QuardError(code, f"{path}: {exc}") from exc
    return trees


def source_imports(name: str, path: str, tree: ast.Module) -> list[str]:
    package = name if Path(path).stem == PACKAGE_FILE else name.rpartition(".")[0]
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            prefix = parts[:len(parts) - node.level + 1] if node.level else []
            source = ".".join([*prefix, node.module] if node.module else prefix)
            targets.extend(source + "." + alias.name for alias in node.names)
    return targets


def destination(target: str, paths: Mapping[str, object]) -> str | None:
    prefix = target
    while prefix:
        if prefix in paths:
            return prefix
        prefix = prefix.rpartition(".")[0]
    return None
