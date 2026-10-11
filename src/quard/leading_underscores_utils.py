"""Check names introduced by discovered first-party Python files and their paths."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import cast

from quard.leading_underscores_scanner import BindingScanner

# Python's data model, import machinery, class creation and typing reserve these names.
# Arbitrary __custom__ spellings and conventions such as __version__ are not special.
SPECIAL = frozenset("""
__name__ __doc__ __package__ __loader__ __spec__ __path__ __file__ __cached__
__builtins__ __all__ __annotations__ __dict__ __weakref__ __module__ __qualname__
__class__ __classcell__ __slots__ __match_args__ __orig_bases__ __parameters__
__type_params__ __abstractmethods__ __isabstractmethod__ __final__ __wrapped__
__signature__ __defaults__ __kwdefaults__ __code__ __closure__ __globals__
__self__ __func__ __objclass__ __args__ __origin__ __metadata__ __required_keys__
__optional_keys__ __total__ __dataclass_fields__ __dataclass_params__
__init__ __new__ __del__ __init_subclass__ __class_getitem__ __mro_entries__
__prepare__ __instancecheck__ __subclasscheck__ __subclasshook__
__get__ __set__ __delete__ __set_name__ __getattr__ __getattribute__
__setattr__ __delattr__ __dir__ __call__ __getstate__ __setstate__
__reduce__ __reduce_ex__ __copy__ __deepcopy__ __missing__
__getnewargs__ __getnewargs_ex__ __mro__ __bases__ __flags__ __subclasses__
__text_signature__ __post_init__ __static_attributes__ __firstlineno__
__repr__ __str__ __bytes__ __format__ __hash__ __bool__ __len__
__iter__ __next__ __reversed__ __contains__ __getitem__ __setitem__
__delitem__ __length_hint__ __enter__ __exit__ __aenter__ __aexit__
__aiter__ __anext__ __await__ __lt__ __le__ __eq__ __ne__ __gt__ __ge__
__add__ __sub__ __mul__ __matmul__ __truediv__ __floordiv__ __mod__
__divmod__ __pow__ __lshift__ __rshift__ __and__ __xor__ __or__
__radd__ __rsub__ __rmul__ __rmatmul__ __rtruediv__ __rfloordiv__
__rmod__ __rdivmod__ __rpow__ __rlshift__ __rrshift__ __rand__
__rxor__ __ror__ __iadd__ __isub__ __imul__ __imatmul__ __itruediv__
__ifloordiv__ __imod__ __ipow__ __ilshift__ __irshift__ __iand__
__ixor__ __ior__ __neg__ __pos__ __abs__ __invert__ __complex__
__int__ __float__ __index__ __round__ __trunc__ __floor__ __ceil__
__fspath__ __sizeof__ __buffer__ __release_buffer__
""".split())
MODULES = frozenset({"__init__", "__main__"})
PLACEHOLDER = "_"
PARENT_SEGMENT = ".."


def forbidden(name: str) -> bool:
    return name.startswith(PLACEHOLDER) and name != PLACEHOLDER and name not in SPECIAL


def read_manifest(path: Path) -> list[str]:
    paths: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(paths, list):
        raise ValueError("Leading-underscores manifest must be a list of Python paths")
    entries = cast(list[object], paths)
    if any(not isinstance(entry, str) for entry in entries):
        raise ValueError("Leading-underscores manifest must be a list of Python paths")
    return cast(list[str], entries)


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    diagnostics: list[dict[str, object]] = []
    error: str | None = None
    seen_paths: set[tuple[str, str]] = set()
    for path in paths:
        source = Path(path)
        if (source.is_absolute() or PARENT_SEGMENT in source.parts
                or source.suffix not in {".py", ".pyi"}
                or not source.parts or source.parts[0] not in {"src", "tests"}
                or not (root / source).resolve().is_relative_to(root.resolve())):
            error = "LEADING_UNDERSCORES_EXECUTION_ERROR"
            diagnostics.append({"rule": error, "path": path, "message": f"Invalid source path: {path!r}",
                                "is_infrastructure_error": True})
            continue
        for index, part in enumerate(source.parts[1:]):
            name = source.stem if index == len(source.parts) - 2 else part
            if name in MODULES and index == len(source.parts) - 2:
                continue
            if name.startswith(PLACEHOLDER):
                location = "/".join(source.parts[:index + 2])
                key = (location, name)
                if key not in seen_paths:
                    seen_paths.add(key)
                    diagnostics.append({"rule": "LEADING_UNDERSCORE_PATH", "path": path,
                                        "symbol": name, "message": f"{location}: name {name!r} starts with _."})
        try:
            text = (root / source).read_text(encoding="utf-8")
            tree = ast.parse(text, filename=path)
        except (OSError, ValueError, SyntaxError) as exc:
            error = "LEADING_UNDERSCORES_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
            continue
        scanner = BindingScanner(path, text.splitlines())
        scanner.visit(tree)
        diagnostics.extend(scanner.diagnostics)
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        diagnostics, error = analyze(Path.cwd(), read_manifest(args.manifest))
    except (OSError, ValueError, TypeError) as exc:
        error = "LEADING_UNDERSCORES_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc), "is_infrastructure_error": True}]
    print(json.dumps({"quard_leading_underscores_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
