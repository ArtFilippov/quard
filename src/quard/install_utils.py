#!/usr/bin/env python3
"""Install Quard's pinned tools and consumer-owned configuration transactionally."""
from __future__ import annotations

import argparse
import contextlib
from pathlib import Path
import subprocess
import sys
sys.dont_write_bytecode = True
from typing import Any, cast

import tomlkit
from packaging.specifiers import SpecifierSet, InvalidSpecifier

from quard._common import (QuardError, atomic_write, capture, emit_event, error_exit,
                       load_toml, read_json, repo_lock, require_runtime, project_dir,
                       safe_target, tool_dir, write_json)
from quard.installation.transactional_installation import TransactionalInstallation
from quard.check_selection_utils import DEPENDENCY_TYPES, architecture_defaults, validate_checks
from quard.architecture_draft.generated_architecture_utils import select
from quard.policy_utils import validate_limits, validate_scope, validate_settings

__all__ = ["capture", "require_runtime", "write_json"]

OLD_SETTINGS = ("readability.toml", "scope.toml", "checks.toml")
STATE_FILES = ["config.toml", *OLD_SETTINGS, "architecture.ready.json",
               "architecture.generated.json",
               "pylint-catalog.json", "pylint-tests.toml", "policy-notes.json", "installation.json"]


def dependency_specs(tool: Path) -> list[str]:
    manifest = read_json(tool / "tools.json")
    specs = []
    for name, version in manifest["dependencies"].items():
        extras = manifest.get("extras", {}).get(name)
        specs.append(name + ("[" + ",".join(extras) + "]" if extras else "") + "==" + version)
    if manifest.get("native_source") or manifest.get("native_wheel"):
        specs.append("quard-clones==" + manifest["native_components"]["quard-clones"])
    return specs


def clone_source(root: Path, tool: Path) -> dict[str, str] | None:
    """Pin uv to the selected tool revision, including its locked Rust crate graph."""
    if not read_json(tool / "tools.json").get("native_source"):
        return None

    def git(cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                                text=True, check=False, timeout=30)
        if result.returncode:
            raise QuardError("CLONE_SOURCE_UNAVAILABLE", f"Cannot resolve Quard Git source: {result.stderr.strip()}")
        return result.stdout.strip()

    revision = git(tool, "rev-parse", "HEAD")
    if git(tool, "status", "--porcelain", "--untracked-files=all", "--",
           "rust/quard-clones"):
        raise QuardError("CLONE_SOURCE_DIRTY",
                         "Commit the Rust component and Cargo.lock before installing from source.")
    origin = git(tool, "remote", "get-url", "origin")
    if not origin:
        raise QuardError("CLONE_SOURCE_UNAVAILABLE", "Quard needs a reachable origin to pin its Rust source.")
    if Path(origin).is_absolute():
        origin = Path(origin).as_uri()
    elif origin.startswith("git@") and ":" in origin:
        host, repo = origin.split(":", 1)
        origin = f"ssh://{host}/{repo}"
    if tool != root and tool.is_relative_to(root):
        relative = tool.relative_to(root).as_posix()
        entry = git(root, "ls-files", "--stage", "--", relative)
        if not entry.startswith(f"160000 {revision} 0\t{relative}"):
            raise QuardError("CLONE_GITLINK_MISMATCH",
                             "The selected Quard revision must match the consumer's staged gitlink.")
    return {"git": origin, "rev": revision, "subdirectory": "rust/quard-clones"}


def initialize_state(root: Path, tool: Path) -> None:
    # Legacy quality/ is read only if the current .quard state has not been initialized.
    for name in STATE_FILES:
        if name == "config.toml" or (name in OLD_SETTINGS and
                                     (root / ".quard/config.toml").exists()):
            continue
        destination = root / ".quard" / name
        if destination.exists():
            continue
        legacy = root / "quality" / name
        default = tool / "defaults" / name
        source = (legacy if legacy.is_file() else
                  None if name in OLD_SETTINGS else default)
        if source is not None and source.is_file():
            content = source.read_text(encoding="utf-8")
            if name == "scope.toml":
                content = content.replace('".quality"', '".quard"')
            if name == "readability.toml":
                content = content.replace("quality/apply.py", "quard/apply.py")
            atomic_write(destination, content, root)
    _ = select(root)
    migrate_settings(root, tool)


