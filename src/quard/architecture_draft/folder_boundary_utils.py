"""Generate file-level Tach boundaries from production directory ownership."""
from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any

from quard._policy import discover_files, exclude_globs, read_scope
from quard.architecture_draft.boundary_source_utils import (
    PACKAGE_FILE, destination, parse_trees, source_imports,
)
from quard.contract_analysis.contract_utils import module_name
from quard.folder_analysis.folder_ownership import FolderOwnership

MAIN_FOLDER = "main"
ENVELOPES_FOLDER = "envelopes"
ORDINARY = "ordinary"
EXPORTS = "__all__"
CONTRACT_EXPORT = "contract"
CONCRETE_EXPORT = "concrete"
ROLES = ["Bootstrap", "Envelopes", "Infrastructure", "Contract"]
ALLOWED_ROLES = {
    "Contract": {"Contract"},
    "Infrastructure": {"Contract", "Infrastructure"},
    "Envelopes": {"Contract", "Infrastructure", "Envelopes"},
    "Bootstrap": {"Contract", "Infrastructure", "Envelopes"},
}


def _sources(trees: dict[str, ast.Module]) -> dict[str, tuple[str, ast.Module]]:
    sources: dict[str, tuple[str, ast.Module]] = {}
    for path in sorted(trees):
        name = module_name(path)
        if name not in sources or path.endswith(".py"):
            sources[name] = (path, trees[path])
    return sources


def _area(path: str) -> tuple[str, str]:
    """Identify the nearest group and its assembly directory.

    Args:
        path (str): Relative Python source or stub path.

    Returns:
        tuple[str, str]: Group identity and assembly kind.
    """
    directories = Path(path).parts[1:-1]
    if MAIN_FOLDER in directories:
        return ".".join(directories[:directories.index(MAIN_FOLDER) + 1]), MAIN_FOLDER
    if len(directories) > 1 and directories[-1] == ENVELOPES_FOLDER:
        return ".".join(directories[:-1]), ENVELOPES_FOLDER
    return ".".join(directories), ORDINARY


def _contract_only(path: str, tree: ast.Module, ownership: FolderOwnership,
                   contracts: set[str]) -> bool:
    if declared := [name for name, _, source in ownership.all_declarations() if source == path]:
        return all(item in contracts for item in declared)
    imports = [alias.asname or alias.name for node in tree.body
               if isinstance(node, ast.ImportFrom) for alias in node.names]
    other = [node for node in tree.body if not isinstance(node, ast.ImportFrom)
             and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                      and isinstance(node.value.value, str))
             and not (isinstance(node, ast.Assign) and len(node.targets) == 1
                      and isinstance(node.targets[0], ast.Name)
                      and node.targets[0].id == EXPORTS)]
    if other:
        return False
    if not imports:
        return Path(path).stem == PACKAGE_FILE
    name = module_name(path)
    return all(ownership.provenance(f"{name}.{alias}") in contracts for alias in imports)


def _role(path: str, tree: ast.Module, ownership: FolderOwnership,
          contracts: set[str]) -> str:
    kind = _area(path)[1]
    role = "Infrastructure"
    if kind == MAIN_FOLDER:
        role = "Bootstrap"
    elif kind == ENVELOPES_FOLDER:
        role = "Envelopes"
    elif _contract_only(path, tree, ownership, contracts):
        role = "Contract"
    return role


def _aliases(path: str, tree: ast.Module, ownership: FolderOwnership) -> set[str]:
    module = module_name(path)
    return {alias.asname or alias.name for node in tree.body
            if isinstance(node, ast.ImportFrom) for alias in node.names
            if ownership.is_contract(ownership.provenance(
                f"{module}.{alias.asname or alias.name}"))}


def _exports(trees: dict[str, ast.Module], ownership: FolderOwnership
             ) -> dict[str, dict[str, set[str]]]:
    exports: dict[str, dict[str, set[str]]] = {}
    for qualified, node, path in ownership.all_declarations():
        module = module_name(path)
        kind = CONTRACT_EXPORT if ownership.is_contract(qualified) else CONCRETE_EXPORT
        if kind == CONTRACT_EXPORT or qualified.rpartition(".")[0] == module:
            exports.setdefault(module, {}).setdefault(kind, set()).add(node.name)
    for path, tree in trees.items():
        if aliases := _aliases(path, tree, ownership):
            exports.setdefault(module_name(path), {}).setdefault(CONTRACT_EXPORT, set()).update(
                aliases)
    return exports


