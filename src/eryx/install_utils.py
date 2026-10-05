#!/usr/bin/env python3
"""Install Eryx's pinned tools and consumer-owned configuration transactionally."""
from __future__ import annotations

import argparse
import contextlib
from pathlib import Path
import sys
sys.dont_write_bytecode = True
from typing import Any

import tomlkit
from packaging.specifiers import SpecifierSet, InvalidSpecifier

from eryx._common import (EryxError, atomic_write, capture, emit_event, error_exit,
                       load_toml, read_json, repo_lock, require_runtime, project_dir,
                       safe_target, tool_dir, write_json)
from eryx.installation.transactional_installation import TransactionalInstallation
from eryx.policy_utils import validate_checks, validate_limits, validate_scope, validate_settings

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
    return specs


def initialize_state(root: Path, tool: Path) -> None:
    # Legacy quality/ is read only if the current .eryx state has not been initialized.
    for name in STATE_FILES:
        if name == "config.toml" or (name in OLD_SETTINGS and
                                     (root / ".eryx/config.toml").exists()):
            continue
        destination = root / ".eryx" / name
        if destination.exists():
            continue
        legacy = root / "quality" / name
        default = tool / "defaults" / name
        source = (legacy if legacy.is_file() else
                  None if name in OLD_SETTINGS else default)
        if source is not None and source.is_file():
            content = source.read_text(encoding="utf-8")
            if name == "scope.toml":
                content = content.replace('".quality"', '".eryx"')
            if name == "readability.toml":
                content = content.replace("quality/apply.py", "eryx/apply.py")
            atomic_write(destination, content, root)
    migrate_settings(root, tool)


def migrate_settings(root: Path, tool: Path) -> None:
    destination = root / ".eryx/config.toml"
    if not destination.exists():
        document = tomlkit.parse((tool / "defaults/config.toml").read_text(encoding="utf-8"))
        for name, section in (("readability.toml", "readability"),
                              ("scope.toml", "scope"), ("checks.toml", "checks")):
            old = root / ".eryx" / name
            if old.exists():
                data = load_toml(old)
                if name == "readability.toml":
                    validate_limits(data)
                elif name == "scope.toml":
                    validate_scope(data)
                else:
                    validate_checks(data)
                document[section] = tomlkit.parse(old.read_text(encoding="utf-8"))
        atomic_write(destination, tomlkit.dumps(document), root)
    validate_settings(root)
    for name in OLD_SETTINGS:
        old = root / ".eryx" / name
        safe_target(old, root)
        old.unlink(missing_ok=True)


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
    from eryx import install as entry
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
