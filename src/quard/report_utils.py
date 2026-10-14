"""Normalize real tool diagnostics; retain raw output on every path."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, cast

from quard.restricted_report_utils import parse_encapsulated_only

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
JSCPD_SIMILAR = "similar"
JSCPD_GAP_METHOD = "gap"
JSCPD_KINDS = {"exact": "Exact", "renamed": "Renamed", JSCPD_SIMILAR: "Similar"}


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
    obj = find_json(text, "quard_lcom4_adapter")
    if obj["quard_lcom4_adapter"] != 1 or not isinstance(obj.get("diagnostics"), list):
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
    obj = find_json(text, "quard_method_decorators_adapter")
    if (obj["quard_method_decorators_adapter"] != 1 or not isinstance(obj.get("diagnostics"), list)
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
    obj = find_json(text, "quard_instance_fields_adapter")
    if (obj["quard_instance_fields_adapter"] != 1 or not isinstance(obj.get("diagnostics"), list)
            or "execution_error" not in obj):
        raise ValueError("Unsupported instance fields adapter report schema")
    error = obj["execution_error"]
    if error not in {None, "INSTANCE_FIELDS_PARSE_ERROR", "INSTANCE_FIELDS_EXECUTION_ERROR"}:
        raise ValueError("Unknown instance fields execution error")
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in {
                "EXTERNAL_INSTANCE_FIELD", "EXTERNAL_CLASS_FIELD", "NO_CLASS_STATE",
                "MISSING_FIELD_INITIALIZATION", "UNKNOWN_FIELD_TYPE",
                "NON_PRIMITIVE_FIELD_REBINDING", "DELETED_INSTANCE_FIELD",
                "INSTANCE_FIELDS_PARSE_ERROR", "INSTANCE_FIELDS_EXECUTION_ERROR"}:
            raise ValueError("Malformed instance field diagnostic")
        rule = entry["rule"]
        if (not isinstance(entry.get("message"), str)
                or entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)):
            raise ValueError("Malformed instance field diagnostic location")
        if rule in {"EXTERNAL_INSTANCE_FIELD", "EXTERNAL_CLASS_FIELD", "NO_CLASS_STATE",
                    "MISSING_FIELD_INITIALIZATION", "UNKNOWN_FIELD_TYPE",
                     "NON_PRIMITIVE_FIELD_REBINDING", "DELETED_INSTANCE_FIELD"} and (
                not isinstance(entry.get("path"), str) or not isinstance(entry.get("symbol"), str)
                or type(entry.get("line")) is not int or type(entry.get("column")) is not int):
            raise ValueError("Missing instance field location")
        items.append(diagnostic("instance-fields", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule not in {
                                     "EXTERNAL_INSTANCE_FIELD", "EXTERNAL_CLASS_FIELD", "NO_CLASS_STATE",
                                     "MISSING_FIELD_INITIALIZATION", "UNKNOWN_FIELD_TYPE",
                                      "NON_PRIMITIVE_FIELD_REBINDING", "DELETED_INSTANCE_FIELD"}))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Instance field error/diagnostic mismatch")
    return items, error


def parse_class_contracts(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    obj = find_json(text, "quard_class_contracts_adapter")
    if (obj["quard_class_contracts_adapter"] != 1 or not isinstance(obj.get("diagnostics"), list)
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


def parse_contract_folders(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    obj = find_json(text, "quard_contract_folders_adapter")
    if (type(obj["quard_contract_folders_adapter"]) is not int
            or obj["quard_contract_folders_adapter"] != 1
            or not isinstance(obj.get("diagnostics"), list)
            or "execution_error" not in obj):
        raise ValueError("Unsupported contract folder adapter report schema")
    error = obj["execution_error"]
    if error not in {None, "CONTRACT_FOLDERS_PARSE_ERROR", "CONTRACT_FOLDERS_EXECUTION_ERROR"}:
        raise ValueError("Unknown contract folder execution error")
    violations = {"CONTRACT_FOLDER", "IMPLEMENTATION_FOLDER", "COMPETING_LOCAL_BASES",
                  "MIXED_MODULE_ROLES", "CONFLICTING_MODULE_ROLE", "CROSS_GROUP_CONTRACT",
                  "UNRESOLVED_GROUP_OWNER", "UNRESOLVED_EXTERNAL_CONTRACT",
                  "UNRESOLVED_VALUE_FACTORY", "CONFLICTING_VALUE_FACTORY",
                  "CONCRETE_VALUE_DEPENDENCY", "CONCRETE_FACADE_EXPORT",
                  "CONCRETE_IMPLEMENTATION_IMPORT", "CONCRETE_IMPLEMENTATION_BASE",
                  "CONCRETE_IMPLEMENTATION_REFERENCE",
                  "UNRESOLVED_FACADE_EXPORT", "CONFLICTING_FACADE_EXPORT",
                   "CONFLICTING_CONTRACT_ALIAS", "UNRESOLVED_CONTRACT_ALIAS",
                  "UNRESOLVED_FOLDER_OWNER", "ENVELOPE_FOLDER", "ENVELOPE_CLASS",
                    "ENVELOPE_FILE", "ENVELOPE_INHERITANCE", "ENVELOPE_DELEGATION",
                    "SOURCE_ROOT_LAYOUT",
                   "MULTIPLE_MAIN_DIRECTORIES", "BOOTSTRAP_DEPENDENCY"}
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in violations | {
                "CONTRACT_FOLDERS_PARSE_ERROR", "CONTRACT_FOLDERS_EXECUTION_ERROR"}:
            raise ValueError("Malformed contract folder diagnostic")
        rule = entry["rule"]
        if (not isinstance(entry.get("message"), str)
                or entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)):
            raise ValueError("Malformed contract folder location")
        if rule in violations and (not isinstance(entry.get("path"), str)
                                   or not isinstance(entry.get("symbol"), str)
                                   or type(entry.get("line")) is not int
                                   or type(entry.get("column")) is not int):
            raise ValueError("Missing contract folder location")
        items.append(diagnostic("contract-folders", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule not in violations))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Contract folder error/diagnostic mismatch")
    return items, error


def parse_dependency_types(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    obj = find_json(text, "quard_dependency_types_adapter")
    if (type(obj["quard_dependency_types_adapter"]) is not int
            or obj["quard_dependency_types_adapter"] != 1
            or not isinstance(obj.get("diagnostics"), list)
            or "execution_error" not in obj):
        raise ValueError("Unsupported dependency types adapter report schema")
    error = obj["execution_error"]
    failures = {"DEPENDENCY_TYPES_PARSE_ERROR", "DEPENDENCY_TYPES_EXECUTION_ERROR"}
    violations = {"CONCRETE_DEPENDENCY_TYPE", "UNRESOLVED_DEPENDENCY_TYPE"}
    if error not in failures | {None}:
        raise ValueError("Unknown dependency types execution error")
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in violations | failures:
            raise ValueError("Malformed dependency types diagnostic")
        rule = entry["rule"]
        if (not isinstance(entry.get("message"), str)
                or entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)
                or rule in violations and (not isinstance(entry.get("path"), str)
                                           or not isinstance(entry.get("symbol"), str)
                                           or type(entry.get("line")) is not int
                                           or type(entry.get("column")) is not int)):
            raise ValueError("Malformed dependency types location")
        items.append(diagnostic("dependency-types", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule in failures))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Dependency types error/diagnostic mismatch")
    return items, error


def parse_test_placement(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Validate positioned test mapping diagnostics and independent worker failures."""
    obj = find_json(text, "quard_test_placement_adapter")
    if (type(obj["quard_test_placement_adapter"]) is not int
            or obj["quard_test_placement_adapter"] != 1
            or not isinstance(obj.get("diagnostics"), list)
            or "execution_error" not in obj):
        raise ValueError("Unsupported test placement adapter report schema")
    error = obj["execution_error"]
    if error not in {None, "TEST_PLACEMENT_PARSE_ERROR", "TEST_PLACEMENT_EXECUTION_ERROR"}:
        raise ValueError("Unknown test placement execution error")
    violations = {"TOP_LEVEL_TEST_FUNCTION", "TEST_CLASS_NAME", "TEST_MODULE_NAME",
                  "MISSING_TEST_TARGET", "TEST_CLASS_PLACEMENT", "AMBIGUOUS_TEST_TARGET",
                  "TEST_TARGET_CONTRACT"}
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in violations | {
                "TEST_PLACEMENT_PARSE_ERROR", "TEST_PLACEMENT_EXECUTION_ERROR"}:
            raise ValueError("Malformed test placement diagnostic")
        rule = entry["rule"]
        if (not isinstance(entry.get("message"), str)
                or entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)):
            raise ValueError("Malformed test placement location")
        if rule in violations and (not isinstance(entry.get("path"), str)
                                   or not isinstance(entry.get("symbol"), str)
                                   or type(entry.get("line")) is not int
                                   or type(entry.get("column")) is not int):
            raise ValueError("Missing test placement location")
        items.append(diagnostic("test-placement", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule not in violations))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Test placement error/diagnostic mismatch")
    return items, error


