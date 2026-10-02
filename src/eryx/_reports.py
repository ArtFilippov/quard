"""Normalize real tool diagnostics; retain raw output on every path."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def clean(text: str) -> str:
    return ANSI.sub("", text)


def relative_path(path: str | None, root: Path) -> str | None:
    if not path:
        return None
    target = Path(path)
    if target.is_absolute():
        try:
            return target.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return target.as_posix()
    return target.as_posix().removeprefix("./")


def diagnostic(tool: str, rule: str, message: str, *, path: str | None = None,
               line: int | None = None, column: int | None = None,
               severity: str = "error", symbol: str | None = None,
               documentation_url: str | None = None, **extra: Any) -> dict[str, Any]:
    data = {"tool": tool, "rule": rule, "severity": severity, "path": path,
            "line": line, "column": column, "message": clean(message).strip(),
            "symbol": symbol, "documentation_url": documentation_url, **extra}
    key = json.dumps([tool, rule, path, line, column, data["message"]], ensure_ascii=False)
    data["fingerprint"] = hashlib.sha256(key.encode()).hexdigest()[:24]
    return data


def find_json(text: str, required_key: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and required_key in obj:
            return obj
    raise ValueError(f"No JSON object containing {required_key!r}")


def parse_pylint(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    obj = find_json(text, "messages")
    if not isinstance(obj["messages"], list):
        raise ValueError("Pylint JSON2 messages must be a list")
    items = []
    error = None
    for entry in obj["messages"]:
        rule = entry.get("messageId") or entry.get("message-id")
        if not rule or not isinstance(entry.get("message"), str):
            raise ValueError("Malformed Pylint message")
        if rule.startswith("F") or rule in {"E0013", "E0015"}:
            error = "PYLINT_ANALYSIS_ERROR"
        symbol = entry.get("symbol")
        raw_type = entry.get("type", "warning")
        items.append(diagnostic(
            "pylint", rule, entry["message"], path=relative_path(entry.get("path"), root),
            line=entry.get("line") if isinstance(entry.get("line"), int) and entry["line"] > 0 else None, column=(entry["column"] + 1) if isinstance(entry.get("column"), int) else None,
            severity={"convention": "warning", "refactor": "warning", "fatal": "error"}.get(raw_type, raw_type),
            symbol=symbol, native_severity=raw_type,
            documentation_url=("https://pylint.readthedocs.io/en/stable/user_guide/messages/messages_overview.html"
                               if not rule[1:3] in {"90", "91", "92", "93", "95", "96"}
                               else "https://github.com/noah-goodrich/pylint-clean-architecture/blob/main/RULES.md"),
        ))
    return items, error


def parse_pyright(text: str, root: Path) -> list[dict[str, Any]]:
    obj = find_json(text, "generalDiagnostics")
    if not isinstance(obj["generalDiagnostics"], list):
        raise ValueError("Pyright generalDiagnostics must be a list")
    items = []
    for entry in obj["generalDiagnostics"]:
        start = entry.get("range", {}).get("start", {})
        rule = entry.get("rule", "syntax")
        items.append(diagnostic(
            "pyright", rule, entry["message"], path=relative_path(entry.get("file"), root),
            line=(start["line"] + 1) if "line" in start else None,
            column=(start["character"] + 1) if "character" in start else None,
            severity=entry.get("severity", "error"),
            documentation_url="https://github.com/microsoft/pyright/blob/main/docs/configuration.md",
        ))
    return items


def parse_pydoclint(text: str, root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    regex = re.compile(r"^(?P<path>.+?):(?P<line>\d+):\s*(?P<rule>DOC\d+):\s*(?P<message>.*)$")
    for raw_line in clean(text).splitlines():
        match = regex.match(raw_line.strip())
        if match:
            data = match.groupdict()
            items.append(diagnostic("pydoclint", data["rule"], data["message"],
                                    path=relative_path(data["path"], root), line=int(data["line"]),
                                    documentation_url="https://jsh9.github.io/pydoclint/violation_codes.html"))
        elif items and raw_line.startswith((" ", "\t")) and raw_line.strip():
            items[-1]["message"] += "\n" + raw_line.strip()
    return items


def tach_rule(message: str, external: bool = False) -> str:
    text = message.lower()
    if "interface" in text or "not public" in text:
        return "public-interface"
    if "circular" in text or "cycle" in text:
        return "cyclic-dependency"
    if "unused" in text and "ignore" in text:
        return "unused-ignore-directive"
    if "ignore" in text and "reason" in text:
        return "ignore-requires-reason"
    if "unused" in text:
        return "unused-external-dependency" if external else "unused-dependency"
    return "external-dependency" if external else "dependency"


def parse_tach(text: str, root: Path, external: bool = False) -> list[dict[str, Any]]:
    items = []
    # The CLI has no promised JSON diagnostic schema. These labels are adapter
    # categories, not invented upstream numeric rule IDs.
    positioned = re.compile(r"^\s*(?:[❌⚠✓✗✘]\s*)?(?P<path>.+?)\[L(?P<line>\d+)\]:\s*(?P<message>.*)$")
    for raw in clean(text).splitlines():
        match = positioned.match(raw)
        if match:
            msg = match["message"]
            items.append(diagnostic("tach", tach_rule(msg, external), msg,
                                    path=relative_path(match["path"], root), line=int(match["line"]),
                                    rule_is_adapter_category=True,
                                    documentation_url="https://docs.gauge.sh/usage/commands/"))
        elif raw.lstrip().startswith(("❌", "✗", "✘", "⚠")):
            msg = raw.lstrip().lstrip("❌✗✘⚠ ").strip()
            if msg:
                items.append(diagnostic("tach", tach_rule(msg, external), msg,
                                        severity="warning" if "⚠" in raw else "error",
                                        rule_is_adapter_category=True,
                                        documentation_url="https://docs.gauge.sh/usage/commands/"))
    return items


def parse_lcom4(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    obj = find_json(text, "eryx_lcom4_adapter")
    if obj["eryx_lcom4_adapter"] != 1 or not isinstance(obj.get("diagnostics"), list):
        raise ValueError("Unsupported LCOM4 adapter report schema")
    error = obj.get("execution_error")
    if error is not None and error not in {"LCOM4_PARSE_ERROR", "LCOM4_EXECUTION_ERROR"}:
        raise ValueError("Unknown LCOM4 execution error")
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in {"LCOM4", "LCOM4_PARSE_ERROR", "LCOM4_EXECUTION_ERROR"}:
            raise ValueError("Malformed LCOM4 diagnostic")
        rule = entry["rule"]
        if not isinstance(entry.get("message"), str):
            raise ValueError("Missing LCOM4 message")
        if rule == "LCOM4" and (type(entry.get("score")) is not int or entry["score"] == 1
                                 or entry["score"] < 0 or not isinstance(entry.get("groups"), list)
                                 or not isinstance(entry.get("path"), str)
                                 or type(entry.get("line")) is not int or type(entry.get("column")) is not int):
            raise ValueError("Malformed LCOM4 score or class location")
        items.append(diagnostic("lcom4", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                score=entry.get("score"), groups=entry.get("groups"),
                                is_infrastructure_error=rule != "LCOM4",
                                documentation_url="https://www.aivosto.com/project/help/pm-oo-cohesion.html#LCOM4"))
    if bool(error) != any(item["is_infrastructure_error"] for item in items):
        raise ValueError("LCOM4 error/diagnostic mismatch")
    return items, error


def parse_method_decorators(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    obj = find_json(text, "eryx_method_decorators_adapter")
    if (obj["eryx_method_decorators_adapter"] != 1 or not isinstance(obj.get("diagnostics"), list)
            or "execution_error" not in obj):
        raise ValueError("Unsupported method decorator adapter report schema")
    error = obj.get("execution_error")
    if error is not None and error not in {"METHOD_DECORATORS_PARSE_ERROR", "METHOD_DECORATORS_EXECUTION_ERROR"}:
        raise ValueError("Unknown method decorator execution error")
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in {
                "NON_INSTANCE_METHOD", "MISSING_INSTANCE_RECEIVER",
                "METHOD_DECORATORS_PARSE_ERROR", "METHOD_DECORATORS_EXECUTION_ERROR"}:
            raise ValueError("Malformed method decorator diagnostic")
        rule = entry["rule"]
        if not isinstance(entry.get("message"), str):
            raise ValueError("Missing method decorator message")
        if (entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)):
            raise ValueError("Malformed method decorator diagnostic position")
        if rule in {"NON_INSTANCE_METHOD", "MISSING_INSTANCE_RECEIVER"} and (
                not isinstance(entry.get("path"), str) or not isinstance(entry.get("symbol"), str)
                or type(entry.get("line")) is not int or entry["line"] < 1
                or type(entry.get("column")) is not int or entry["column"] < 1):
            raise ValueError("Malformed method decorator location")
        items.append(diagnostic("method-decorators", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule not in {
                                    "NON_INSTANCE_METHOD", "MISSING_INSTANCE_RECEIVER"}))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Method decorator error/diagnostic mismatch")
    return items, error


def parse_instance_fields(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    obj = find_json(text, "eryx_instance_fields_adapter")
    if (obj["eryx_instance_fields_adapter"] != 1 or not isinstance(obj.get("diagnostics"), list)
            or "execution_error" not in obj):
        raise ValueError("Unsupported instance fields adapter report schema")
    error = obj["execution_error"]
    if error not in {None, "INSTANCE_FIELDS_PARSE_ERROR", "INSTANCE_FIELDS_EXECUTION_ERROR"}:
        raise ValueError("Unknown instance fields execution error")
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in {
                "EXTERNAL_INSTANCE_FIELD", "EXTERNAL_CLASS_FIELD", "NO_CLASS_STATE",
                "MISSING_FIELD_INITIALIZATION",
                "INSTANCE_FIELDS_PARSE_ERROR", "INSTANCE_FIELDS_EXECUTION_ERROR"}:
            raise ValueError("Malformed instance field diagnostic")
        rule = entry["rule"]
        if (not isinstance(entry.get("message"), str)
                or entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)):
            raise ValueError("Malformed instance field diagnostic location")
        if rule in {"EXTERNAL_INSTANCE_FIELD", "EXTERNAL_CLASS_FIELD", "NO_CLASS_STATE",
                    "MISSING_FIELD_INITIALIZATION"} and (
                not isinstance(entry.get("path"), str) or not isinstance(entry.get("symbol"), str)
                or type(entry.get("line")) is not int or type(entry.get("column")) is not int):
            raise ValueError("Missing instance field location")
        items.append(diagnostic("instance-fields", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule not in {
                                     "EXTERNAL_INSTANCE_FIELD", "EXTERNAL_CLASS_FIELD", "NO_CLASS_STATE",
                                     "MISSING_FIELD_INITIALIZATION"}))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Instance field error/diagnostic mismatch")
    return items, error


def parse_class_contracts(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    obj = find_json(text, "eryx_class_contracts_adapter")
    if (obj["eryx_class_contracts_adapter"] != 1 or not isinstance(obj.get("diagnostics"), list)
            or "execution_error" not in obj):
        raise ValueError("Unsupported class contract adapter report schema")
    error = obj["execution_error"]
    if error not in {None, "CLASS_CONTRACTS_PARSE_ERROR", "CLASS_CONTRACTS_EXECUTION_ERROR"}:
        raise ValueError("Unknown class contract execution error")
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in {
                "MISSING_CLASS_CONTRACT", "UNCOVERED_CLASS_MEMBER", "CUSTOMER_METHOD_OVERRIDE",
                "CLASS_CONTRACTS_PARSE_ERROR", "CLASS_CONTRACTS_EXECUTION_ERROR"}:
            raise ValueError("Malformed class contract diagnostic")
        rule = entry["rule"]
        if (not isinstance(entry.get("message"), str)
                or entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)):
            raise ValueError("Malformed class contract location")
        if rule in {"MISSING_CLASS_CONTRACT", "UNCOVERED_CLASS_MEMBER", "CUSTOMER_METHOD_OVERRIDE"} and (
                not isinstance(entry.get("path"), str) or not isinstance(entry.get("symbol"), str)
                or type(entry.get("line")) is not int or type(entry.get("column")) is not int):
            raise ValueError("Missing class contract location")
        items.append(diagnostic("class-contracts", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule not in {
                                    "MISSING_CLASS_CONTRACT", "UNCOVERED_CLASS_MEMBER",
                                    "CUSTOMER_METHOD_OVERRIDE"}))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Class contract error/diagnostic mismatch")
    return items, error


def normalize(tool: str, result: dict[str, Any], root: Path, json_file: Path | None = None) -> dict[str, Any]:
    text = result.get("stdout", "") + "\n" + result.get("stderr", "")
    error = result.get("error")
    items: list[dict[str, Any]] = []
    try:
        if tool.startswith("pylint"):
            payload = json_file.read_text(encoding="utf-8") if json_file and json_file.exists() else text
            items, parse_error = parse_pylint(payload, root)
            error = error or parse_error
            if result.get("exit_code", 0) is not None and result["exit_code"] & 32:
                error = error or "PYLINT_USAGE_ERROR"
        elif tool == "pyright":
            items = parse_pyright(text, root)
            if result.get("exit_code") not in {0, 1}:
                error = error or "PYRIGHT_EXECUTION_ERROR"
            config_text = result.get("stderr", "") + "\n" + "\n".join(line for line in result.get("stdout", "").splitlines() if not line.lstrip().startswith(('"', "{", "[")))
            if re.search(r"unrecognized setting|unknown configuration|(?m:^\s*config[^\n]*(?:error|invalid|could not))", config_text, re.I):
                error = error or "PYRIGHT_CONFIG_ERROR"
        elif tool == "pydoclint":
            items = parse_pydoclint(text, root)
            if result.get("exit_code") not in {0, 1}:
                error = error or "PYDOCLINT_EXECUTION_ERROR"
        elif tool == "lcom4":
            items, parse_error = parse_lcom4(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "LCOM4_EXECUTION_ERROR"
        elif tool == "method-decorators":
            items, parse_error = parse_method_decorators(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "METHOD_DECORATORS_EXECUTION_ERROR"
        elif tool == "instance-fields":
            items, parse_error = parse_instance_fields(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "INSTANCE_FIELDS_EXECUTION_ERROR"
        elif tool == "class-contracts":
            items, parse_error = parse_class_contracts(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "CLASS_CONTRACTS_EXECUTION_ERROR"
        elif tool.startswith("tach"):
            if '"eryx_tach_adapter"' in text:
                obj = find_json(text, "eryx_tach_adapter")
                if obj["eryx_tach_adapter"] != 1 or not isinstance(obj.get("diagnostics"), list):
                    raise ValueError("Unsupported Tach adapter report schema")
                for entry in obj["diagnostics"]:
                    items.append(diagnostic("tach", entry["rule"], entry["message"],
                        path=relative_path(entry.get("path"), root), line=entry.get("line") or None,
                        severity=entry.get("severity", "error"),
                        is_infrastructure_error=bool(entry.get("is_infrastructure_error")),
                        rule_is_adapter_category=bool(entry.get("rule_is_adapter_category")),
                        native_details=entry.get("native_details"),
                        documentation_url="https://docs.gauge.sh/usage/configuration/"))
                error = error or obj.get("execution_error")
            else:
                items = parse_tach(text, root, external=tool.endswith("external"))
            if result.get("exit_code") not in {0, 1}:
                error = error or "TACH_EXECUTION_ERROR"
            if re.search(r"error parsing|invalid configuration|validationerror|tomldecodeerror", text, re.I):
                error = error or "TACH_CONFIG_ERROR"
    except (ValueError, KeyError, TypeError, OSError) as exc:
        error = error or "UNREADABLE_TOOL_OUTPUT"
        result["parse_error"] = str(exc)
    if "Traceback (most recent call last)" in text:
        error = error or "TOOL_CRASH"
    if result.get("exit_code") not in (0, None) and not items and not error:
        error = "UNPARSED_TOOL_FAILURE"
    if error:
        items.append(diagnostic(tool, error, result.get("parse_error") or
                                (clean(result.get("stderr", "") or result.get("stdout", ""))[-3000:]
                                 or "Tool failed; inspect its raw output."), is_infrastructure_error=True))
    return {"status": "error" if error else ("violations" if items else "passed"),
            "diagnostics": items, "error": error}


def sorted_diagnostics(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Only remove byte-identical observations of the same native diagnostic.
    seen = set()
    unique = []
    for item in items:
        encoded = json.dumps([item["tool"], item["rule"], item["path"], item["line"], item["column"], item["message"]], ensure_ascii=False)
        item["fingerprint"] = hashlib.sha256(encoded.encode()).hexdigest()[:24]
        key = (item["tool"], item["rule"], item["path"], item["line"], item["column"], item["message"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return sorted(unique, key=lambda d: (d["path"] or "", d["line"] or 0,
                                        d["column"] or 0, d["tool"], d["rule"], d["message"]))
