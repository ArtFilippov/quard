"""Consumer-selected check groups and safe working-directory boundaries."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
from typing import Any

from _common import EryxError, load_toml, tool_dir

ANONYMITY_DEPENDENCIES = frozenset({"pylint", "nbstripout", "tomlkit", "packaging"})
IGNORED_DIRECTORIES = frozenset({".git", ".eryx", ".venv", "venv", "__pycache__",
                                 ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
                                 ".nox", "node_modules", "build", "dist"})
NOTEBOOK_PYLINT_CONFIG = ('[tool.pylint.main]\nload-plugins = ["_notebook_checker"]\n'
                          '[tool.pylint.messages_control]\ndisable = ["all"]\n'
                          'enable = ["unstripped-notebook", "notebook-verification-failed"]\n')


def read_checks(root: Path) -> dict[str, Any]:
    path = root / ".eryx/checks.toml"
    if path.is_symlink():
        raise EryxError("INVALID_CHECKS", "checks.toml must not be a symlink.")
    if not path.exists():
        return {"groups": ["codestyle"], "working_directories": ["."], "legacy": True}
    data = load_toml(path)
    if set(data) != {"groups", "working_directories"}:
        raise EryxError("INVALID_CHECKS", "checks.toml requires groups and working_directories only.")
    groups = data["groups"]
    dirs = data["working_directories"]
    if (not isinstance(groups, list) or not groups or
            any(type(group) is not str or group not in {"codestyle", "anonymity"} for group in groups) or
            len(set(groups)) != len(groups)):
        raise EryxError("INVALID_CHECKS", "groups must list distinct codestyle and/or anonymity values.")
    if not isinstance(dirs, list) or not dirs or any(type(item) is not str for item in dirs):
        raise EryxError("INVALID_CHECKS", "working_directories must be a nonempty list of relative directories.")
    selected = []
    for item in dirs:
        parts = PurePosixPath(item).parts
        if (not item or item.startswith("/") or "\\" in item or
                (item != "." and (any(part in {".", ".."} for part in item.split("/")) or
                                  not re.fullmatch(r"[A-Za-z0-9_. /-]+", item) or not parts))):
            raise EryxError("INVALID_CHECKS", f"Invalid working directory: {item!r}")
        target = root
        for part in parts:
            target /= part
            if target.is_symlink():
                raise EryxError("INVALID_CHECKS", f"Symlink working directory: {item!r}")
        if not target.is_dir() or target.resolve() == tool_dir():
            raise EryxError("INVALID_CHECKS", f"Working directory does not exist: {item!r}")
        selected.append(PurePosixPath(*parts).as_posix())
    if len(set(selected)) != len(selected):
        raise EryxError("INVALID_CHECKS", "Duplicate working directories.")
    if "codestyle" in groups and not {"src", "."}.intersection(selected):
        raise EryxError("INVALID_CHECKS", "codestyle requires src or . in working_directories.")
    return {"groups": groups, "working_directories": selected, "legacy": False}


def in_working_directories(path: str, directories: list[str]) -> bool:
    return any(directory == "." or path.startswith(directory + "/") for directory in directories)


def selected_test_roots(root: Path, checks: dict[str, Any]) -> list[str]:
    """Select root test directories without widening a nested selection."""
    if not (root / "tests").is_dir():
        return []
    directories = checks["working_directories"]
    if checks["legacy"] or "." in directories or "tests" in directories:
        return ["tests"]
    roots: list[str] = []
    for directory in sorted(d for d in directories if d.startswith("tests/")):
        if not any(directory.startswith(parent + "/") for parent in roots):
            roots.append(directory)
    return roots


def covered_files(root: Path, checks: dict[str, Any], excluded_names: list[str]) -> list[str]:
    """List effective input files, de-duplicating overlapping roots and skipping tool/cache paths."""
    excluded = set(excluded_names) | IGNORED_DIRECTORIES
    paths: set[str] = set()
    for name in checks["working_directories"]:
        directory = root / name
        if any(part in excluded for part in directory.relative_to(root).parts):
            continue
        for parent, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = [part for part in sorted(dirs) if part not in excluded and
                       not (Path(parent) / part).is_symlink() and
                       (Path(parent) / part).resolve() != tool_dir()]
            for filename in files:
                path = Path(parent) / filename
                if path.is_file() and not path.is_symlink():
                    paths.add(path.relative_to(root).as_posix())
    return sorted(paths)
