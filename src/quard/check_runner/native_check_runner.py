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
from threading import Event
import time
import uuid
from typing import Any, override

from quard._common import (SCHEMA, QuardError, capture, read_json,
                          require_runtime, tool_dir, tool_revision, safe_target, write_json)
from quard._reports import diagnostic, sorted_diagnostics
from quard.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
from quard.architecture_draft.configured_architecture_draft import ConfiguredArchitectureDraft
from quard.architecture_draft.generated_architecture_utils import generated, regenerate
from quard.check_runner.check_runner import CheckRunner
from quard.check_selection_utils import (CHECK_DEFAULTS, CONSTRUCTOR_PURITY, DUPLICATE_JOBS,
                                         OPTIONAL_JOBS, TACH_JOBS, validate_architecture_checks)
from quard.check_runner.runner_utils import (binary, execute_job, installed_versions,
                                                inactive_job, make_jobs, select_lcom_files,
                                                capture_clone_batch, dependent_job, envelope_jobs)
from quard.clone_engine_utils import require_source_pin

__all__ = ["NativeCheckRunner", "binary", "execute_job", "installed_versions",
           "make_jobs", "select_lcom_files"]

ARCHITECTURE_NOT_INITIALIZED = "ARCHITECTURE_NOT_INITIALIZED"


