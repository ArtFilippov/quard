"""Check folder ownership of customer contracts and implementations."""
from __future__ import annotations

import ast
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
OPAQUE_BASES = {"builtins.object", "typing.Protocol", "typing_extensions.Protocol",
                "abc.ABC", "abc.ABCMeta"}


class FolderOwnership(Contracts, FolderAnalysis):
    """Resolve the local contract selected by each class's explicit ancestry."""

    def __init__(self, trees: dict[str, ast.Module]):
        super().__init__(trees)
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
    def inherits_envelope(self, name: str, node: ast.ClassDef, path: str, *,
                          owner: str, seen: frozenset[str] = frozenset()) -> bool:
        """Find a correctly placed base envelope in the local concrete ancestry."""
        if name in seen:
            return False
        base_name = owner.rpartition(".")[2] + "Envelope"
        for base in node.bases:
            parent = self.resolved(name, base, path)
            if parent not in self.nodes or parent in self.contracts:
                continue
            parent_node = self.nodes[parent]
            parent_path = self.paths[parent]
            parent_file = Path(parent_path)
            expected = self.folder(owner, parent_file.parts[0]) / "envelopes"
            if (parent_node.name == base_name and parent_file.parent == expected
                    and parent_file.stem == snake_case(base_name)
                    and any(self.resolved(parent, direct) == owner
                            for direct in parent_node.bases)):
                return True
            if self.inherits_envelope(parent, parent_node, parent_path, owner=owner,
                                      seen=seen | {name}):
                return True
        return False

    @override
    def envelope_violation(self, name: str, node: ast.ClassDef, path: str, *,
                           owner: str | None) -> tuple[str, str] | None:
        """Check the reserved direct envelopes folder and its named classes."""
        current = Path(path)
        inside = ENVELOPES_FOLDER in current.parts[1:-1]
        if not inside and (owner is None or not node.name.endswith("Envelope")):
            return None
        if inside and not node.name.endswith("Envelope"):
            return "ENVELOPE_CLASS", f"{name}: only named envelope classes belong in envelopes/."
        if owner is None:
            if (inside and current.parent.name == ENVELOPES_FOLDER
                    and len(node.bases) == 1
                    and (base := self.resolved(name, node.bases[0], path)) is not None
                    and self.delegation(node) is None
                    and not self.local_module(base) and base not in OPAQUE_BASES):
                return None
            return "ENVELOPE_INHERITANCE", (f"{name}: envelope must inherit its parent "
                                            "local contract through the base envelope.")
        expected = self.folder(owner, current.parts[0]) / "envelopes"
        if current.parent != expected:
            return "ENVELOPE_FOLDER", f"{name}: place this envelope in '{expected}/'."
        if current.stem != snake_case(node.name):
            return "ENVELOPE_FILE", (f"{name}: declare this envelope in "
                                     f"'{expected / (snake_case(node.name) + current.suffix)}'.")
        base_name = owner.rpartition(".")[2] + "Envelope"
        if node.name == base_name:
            valid = any(self.resolved(name, base, path) == owner for base in node.bases)
        else:
            valid = self.inherits_envelope(name, node, path, owner=owner)
        if not valid:
            return "ENVELOPE_INHERITANCE", (f"{name}: inherit {owner} directly as "
                                            f"{base_name}, or subclass that base envelope.")
        return None

    @override
    def base_violations(self) -> list[dict[str, object]]:
        """Prove direct-folder base Envelopes from their source bodies.

        Returns:
            list[dict[str, object]]: Positioned failures to prove delegation.
        """
        results: list[dict[str, object]] = []
        for name, node, path in self.declarations:
            current = Path(path)
            if (current.parent.name != ENVELOPES_FOLDER or not node.name.endswith("Envelope")
                    or current.stem != snake_case(node.name)):
                continue
            bases = [self.resolved(name, base, path) for base in node.bases]
            contracts = [base for base in bases if base in self.contracts or (
                base is not None and not self.local_module(base)
                and base not in OPAQUE_BASES)]
            # Concrete descendants remain assembly Envelopes in this first slice.
            if (any(base in self.nodes and base not in self.contracts for base in bases)
                    and (snake_case(node.name).removesuffix("_envelope")
                         != current.parent.parent.name)
                    or len(contracts) == 1 and contracts[0] in self.contracts
                    and node.name != self.nodes[contracts[0]].name + "Envelope"
                    or current.suffix == STUB_SUFFIX
                    and current.with_suffix(".py").as_posix() in self.bindings_by_path):
                continue
            if len(bases) != 1 or len(contracts) != 1:
                message = "directly inherit exactly one proven contract"
            elif current.suffix == STUB_SUFFIX:
                message = "provide Python source to prove delegation"
            else:
                message = self.delegation(node)
            if message is not None:
                results.append({"path": path, "line": node.lineno,
                                "column": node.col_offset + 1,
                                "symbol": name.removeprefix(module_name(path) + "."),
                                "rule": "ENVELOPE_DELEGATION",
                                "message": f"{name}: {message}."})
        return results

    @override
    def delegation(self, node: ast.ClassDef) -> str | None:
        """Recognize one stored parameter and direct forwarding bodies.

        Args:
            node (ast.ClassDef): Candidate base Envelope.

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
        failure = next((item for item in forwarding if not self.forwarded(item, field)), None)
        if not forwarding or failure is not None:
            return (f"forward {failure.name} directly to the stored delegate" if failure
                    else "implement at least one directly forwarded method")
        body = node.body
        if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        return ("declare only forwarding methods and the delegate constructor"
                if any(not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                       for item in body) else None)

    @override
    def forwarded(self, method: ast.FunctionDef | ast.AsyncFunctionDef, field: str) -> bool:
        """Match an unchanged call, including its awaited async form.

        Args:
            method (ast.FunctionDef | ast.AsyncFunctionDef): Implemented method.
            field (str): Stored delegate field.

        Returns:
            bool: Whether the method directly forwards its parameters and result.
        """
        args = method.args
        if (method.decorator_list or not args.args or len(method.body) != 1
                or not isinstance(method.body[0], ast.Return)):
            return False
        receiver = args.args[0].arg
        value = method.body[0].value
        if isinstance(method, ast.AsyncFunctionDef):
            if not isinstance(value, ast.Await):
                return False
            value = value.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
            return False
        positional = args.posonlyargs + args.args[1:]
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
                elif violation := self.envelope_violation(name, node, path, owner=None):
                    rule, message = violation
                    results.append({**location, "rule": rule, "message": message})
                continue
            owner = next(iter(owners))
            if violation := self.envelope_violation(name, node, path, owner=owner):
                rule, message = violation
                results.append({**location, "rule": rule, "message": message})
                continue
            if "envelopes" in current.parts[1:-1] or node.name.endswith("Envelope"):
                continue
            expected = self.folder(owner, current.parts[0])
            if current.parent != expected and expected not in current.parents:
                results.append({**location, "rule": "IMPLEMENTATION_FOLDER",
                                "message": f"{name}: move this implementation into "
                                f"'{expected}/' (owned by {owner})."})
        return results


if __name__ == "__main__":
    sys.exit(main())
