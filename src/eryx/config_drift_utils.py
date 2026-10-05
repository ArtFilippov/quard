"""Detect unapplied consumer limits and scope in native configurations."""
from __future__ import annotations

from pathlib import Path

from eryx._common import load_toml, read_json
from eryx.policy_utils import (LIMIT_MAP, discover_files, exclude_globs, exclude_regex,
                               pyright_config, read_limits, read_scope)


def limit_drift(root: Path) -> list[str]:
    limits = read_limits(root)
    production = load_toml(root / "pyproject.toml").get("tool", {}).get("pylint", {})
    tests = load_toml(root / ".eryx/pylint-tests.toml").get("tool", {}).get("pylint", {})
    mismatches: list[str] = []
    for profile, actual in (("production", production), ("tests", tests)):
        for key, (section, option) in LIMIT_MAP.items():
            if actual.get(section, {}).get(option) != limits[profile][key]:
                mismatches.append(f"{profile}.{key}")
    return mismatches


def scope_drift(root: Path) -> list[str]:
    scope = read_scope(root)
    files = discover_files(root, scope)
    tool = load_toml(root / "pyproject.toml").get("tool", {})
    mismatch = [f"{profile}.main.ignore-paths" for profile, config in
                (("production", tool.get("pylint", {})),
                 ("tests", load_toml(root / ".eryx/pylint-tests.toml")
                  .get("tool", {}).get("pylint", {})))
                if config.get("main", {}).get("ignore-paths") != [exclude_regex(scope)]]
    expected = pyright_config(scope, files)
    for name in ("exclude", "ignore", "include", "executionEnvironments"):
        if tool.get("pyright", {}).get(name) != expected[name]:
            mismatch.append(f"pyright.{name}")
    native = read_json(root / "pyrightconfig.json")
    for name in ("exclude", "ignore", "include", "executionEnvironments"):
        if native.get(name) != expected[name]:
            mismatch.append(f"pyrightconfig.json.{name}")
    if tool.get("pydoclint", {}).get("exclude") != exclude_regex(scope):
        mismatch.append("pydoclint.exclude")
    tach = root / "tach.toml"
    if tach.exists() and not (root / ".eryx/architecture.generated.json").exists():
        exclusions = (exclude_globs(scope) + ["tests/**"]
                      + [f"**/{name}/**" for name in scope["test_directory_names"]])
        if load_toml(tach).get("exclude") != exclusions:
            mismatch.append("tach.exclude")
    return mismatch
