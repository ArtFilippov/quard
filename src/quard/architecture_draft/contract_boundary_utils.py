"""Opt-in Tach draft based on discovered contract ownership, not import approval."""
from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any

from quard._common import QuardError
from quard._policy import ALLOWED_LAYERS
from quard.architecture_draft.boundary_source_utils import (
    PACKAGE_FILE, destination, parse_trees, source_imports,
)
from quard.contract_analysis.contract_utils import module_name
from quard.folder_analysis.folder_ownership import FolderOwnership


STUB_SUFFIX = ".pyi"
MAIN_FOLDER = "main"


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


def _assembly_modules(paths: dict[str, str], ownership: FolderOwnership,
                      names: set[str]) -> set[str]:
    envelopes = {Path(ownership.path_for(contract)).parent / "envelopes"
                 for contract in names}
    return {name for name, path in paths.items()
            if (MAIN_FOLDER in Path(path).parts[2:-1]
                or any(folder == Path(path).parent or folder in Path(path).parents
                       for folder in envelopes))}


def _declared_implementations(ownership: FolderOwnership, owners: dict[str, str]
                              ) -> dict[str, set[str]]:
    implementations: dict[str, set[str]] = {}
    for name, node, path in ownership.all_declarations():
        module = module_name(path)
        if ownership.is_contract(name) or module not in owners:
            continue
        if Path(path).stem.endswith("_utils") or name.rpartition(".")[0] != module:
            continue
        if ownership.owners(name, source=(node, path))[0]:
            implementations.setdefault(module, set()).add(node.name)
    return implementations


def _package_implementations(ownership: FolderOwnership, tree: ast.Module, module: str,
                             implementations: dict[str, set[str]]) -> set[str]:
    exports: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            symbol = alias.asname or alias.name
            source, _, concrete = ownership.provenance(f"{module}.{symbol}").rpartition(".")
            if concrete in implementations.get(source, set()):
                exports.add(symbol)
    return exports


def _reexported_implementations(ownership: FolderOwnership, trees: dict[str, ast.Module],
                                owners: dict[str, str], implementations: dict[str, set[str]]
                                ) -> None:
    for path, tree in trees.items():
        if Path(path).stem != PACKAGE_FILE:
            continue
        if Path(path).suffix == STUB_SUFFIX and str(Path(path).with_suffix(".py")) in trees:
            continue
        if (module := module_name(path)) not in owners:
            continue
        if exported := _package_implementations(ownership, tree, module, implementations):
            implementations.setdefault(module, set()).update(exported)


def _dependencies(name: str, sources: tuple[dict[str, ast.Module], dict[str, str]],
                  ownership: FolderOwnership, *, owners: dict[str, str],
                  assembly: tuple[set[str], dict[str, set[str]]]) -> list[str]:
    allowed: set[str] = set()
    if Path(sources[1][name]).suffix == STUB_SUFFIX:
        return []  # Tach checks source imports, not imports found only in stubs.
    for target in source_imports(name, sources[1][name], sources[0][sources[1][name]]):
        resolved = ownership.provenance(target)
        target_module = destination(target, sources[1]) or destination(resolved, sources[1])
        if target_module is None or target_module == name:
            continue
        assembles = (name in assembly[0] and (target_module in assembly[0]
                      or resolved == target_module and target_module in assembly[1]
                      or resolved.rpartition(".")[2] in assembly[1].get(target_module, set())))
        if ((owners.get(name) is not None and owners.get(name) == owners.get(target_module))
                or ownership.is_contract(resolved) or assembles):
            allowed.add(target_module)
    return sorted(allowed)


def _layer(name: str, layers: dict[str, str]) -> str:
    if name in layers:
        return layers[name]
    parent = next((item for item in sorted(layers, key=len, reverse=True)
                   if name.startswith(item + ".")), None)
    return layers[parent] if parent else "Domain"


def _modules(sources: tuple[dict[str, ast.Module], dict[str, str]],
             ownership: FolderOwnership, *, owners: dict[str, str],
             draft: dict[str, Any], assembly: tuple[set[str], dict[str, set[str]]]
             ) -> list[dict[str, Any]]:
    layers = {module["path"]: module["layer"] for module in draft["modules"]}
    modules: list[dict[str, Any]] = []
    for name in sorted(sources[1]):
        layer = "Bootstrap" if name in assembly[0] else _layer(name, layers)
        forbidden = [other for other in sorted(sources[1]) if other != name
                     and ("Bootstrap" if other in assembly[0] else _layer(other, layers))
                     not in ALLOWED_LAYERS[layer]]
        modules.append({"path": name, "layer": layer, "cannot_depend_on": forbidden,
                        "depends_on": _dependencies(name, sources, ownership,
                                                    owners=owners, assembly=assembly)})
    return modules


def _interfaces(paths: dict[str, str], owners: dict[str, str], public: dict[str, set[str]],
                *, assembly: set[str], implementations: dict[str, set[str]]
                 ) -> list[dict[str, Any]]:
    interfaces: list[dict[str, Any]] = []
    for name in sorted(paths):
        source = ["^" + re.escape(name) + "$"]
        interfaces.append({"from": source, "expose": ["^" + re.escape(symbol) + "$"
                                                    for symbol in sorted(public.get(name, set()))]})
        if assembly and implementations.get(name):
            interfaces.append({"from": source, "visibility": sorted(assembly),
                               "exclusive": True, "expose": ["^" + re.escape(symbol) + "$"
                               for symbol in sorted(implementations[name])]})
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
    assembly = _assembly_modules(paths, ownership, names)
    implementations = _declared_implementations(ownership, owners)
    _reexported_implementations(ownership, trees, owners, implementations)
    draft["modules"] = _modules((trees, paths), ownership, owners=owners, draft=draft,
                                assembly=(assembly, implementations))
    draft["interfaces"] = _interfaces(paths, owners, _public_contracts(trees, ownership, names),
                                      assembly=assembly, implementations=implementations)


def contract_boundaries(root: Path, files: list[str], draft: dict[str, Any],
                        *, validate_ownership: bool = True) -> None:
    """Replace Tach boundaries with discovered contract-owned modules.

    Args:
        root (Path): Selected consumer root.
        files (list[str]): Discovered production source and stub paths.
        draft (dict[str, Any]): Convention-based draft to update in place.
        validate_ownership (bool, default=True): Enforce folder placement in review drafts.

    Raises:
        QuardError: Source parsing or contract folder ownership failed.
    """
    trees = parse_trees(root, files, "CONTRACT_BOUNDARY_SOURCE_ERROR")
    ownership = FolderOwnership(trees)
    if validate_ownership and (violations := ownership.folder_violations()):
        first = violations[0]
        raise QuardError("INVALID_CONTRACT_OWNERSHIP", f"{first['path']}: {first['message']}")
    _render_boundaries(trees, ownership, draft)