def parse_outer_classes(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    obj = find_json(text, "quard_outer_classes_adapter")
    if (type(obj["quard_outer_classes_adapter"]) is not int
            or obj["quard_outer_classes_adapter"] != 1
            or not isinstance(obj.get("diagnostics"), list)
            or "execution_error" not in obj):
        raise ValueError("Unsupported outer-class adapter report schema")
    error = obj["execution_error"]
    if error not in {None, "OUTER_CLASSES_PARSE_ERROR", "OUTER_CLASSES_EXECUTION_ERROR"}:
        raise ValueError("Unknown outer-class execution error")
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in {
                "MULTIPLE_OUTER_CLASSES", "OUTER_CLASSES_PARSE_ERROR", "OUTER_CLASSES_EXECUTION_ERROR"}:
            raise ValueError("Malformed outer-class diagnostic")
        rule = entry["rule"]
        if (not isinstance(entry.get("message"), str)
                or entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)):
            raise ValueError("Malformed outer-class diagnostic location")
        if rule == "MULTIPLE_OUTER_CLASSES" and (
                not isinstance(entry.get("path"), str) or not isinstance(entry.get("symbol"), str)
                or type(entry.get("line")) is not int or type(entry.get("column")) is not int
                or entry["line"] < 1 or entry["column"] < 1):
            raise ValueError("Missing outer-class location")
        items.append(diagnostic("outer-classes", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule != "MULTIPLE_OUTER_CLASSES"))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Outer-class error/diagnostic mismatch")
    return items, error


