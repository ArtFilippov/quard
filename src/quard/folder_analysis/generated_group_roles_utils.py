"""Prove generated contract roles and local group ownership from declarations."""
from __future__ import annotations

import ast
from pathlib import Path

from quard.architecture_draft.boundary_source_utils import destination, source_imports
from quard.architecture_draft.group_area_utils import MAIN_FOLDER, ORDINARY, area
from quard.contract_analysis.contract_utils import module_name
from quard.folder_analysis.folder_analysis import FolderAnalysis
from quard.folder_analysis.generated_external_contracts import GeneratedOwnership
from quard.folder_analysis.generated_facades_utils import facade_evidence
from quard.folder_analysis.generated_reference_utils import (
    concrete_imports, concrete_references, other_declarations,
)

SOURCE_ROOT = "src"
EMPTY_NAMES: frozenset[str] = frozenset()


def _location(path: str, node: ast.stmt, symbol: str) -> dict[str, object]:
    return {"path": path, "line": node.lineno, "column": node.col_offset + 1,
            "symbol": symbol}


def _mixed(path: str, tree: ast.Module, declared: list[tuple[str, ast.ClassDef]],
           ownership: GeneratedOwnership) -> list[dict[str, object]]:
    if not any(ownership.declaration(name, source=(node, path)) for name, node in declared):
        return []
    results: list[dict[str, object]] = []
    for name, node in declared:
        if not ownership.declaration(name, source=(node, path)):
            results.append({**_location(path, node, node.name), "rule": "MIXED_MODULE_ROLES",
                            "message": f"{name}: move the concrete class to an "
                                       + "infrastructure module."})
    for node in other_declarations(path, tree, ownership):
        results.append({**_location(path, node, getattr(node, "name", "<module>")),
                        "rule": "MIXED_MODULE_ROLES",
                        "message": f"{path}: move this concrete declaration to an "
                                   + "infrastructure module."})
    results.extend(_concrete_aliases(path, tree, ownership))
    return results


def _concrete_aliases(path: str, tree: ast.Module,
                      ownership: FolderAnalysis) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom):
            continue
        for alias in statement.names:
            symbol = alias.asname or alias.name
            target = ownership.provenance(f"{module_name(path)}.{symbol}")
            if ownership.node_for(target) is not None and not ownership.is_contract(target):
                results.append({**_location(path, statement, symbol),
                                "rule": "MIXED_MODULE_ROLES",
                                "message": f"{path}: move the concrete alias {symbol} "
                                           + "to an infrastructure module."})
    return results


def _foreign_contracts(ownership: FolderAnalysis, name: str, node: ast.ClassDef,
                       source: str, group: Path, *, seen: frozenset[str] = EMPTY_NAMES
                       ) -> tuple[set[str], bool]:
    if name in seen:
        return set(), True
    foreign: set[str] = set()
    uncertain = False
    for base in node.bases:
        parent = ownership.resolved(name, base, source)
        ancestor = ownership.node_for(parent)
        if parent is not None and ancestor is not None:
            if ownership.is_contract(parent) and Path(ownership.path_for(parent)).parent != group:
                foreign.add(parent)
            inherited, unknown = _foreign_contracts(ownership, parent, ancestor,
                                                     ownership.path_for(parent), group,
                                                     seen=seen | {name})
            foreign.update(inherited)
            uncertain |= unknown
        elif parent is None or ownership.local_module(parent):
            uncertain = True
    return foreign, uncertain


