#!/usr/bin/env python3
"""Run all configured checks, preserving native output and a unified JSON report."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from importlib import metadata
import json
import os
from pathlib import Path
import shlex
import sys
sys.dont_write_bytecode = True
import time
import uuid
from typing import Any, Callable

from eryx._common import (SCHEMA, EryxError, atomic_write, capture, read_json,
                     repo_lock, require_runtime, project_dir, tool_dir, tool_revision, safe_target, write_json)
from eryx._policy import discover_files, limit_drift, read_checks, read_scope
from eryx._reports import diagnostic, normalize, sorted_diagnostics
from eryx.init_architecture import validate_architecture_config


def installed_versions(root: Path) -> tuple[dict[str, str | None], list[dict[str, Any]]]:
    expected = read_json(tool_dir() / "tools.json")["dependencies"]
    versions: dict[str, str | None] = {}
    problems = []
    for name, required in expected.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = None
        versions[name] = actual
        if actual != required:
            problems.append(diagnostic("system", "TOOL_VERSION_MISMATCH",
                f"{name}: expected {required}, found {actual or 'not installed'}. Run the installer or uv sync --locked --group dev.",
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
        raise EryxError("INVALID_LCOM_FILES", "--lcom_files must contain .py paths or be exactly empty to skip LCOM4.")
    selected = []
    for raw in paths:
        path = Path(raw)
        if (path.is_absolute() or ".." in path.parts or path.suffix != ".py"
                or path.as_posix() not in available):
            raise EryxError("INVALID_LCOM_FILES",
                            f"Invalid or out-of-scope --lcom_files path: {raw!r}; expected an existing, "
                            "non-excluded, non-symlinked .py file under src/ or tests/.")
        selected.append(path.as_posix())
    return list(dict.fromkeys(selected))


def make_jobs(root: Path, run_dir: Path, files: dict[str, list[str]], architecture_ready: bool,
              lcom_files: list[str] | None = None, constructor_purity: bool = False) -> list[dict[str, Any]]:
    # Pylint opens --output before the runner writes captured stdout/stderr logs.
    # Create the directory before any parallel analyzer can start.
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    jobs = []
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
                              "--manifest", str(manifest), "--config", str(config), "--output", str(output)],
                      "json_file": output})
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
    manifest = run_dir / "encapsulated-only-files.json"
    write_json(manifest, sorted(files["production"] + files["tests"]), root)
    jobs.append({"name": "encapsulated-only", "tool": "encapsulated-only", "argv": [sys.executable,
                  str(tool_dir() / "_encapsulated_only_worker.py"), "--manifest", str(manifest)]})
    if constructor_purity:
        manifest = run_dir / "constructor-purity-files.json"
        write_json(manifest, sorted(path for profile in ("production", "tests")
                                    for path in files[profile] if path.endswith(".py")), root)
        jobs.append({"name": "constructor-purity", "tool": "constructor-purity", "argv": [sys.executable,
                     str(tool_dir() / "_constructor_purity_worker.py"), "--manifest", str(manifest)]})
    jobs.append({"name": "pyright", "tool": "pyright", "argv": [sys.executable, "-m", "pyright",
                 "--project", str(root / "pyrightconfig.json"), "--pythonpath", sys.executable, "--outputjson"]})
    doc_roots = ["src"] + (["tests"] if (root / "tests").is_dir() else [])
    jobs.append({"name": "pydoclint", "tool": "pydoclint", "argv": [binary("pydoclint"),
                 "--config", str(root / "pyproject.toml"), "--quiet",
                 "--show-filenames-in-every-violation-message=True", *doc_roots]})
    if architecture_ready:
        for name, command in (("tach", "check"), ("tach-external", "check-external")):
            jobs.append({"name": name, "tool": name, "argv": [sys.executable, str(tool_dir() / "_tach_worker.py"), command]})
    return jobs


def execute_job(job: dict[str, Any], root: Path, run_dir: Path, timeout: int,
                capture_fn: Callable[..., dict[str, Any]] = capture) -> dict[str, Any]:
    try:
        result = capture_fn(job["argv"], root, timeout)
    except Exception as exc:
        result = {"argv": job["argv"], "exit_code": None, "stdout": "", "stderr": str(exc),
                  "duration_seconds": 0, "error": "RUNNER_SUBPROCESS_ERROR"}
    logs = {}
    for stream in ("stdout", "stderr"):
        path = run_dir / "raw" / f"{job['name']}.{stream}.log"
        atomic_write(path, result.get(stream, ""), root)
        logs[stream] = path.relative_to(root).as_posix()
    normalized = normalize(job["tool"], result, root, job.get("json_file"))
    for entry in normalized["diagnostics"]:
        entry["job"] = job["name"]
        if "profile" in job:
            entry["profile"] = job["profile"]
    if job.get("json_file") and job["json_file"].exists():
        logs["native_json"] = job["json_file"].relative_to(root).as_posix()
    return {"name": job["name"], "tool": job["tool"], "status": normalized["status"],
            "exit_code": result["exit_code"], "duration_seconds": result["duration_seconds"],
            "argv": job["argv"], "raw_output": logs, "diagnostics": normalized["diagnostics"],
            "error": normalized["error"]}


def run_checks(root: Path, *, serial: bool = False, timeout: int = 600,
               lcom_files: str | None = None,
               capture_fn: Callable[..., dict[str, Any]] = capture,
               versions_fn: Callable[..., Any] = installed_versions) -> dict[str, Any]:
    start = time.monotonic()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = root / ".eryx/reports" / run_id
    safe_target(run_dir, root)
    run_dir.mkdir(parents=True, exist_ok=False)
    diagnostics = []
    versions: dict[str, str | None] = {}
    files: dict[str, list[str]] = {"production": [], "tests": [], "excluded_directories": []}
    lcom_coverage: dict[str, Any] = {"mode": "full" if lcom_files is None else "selected",
                                     "requested": lcom_files, "selected_files": [], "analyzed_files": []}
    architecture_ready = False
    eryx_version = "unknown"
    results = []
    try:
        require_runtime()
        eryx_version = read_json(tool_dir() / "tools.json").get("eryx_version", "unknown")
        versions, problems = versions_fn(root)
        diagnostics.extend(problems)
        files = discover_files(root, read_scope(root))
        if not files["production"]:
            raise EryxError("EMPTY_SOURCE_SCOPE", "No Python source files found under src/. Refusing an empty successful check.")
        try:
            constructor_purity = read_checks(root)
            if not constructor_purity:
                results.append({"name": "constructor-purity", "tool": "constructor-purity", "status": "skipped",
                                "exit_code": None, "duration_seconds": 0, "error": None, "raw_output": {}})
        except EryxError as exc:
            constructor_purity = False
            diagnostics.append(diagnostic("constructor-purity", exc.code, str(exc), is_infrastructure_error=True))
            results.append({"name": "constructor-purity", "tool": "constructor-purity", "status": "error",
                            "exit_code": None, "duration_seconds": 0, "error": exc.code, "raw_output": {}})
        try:
            selected = select_lcom_files(files, lcom_files)
            lcom_coverage["mode"] = ("full" if lcom_files is None else
                                     "skipped" if lcom_files == "" else "selected")
            lcom_coverage["selected_files"] = selected
            if lcom_files == "":
                results.append({"name": "lcom4", "tool": "lcom4", "status": "skipped", "exit_code": None,
                                "duration_seconds": 0, "error": None, "raw_output": {}})
        except EryxError as exc:
            selected = []
            lcom_coverage["mode"] = "invalid"
            diagnostics.append(diagnostic("lcom4", exc.code, str(exc), is_infrastructure_error=True))
            results.append({"name": "lcom4", "tool": "lcom4", "status": "error", "exit_code": None,
                            "duration_seconds": 0, "error": exc.code, "raw_output": {}})
        try:
            mismatch = limit_drift(root)
            if mismatch:
                raise EryxError("READABILITY_NOT_APPLIED", ", ".join(mismatch) + "; run uv run --locked --group dev python eryx/apply.py.")
        except EryxError as exc:
            diagnostics.append(diagnostic("system", exc.code, str(exc), is_infrastructure_error=True))
        try:
            marker = read_json(root / ".eryx/architecture.ready.json")
            if not isinstance(marker, dict) or not marker.get("accepted"):
                raise EryxError("ARCHITECTURE_NOT_INITIALIZED", "Architecture configuration has not been accepted.")
            validate_architecture_config(root)
            architecture_ready = True
        except EryxError as exc:
            diagnostics.append(diagnostic("system", "ARCHITECTURE_NOT_INITIALIZED" if not (root / ".eryx/architecture.ready.json").exists() else exc.code,
                str(exc) + " Run eryx/init_architecture.py, review tach.toml, then run it with --accept.", is_infrastructure_error=True))
        jobs = make_jobs(root, run_dir, files, architecture_ready,
                         None if lcom_files is None else selected, constructor_purity)
        workers = 1 if serial else min(4, max(1, os.cpu_count() or 1), len(jobs))
        # Native analyzers complete independently. A failing job never cancels the others.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(execute_job, job, root, run_dir, timeout, capture_fn): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"name": job["name"], "tool": job["tool"], "status": "error", "exit_code": None,
                              "duration_seconds": 0, "error": "REPORT_WRITE_ERROR", "raw_output": {},
                              "diagnostics": [diagnostic(job["tool"], "REPORT_WRITE_ERROR", str(exc), is_infrastructure_error=True)]}
                diagnostics.extend(result.pop("diagnostics"))
                results.append(result)
                if job["name"] == "lcom4" and result["status"] in {"passed", "violations"}:
                    lcom_coverage["analyzed_files"] = selected
        if not architecture_ready:
            for name in ("tach", "tach-external"):
                results.append({"name": name, "tool": name, "status": "blocked", "exit_code": None,
                                "duration_seconds": 0, "error": "ARCHITECTURE_NOT_INITIALIZED", "raw_output": {}})
    except Exception as exc:
        diagnostics.append(diagnostic("system", getattr(exc, "code", "RUNNER_ERROR"), str(exc), is_infrastructure_error=True))
    diagnostics = sorted_diagnostics(diagnostics)
    has_error = any(item.get("is_infrastructure_error") for item in diagnostics) or any(result["status"] == "error" for result in results)
    code = 2 if has_error else (1 if diagnostics else 0)
    counts = Counter(item["tool"] for item in diagnostics)
    report = {"schema_version": SCHEMA, "eryx_version": eryx_version, "eryx": tool_revision(),
              "run_id": run_id, "status": {0: "passed", 1: "violations", 2: "error"}[code], "exit_code": code,
              "duration_seconds": round(time.monotonic() - start, 3), "python": sys.version.split()[0],
              "versions": versions, "coverage": {**files, "lcom4": lcom_coverage},
              "summary": {"diagnostics": len(diagnostics), "by_tool": dict(sorted(counts.items())),
                          "infrastructure_errors": sum(bool(d.get("is_infrastructure_error")) for d in diagnostics)},
              "tools": sorted(results, key=lambda item: item["name"]), "diagnostics": diagnostics,
              "report_path": (run_dir / "report.json").relative_to(root).as_posix(),
              "latest_report_path": ".eryx/reports/latest.json",
              "notes": ["Every reported diagnostic fails the check, including informational messages.",
                        "No source files are modified. No baseline or automatic fixes are applied.",
                        "Tach names come from native diagnostics or explicitly marked adapter categories; raw details are retained.",
                        "Pylint receives .py files; Pyright also checks discovered .pyi stubs."]}
    write_json(run_dir / "report.json", report, root)
    write_json(root / ".eryx/reports/latest.json", report, root)
    return report


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


if __name__ == "__main__":
    sys.exit(main())