class NativeCheckRunner(CheckRunner):
    """Orchestrate independent analyzer jobs for a selected consumer."""

    @override
    def run_jobs(self, jobs: list[dict[str, Any]], *, serial: bool,
                 execute: Callable[[dict[str, Any]], dict[str, Any]]
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        results: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        workers = 1 if serial else min(4, max(1, os.cpu_count() or 1), len(jobs))
        ready = Event()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(dependent_job, job, execute, ready): job
                       for job in envelope_jobs(jobs)}
            for future in as_completed(futures):
                job = futures[future]
                result: dict[str, Any]
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
        run_dir = root / ".quard/reports" / run_id
        safe_target(run_dir, root)
        run_dir.mkdir(parents=True, exist_ok=False)
        diagnostics: list[dict[str, Any]] = []
        versions: dict[str, str | None] = {}
        files: dict[str, list[str]] = {"production": [], "tests": [], "excluded_directories": [],
                                       "excluded_gateways": []}
        lcom_coverage: dict[str, Any] = {"mode": "full" if lcom_files is None else "selected",
                                         "requested": lcom_files, "selected_files": [],
                                         "analyzed_files": []}
        architecture_ready = False
        quard_version = "unknown"
        results: list[dict[str, Any]] = []
        try:
            require_runtime()
            quard_version = read_json(tool_dir() / "tools.json").get("quard_version", "unknown")
            versions, problems = versions_fn(root)
            diagnostics.extend(problems)
            policy = ConfiguredConsumerPolicy(root)
            files = policy.files()
            if not files["production"]:
                raise QuardError("EMPTY_SOURCE_SCOPE", "No Python source files found under src/. "
                                + "Refusing an empty successful check.")
            try:
                checks = policy.checks()
            except QuardError as exc:
                checks = {**CHECK_DEFAULTS, CONSTRUCTOR_PURITY: False}
                diagnostics.append(diagnostic("constructor-purity", exc.code, str(exc),
                                              is_infrastructure_error=True))
                results.append({"name": "constructor-purity", "tool": "constructor-purity",
                                 "status": "error", "exit_code": None,
                                 "duration_seconds": 0, "error": exc.code, "raw_output": {}})
            else:
                if not checks["constructor_purity"]:
                    results.append({"name": "constructor-purity", "tool": "constructor-purity",
                                    "status": "skipped", "exit_code": None,
                                    "duration_seconds": 0, "error": None, "raw_output": {}})
            for key, name in OPTIONAL_JOBS.items():
                if not checks[key] and (key != "lcom4" or lcom_files is None):
                    results.append({"name": name, "tool": name, "status": "skipped",
                                    "exit_code": None, "duration_seconds": 0,
                                    "error": None, "raw_output": {}})
            if not checks["duplicate_detection"]:
                for name in DUPLICATE_JOBS:
                    results.append({"name": name, "tool": "jscpd", "status": "skipped",
                                     "exit_code": None, "duration_seconds": 0,
                                     "error": None, "raw_output": {}})
            try:
                require_source_pin(root)
            except QuardError as exc:
                if not any(item["rule"] == exc.code for item in diagnostics):
                    diagnostics.append(diagnostic("system", exc.code, str(exc),
                                                  is_infrastructure_error=True))
                if checks["duplicate_detection"]:
                    for name in DUPLICATE_JOBS:
                        results.append({"name": name, "tool": "jscpd", "status": "error",
                                        "exit_code": None, "duration_seconds": 0,
                                        "error": exc.code, "raw_output": {}})
                    checks["duplicate_detection"] = False
            if not checks["tach"]:
                for name in TACH_JOBS:
                    results.append(inactive_job(name, "skipped"))
            if not checks["dependency_types"]:
                results.append(inactive_job("dependency-types", "skipped"))
            if not checks["tuple_signatures"]:
                results.append(inactive_job("tuple-signatures", "skipped"))
            try:
                if not checks["lcom4"] and lcom_files is not None:
                    raise QuardError("LCOM4_DISABLED",
                                    "--lcom_files conflicts with [checks] lcom4 = false.")
                selected = select_lcom_files(files, lcom_files)
                lcom_coverage["mode"] = ("full" if lcom_files is None else
                                         "skipped" if lcom_files == "" else "selected")
                if not checks["lcom4"]:
                    selected = []
                    lcom_coverage["mode"] = "skipped"
                lcom_coverage["selected_files"] = selected
                if lcom_files == "" and checks["lcom4"]:
                    results.append({"name": "lcom4", "tool": "lcom4", "status": "skipped",
                                    "exit_code": None, "duration_seconds": 0,
                                    "error": None, "raw_output": {}})
            except QuardError as exc:
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
                    raise QuardError("READABILITY_NOT_APPLIED", ", ".join(mismatch)
                                    + "; run uv run --locked --group dev python quard/apply.py.")
            except QuardError as exc:
                diagnostics.append(diagnostic("system", exc.code, str(exc),
                                              is_infrastructure_error=True))
            try:
                mismatch = policy.scope_drift()
                if mismatch:
                    raise QuardError("SCOPE_NOT_APPLIED", ", ".join(mismatch)
                                    + "; run uv run --locked --group dev python quard/apply.py.")
            except QuardError as exc:
                diagnostics.append(diagnostic("system", exc.code, str(exc),
                                              is_infrastructure_error=True))
            architecture_error: str | None = None
            try:
                if generated(root):
                    _ = regenerate(root)
                elif checks["tach"]:
                    marker_path = root / ".quard/architecture.ready.json"
                    if not marker_path.exists():
                        raise QuardError(ARCHITECTURE_NOT_INITIALIZED,
                                        "Architecture configuration has not been accepted.")
                    marker = read_json(marker_path)
                    if not isinstance(marker, dict) or not marker.get("accepted"):
                        raise QuardError(ARCHITECTURE_NOT_INITIALIZED,
                                        "Architecture configuration has not been accepted.")
                    ConfiguredArchitectureDraft(root).validate()
                architecture_ready = True
            except Exception as exc:
                architecture_error = str(getattr(exc, "code", "ARCHITECTURE_VALIDATION_FAILED"))
                legacy = not (root / ".quard/architecture.generated.json").exists()
                message = str(exc)
                if legacy and checks["tach"] and architecture_error == ARCHITECTURE_NOT_INITIALIZED:
                    message += (" Run quard/init_architecture.py, review tach.toml, "
                                "then run it with --accept.")
                diagnostics.append(diagnostic("system", architecture_error, message,
                                               is_infrastructure_error=True))
            try:
                validate_architecture_checks(root, checks)
            except QuardError as exc:
                diagnostics.append(diagnostic("dependency-types", exc.code, str(exc),
                                              is_infrastructure_error=True))
                results.append(inactive_job("dependency-types", "error", exc.code))
                checks["dependency_types"] = False
            jobs = make_jobs(root, run_dir, files, architecture_ready,
                             None if lcom_files is None and checks["lcom4"] else selected,
                             checks["constructor_purity"],
                             policy.scope()["test_directory_names"], checks)
            # Native analyzers complete independently. A failing job never cancels the others.
            job_results, job_diagnostics = self.run_jobs(
                jobs, serial=serial, execute=partial(execute_job, root=root, run_dir=run_dir,
                     timeout=timeout, capture_fn=capture_clone_batch(
                         jobs, root, run_dir, timeout, capture_fn)))
            diagnostics.extend(job_diagnostics)
            results.extend(job_results)
            if any(result["name"] == "lcom4" and result["status"] in {"passed", "violations"}
                   for result in job_results):
                lcom_coverage["analyzed_files"] = selected
            if not architecture_ready and checks["tach"]:
                for name in TACH_JOBS:
                    results.append(inactive_job(name, "blocked", architecture_error))
            if not architecture_ready and checks["dependency_types"]:
                results.append(inactive_job("dependency-types", "blocked", architecture_error))
        except Exception as exc:
            diagnostics.append(diagnostic("system", getattr(exc, "code", "RUNNER_ERROR"),
                                          str(exc), is_infrastructure_error=True))
        diagnostics = sorted_diagnostics(diagnostics)
        has_error = (any(item.get("is_infrastructure_error") for item in diagnostics)
                     or any(result["status"] == "error" for result in results))
        code = 2 if has_error else (1 if diagnostics else 0)
        counts = Counter(item["tool"] for item in diagnostics)
        report = {"schema_version": SCHEMA, "quard_version": quard_version,
                  "quard": tool_revision(), "run_id": run_id,
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
                  "latest_report_path": ".quard/reports/latest.json",
                  "notes": ["Every reported diagnostic fails the check, "
                            + "including informational messages.",
                            "No source files are modified. No baseline or automatic "
                            + "fixes are applied.",
                            "Tach names come from native diagnostics or explicitly marked adapter "
                            + "categories; raw details are retained.",
                            "Pylint receives .py files; Pyright also checks discovered "
                            + ".pyi stubs."]}
        write_json(run_dir / "report.json", report, root)
        write_json(root / ".quard/reports/latest.json", report, root)
        return report
