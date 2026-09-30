#!/usr/bin/env python3
"""Install Eryx's pinned tools and consumer-owned configuration transactionally."""
from __future__ import annotations

import argparse
import contextlib
from pathlib import Path
import shutil
import sys
sys.dont_write_bytecode = True
from typing import Any

import tomlkit
from packaging.specifiers import SpecifierSet, InvalidSpecifier

from eryx._common import (EryxError, atomic_write, capture, emit_event, error_exit,
                     load_toml, read_json, repo_lock, require_runtime, project_dir,
                     safe_target, tool_dir, write_json)

STATE_FILES = ["readability.toml", "scope.toml", "architecture.ready.json",
               "pylint-catalog.json", "pylint-tests.toml", "policy-notes.json", "installation.json"]


def dependency_specs(tool: Path) -> list[str]:
    manifest = read_json(tool / "tools.json")
    specs = []
    for name, version in manifest["dependencies"].items():
        extras = manifest.get("extras", {}).get(name)
        specs.append(name + ("[" + ",".join(extras) + "]" if extras else "") + "==" + version)
    return specs


def initialize_state(root: Path, tool: Path) -> None:
    # Legacy files are read only for migration; removal is a consumer Git change.
    # Existing .eryx files always win, even if installation.json is absent.
    for name in STATE_FILES:
        destination = root / ".eryx" / name
        if destination.exists():
            continue
        legacy = root / "quality" / name
        default = tool / "defaults" / name
        source = legacy if legacy.is_file() else default
        if source.is_file():
            content = source.read_text(encoding="utf-8")
            if name == "scope.toml":
                content = content.replace('".quality"', '".eryx"')
            if name == "readability.toml":
                content = content.replace("quality/apply.py", "eryx/apply.py")
            atomic_write(destination, content, root)


def validate_project(root: Path) -> dict[str, Any]:
    if not root.is_dir() or not (root / "pyproject.toml").is_file():
        raise EryxError("NOT_A_PROJECT", "Run from a uv repository with pyproject.toml, or pass --project /path/to/repository.")
    safe_target(root / "pyproject.toml", root)
    project = load_toml(root / "pyproject.toml")
    if not isinstance(project.get("project"), dict) or not project["project"].get("name"):
        raise EryxError("UNSUPPORTED_PROJECT", "A named [project] table is required.")
    if "workspace" in project.get("tool", {}).get("uv", {}):
        raise EryxError("WORKSPACES_UNSUPPORTED", "This installer supports one project, not a uv workspace.")
    if not (root / "src").is_dir() or (root / "src").is_symlink():
        raise EryxError("SRC_LAYOUT_REQUIRED", "Expected a real src/ directory.")
    spec_text = project["project"].get("requires-python", ">=3.12")
    try:
        spec = SpecifierSet(spec_text)
    except InvalidSpecifier as exc:
        raise EryxError("INVALID_PYTHON_REQUIREMENT", str(exc)) from exc
    if not any(spec.contains(f"3.{minor}.0") for minor in range(12, 20)):
        raise EryxError("UNSUPPORTED_PYTHON", f"Project requirement {spec_text!r} has no supported Python >=3.12.")
    return project


def prepare_project(root: Path) -> None:
    doc = tomlkit.parse((root / "pyproject.toml").read_text(encoding="utf-8"))
    # Preserve runtime Python support and upper bounds. Only the dev group requires a modern interpreter.
    project = doc["project"]
    if "requires-python" not in project:
        project["requires-python"] = ">=3.12"
    doc.setdefault("dependency-groups", {}).setdefault("dev", [])
    groups = doc.setdefault("tool", {}).setdefault("uv", {}).setdefault("dependency-groups", {})
    group = groups.setdefault("dev", {})
    current = group.get("requires-python", "")
    group["requires-python"] = (current + ",>=3.12") if current and ">=3.12" not in current else (current or ">=3.12")
    atomic_write(root / "pyproject.toml", tomlkit.dumps(doc), root)


def snapshot(root: Path, names: list[str]) -> dict[str, tuple[bytes, int] | None]:
    saved = {}
    for name in names:
        path = root / name
        safe_target(path, root)
        if path.exists() and not path.is_file():
            raise EryxError("INVALID_MANAGED_TARGET", f"Expected a file: {name}")
        saved[name] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
    return saved


def rollback(root: Path, saved: dict[str, tuple[bytes, int] | None]) -> list[str]:
    errors = []
    for name, previous in saved.items():
        path = root / name
        try:
            safe_target(path, root)
            if previous is None:
                if path.exists():
                    path.unlink()
            else:
                atomic_write(path, previous[0], root, previous[1])
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return errors


