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
import sys
sys.dont_write_bytecode = True
import time
import uuid
from typing import Any, Callable

from _common import (SCHEMA, EryxError, atomic_write, capture, read_json,
                     repo_lock, require_runtime, project_dir, tool_dir, tool_revision, safe_target, write_json)
from _checks import (ANONYMITY_DEPENDENCIES, PRESIDIO_DEPENDENCIES, ANONYMITY_PYLINT_CONFIG, covered_files,
                     in_working_directories, read_checks, selected_test_roots)
from _policy import discover_files, discover_notebooks, limit_drift, pyright_config, read_scope
from _reports import diagnostic, normalize, normalize_presidio, sorted_diagnostics
from init_architecture import validate_architecture_config


def installed_versions(root: Path) -> tuple[dict[str, str | None], list[dict[str, Any]]]:
    expected = read_json(tool_dir() / "tools.json")["dependencies"]
    groups = read_checks(root)["groups"]
    expected = {key: value for key, value in expected.items()
                if ("codestyle" in groups or key in ANONYMITY_DEPENDENCIES) and
                ("anonymity" in groups or key not in PRESIDIO_DEPENDENCIES)}
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


def make_jobs(root: Path, run_dir: Path, files: dict[str, Any], architecture_ready: bool,
              groups: list[str] | None = None) -> list[dict[str, Any]]:
    # Pylint opens --output before the runner writes captured stdout/stderr logs.
    # Create the directory before any parallel analyzer can start.
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    groups = groups or ["codestyle"]
    jobs = []
    if "codestyle" not in groups:
        # A minimal, generated config makes the anonymity checkers independent of
        # consumer Pylint/architecture settings. The empty input is never claimed as coverage.
        dummy = run_dir / "anonymity-input.py"
        atomic_write(dummy, "", root)
        config = run_dir / "anonymity-pylint.toml"
        atomic_write(config, ANONYMITY_PYLINT_CONFIG, root)
        manifest = run_dir / "pylint-anonymity-files.json"
        secret_manifest = run_dir / "pylint-secrets.json"
        secret_coverage = run_dir / "raw/secret-coverage.json"
        notebook_manifest = run_dir / "pylint-notebooks.json"
        output = run_dir / "raw/pylint-anonymity.json"
        write_json(manifest, [str(dummy)], root)
        write_json(secret_manifest, files["secret_inputs"], root)
        write_json(notebook_manifest, files["notebooks"], root)
        jobs.append({"name": "pylint-anonymity", "tool": "pylint", "profile": "anonymity",
                     "argv": [sys.executable, str(tool_dir() / "_pylint_worker.py"), "--manifest", str(manifest),
                              "--config", str(config), "--output", str(output), "--notebooks", str(notebook_manifest),
                              "--secrets", str(secret_manifest), "--secret-coverage", str(secret_coverage),
                              "--notebook-only"],
                     "json_file": output, "secret_coverage": secret_coverage,
                     "secret_inputs": files["secret_inputs"]})
    if "codestyle" in groups:
        jobs.extend(make_codestyle_jobs(root, run_dir, files, architecture_ready, groups))
    if "anonymity" in groups:
        manifest = run_dir / "presidio-files.json"
        output = run_dir / "raw/presidio.json"
        write_json(manifest, files["presidio_inputs"], root)
        jobs.append({"name": "presidio", "tool": "presidio", "argv":
                     [sys.executable, str(tool_dir() / "_presidio_worker.py"),
                      "--manifest", str(manifest), "--output", str(output)],
                     "json_file": output, "presidio_inputs": files["presidio_inputs"]})
    return jobs


