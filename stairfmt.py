#!/usr/bin/env python3
"""Staircase formatter/checker for Python call compositions.

Rules:
- In one compositional call tree, no two called names share a source line,
  except for the first dotted call (e.g. a().b()).
- A method after a multiline call shares its closing-parenthesis line.
- Calls with multiple arguments start the argument list on the next line.
- Parenthesized tuples follow the same layout as call arguments.
- Simple arguments may share a line (e.g. Point(\n    x, y\n)).
- A single non-call argument MUST remain inline (e.g. Point(x)).
- The closing parenthesis of a multiline call is on its own line.
- Indentation uses spaces, with tabs reported independently of call layout.

--check works broadly; --fix is conservative (simple nested calls, constructors,
method chains and argument lists). Unsupported constructs and comments are
left intact and reported, not reformatted unsafely.
"""

from __future__ import annotations

import argparse
import ast
from bisect import bisect_right
from datetime import datetime, timezone
from difflib import SequenceMatcher
import io
import json
import re
import sys
import tokenize
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from quard._common import QuardError, atomic_write, project_dir, repo_lock, write_json
from quard._policy import discover_files, read_scope
from quard.policy_utils import read_settings, validate_stairfmt


@dataclass(frozen=True)
class Issue:
    """One source-positioned Stairfmt layout violation."""

    line: int
    col: int
    code: str
    message: str


def _interpolated(node: ast.AST) -> bool:
    return (isinstance(node, ast.JoinedStr)
            or sys.version_info >= (3, 14) and isinstance(node, ast.TemplateStr))


def _under_interpolation(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    while node in parents:
        node = parents[node]
        if _interpolated(node):
            return True
    return False


def _roots(tree: ast.AST) -> list[ast.Call]:
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    result: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _under_interpolation(node, parents):
            continue
        parent = parents.get(node)
        if any(_protected(ancestor) for ancestor in _ancestors(node, parents)):
            continue
        while parent is not None and not isinstance(parent, ast.Call):
            parent = parents.get(parent)
        if parent is None:
            result.append(node)
    return result


def _ancestors(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    result: list[ast.AST] = []
    while node in parents:
        node = parents[node]
        result.append(node)
    return result


def _protected(node: ast.AST) -> bool:
    return isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp, ast.BoolOp, ast.IfExp,
                             ast.Compare, ast.NamedExpr)) or (isinstance(node, ast.Dict)
                                                              and len(node.keys) == 1
                                                              and not _structured(node))


def _visible(node: ast.AST) -> list[ast.Call]:
    if _interpolated(node) or _protected(node):
        return []
    return ([node] if isinstance(node, ast.Call) else []) + [call for child in ast.iter_child_nodes(node)
                                                           for call in _visible(child)]


def _has_nested_call(node: ast.AST) -> bool:
    return bool(_visible(node))


def _multiline(node: ast.AST) -> bool:
    return (isinstance(node, ast.BinOp) and (_multiline(node.left) or _multiline(node.right))
            or isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
            and _structured(node)
            or _has_nested_call(node)
            or isinstance(node, ast.Tuple) and len(node.elts) > 1
            or isinstance(node, ast.Dict) and len(node.keys) > 1
            or isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict))
            and not _protected(node) and any(
                isinstance(part, (ast.Tuple, ast.List, ast.Set, ast.Dict)) or _multiline(part)
                for part in ast.iter_child_nodes(node)))


def _first_dot(call: ast.Call) -> bool:
    return (isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Call)
            and not isinstance(call.func.value.func, ast.Attribute))


def _call_column(call: ast.Call, lines: list[str]) -> int:
    if isinstance(call.func, ast.Attribute):
        return _column(lines, call.func.end_lineno,
                       call.func.end_col_offset - len(call.func.attr.encode("utf-8")))
    return _column(lines, call.func.lineno, call.func.col_offset)


def _field_pair(previous: ast.Call, call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """Allow a dictionary field's callable key and value to share a line."""
    return any(isinstance(node, ast.Dict) and any(
        key is not None and previous in ast.walk(key) and call in ast.walk(value)
        for key, value in zip(node.keys, node.values))
        for node in _ancestors(call, parents))


def _issue_list(root: ast.Call, lines: list[str]) -> list[Issue]:
    issues: list[Issue] = []
    calls = _visible(root)
    grouped: dict[int, list[ast.Call]] = {}
    for call in calls:
        # The END position of the callable (rather than Call.lineno) points to
        # the actual method name in chained calls such as a().b().
        grouped.setdefault(call.func.end_lineno, []).append(call)

        if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Call):
            receiver = call.func.value
            if (receiver.end_lineno is not None
                    and receiver.func.end_lineno is not None
                    and call.func.end_lineno is not None
                    and call.func.end_col_offset is not None
                    and receiver.end_lineno > receiver.func.end_lineno
                    and call.func.end_lineno > receiver.end_lineno):
                issues.append(Issue(
                    call.func.end_lineno, _call_column(call, lines), "ST005",
                    "метод после многострочного вызова должен быть на строке закрывающей скобки"
                ))

        arguments = [*call.args, *call.keywords]
        if len(arguments) > 1:
            first = min(arguments, key=lambda n: (n.lineno, n.col_offset))
            if first.lineno <= call.func.end_lineno:
                issues.append(Issue(
                    first.lineno, _column(lines, first.lineno, first.col_offset), "ST002",
                    "при нескольких аргументах первый должен начинаться с новой строки"
                ))
        elif len(arguments) == 1 and not _multiline(arguments[0]):
            arg = arguments[0]
            # A single simple argument cannot be split across lines. The
            # function name, argument, and closing ')' must share a line.
            if (call.func.end_lineno != arg.lineno or
                    arg.end_lineno != call.end_lineno):
                issues.append(Issue(
                    call.func.end_lineno, _column(lines, call.func.end_lineno, call.func.end_col_offset), "ST003",
                    "единственный простой аргумент должен быть в строке с вызовом: Point(x)"
                ))

        # Parentheses around a nested call (or several arguments) are always
        # multiline and close AFTER the last argument's source line.
        if arguments and (len(arguments) > 1 or _multiline(arguments[0])):
            last = max(arguments, key=lambda n: (n.end_lineno, n.end_col_offset))
            if call.end_lineno <= last.end_lineno:
                issues.append(Issue(
                    call.end_lineno, _column(lines, call.end_lineno, call.end_col_offset) - 1, "ST004",
                    "закрывающая скобка многострочного вызова должна быть на отдельной строке"
                ))

    parents = {child: parent for parent in ast.walk(root)
               for child in ast.iter_child_nodes(parent)}
    for candidates in grouped.values():
        ordered = sorted(candidates, key=lambda n: n.func.end_col_offset)
        previous = ordered[0]
        for call in ordered[1:]:
            if _first_dot(call):
                previous = call
                continue
            if isinstance(call.func, ast.Call) and previous is call.func:
                previous = call
                continue
            if previous is not None and any(
                isinstance(ancestor, ast.BinOp) and previous in ast.walk(ancestor.left)
                and call in ast.walk(ancestor.right)
                for ancestor in _ancestors(call, parents)):
                previous = call
                continue
            if _field_pair(previous, call, parents):
                previous = call
                continue
            issues.append(Issue(
                call.func.end_lineno, _call_column(call, lines), "ST001",
                "следующий вызов функции/конструктора должен начинаться с новой строки"
            ))
            previous = call
    return issues