def install(root: Path, tool: Path, *, dry_run: bool = False, progress: bool = True,
            extras: list[str] | None = None) -> dict[str, Any]:
    require_runtime()
    validate_project(root)
    uv = shutil.which("uv")
    if not uv:
        raise EryxError("UV_NOT_FOUND", "uv must already be installed and available on PATH.")
    managed = sorted([f".eryx/{name}" for name in STATE_FILES] +
                     ["pyproject.toml", "pyrightconfig.json", "uv.lock", "tach.toml", ".gitignore"])
    saved = snapshot(root, managed)
    specs = dependency_specs(tool)
    previous_path = root / ".eryx/installation.json"
    previous = read_json(previous_path) if previous_path.exists() else {}
    selected_extras = sorted(set(extras if extras is not None else previous.get("extras", [])))
    available = load_toml(root / "pyproject.toml")["project"].get("optional-dependencies", {})
    if any(extra not in available for extra in selected_extras):
        raise EryxError("UNKNOWN_EXTRA", f"Unknown project extra in {selected_extras}")
    extra_args = [arg for extra in selected_extras for arg in ("--extra", extra)]
    plan = {"project": str(root), "dependencies": specs, "extras": selected_extras, "managed_files": managed,
            "architecture_initialization": "separate; no source layout is changed"}
    if dry_run:
        return {"status": "planned", **plan, "exit_code": 0}
    safe_target(root / ".eryx/logs/install-probes", root)
    safe_target(root / ".eryx/logs/install.log", root)
    log_parts = []
    try:
        if progress:
            print("Installing Eryx configuration and exact dev dependencies...", file=sys.stderr)
        initialize_state(root, tool)
        from eryx._policy import discover_files, read_scope
        if not discover_files(root, read_scope(root))["production"]:
            raise EryxError("EMPTY_SOURCE_SCOPE", "No Python files under src/.")
        prepare_project(root)
        commands = [
            [uv, "add", "--dev", "--no-sync", "--python", ">=3.12", *specs],
            [uv, "sync", "--locked", "--group", "dev", "--python", ">=3.12", *extra_args],
            [uv, "run", "--locked", "--no-sync", "--group", "dev", *extra_args,
             "python", str(tool / "_audit.py"), "--project", str(root), "--json"],
        ]
        for index, argv in enumerate(commands, 1):
            if progress:
                print(f"Step {index}/3: {('resolve dependencies', 'sync project environment', 'probe native tools and generate configuration')[index - 1]}", file=sys.stderr)
            result = capture(argv, root, timeout=1200)
            log_parts.append("$ " + " ".join(argv) + "\n" + result["stdout"] + "\n" + result["stderr"])
            atomic_write(root / ".eryx/logs/install.log", "\n\n".join(log_parts), root)
            if result["error"] or result["exit_code"] != 0:
                raise EryxError(result["error"] or "INSTALL_STEP_FAILED",
                    f"Step {index} failed (exit {result['exit_code']}). See .eryx/logs/install.log and .eryx/logs/install-probes/.\n" +
                    (result["stdout"] + "\n" + result["stderr"])[-8000:])
        ignore_path = root / ".gitignore"
        old_ignore = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
        additions = [line for line in ("/.eryx/reports/", "/.eryx/logs/", "/.eryx/cache/", "/.tach/") if line not in old_ignore.splitlines()]
        if additions:
            atomic_write(ignore_path, old_ignore.rstrip("\n") + ("\n" if old_ignore else "") + "\n# Local Eryx reports, logs and native cache\n" + "\n".join(additions) + "\n", root)
        audit_result = read_json(root / ".eryx/logs/install-probes/result.json")
        marker = root / ".eryx/architecture.ready.json"
        installation = {"eryx_version": read_json(tool / "tools.json")["eryx_version"], "extras": selected_extras,
                        "dependencies": specs, "source_layout": "src", "mode": "strict",
                        "architecture_initialization_required": not (marker.exists() and read_json(marker).get("accepted"))}
        write_json(root / ".eryx/installation.json", installation, root)
        modified = [name for name, old in saved.items() if ((root / name).read_bytes() if (root / name).exists() else None) != (old[0] if old else None)]
        return {"status": "installed", "project": str(root), "modified_files": sorted(modified),
                "versions": audit_result["versions"], "smoke_tests": audit_result["smoke_tests"],
                "architecture_initialization_required": installation["architecture_initialization_required"],
                "instructions": str(tool / "README.md"), "agent_instructions": str(tool / "AGENTS.md"),
                "next_command": "uv run --locked --group dev python eryx/init_architecture.py", "exit_code": 0}
    except BaseException as exc:
        errors = rollback(root, saved)
        error_data = {"status": "error", "code": getattr(exc, "code", "INSTALL_FAILED"), "message": str(exc),
                      "managed_files_rolled_back": not errors, "rollback_errors": errors,
                      "environment_note": "The virtual environment/cache may have changed. Re-sync the restored project with uv sync.", "exit_code": 2}
        try:
            write_json(root / ".eryx/logs/install-error.json", error_data, root)
        except Exception:
            pass
        if errors:
            raise EryxError("ROLLBACK_INCOMPLETE", str(exc) + "; rollback failures: " + "; ".join(errors)) from exc
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", help="Emit one machine-readable event on stdout")
    parser.add_argument("--dry-run", action="store_true", help="Validate project and list planned changes without installing or writing")
    parser.add_argument("--extra", action="append", default=None, help="Retain a project optional dependency group (repeatable; defaults to previous selection)")
    args = parser.parse_args()
    try:
        root = project_dir(args.project)
        with contextlib.nullcontext() if args.dry_run else repo_lock(root):
            data = install(root, tool_dir(), dry_run=args.dry_run, progress=not args.json, extras=args.extra)
        emit_event(data.pop("status"), json_output=args.json, **data)
        return 0
    except Exception as exc:
        return error_exit(exc, args.json)


if __name__ == "__main__":
    sys.exit(main())
