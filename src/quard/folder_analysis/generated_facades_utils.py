"""Prove package exports from both runtime source and companion stubs."""
from __future__ import annotations

import ast
from pathlib import Path

from quard.contract_analysis.contract_utils import dotted, module_name
from quard.folder_analysis.generated_external_contracts import GeneratedOwnership

FACADE_FILE = "__init__"
SOURCE_ROOT = "src"
EXPORTS = "__all__"
FUTURE_MODULE = "__future__"


def _facade(path: str) -> bool:
    return Path(path).stem == FACADE_FILE and Path(path).parts[0] == SOURCE_ROOT


def _bindings(path: str, tree: ast.Module) -> tuple[dict[str, tuple[str | None, ast.stmt]],
                                                    list[tuple[str, ast.stmt]]]:
    module = module_name(path)
    bindings: dict[str, tuple[str | None, ast.stmt]] = {}
    conflicts: list[tuple[str, ast.stmt]] = []
    for node in tree.body:
        entries: list[tuple[str, str | None]] = []
        if isinstance(node, ast.ImportFrom):
            package = module.split(".")
            prefix = package[:len(package) - node.level + 1] if node.level else []
            origin = ".".join([*prefix, node.module] if node.module else prefix)
            entries = [(alias.asname or alias.name, f"{origin}.{alias.name}")
                       for alias in node.names if origin != FUTURE_MODULE]
        elif isinstance(node, ast.Import):
            entries = [(alias.asname or alias.name.split(".")[0],
                        alias.name if alias.asname else alias.name.split(".")[0])
                       for alias in node.names]
        elif isinstance(node, ast.ClassDef):
            entries = [(node.name, f"{module}.{node.name}")]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Name) or target.id == EXPORTS:
                    continue
                value = dotted(node.value) if node.value is not None else None
                first, _, rest = value.partition(".") if value else ("", "", "")
                origin = bindings.get(first, (None, node))[0]
                entries.append((target.id, origin + ("." + rest if rest else "")
                                if origin else None))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entries = [(node.name, None)]
        for symbol, target in entries:
            if symbol in bindings:
                conflicts.append((symbol, node))
                bindings[symbol] = (None, node)
            else:
                bindings[symbol] = (target, node)
    return bindings, conflicts


def _all_values(tree: ast.Module) -> str | None:
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == EXPORTS for target in targets):
            return ast.unparse(node.value)
    return None