def parse_free_function_placement(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Validate free-function placement worker diagnostics and error agreement."""
    obj = find_json(text, "quard_free_function_placement_adapter")
    if (type(obj["quard_free_function_placement_adapter"]) is not int
            or obj["quard_free_function_placement_adapter"] != 1
            or not isinstance(obj.get("diagnostics"), list)
            or "execution_error" not in obj):
        raise ValueError("Unsupported free-function placement adapter report schema")
    error = obj["execution_error"]
    if error not in {None, "FREE_FUNCTION_PLACEMENT_PARSE_ERROR",
                     "FREE_FUNCTION_PLACEMENT_EXECUTION_ERROR"}:
        raise ValueError("Unknown free-function placement execution error")
    violations = {"FREE_FUNCTION_PLACEMENT", "UTILITIES_MODULE_CLASS"}
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in violations | {
                "FREE_FUNCTION_PLACEMENT_PARSE_ERROR", "FREE_FUNCTION_PLACEMENT_EXECUTION_ERROR"}:
            raise ValueError("Malformed free-function placement diagnostic")
        rule = entry["rule"]
        if (not isinstance(entry.get("message"), str)
                or entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)):
            raise ValueError("Malformed free-function placement location")
        if rule in violations and (not isinstance(entry.get("path"), str)
                                   or not isinstance(entry.get("symbol"), str)
                                   or type(entry.get("line")) is not int or entry["line"] < 1
                                   or type(entry.get("column")) is not int or entry["column"] < 1):
            raise ValueError("Missing free-function placement location")
        items.append(diagnostic("free-function-placement", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule not in violations))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Free-function placement error/diagnostic mismatch")
    return items, error


def parse_leading_underscores(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Validate naming findings and worker error agreement before normalization."""
    obj = find_json(text, "quard_leading_underscores_adapter")
    if (type(obj["quard_leading_underscores_adapter"]) is not int
            or obj["quard_leading_underscores_adapter"] != 1
            or not isinstance(obj.get("diagnostics"), list) or "execution_error" not in obj):
        raise ValueError("Unsupported leading-underscores adapter report schema")
    errors = {"LEADING_UNDERSCORES_PARSE_ERROR", "LEADING_UNDERSCORES_EXECUTION_ERROR"}
    violations = {"LEADING_UNDERSCORE_NAME", "LEADING_UNDERSCORE_PATH"}
    error = obj["execution_error"]
    if error is not None and error not in errors:
        raise ValueError("Unknown leading-underscores execution error")
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in errors | violations:
            raise ValueError("Malformed leading-underscores diagnostic")
        rule = entry["rule"]
        if (not isinstance(entry.get("message"), str)
                or entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)):
            raise ValueError("Malformed leading-underscores diagnostic location")
        if rule in violations and (not isinstance(entry.get("path"), str)
                                   or not isinstance(entry.get("symbol"), str)
                                   or rule == "LEADING_UNDERSCORE_NAME" and (
                                       type(entry.get("line")) is not int
                                       or type(entry.get("column")) is not int)):
            raise ValueError("Missing leading-underscores location")
        items.append(diagnostic("leading-underscores", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule in errors))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Leading-underscores error/diagnostic mismatch")
    return items, error