def migrate_settings(root: Path, tool: Path) -> None:
    destination = root / ".quard/config.toml"
    if not destination.exists():
        document = tomlkit.parse((tool / "defaults/config.toml").read_text(encoding="utf-8"))
        cast(Any, document["checks"])[DEPENDENCY_TYPES] = architecture_defaults(root)[
            DEPENDENCY_TYPES]
        for name, section in (("readability.toml", "readability"),
                              ("scope.toml", "scope"), ("checks.toml", "checks")):
            old = root / ".quard" / name
            if old.exists():
                data = load_toml(old)
                if name == "readability.toml":
                    validate_limits(data)
                elif name == "scope.toml":
                    validate_scope(data)
                else:
                    _ = validate_checks(data)
                document[section] = tomlkit.parse(old.read_text(encoding="utf-8"))
        atomic_write(destination, tomlkit.dumps(document), root)
    validate_settings(root)
    # Upgrade old unified and legacy configurations without replacing explicit choices.
    document = tomlkit.parse(destination.read_text(encoding="utf-8"))
    existing = load_toml(destination)["checks"]
    for key, default in architecture_defaults(root).items():
        if key not in existing:
            cast(Any, document["checks"])[key] = default
    atomic_write(destination, tomlkit.dumps(document), root)
    for name in OLD_SETTINGS:
        old = root / ".quard" / name
        safe_target(old, root)
        old.unlink(missing_ok=True)


def validate_project(root: Path) -> dict[str, Any]:
    if not root.is_dir() or not (root / "pyproject.toml").is_file():
        raise QuardError("NOT_A_PROJECT", "Run from a uv repository with pyproject.toml, or pass --project /path/to/repository.")
    safe_target(root / "pyproject.toml", root)
    project = load_toml(root / "pyproject.toml")
    if not isinstance(project.get("project"), dict) or not project["project"].get("name"):
        raise QuardError("UNSUPPORTED_PROJECT", "A named [project] table is required.")
    if "workspace" in project.get("tool", {}).get("uv", {}):
        raise QuardError("WORKSPACES_UNSUPPORTED", "This installer supports one project, not a uv workspace.")
    if not (root / "src").is_dir() or (root / "src").is_symlink():
        raise QuardError("SRC_LAYOUT_REQUIRED", "Expected a real src/ directory.")
    spec_text = project["project"].get("requires-python", ">=3.12")
    try:
        spec = SpecifierSet(spec_text)
    except InvalidSpecifier as exc:
        raise QuardError("INVALID_PYTHON_REQUIREMENT", str(exc)) from exc
    if not any(spec.contains(f"3.{minor}.0") for minor in range(12, 20)):
        raise QuardError("UNSUPPORTED_PYTHON", f"Project requirement {spec_text!r} has no supported Python >=3.12.")
    return project


def prepare_project(root: Path, source: dict[str, str] | None = None) -> None:
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
    if source is not None:
        doc["tool"]["uv"].setdefault("sources", {})["quard-clones"] = source
    atomic_write(root / "pyproject.toml", tomlkit.dumps(doc), root)


def snapshot(root: Path, names: list[str]) -> dict[str, tuple[bytes, int] | None]:
    saved = {}
    for name in names:
        path = root / name
        safe_target(path, root)
        if path.exists() and not path.is_file():
            raise QuardError("INVALID_MANAGED_TARGET", f"Expected a file: {name}")
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
    from quard import install as entry
    return TransactionalInstallation(entry).install(
        root, tool, dry_run=dry_run, progress=progress, extras=extras)


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