def _inline_issues(node: ast.AST, source: str, limit: int) -> list[Issue]:
    segment = ast.get_source_segment(source, node)
    if segment is None:
        return []
    issues: list[Issue] = []
    column = _column(_lines(source), node.lineno, node.col_offset)
    lengths = [len(line.strip()) for line in _lines(segment)]
    length = max(lengths, default=0)
    if length > limit or "\n" in segment and not isinstance(node, ast.BinOp):
        issues.append(Issue(node.lineno, column, "ST006",
                            f"упростите выражение: длина {length} превышает предел {limit} "
                            if length > limit else "упростите выражение: оно должно быть в одной строке"))

    def bearing(part: ast.AST) -> bool:
        return (not _interpolated(part)
                and (isinstance(part, ast.Call) or any(bearing(child) for child in ast.iter_child_nodes(part))))

    def branches(part: ast.AST, count: int) -> bool:
        if _interpolated(part):
            return False
        children = list(ast.iter_child_nodes(part))
        if sum(bearing(child) for child in children) >= 2:
            count += 1
        return count > 1 or any(branches(child, count) for child in children)

    if branches(node, 0):
        issues.append(Issue(node.lineno, column, "ST007",
                            "упростите выражение: несколько уровней разветвления вызовов"))
    return issues


def _end_line(node: ast.expr) -> int:
    if node.end_lineno is None:
        raise ValueError("выражение без конечной строки в AST")
    return node.end_lineno


def _container_issues(node: ast.AST, lines: list[str]) -> list[Issue]:
    if isinstance(node, ast.Dict) and len(node.keys) > 1:
        fields = [key if key is not None else value for key, value in zip(node.keys, node.values)]
        if (any(field.lineno <= _end_line(node.values[index - 1])
                for index, field in enumerate(fields[1:], 1))
                or node.end_lineno == node.values[-1].end_lineno):
            return [Issue(node.lineno, _column(lines, node.lineno, node.col_offset), "ST009",
                          "поля словаря и закрывающая скобка должны быть на отдельных строках")]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        children = node.elts
        if (len(children) == 1 and _has_nested_call(children[0])
                and node.end_lineno == children[0].end_lineno):
            return [Issue(node.lineno, _column(lines, node.lineno, node.col_offset), "ST009",
                          "вызов и закрывающая скобка контейнера должны быть на разных строках")]
        for index, child in enumerate(children[1:], 1):
            nested = (isinstance(child, (ast.List, ast.Tuple, ast.Set, ast.Dict))
                      or isinstance(children[index - 1], (ast.List, ast.Tuple, ast.Set, ast.Dict))
                      or _has_nested_call(child) or _has_nested_call(children[index - 1]))
            if (nested and child.lineno <= _end_line(children[index - 1]) or
                    not nested and child.lineno != children[index - 1].lineno):
                return [Issue(child.lineno, _column(lines, child.lineno, child.col_offset), "ST009",
                              "обычные элементы должны делить строку, вызовы и вложенные контейнеры — начинаться с новой")]
    return []


def _structured(node: ast.AST) -> bool:
    return any(isinstance(part, ast.Dict) and len(part.keys) > 1
               for part in ast.walk(node))


def decode_source(data: bytes) -> tuple[str, str]:
    """Decode a Python file according to its BOM and encoding declaration."""
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding), encoding


def _strings(tree: ast.AST, source: str, limit: int
             ) -> list[tuple[Issue, tokenize.TokenInfo, ast.Constant]]:
    """Select ordinary string tokens outside protected and call expressions."""
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    lines = _lines(source)
    selected: list[tuple[Issue, tokenize.TokenInfo, ast.Constant]] = []
    constants = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.STRING:
            continue
        match = re.match(r"(?i)([rubf]*)(['\"])(.*)\2$", token.string, re.DOTALL)
        if match is None or any(letter in match.group(1).lower() for letter in "rbf"):
            continue
        if token.string[len(match.group(1)):].startswith(match.group(2) * 3):
            continue
        if len(match.group(3)) <= limit:
            continue
        start = _index(lines, token.start[0], len(lines[token.start[0] - 1][:token.start[1]].encode("utf-8")))
        end = start + len(token.string)
        for node in constants:
            if (node.lineno is None or node.col_offset is None
                    or node.end_lineno is None or node.end_col_offset is None):
                raise ValueError("строковый литерал без позиции в AST")
            left = _index(lines, node.lineno, node.col_offset)
            right = _index(lines, node.end_lineno, node.end_col_offset)
            if not left <= start < end <= right:
                continue
            ancestry = _ancestors(node, parents)
            if any(isinstance(part, (ast.Call, ast.BinOp)) or _interpolated(part) or _protected(part)
                   for part in ancestry):
                break
            if (isinstance(parents.get(node), ast.Expr)
                    and any(isinstance(parent, (ast.Module, ast.ClassDef,
                                                ast.FunctionDef, ast.AsyncFunctionDef))
                            and parent.body and parent.body[0] is parents[node]
                            for parent in ancestry)):
                break
            selected.append((Issue(token.start[0], token.start[1] + 1, "ST011",
                                   f"строковый литерал длиной {len(match.group(3))} превышает предел {limit}"),
                             token, node))
            break
    return selected


