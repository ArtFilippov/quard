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
from datetime import datetime, timezone
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


def _under_fstring(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.JoinedStr):
            return True
    return False


def _roots(tree: ast.AST) -> list[ast.Call]:
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    result: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _under_fstring(node, parents):
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
                             ast.GeneratorExp, ast.BinOp, ast.BoolOp, ast.IfExp,
                             ast.Compare, ast.NamedExpr)) or isinstance(node, ast.Dict) and len(node.keys) == 1


def _visible(node: ast.AST) -> list[ast.Call]:
    if isinstance(node, ast.JoinedStr) or _protected(node):
        return []
    return ([node] if isinstance(node, ast.Call) else []) + [call for child in ast.iter_child_nodes(node)
                                                           for call in _visible(child)]


def _has_nested_call(node: ast.AST) -> bool:
    return bool(_visible(node))


def _multiline(node: ast.AST) -> bool:
    return (_has_nested_call(node)
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


def _issue_list(root: ast.Call) -> list[Issue]:
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
                    call.func.end_lineno, call.func.end_col_offset + 1, "ST005",
                    "метод после многострочного вызова должен быть на строке закрывающей скобки"
                ))

        arguments = [*call.args, *call.keywords]
        if len(arguments) > 1:
            first = min(arguments, key=lambda n: (n.lineno, n.col_offset))
            if first.lineno <= call.func.end_lineno:
                issues.append(Issue(
                    first.lineno, first.col_offset + 1, "ST002",
                    "при нескольких аргументах первый должен начинаться с новой строки"
                ))
        elif len(arguments) == 1 and not _multiline(arguments[0]):
            arg = arguments[0]
            # A single simple argument cannot be split across lines. The
            # function name, argument, and closing ')' must share a line.
            if (call.func.end_lineno != arg.lineno or
                    arg.end_lineno != call.end_lineno):
                issues.append(Issue(
                    call.func.end_lineno, call.func.end_col_offset + 1, "ST003",
                    "единственный простой аргумент должен быть в строке с вызовом: Point(x)"
                ))

        # Parentheses around a nested call (or several arguments) are always
        # multiline and close AFTER the last argument's source line.
        if arguments and (len(arguments) > 1 or _multiline(arguments[0])):
            last = max(arguments, key=lambda n: (n.end_lineno, n.end_col_offset))
            if call.end_lineno <= last.end_lineno:
                issues.append(Issue(
                    call.end_lineno, call.end_col_offset, "ST004",
                    "закрывающая скобка многострочного вызова должна быть на отдельной строке"
                ))

    for line, candidates in grouped.items():
        for call in sorted(candidates, key=lambda n: n.func.end_col_offset)[1:]:
            if _first_dot(call):
                continue
            issues.append(Issue(
                call.func.end_lineno, call.func.end_col_offset + 1, "ST001",
                "следующий вызов функции/конструктора должен начинаться с новой строки"
            ))
    return issues


def _inline_issues(node: ast.AST, source: str, limit: int) -> list[Issue]:
    segment = ast.get_source_segment(source, node)
    if segment is None:
        return []
    issues: list[Issue] = []
    length = len(re.sub(r"\s*\n\s*", " ", segment))
    if length > limit or "\n" in segment:
        issues.append(Issue(node.lineno, node.col_offset + 1, "ST006",
                            f"упростите выражение: длина {length} превышает предел {limit} "
                            if length > limit else "упростите выражение: оно должно быть в одной строке"))

    def bearing(part: ast.AST) -> bool:
        return (not isinstance(part, ast.JoinedStr)
                and (isinstance(part, ast.Call) or any(bearing(child) for child in ast.iter_child_nodes(part))))

    def branches(part: ast.AST, count: int) -> bool:
        if isinstance(part, ast.JoinedStr):
            return False
        children = list(ast.iter_child_nodes(part))
        if sum(bearing(child) for child in children) >= 2:
            count += 1
        return count > 1 or any(branches(child, count) for child in children)

    if branches(node, 0):
        issues.append(Issue(node.lineno, node.col_offset + 1, "ST007",
                            "упростите выражение: несколько уровней разветвления вызовов"))
    return issues


def _container_issues(node: ast.AST) -> list[Issue]:
    if isinstance(node, ast.Dict) and len(node.keys) > 1:
        fields = [key if key is not None else value for key, value in zip(node.keys, node.values)]
        if len({field.lineno for field in fields}) != len(fields) or node.end_lineno == fields[-1].end_lineno:
            return [Issue(node.lineno, node.col_offset + 1, "ST009",
                          "каждое поле словаря должно быть на отдельной строке")]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        children = node.elts
        for index, child in enumerate(children[1:], 1):
            nested = (isinstance(child, (ast.List, ast.Tuple, ast.Set, ast.Dict))
                      or isinstance(children[index - 1], (ast.List, ast.Tuple, ast.Set, ast.Dict))
                      or _has_nested_call(child) or _has_nested_call(children[index - 1]))
            if (nested and child.lineno == children[index - 1].lineno or
                    not nested and child.lineno != children[index - 1].lineno):
                return [Issue(child.lineno, child.col_offset + 1, "ST009",
                              "обычные элементы должны делить строку, вызовы и вложенные контейнеры — начинаться с новой")]
    return []