def _implementation(path: str, name: str, node: ast.ClassDef,
                    ownership: FolderAnalysis) -> list[dict[str, object]]:
    if ownership.declaration(name, source=(node, path)) or area(path)[1] != ORDINARY:
        return []
    foreign, uncertain = _foreign_contracts(ownership, name, node, path, Path(path).parent)
    results: list[dict[str, object]] = []
    if uncertain:
        results.append({**_location(path, node, node.name), "rule": "UNRESOLVED_GROUP_OWNER",
                        "message": f"{name}: resolve local bases before verifying group ownership."})
    direct = [ownership.resolved(name, base, path) for base in node.bases]
    local = [item for item in direct if item is not None and ownership.is_contract(item)
             and Path(ownership.path_for(item)).parent == Path(path).parent]
    covered: set[str] = set()
    for item in local:
        covered.update(ownership.contract_ancestors(item))
    if missing := foreign - covered:
        results.append({**_location(path, node, node.name), "rule": "CROSS_GROUP_CONTRACT",
                        "message": f"{name}: declare and inherit a combined contract in "
                                   + f"this group covering {', '.join(sorted(missing))}."})
    return results


def _concrete_bases(path: str, name: str, node: ast.ClassDef,
                    ownership: GeneratedOwnership) -> list[dict[str, object]]:
    if area(path)[1] != ORDINARY:
        return []
    results: list[dict[str, object]] = []
    for base in node.bases:
        parent = ownership.resolved(name, base, path)
        if ownership.node_for(parent) is None or ownership.is_contract(parent):
            continue
        results.append({**_location(path, base, node.name),
                        "rule": "CONCRETE_IMPLEMENTATION_BASE",
                        "message": f"{name}: {parent} is a concrete implementation; "
                                   + "inherit a contract instead."})
    return results


def _stub_role(path: str, name: str, node: ast.ClassDef,
               declarations: dict[str, list[tuple[str, ast.ClassDef]]],
               ownership: GeneratedOwnership) -> list[dict[str, object]]:
    if not path.endswith(".pyi"):
        return []
    companion = str(Path(path).with_suffix(".py"))
    if (not any(item == name for item, _ in declarations.get(companion, []))
            or name not in ownership.role_conflicts):
        return []
    return [{**_location(path, node, node.name), "rule": "CONFLICTING_MODULE_ROLE",
             "message": f"{name}: source and stub must agree on contract role."}]


def _value_imports(path: str, tree: ast.Module, ownership: GeneratedOwnership
                   ) -> list[dict[str, object]]:
    if area(path)[1] != ORDINARY:
        return []
    concrete = {f"{module_name(source)}.{value.declaration.name}"
                for source, evidence in ownership.value_evidence.items()
                for value in evidence if value.declaration is not None}
    concrete_modules = {name.rpartition(".")[0] for name in concrete}
    results: list[dict[str, object]] = []
    for statement in ast.walk(tree):
        if not isinstance(statement, (ast.ImportFrom, ast.Import)):
            continue
        targets: list[tuple[str, str]] = []
        if isinstance(statement, ast.ImportFrom):
            module = module_name(path)
            package = module if Path(path).stem == "__init__" else module.rpartition(".")[0]
            parts = package.split(".")
            prefix = parts[:len(parts) - statement.level + 1] if statement.level else []
            origin = ".".join([*prefix, statement.module] if statement.module else prefix)
            targets.extend((f"{origin}.{alias.name}", alias.asname or alias.name)
                           for alias in statement.names)
        elif isinstance(statement, ast.Import):
            targets.extend((alias.name, alias.asname or alias.name) for alias in statement.names)
        for imported, symbol in targets:
            target = ownership.provenance(imported)
            if (target in concrete or target in concrete_modules) and module_name(path) != (
                    target.rpartition(".")[0] if target in concrete else target):
                results.append({**_location(path, statement, symbol),
                                "rule": "CONCRETE_VALUE_DEPENDENCY",
                                "message": f"{path}: {target} is a concrete value dependency; "
                                           + "import its explicit contract instead."})
    return results


