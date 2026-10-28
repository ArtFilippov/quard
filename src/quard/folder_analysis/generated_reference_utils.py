"""Reject concrete imports hidden from Tach in generated ordinary modules."""
from __future__ import annotations

import ast
from pathlib import Path

from quard.architecture_draft.group_area_utils import ORDINARY, area
from quard.contract_analysis.contract_utils import dotted, module_name
from quard.folder_analysis.folder_analysis import FolderAnalysis
from quard.folder_analysis.generated_facades_utils import alias_exports

EXPORTS = "__all__"
DECLARATION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign, ast.AugAssign)


def other_declarations(path: str, tree: ast.Module,
                       ownership: FolderAnalysis) -> list[ast.stmt]:
    """Find concrete definitions mixed into a contract module."""
    declarations: list[ast.stmt] = []
    for node in tree.body:
        if not isinstance(node, DECLARATION):
            continue
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            name = node.targets[0].id
            if (name == EXPORTS or ownership.export_contract(
                    f"{module_name(path)}.{name}")):
                continue
        declarations.append(node)
    return declarations


def contract_only(path: str, tree: ast.Module, ownership: FolderAnalysis,
                  contracts: set[str]) -> bool:
    """Prove whether a generated module contains only contracts and contract aliases."""
    if any(source == path for _, _, source in ownership.all_declarations()):
        if not all(ownership.declaration(item, source=(node, path)) for item, node, source
                   in ownership.all_declarations() if source == path):
            return False
        module = module_name(path)
        concrete_alias = any(ownership.node_for(target := ownership.provenance(
                                 f"{module}.{alias.asname or alias.name}")) is not None
                             and target not in contracts
                             for statement in tree.body if isinstance(statement, ast.ImportFrom)
                             for alias in statement.names)
        return not concrete_alias and not other_declarations(path, tree, ownership)
    imports = [alias.asname or alias.name for node in tree.body
                if isinstance(node, ast.ImportFrom) for alias in node.names]
    aliases = alias_exports(path, tree, ownership)
    other = [node for node in tree.body if not isinstance(node, ast.ImportFrom)
              and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                       and isinstance(node.value.value, str))
              and not (isinstance(node, ast.Assign) and len(node.targets) == 1
                       and isinstance(node.targets[0], ast.Name)
                       and (node.targets[0].id == EXPORTS or
                            node.targets[0].id in aliases))]
    if other:
        return False
    if not imports and not aliases:
        return Path(path).stem == "__init__"
    name = module_name(path)
    specialized = {node.value.value.id for node in tree.body
                   if isinstance(node, ast.Assign) and len(node.targets) == 1
                   and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id in aliases
                   and isinstance(node.value, ast.Subscript)
                   and isinstance(node.value.value, ast.Name)}
    return all(ownership.export_contract(f"{name}.{alias}") or alias in specialized
               for alias in imports)