def issues_for(tree: ast.AST, source: str = "", limit: int = 240) -> list[Issue]:
    issues: list[Issue] = []
    lines = source.splitlines(keepends=True)
    if source and isinstance(tree, ast.Module):
        issues.extend(_indentation(source, 4)[0])
    for root in _roots(tree):
        issues.extend(_issue_list(root))
        if source:
            start = _index(lines, root.lineno, root.col_offset)
            end = _index(lines, root.end_lineno, root.end_col_offset)
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.COMMENT:
                    pos = _index(lines, token.start[0], len(lines[token.start[0] - 1][:token.start[1]].encode("utf-8")))
                    if start <= pos < end:
                        issues.append(Issue(token.start[0], token.start[1] + 1, "ST008",
                                            "комментарий внутри вызова: перенесите его за пределы вызова"))
            def visit(node: ast.AST) -> None:
                if isinstance(node, ast.JoinedStr):
                    return
                if _protected(node):
                    issues.extend(_inline_issues(node, source, limit))
                    return
                issues.extend(_container_issues(node))
                for child in ast.iter_child_nodes(node):
                    visit(child)
            visit(root)
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
            return "\n" not in self.compact(arg) and (
                len(self.compact(arg)) <= self.limit or "\n" not in self.segment(arg))
        if isinstance(arg, ast.Call):
            return self.supported(arg)
        if isinstance(arg, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
            if isinstance(arg, ast.Dict):
                return (all(key is None or self.supported_arg(key) for key in arg.keys)
                        and all(self.supported_arg(value) for value in arg.values))
            return all(self.supported_arg(item) for item in arg.elts)
        return self.simple(arg)

    def supported(self, call: ast.Call) -> bool:
        if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Call):
            if not self.supported(call.func.value):
                return False
        elif not self.simple(call.func):
            return False

        return (all(self.supported_arg(arg) for arg in call.args)
                and all(self.supported_arg(kw.value) for kw in call.keywords))

    def render_arg(self, arg: ast.AST, indent: str) -> str:
        if _protected(arg):
            return self.compact(arg)
        if isinstance(arg, ast.Call):
            return self.render_call(arg, indent, in_call=True)
        if isinstance(arg, ast.keyword):
            prefix = f"{arg.arg}=" if arg.arg is not None else "**"
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

    def render_args(self, call: ast.Call, name: str, indent: str) -> str:
        args = sorted([*call.args, *call.keywords], key=lambda a: (a.lineno, a.col_offset))
        return self.render_values(args, name, indent)

    def render_values(self, args: Sequence[ast.AST], name: str, indent: str,
                      tuple_mode: bool = False, container_mode: bool = False) -> str:
        if not args:
            return name + "()"
        child_indent = indent + self.step
        values = [self.render_arg(arg, child_indent) for arg in args]
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
            return self.render_args(call, self.segment(call.func), indent)

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


def _index(lines: list[str], lineno: int, byte_col: int) -> int:
    prefix = sum(len(line) for line in lines[:lineno - 1])
    # CPython AST columns are UTF-8 byte offsets rather than Unicode indices.
    text = lines[lineno - 1]
    return prefix + len(text.encode("utf-8")[:byte_col].decode("utf-8"))


def _leading_indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _indentation(source: str, size: int) -> tuple[list[Issue], str]:
    """Locate indentation tabs and render block depths without touching literals."""
    lines = source.splitlines(keepends=True)
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
        elif token.type == tokenize.FSTRING_START:
            fstrings.append(token.start[0])
        elif token.type == tokenize.FSTRING_END:
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