def _pieces(token: str, limit: int) -> list[str]:
    """Split the spelled body only between complete Python escape sequences."""
    match = re.match(r"(?i)(u?)(['\"])(.*)\2$", token, re.DOTALL)
    if match is None:
        return []
    prefix, quote, body = match.groups()
    boundaries: list[int] = []
    index = 0
    while index < len(body):
        if body[index] == "\\":
            index += 1
            if index < len(body) and body[index] in "xXuU":
                index += {"x": 2, "X": 2, "u": 4, "U": 8}[body[index]] + 1
            elif index < len(body) and body[index] == "N" and "}" in body[index:]:
                index = body.index("}", index) + 1
            elif index < len(body) and body[index] in "01234567":
                first = index
                index += 1
                while index < len(body) and index < first + 3 and body[index] in "01234567":
                    index += 1
            else:
                index += 1
        else:
            index += 1
        boundaries.append(index)
    pieces: list[str] = []
    start = 0
    while start < len(body):
        first = bisect_right(boundaries, start)
        last = bisect_right(boundaries, start + limit)
        if first == last:
            return []
        end = boundaries[last - 1]
        if end < len(body):
            end = next((point for point in reversed(boundaries[first:last])
                        if body[point - 1].isspace()), end)
        pieces.append(prefix + quote + body[start:end] + quote)
        start = end
    return pieces


