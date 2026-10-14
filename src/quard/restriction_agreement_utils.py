"""Compare restriction declarations within the same owner across source and stubs."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from quard.restricted_targets.restricted_targets import RestrictedTargets


Evidence = dict[str, object]
Index = dict[tuple[str, str, str], list[Evidence]]
Marks = dict[tuple[str, str], bool]
Emit = Callable[[str, str, Evidence, str], None]
PROPERTY_KIND = "kind"

__all__ = ["Emit", "Evidence", "Index", "Marks", "PROPERTY_KIND",
           "_inherited_stub", "_marked", "_overloads", "_source_stub", "agreement"]


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


def _inherited_stub(restrictions: RestrictedTargets, index: Index, marks: Marks,
                    emit: Emit) -> None:
    for declaration in restrictions.report_evidence()[1]:
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
                          if candidate == base and path == restrictions.contract_source_path(base)]
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


def agreement(restrictions: RestrictedTargets) -> list[Evidence]:
    """Find disagreement without changing effective access restrictions.

    Args:
        restrictions (RestrictedTargets): Parsed declaration and ancestry evidence.

    Returns:
        list[Evidence]: Positioned declaration-agreement violations.
    """
    index: Index = defaultdict(list)
    declarations, classes, _ = restrictions.report_evidence()
    for item in declarations:
        owner, name, path = (cast(str, item[key]) for key in ("owner", "member", "path"))
        index[owner, name, path].append(item)
    marks = {(str(item["owner"]), str(item["path"])): bool(item["marked"])
              for item in classes}
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
    return sorted(results, key=lambda item: (str(item["path"]), cast(int, item["line"]),
                                             cast(int, item["column"]), str(item["symbol"])))
