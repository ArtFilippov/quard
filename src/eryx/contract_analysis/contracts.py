"""Require an explicit inherited contract on each discovered customer class."""
from __future__ import annotations

import argparse
import ast
import builtins
import json
from pathlib import Path
import sys
from typing import override

from eryx._lcom4_worker import SPECIAL_METHODS
from eryx.contract_analysis.contract_analysis import ContractAnalysis


METHOD = (ast.FunctionDef, ast.AsyncFunctionDef)
CONTRACT_BASES = frozenset({"typing.Protocol", "typing_extensions.Protocol",
                            "abc.ABC", "abc.ABCMeta", "builtins.object"})


def dotted(node: ast.expr) -> str | None:
    """Return a syntactic dotted name, unwrapping generic base arguments."""
    if isinstance(node, ast.Subscript):
        return dotted(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def module_name(path: str) -> str:
    """Map a discovered source path to its importable module name."""
    parts = Path(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def collect_nodes(module: str, tree: ast.Module) -> dict[str, ast.ClassDef]:
    nodes: dict[str, ast.ClassDef] = {}

    def visit(node: ast.AST, parents: tuple[str, ...]) -> None:
        if isinstance(node, ast.ClassDef):
            parents = (*parents, node.name)
            nodes[".".join((module, *parents))] = node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parents = (*parents, node.name)
        for child in ast.iter_child_nodes(node):
            visit(child, parents)

    visit(tree, ())
    return nodes


class Contracts(ContractAnalysis):
    """Resolve explicit imports and local inheritance across the discovered scope."""

    def __init__(self, trees: dict[str, ast.Module]):
        self.paths: dict[str, str] = {}
        self.nodes: dict[str, ast.ClassDef] = {}
        self.imports: dict[str, dict[str, str | None]] = {}
        self.bindings_by_path: dict[str, dict[str, str | None]] = {}
        self.declarations: list[tuple[str, ast.ClassDef, str]] = []
        self.modules = {module_name(path) for path in trees}
        self.customer_roots = {module.split(".")[0] for module in self.modules}
        for path, tree in trees.items():
            module = module_name(path)
            for name, node in collect_nodes(module, tree).items():
                self.declarations.append((name, node, path))
                # Prefer implementation source when a module also has a stub.
                if name not in self.nodes or path.endswith(".py"):
                    self.nodes[name] = node
                    self.paths[name] = path
            bindings: dict[str, str | None] = {}
            package = module if Path(path).stem == "__init__" else module.rpartition(".")[0]
            for statement in tree.body:
                if isinstance(statement, ast.Import):
                    for alias in statement.names:
                        bindings[alias.asname or alias.name.split(".")[0]] = (
                            alias.name if alias.asname else alias.name.split(".")[0])
                elif isinstance(statement, ast.ImportFrom):
                    prefix = package.split(".")[:len(package.split(".")) - statement.level + 1] if statement.level else []
                    target = ".".join([*prefix, statement.module] if statement.module else prefix)
                    for alias in statement.names:
                        bindings[alias.asname or alias.name] = f"{target}.{alias.name}"
                elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bindings[statement.name] = None
                elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            bindings[target.id] = None
            if module not in self.imports or path.endswith(".py"):
                self.imports[module] = bindings
            self.bindings_by_path[path] = bindings

    @override
    def provenance(self, target: str) -> str:
        """Follow customer re-exports without loading or inspecting external modules."""
        seen: set[str] = set()
        while target not in seen:
            seen.add(target)
            if target in self.nodes:
                return target
            for module in sorted(self.modules, key=len, reverse=True):
                if not module or not target.startswith(module + "."):
                    continue
                first, dot, rest = target[len(module) + 1:].partition(".")
                binding = self.imports[module].get(first)
                if binding:
                    target = binding + (dot + rest if dot else "")
                    break
            else:
                return target
        return target

    @override
    def resolved(self, name: str, base: ast.expr, path: str | None = None) -> str | None:
        text = dotted(base)
        if text is None:
            return None
        path = path or self.paths[name]
        module = module_name(path)
        scope = name.rpartition(".")[0]
        prefixes = [scope]
        while "." in scope and scope != module:
            scope = scope.rpartition(".")[0]
            prefixes.append(scope)
        for prefix in prefixes:
            candidate = f"{prefix}.{text}"
            if candidate in self.nodes:
                return candidate
        first, _, rest = text.partition(".")
        imported = self.bindings_by_path[path].get(first)
        if imported:
            return self.provenance(f"{imported}.{rest}" if rest else imported)
        if (first not in self.bindings_by_path[path] and first in vars(builtins)
                and isinstance(vars(builtins)[first], type)):
            return f"builtins.{text}"
        return None

    @override
    def has_contract(self, name: str, seen: frozenset[str] = frozenset(),
                     source: tuple[ast.ClassDef, str] | None = None) -> bool:
        if name in seen:
            return False
        node, path = source or (self.nodes[name], self.paths[name])
        for base in node.bases:
            resolved = self.resolved(name, base, path)
            if resolved in {"typing.Protocol", "typing_extensions.Protocol"}:
                return True
            if resolved in self.nodes and self.has_contract(resolved, seen | {name}):
                return True
            if resolved and resolved not in self.nodes and not self.local_module(resolved):
                return True
        return False

    @override
    def local_module(self, resolved: str) -> bool:
        return resolved.split(".")[0] in self.customer_roots

    @override
    def inherited_api(self, name: str, seen: frozenset[str] = frozenset(),
                      source: tuple[ast.ClassDef, str] | None = None
                      ) -> tuple[set[str], bool]:
        """Collect local contract members and proven opaque external ancestry."""
        if name in seen:
            return set(), False
        members: set[str] = set()
        external = False
        node, path = source or (self.nodes[name], self.paths[name])
        for base in node.bases:
            resolved = self.resolved(name, base, path)
            if resolved in self.nodes:
                if self.declaration(resolved):
                    members.update(self.public_members(resolved, self.nodes[resolved],
                                                       self.paths[resolved]))
                inherited, opaque = self.inherited_api(resolved, seen | {name})
                members.update(inherited)
                external |= opaque
            elif (resolved and resolved not in CONTRACT_BASES
                  and not self.local_module(resolved)):
                external = True
        return members, external

    @override
    def public_members(self, name: str, node: ast.ClassDef, path: str) -> dict[str, ast.stmt]:
        """Find locally declared public instance methods and properties."""
        members: dict[str, ast.stmt] = {}
        for item in node.body:
            if isinstance(item, METHOD):
                if (item.name.startswith("_") and not item.name.startswith("__")
                        or item.name in SPECIAL_METHODS):
                    continue
                if any(self.resolved(name, decorator, path) in {
                        "builtins.staticmethod", "builtins.classmethod"}
                        for decorator in item.decorator_list):
                    continue
                members.setdefault(item.name, item)
            elif (isinstance(item, ast.Assign) and isinstance(item.value, ast.Call)
                  and self.resolved(name, item.value.func, path) in {
                      "builtins.property", "functools.cached_property"}):
                for target in item.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        members.setdefault(target.id, item)
        return members

    @override
    def declaration(self, name: str, seen: frozenset[str] = frozenset(),
                    source: tuple[ast.ClassDef, str] | None = None) -> bool:
        if name in seen:
            return False
        node, path = source or (self.nodes[name], self.paths[name])
        bases = {self.resolved(name, base, path) for base in node.bases}
        if bases & {"typing.Protocol", "typing_extensions.Protocol"}:
            return True
        metaclass = {self.resolved(name, keyword.value, path) for keyword in node.keywords
                     if keyword.arg == "metaclass"}
        if not (bases | metaclass) & {"abc.ABC", "abc.ABCMeta"} and not any(
                base in self.nodes and self.declaration(base, seen | {name}) for base in bases):
            return False
        return any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and any(self.resolved(name, decorator, path) in {"abc.abstractmethod", "abc.abstractproperty"}
                           for decorator in item.decorator_list) for item in node.body)

    def _override_members(self, name: str, node: ast.ClassDef, path: str
                          ) -> dict[str, ast.stmt]:
        """Collect first declarations by member name, including recognized properties.

        Args:
            name (str): Qualified customer class name.
            node (ast.ClassDef): Class declaration being checked.
            path (str): Source or stub path for resolving property constructors.

        Returns:
            dict[str, ast.stmt]: Member names mapped to their first declaration.
        """
        members: dict[str, ast.stmt] = {}
        for item in node.body:
            if isinstance(item, METHOD):
                if item.name in SPECIAL_METHODS or (
                        item.name.startswith("__") and not item.name.endswith("__")):
                    continue
                _ = members.setdefault(item.name, item)
            elif (isinstance(item, ast.Assign) and isinstance(item.value, ast.Call)
                  and self.resolved(name, item.value.func, path) in {
                      "builtins.property", "functools.cached_property"}):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id not in SPECIAL_METHODS and not (
                            target.id.startswith("__") and not target.id.endswith("__")):
                        _ = members.setdefault(target.id, item)
        return members

    @override
    def inherited_implementations(self, name: str, seen: frozenset[str] | None = None,
                                   source: tuple[ast.ClassDef, str] | None = None
                                   ) -> dict[str, str]:
        """Find methods and properties on customer-owned, non-contract ancestors.

        Args:
            name (str): Qualified customer class name.
            seen (frozenset[str] | None, default=None): Ancestors visited on this path.
            source (tuple[ast.ClassDef, str] | None, default=None): Separate stub declaration.

        Returns:
            dict[str, str]: Member names mapped to their declaring classes.
        """
        seen = seen or frozenset()
        if name in seen:
            return {}
        node, path = source or (self.nodes[name], self.paths[name])
        methods: dict[str, str] = {}
        for base in node.bases:
            if (parent := self.resolved(name, base, path)) not in self.nodes:
                continue
            if not self.declaration(parent):
                for member in self._override_members(parent, self.nodes[parent], self.paths[parent]):
                    _ = methods.setdefault(member, parent)
            for member, owner in self.inherited_implementations(parent, seen | {name}).items():
                if member not in methods:
                    methods[member] = owner
        return methods

    @override
    def method_overrides(self, name: str, node: ast.ClassDef, path: str
                         ) -> list[dict[str, object]]:
        if self.declaration(name, source=(node, path)):
            return []
        inherited = self.inherited_implementations(name, source=(node, path))
        module = module_name(path)
        results: list[dict[str, object]] = []
        for member, item in self._override_members(name, node, path).items():
            if member not in inherited:
                continue
            symbol = f"{name.removeprefix(module + '.')}.{member}"
            message = f"{name}.{member}: overrides customer member from {inherited[member]}."
            results.append({"rule": "CUSTOMER_METHOD_OVERRIDE", "path": path,
                            "line": item.lineno, "column": item.col_offset + 1,
                            "symbol": symbol, "message": message})
        return results

    @override
    def diagnostics(self) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for name, node, path in self.declarations:
            module = module_name(path)
            source = (node, path)
            results.extend(self.method_overrides(name, node, path))
            if not self.declaration(name, source=source) and not self.has_contract(name, source=source):
                results.append({"rule": "MISSING_CLASS_CONTRACT", "path": path,
                                "line": node.lineno, "column": node.col_offset + 1,
                                "symbol": name.removeprefix(module + "."),
                                "message": f"{name}: customer class requires an explicitly inherited contract."})
            elif not self.declaration(name, source=source):
                members, external = self.inherited_api(name, source=source)
                if not external:
                    for member, item in self.public_members(name, node, path).items():
                        if member not in members:
                            symbol = f"{name.removeprefix(module + '.')}.{member}"
                            results.append({"rule": "UNCOVERED_CLASS_MEMBER", "path": path,
                                            "line": item.lineno, "column": item.col_offset + 1,
                                            "symbol": symbol, "message":
                                            f"{name}.{member}: no inherited local contract declares this member."})
        return results


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    trees: dict[str, ast.Module] = {}
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid class contract source path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "CLASS_CONTRACTS_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
    diagnostics.extend(Contracts(trees).diagnostics())
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Class contract manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), paths)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        error = "CLASS_CONTRACTS_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc), "is_infrastructure_error": True}]
    print(json.dumps({"eryx_class_contracts_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
