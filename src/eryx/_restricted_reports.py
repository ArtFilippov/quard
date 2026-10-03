"""Strict validation for the independent restricted-call worker report."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast


def object_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected an encapsulated-only JSON object")
    return cast(dict[str, Any], value)


def array(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("Expected an encapsulated-only JSON array")
    return cast(list[Any], value)


def parse_encapsulated_only(text: str,
                           diagnostic: Callable[..., dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    obj = object_dict(json.loads(text))
    if (type(obj.get("eryx_encapsulated_only_adapter")) is not int
            or obj["eryx_encapsulated_only_adapter"] != 1
            or "execution_error" not in obj):
        raise ValueError("Unsupported encapsulated-only adapter report schema")
    errors = {"ENCAPSULATED_ONLY_PARSE_ERROR", "ENCAPSULATED_ONLY_EXECUTION_ERROR"}
    error = obj["execution_error"]
    if error is not None and (not isinstance(error, str) or error not in errors):
        raise ValueError("Unknown encapsulated-only execution error")
    marked: set[str] = set()
    for raw in array(obj.get("declarations")):
        declaration = object_dict(raw)
        if (type(declaration.get("marked")) is not bool
                or any(not isinstance(declaration.get(key), str) or not declaration[key]
                       for key in ("owner", "member", "path"))
                or any(type(declaration.get(key)) is not int or declaration[key] < 1
                       for key in ("line", "column"))):
            raise ValueError("Malformed encapsulated-only declaration evidence")
        if declaration["marked"]:
            marked.add(f"{declaration['owner']}.{declaration['member']}")
    items: list[dict[str, Any]] = []
    for raw in array(obj.get("diagnostics")):
        entry = object_dict(raw)
        if (not isinstance(entry.get("rule"), str)
                or entry["rule"] not in errors | {"ENCAPSULATED_ONLY_CALL"}
                or not isinstance(entry.get("message"), str)):
            raise ValueError("Malformed encapsulated-only diagnostic")
        rule = entry["rule"]
        if (entry.get("path") is not None and not isinstance(entry["path"], str)
                or any(entry.get(key) is not None and
                       (type(entry[key]) is not int or entry[key] < 1) for key in ("line", "column"))):
            raise ValueError("Malformed encapsulated-only diagnostic position")
        if rule == "ENCAPSULATED_ONLY_CALL" and (
                not isinstance(entry.get("path"), str) or not entry["path"]
                or not isinstance(entry.get("symbol"), str) or not entry["symbol"]
                or any(type(entry.get(key)) is not int for key in ("line", "column"))):
            raise ValueError("Missing encapsulated-only call location")
        if rule == "ENCAPSULATED_ONLY_CALL" and entry["symbol"] not in marked:
            raise ValueError("Restricted call has no marked declaration evidence")
        items.append(diagnostic("encapsulated-only", rule, entry["message"],
                                path=entry.get("path"), line=entry.get("line"),
                                column=entry.get("column"), symbol=entry.get("symbol"),
                                is_infrastructure_error=rule in errors))
    if bool(error) != any(item["is_infrastructure_error"] for item in items) or (
            error and not any(item["rule"] == error for item in items)):
        raise ValueError("Encapsulated-only error/diagnostic mismatch")
    return items, error
