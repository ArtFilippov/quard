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
from quard.folder_analysis.generated_facades_utils import facade_evidence

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


def _contract_only(path: str, tree: ast.Module, ownership: GeneratedOwnership,
                   contracts: set[str]) -> bool:
    if any(source == path for _, _, source in ownership.all_declarations()):
        if not all(ownership.declaration(item, source=(node, path)) for item, node, source
                   in ownership.all_declarations() if source == path):
            return False
        from quard.folder_analysis.generated_group_roles_utils import other_declarations

        module = module_name(path)
        concrete_alias = any((target := ownership.provenance(
                                 f"{module}.{alias.asname or alias.name}"))
                             in ownership.nodes and target not in contracts
                             for statement in tree.body if isinstance(statement, ast.ImportFrom)
                             for alias in statement.names)
        return not concrete_alias and not other_declarations(path, tree, ownership)
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
    return all(ownership.export_contract(f"{name}.{alias}") for alias in imports)


def _role(path: str, tree: ast.Module, ownership: GeneratedOwnership,
          contracts: set[str]) -> str:
    kind = area(path)[1]
    role = "Infrastructure"
    if kind == MAIN_FOLDER:
        role = "Bootstrap"
    elif kind == ENVELOPES_FOLDER:
        role = "Envelopes"
    elif _contract_only(path, tree, ownership, contracts):
        role = "Contract"
    return role


def _aliases(path: str, tree: ast.Module, ownership: GeneratedOwnership) -> set[str]:
    module = module_name(path)
    return {alias.asname or alias.name for node in tree.body
            if isinstance(node, ast.ImportFrom) for alias in node.names
            if ownership.export_contract(
                f"{module}.{alias.asname or alias.name}")}


def _exports(trees: dict[str, ast.Module], ownership: GeneratedOwnership
             ) -> dict[str, dict[str, set[str]]]:
    exports: dict[str, dict[str, set[str]]] = {}
    facades, _ = facade_evidence(trees, ownership)
    for qualified, node, path in ownership.all_declarations():
        module = module_name(path)
        if Path(path).stem == PACKAGE_FILE:
            continue
        kind = CONTRACT_EXPORT if ownership.is_contract(qualified) else CONCRETE_EXPORT
        if kind == CONTRACT_EXPORT or qualified.rpartition(".")[0] == module:
            exports.setdefault(module, {}).setdefault(kind, set()).add(node.name)
    for path, tree in trees.items():
        if Path(path).stem == PACKAGE_FILE:
            continue
        if aliases := _aliases(path, tree, ownership):
            exports.setdefault(module_name(path), {}).setdefault(CONTRACT_EXPORT, set()).update(
                aliases)
    for module, names in facades.items():
        exports.setdefault(module, {}).setdefault(CONTRACT_EXPORT, set()).update(names)
    return exports


def _eligible(name: str, reference: tuple[str, str, str], ownership: GeneratedOwnership,
               areas: dict[str, tuple[str, str]], *, exports: dict[str, dict[str, set[str]]],
               facades: set[str]
               ) -> bool:
    target_module, target, resolved = reference
    symbol = target.removeprefix(target_module + ".").partition(".")[0]
    target_exports = exports.get(target_module, {})
    if target_module in facades:
        return symbol in target_exports.get(CONTRACT_EXPORT, set())
    if ownership.is_contract(resolved) or symbol in target_exports.get(CONTRACT_EXPORT, set()):
        return True
    assembly = areas[name][1] in {MAIN_FOLDER, ENVELOPES_FOLDER}
    return (assembly and areas[target_module][1] != MAIN_FOLDER
            and symbol in target_exports.get(CONCRETE_EXPORT, set()))


def _dependencies(name: str, sources: dict[str, tuple[str, ast.Module]],
                  ownership: GeneratedOwnership, areas: dict[str, tuple[str, str]],
                  *, exports: dict[str, dict[str, set[str]]], facades: set[str]) -> list[str]:
    allowed: set[str] = set()
    if sources[name][0].endswith(".py"):
        for target in source_imports(name, *sources[name]):
            resolved = ownership.provenance(target)
            target_module = destination(target, sources) or destination(resolved, sources)
            if target_module is None or target_module == name:
                continue
            if _eligible(name, (target_module, target, resolved), ownership, areas,
                         exports=exports, facades=facades):
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


def _roles(sources: dict[str, tuple[str, ast.Module]], ownership: GeneratedOwnership,
           trees: dict[str, ast.Module]
           ) -> dict[str, str]:
    contracts = {name for name, _, _ in ownership.all_declarations()
                 if ownership.is_contract(name)}
    invalid = {item["path"] for item in facade_evidence(trees, ownership)[1]}
    return {name: ("Infrastructure" if path in invalid else "Contract")
            if Path(path).stem == PACKAGE_FILE and area(path)[1] == ORDINARY else
            _role(path, tree, ownership, contracts)
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
                        "depends_on": _dependencies(name, sources, ownership, areas,
                                                    exports=exports, facades=facades),
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
