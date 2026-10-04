#!/usr/bin/env python3
"""Run all configured checks, preserving native output and a unified JSON report."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from importlib import metadata
from functools import partial
import os
from pathlib import Path
import shlex
import sys
import time
import uuid
from typing import Any, override

from eryx._common import (SCHEMA, EryxError, capture, read_json,
                          require_runtime, tool_dir, tool_revision, safe_target, write_json)
from eryx._reports import diagnostic, sorted_diagnostics
from eryx.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
from eryx.architecture_draft.configured_architecture_draft import ConfiguredArchitectureDraft
from eryx.check_job.captured_check_job import CapturedCheckJob
from eryx.check_runner.check_runner import CheckRunner


def installed_versions(root: Path) -> tuple[dict[str, str | None], list[dict[str, Any]]]:
    expected = read_json(tool_dir() / "tools.json")["dependencies"]
    versions: dict[str, str | None] = {}
    problems: list[dict[str, Any]] = []
    for name, required in expected.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = None
        versions[name] = actual
        if actual != required:
            message = (f"{name}: expected {required}, found {actual or 'not installed'}. "
                       "Run the installer or uv sync --locked --group dev.")
            problems.append(diagnostic("system", "TOOL_VERSION_MISMATCH", message,
                                       is_infrastructure_error=True))
    return versions, problems


def binary(name: str) -> str:
    # Never accidentally execute an unrelated globally installed analyzer.
    return str(Path(sys.executable).parent / name)


def select_lcom_files(files: dict[str, list[str]], requested: str | None) -> list[str]:
    """Resolve explicit paths against the same discovered scope as the full check."""
    available = {path for profile in ("production", "tests") for path in files[profile]
                 if path.endswith(".py")}
    if requested is None:
        return sorted(available)
    if requested == "":
        return []
    try:
        paths = shlex.split(requested)
    except ValueError as exc:
        raise EryxError("INVALID_LCOM_FILES", f"Invalid --lcom_files quoting: {exc}") from exc
    if not paths:
        raise EryxError("INVALID_LCOM_FILES",
                        "--lcom_files must contain .py paths or be exactly empty to skip LCOM4.")
    selected: list[str] = []
    for raw in paths:
        path = Path(raw)
        if (path.is_absolute() or ".." in path.parts or path.suffix != ".py"
                or path.as_posix() not in available):
            raise EryxError("INVALID_LCOM_FILES",
                            f"Invalid or out-of-scope --lcom_files path: {raw!r}; "
                            + "expected an existing, non-excluded, non-symlinked .py file "
                            + "under src/ or tests/.")
        selected.append(path.as_posix())
    return list(dict.fromkeys(selected))


def make_jobs(root: Path, run_dir: Path, files: dict[str, list[str]], architecture_ready: bool,
              lcom_files: list[str] | None = None, constructor_purity: bool = False,
              test_directory_names: list[str] | None = None) -> list[dict[str, Any]]:
    # Pylint opens --output before the runner writes captured stdout/stderr logs.
    # Create the directory before any parallel analyzer can start.
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    jobs: list[dict[str, Any]] = []
    for profile in ("production", "tests"):
        python_files = [path for path in files[profile] if path.endswith(".py")]
        if not python_files:
            continue
        manifest = run_dir / f"pylint-{profile}-files.json"
        output = run_dir / "raw" / f"pylint-{profile}.json"
        write_json(manifest, python_files, root)
        config = root / ("pyproject.toml" if profile == "production" else ".eryx/pylint-tests.toml")
        jobs.append({"name": f"pylint-{profile}", "tool": "pylint", "profile": profile,
                     "argv": [sys.executable, str(tool_dir() / "_pylint_worker.py"),
                              "--manifest", str(manifest), "--config", str(config),
                              "--output", str(output)], "json_file": output})
    source_files = select_lcom_files(files, None) if lcom_files is None else lcom_files
    if lcom_files is None or source_files:
        manifest = run_dir / "lcom4-files.json"
        write_json(manifest, source_files, root)
        jobs.append({"name": "lcom4", "tool": "lcom4", "argv": [sys.executable,
                     str(tool_dir() / "_lcom4_worker.py"), "--manifest", str(manifest)]})
    manifest = run_dir / "method-decorators-files.json"
    write_json(manifest, sorted(files["production"] + files["tests"]), root)
    jobs.append({"name": "method-decorators", "tool": "method-decorators", "argv": [sys.executable,
                  str(tool_dir() / "_method_decorators_worker.py"), "--manifest", str(manifest)]})
    manifest = run_dir / "instance-fields-files.json"
    write_json(manifest, sorted(files["production"] + files["tests"]), root)
    jobs.append({"name": "instance-fields", "tool": "instance-fields", "argv": [sys.executable,
                  str(tool_dir() / "_instance_fields_worker.py"), "--manifest", str(manifest)]})
    manifest = run_dir / "class-contracts-files.json"
    write_json(manifest, sorted(files["production"] + files["tests"]), root)
    jobs.append({"name": "class-contracts", "tool": "class-contracts", "argv": [sys.executable,
                  str(tool_dir() / "_class_contracts_worker.py"), "--manifest", str(manifest)]})
    manifest = run_dir / "contract-folders-files.json"
    write_json(manifest, sorted(files["production"] + files["tests"]), root)
    jobs.append({"name": "contract-folders", "tool": "contract-folders", "argv": [sys.executable,
                  str(tool_dir() / "_contract_folders_worker.py"), "--manifest", str(manifest)]})
    manifest = run_dir / "test-placement-files.json"
    write_json(manifest, {"production": files["production"], "tests": files["tests"],
                          "test_directory_names": test_directory_names or ["tests"]}, root)
    jobs.append({"name": "test-placement", "tool": "test-placement", "argv": [sys.executable,
                  str(tool_dir() / "_test_placement_worker.py"), "--manifest", str(manifest)]})
    manifest = run_dir / "outer-classes-files.json"
    write_json(manifest, sorted(files["production"] + files["tests"]), root)
    jobs.append({"name": "outer-classes", "tool": "outer-classes", "argv": [sys.executable,
                 str(tool_dir() / "_outer_classes_worker.py"), "--manifest", str(manifest)]})
    manifest = run_dir / "encapsulated-only-files.json"
    write_json(manifest, sorted(files["production"] + files["tests"]), root)
    jobs.append({"name": "encapsulated-only", "tool": "encapsulated-only", "argv": [sys.executable,
                  str(tool_dir() / "_encapsulated_only_worker.py"), "--manifest", str(manifest)]})
    if constructor_purity:
        manifest = run_dir / "constructor-purity-files.json"
        write_json(manifest, sorted(path for profile in ("production", "tests")
                                    for path in files[profile] if path.endswith(".py")), root)
        jobs.append({"name": "constructor-purity", "tool": "constructor-purity",
                     "argv": [sys.executable, str(tool_dir() / "_constructor_purity_worker.py"),
                              "--manifest", str(manifest)]})
    jobs.append({"name": "pyright", "tool": "pyright", "argv": [
        sys.executable, "-m", "pyright", "--project", str(root / "pyrightconfig.json"),
        "--pythonpath", sys.executable, "--outputjson"]})
    doc_roots = ["src"] + (["tests"] if (root / "tests").is_dir() else [])
    jobs.append({"name": "pydoclint", "tool": "pydoclint", "argv": [binary("pydoclint"),
                 "--config", str(root / "pyproject.toml"), "--quiet",
                 "--show-filenames-in-every-violation-message=True", *doc_roots]})
    if architecture_ready:
        for name, command in (("tach", "check"), ("tach-external", "check-external")):
            jobs.append({"name": name, "tool": name,
                         "argv": [sys.executable, str(tool_dir() / "_tach_worker.py"), command]})
    return jobs


def execute_job(job: dict[str, Any], root: Path, run_dir: Path, timeout: int,
                 capture_fn: Callable[..., dict[str, Any]] = capture) -> dict[str, Any]:
    return CapturedCheckJob().execute(job, root, run_dir, timeout, capture_fn=capture_fn)


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
                marker = read_json(root / ".eryx/architecture.ready.json")
                if not isinstance(marker, dict) or not marker.get("accepted"):
                    raise EryxError("ARCHITECTURE_NOT_INITIALIZED",
                                    "Architecture configuration has not been accepted.")
                ConfiguredArchitectureDraft(root).validate()
                architecture_ready = True
            except EryxError as exc:
                code = ("ARCHITECTURE_NOT_INITIALIZED"
                        if not (root / ".eryx/architecture.ready.json").exists() else exc.code)
                message = (str(exc) + " Run eryx/init_architecture.py, review tach.toml, "
                           + "then run it with --accept.")
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
                                    "error": "ARCHITECTURE_NOT_INITIALIZED", "raw_output": {}})
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