def parse_constructor_purity(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    obj = find_json(text, "quard_constructor_purity_adapter")
    if (type(obj["quard_constructor_purity_adapter"]) is not int
            or obj["quard_constructor_purity_adapter"] != 1
            or not isinstance(obj.get("diagnostics"), list)
            or "execution_error" not in obj):
        raise ValueError("Unsupported constructor purity adapter report schema")
    error = obj["execution_error"]
    if error not in {None, "CONSTRUCTOR_PURITY_PARSE_ERROR", "CONSTRUCTOR_PURITY_EXECUTION_ERROR"}:
        raise ValueError("Unknown constructor purity execution error")
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in {
                "CONSTRUCTOR_PURITY", "CONSTRUCTOR_PURITY_PARSE_ERROR",
                "CONSTRUCTOR_PURITY_EXECUTION_ERROR"}:
            raise ValueError("Malformed constructor purity diagnostic")
        rule = entry["rule"]
        if (not isinstance(entry.get("message"), str)
                or entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)):
            raise ValueError("Malformed constructor purity diagnostic location")
        if rule == "CONSTRUCTOR_PURITY" and (
                not isinstance(entry.get("path"), str) or not isinstance(entry.get("symbol"), str)
                or type(entry.get("line")) is not int or type(entry.get("column")) is not int
                or entry["line"] < 1 or entry["column"] < 1):
            raise ValueError("Missing constructor purity location")
        items.append(diagnostic("constructor-purity", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule != "CONSTRUCTOR_PURITY"))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Constructor purity error/diagnostic mismatch")
    return items, error