def _bootstrap_imports(path: str, tree: ast.Module,
                       sources: dict[str, str]) -> list[dict[str, object]]:
    """Check source, typing and stub imports into Bootstrap.

    Args:
        path (str): Relative production file path.
        tree (ast.Module): Parsed source or stub.
        sources (dict[str, str]): Local module names and their source paths.

    Returns:
        list[dict[str, object]]: Positioned forbidden dependencies.
    """
    name = module_name(path)
    results: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for target in source_imports(name, path, ast.Module(body=[node], type_ignores=[])):
            module = destination(target, sources)
            if module is None or module == name or area(sources[module])[1] != MAIN_FOLDER:
                continue
            results.append({**_location(path, node, target), "rule": "BOOTSTRAP_DEPENDENCY",
                            "message": f"{path}: {target} belongs to Bootstrap; "
                                       + "Bootstrap modules cannot be dependencies."})
    return results


def analyze(trees: dict[str, ast.Module], ownership: GeneratedOwnership) -> list[dict[str, object]]:
    """Check production modules and their companion stubs for proven roles.

    Args:
        trees (dict[str, ast.Module]): Parsed discovered source and test files.
        ownership (GeneratedOwnership): Resolved class declarations and bases.

    Returns:
        list[dict[str, object]]: Positioned generated-policy findings.
    """
    declarations: dict[str, list[tuple[str, ast.ClassDef]]] = {}
    for name, node, path in ownership.all_declarations():
        if Path(path).parts[0] == SOURCE_ROOT:
            declarations.setdefault(path, []).append((name, node))
    results: list[dict[str, object]] = []
    results.extend(ownership.alias_violations)
    results.extend(facade_evidence(trees, ownership)[1])
    sources = {module_name(path): path for path in sorted(trees)
               if Path(path).parts[0] == SOURCE_ROOT}
    for path, tree in sorted(trees.items()):
        if Path(path).parts[0] != SOURCE_ROOT:
            continue
        declared = declarations.get(path, [])
        results.extend(_bootstrap_imports(path, tree, sources))
        results.extend(_value_imports(path, tree, ownership))
        results.extend(concrete_imports(path, tree, trees, ownership))
        results.extend(concrete_references(path, tree, ownership))
        for value in ownership.value_evidence.get(path, []):
            if value.declaration is None:
                results.append({**_location(path, value.assignment,
                                             ast.unparse(value.assignment.targets[0])),
                                "rule": "UNRESOLVED_VALUE_FACTORY",
                                "message": f"{path}: value factory identity or fields are unproved."})
                continue
            companion = str(Path(path).with_suffix(".py")) if path.endswith(".pyi") else str(
                Path(path).with_suffix(".pyi"))
            peers = {item.declaration.name: item for item in
                     ownership.value_evidence.get(companion, []) if item.declaration}
            companion_class = any(isinstance(item, ast.ClassDef) and
                                  item.name == value.declaration.name for item in
                                  trees[companion].body) if companion in trees else False
            if companion_class:
                results.append({**_location(path, value.assignment, value.declaration.name),
                                "rule": "CONFLICTING_VALUE_FACTORY",
                                "message": f"{value.declaration.name}: source and stub must "
                                           + "agree on value factory evidence."})
            if (companion in trees and (peer := peers.get(value.declaration.name)) is not None
                    and peer.declaration is not None):
                if (peer.identity != value.identity or
                        ast.dump(ast.Module(body=peer.declaration.body, type_ignores=[])) !=
                        ast.dump(ast.Module(body=value.declaration.body, type_ignores=[]))):
                    results.append({**_location(path, value.assignment, value.declaration.name),
                                    "rule": "CONFLICTING_VALUE_FACTORY",
                                    "message": f"{value.declaration.name}: source and stub value "
                                               + "factory evidence must agree."})
        results.extend(_mixed(path, tree, declared, ownership))
        for name, node in declared:
            if ownership.conflicting_base(name, path):
                results.append({**_location(path, node, node.name),
                                "rule": "UNRESOLVED_EXTERNAL_CONTRACT",
                                "message": f"{name}: external base evidence conflicts or is unproved "
                                           + "in source or stub."})
            results.extend(_stub_role(path, name, node, declarations, ownership))
            results.extend(_concrete_bases(path, name, node, ownership))
            results.extend(_implementation(path, name, node, ownership))
    return results