def facade_evidence(trees: dict[str, ast.Module], ownership: GeneratedOwnership
                    ) -> tuple[dict[str, set[str]], list[dict[str, object]]]:
    """Prove contract-only package exports.

    Args:
        trees (dict[str, ast.Module]): Parsed production source and stubs.
        ownership (GeneratedOwnership): Proven class and value roles.

    Returns:
        tuple[dict[str, set[str]], list[dict[str, object]]]: Public names and findings.
    """
    facades = {path: tree for path, tree in trees.items() if _facade(path)}
    bindings = {path: _bindings(path, tree) for path, tree in facades.items()}
    by_module = {module_name(path): path for path in sorted(facades, reverse=True)
                 if path.endswith(".py") or not any(
                     module_name(other) == module_name(path) and other.endswith(".py")
                     for other in facades)}
    module_paths = {module_name(path): path for path in sorted(trees, reverse=True)
                    if path.endswith(".py") or not any(
                        module_name(other) == module_name(path) and other.endswith(".py")
                        for other in trees)}
    intermediate = {path: _bindings(path, tree) for path, tree in trees.items()
                    if path not in facades}
    mismatched: set[str] = set()
    for path, (stub_entries, _) in bindings.items():
        if not path.endswith(".pyi"):
            continue
        source = str(Path(path).with_suffix(".py"))
        if source not in bindings:
            continue
        source_entries = bindings[source][0]
        all_conflict = (_all_values(facades[path]) is not None and
                        _all_values(facades[source]) is not None and
                        _all_values(facades[path]) != _all_values(facades[source]))
        for symbol in stub_entries.keys() | source_entries.keys():
            if all_conflict or stub_entries.get(symbol, (None, None))[0] != (
                    source_entries.get(symbol, (None, None))[0]):
                mismatched.add(f"{module_name(path)}.{symbol}")

    def resolve(target: str, seen: frozenset[str]) -> str | None:
        if target in seen or target in mismatched:
            return None
        if ownership.node_for(target) is not None:
            return target
        for module in sorted(by_module, key=len, reverse=True):
            if target.startswith(module + "."):
                symbol, _, suffix = target[len(module) + 1:].partition(".")
                entry = bindings[by_module[module]][0].get(symbol)
                if entry is None or entry[0] is None:
                    return None
                return resolve(entry[0] + ("." + suffix if suffix else ""),
                               seen | {target})
        for module in sorted(module_paths, key=len, reverse=True):
            if not target.startswith(module + "."):
                continue
            symbol = target[len(module) + 1:].partition(".")[0]
            path = module_paths[module]
            stub = str(Path(path).with_suffix(".pyi"))
            if path not in intermediate:
                break
            source_entries, conflicts = intermediate[path]
            if symbol in {name for name, _ in conflicts} or (stub in intermediate and
                    symbol in source_entries and symbol in intermediate[stub][0] and
                    source_entries[symbol][0] != intermediate[stub][0][symbol][0]):
                return None
            break
        return ownership.provenance(target)

    results: list[dict[str, object]] = []
    proven: dict[str, set[str]] = {}
    evidence: dict[str, dict[str, str | None]] = {}

    def violation(path: str, node: ast.stmt, symbol: str, rule: str) -> None:
        results.append({"path": path, "line": node.lineno, "column": node.col_offset + 1,
                        "symbol": symbol, "rule": rule,
                        "message": f"{module_name(path)}.{symbol}: package facades expose only "
                                   + "proven contracts; resolve or remove this export."})

    for path, tree in sorted(facades.items()):
        module = module_name(path)
        entries, conflicts = bindings[path]
        conflict_names = {name for name, _ in conflicts}
        for symbol, node in conflicts:
            violation(path, node, symbol, "CONFLICTING_FACADE_EXPORT")
        all_names: set[str] = set()
        all_node: ast.stmt | None = None
        for node in tree.body:
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and (
                    node.target.id == EXPORTS):
                violation(path, node, EXPORTS, "UNRESOLVED_FACADE_EXPORT")
                continue
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not any(
                    isinstance(target, ast.Name) and target.id == EXPORTS for target in
                    (node.targets if isinstance(node, ast.Assign) else [node.target])):
                continue
            if all_node is not None:
                violation(path, node, EXPORTS, "CONFLICTING_FACADE_EXPORT")
            all_node = node
            value = node.value
            if not isinstance(value, (ast.List, ast.Tuple)) or any(
                    not isinstance(item, ast.Constant) or not isinstance(item.value, str)
                    for item in value.elts):
                violation(path, node, EXPORTS, "UNRESOLVED_FACADE_EXPORT")
            else:
                for item in value.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        all_names.add(item.value)
        names = set(entries) | all_names
        evidence[path] = {}
        for symbol in sorted(names):
            target, node = entries.get(symbol, (None, all_node or ast.Pass(
                lineno=1, col_offset=0)))
            resolved = resolve(target, frozenset()) if target else None
            evidence[path][symbol] = resolved
            if symbol in conflict_names:
                continue
            if resolved is None or not ownership.export_contract(resolved):
                violation(path, node, symbol, "CONCRETE_FACADE_EXPORT" if resolved is not None
                          and ownership.node_for(resolved) is not None
                          else "UNRESOLVED_FACADE_EXPORT")
            else:
                proven.setdefault(module, set()).add(symbol)
    for path in sorted(facades):
        if not path.endswith(".pyi"):
            continue
        source = str(Path(path).with_suffix(".py"))
        if source not in evidence:
            continue
        if ((stub_all := _all_values(facades[path])) is not None and
                (source_all := _all_values(facades[source])) is not None and
                stub_all != source_all):
            violation(path, facades[path].body[0], EXPORTS, "CONFLICTING_FACADE_EXPORT")
            proven.get(module_name(path), set()).difference_update(evidence[path])
        for symbol in sorted(evidence[path].keys() | evidence[source].keys()):
            if evidence[path].get(symbol) == evidence[source].get(symbol) and symbol in evidence[path]:
                continue
            node = bindings[path][0].get(symbol, (None, facades[path].body[0] if
                                                  facades[path].body else ast.Pass(
                                                      lineno=1, col_offset=0)))[1]
            violation(path, node, symbol, "CONFLICTING_FACADE_EXPORT")
            proven.get(module_name(path), set()).discard(symbol)
    blocked = {module_name(str(item["path"])) for item in results}
    while True:
        additional: set[str] = set()
        for path, (entries, _) in bindings.items():
            module = module_name(path)
            if module in blocked:
                continue
            for symbol, (target, node) in entries.items():
                if target is not None and any(target.startswith(parent + ".")
                                              for parent in blocked):
                    violation(path, node, symbol, "UNRESOLVED_FACADE_EXPORT")
                    additional.add(module)
        if not additional - blocked:
            break
        blocked.update(additional)
    for module in blocked:
        proven.pop(module, None)
    return proven, results
