"""Build and execute analyzer jobs over the discovered consumer scope."""
from __future__ import annotations

from collections.abc import Callable
from importlib import metadata
import os
from pathlib import Path
import shlex
import sys
from typing import Any

from quard._common import QuardError, capture, read_json, tool_dir, write_json
from quard._reports import diagnostic
from quard.check_job.captured_check_job import CapturedCheckJob
from quard.clone_engine_utils import EXPECTED
from quard.check_selection_utils import architecture_defaults
from quard.duplication_utils import read_duplication_min_lines


def inactive_job(name: str, status: str, error: str | None = None) -> dict[str, Any]:
    """Record an architecture job that never started.

    Args:
        name (str): Job and tool name.
        status (str): Skipped, blocked or error status.
        error (str | None, default=None): Reason when the job could not run.

    Returns:
        dict[str, Any]: Report entry without an execution result.
    """
    return {"name": name, "tool": name, "status": status, "exit_code": None,
            "duration_seconds": 0, "error": error, "raw_output": {}}


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
    if os.environ.get("QUARD_CLONE_ENGINE") == "rust":
        try:
            actual = metadata.version("quard-clones")
        except metadata.PackageNotFoundError:
            actual = None
        versions["quard-clones"] = actual
        if actual != EXPECTED["quard-clones"]:
            problems.append(diagnostic("system", "TOOL_VERSION_MISMATCH",
                                       f"quard-clones: expected {EXPECTED['quard-clones']}, "
                                       f"found {actual or 'not installed'}.",
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
        raise QuardError("INVALID_LCOM_FILES", f"Invalid --lcom_files quoting: {exc}") from exc
    if not paths:
        raise QuardError("INVALID_LCOM_FILES",
                        "--lcom_files must contain .py paths or be exactly empty to skip LCOM4.")
    selected: list[str] = []
    for raw in paths:
        path = Path(raw)
        if (path.is_absolute() or ".." in path.parts or path.suffix != ".py"
                or path.as_posix() not in available):
            raise QuardError("INVALID_LCOM_FILES",
                            f"Invalid or out-of-scope --lcom_files path: {raw!r}; "
                            + "expected an existing, non-excluded, non-symlinked .py file "
                            + "under src/ or tests/.")
        selected.append(path.as_posix())
    return list(dict.fromkeys(selected))


def make_jobs(root: Path, run_dir: Path, files: dict[str, list[str]], architecture_ready: bool,
              lcom_files: list[str] | None = None, constructor_purity: bool = False,
              test_directory_names: list[str] | None = None,
              checks: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    # Pylint opens --output before the runner writes captured stdout/stderr logs.
    # Create the directory before any parallel analyzer can start.
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    jobs: list[dict[str, Any]] = []
    enabled = architecture_defaults(root) if checks is None else checks

    def add_worker(key: str, name: str, scope: object) -> None:
        if not enabled[key]:
            return
        manifest = run_dir / f"{name}-files.json"
        write_json(manifest, scope, root)
        jobs.append({"name": name, "tool": name, "argv": [sys.executable,
                     str(tool_dir() / f"_{name.replace('-', '_')}_worker.py"),
                     "--manifest", str(manifest)]})

    for profile in ("production", "tests"):
        python_files = [path for path in files[profile] if path.endswith(".py")]
        if not python_files:
            continue
        manifest = run_dir / f"pylint-{profile}-files.json"
        output = run_dir / "raw" / f"pylint-{profile}.json"
        write_json(manifest, python_files, root)
        config = root / ("pyproject.toml" if profile == "production" else ".quard/pylint-tests.toml")
        argv = [sys.executable, str(tool_dir() / "_pylint_worker.py"),
                "--manifest", str(manifest), "--config", str(config), "--output", str(output)]
        if (root / ".quard/architecture.generated.json").exists():
            argv.append("--generated")
        jobs.append({"name": f"pylint-{profile}", "tool": "pylint", "profile": profile,
                     "argv": argv, "json_file": output})
    source_files = select_lcom_files(files, None) if lcom_files is None else lcom_files
    if lcom_files is None or source_files:
        add_worker("lcom4", "lcom4", source_files)
    if enabled["duplicate_detection"]:
        min_lines = read_duplication_min_lines(root)
        clone_files = sorted(path for profile in ("production", "tests") for path in files[profile]
                              if path.endswith(".py"))
        eligible = [path for path in clone_files if len((root / path).read_text(
            encoding="utf-8").splitlines()) >= min_lines]
        # An explicit empty config prevents consumer-owned jscpd settings from changing scope.
        config = run_dir / "jscpd-config.json"
        write_json(config, {}, root)
        # Normalization changes jscpd's exact-clone search on larger scopes. Keep the
        # original native exact pass independent so it cannot lose Type 1 findings.
        for name, kind, flags in (("jscpd", "exact", []),
                                  ("jscpd-renamed", "renamed",
                                   ["--ignore-identifiers", "--ignore-literals"]),
                                  ("jscpd-similar", "similar", ["--max-gap-lines", "2"])):
            output_dir = run_dir / "raw" if name == "jscpd" else run_dir / "raw" / name
            output_dir.mkdir(parents=True, exist_ok=True)
            argv = [binary("jscpd"), "--config", str(config), "--no-gitignore",
                    "--absolute", "--kind", kind, *flags, "--mode", "strict",
                    "--min-lines", str(min_lines), "--min-tokens", "1", "--fail-on-empty",
                    "--reporters", "json", "--output", str(output_dir), "--exit-code",
                    "1", *clone_files]
            engine = name != "jscpd-similar" and os.environ.get("QUARD_CLONE_ENGINE") == "rust"
            if engine:
                manifest = run_dir / "clone-engine-files.json"
                write_json(manifest, {"paths": eligible, "min_lines": min_lines}, root)
                argv = [sys.executable, str(tool_dir() / "_clone_engine_worker.py"),
                        "--manifest", str(manifest), "--output",
                        str(output_dir / "jscpd-report.json"), "--kind", kind]
            jobs.append({"name": name, "tool": "jscpd", "expected_paths": eligible,
                         "engine": engine, "kind": kind,
                         "json_file": output_dir / "jscpd-report.json", "argv": argv})
    full_scope = sorted(files["production"] + files["tests"])
    for key, name in (("method_decorators", "method-decorators"),
                      ("instance_fields", "instance-fields"),
                      ("class_contracts", "class-contracts")):
        add_worker(key, name, full_scope)
    if (architecture_ready and enabled["dependency_types"]
            and (root / ".quard/architecture.generated.json").is_file()):
        manifest = run_dir / "dependency-types-files.json"
        write_json(manifest, sorted(files["production"] + files["tests"]), root)
        jobs.append({"name": "dependency-types", "tool": "dependency-types", "argv": [sys.executable,
                     str(tool_dir() / "_dependency_types_worker.py"), "--manifest", str(manifest)]})
    add_worker("contract_folders", "contract-folders", full_scope)
    add_worker("test_placement", "test-placement", {
        "production": files["production"], "tests": files["tests"],
        "test_directory_names": test_directory_names or ["tests"]})
    for key, name in (("outer_classes", "outer-classes"),
                      ("free_function_placement", "free-function-placement"),
                      ("encapsulated_only", "encapsulated-only")):
        add_worker(key, name, full_scope)
    if constructor_purity:
        manifest = run_dir / "constructor-purity-files.json"
        write_json(manifest, sorted(path for profile in ("production", "tests")
                                    for path in files[profile] if path.endswith(".py")), root)
        jobs.append({"name": "constructor-purity", "tool": "constructor-purity",
                     "argv": [sys.executable, str(tool_dir() / "_constructor_purity_worker.py"),
                              "--manifest", str(manifest)]})
    if enabled["pyright"]:
        jobs.append({"name": "pyright", "tool": "pyright", "argv": [
            sys.executable, "-m", "pyright", "--project", str(root / "pyrightconfig.json"),
            "--pythonpath", sys.executable, "--outputjson"]})
    if enabled["pydoclint"]:
        doc_roots = ["src"] + (["tests"] if (root / "tests").is_dir() else [])
        jobs.append({"name": "pydoclint", "tool": "pydoclint", "argv": [binary("pydoclint"),
                     "--config", str(root / "pyproject.toml"), "--quiet",
                     "--show-filenames-in-every-violation-message=True", *doc_roots]})
    if architecture_ready and enabled["tach"]:
        for name, command in (("tach", "check"), ("tach-external", "check-external")):
            jobs.append({"name": name, "tool": name,
                         "argv": [sys.executable, str(tool_dir() / "_tach_worker.py"), command]})
    return jobs


def execute_job(job: dict[str, Any], root: Path, run_dir: Path, timeout: int,
                capture_fn: Callable[..., dict[str, Any]] = capture) -> dict[str, Any]:
    return CapturedCheckJob().execute(job, root, run_dir, timeout, capture_fn=capture_fn)
