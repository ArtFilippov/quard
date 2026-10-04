"""Calculate LCOM4 components from methods and their direct references."""
from __future__ import annotations

import ast

from eryx.cohesion_analysis.declaration_utils import METHOD
from eryx.cohesion_analysis.declarations import Declarations
from eryx.cohesion_references.method_references import MethodReferences

# Python data-model hooks, rather than a blanket double-underscore pattern.
SPECIAL_METHODS = frozenset("""
__new__ __init__ __del__ __repr__ __str__ __bytes__ __format__ __lt__ __le__ __eq__
__ne__ __gt__ __ge__ __hash__ __bool__ __getattr__ __getattribute__ __setattr__
__delattr__ __dir__ __get__ __set__ __delete__ __set_name__ __slots__
__init_subclass__ __class_getitem__ __call__ __len__ __length_hint__ __getitem__
__setitem__ __delitem__ __missing__ __iter__ __next__ __reversed__ __contains__
__add__ __sub__ __mul__ __matmul__ __truediv__ __floordiv__ __mod__ __divmod__
__pow__ __lshift__ __rshift__ __and__ __xor__ __or__ __radd__ __rsub__ __rmul__
__rmatmul__ __rtruediv__ __rfloordiv__ __rmod__ __rdivmod__ __rpow__ __rlshift__
__rrshift__ __rand__ __rxor__ __ror__ __iadd__ __isub__ __imul__ __imatmul__
__itruediv__ __ifloordiv__ __imod__ __ipow__ __ilshift__ __irshift__ __iand__
__ixor__ __ior__ __neg__ __pos__ __abs__ __invert__ __complex__ __int__
__float__ __index__ __round__ __trunc__ __floor__ __ceil__ __enter__ __exit__
__aenter__ __aexit__ __await__ __aiter__ __anext__ __fspath__ __copy__
__deepcopy__ __reduce__ __reduce_ex__ __getstate__ __setstate__ __getnewargs__
__getnewargs_ex__ __sizeof__ __instancecheck__ __subclasscheck__ __mro_entries__
__prepare__ __post_init__ __buffer__ __release_buffer__
""".split())


def is_placeholder(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = method.body
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
        if not body:
            return True
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return stmt.value.value is Ellipsis
    if isinstance(stmt, ast.Return):
        return stmt.value is None or isinstance(stmt.value, ast.Constant) and stmt.value.value is None
    if isinstance(stmt, ast.Raise):
        exc = stmt.exc
        return (isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
                or isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name)
                and exc.func.id == "NotImplementedError")
    return False


def eligible(method: ast.FunctionDef | ast.AsyncFunctionDef, declarations: Declarations) -> bool:
    return (method.name not in SPECIAL_METHODS
            and not declarations.decorated(method, "overload", "typing")
            and not declarations.decorated(method, "overload", "typing_extensions")
            and not declarations.is_abstract(method)
            and not is_placeholder(method))


def components(node: ast.ClassDef, declarations: Declarations) -> list[list[str]]:
    methods = [item for item in node.body if isinstance(item, METHOD) and eligible(item, declarations)]
    names = {item.name for item in methods}
    declared = {item.name for item in node.body if isinstance(item, METHOD)}
    references: list[MethodReferences] = []
    for method in methods:
        instance_receiver = None
        class_receivers = {node.name}
        arguments = method.args.posonlyargs + method.args.args
        static = any(isinstance(decorator, ast.Name) and decorator.id == "staticmethod"
                     for decorator in method.decorator_list)
        classmethod = any(isinstance(decorator, ast.Name) and decorator.id == "classmethod"
                          for decorator in method.decorator_list)
        if arguments and not static:
            if classmethod:
                class_receivers.add(arguments[0].arg)
            else:
                instance_receiver = arguments[0].arg
        refs = MethodReferences(instance_receiver, class_receivers, names, declared)
        for statement in method.body:
            refs.visit(statement)
        references.append(refs)

    # Edges are undirected: a call or a shared field connects both methods.
    parents = list(range(len(methods)))

    def root(index: int) -> int:
        while parents[index] != index:
            index = parents[index]
        return index

    def connect(left: int, right: int) -> None:
        parents[root(left)] = root(right)

    for index, refs in enumerate(references):
        for other, method in enumerate(methods):
            if (index == other and refs.connects_to(refs, method.name)
                    or index != other and refs.connects_to(references[other], method.name)):
                connect(index, other)
    groups: dict[int, list[str]] = {}
    for index, method in enumerate(methods):
        groups.setdefault(root(index), []).append(method.name)
    return list(groups.values())
