"""Prove generated contract roles and local group ownership from declarations."""
from __future__ import annotations

import ast
from pathlib import Path

from quard.architecture_draft.group_area_utils import ORDINARY, area
from quard.contract_analysis.contract_utils import module_name
from quard.folder_analysis.folder_analysis import FolderAnalysis

SOURCE_ROOT = "src"
EXPORTS = "__all__"
DECLARATION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign, ast.AugAssign)
EMPTY_NAMES: frozenset[str] = frozenset()


def _location(path: str, node: ast.stmt, symbol: str) -> dict[str, object]:
    return {"path": path, "line": node.lineno, "column": node.col_offset + 1,
            "symbol": symbol}


def other_declarations(path: str, tree: ast.Module,
                       ownership: FolderAnalysis) -> list[ast.stmt]:
    """Find definitions incompatible with a contract-only module.

    Args:
        path (str): Source or stub path.
        tree (ast.Module): Parsed module.
        ownership (FolderAnalysis): Local declaration index.

    Returns:
        list[ast.stmt]: Concrete definitions in the module.
    """
    declarations: list[ast.stmt] = []
    for node in tree.body:
        if not isinstance(node, DECLARATION):
            continue
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            name = node.targets[0].id
            if (name == EXPORTS or ownership.is_contract(ownership.provenance(
                    f"{module_name(path)}.{name}"))):
                continue
        declarations.append(node)
    return declarations


def _mixed(path: str, tree: ast.Module, declared: list[tuple[str, ast.ClassDef]],
           ownership: FolderAnalysis) -> list[dict[str, object]]:
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


def _stub_role(path: str, name: str, node: ast.ClassDef,
               declarations: dict[str, list[tuple[str, ast.ClassDef]]],
               ownership: FolderAnalysis) -> list[dict[str, object]]:
    if not path.endswith(".pyi"):
        return []
    companion = str(Path(path).with_suffix(".py"))
    if (not any(item == name for item, _ in declarations.get(companion, []))
            or ownership.is_contract(name) == ownership.declaration(name, source=(node, path))):
        return []
    return [{**_location(path, node, node.name), "rule": "CONFLICTING_MODULE_ROLE",
             "message": f"{name}: source and stub must agree on contract role."}]


def analyze(trees: dict[str, ast.Module], ownership: FolderAnalysis) -> list[dict[str, object]]:
    """Check production modules and their companion stubs for proven roles.

    Args:
        trees (dict[str, ast.Module]): Parsed discovered source and test files.
        ownership (FolderAnalysis): Resolved class declarations and bases.

    Returns:
        list[dict[str, object]]: Positioned generated-policy findings.
    """
    declarations: dict[str, list[tuple[str, ast.ClassDef]]] = {}
    for name, node, path in ownership.all_declarations():
        if Path(path).parts[0] == SOURCE_ROOT:
            declarations.setdefault(path, []).append((name, node))
    results: list[dict[str, object]] = []
    for path, tree in sorted(trees.items()):
        if Path(path).parts[0] != SOURCE_ROOT:
            continue
        declared = declarations.get(path, [])
        results.extend(_mixed(path, tree, declared, ownership))
        for name, node in declared:
            results.extend(_stub_role(path, name, node, declarations, ownership))
            results.extend(_implementation(path, name, node, ownership))
    return results
