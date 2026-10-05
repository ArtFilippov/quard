"""Opt-in Tach draft based on discovered contract ownership, not import approval."""
from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any

from eryx._common import EryxError
from eryx._policy import ALLOWED_LAYERS
from eryx.contract_analysis.contract_utils import module_name
from eryx.folder_analysis.folder_ownership import FolderOwnership


PACKAGE_FILE = "__init__"
STUB_SUFFIX = ".pyi"


def _trees(root: Path, files: list[str]) -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for path in files:
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise EryxError("CONTRACT_BOUNDARY_SOURCE_ERROR", f"{path}: {exc}") from exc
    return trees


def _owners(paths: dict[str, str], ownership: FolderOwnership,
            names: set[str]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for name, path in paths.items():
        parent = Path(path).parent
        folders = [contract for contract in names
                   if Path(ownership.path_for(contract)).parent == parent]
        if folders:
            owners[name] = min(folders)
            continue
        enclosing = [contract for contract in names
                     if Path(ownership.path_for(contract)).parent in parent.parents]
        if enclosing:
            owners[name] = max(enclosing, key=lambda item: len(
                Path(ownership.path_for(item)).parts))
    return owners


def _package_contracts(module: str, tree: ast.Module, ownership: FolderOwnership,
                       names: set[str]) -> set[str]:
    exported: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            symbol = alias.asname or alias.name
            if ownership.provenance(f"{module}.{symbol}") in names:
                exported.add(symbol)
    return exported


def _public_contracts(trees: dict[str, ast.Module], ownership: FolderOwnership,
                      names: set[str]) -> dict[str, set[str]]:
    contracts: dict[str, set[str]] = {}
    for contract in sorted(names):
        module = module_name(ownership.path_for(contract))
        symbol = contract.split(".")[-1]
        contracts.setdefault(module, set()).add(symbol)
    for path, tree in trees.items():
        if Path(path).stem != PACKAGE_FILE:
            continue
        if Path(path).suffix == STUB_SUFFIX and str(Path(path).with_suffix(".py")) in trees:
            continue
        module = module_name(path)
        contracts.setdefault(module, set()).update(_package_contracts(module, tree, ownership,
                                                                       names))
    return contracts


def _imports(name: str, path: str, tree: ast.Module) -> list[str]:
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


def _destination(path: str, paths: dict[str, str]) -> str | None:
    return next((module for module in sorted(paths, key=len, reverse=True)
                 if path == module or path.startswith(module + ".")), None)


def _dependencies(name: str, *, trees: dict[str, ast.Module], paths: dict[str, str],
                  ownership: FolderOwnership, owners: dict[str, str]) -> list[str]:
    allowed: set[str] = set()
    for target in _imports(name, paths[name], trees[paths[name]]):
        resolved = ownership.provenance(target)
        destination = _destination(target, paths) or _destination(resolved, paths)
        if destination is None or destination == name:
            continue
        if ((owners.get(name) is not None and owners.get(name) == owners.get(destination))
                or ownership.is_contract(resolved)):
            allowed.add(destination)
    return sorted(allowed)


def _layer(name: str, layers: dict[str, str]) -> str:
    if name in layers:
        return layers[name]
    parent = next((item for item in sorted(layers, key=len, reverse=True)
                   if name.startswith(item + ".")), None)
    return layers[parent] if parent else "Domain"


def _modules(paths: dict[str, str], *, trees: dict[str, ast.Module],
             ownership: FolderOwnership, owners: dict[str, str],
             draft: dict[str, Any]) -> list[dict[str, Any]]:
    layers = {module["path"]: module["layer"] for module in draft["modules"]}
    modules: list[dict[str, Any]] = []
    for name in sorted(paths):
        layer = _layer(name, layers)
        forbidden = [other for other in sorted(paths) if other != name
                     and _layer(other, layers) not in ALLOWED_LAYERS[layer]]
        modules.append({"path": name, "layer": layer, "cannot_depend_on": forbidden,
                        "depends_on": _dependencies(name, trees=trees, paths=paths,
                                                    ownership=ownership, owners=owners)})
    return modules


def _interfaces(paths: dict[str, str], owners: dict[str, str], public: dict[str, set[str]]
                ) -> list[dict[str, Any]]:
    interfaces: list[dict[str, Any]] = []
    for name in sorted(paths):
        source = ["^" + re.escape(name) + "$"]
        interfaces.append({"from": source, "expose": ["^" + re.escape(symbol) + "$"
                                                   for symbol in sorted(public.get(name, set()))]})
        peers = sorted(peer for peer, owner in owners.items() if peer != name
                       and owner == owners[name]) if name in owners else []
        if peers:
            interfaces.append({"from": source, "visibility": peers,
                               "exclusive": True, "expose": [".*"]})
    return interfaces


def _render_boundaries(trees: dict[str, ast.Module], ownership: FolderOwnership,
                       draft: dict[str, Any]) -> None:
    paths: dict[str, str] = {}
    for path in sorted(trees):
        _ = paths.setdefault(module_name(path), path)
    names = {name for name, _, _ in ownership.all_declarations() if ownership.is_contract(name)}
    owners = _owners(paths, ownership, names)
    draft["modules"] = _modules(paths, trees=trees, ownership=ownership,
                                owners=owners, draft=draft)
    draft["interfaces"] = _interfaces(paths, owners, _public_contracts(trees, ownership, names))


def contract_boundaries(root: Path, files: list[str], draft: dict[str, Any],
                        *, validate_ownership: bool = True) -> None:
    """Replace Tach boundaries with discovered contract-owned modules.

    Args:
        root (Path): Selected consumer root.
        files (list[str]): Discovered production source and stub paths.
        draft (dict[str, Any]): Convention-based draft to update in place.
        validate_ownership (bool, default=True): Enforce folder placement in review drafts.

    Raises:
        EryxError: Source parsing or contract folder ownership failed.
    """
    trees = _trees(root, files)
    ownership = FolderOwnership(trees)
    if validate_ownership and (violations := ownership.folder_violations()):
        first = violations[0]
        raise EryxError("INVALID_CONTRACT_OWNERSHIP", f"{first['path']}: {first['message']}")
    _render_boundaries(trees, ownership, draft)
