#!/usr/bin/env python3
"""Run all configured checks, preserving native output and a unified JSON report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
from typing import Any, Callable

from eryx._common import SCHEMA, capture, project_dir, repo_lock
from eryx._reports import diagnostic
from eryx.check_runner.native_check_runner import (
    NativeCheckRunner, binary, execute_job, installed_versions, make_jobs, select_lcom_files,
)

__all__ = ["argparse", "json", "Path", "sys", "Any", "Callable", "SCHEMA", "capture",
           "project_dir", "repo_lock", "diagnostic", "NativeCheckRunner", "binary",
           "execute_job", "installed_versions", "make_jobs", "select_lcom_files",
           "run_checks", "display", "main"]


def run_checks(root: Path, *, serial: bool = False, timeout: int = 600,
               lcom_files: str | None = None,
               capture_fn: Callable[..., dict[str, Any]] = capture,
               versions_fn: Callable[..., Any] = installed_versions) -> dict[str, Any]:
    """Run checks through the native runner for existing command callers.

    Args:
        root (Path): Consumer project root.
        serial (bool, default=False): Run jobs one at a time.
        timeout (int, default=600): Per-job time limit in seconds.
        lcom_files (str | None, default=None): Optional selected LCOM4 paths.
        capture_fn (Callable[..., dict[str, Any]], default=capture): Native subprocess capture.
        versions_fn (Callable[..., Any], default=installed_versions): Tool version checker.

    Returns:
        dict[str, Any]: Complete archived check report.
    """
    return NativeCheckRunner().run(root, serial=serial, timeout=timeout, lcom_files=lcom_files,
                                   capture_fn=capture_fn, versions_fn=versions_fn)


def display(report: dict[str, Any], json_output: bool, limit: int) -> None:
    if json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    summary = report["summary"]
    print(f"{report['status'].upper()}: {summary['diagnostics']} diagnostics; exit={report['exit_code']}; {report['duration_seconds']}s")
    for item in report["diagnostics"][:limit]:
        location = item.get("path") or "<system>"
        if item.get("line") is not None:
            location += f":{item['line']}"
            if item.get("column") is not None:
                location += f":{item['column']}"
        text = " ".join(item["message"].split())
        if len(text) > 600:
            text = text[:597] + "..."
        print(f"{location}: {item['tool']}/{item['rule']}: {text}")
    omitted = max(0, summary["diagnostics"] - limit)
    if omitted:
        print(f"... {omitted} more diagnostics in the JSON report.")
    print(f"REPORT: {report['latest_report_path']}")
    print(f"RAW LOGS: {Path(report['report_path']).parent.as_posix()}/raw/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Consumer root (default: current directory)")
    parser.add_argument("--json", action="store_true", help="Emit one full JSON document, with no human output on stdout")
    parser.add_argument("--serial", action="store_true", help="Run all analyzers sequentially (same coverage)")
    parser.add_argument("--timeout", type=int, default=600, help="Maximum seconds per analyzer (default 600)")
    parser.add_argument("--max-diagnostics", type=int, default=100, help="Limit human output only; JSON always contains all diagnostics")
    parser.add_argument("--lcom_files", help="Whitespace-separated project-relative .py paths for LCOM4 only; empty string skips LCOM4")
    args = parser.parse_args()
    if args.timeout < 1 or args.max_diagnostics < 0:
        parser.error("timeout must be positive and max-diagnostics nonnegative")
    try:
        root = project_dir(args.project)
        with repo_lock(root):
            report = run_checks(root, serial=args.serial, timeout=args.timeout, lcom_files=args.lcom_files)
        display(report, args.json, args.max_diagnostics)
        return report["exit_code"]
    except Exception as exc:
        # Do not overwrite another process's current report on lock contention.
        data = {"schema_version": SCHEMA, "status": "error", "exit_code": 2,
                "summary": {"diagnostics": 1, "infrastructure_errors": 1},
                "diagnostics": [diagnostic("system", getattr(exc, "code", "RUNNER_ERROR"), str(exc), is_infrastructure_error=True)]}
        print(json.dumps(data, ensure_ascii=False) if args.json else f"ERROR: {data['diagnostics'][0]['rule']}: {exc}")
        return 2
