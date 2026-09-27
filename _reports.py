"""Normalize real tool diagnostics; retain raw output on every path."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from _presidio_policy import ALL_ENTITIES

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
            is_infrastructure_error=rule in {"F9802", "F9804"},
            documentation_url=("https://github.com/Yelp/detect-secrets" if rule in {"W9803", "F9804"}
                               else "https://pylint.readthedocs.io/en/stable/user_guide/messages/messages_overview.html"
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


def normalize_presidio(result: dict[str, Any], root: Path, output: Path,
                       expected: list[str]) -> dict[str, Any]:
    """Accept only the worker's closed schema, never third-party prose or source text."""
    coverage: dict[str, list[str]] = {"scanned": [], "skipped": []}
    items: list[dict[str, Any]] = []
    error = "PRESIDIO_SCAN_INCOMPLETE"
    try:
        data = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) not in (
                {"scanned", "skipped", "findings", "failed"},
                {"scanned", "skipped", "findings", "failed", "error"}):
            raise ValueError("Invalid worker schema")
        if ("error" in data and data["error"] not in
                {"PRESIDIO_MODEL_UNAVAILABLE", "PRESIDIO_POLICY_MISMATCH",
                 "PRESIDIO_SEED_CONFIG_ERROR", "PRESIDIO_IDENTIFIER_CONFIG_ERROR"}):
            raise ValueError("Invalid worker error")
        for key in ("scanned", "skipped", "failed"):
            if (not isinstance(data[key], list) or any(type(p) is not str for p in data[key]) or
                    len(data[key]) != len(set(data[key])) or not set(data[key]) <= set(expected)):
                raise ValueError("Invalid worker coverage")
        if (set(data["scanned"]) & set(data["skipped"]) or
                set(data["scanned"]) & set(data["failed"]) or
                set(data["skipped"]) & set(data["failed"])):
            raise ValueError("Overlapping worker coverage")
        if not isinstance(data["findings"], list):
            raise ValueError("Invalid findings")
        for entry in data["findings"]:
            if (not isinstance(entry, dict) or set(entry) !=
                    {"path", "line", "column", "span_length", "entity", "score"} or
                    entry["path"] not in data["scanned"] or type(entry["line"]) is not int or
                    entry["line"] < 1 or type(entry["column"]) is not int or entry["column"] < 1 or
                    type(entry["span_length"]) is not int or entry["span_length"] < 1 or
                    entry["entity"] not in ALL_ENTITIES or
                    type(entry["score"]) not in (int, float) or not 0 <= entry["score"] <= 1):
                raise ValueError("Invalid finding")
            items.append(diagnostic("presidio", entry["entity"],
                                    f"Potential {entry['entity']} detected (score {entry['score']:.4f}).",
                                    path=entry["path"], line=entry["line"], column=entry["column"],
                                    score=entry["score"], span_length=entry["span_length"]))
        if result.get("exit_code") in (0, 1, 2) and not result.get("error") and not result.get("stdout") and not result.get("stderr"):
            coverage = {key: data[key] for key in ("scanned", "skipped")}
        if (set(data["scanned"] + data["skipped"] + data["failed"]) == set(expected) and
                not data["failed"] and data["scanned"] and "error" not in data and
                result.get("exit_code") == (1 if items else 0) and not result.get("error") and
                not result.get("stdout") and not result.get("stderr")):
            error = None
        elif data.get("error") in {"PRESIDIO_MODEL_UNAVAILABLE", "PRESIDIO_POLICY_MISMATCH",
                                   "PRESIDIO_SEED_CONFIG_ERROR", "PRESIDIO_IDENTIFIER_CONFIG_ERROR"}:
            error = data["error"]
    except (OSError, ValueError, KeyError, TypeError):
        coverage = {"scanned": [], "skipped": []}
        items = []
    if result.get("error") or result.get("exit_code") not in (0, 1, 2) or result.get("stdout") or result.get("stderr"):
        items = []
    if error:
        items.append(diagnostic("presidio", error,
                                "Presidio could not confirm the selected text scan; check identifier configuration, pinned models, recognizer inventory and worker status.",
                                is_infrastructure_error=True))
    return {"status": "error" if error else ("violations" if items else "passed"),
            "error": error, "diagnostics": items, "coverage": coverage}


def sorted_diagnostics(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Only remove byte-identical observations of the same native diagnostic.
    seen = set()
    unique = []
    for item in items:
        identity = [item["tool"], item["rule"], item["path"], item["line"], item["column"], item["message"]]
        if "span_length" in item:
            identity.append(item["span_length"])
        encoded = json.dumps(identity, ensure_ascii=False)
        item["fingerprint"] = hashlib.sha256(encoded.encode()).hexdigest()[:24]
        key = tuple(identity)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return sorted(unique, key=lambda d: (d["path"] or "", d["line"] or 0,
                                        d["column"] or 0, d["tool"], d["rule"], d["message"]))