def _eligible(name: str, reference: tuple[str, str], ownership: FolderOwnership,
              areas: dict[str, tuple[str, str]], *, exports: dict[str, dict[str, set[str]]]
              ) -> bool:
    target_module, resolved = reference
    symbol = resolved.rpartition(".")[2]
    target_exports = exports.get(target_module, {})
    if ownership.is_contract(resolved) or symbol in target_exports.get(CONTRACT_EXPORT, set()):
        return True
    assembly = areas[name][1] in {MAIN_FOLDER, ENVELOPES_FOLDER}
    return (assembly and areas[target_module][1] != MAIN_FOLDER
            and symbol in target_exports.get(CONCRETE_EXPORT, set()))


def _dependencies(name: str, sources: dict[str, tuple[str, ast.Module]],
                  ownership: FolderOwnership, areas: dict[str, tuple[str, str]],
                  *, exports: dict[str, dict[str, set[str]]]) -> list[str]:
    allowed: set[str] = set()
    if sources[name][0].endswith(".py"):
        for target in source_imports(name, *sources[name]):
            resolved = ownership.provenance(target)
            target_module = destination(target, sources) or destination(resolved, sources)
            if target_module is None or target_module == name:
                continue
            if _eligible(name, (target_module, resolved), ownership, areas, exports=exports):
                allowed.add(target_module)
    return sorted(allowed)


def _interfaces(name: str, areas: dict[str, tuple[str, str]],
                exports: dict[str, dict[str, set[str]]], assembly: set[str]
                ) -> list[dict[str, Any]]:
    source = ["^" + re.escape(name) + "$"]
    public = exports.get(name, {})
    interfaces: list[dict[str, Any]] = [{
        "from": source, "expose": ["^" + re.escape(symbol) + "$"
                                   for symbol in sorted(public.get(CONTRACT_EXPORT, set()))]}]
    if assembly and public.get(CONCRETE_EXPORT):
        viewers = sorted(viewer for viewer in assembly if viewer != name and not (
            areas[viewer][1] == ENVELOPES_FOLDER and areas[name][1] == MAIN_FOLDER))
        if viewers:
            interfaces.append({"from": source, "visibility": viewers, "exclusive": True,
                               "expose": ["^" + re.escape(symbol) + "$"
                                          for symbol in sorted(public[CONCRETE_EXPORT])]})
    return interfaces


def _roles(sources: dict[str, tuple[str, ast.Module]], ownership: FolderOwnership
           ) -> dict[str, str]:
    contracts = {name for name, _, _ in ownership.all_declarations()
                 if ownership.is_contract(name)}
    return {name: _role(path, tree, ownership, contracts)
            for name, (path, tree) in sources.items()}


def _boundaries(trees: dict[str, ast.Module], ownership: FolderOwnership,
                sources: dict[str, tuple[str, ast.Module]]
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    areas = {name: _area(path) for name, (path, _) in sources.items()}
    roles = _roles(sources, ownership)
    exports = _exports(trees, ownership)
    assembly = {name for name in sources if areas[name][1] != ORDINARY}
    modules: list[dict[str, Any]] = []
    interfaces: list[dict[str, Any]] = []
    for name in sorted(sources):
        modules.append({"path": name, "layer": roles[name],
                        "depends_on": _dependencies(name, sources, ownership, areas,
                                                    exports=exports),
                        "cannot_depend_on": sorted(other for other in sources if other != name
                                                    and roles[other] not in ALLOWED_ROLES[
                                                        roles[name]])})
        interfaces.extend(_interfaces(name, areas, exports, assembly))
    return modules, interfaces


def folder_boundaries(root: Path) -> dict[str, Any]:
    """Build a deterministic generated policy without accepting forbidden imports.

    Args:
        root (Path): Selected project root.

    Returns:
        dict[str, Any]: Exact Tach configuration for discovered production modules.
    """
    scope = read_scope(root)
    files = discover_files(root, scope)["production"]
    trees = parse_trees(root, files, "GENERATED_ARCHITECTURE_SOURCE_ERROR")
    modules, interfaces = _boundaries(trees, FolderOwnership(trees), _sources(trees))
    config: dict[str, Any] = {"source_roots": ["src"], "exact": True,
                              "forbid_circular_dependencies": True,
                              "ignore_type_checking_imports": False,
                              "layers_explicit_depends_on": True, "root_module": "forbid",
                              "respect_gitignore": False, "layers": ROLES,
                              "modules": modules, "interfaces": interfaces,
                              "rules": {"unused_ignore_directives": "error",
                                        "require_ignore_directive_reasons": "error",
                                        "unused_external_dependencies": "error"}}
    config["exclude"] = exclude_globs(scope) + ["tests/**"] + [
        f"**/{name}/**" for name in scope["test_directory_names"]]
    return config
