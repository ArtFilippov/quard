"""Build and execute analyzer jobs over the discovered consumer scope."""
from __future__ import annotations

from collections.abc import Callable
from importlib import metadata
from pathlib import Path
import shlex
import sys
from typing import Any

from eryx._common import EryxError, capture, read_json, tool_dir, write_json
from eryx._reports import diagnostic
from eryx.check_job.captured_check_job import CapturedCheckJob


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
    clone_files = sorted(path for profile in ("production", "tests") for path in files[profile]
                         if path.endswith(".py"))
    eligible = [path for path in clone_files if len((root / path).read_text(
        encoding="utf-8").splitlines()) >= 5]
    # An explicit empty config prevents consumer-owned jscpd settings from changing scope.
    config = run_dir / "jscpd-config.json"
    write_json(config, {}, root)
    output = run_dir / "raw" / "jscpd-report.json"
    jobs.append({"name": "jscpd", "tool": "jscpd", "expected_paths": eligible,
                 "json_file": output,
                 "argv": [binary("jscpd"), "--config", str(config), "--no-gitignore", "--absolute",
                          "--kind", "exact", "--mode", "strict", "--min-lines", "5",
                          "--min-tokens", "1", "--fail-on-empty", "--reporters", "json", "--output",
                          str(run_dir / "raw"), "--exit-code", "1", *clone_files]})
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
    if (root / ".eryx/architecture.generated.json").is_file():
        manifest = run_dir / "dependency-types-files.json"
        write_json(manifest, sorted(files["production"] + files["tests"]), root)
        jobs.append({"name": "dependency-types", "tool": "dependency-types", "argv": [sys.executable,
                     str(tool_dir() / "_dependency_types_worker.py"), "--manifest", str(manifest)]})
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
    manifest = run_dir / "free-function-placement-files.json"
    write_json(manifest, sorted(files["production"] + files["tests"]), root)
    jobs.append({"name": "free-function-placement", "tool": "free-function-placement",
                 "argv": [sys.executable, str(tool_dir() / "_free_function_placement_worker.py"),
                          "--manifest", str(manifest)]})
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