def parse_lambda_injection(text: str, root: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Validate worker evidence before classifying syntax or infrastructure failures."""
    obj = find_json(text, "quard_lambda_injection_adapter")
    if (type(obj["quard_lambda_injection_adapter"]) is not int
            or obj["quard_lambda_injection_adapter"] != 1
            or not isinstance(obj.get("diagnostics"), list)
            or "execution_error" not in obj):
        raise ValueError("Unsupported lambda injection adapter report schema")
    error = obj["execution_error"]
    errors = {"LAMBDA_INJECTION_PARSE_ERROR", "LAMBDA_INJECTION_EXECUTION_ERROR"}
    if error is not None and error not in errors:
        raise ValueError("Unknown lambda injection execution error")
    items = []
    for entry in obj["diagnostics"]:
        if not isinstance(entry, dict) or entry.get("rule") not in {"LAMBDA_INJECTION", *errors}:
            raise ValueError("Malformed lambda injection diagnostic")
        rule = entry["rule"]
        if (not isinstance(entry.get("message"), str)
                or entry.get("path") is not None and not isinstance(entry["path"], str)
                or entry.get("line") is not None and (type(entry["line"]) is not int or entry["line"] < 1)
                or entry.get("column") is not None and (type(entry["column"]) is not int or entry["column"] < 1)):
            raise ValueError("Malformed lambda injection diagnostic location")
        if rule == "LAMBDA_INJECTION" and (
                not isinstance(entry.get("path"), str) or not isinstance(entry.get("symbol"), str)
                or type(entry.get("line")) is not int or type(entry.get("column")) is not int):
            raise ValueError("Missing lambda injection location")
        items.append(diagnostic("lambda-injection", rule, entry["message"],
                                path=relative_path(entry.get("path"), root), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule in errors))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Lambda injection error/diagnostic mismatch")
    return items, error


def _jscpd_integer(value: object, *, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _jscpd_fragment(side: object, root: Path, allowed: set[str]) -> tuple[str, int, int]:
    if not isinstance(side, dict):
        raise ValueError("Missing jscpd fragment")
    data = cast("dict[str, object]", side)
    name = data.get("name")
    loc = data.get("startLoc")
    if not isinstance(name, str) or not isinstance(loc, dict):
        raise ValueError("Invalid jscpd fragment location")
    position = cast("dict[str, object]", loc)
    line = position.get("line")
    column = position.get("column")
    path = relative_path(name, root)
    if path not in allowed or not _jscpd_integer(line) or not _jscpd_integer(column, minimum=0):
        raise ValueError("Invalid or out-of-scope jscpd fragment location")
    return path, cast("int", line), cast("int", column) + 1


def _jscpd_duplicates(payload: str, expected_count: int) -> list[object]:
    raw: object = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("Missing jscpd report")
    obj = cast("dict[str, object]", raw)
    duplicates = obj.get("duplicates")
    stats = obj.get("statistics")
    if not isinstance(duplicates, list) or not isinstance(stats, dict):
        raise ValueError("Missing jscpd duplicates or statistics")
    duplicates = cast("list[object]", duplicates)
    total = cast("dict[str, object]", stats).get("total")
    if not isinstance(total, dict):
        raise ValueError("Missing jscpd totals")
    counts = cast("dict[str, object]", total)
    sources = counts.get("sources")
    clones = counts.get("clones")
    if not _jscpd_integer(sources) or sources != expected_count:
        raise ValueError("Empty or incomplete jscpd scan")
    if not _jscpd_integer(clones, minimum=0) or clones != len(duplicates):
        raise ValueError("Incomplete jscpd clone report")
    return duplicates


def _jscpd_similarity(clone: dict[str, object]) -> str:
    similarity = clone.get("similarity")
    if not isinstance(similarity, (int, float)) or isinstance(similarity, bool):
        raise ValueError("Malformed jscpd similarity or method")
    if not math.isfinite(similarity) or not 0 < similarity <= 1:
        raise ValueError("Malformed jscpd similarity or method")
    if clone.get("method") != JSCPD_GAP_METHOD:
        raise ValueError("Malformed jscpd similarity or method")
    return f", similarity {similarity:g}, method {JSCPD_GAP_METHOD}"


def _jscpd_clone(entry: object, root: Path, allowed: set[str]) -> list[dict[str, Any]]:
    if not isinstance(entry, dict):
        raise ValueError("Malformed jscpd clone")
    clone = cast("dict[str, object]", entry)
    kind = clone.get("kind")
    if (not isinstance(kind, str) or kind not in JSCPD_KINDS
            or not _jscpd_integer(clone.get("lines"))
            or not _jscpd_integer(clone.get("tokens"))):
        raise ValueError("Malformed jscpd clone")
    detail = _jscpd_similarity(clone) if kind == JSCPD_SIMILAR else ""
    if "matchedUnits" in clone:
        detail += f", {clone['matchedUnits'][0]} / {clone['matchedUnits'][1]} matched units"
    locations = (_jscpd_fragment(clone.get("firstFile"), root, allowed),
                 _jscpd_fragment(clone.get("secondFile"), root, allowed))
    items: list[dict[str, Any]] = []
    for current, other in ((locations[0], locations[1]), (locations[1], locations[0])):
        items.append(diagnostic(
            "jscpd", f"{kind.upper()}_CLONE",
            f"{JSCPD_KINDS[kind]} clone of {other[0]}:{other[1]}:{other[2]} " +
            f"({clone['lines']} source lines, {clone['tokens']} tokens{detail})", path=current[0],
            line=current[1], column=current[2], native_details=clone,
            rule_is_adapter_category=True,
            documentation_url="https://jscpd.dev/reporters/json"))
    return items


def _validate_clone_engine(raw: dict[str, Any], root: Path, expected_paths: list[str],
                           expected_kind: str) -> None:
    from quard.clone_engine_utils import EXPECTED, validate_source_evidence

    if raw.get("engine") != EXPECTED or raw.get("sourcePaths") != [str(root / path)
                                                                  for path in expected_paths]:
        raise ValueError("Missing or mismatched native engine/source evidence")
    validate_source_evidence(raw, root, expected_paths, skipped=False)
    if raw.get("pass") != expected_kind:
        raise ValueError("Mismatched native clone pass")
    statistics = raw.get("statistics")
    if not isinstance(statistics, dict) or not isinstance(raw.get("duplicates"), list):
        raise ValueError("Missing native engine statistics or clones")
    total = statistics.get("total")
    formats = statistics.get("formats")
    if not isinstance(total, dict) or not isinstance(formats, dict):
        raise ValueError("Missing native engine totals or formats")
    python_stats = formats.get("python")
    if not isinstance(python_stats, dict) or any(
            not _jscpd_integer(total.get(key), minimum=0)
            or total[key] != python_stats.get(key)
            for key in ("sources", "clones", "lines", "tokens", "duplicatedLines",
                        "duplicatedTokens")):
        raise ValueError("Incomplete native engine accounting")
    if (total["sources"] != len(expected_paths) or total["clones"] != len(raw["duplicates"])
            or total["duplicatedTokens"] != sum(
                clone.get("tokens", 0) for clone in raw["duplicates"] if isinstance(clone, dict))
            or total["duplicatedLines"] != sum(
                clone.get("lines", 0) - clone.get("unmatchedLines", [0, 0])[0]
                for clone in raw["duplicates"] if isinstance(clone, dict))):
        raise ValueError("Inconsistent native engine accounting")
    for clone in raw["duplicates"]:
        if not isinstance(clone, dict) or clone.get("kind") != expected_kind:
            raise ValueError("Unexpected native engine clone kind")
        matched = clone.get("matchedUnits")
        if (not isinstance(matched, list) or len(matched) != 2
                or any(not _jscpd_integer(n, minimum=raw["minimumUnits"])
                       for n in matched)):
            raise ValueError("Invalid native matched-unit accounting")
        if expected_kind == JSCPD_SIMILAR:
            _ = _jscpd_similarity(clone)
            unmatched = clone.get("unmatchedLines")
            if (not isinstance(unmatched, list) or len(unmatched) != 2
                    or any(not _jscpd_integer(n, minimum=0) for n in unmatched)):
                raise ValueError("Invalid native unmatched line counts")
        elif clone.get("similarity") is not None or clone.get("method") is not None:
            raise ValueError("Unexpected native similarity metadata")
        for key in ("firstFile", "secondFile"):
            side = clone.get(key)
            if not isinstance(side, dict):
                raise ValueError("Missing native fragment")
            path = relative_path(side.get("name"), root)
            if path not in expected_paths:
                raise ValueError("Native fragment outside source scope")
            if matched[0 if key == "firstFile" else 1] > max(raw["unitSizes"][str(root / path)],
                                                               default=0):
                raise ValueError("Native match exceeds a prepared unit")
            start = side.get("startLoc")
            end = side.get("endLoc")
            original = (root / path).read_text(encoding="utf-8")
            lines = original.splitlines()
            source_lines = original.splitlines(keepends=True)
            if not isinstance(start, dict) or not isinstance(end, dict):
                raise ValueError("Missing native source range")
            first, last = start.get("line"), end.get("line")
            if (not _jscpd_integer(first) or not _jscpd_integer(last) or last < first
                    or last > len(lines) or not _jscpd_integer(start.get("column"), minimum=0)
                    or not _jscpd_integer(end.get("column"), minimum=0)
                    or not _jscpd_integer(start.get("position"), minimum=0)
                    or not _jscpd_integer(end.get("position"), minimum=0)
                    or side.get("start") != first or side.get("end") != last
                    or start["column"] > len(lines[first - 1].encode("utf-8"))
                    or end["column"] > len(lines[last - 1].encode("utf-8"))
                    or start["position"] != len("".join(source_lines[:first - 1]).encode("utf-8"))
                    + start["column"]
                    or end["position"] != len("".join(source_lines[:last - 1]).encode("utf-8"))
                    + end["column"]):
                raise ValueError("Native fragment does not map to original source")
            if expected_kind == JSCPD_SIMILAR and clone["unmatchedLines"][
                    0 if key == "firstFile" else 1] >= last - first + 1:
                raise ValueError("Invalid native matched line count")
        first_side = clone["firstFile"]
        first_lines = (Path(first_side["name"])).read_text(encoding="utf-8").splitlines()
        fragment = "\n".join(first_lines[first_side["startLoc"]["line"] - 1:
                                         first_side["endLoc"]["line"]])
        if clone.get("fragment") != fragment or clone.get("lines") != (
                first_side["endLoc"]["line"] - first_side["startLoc"]["line"] + 1):
            raise ValueError("Native clone fragment text differs from original source")


def parse_jscpd(payload: str, root: Path, expected_paths: list[str],
                expected_engine: bool = False, expected_kind: str = "exact"
                ) -> list[dict[str, Any]]:
    # Fail closed on a missing, truncated or incomplete native report.
    allowed = set(expected_paths)
    if expected_engine:
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError("Missing native engine report")
        _validate_clone_engine(raw, root, expected_paths, expected_kind)
    items: list[dict[str, Any]] = []
    for entry in _jscpd_duplicates(payload, len(allowed)):
        items.extend(_jscpd_clone(entry, root, allowed))
    return items


def normalize(tool: str, result: dict[str, Any], root: Path, json_file: Path | None = None,
              expected_paths: list[str] | None = None,
              expected_engine: bool = False, expected_kind: str = "exact") -> dict[str, Any]:
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
        elif tool == "jscpd":
            payload = json_file.read_text(encoding="utf-8") if json_file else ""
            items = parse_jscpd(payload, root, expected_paths or [], expected_engine, expected_kind)
            if result.get("exit_code") != (1 if items else 0):
                error = error or "JSCPD_EXECUTION_ERROR"
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
        elif tool == "dependency-types":
            items, parse_error = parse_dependency_types(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "DEPENDENCY_TYPES_EXECUTION_ERROR"
        elif tool == "contract-folders":
            items, parse_error = parse_contract_folders(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "CONTRACT_FOLDERS_EXECUTION_ERROR"
        elif tool == "test-placement":
            items, parse_error = parse_test_placement(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "TEST_PLACEMENT_EXECUTION_ERROR"
        elif tool == "outer-classes":
            items, parse_error = parse_outer_classes(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "OUTER_CLASSES_EXECUTION_ERROR"
        elif tool == "free-function-placement":
            items, parse_error = parse_free_function_placement(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "FREE_FUNCTION_PLACEMENT_EXECUTION_ERROR"
        elif tool == "leading-underscores":
            items, parse_error = parse_leading_underscores(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "LEADING_UNDERSCORES_EXECUTION_ERROR"
        elif tool == "constructor-purity":
            items, parse_error = parse_constructor_purity(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "CONSTRUCTOR_PURITY_EXECUTION_ERROR"
        elif tool == "lambda-injection":
            items, parse_error = parse_lambda_injection(result.get("stdout", ""), root)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "LAMBDA_INJECTION_EXECUTION_ERROR"
        elif tool == "encapsulated-only":
            items, parse_error = parse_encapsulated_only(result.get("stdout", ""), diagnostic)
            error = error or parse_error
            expected = 2 if parse_error else (1 if items else 0)
            if result.get("exit_code") != expected:
                error = error or "ENCAPSULATED_ONLY_EXECUTION_ERROR"
        elif tool.startswith("tach"):
            if '"quard_tach_adapter"' in text:
                obj = find_json(text, "quard_tach_adapter")
                if obj["quard_tach_adapter"] != 1 or not isinstance(obj.get("diagnostics"), list):
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
