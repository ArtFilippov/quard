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
from quard.architecture_draft.group_area_utils import (
    ENVELOPES_FOLDER, MAIN_FOLDER, ORDINARY, area,
)
from quard.contract_analysis.contract_utils import module_name
from quard.folder_analysis.generated_external_contracts import GeneratedOwnership
from quard.folder_analysis.generated_facades_utils import alias_exports, facade_evidence
from quard.folder_analysis.generated_reference_utils import contract_only

CONTRACT_EXPORT = "contract"
CONCRETE_EXPORT = "concrete"
ROLES = ["Bootstrap", "Envelopes", "Infrastructure", "Contract"]
CONTRACT_ROLE = "Contract"
BOOTSTRAP_ROLE = "Bootstrap"
ALLOWED_ROLES = {
    "Contract": {"Contract"},
    "Infrastructure": {"Contract"},
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


def _role(path: str, tree: ast.Module, ownership: GeneratedOwnership,
          contracts: set[str]) -> str:
    kind = area(path)[1]
    role = "Infrastructure"
    if kind == MAIN_FOLDER:
        role = "Bootstrap"
    elif kind == ENVELOPES_FOLDER:
        role = "Envelopes"
    elif contract_only(path, tree, ownership, contracts):
        role = "Contract"
    return role


def _aliases(path: str, tree: ast.Module, ownership: GeneratedOwnership) -> set[str]:
    module = module_name(path)
    return {alias.asname or alias.name for node in tree.body
            if isinstance(node, ast.ImportFrom) for alias in node.names
            if f"{module}.{alias.asname or alias.name}" not in ownership.blocked_aliases
            and ownership.export_contract(
                f"{module}.{alias.asname or alias.name}")}


def _exports(trees: dict[str, ast.Module], ownership: GeneratedOwnership
             ) -> dict[str, dict[str, set[str]]]:
    exports: dict[str, dict[str, set[str]]] = {}
    facades, _ = facade_evidence(trees, ownership)
    selected = _sources(trees)
    for qualified, node, path in ownership.all_declarations():
        module = module_name(path)
        if Path(path).stem == PACKAGE_FILE or selected[module][0] != path:
            continue
        kind = CONTRACT_EXPORT if ownership.is_contract(qualified) else CONCRETE_EXPORT
        if kind == CONTRACT_EXPORT or qualified.rpartition(".")[0] == module:
            exports.setdefault(module, {}).setdefault(kind, set()).add(node.name)
    for module, (path, tree) in selected.items():
        if Path(path).stem == PACKAGE_FILE:
            continue
        if aliases := _aliases(path, tree, ownership) | alias_exports(path, tree, ownership):
            exports.setdefault(module, {}).setdefault(CONTRACT_EXPORT, set()).update(aliases)
        functions = {node.name for node in tree.body if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if functions:
            exports.setdefault(module, {}).setdefault(CONCRETE_EXPORT, set()).update(functions)
    for module, names in facades.items():
        exports.setdefault(module, {}).setdefault(CONTRACT_EXPORT, set()).update(names)
    return exports


def _eligible(name: str, reference: tuple[str, str, str], ownership: GeneratedOwnership,
                *, exports: dict[str, dict[str, set[str]]], facades: set[str],
                roles: dict[str, str]
                ) -> bool:
    target_module, target, resolved = reference
    if target in ownership.blocked_aliases:
        return False
    if roles[target_module] not in ALLOWED_ROLES[roles[name]]:
        return False
    if target == target_module:
        # A module-level Infrastructure import can access its private utilities.
        # Assembly imports the proven concrete symbol from its defining module.
        return roles[target_module] == CONTRACT_ROLE
    symbol = target.removeprefix(target_module + ".").partition(".")[0]
    target_exports = exports.get(target_module, {})
    if target_module in facades:
        return (roles[target_module] == CONTRACT_ROLE and
                symbol in target_exports.get(CONTRACT_EXPORT, set()))
    if (roles[target_module] == CONTRACT_ROLE and
            (ownership.is_contract(resolved) or
             symbol in target_exports.get(CONTRACT_EXPORT, set()))):
        return True
    assembly = roles[name] in {"Bootstrap", "Envelopes"}
    return (assembly and roles[target_module] != BOOTSTRAP_ROLE
            and symbol in target_exports.get(CONCRETE_EXPORT, set()))


def _dependencies(name: str, sources: dict[str, tuple[str, ast.Module]],
                   ownership: GeneratedOwnership, *, exports: dict[str, dict[str, set[str]]],
                   facades: set[str], roles: dict[str, str]) -> list[str]:
    allowed: set[str] = set()
    if sources[name][0].endswith(".py"):
        for target in source_imports(name, *sources[name]):
            resolved = ownership.provenance(target)
            target_module = destination(target, sources) or destination(resolved, sources)
            if target_module is None or target_module == name:
                continue
            if _eligible(name, (target_module, target, resolved), ownership,
                         exports=exports, facades=facades, roles=roles):
                allowed.add(target_module)
    return sorted(allowed)


def _interfaces(name: str, areas: dict[str, tuple[str, str]],
                 exports: dict[str, dict[str, set[str]]], assembly: set[str],
                 *, roles: dict[str, str]
                 ) -> list[dict[str, Any]]:
    source = ["^" + re.escape(name) + "$"]
    public = exports.get(name, {})
    interfaces: list[dict[str, Any]] = [{
        "from": source, "expose": ["^" + re.escape(symbol) + "$"
                                   for symbol in sorted(public.get(CONTRACT_EXPORT, set()))]
        if roles[name] == CONTRACT_ROLE else []}]
    if assembly and public.get(CONCRETE_EXPORT):
        viewers = sorted(viewer for viewer in assembly if viewer != name and not (
            areas[viewer][1] == ENVELOPES_FOLDER and areas[name][1] == MAIN_FOLDER))
        if viewers:
            interfaces.append({"from": source, "visibility": viewers, "exclusive": True,
                               "expose": ["^" + re.escape(symbol) + "$"
                                          for symbol in sorted(public[CONCRETE_EXPORT])]})
    return interfaces


def _roles(sources: dict[str, tuple[str, ast.Module]], ownership: GeneratedOwnership,
           trees: dict[str, ast.Module]
           ) -> dict[str, str]:
    contracts = {name for name, _, _ in ownership.all_declarations()
                 if ownership.is_contract(name)}
    invalid = {module_name(str(item["path"])) for item in facade_evidence(trees, ownership)[1]}
    invalid.update(module_name(str(item["path"])) for item in ownership.alias_violations)
    invalid.update(module_name(path) for name, _, path in ownership.all_declarations()
                   if name in ownership.role_conflicts)
    return {name: ("Infrastructure" if name in invalid else
                   "Contract" if Path(path).stem == PACKAGE_FILE else
                   _role(path, tree, ownership, contracts))
            if area(path)[1] == ORDINARY else _role(path, tree, ownership, contracts)
            for name, (path, tree) in sources.items()}


def _boundaries(trees: dict[str, ast.Module], ownership: GeneratedOwnership,
                sources: dict[str, tuple[str, ast.Module]]
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    areas = {name: area(path) for name, (path, _) in sources.items()}
    roles = _roles(sources, ownership, trees)
    exports = _exports(trees, ownership)
    facades = {name for name, (path, _) in sources.items() if Path(path).stem == PACKAGE_FILE}
    assembly = {name for name in sources if areas[name][1] != ORDINARY}
    modules: list[dict[str, Any]] = []
    interfaces: list[dict[str, Any]] = []
    for name in sorted(sources):
        modules.append({"path": name, "layer": roles[name],
                         "depends_on": _dependencies(name, sources, ownership,
                                                     exports=exports, facades=facades,
                                                     roles=roles),
                        "cannot_depend_on": sorted(other for other in sources if other != name
                                                    and roles[other] not in ALLOWED_ROLES[
                                                        roles[name]])})
        interfaces.extend(_interfaces(name, areas, exports, assembly, roles=roles))
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
    modules, interfaces = _boundaries(trees, GeneratedOwnership(trees), _sources(trees))
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