def make_codestyle_jobs(root: Path, run_dir: Path, files: dict[str, Any],
                        architecture_ready: bool, groups: list[str]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for profile in ("production", "tests"):
        python_files = [path for path in files[profile] if path.endswith(".py")]
        if not python_files:
            continue
        manifest = run_dir / f"pylint-{profile}-files.json"
        output = run_dir / "raw" / f"pylint-{profile}.json"
        write_json(manifest, python_files, root)
        config = root / ("pyproject.toml" if profile == "production" else ".eryx/pylint-tests.toml")
        notebook_args = []
        secret_coverage = run_dir / "raw/secret-coverage.json"
        if profile == "production":
            notebook_manifest = run_dir / "pylint-notebooks.json"
            write_json(notebook_manifest, files.get("notebooks", []), root)
            notebook_args = ["--notebooks", str(notebook_manifest)]
            if "anonymity" in groups:
                secret_manifest = run_dir / "pylint-secrets.json"
                write_json(secret_manifest, files["secret_inputs"], root)
                notebook_args += ["--secrets", str(secret_manifest), "--secret-coverage", str(secret_coverage)]
        jobs.append({"name": f"pylint-{profile}", "tool": "pylint", "profile": profile,
                     "argv": [sys.executable, str(tool_dir() / "_pylint_worker.py"),
                              "--manifest", str(manifest), "--config", str(config), "--output", str(output),
                              *notebook_args],
                     "json_file": output,
                     **({"secret_coverage": secret_coverage, "secret_inputs": files["secret_inputs"]}
                        if profile == "production" and "anonymity" in groups else {})})
    jobs.append({"name": "pyright", "tool": "pyright", "argv": [sys.executable, "-m", "pyright",
                 "--project", str(root / "pyrightconfig.json"), "--pythonpath", sys.executable, "--outputjson"]})
    doc_roots = files.get("doc_roots", ["src"] + (["tests"] if (root / "tests").is_dir() else []))
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
    if "secret_coverage" in job or "presidio_inputs" in job:
        # Tool/third-party exceptions can contain source text. Only structured
        # diagnostics and coverage from the worker are safe to persist.
        result["stdout"] = "[Secret-enabled worker output withheld.]" if result.get("stdout") else ""
        result["stderr"] = "[Secret-enabled worker output withheld.]" if result.get("stderr") else ""
    logs = {}
    for stream in ("stdout", "stderr"):
        path = run_dir / "raw" / f"{job['name']}.{stream}.log"
        atomic_write(path, result.get(stream, ""), root)
        logs[stream] = path.relative_to(root).as_posix()
    normalized = (normalize_presidio(result, root, job["json_file"], job["presidio_inputs"])
                  if "presidio_inputs" in job else normalize(job["tool"], result, root, job.get("json_file")))
    secret_coverage = {"scanned": [], "skipped": []}
    if "secret_coverage" in job:
        if (result.get("exit_code") is None or
                (result.get("exit_code") == 0 and normalized["diagnostics"]) or
                result.get("stdout") or result.get("stderr")):
            normalized["status"] = "error"
            normalized["error"] = "SECRET_SCAN_WORKER_ERROR"
            normalized["diagnostics"].append(diagnostic("pylint", "SECRET_SCAN_WORKER_ERROR",
                "Secret-enabled worker returned unexpected output or exit status.",
                is_infrastructure_error=True))
        try:
            data = read_json(job["secret_coverage"])
            expected = set(job["secret_inputs"])
            if (not isinstance(data, dict) or set(data) != {"scanned", "skipped"} or
                    any(not isinstance(data[key], list) or
                        any(type(path) is not str for path in data[key]) or
                        len(data[key]) != len(set(data[key])) for key in ("scanned", "skipped")) or
                    set(data["scanned"]) & set(data["skipped"]) or
                    not set(data["scanned"] + data["skipped"]) <= expected):
                raise ValueError("Invalid secret coverage")
            secret_coverage = data
            if set(data["scanned"] + data["skipped"]) != expected:
                raise ValueError("Incomplete secret coverage")
        except (OSError, ValueError, EryxError):
            normalized["status"] = "error"
            normalized["error"] = "SECRET_SCAN_INCOMPLETE"
            normalized["diagnostics"].append(diagnostic("pylint", "SECRET_SCAN_INCOMPLETE",
                "Secret scanner did not confirm every selected input; inspect the tool status and file paths.",
                is_infrastructure_error=True))
    presidio_coverage = normalized.pop("coverage", None)
    for entry in normalized["diagnostics"]:
        entry["job"] = job["name"]
        if "profile" in job:
            entry["profile"] = job["profile"]
    if job.get("json_file") and job["json_file"].exists():
        logs["native_json"] = job["json_file"].relative_to(root).as_posix()
    return {"name": job["name"], "tool": job["tool"], "status": normalized["status"],
            "exit_code": result["exit_code"], "duration_seconds": result["duration_seconds"],
            "argv": job["argv"], "raw_output": logs, "diagnostics": normalized["diagnostics"],
            "error": normalized["error"], **({"secret_coverage": secret_coverage} if "secret_coverage" in job else {}),
            **({"presidio_coverage": presidio_coverage} if "presidio_inputs" in job else {})}


def run_checks(root: Path, *, serial: bool = False, timeout: int = 600,
               capture_fn: Callable[..., dict[str, Any]] = capture,
               versions_fn: Callable[..., Any] = installed_versions) -> dict[str, Any]:
    start = time.monotonic()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = root / ".eryx/reports" / run_id
    safe_target(run_dir, root)
    run_dir.mkdir(parents=True, exist_ok=False)
    diagnostics = []
    versions: dict[str, str | None] = {}
    files: dict[str, Any] = {"production": [], "tests": [], "excluded_directories": [],
                             "notebooks": [], "notebook_count": 0, "groups": [], "working_directories": [],
                             "secret_inputs": [], "secret_scanned": [], "secret_count": 0,
                             "secret_skipped": [], "secret_skipped_count": 0,
                             "presidio_inputs": [], "presidio_scanned": [], "presidio_count": 0,
                             "presidio_skipped": [], "presidio_skipped_count": 0,
                             "presidio_unconfirmed": []}
    architecture_ready = False
    eryx_version = "unknown"
    results = []
    try:
        require_runtime()
        checks = read_checks(root)
        files["groups"] = checks["groups"]
        files["working_directories"] = checks["working_directories"]
        scope = read_scope(root)
        effective = covered_files(root, checks, scope["exclude_directory_names"])
        if not effective:
            raise EryxError("EMPTY_CHECK_SCOPE", "No files in selected working directories after exclusions.")
        files["secret_inputs"] = effective if "anonymity" in checks["groups"] else []
        files["presidio_inputs"] = effective if "anonymity" in checks["groups"] else []
        files["presidio_unconfirmed"] = files["presidio_inputs"].copy()
        eryx_version = read_json(tool_dir() / "tools.json").get("eryx_version", "unknown")
        versions, problems = versions_fn(root)
        diagnostics.extend(problems)
        if "codestyle" in checks["groups"]:
            files.update(discover_files(root, scope))
            if not checks["legacy"]:
                files["tests"] = [path for path in files["tests"]
                                  if in_working_directories(path, checks["working_directories"])]
                files["doc_roots"] = ["src", *selected_test_roots(root, checks)]
        notebooks, excluded = discover_notebooks(root, scope)
        if not checks["legacy"]:
            notebooks = sorted(set(notebooks) & set(effective))
        files["notebooks"] = notebooks
        files["notebook_count"] = len(notebooks)
        files["excluded_directories"] = sorted(set(files["excluded_directories"] + excluded))
        if not checks["legacy"]:
            files["excluded_directories"] = [path for path in files["excluded_directories"]
                                              if in_working_directories(path, checks["working_directories"])]
        if "codestyle" in checks["groups"] and not files["production"]:
            raise EryxError("EMPTY_SOURCE_SCOPE", "No Python source files found under src/. Refusing an empty successful check.")
        if "codestyle" in checks["groups"] and not checks["legacy"]:
            expected = pyright_config(scope, files, selected_test_roots(root, checks))["include"]
            if read_json(root / "pyrightconfig.json").get("include") != expected:
                raise EryxError("SCOPE_NOT_APPLIED", "Selected code-style directories differ from Pyright's include list; run eryx/apply.py.")
        if "codestyle" in checks["groups"]:
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
        jobs = make_jobs(root, run_dir, files, architecture_ready, checks["groups"])
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
                               "diagnostics": [diagnostic(job["tool"], "REPORT_WRITE_ERROR",
                                   "Presidio job failed to write its report." if job["tool"] == "presidio" else str(exc),
                                   is_infrastructure_error=True)]}
                diagnostics.extend(result.pop("diagnostics"))
                if "secret_coverage" in result:
                    coverage = result.pop("secret_coverage")
                    files["secret_scanned"] = coverage["scanned"]
                    files["secret_count"] = len(coverage["scanned"])
                    files["secret_skipped"] = coverage["skipped"]
                    files["secret_skipped_count"] = len(coverage["skipped"])
                if "presidio_coverage" in result:
                    coverage = result.pop("presidio_coverage")
                    files["presidio_scanned"] = coverage["scanned"]
                    files["presidio_count"] = len(coverage["scanned"])
                    files["presidio_skipped"] = coverage["skipped"]
                    files["presidio_skipped_count"] = len(coverage["skipped"])
                    files["presidio_unconfirmed"] = sorted(
                        set(files["presidio_inputs"]) - set(coverage["scanned"] + coverage["skipped"]))
                results.append(result)
        if "codestyle" in checks["groups"] and not architecture_ready:
            for name in ("tach", "tach-external"):
                results.append({"name": name, "tool": name, "status": "blocked", "exit_code": None,
                                "duration_seconds": 0, "error": "ARCHITECTURE_NOT_INITIALIZED", "raw_output": {}})
        if "anonymity" in checks["groups"] and not files["secret_count"]:
            diagnostics.append(diagnostic("system", "EMPTY_SECRET_SCAN_SCOPE",
                "No text files were scanned for secrets in the selected working directories.",
                is_infrastructure_error=True))
        if "anonymity" in checks["groups"] and not files["presidio_count"]:
            diagnostics.append(diagnostic("presidio", "EMPTY_PRESIDIO_SCAN_SCOPE",
                "No text files were confirmed scanned by Presidio.", is_infrastructure_error=True))
    except Exception as exc:
        diagnostics.append(diagnostic("system", getattr(exc, "code", "RUNNER_ERROR"), str(exc), is_infrastructure_error=True))
    diagnostics = sorted_diagnostics(diagnostics)
    has_error = any(item.get("is_infrastructure_error") for item in diagnostics) or any(result["status"] == "error" for result in results)
    code = 2 if has_error else (1 if diagnostics else 0)
    counts = Counter(item["tool"] for item in diagnostics)
    report = {"schema_version": SCHEMA, "eryx_version": eryx_version, "eryx": tool_revision(),
              "run_id": run_id, "status": {0: "passed", 1: "violations", 2: "error"}[code], "exit_code": code,
              "duration_seconds": round(time.monotonic() - start, 3), "python": sys.version.split()[0],
              "versions": versions, "coverage": files,
              "summary": {"diagnostics": len(diagnostics), "by_tool": dict(sorted(counts.items())),
                          "infrastructure_errors": sum(bool(d.get("is_infrastructure_error")) for d in diagnostics)},
              "tools": sorted(results, key=lambda item: item["name"]), "diagnostics": diagnostics,
              "report_path": (run_dir / "report.json").relative_to(root).as_posix(),
              "latest_report_path": ".eryx/reports/latest.json",
               "notes": ["Every reported diagnostic fails the check, including informational messages.",
                         "No source files are modified. No baseline or automatic fixes are applied.",
                         "Tach names come from native diagnostics or explicitly marked adapter categories; raw details are retained.",
                         "Pylint receives .py files; Pyright also checks discovered .pyi stubs."]
               + (["No notebooks discovered; notebook anonymity was not verified."] if not files["notebooks"] else [])
               + (["No text files scanned for secrets; secret anonymity was not verified."]
                  if "anonymity" in files["groups"] and not files["secret_count"] else [])}
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
    args = parser.parse_args()
    if args.timeout < 1 or args.max_diagnostics < 0:
        parser.error("timeout must be positive and max-diagnostics nonnegative")
    try:
        root = project_dir(args.project)
        with repo_lock(root):
            report = run_checks(root, serial=args.serial, timeout=args.timeout)
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