def _imported(path: str, statement: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
    if isinstance(statement, ast.Import):
        return [(alias.name, alias.asname or alias.name) for alias in statement.names]
    module = module_name(path)
    package = module if path.endswith(("/__init__.py", "/__init__.pyi")) else module.rpartition(".")[0]
    parts = package.split(".")
    prefix = parts[:len(parts) - statement.level + 1] if statement.level else []
    origin = ".".join([*prefix, statement.module] if statement.module else prefix)
    return [(f"{origin}.{alias.name}", alias.asname or alias.name)
            for alias in statement.names]


def concrete_modules(trees: dict[str, ast.Module], ownership: FolderAnalysis) -> set[str]:
    """Index concrete source modules once for every import in a check.

    Args:
        trees (dict[str, ast.Module]): Discovered source, test and stub syntax.
        ownership (FolderAnalysis): Resolved contract and facade evidence.

    Returns:
        set[str]: Importable concrete modules outside validated package facades.
    """
    paths: dict[str, list[str]] = {}
    for path in trees:
        if path.startswith("src/"):
            paths.setdefault(module_name(path), []).append(path)
    contracts = {name for name, _, _ in ownership.all_declarations()
                 if ownership.is_contract(name)}
    # Package facades are validated independently. Classify all source/stub views
    # together so a stub-only import cannot disagree with Tach's source role.
    return {name for name, files in paths.items()
            if not any(path.endswith(("/__init__.py", "/__init__.pyi")) for path in files)
            and any(area(path)[1] != ORDINARY or
                    not contract_only(path, trees[path], ownership, contracts)
                    for path in files)}


def concrete_imports(path: str, tree: ast.Module, ownership: FolderAnalysis,
                     modules: set[str]) -> list[dict[str, object]]:
    """Check source and stub imports, including those under TYPE_CHECKING.

    Args:
        path (str): Importing source or stub path.
        tree (ast.Module): Its parsed syntax.
        ownership (FolderAnalysis): Resolved contract evidence.
        modules (set[str]): Concrete module names for this check.

    Returns:
        list[dict[str, object]]: Positioned concrete import findings.
    """
    if area(path)[1] != ORDINARY:
        return []
    results: list[dict[str, object]] = []
    for statement in ast.walk(tree):
        if not isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        for imported, symbol in _imported(path, statement):
            target = ownership.provenance(imported)
            if (ownership.node_for(target) is not None and
                    module_name(path) == module_name(ownership.path_for(target))):
                continue
            concrete = (ownership.node_for(target) is not None and
                        not ownership.is_contract(target))
            module_target = target.rpartition(".")[0]
            private_member = (module_target in modules and
                              not ownership.export_contract(target))
            if not concrete and not private_member and target not in modules:
                continue
            results.append({"rule": "CONCRETE_IMPLEMENTATION_IMPORT", "path": path,
                            "line": statement.lineno, "column": statement.col_offset + 1,
                            "symbol": symbol, "message": f"{path}: {target} is concrete; "
                            + "import a contract or move composition to main/envelopes."})
    return results


def concrete_references(path: str, tree: ast.Module,
                        ownership: FolderAnalysis) -> list[dict[str, object]]:
    """Catch proven concrete values in ordinary modules, including colocated classes."""
    if area(path)[1] != ORDINARY:
        return []
    results: list[dict[str, object]] = []

    def visit(node: ast.AST, owner: str | None, shadowed: frozenset[str]) -> None:
        if isinstance(node, ast.ClassDef):
            prefix = owner if owner and ownership.node_for(owner) else module_name(path)
            owner = f"{prefix}.{node.name}"
            shadowed = frozenset()
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            parameters = (*args.posonlyargs, *args.args, *args.kwonlyargs)
            parameters += tuple(arg for arg in (args.vararg, args.kwarg) if arg)
            # A local assignment or argument shadows module-level class evidence.
            shadowed = shadowed | {arg.arg for arg in parameters} | {
                name.id for name in ast.walk(node)
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)}
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            first = (dotted(node) or "").partition(".")[0]
            target = (ownership.resolved(owner or module_name(path), node, path)
                      if first not in shadowed else None)
            if (target != owner and ownership.node_for(target) is not None and
                    not ownership.is_contract(target)):
                results.append({"rule": "CONCRETE_IMPLEMENTATION_REFERENCE", "path": path,
                                "line": node.lineno, "column": node.col_offset + 1,
                                "symbol": ast.unparse(node),
                                "message": f"{path}: reference to {target} requires "
                                           + "main/envelopes; depend on its contract instead."})
        for child in ast.iter_child_nodes(node):
            if (isinstance(node, ast.ClassDef) and child in node.bases or
                    isinstance(node, ast.AnnAssign) and child is node.annotation or
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and
                    child is node.returns or isinstance(child, ast.arg)):
                continue
            visit(child, owner, shadowed)

    visit(tree, None, frozenset())
    return results
