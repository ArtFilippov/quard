#!/usr/bin/env python3
"""JSON adapter for pinned Tach APIs, including exact unused-dependency checks.

No new architecture rules. Avoid the native CLI JSON early-exit that omits exact checks.
Native messages are also written to stderr for diagnosis.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


def run(mode: str, root: Path) -> dict[str, Any]:
    from tach import extension
    from tach.check_external import check_external
    from tach.errors import TachCircularDependencyError, TachVisibilityError
    from tach.parsing import parse_project_config

    config = parse_project_config(root=root)
    if config is None:
        raise RuntimeError("Tach configuration was not found.")
    items = []
    native = []
    has_execution_error = False
    try:
        if mode == "check-external":
            native = check_external(project_root=root, project_config=config)
        else:
            native = extension.check(project_root=root, project_config=config,
                                     dependencies=True, interfaces=True)
        native_details = json.loads(extension.serialize_diagnostics_json(native, pretty_print=False))
        if native:
            print(extension.format_diagnostics(diagnostics=native), file=sys.stderr)
        for index, value in enumerate(native):
            detail = native_details[index]
            data = detail.get("Located", detail.get("Global", {}))
            category = data.get("details", {})
            code = category.get("Code", category.get("Configuration", "diagnostic"))
            name = next(iter(code)) if isinstance(code, dict) and code else str(code)
            is_configuration = value.is_configuration()
            has_execution_error |= is_configuration
            items.append({"rule": name, "message": value.to_string(), "path": value.pyfile_path(),
                          "line": value.pyline_number(), "severity": "error" if value.is_error() else "warning",
                          "is_infrastructure_error": is_configuration, "native_details": detail})
        if mode == "check" and config.exact:
            unused = extension.detect_unused_dependencies(project_root=root, project_config=config)
            for entry in unused:
                names = [dependency.path for dependency in entry.dependencies]
                message = f"'{entry.path}' does not depend on: {names}. Remove unused declarations from tach.toml."
                print(message, file=sys.stderr)
                items.append({"rule": "unused-dependency", "message": message, "path": "tach.toml",
                              "line": None, "severity": "error", "rule_is_adapter_category": True})
    except TachCircularDependencyError as exc:
        for module in exc.dependencies:
            items.append({"rule": "cyclic-dependency", "message": f"Circular dependency detected for module '{module}'.",
                          "path": "tach.toml", "line": None, "severity": "error", "rule_is_adapter_category": True})
    except TachVisibilityError as exc:
        for dependent, dependency, visibility in exc.visibility_errors:
            items.append({"rule": "visibility", "message": f"'{dependent}' cannot depend on '{dependency}'; allowed visibility: {visibility}.",
                          "path": "tach.toml", "line": None, "severity": "error", "rule_is_adapter_category": True})
    return {"eryx_tach_adapter": 1, "diagnostics": items,
            "execution_error": "TACH_CONFIGURATION_DIAGNOSTIC" if has_execution_error else None,
            "exit_code": 2 if has_execution_error else (1 if items else 0)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "check-external"])
    args = parser.parse_args()
    try:
        report = run(args.mode, Path.cwd())
    except Exception as exc:
        report = {"eryx_tach_adapter": 1, "diagnostics": [], "execution_error": "TACH_EXECUTION_ERROR",
                  "message": f"{type(exc).__name__}: {exc}", "exit_code": 2}
        print(report["message"], file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False))
    return report["exit_code"]
