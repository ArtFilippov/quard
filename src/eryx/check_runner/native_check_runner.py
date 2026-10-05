#!/usr/bin/env python3
"""Run all configured checks, preserving native output and a unified JSON report."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import partial
import os
from pathlib import Path
import sys
import time
import uuid
from typing import Any, override

from eryx._common import (SCHEMA, EryxError, capture, read_json,
                          require_runtime, tool_dir, tool_revision, safe_target, write_json)
from eryx._reports import diagnostic, sorted_diagnostics
from eryx.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
from eryx.architecture_draft.configured_architecture_draft import ConfiguredArchitectureDraft
from eryx.architecture_draft.generated_architecture_utils import generated, regenerate
from eryx.check_runner.check_runner import CheckRunner
from eryx.check_runner.runner_utils import (binary, execute_job, installed_versions,
                                             make_jobs, select_lcom_files)

__all__ = ["NativeCheckRunner", "binary", "execute_job", "installed_versions",
           "make_jobs", "select_lcom_files"]


class NativeCheckRunner(CheckRunner):
    """Orchestrate independent analyzer jobs for a selected consumer."""

    @override
    def run_jobs(self, jobs: list[dict[str, Any]], *, serial: bool,
                 execute: Callable[[dict[str, Any]], dict[str, Any]]
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        results: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        workers = 1 if serial else min(4, max(1, os.cpu_count() or 1), len(jobs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(execute, job): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"name": job["name"], "tool": job["tool"], "status": "error",
                              "exit_code": None, "duration_seconds": 0,
                              "error": "REPORT_WRITE_ERROR",
                              "raw_output": {}, "diagnostics": [diagnostic(
                                  job["tool"], "REPORT_WRITE_ERROR", str(exc),
                                  is_infrastructure_error=True)]}
                diagnostics.extend(result.pop("diagnostics"))
                results.append(result)
        return results, diagnostics

    @override
    def run(self, root: Path, *, serial: bool = False, timeout: int = 600,
            lcom_files: str | None = None,
            capture_fn: Callable[..., dict[str, Any]] = capture,
            versions_fn: Callable[..., Any] = installed_versions) -> dict[str, Any]:
        start = time.monotonic()
        run_id = (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                  + "-" + uuid.uuid4().hex[:8])
        run_dir = root / ".eryx/reports" / run_id
        safe_target(run_dir, root)
        run_dir.mkdir(parents=True, exist_ok=False)
        diagnostics: list[dict[str, Any]] = []
        versions: dict[str, str | None] = {}
        files: dict[str, list[str]] = {"production": [], "tests": [], "excluded_directories": []}
        lcom_coverage: dict[str, Any] = {"mode": "full" if lcom_files is None else "selected",
                                         "requested": lcom_files, "selected_files": [],
                                         "analyzed_files": []}
        architecture_ready = False
        eryx_version = "unknown"
        results: list[dict[str, Any]] = []
        try:
            require_runtime()
            eryx_version = read_json(tool_dir() / "tools.json").get("eryx_version", "unknown")
            versions, problems = versions_fn(root)
            diagnostics.extend(problems)
            policy = ConfiguredConsumerPolicy(root)
            files = policy.files()
            if not files["production"]:
                raise EryxError("EMPTY_SOURCE_SCOPE", "No Python source files found under src/. "
                                + "Refusing an empty successful check.")
            try:
                constructor_purity = policy.constructor_purity()
                if not constructor_purity:
                    results.append({"name": "constructor-purity", "tool": "constructor-purity",
                                    "status": "skipped", "exit_code": None,
                                    "duration_seconds": 0, "error": None, "raw_output": {}})
            except EryxError as exc:
                constructor_purity = False
                diagnostics.append(diagnostic("constructor-purity", exc.code, str(exc),
                                              is_infrastructure_error=True))
                results.append({"name": "constructor-purity", "tool": "constructor-purity",
                                "status": "error", "exit_code": None,
                                "duration_seconds": 0, "error": exc.code, "raw_output": {}})
            try:
                selected = select_lcom_files(files, lcom_files)
                lcom_coverage["mode"] = ("full" if lcom_files is None else
                                         "skipped" if lcom_files == "" else "selected")
                lcom_coverage["selected_files"] = selected
                if lcom_files == "":
                    results.append({"name": "lcom4", "tool": "lcom4", "status": "skipped",
                                    "exit_code": None, "duration_seconds": 0,
                                    "error": None, "raw_output": {}})
            except EryxError as exc:
                selected = []
                lcom_coverage["mode"] = "invalid"
                diagnostics.append(diagnostic("lcom4", exc.code, str(exc),
                                              is_infrastructure_error=True))
                results.append({"name": "lcom4", "tool": "lcom4", "status": "error",
                                "exit_code": None, "duration_seconds": 0,
                                "error": exc.code, "raw_output": {}})
            try:
                mismatch = policy.limit_drift()
                if mismatch:
                    raise EryxError("READABILITY_NOT_APPLIED", ", ".join(mismatch)
                                    + "; run uv run --locked --group dev python eryx/apply.py.")
            except EryxError as exc:
                diagnostics.append(diagnostic("system", exc.code, str(exc),
                                              is_infrastructure_error=True))
            try:
                mismatch = policy.scope_drift()
                if mismatch:
                    raise EryxError("SCOPE_NOT_APPLIED", ", ".join(mismatch)
                                    + "; run uv run --locked --group dev python eryx/apply.py.")
            except EryxError as exc:
                diagnostics.append(diagnostic("system", exc.code, str(exc),
                                              is_infrastructure_error=True))
            try:
                if generated(root):
                    _ = regenerate(root)
                else:
                    marker = read_json(root / ".eryx/architecture.ready.json")
                    if not isinstance(marker, dict) or not marker.get("accepted"):
                        raise EryxError("ARCHITECTURE_NOT_INITIALIZED",
                                        "Architecture configuration has not been accepted.")
                    ConfiguredArchitectureDraft(root).validate()
                architecture_ready = True
            except EryxError as exc:
                legacy = not (root / ".eryx/architecture.generated.json").exists()
                code = ("ARCHITECTURE_NOT_INITIALIZED" if legacy and not (
                    root / ".eryx/architecture.ready.json").exists() else exc.code)
                message = str(exc) + (" Run eryx/init_architecture.py, review tach.toml, "
                                      + "then run it with --accept." if legacy else "")
                diagnostics.append(diagnostic("system", code, message,
                                              is_infrastructure_error=True))
            jobs = make_jobs(root, run_dir, files, architecture_ready,
                             None if lcom_files is None else selected, constructor_purity,
                             policy.scope()["test_directory_names"])
            # Native analyzers complete independently. A failing job never cancels the others.
            job_results, job_diagnostics = self.run_jobs(
                jobs, serial=serial, execute=partial(execute_job, root=root, run_dir=run_dir,
                                                     timeout=timeout, capture_fn=capture_fn))
            diagnostics.extend(job_diagnostics)
            results.extend(job_results)
            if any(result["name"] == "lcom4" and result["status"] in {"passed", "violations"}
                   for result in job_results):
                lcom_coverage["analyzed_files"] = selected
            if not architecture_ready:
                for name in ("tach", "tach-external"):
                    results.append({"name": name, "tool": name, "status": "blocked",
                                     "exit_code": None, "duration_seconds": 0,
                                     "error": ("ARCHITECTURE_GENERATION_FAILED" if (
                                         root / ".eryx/architecture.generated.json").exists()
                                         else "ARCHITECTURE_NOT_INITIALIZED"), "raw_output": {}})
        except Exception as exc:
            diagnostics.append(diagnostic("system", getattr(exc, "code", "RUNNER_ERROR"),
                                          str(exc), is_infrastructure_error=True))
        diagnostics = sorted_diagnostics(diagnostics)
        has_error = (any(item.get("is_infrastructure_error") for item in diagnostics)
                     or any(result["status"] == "error" for result in results))
        code = 2 if has_error else (1 if diagnostics else 0)
        counts = Counter(item["tool"] for item in diagnostics)
        report = {"schema_version": SCHEMA, "eryx_version": eryx_version,
                  "eryx": tool_revision(), "run_id": run_id,
                  "status": {0: "passed", 1: "violations", 2: "error"}[code], "exit_code": code,
                  "duration_seconds": round(time.monotonic() - start, 3),
                  "python": sys.version.split()[0],
                  "versions": versions, "coverage": {**files, "lcom4": lcom_coverage},
                  "summary": {"diagnostics": len(diagnostics),
                              "by_tool": dict(sorted(counts.items())),
                              "infrastructure_errors": sum(
                                  bool(d.get("is_infrastructure_error")) for d in diagnostics)},
                  "tools": sorted(results, key=lambda item: item["name"]),
                  "diagnostics": diagnostics,
                  "report_path": (run_dir / "report.json").relative_to(root).as_posix(),
                  "latest_report_path": ".eryx/reports/latest.json",
                  "notes": ["Every reported diagnostic fails the check, "
                            + "including informational messages.",
                            "No source files are modified. No baseline or automatic "
                            + "fixes are applied.",
                            "Tach names come from native diagnostics or explicitly marked adapter "
                            + "categories; raw details are retained.",
                            "Pylint receives .py files; Pyright also checks discovered "
                            + ".pyi stubs."]}
        write_json(run_dir / "report.json", report, root)
        write_json(root / ".eryx/reports/latest.json", report, root)
        return report