def issues_for(tree: ast.AST, source: str = "", inline_limit: int = 240,
               string_limit: int = 160) -> list[Issue]:
    issues: list[Issue] = []
    lines = _lines(source)
    if source and isinstance(tree, ast.Module):
        issues.extend(_indentation(source, 4)[0])
        issues.extend(issue for issue, _, _ in _strings(tree, source, string_limit))
    for root in _roots(tree):
        issues.extend(_issue_list(root, lines))
        if source:
            start = _index(lines, root.lineno, root.col_offset)
            end = _index(lines, root.end_lineno, root.end_col_offset)
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.COMMENT:
                    pos = _index(lines, token.start[0], len(lines[token.start[0] - 1][:token.start[1]].encode("utf-8")))
                    if start <= pos < end:
                        issues.append(Issue(token.start[0], token.start[1] + 1, "ST008",
                                            "комментарий внутри вызова: перенесите его за пределы вызова"))
            def visit(node: ast.AST, parent: ast.AST) -> None:
                if _interpolated(node):
                    return
                if _protected(node):
                    if not _structured(node):
                        layout = _layout_issues(node, source, inline_limit,
                                                _leading_indent(lines[node.lineno - 1]))
                        issues.extend(issue for issue in _inline_issues(node, source, inline_limit)
                                      if not layout or issue.code != "ST006"
                                      or "длина" in issue.message)
                        issues.extend(layout)
                    else:
                        issues.extend(issue for issue in _inline_issues(node, source, inline_limit)
                                      if issue.code == "ST007" or issue.code == "ST006"
                                      and "длина" in issue.message)
                        for part in ast.walk(node):
                            if isinstance(part, ast.Dict):
                                issues.extend(_container_issues(part, lines))
                        issues.extend(_layout_issues(node, source, inline_limit,
                                                     _leading_indent(lines[node.lineno - 1])))
                    return
                if isinstance(node, ast.BinOp):
                    issues.extend(_inline_issues(node, source, inline_limit))
                    indent = (_leading_indent(lines[node.lineno - 1])
                              if isinstance(parent, (ast.Call, ast.keyword, ast.Dict))
                              else " " * (_column(lines, node.lineno, node.col_offset) - 1))
                    if not isinstance(parent, ast.BinOp):
                        issues.extend(_layout_issues(node, source, inline_limit, indent))
                issues.extend(_container_issues(node, lines))
                for child in ast.iter_child_nodes(node):
                    visit(child, node)
            visit(root, root)
    if isinstance(tree, ast.Module):
        parents = {child: parent for parent in ast.walk(tree)
                   for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not _visible(node) or any(
                    isinstance(part, (ast.Call, ast.BinOp)) or _protected(part)
                    for part in _ancestors(node, parents)):
                continue
            issues.extend(_inline_issues(node, source, inline_limit))
            issues.extend(_layout_issues(node, source, inline_limit,
                                         " " * (_column(lines, node.lineno, node.col_offset) - 1)))
    return sorted(issues, key=lambda i: (i.line, i.col, i.code))


class Renderer:
    """Render supported call trees with their inline-expression boundaries."""

    def __init__(self, source: str, indent_size: int, inline_limit: int) -> None:
        self.source = source
        self.step = " " * indent_size
        self.limit = inline_limit

    def segment(self, node: ast.AST) -> str:
        segment = ast.get_source_segment(self.source, node)
        if segment is None:
            raise ValueError("отсутствует диапазон текста для выражения")
        return segment

    def simple(self, node: ast.AST) -> bool:
        return not _has_nested_call(node) and "\n" not in self.segment(node)

    def compact(self, node: ast.AST) -> str:
        segment = self.segment(node)
        if "\n" not in segment:
            return segment
        if any(isinstance(part, (ast.Constant, ast.JoinedStr))
               and "\n" in (ast.get_source_segment(self.source, part) or "")
               for part in ast.walk(node)):
            return segment
        return re.sub(r"\s*\n\s*", " ", segment)

    def supported_arg(self, arg: ast.AST) -> bool:
        if _protected(arg):
            if isinstance(arg, ast.DictComp):
                return (self.supported_arg(arg.key) and self.supported_arg(arg.value)
                        and self.supported_clauses(arg))
            if isinstance(arg, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                return self.supported_arg(arg.elt) and self.supported_clauses(arg)
            return "\n" not in self.compact(arg) and (
                len(self.compact(arg)) <= self.limit or "\n" not in self.segment(arg))
        if isinstance(arg, ast.BinOp):
            return self.supported_arg(arg.left) and self.supported_arg(arg.right)
        if isinstance(arg, ast.Starred):
            return self.supported_arg(arg.value)
        if isinstance(arg, ast.Call):
            return self.supported(arg)
        if isinstance(arg, (ast.Subscript, ast.Attribute)):
            if _has_nested_call(arg):
                return (_has_nested_call(arg.value) and self.supported_arg(arg.value)
                        and (isinstance(arg, ast.Attribute) or self.simple(arg.slice)))
            return "\n" not in self.segment(arg)
        if isinstance(arg, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
            if isinstance(arg, ast.Dict):
                return (all(key is None or self.supported_arg(key) for key in arg.keys)
                        and all(self.supported_arg(value) for value in arg.values))
            return all(self.supported_arg(item) for item in arg.elts)
        return self.simple(arg)

    def supported_clauses(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
                          ) -> bool:
        return all(self.supported_arg(clause.iter)
                   and all(self.supported_arg(test) for test in clause.ifs)
                   for clause in node.generators)

    def supported(self, call: ast.Call) -> bool:
        if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Call):
            if not self.supported(call.func.value):
                return False
        elif isinstance(call.func, ast.Call):
            if not self.supported(call.func):
                return False
        elif isinstance(call.func, ast.Subscript) and _has_nested_call(call.func):
            if not self.supported_arg(call.func):
                return False
        elif not self.simple(call.func):
            return False

        return (all(self.supported_arg(arg) for arg in call.args)
                and all(self.supported_arg(kw.value) for kw in call.keywords))

    def render_arg(self, arg: ast.AST, indent: str) -> str:
        if isinstance(arg, ast.BinOp):
            return self.binary(arg, indent)
        if isinstance(arg, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            return self.comprehension(arg, indent)
        if _protected(arg):
            return self.compact(arg)
        if isinstance(arg, ast.Starred):
            return "*" + self.render_arg(arg.value, indent)
        if isinstance(arg, ast.Call):
            return self.render_call(arg, indent, in_call=True)
        if isinstance(arg, (ast.Subscript, ast.Attribute)) and _has_nested_call(arg.value):
            lines = _lines(self.source)
            left = _index(lines, arg.lineno, arg.col_offset)
            start = _index(lines, arg.value.lineno, arg.value.col_offset)
            end = _index(lines, arg.value.end_lineno, arg.value.end_col_offset)
            right = _index(lines, arg.end_lineno, arg.end_col_offset)
            return (self.source[left:start] + self.render_arg(arg.value, indent)
                    + self.source[end:right])
        if isinstance(arg, ast.keyword):
            prefix = f"{arg.arg}=" if arg.arg is not None else "**"
            if isinstance(arg.value, ast.NamedExpr):
                return prefix + "(" + self.render_arg(arg.value, indent) + ")"
            return prefix + self.render_arg(arg.value, indent)
        if isinstance(arg, ast.Dict):
            if not arg.keys:
                return "{}"
            fields = [f"{self.render_arg(key, indent + self.step)}: {self.render_arg(value, indent + self.step)}"
                      if key is not None else "**" + self.render_arg(value, indent + self.step)
                      for key, value in zip(arg.keys, arg.values)]
            return "{\n" + ",\n".join(indent + self.step + field for field in fields) + "\n" + indent + "}"
        if isinstance(arg, (ast.Tuple, ast.List, ast.Set)):
            opening, closing = ("(", ")") if isinstance(arg, ast.Tuple) else (
                ("[", "]") if isinstance(arg, ast.List) else ("{", "}"))
            if not arg.elts:
                return opening + closing
            result = self.render_values(arg.elts, "", indent, tuple_mode=isinstance(arg, ast.Tuple),
                                        container_mode=True)
            return opening + result[1:-1] + closing
        return self.segment(arg)

    def binary(self, node: ast.BinOp, indent: str) -> str:
        lines = _lines(self.source)
        begin = _index(lines, node.lineno, node.col_offset)
        operand = _index(lines, node.left.lineno, node.left.col_offset)
        left = _index(lines, node.left.end_lineno, node.left.end_col_offset)
        right = _index(lines, node.right.lineno, node.right.col_offset)
        between = self.source[left:right]
        if "#" in between:
            raise ValueError("комментарий между операндами бинарного выражения")
        prefix = self.render_arg(node.left, indent)
        suffix = self.render_arg(node.right, indent)
        # Retain parentheses around operands, rather than reconstructing precedence.
        gap = re.sub(r"\s*\n\s*", " ", between)
        if isinstance(node.right, ast.Call) and "\n" in suffix and "(" not in gap:
            gap = gap.rstrip() + "\n" + indent
        return self.source[begin:operand] + prefix + gap + suffix + self.source[
            _index(lines, node.right.end_lineno, node.right.end_col_offset):
            _index(lines, node.end_lineno, node.end_col_offset)]

    def comprehension(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
                      indent: str) -> str:
        key = self.render_arg(node.key, indent + self.step) if isinstance(node, ast.DictComp) else ""
        if isinstance(node, ast.DictComp):
            value = self.render_arg(node.value, indent + self.step)
            if "\n" not in key + value:
                return self.compact(node)
            element = key + ": " + value
            opening, closing = "{", "}"
        else:
            element = self.render_arg(node.elt, indent + self.step)
            if "\n" not in element and not _structured(node):
                return self.compact(node)
            opening, closing = ("(", ")") if isinstance(node, ast.GeneratorExp) else (
                ("[", "]") if isinstance(node, ast.ListComp) else ("{", "}"))
        clauses = []
        for clause in node.generators:
            text = ("async for " if clause.is_async else "for ") + self.compact(clause.target)
            text += " in " + self.render_arg(clause.iter, indent + self.step)
            text += "".join(" if " + ("(" + self.render_arg(condition, indent + self.step) + ")"
                                         if isinstance(condition, ast.NamedExpr)
                                         else self.render_arg(condition, indent + self.step))
                            for condition in clause.ifs)
            clauses.append(text)
        prefix = indent + self.step
        if isinstance(node, ast.DictComp) and "\n" in key:
            first, *rest = clauses
            lines = [prefix + element + " " + first]
            lines.extend(prefix + self.step * (index + 1) + clause
                         for index, clause in enumerate(rest))
            return opening + "\n" + "\n".join(lines) + "\n" + indent + closing
        return (opening + "\n" + prefix + element + "\n"
                + "\n".join(prefix + clause for clause in clauses) + "\n" + indent + closing)

    def render_args(self, call: ast.Call, name: str, indent: str) -> str:
        args = sorted([*call.args, *call.keywords], key=lambda a: (a.lineno, a.col_offset))
        lines = _lines(self.source)
        wrappers: list[int] = []
        for index, arg in enumerate(args):
            if not isinstance(arg, ast.BinOp):
                wrappers.append(0)
                continue
            previous = call.func if index == 0 else args[index - 1]
            following = call if index == len(args) - 1 else args[index + 1]
            before = self.source[_index(lines, previous.end_lineno, previous.end_col_offset):
                                 _index(lines, arg.lineno, arg.col_offset)]
            after = self.source[_index(lines, arg.end_lineno, arg.end_col_offset):
                                _index(lines, following.end_lineno, following.end_col_offset)
                                if following is call else _index(lines, following.lineno,
                                                                 following.col_offset)]
            opening = len(before) - len(before.rstrip(' ('))
            closing = len(after) - len(after.lstrip(' )'))
            wrappers.append(max(0, min((before[-opening:] if opening else '').count('(')
                                        - (index == 0),
                                        after[:closing].count(')') - (index == len(args) - 1))))
        return self.render_values(args, name, indent, wrappers=wrappers)

    def render_values(self, args: Sequence[ast.AST], name: str, indent: str,
                      tuple_mode: bool = False, container_mode: bool = False,
                      wrappers: Sequence[int] = ()) -> str:
        if not args:
            return name + "()"
        child_indent = indent + self.step
        values = [self.render_arg(arg, child_indent) for arg in args]
        values = ['(' * wrappers[index] + value + ')' * wrappers[index]
                  if wrappers and wrappers[index] > 0 else value
                  for index, value in enumerate(values)]
        def separate(arg: ast.AST, value: str) -> bool:
            return (_has_nested_call(arg) or "\n" in value
                    or (container_mode or _multiline(arg))
                    and isinstance(arg, (ast.List, ast.Tuple, ast.Set, ast.Dict)) and not _protected(arg))

        force_break = (len(args) > 1 or any(separate(arg, value)
                                             for arg, value in zip(args, values)))
        if not force_break:
            return f"{name}({values[0]}{',' if tuple_mode else ''})"

        # Multiple simple values can share a line: Point(\n    x, y\n).
        # An actual nested call must start on a new line: f(\n    g(x)\n).
        rendered: list[str] = []
        pending: list[str] = []
        attached = False

        def flush() -> None:
            if pending:
                rendered.append(child_indent + ", ".join(pending))
                pending.clear()

        for index, (arg, value) in enumerate(zip(args, values)):
            isolated = separate(arg, value)
            if isolated:
                flush()
                rendered.append(child_indent + value)
            else:
                if (rendered and not pending and (attached or index
                        and isinstance(args[index - 1], ast.BinOp)
                        and "\n" in values[index - 1]) and "\n" not in value):
                    rendered[-1] += (", " if attached else " ") + value
                    attached = True
                else:
                    pending.append(value)
            # Separate adjacent values; a singleton tuple needs its own comma.
            if index < len(args) - 1 and isolated:
                rendered[-1] += ","
            elif index < len(args) - 1:
                # The comma between simple arguments is supplied by join().
                # If the next argument is a nested call, add a trailing comma
                # to the last simple argument on its line instead.
                if separate(args[index + 1], values[index + 1]):
                    flush()
                    rendered[-1] += ","
        flush()
        if tuple_mode and len(args) == 1:
            rendered[-1] += ","
        return name + "(\n" + "\n".join(rendered) + "\n" + indent + ")"

    def render_call(self, call: ast.Call, indent: str, in_call: bool = False) -> str:
        # Recover e.g. a.b().c(x).d() as a base call followed by method calls.
        methods: list[ast.Call] = []
        base = call
        while isinstance(base.func, ast.Attribute) and isinstance(base.func.value, ast.Call):
            methods.append(base)
            base = base.func.value
        if not methods:
            if isinstance(call.func, ast.Call):
                name = self.render_call(call.func, indent, in_call=True)
            elif isinstance(call.func, ast.Subscript) and _has_nested_call(call.func):
                name = self.render_arg(call.func, indent)
            elif isinstance(call.func, ast.Lambda):
                name = "(" + self.compact(call.func) + ")"
            else:
                name = self.segment(call.func)
            return self.render_args(call, name, indent)

        inline = _first_dot(methods[-1]) and len(methods) == 1
        chain_indent = indent if in_call or inline else indent + self.step
        previous = self.render_call(base, chain_indent, in_call=True)
        output = previous
        for step_call in reversed(methods):
            method_name = "." + step_call.func.attr
            prefix = "" if _first_dot(step_call) or "\n" in previous else "\n" + chain_indent
            previous = self.render_args(step_call, method_name, chain_indent)
            output += prefix + previous
        if in_call or inline:
            return output
        return "(\n" + chain_indent + output + "\n" + indent + ")"


def _layout_issues(node: ast.AST, source: str, limit: int, indent: str) -> list[Issue]:
    lines = _lines(source)
    left = _index(lines, node.lineno, node.col_offset)
    right = _index(lines, node.end_lineno, node.end_col_offset)
    if _has_comments(source, left, right, lines):
        return []
    renderer = Renderer(source, 4, limit)
    if not renderer.supported_arg(node):
        return []
    try:
        expected = renderer.render_arg(node, indent)
        if (_without_endings(source[left:right]) == _without_endings(expected)
                or "\n" not in source[left:right] + expected):
            return []
        try:
            candidate = ast.parse(source[:left] + expected + source[right:])
        except SyntaxError:
            candidate = ast.parse(source[:left] + "(" + expected + ")" + source[right:])
        if ast.dump(candidate, include_attributes=False) != ast.dump(
                ast.parse(source), include_attributes=False):
            return []
    except (SyntaxError, ValueError):
        return []
    return [Issue(node.lineno, _column(lines, node.lineno, node.col_offset), "ST012",
                  "исправимый перенос составного выражения не соответствует канонической раскладке")]


def _index(lines: list[str], lineno: int, byte_col: int) -> int:
    prefix = sum(len(line) for line in lines[:lineno - 1])
    # CPython AST columns are UTF-8 byte offsets rather than Unicode indices.
    return prefix + _column(lines, lineno, byte_col) - 1


def _column(lines: list[str], lineno: int, byte_col: int) -> int:
    if not lines:
        return byte_col + 1
    return len(lines[lineno - 1].encode("utf-8")[:byte_col].decode("utf-8")) + 1


def _lines(source: str) -> list[str]:
    """Split only at physical Python source line endings."""
    return re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", source)[:-1]


def _leading_indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _without_endings(source: str) -> str:
    return source.replace("\r\n", "\n").replace("\r", "\n")


def _ending(line: str) -> str:
    match = re.search(r"(?:\r\n|\n|\r)$", line)
    return match.group() if match else ""


def _local_endings(source: str, start: int, end: int, replacement: str) -> str:
    """Keep surviving line endings and give inserted lines nearby source style."""
    original = _lines(source[start:end])
    rendered = _lines(replacement)
    lines = _lines(source)
    position = len(re.findall(r"\r\n|\r|\n", source[:start]))
    nearby = _ending(lines[position]) if position < len(lines) else ""
    if not nearby:
        nearby = next((_ending(line) for line in reversed(lines[:position]) if _ending(line)), "\n")
    result = rendered.copy()
    matcher = SequenceMatcher(None, [_without_endings(line).rstrip("\n") for line in original],
                              [_without_endings(line).rstrip("\n") for line in rendered],
                              autojunk=False)
    for _, first, last, left, right in matcher.get_opcodes():
        for index in range(left, right):
            line = rendered[index]
            ending = _ending(line)
            if not ending:
                continue
            offset = min(first + index - left, last - 1) if last > first else first
            style = _ending(original[min(offset, len(original) - 1)]) if original else nearby
            result[index] = line[:-len(ending)] + (style or nearby)
    return "".join(result)


def _indentation(source: str, size: int) -> tuple[list[Issue], str]:
    """Locate indentation tabs and render block depths without touching literals."""
    lines = _lines(source)
    depths: dict[int, int] = {}
    protected: set[int] = set()
    fstrings: list[int] = []
    prefixes = [""]
    logical = True
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.INDENT:
            prefixes.append(token.string)
        elif token.type == tokenize.DEDENT:
            prefixes.pop()
        elif token.type == tokenize.NEWLINE:
            logical = True
        elif token.type == tokenize.STRING:
            protected.update(range(token.start[0] + 1, token.end[0] + 1))
        elif token.type == tokenize.FSTRING_START or tokenize.tok_name[token.type] == "TSTRING_START":
            fstrings.append(token.start[0])
        elif token.type == tokenize.FSTRING_END or tokenize.tok_name[token.type] == "TSTRING_END":
            protected.update(range(fstrings.pop() + 1, token.end[0] + 1))
        if (logical and token.type not in (tokenize.INDENT, tokenize.DEDENT,
                                           tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT,
                                           tokenize.ENDMARKER)):
            depths[token.start[0]] = len(prefixes) - 1
            logical = False

    issues: list[Issue] = []
    for number, line in enumerate(lines, 1):
        if number in protected:
            continue
        prefix = _leading_indent(line)
        if "\t" in prefix:
            issues.append(Issue(number, prefix.index("\t") + 1, "ST010",
                                "табуляция в отступе: используйте пробелы"))
        if number in depths:
            replacement = " " * (depths[number] * size)
        else:
            replacement = prefix.expandtabs(size) if "\t" in prefix else prefix
        lines[number - 1] = replacement + line[len(prefix):]
    return issues, "".join(lines)


def _has_comments(source: str, start: int, end: int, lines: list[str]) -> bool:
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                pos = _index(lines, tok.start[0], len(lines[tok.start[0] - 1][:tok.start[1]].encode("utf-8")))
                if start <= pos < end:
                    return True
    except tokenize.TokenError:
        return True
    return False


def _trailing_comment(source: str, end: int) -> bool:
    """Keep trailing comments attached to their original source line."""
    line = re.split(r"\r\n|\r|\n", source[end:], maxsplit=1)[0]
    return _has_comments(source, end, end + len(line), _lines(source))


def format_source(source: str, indent_size: int = 4, inline_limit: int = 240,
                  string_limit: int = 160) -> tuple[str, list[str]]:
    if indent_size != 4:
        raise ValueError("--indent должен быть равен 4: только четыре пробела каноничны")
    tree = ast.parse(source)
    _, normalized = _indentation(source, indent_size)
    if normalized != source:
        if ast.dump(ast.parse(normalized), include_attributes=False) != ast.dump(tree, include_attributes=False):
            raise ValueError("нормализация отступов меняет структуру AST")
        source = normalized
        tree = ast.parse(source)
    lines = _lines(source)
    selected: dict[ast.Constant, list[tokenize.TokenInfo]] = {}
    for _, token, node in _strings(tree, source, string_limit):
        selected.setdefault(node, []).append(token)
    warnings: list[str] = []
    for node, tokens in sorted(selected.items(), key=lambda item: item[1][0].start, reverse=True):
        start = _index(lines, node.lineno, node.col_offset)
        end = _index(lines, node.end_lineno, node.end_col_offset)
        if any("\n" in token.string for token in tokens):
            warnings.append(f"{node.lineno}: пропущено: физический перенос внутри строкового литерала")
            continue
        if _trailing_comment(source, end):
            warnings.append(f"{node.lineno}: пропущено: комментарий в конце строки")
            continue
        if _has_comments(source, start, end, lines):
            warnings.append(f"{node.lineno}: пропущено: комментарий внутри строкового литерала")
            continue
        literals = [token for token in tokenize.generate_tokens(io.StringIO(source).readline)
                    if token.type == tokenize.STRING and start <= _index(
                        lines, token.start[0], len(lines[token.start[0] - 1][
                            :token.start[1]].encode("utf-8"))) < end]
        pieces = [_pieces(token.string, string_limit) if any(
            token.start == selected.start and token.string == selected.string
            for selected in tokens) else [token.string]
                  for token in literals]
        if any(not part for part in pieces):
            warnings.append(f"{node.lineno}: пропущено: нет безопасной границы для строкового литерала")
            continue
        indent = _leading_indent(lines[node.lineno - 1])
        replacement = "(\n" + "".join(indent + " " * indent_size + piece + "\n"
                                      for group in pieces for piece in group) + indent + ")"
        replacement = _local_endings(source, start, end, replacement)
        candidate = source[:start] + replacement + source[end:]
        try:
            if ast.dump(ast.parse(candidate), include_attributes=False) == ast.dump(tree, include_attributes=False):
                source = candidate
                lines = _lines(source)
            else:
                warnings.append(f"{node.lineno}: пропущено: перенос меняет структуру AST")
        except SyntaxError:
            warnings.append(f"{node.lineno}: пропущено: нет безопасного синтаксического переноса строки")
    tree = ast.parse(source)
    renderer = Renderer(source, indent_size, inline_limit)
    lines = _lines(source)
    edits: list[tuple[int, int, str]] = []
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not _visible(node) or any(
                isinstance(part, (ast.Call, ast.BinOp)) or _protected(part)
                for part in _ancestors(node, parents)):
            continue
        left = _index(lines, node.lineno, node.col_offset)
        right = _index(lines, node.end_lineno, node.end_col_offset)
        indent = " " * (_column(lines, node.lineno, node.col_offset) - 1)
        if (any(start <= left < end for start, end, _ in edits)
                or _has_comments(source, left, right, lines)
                or _trailing_comment(source, right)
                or not renderer.supported_arg(node)
                or not _layout_issues(node, source, inline_limit, indent)):
            continue
        try:
            replacement = renderer.render_arg(node, indent)
            if _without_endings(replacement) != _without_endings(source[left:right]):
                try:
                    _ = ast.parse(source[:left] + replacement + source[right:])
                except SyntaxError:
                    replacement = "(" + renderer.render_arg(node, indent + " ") + ")"
                edits.append((left, right, _local_endings(source, left, right, replacement)))
        except (SyntaxError, ValueError) as exc:
            warnings.append(f"{node.lineno}: пропущено: {exc}")
    for root in _roots(tree):
        if any(left <= _index(lines, root.lineno, root.col_offset) < right
               for left, right, _ in edits):
            continue
        findings = issues_for(root, source, inline_limit)
        if not findings:
            continue
        start = _index(lines, root.lineno, root.col_offset)
        end = _index(lines, root.end_lineno, root.end_col_offset)
        blocked = _has_comments(source, start, end, lines)
        if _trailing_comment(source, end):
            warnings.append(f"{root.lineno}: пропущено: комментарий привязан к строке вызова")
            continue
        if blocked:
            warnings.append(f"{root.lineno}: пропущено: комментарий внутри вызова")

        def collect(call: ast.Call) -> None:
            left = _index(lines, call.lineno, call.col_offset)
            right = _index(lines, call.end_lineno, call.end_col_offset)
            if (any(issue.code in {"ST001", "ST002", "ST003", "ST004", "ST005", "ST009", "ST012"}
                    or issue.code == "ST006" and "одной строке" in issue.message
                    for issue in issues_for(call, source, inline_limit))
                    and not _has_comments(source, left, right, lines)
                    and renderer.supported(call)):
                indent = _leading_indent(lines[call.lineno - 1])
                try:
                    edits.append((left, right, _local_endings(
                        source, left, right, renderer.render_call(call, indent))))
                    return
                except ValueError:
                    pass
            for child in _visible(call)[1:]:
                if child is not call and not any(
                    left <= edit[0] < right for edit in edits):
                    collect(child)

        if blocked or not renderer.supported(root):
            if not blocked and any(issue.code in {"ST001", "ST002", "ST003", "ST004", "ST005", "ST009", "ST012"}
                                   for issue in findings):
                unsupported = next((arg for arg in [*root.args, *(kw.value for kw in root.keywords)]
                                    if not renderer.supported_arg(arg)), root.func)
                warnings.append(f"{root.lineno}: пропущено: {type(unsupported).__name__} "
                                "нельзя безопасно разложить внутри вызова")
            for child in _visible(root)[1:]:
                if not any(start <= edit[0] < end for edit in edits):
                    collect(child)
        else:
            collect(root)

    formatted = source
    # Test each edit independently so an unsafe branch cannot undo safe peers.
    for start, end, replacement in sorted(edits, reverse=True):
        candidate = formatted[:start] + replacement + formatted[end:]
        try:
            if ast.dump(ast.parse(candidate), include_attributes=False) == ast.dump(tree, include_attributes=False):
                formatted = candidate
            else:
                warnings.append("изменение отменено: результат меняет структуру AST")
        except SyntaxError:
            warnings.append("изменение отменено: результат синтаксически некорректен")
    return formatted, warnings


def _paths(root: Path, inputs: list[str]) -> list[Path]:
    files = discover_files(root, read_scope(root))
    available = {root / path for profile in ("production", "tests")
                 for path in files[profile] if path.endswith(".py")}
    if not inputs:
        return sorted(available)
    selected: set[Path] = set()
    for value in inputs:
        path = (root / value).absolute()
        if not path.is_relative_to(root) or ".." in path.relative_to(root).parts:
            raise ValueError(f"Путь вне проекта: {value}")
        matching = {candidate for candidate in available
                    if candidate == path or path.is_dir() and candidate.is_relative_to(path)}
        if not matching:
            raise ValueError(f"Файл или каталог вне области проверки Quard: {value}")
        selected.update(matching)
    return sorted(selected)


def _advice(reason: str) -> str:
    if "нет безопасной границы" in reason:
        return "Разделите литерал вручную без изменения escape-последовательностей."
    if "физический перенос" in reason or "комментарий в конце строки" in reason:
        return "Расставьте строковый литерал вручную, сохранив перенос или комментарий."
    if "строковый литерал" in reason:
        return "Запустите --fix для переноса строкового литерала по частям."
    if "упростите выражение" in reason:
        return "Упростите выражение: сократите его длину или разделите разветвления вызовов."
    if "комментарий внутри вызова" in reason:
        return "Перенесите комментарий за пределы вызова или расставьте вызовы вручную."
    if "сложное выражение" in reason:
        return "Расставьте вложенные вызовы вручную, сохранив смысл выражения."
    if "табуляция" in reason:
        return "Запустите --fix для замены табуляции в отступе пробелами."
    return "Расставьте вызовы вручную и повторите --fix."


def _report(root: Path, paths: list[Path], fix: bool, indent: int,
            limits: dict[str, int]) -> dict[str, object]:
    changed: list[str] = []
    remaining: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for path in paths:
        name = path.relative_to(root).as_posix()
        try:
            source, encoding = decode_source(path.read_bytes())
            tree = ast.parse(source, filename=name)
            if fix:
                formatted, skipped = format_source(source, indent, **limits)
                for warning in skipped:
                    position, separator, reason = warning.partition(": ")
                    warnings.append({"path": name, "line": int(position) if separator and
                                     position.isdecimal() else None, "reason": reason if separator
                                     else warning, "suggestion": _advice(warning)})
                if formatted != source:
                    atomic_write(path, formatted.encode(encoding), root)
                    changed.append(name)
                tree = ast.parse(formatted, filename=name)
            remaining.extend({"path": name, "line": issue.line, "column": issue.col,
                              "rule": issue.code, "message": issue.message,
                               "suggestion": _advice(issue.message)} for issue in issues_for(
                                   tree, formatted if fix else source, **limits))
        except (OSError, UnicodeError, SyntaxError, LookupError, ValueError, TypeError, IndexError) as exc:
            errors.append({"path": name, "message": str(exc),
                           "suggestion": "Исправьте файл или доступ к нему и повторите --fix."})
    code = 2 if errors else (1 if remaining or warnings else 0)
    return {"schema_version": "quard-stairfmt-fix/v1", "status": {0: "passed", 1: "violations",
            2: "error"}[code], "exit_code": code, "checked_files": len(paths),
            "changed_files": changed, "remaining": remaining, "warnings": warnings, "errors": errors}


def _display(report: dict[str, object]) -> None:
    print(f"STAIRFMT {str(report['status']).upper()}: изменено {len(report['changed_files'])}; "
          f"осталось {len(report['remaining'])} нарушений, "
          f"{len(report['warnings'])} пропусков, {len(report['errors'])} ошибок")
    for path in report["changed_files"]:
        print(f"CHANGED: {path}")
    for warning in report["warnings"]:
        print(f"{warning['path']}:{warning['line'] or '?'}: {warning['reason']} — "
              f"{warning['suggestion']}")
    for issue in report["remaining"]:
        print(f"{issue['path']}:{issue['line']}:{issue['column']}: {issue['rule']} "
              f"{issue['message']} — {issue['suggestion']}")
    for error in report["errors"]:
        print(f"{error['path']}: ошибка: {error['message']} — {error['suggestion']}")
    if "report_path" in report:
        print(f"REPORT: {report['report_path']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="проверить стиль (по умолчанию)")
    mode.add_argument("--fix", action="store_true", help="исправить поддерживаемые случаи")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="корень проекта")
    parser.add_argument("--json", action="store_true", help="вывести полный отчёт JSON")
    parser.add_argument("--indent", type=int, default=4, help="канонический отступ: только 4")
    parser.add_argument("paths", nargs="*", help="файлы .py или каталоги внутри области Quard")
    args = parser.parse_args(argv)
    try:
        if args.indent != 4:
            raise ValueError("--indent должен быть равен 4: только четыре пробела каноничны")
        root = project_dir(args.project)
        with repo_lock(root):
            try:
                paths = _paths(root, args.paths)
                limits = validate_stairfmt(read_settings(root).get("stairfmt", {}))
                report = _report(root, paths, args.fix, args.indent, limits)
            except (QuardError, OSError, UnicodeError, ValueError) as exc:
                report = {"schema_version": "quard-stairfmt-fix/v1", "status": "error",
                          "exit_code": 2, "changed_files": [], "remaining": [], "warnings": [],
                          "errors": [{"path": str(args.project), "message": str(exc),
                                      "suggestion": "Проверьте настройки проекта и повторите команду."}]}
            if args.fix:
                run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
                archive = root / ".quard/reports/stairfmt" / f"{run_id}.json"
                report["report_path"] = archive.relative_to(root).as_posix()
                try:
                    write_json(archive, report, root)
                except (QuardError, OSError, UnicodeError, ValueError) as exc:
                    del report["report_path"]
                    report["status"] = "error"
                    report["exit_code"] = 2
                    report["errors"].append({"path": archive.relative_to(root).as_posix(),
                                             "message": f"Не удалось сохранить архив отчёта: {exc}",
                                             "suggestion": "Проверьте хранилище отчётов и повторите команду."})
                else:
                    latest = root / ".quard/reports/stairfmt/latest.json"
                    try:
                        write_json(latest, report, root)
                    except (QuardError, OSError, UnicodeError, ValueError) as exc:
                        report["status"] = "error"
                        report["exit_code"] = 2
                        report["errors"].append({"path": latest.relative_to(root).as_posix(),
                                                 "message": f"latest.json не обновлён: {exc}",
                                                 "suggestion": "Используйте архив отчёта и проверьте хранилище."})
    except (QuardError, OSError, UnicodeError, ValueError) as exc:
        report = {"schema_version": "quard-stairfmt-fix/v1", "status": "error", "exit_code": 2,
                  "changed_files": [], "remaining": [], "warnings": [],
                  "errors": [{"path": str(args.project), "message": str(exc),
                              "suggestion": "Проверьте настройки проекта и повторите команду."}]}
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        _display(report)
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