def format_source(source: str, indent_size: int = 4, inline_limit: int = 240) -> tuple[str, list[str]]:
    tree = ast.parse(source)
    _, normalized = _indentation(source, indent_size)
    if normalized != source:
        if ast.dump(ast.parse(normalized), include_attributes=False) != ast.dump(tree, include_attributes=False):
            raise ValueError("нормализация отступов меняет структуру AST")
        source = normalized
        tree = ast.parse(source)
    renderer = Renderer(source, indent_size, inline_limit)
    lines = source.splitlines(keepends=True)
    edits: list[tuple[int, int, str]] = []
    warnings: list[str] = []
    for root in _roots(tree):
        findings = issues_for(root, source, inline_limit)
        if not findings:
            continue
        start = _index(lines, root.lineno, root.col_offset)
        end = _index(lines, root.end_lineno, root.end_col_offset)
        blocked = _has_comments(source, start, end, lines)
        if blocked:
            warnings.append(f"{root.lineno}: пропущено: комментарий внутри вызова")

        def collect(call: ast.Call) -> None:
            left = _index(lines, call.lineno, call.col_offset)
            right = _index(lines, call.end_lineno, call.end_col_offset)
            if (any(issue.code in {"ST001", "ST002", "ST003", "ST004", "ST005", "ST009"}
                    or issue.code == "ST006" and "одной строке" in issue.message
                    for issue in issues_for(call, source, inline_limit))
                    and not _has_comments(source, left, right, lines)
                    and renderer.supported(call)):
                indent = _leading_indent(lines[call.lineno - 1])
                try:
                    edits.append((left, right, renderer.render_call(call, indent)))
                    return
                except ValueError:
                    pass
            for child in _visible(call)[1:]:
                if child is not call and not any(
                    left <= edit[0] < right for edit in edits):
                    collect(child)

        if blocked or not renderer.supported(root):
            if not blocked:
                warnings.append(f"{root.lineno}: пропущено: сложное выражение внутри вызова")
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
    if not available:
        raise ValueError("В области проверки Quard нет файлов .py")
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
    if "упростите выражение" in reason:
        return "Упростите выражение: сократите его длину или разделите разветвления вызовов."
    if "комментарий внутри вызова" in reason:
        return "Перенесите комментарий за пределы вызова или расставьте вызовы вручную."
    if "сложное выражение" in reason:
        return "Расставьте вложенные вызовы вручную, сохранив смысл выражения."
    if "табуляция" in reason:
        return "Запустите --fix для замены табуляции в отступе пробелами."
    return "Расставьте вызовы вручную и повторите --fix."


def _report(root: Path, paths: list[Path], fix: bool, indent: int, limit: int) -> dict[str, object]:
    changed: list[str] = []
    remaining: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for path in paths:
        name = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=name)
            if fix:
                formatted, skipped = format_source(source, indent, limit)
                for warning in skipped:
                    position, separator, reason = warning.partition(": ")
                    warnings.append({"path": name, "line": int(position) if separator and
                                     position.isdecimal() else None, "reason": reason if separator
                                     else warning, "suggestion": _advice(warning)})
                if formatted != source:
                    atomic_write(path, formatted, root)
                    changed.append(name)
                tree = ast.parse(formatted, filename=name)
            remaining.extend({"path": name, "line": issue.line, "column": issue.col,
                              "rule": issue.code, "message": issue.message,
                               "suggestion": _advice(issue.message)} for issue in issues_for(tree, formatted if fix else source, limit))
        except (OSError, UnicodeError, SyntaxError, ValueError, TypeError, IndexError) as exc:
            errors.append({"path": name, "message": str(exc),
                           "suggestion": "Исправьте файл или доступ к нему и повторите --fix."})
    code = 2 if errors else (1 if remaining or warnings else 0)
    return {"schema_version": "quard-stairfmt-fix/v1", "status": {0: "passed", 1: "violations",
            2: "error"}[code], "exit_code": code, "checked_files": len(paths),
            "changed_files": changed, "remaining": remaining, "warnings": warnings, "errors": errors}


def _display(report: dict[str, object]) -> None:
    print(f"STAIRFMT {str(report['status']).upper()}: изменено {len(report['changed_files'])}; "
          f"осталось {len(report['remaining'])} нарушений, {len(report['errors'])} ошибок")
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
    parser.add_argument("--indent", type=int, default=4, help="размер отступа (по умолчанию 4)")
    parser.add_argument("paths", nargs="*", help="файлы .py или каталоги внутри области Quard")
    args = parser.parse_args(argv)
    if args.indent < 1:
        parser.error("--indent должен быть положительным")
    try:
        root = project_dir(args.project)
        with repo_lock(root):
            try:
                paths = _paths(root, args.paths)
                limit = validate_stairfmt(read_settings(root).get("stairfmt", {}))
                report = _report(root, paths, args.fix, args.indent, limit)
            except (QuardError, OSError, UnicodeError, ValueError) as exc:
                report = {"schema_version": "quard-stairfmt-fix/v1", "status": "error",
                          "exit_code": 2, "changed_files": [], "remaining": [], "warnings": [],
                          "errors": [{"path": str(args.project), "message": str(exc),
                                      "suggestion": "Проверьте настройки проекта и повторите команду."}]}
            if args.fix:
                run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
                archive = root / ".quard/reports/stairfmt" / f"{run_id}.json"
                report["report_path"] = archive.relative_to(root).as_posix()
                write_json(archive, report, root)
                write_json(root / ".quard/reports/stairfmt/latest.json", report, root)
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
