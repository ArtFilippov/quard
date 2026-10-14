"""Check folder ownership of customer contracts and implementations."""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import sys
from typing import override

from quard.contract_analysis.contracts import Contracts, module_name
from quard.folder_analysis.folder_analysis import FolderAnalysis
from quard.folder_analysis.folder_utils import analyze, main, snake_case

__all__ = ["FolderOwnership", "analyze", "main", "snake_case"]
SOURCE_ROOT = "src"
MAIN_FOLDER = "main"
ENVELOPES_FOLDER = "envelopes"
STUB_SUFFIX = ".pyi"
CONSTRUCTOR = "__init__"
TEST_ROOT = "tests"
SUPER_NAME = "super"
BASE_ROLE = "base"
ASSEMBLY_ROLE = "assembly"
METHOD_KIND = "method"
GET_KIND = "get"
SET_KIND = "set"
DELETE_KIND = "delete"
PROPERTY_NAME = "builtins.property"
OPAQUE_BASES = {"builtins.object", "typing.Protocol", "typing_extensions.Protocol",
                "abc.ABC", "abc.ABCMeta"}


class FolderOwnership(Contracts, FolderAnalysis):
    """Resolve the local contract selected by each class's explicit ancestry."""

    def __init__(self, trees: dict[str, ast.Module]):
        super().__init__(trees)
        self.trees = trees
        self.contracts = {name for name in self.nodes if self.declaration(name)}

    @override
    def is_contract(self, name: str) -> bool:
        return name in self.contracts

    @override
    def owners(self, name: str, seen: frozenset[str] = frozenset(),
               source: tuple[ast.ClassDef, str] | None = None) -> tuple[set[str], bool]:
        """Return nearest local contracts and whether ancestry is unresolved."""
        if name in seen:
            return set(), True
        if name in self.contracts and source is None:
            return {name}, False
        node, path = source or (self.nodes[name], self.paths[name])
        owners: set[str] = set()
        direct_contracts: set[str] = set()
        unknown = False
        for base in node.bases:
            resolved = self.resolved(name, base, path)
            if resolved in self.nodes:
                if resolved in self.contracts:
                    direct_contracts.add(resolved)
                inherited, uncertain = self.owners(resolved, seen | {name})
                owners.update(inherited)
                unknown |= uncertain
            elif resolved is None or self.local_module(resolved):
                unknown = True
        if len(direct_contracts) == 1:
            selected = next(iter(direct_contracts))
            if owners - {selected} <= self.contract_ancestors(selected):
                return {selected}, unknown
        return owners, unknown

    @override
    def folder(self, contract: str, profile: str) -> Path:
        """Find the expected contract directory in the current profile."""
        path = Path(self.paths[contract])
        parent = Path(profile, *path.parts[1:-1])
        expected = snake_case(contract.rpartition(".")[2])
        return parent if parent.name == expected else parent / expected

    @override
    def contract_ancestors(self, name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        """Collect contracts named by a combined contract's local bases."""
        if name in seen:
            return set()
        ancestors = {name}
        for base in self.nodes[name].bases:
            parent = self.resolved(name, base)
            if parent in self.contracts:
                ancestors.update(self.contract_ancestors(parent, seen | {name}))
        return ancestors

    @override
    def envelope_role(self, name: str, node: ast.ClassDef, path: str,
                      seen: frozenset[str] = frozenset()) -> str | None:
        """Prove a direct contract base or a chain of assembly ancestors.

        Args:
            name (str): Qualified Envelope name.
            node (ast.ClassDef): Declaration to classify.
            path (str): Source or stub path.
            seen (frozenset[str], default=frozenset()): Visited ancestors.

        Returns:
            str | None: Proven role, if any.
        """
        if name in seen or len(node.bases) != 1:
            return None
        parent = self.resolved(name, node.bases[0], path)
        if parent in self.contracts or (parent is not None and not self.local_module(parent)
                                        and parent not in OPAQUE_BASES):
            return BASE_ROLE
        if parent not in self.nodes or parent == name:
            return None
        role = self.envelope_role(parent, self.nodes[parent], self.paths[parent], seen | {name})
        if self.paths[parent].endswith(".py") and (
                role == BASE_ROLE and self.delegation(
                    parent, self.nodes[parent], self.paths[parent]) is None or
                role == ASSEMBLY_ROLE and
                self.assembly(self.nodes[parent], self.paths[parent]) is None):
            return ASSEMBLY_ROLE
        return None

    @override
    def stub_agreement(self, name: str, node: ast.ClassDef, path: str, *,
                       original: ast.ClassDef, source: str) -> bool:
        """Compare source and stub Envelope ancestry and behavior declarations.

        Args:
            name (str): Qualified Envelope name.
            node (ast.ClassDef): Stub class.
            path (str): Stub path.
            original (ast.ClassDef): Source class.
            source (str): Source path.

        Returns:
            bool: Whether the stub describes the same role and members.
        """
        members = [(item.name, self.member_kind(name, item, path),
                    isinstance(item, ast.AsyncFunctionDef)) for item in node.body
                   if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and item.name != CONSTRUCTOR]
        declared = [(item.name, self.member_kind(name, item, source),
                     isinstance(item, ast.AsyncFunctionDef)) for item in original.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name != CONSTRUCTOR]
        constructors = [item for item in node.body
                        if isinstance(item, ast.FunctionDef) and item.name == CONSTRUCTOR]
        return (self.envelope_role(name, node, path) ==
                self.envelope_role(name, original, source) and
                [self.resolved(name, base, path) for base in node.bases] ==
                [self.resolved(name, base, source) for base in original.bases] and
                Counter(members) == Counter(declared) and
                (self.envelope_role(name, node, path) != ASSEMBLY_ROLE or
                 len(constructors) == 1 and all(
                     item is constructors[0] or isinstance(item, ast.Expr) and
                     isinstance(item.value, ast.Constant) and
                     isinstance(item.value.value, str)
                     for item in node.body)))

    @override
    def base_violations(self) -> list[dict[str, object]]:
        """Prove direct-folder Envelope roles from source and matching stubs.

        Returns:
            list[dict[str, object]]: Positioned failures to prove delegation.
        """
        results: list[dict[str, object]] = []
        for path, tree in self.trees.items():
            current = Path(path)
            if (current.parent.name != ENVELOPES_FOLDER or current.parts[0] == TEST_ROOT
                    or current.stem == CONSTRUCTOR):
                continue
            if not any(isinstance(item, ast.ClassDef) for item in tree.body):
                results.append({"path": path, "line": 1, "column": 1,
                                "symbol": current.stem, "rule": "ENVELOPE_CLASS",
                                "message": f"{path}: envelopes/ requires an Envelope class."})
        for name, node, path in self.declarations:
            current = Path(path)
            if current.parent.name != ENVELOPES_FOLDER or current.parts[0] == TEST_ROOT:
                continue
            role = self.envelope_role(name, node, path)
            source = current.with_suffix(".py").as_posix()
            if current.suffix == STUB_SUFFIX and source in self.trees:
                original = next((item for item in self.trees[source].body
                                 if isinstance(item, ast.ClassDef) and item.name == node.name),
                                None)
                if original is not None:
                    message = (None if self.stub_agreement(name, node, path,
                                                           original=original, source=source)
                               else "match the source Envelope ancestry and declared members")
                else:
                    message = "declare the same Envelope class in source"
            elif role is None:
                message = "inherit exactly one proven contract or a proven Envelope parent"
            elif current.suffix == STUB_SUFFIX:
                message = "provide Python source to prove Envelope behavior"
            elif role == BASE_ROLE:
                message = self.delegation(name, node, path)
            else:
                message = self.assembly(node, path)
            if message is not None:
                results.append({"path": path, "line": node.lineno,
                                "column": node.col_offset + 1,
                                "symbol": name.removeprefix(module_name(path) + "."),
                                "rule": ("ENVELOPE_DELEGATION" if role == BASE_ROLE
                                         else "ENVELOPE_INHERITANCE"),
                                "message": f"{name}: {message}."})
        return results

    @override
    def assembly(self, node: ast.ClassDef, path: str) -> str | None:
        """Require one direct parent constructor call and no other declared behavior.

        Args:
            node (ast.ClassDef): Assembly Envelope declaration.
            path (str): Source path for builtin name resolution.

        Returns:
            str | None: Reason that assembly could not be proved, if any.
        """
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        if (len(body) != 1 or not isinstance(body[0], ast.FunctionDef)
                or body[0].name != CONSTRUCTOR or body[0].decorator_list):
            return "declare only one constructor and no other members"
        constructor = body[0]
        statements = constructor.body
        if (statements and isinstance(statements[0], ast.Expr)
                and isinstance(statements[0].value, ast.Constant)
                and isinstance(statements[0].value.value, str)):
            statements = statements[1:]
        calls = [item.value for item in statements if isinstance(item, ast.Expr)
                 and isinstance(item.value, ast.Call) and isinstance(item.value.func, ast.Attribute)
                 and item.value.func.attr == CONSTRUCTOR
                 and isinstance(item.value.func.value, ast.Call)
                 and isinstance(item.value.func.value.func, ast.Name)
                 and item.value.func.value.func.id == SUPER_NAME
                 and not item.value.func.value.args and not item.value.func.value.keywords]
        if (len(calls) != 1 or SUPER_NAME in self.bindings_by_path[path] or
                SUPER_NAME in {arg.arg for arg in constructor.args.posonlyargs +
                               constructor.args.args + constructor.args.kwonlyargs} or
                any(isinstance(item, ast.Name) and item.id == SUPER_NAME
                    and isinstance(item.ctx, ast.Store) for item in ast.walk(constructor))
                or sum(1 for item in ast.walk(constructor) if isinstance(item, ast.Call)
                       and isinstance(item.func, ast.Attribute)
                       and item.func.attr == CONSTRUCTOR) != 1):
            return "call super().__init__(...) exactly once in the constructor"
        return None

    @override
    def delegation(self, name: str, node: ast.ClassDef, path: str) -> str | None:
        """Recognize one stored parameter and direct forwarding bodies.

        Args:
            name (str): Qualified class name.
            node (ast.ClassDef): Candidate base Envelope.
            path (str): Source path for decorator resolution.

        Returns:
            str | None: Reason the class is not a forwarding base, if any.
        """
        methods = [item for item in node.body if isinstance(item, (ast.FunctionDef,
                                                                     ast.AsyncFunctionDef))]
        constructors = [item for item in methods if item.name == CONSTRUCTOR]
        if len(constructors) != 1:
            return "store exactly one delegate in a constructor"
        constructor = constructors[0]
        body = constructor.body
        if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        if (constructor.decorator_list or len(body) != 1 or not isinstance(body[0], ast.Assign)
                or len(body[0].targets) != 1
                or not isinstance(body[0].targets[0], ast.Attribute)
                or not isinstance(body[0].targets[0].value, ast.Name)
                or constructor.args.posonlyargs or not constructor.args.args
                or body[0].targets[0].value.id != constructor.args.args[0].arg
                or not isinstance(body[0].value, ast.Name)
                or body[0].value.id not in {arg.arg for arg in constructor.args.args[1:]}
                or len(constructor.args.args[1:]) != 1
                or constructor.args.vararg or constructor.args.kwarg
                or constructor.args.kwonlyargs or constructor.args.defaults):
            return "assign one constructor parameter to one receiver field without other work"
        field = body[0].targets[0].attr
        forwarding = [item for item in methods if item.name != CONSTRUCTOR]
        getters = {item.name for item in forwarding
                   if self.member_kind(name, item, path) == GET_KIND}
        failure = next((item for item in forwarding
                        if (kind := self.member_kind(name, item, path)) is None
                        or kind in {SET_KIND, DELETE_KIND} and item.name not in getters
                        or not self.forwarded(item, field, kind)), None)
        if not forwarding or failure is not None:
            return (f"forward {failure.name} directly to the stored delegate" if failure
                    else "implement at least one directly forwarded member")
        body = node.body
        if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        return ("declare only forwarding methods and the delegate constructor"
                if any(not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                       for item in body) else None)

    @override
    def member_kind(self, name: str, method: ast.FunctionDef | ast.AsyncFunctionDef,
                    path: str) -> str | None:
        """Identify an ordinary method or a direct built-in property accessor.

        Args:
            name (str): Qualified class name.
            method (ast.FunctionDef | ast.AsyncFunctionDef): Declared member.
            path (str): Source or stub path.

        Returns:
            str | None: Member kind, or no recognized declaration kind.
        """
        if not method.decorator_list:
            return METHOD_KIND
        if len(method.decorator_list) == 1 and isinstance(method, ast.FunctionDef):
            decorator = method.decorator_list[0]
            if self.resolved(name, decorator, path) == PROPERTY_NAME:
                return GET_KIND
            if (isinstance(decorator, ast.Attribute) and isinstance(decorator.value, ast.Name)
                    and decorator.value.id == method.name):
                return {"setter": SET_KIND, "deleter": DELETE_KIND}.get(decorator.attr)
        return None

    @override
    def forwarded(self, method: ast.FunctionDef | ast.AsyncFunctionDef, field: str,
                  kind: str) -> bool:
        """Match an unchanged call, including its awaited async form.

        Args:
            method (ast.FunctionDef | ast.AsyncFunctionDef): Implemented method.
            field (str): Stored delegate field.
            kind (str): Proven member declaration kind.

        Returns:
            bool: Whether the method directly forwards its parameters and result.
        """
        args = method.args
        if not (args.posonlyargs or args.args) or len(method.body) != 1:
            return False
        if kind != METHOD_KIND:
            return self.property_forwarded(method, field, kind)
        receiver = (args.posonlyargs + args.args)[0].arg
        if not isinstance(method.body[0], ast.Return):
            return False
        value = method.body[0].value
        if isinstance(method, ast.AsyncFunctionDef):
            if not isinstance(value, ast.Await):
                return False
            value = value.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
            return False
        positional = (args.posonlyargs + args.args)[1:]
        star = value.args[-1] if args.vararg is not None and value.args else None
        actual = value.args[:-1] if args.vararg is not None and value.args else value.args
        expected: list[tuple[str | None, str]] = [(arg.arg, arg.arg) for arg in args.kwonlyargs]
        if args.kwarg is not None:
            expected.append((None, args.kwarg.arg))
        return (value.func.attr == method.name and isinstance(value.func.value, ast.Attribute)
                and value.func.value.attr == field
                and isinstance(value.func.value.value, ast.Name)
                and value.func.value.value.id == receiver
                and (args.vararg is None or isinstance(star, ast.Starred)
                     and isinstance(star.value, ast.Name) and star.value.id == args.vararg.arg)
                and len(actual) == len(positional)
                and all(isinstance(item, ast.Name) and item.id == arg.arg
                        for item, arg in zip(actual, positional))
                and len(value.keywords) == len(expected)
                and all(item.arg == name and isinstance(item.value, ast.Name)
                        and item.value.id == origin
                        for item, (name, origin) in zip(value.keywords, expected)))

    @override
    def property_forwarded(self, method: ast.FunctionDef | ast.AsyncFunctionDef,
                           field: str, kind: str) -> bool:
        """Prove a direct property operation on the stored delegate.

        Args:
            method (ast.FunctionDef | ast.AsyncFunctionDef): Accessor declaration.
            field (str): Stored delegate field.
            kind (str): Getter, setter or deleter kind.

        Returns:
            bool: Whether the matching property is forwarded without other work.
        """
        args = method.args
        if (args.posonlyargs or args.vararg or args.kwarg or args.kwonlyargs
                or args.defaults or args.kw_defaults
                or len(args.args) != (2 if kind == SET_KIND else 1)):
            return False
        statement = method.body[0]
        if kind == GET_KIND:
            target = statement.value if isinstance(statement, ast.Return) else None
        elif (kind == SET_KIND and isinstance(statement, ast.Assign)
              and len(statement.targets) == 1):
            target = statement.targets[0]
            if (not isinstance(statement.value, ast.Name)
                    or statement.value.id != args.args[1].arg):
                return False
        elif (kind == DELETE_KIND and isinstance(statement, ast.Delete)
              and len(statement.targets) == 1):
            target = statement.targets[0]
        else:
            return False
        return (isinstance(target, ast.Attribute) and target.attr == method.name
                and isinstance(target.value, ast.Attribute) and target.value.attr == field
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id == args.args[0].arg)

    @override
    def folder_violations(self) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for name, node, path in self.declarations:
            module = module_name(path)
            symbol = name.removeprefix(module + ".")
            location = {"path": path, "line": node.lineno,
                        "column": node.col_offset + 1, "symbol": symbol}
            current = Path(path)
            if self.declaration(name, source=(node, path)):
                expected = snake_case(node.name)
                if current.parent.name != expected or current.stem != expected:
                    results.append({**location, "rule": "CONTRACT_FOLDER",
                                    "message": f"{name}: declare this contract in "
                                    f"'{expected}/{expected}{current.suffix}' (folder and module "
                                    "must match the contract name)."})
                continue
            if current.parts[0] == SOURCE_ROOT and MAIN_FOLDER in current.parts[2:-1]:
                # Assembly classes can combine multiple local implementations here.
                continue
            local_bases = [self.resolved(name, base, path) for base in node.bases]
            concrete_bases = [base for base in local_bases
                              if base in self.nodes and base not in self.contracts]
            direct_contracts = [base for base in local_bases if base in self.contracts]
            owners, unknown = self.owners(name, source=(node, path))
            if len(concrete_bases) > 1:
                results.append({**location, "rule": "COMPETING_LOCAL_BASES",
                                "message": f"{name}: select one local concrete base and inherit "
                                "a separate combined local contract for the others."})
                continue
            if len(direct_contracts) > 1:
                results.append({**location, "rule": "COMPETING_LOCAL_BASES",
                                "message": f"{name}: declare a separate combined local contract "
                                "instead of inheriting multiple local contracts here."})
                continue
            if len(owners) > 1 and not direct_contracts:
                results.append({**location, "rule": "COMPETING_LOCAL_BASES",
                                "message": f"{name}: inherit a separate combined local contract "
                                "and select one local base for this implementation."})
                continue
            # An explicit combined contract selects the owner over a concrete base.
            if len(direct_contracts) == 1:
                inherited = owners - {direct_contracts[0]}
                if inherited - self.contract_ancestors(direct_contracts[0]):
                    results.append({**location, "rule": "COMPETING_LOCAL_BASES",
                                    "message": f"{name}: combined contract {direct_contracts[0]} "
                                    "must inherit the selected local base's contract."})
                    continue
                owners = {direct_contracts[0]}
            if unknown or len(owners) != 1:
                if unknown:
                    results.append({**location, "rule": "UNRESOLVED_FOLDER_OWNER",
                                    "message": f"{name}: resolve local ancestry before its "
                                    "implementation folder can be verified."})
                continue
            owner = next(iter(owners))
            expected = self.folder(owner, current.parts[0])
            if current.parent != expected and expected not in current.parents:
                results.append({**location, "rule": "IMPLEMENTATION_FOLDER",
                                "message": f"{name}: move this implementation into "
                                f"'{expected}/' (owned by {owner})."})
        return results


if __name__ == "__main__":
    sys.exit(main())
