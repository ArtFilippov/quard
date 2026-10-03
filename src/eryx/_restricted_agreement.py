"""Compare declaration-local restriction evidence across explicit interfaces."""
from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from eryx._restricted_calls import Restrictions


Evidence = dict[str, object]
Index = dict[tuple[str, str, str], list[Evidence]]
Marks = dict[tuple[str, str], bool]
Members = dict[tuple[str, str], set[str]]
Emit = Callable[[str, str, Evidence, str], None]
PROPERTY_KIND = "kind"


def _marked(index: Index, marks: Marks, owner: str, name: str, *, path: str) -> bool:
    entries = index[owner, name, path]
    return any(bool(item["marked"]) for item in entries) or (
        marks.get((owner, path), False)
        and (PROPERTY_KIND in entries[0] or not (name.startswith("__") and name.endswith("__"))))


def _overloads(index: Index, marks: Marks, emit: Emit) -> None:
    for (owner, name, path), entries in index.items():
        if PROPERTY_KIND not in entries[0]:
            class_marked = marks.get((owner, path), False) and not (
                name.startswith("__") and name.endswith("__"))
            first = bool(entries[0]["marked"]) or class_marked
            for item in entries[1:]:
                if (bool(item["marked"]) or class_marked) != first:
                    emit(owner, name, item, f"{path}:{entries[0]['line']}")


def _source_stub(index: Index, marks: Marks, emit: Emit) -> None:
    for (owner, name, path), entries in index.items():
        if not path.endswith(".pyi"):
            continue
        source = path[:-1]
        peer = index.get((owner, name, source))
        if peer and (_marked(index, marks, owner, name, path=path) !=
                     _marked(index, marks, owner, name, path=source)
                     or (PROPERTY_KIND in entries[0]) != (PROPERTY_KIND in peer[0])):
            emit(owner, name, entries[0], f"{source}:{peer[0]['line']}")


def _inherited_stub(restrictions: Restrictions, index: Index, marks: Marks, emit: Emit) -> None:
    for declaration in restrictions.class_declarations:
        stub = cast(str, declaration["path"])
        if not stub.endswith(".pyi"):
            continue
        owner = cast(str, declaration["owner"])
        source = stub[:-1]
        if (owner, source) not in marks:
            continue
        seen: set[str] = set()
        for base in (restrictions.mro(owner) or [])[1:]:
            inherited = [(name, path, entries) for (candidate, name, path), entries in index.items()
                         if candidate == base and path == restrictions.contracts.paths.get(base)]
            for name, _, entries in inherited:
                if name in seen:
                    continue
                seen.add(name)
                if (owner, name, source) in index and (owner, name, stub) in index:
                    continue
                property_access = PROPERTY_KIND in entries[0]
                classwide = property_access or not (name.startswith("__") and name.endswith("__"))
                inherited_mark = bool(restrictions.property_target(base, name) if property_access
                                      else restrictions.target(base, name))
                source_mark = (marks[owner, source] and classwide or inherited_mark or
                               (owner, name, source) in index and
                               _marked(index, marks, owner, name, path=source))
                stub_mark = (marks[owner, stub] and classwide or inherited_mark or
                             (owner, name, stub) in index and
                             _marked(index, marks, owner, name, path=stub))
                if source_mark != stub_mark:
                    emit(owner, name, index.get((owner, name, stub), [declaration])[0],
                         f"{source} ({owner}.{name})")


def _ancestors(restrictions: Restrictions, owner: str, node: ast.ClassDef, path: str) -> set[str]:
    contracts = restrictions.contracts
    visited: set[str] = set()

    def visit(name: str | None) -> None:
        if name is None or name not in contracts.nodes or name in visited:
            return
        visited.add(name)
        parent = contracts.nodes[name]
        for base in parent.bases:
            visit(contracts.resolved(name, base))

    for base in node.bases:
        visit(contracts.resolved(owner, base, path))
    return {name for name in visited if contracts.declaration(name)}


def _requirements(restrictions: Restrictions, index: Index, marks: Marks,
                  members: Members, *, ancestors: set[str]) -> dict[str, list[tuple[bool, str]]]:
    required: dict[str, list[tuple[bool, str]]] = defaultdict(list)
    for contract in sorted(ancestors):
        source = restrictions.contracts.paths[contract]
        for name in sorted(members.get((contract, source), set())):
            required[name].append((_marked(index, marks, contract, name, path=source),
                                   f"{source}:{index[contract, name, source][0]['line']} "
                                   + f"({contract}.{name})"))
        if marks.get((contract, source), False):
            for base in restrictions.mro(contract) or []:
                for name in sorted(members.get((base, restrictions.contracts.paths.get(base, "")),
                                               set()) - members.get((contract, source), set())):
                    required[name].append((True, f"{source} ({contract})"))
    return required


def _inherited(restrictions: Restrictions, index: Index, marks: Marks,
               members: Members, *, emit: Emit) -> None:
    contracts = restrictions.contracts
    for owner, node, path in contracts.declarations:
        for name, statuses in _requirements(
                restrictions, index, marks, members,
                ancestors=_ancestors(restrictions, owner, node, path)).items():
            if len({status for status, _ in statuses}) > 1:
                emit(owner, name, {"path": path, "line": node.lineno,
                                   "column": node.col_offset + 1},
                     " and ".join(location for _, location in statuses))
                continue
            if (local := index.get((owner, name, path))) and not contracts.declaration(
                    owner, source=(node, path)) and (
                    _marked(index, marks, owner, name, path=path) != statuses[0][0]):
                emit(owner, name, local[0], statuses[0][1])


def agreement(restrictions: Restrictions) -> list[Evidence]:
    """Find disagreement without changing effective access restrictions.

    Args:
        restrictions (Restrictions): Parsed declaration and ancestry evidence.

    Returns:
        list[Evidence]: Positioned declaration-agreement violations.
    """
    index: Index = defaultdict(list)
    members: Members = defaultdict(set)
    for item in restrictions.declarations:
        owner, name, path = (cast(str, item[key]) for key in ("owner", "member", "path"))
        index[owner, name, path].append(item)
        members[owner, path].add(name)
    marks = {(str(item["owner"]), str(item["path"])): bool(item["marked"])
             for item in restrictions.class_declarations}
    results: list[Evidence] = []
    emitted: set[tuple[str, str, int]] = set()

    def emit(owner: str, name: str, item: Evidence, other: str) -> None:
        path, line = cast(str, item["path"]), cast(int, item["line"])
        if (key := (path, f"{owner}.{name}", line)) in emitted:
            return
        emitted.add(key)
        results.append({"rule": "ENCAPSULATED_ONLY_DECLARATION", "path": path,
                        "line": line, "column": item["column"],
                        "symbol": f"{owner}.{name}", "message":
                        f"{owner}.{name}: restriction declaration disagrees with {other}."})

    _overloads(index, marks, emit)
    _source_stub(index, marks, emit)
    _inherited_stub(restrictions, index, marks, emit)
    _inherited(restrictions, index, marks, members, emit=emit)
    return sorted(results, key=lambda item: (str(item["path"]), cast(int, item["line"]),
                                             cast(int, item["column"]), str(item["symbol"])))
