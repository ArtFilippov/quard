"""Validate consumer settings and render native configurations without adding rules."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Any, cast

import tomlkit

from quard._common import QuardError, atomic_write, load_toml, read_json, safe_target
from quard.check_selection_utils import (DEPENDENCY_TYPES, architecture_defaults,
                                         validate_architecture_checks, validate_checks)
from quard.duplication_utils import validate_duplication
from quard.architecture_draft.generated_selection_utils import generated
from quard.pylint_config_utils import (LEGACY_PLUGIN, official_plugins,
                                      clean_config as render_clean_config,
                                      policy_notes as render_policy_notes)

LIMIT_MAP = {
    "max_line_length": ("format", "max-line-length"),
    "max_module_lines": ("format", "max-module-lines"),
    "max_statements": ("design", "max-statements"),
    "max_complexity": ("design", "max-complexity"),
    "max_branches": ("design", "max-branches"),
    "max_nested_blocks": ("refactoring", "max-nested-blocks"),
    "max_locals": ("design", "max-locals"),
    "max_returns": ("design", "max-returns"),
    "max_bool_expr": ("design", "max-bool-expr"),
    "max_args": ("design", "max-args"),
    "max_positional_arguments": ("design", "max-positional-arguments"),
    "max_attributes": ("design", "max-attributes"),
    "max_public_methods": ("design", "max-public-methods"),
    "max_parents": ("design", "max-parents"),
    "min_public_methods": ("design", "min-public-methods"),
    "min_similarity_lines": ("similarities", "min-similarity-lines"),
    "max_try_statements": ("broad_try_clause", "max-try-statements"),
}
PRODUCTION_DISABLED = {
    "missing-module-docstring": "User policy: docstrings are optional.",
    "missing-class-docstring": "User policy: docstrings are optional.",
    "missing-function-docstring": "User policy: docstrings are optional.",
    "reimported": "Pyright reportDuplicateImport=error.",
    "undefined-variable": "Pyright reportUndefinedVariable=error.",
    "used-before-assignment": "Pyright reportUnboundVariable=error.",
    "possibly-used-before-assignment": "Pyright reportPossiblyUnboundVariable=error.",
}
TEST_DISABLED = {
    "too-few-public-methods": "Test containers and fakes may expose one operation.",
    "no-self-use": "Test frameworks call instance methods without instance state.",
    "redefined-outer-name": "pytest fixture names are intentionally reused in test arguments.",
    "unused-argument": "Fixtures can be requested only for their side effects.",
    "protected-access": "White-box tests may access protected implementation details.",
    "magic-value-comparison": "Concrete expected values make assertions readable.",
    "duplicate-code": "Repeated arrange/assert sections need not become shared abstractions.",
}
# Plugin 1.2.2: keep fragile-test-mocks W9101 and anti-bypass W9501.
# Filter through the installed catalog before use to avoid unknown message IDs.
TEST_ARCH_DISABLED = [
    "W9001", "W9003", "W9004", "W9005", "W9006", "W9007", "W9009",
    "W9010", "W9011", "W9012", "W9013", "W9015", "W9102", "W9201", "W9202", "W9301", "W9601",
]
PYRIGHT_EXTRA = [
    "reportCallInDefaultInitializer", "reportImplicitOverride", "reportImplicitStringConcatenation",
    "reportMissingSuperCall", "reportPropertyTypeMismatch", "reportUninitializedInstanceVariable",
    "reportUnnecessaryTypeIgnoreComment", "reportUnreachable", "reportUnusedCallResult",
    "reportMissingModuleSource",
]
LAYERS = ["Bootstrap", "Interface", "Infrastructure", "UseCase", "Domain"]
# Generic framework bases; consumers keep additional policy in native config.
IGNORED_FRAMEWORK_PARENTS = [
    "builtins.BaseException", "builtins.Exception", "builtins.RuntimeError",
    "builtins.OSError", "builtins.TimeoutError", "typing.Protocol",
    "typing_extensions.Protocol", "enum.Enum", "enum.StrEnum",
]
LAYER_DIRS = {
    "domain": "Domain", "application": "UseCase", "use_cases": "UseCase",
    "usecase": "UseCase", "usecases": "UseCase", "infrastructure": "Infrastructure",
    "adapters": "Infrastructure", "interfaces": "Interface", "interface": "Interface",
    "bootstrap": "Bootstrap", "composition": "Bootstrap", "main": "Bootstrap",
}
ALLOWED_LAYERS = {
    "Bootstrap": set(LAYERS), "Interface": {"Interface", "UseCase", "Domain"},
    "Infrastructure": {"Infrastructure", "Domain"}, "UseCase": {"UseCase", "Domain"},
    "Domain": {"Domain"},
}


def read_settings(root: Path) -> dict[str, Any]:
    path = root / ".quard/config.toml"
    if not path.is_file():
        raise QuardError("CONFIG_MIGRATION_REQUIRED",
                        "Missing .quard/config.toml; run bash quard/install.sh --project . "
                        + "to migrate existing settings before checking or applying.")
    data = load_toml(path)
    if not {"checks", "readability", "scope"} <= set(data) or set(data) - {
            "checks", "readability", "scope", "duplication"}:
        raise QuardError("INVALID_CONFIG",
                        "config.toml expects [checks], [readability], [scope] and "
                        + "optional [duplication].")
    _ = validate_duplication(data.get("duplication", {}))
    return data


def validate_limits(data: object) -> dict[str, dict[str, int]]:
    if not isinstance(data, dict):
        raise QuardError("INVALID_LIMITS", "[readability] must be a TOML table.")
    data = cast(dict[str, Any], data)
    if set(data) != {"production", "tests"}:
        raise QuardError("INVALID_LIMITS",
                        "[readability] expects [readability.production] "
                        + "and [readability.tests] only.")
    production, tests = data["production"], data["tests"]
    if not isinstance(production, dict) or not isinstance(tests, dict):
        raise QuardError("INVALID_LIMITS", "Readability profiles must be TOML tables.")
    if set(production) != set(LIMIT_MAP) or set(tests) - set(LIMIT_MAP):
        raise QuardError("INVALID_LIMITS", f"Supported parameters: {', '.join(sorted(LIMIT_MAP))}")
    profiles = {"production": production, "tests": {**production, **tests}}
    for name, limits in profiles.items():
        for key, value in limits.items():
            lower = 0 if key == "min_public_methods" else 1
            if type(value) is not int or not lower <= value <= 1000000:
                raise QuardError("INVALID_LIMITS", f"{name}.{key} must be an integer >= {lower}.")
        if limits["max_positional_arguments"] > limits["max_args"]:
            raise QuardError("INVALID_LIMITS", f"{name}: positional arguments cannot exceed total arguments.")
        if limits["min_public_methods"] > limits["max_public_methods"]:
            raise QuardError("INVALID_LIMITS", f"{name}: minimum methods exceeds maximum.")
    return profiles


def read_limits(root: Path) -> dict[str, dict[str, int]]:
    return validate_limits(read_settings(root)["readability"])


def validate_scope(data: object) -> dict[str, list[str]]:
    if not isinstance(data, dict):
        raise QuardError("INVALID_SCOPE", "[scope] must be a TOML table.")
    data = cast(dict[str, Any], data)
    expected = {"exclude_directory_names", "test_directory_names"}
    if set(data) != expected:
        raise QuardError("INVALID_SCOPE", f"[scope] expects: {', '.join(sorted(expected))}")
    for key in expected:
        values = data[key]
        if not isinstance(values, list) or not values:
            raise QuardError("INVALID_SCOPE", f"{key} must be a nonempty array.")
        if any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", x)
               or x in {".", ".."} for x in values):
            raise QuardError("INVALID_SCOPE", f"{key} accepts directory basenames only.")
    return data


def read_scope(root: Path) -> dict[str, list[str]]:
    return validate_scope(read_settings(root)["scope"])


def read_checks(root: Path) -> dict[str, bool]:
    configured = read_settings(root)["checks"]
    checks = validate_checks(configured)
    if DEPENDENCY_TYPES not in configured:
        checks[DEPENDENCY_TYPES] = architecture_defaults(root)[DEPENDENCY_TYPES]
    return checks


def validate_settings(root: Path) -> None:
    data = read_settings(root)
    validate_limits(data["readability"])
    validate_scope(data["scope"])
    validate_architecture_checks(root, read_checks(root))


def exclude_globs(scope: dict[str, list[str]]) -> list[str]:
    return [pattern for name in scope["exclude_directory_names"]
            for pattern in (f"{name}/**", f"**/{name}/**")]


def exclude_regex(scope: dict[str, list[str]]) -> str:
    return r"(?:^|[/\\])(?:" + "|".join(map(re.escape, scope["exclude_directory_names"])) + r")(?:[/\\]|$)"


def discover_files(root: Path, scope: dict[str, list[str]]) -> dict[str, list[str]]:
    import os
    production: list[str] = []
    tests: list[str] = []
    excluded: list[str] = []
    excluded_dirs = set(scope["exclude_directory_names"])
    test_dirs = set(scope["test_directory_names"])
    for top in (root / "src", root / "tests"):
        if not top.is_dir():
            continue
        if top.is_symlink():
            raise QuardError("SYMLINK_SOURCE", f"Source root must not be a symlink: {top}")
        for parent, dirs, files in os.walk(top, followlinks=False):
            kept = []
            for dirname in sorted(dirs):
                path = Path(parent) / dirname
                if path.is_symlink():
                    raise QuardError("SYMLINK_SOURCE", f"Source directory symlinks are unsupported: {path}")
                if dirname in excluded_dirs:
                    excluded.append(path.relative_to(root).as_posix() + "/")
                else:
                    kept.append(dirname)
            dirs[:] = kept
            for name in sorted(files):
                path = Path(parent) / name
                if path.suffix not in {".py", ".pyi"}:
                    continue
                if path.is_symlink():
                    raise QuardError("SYMLINK_SOURCE", f"Source file symlinks are unsupported: {path}")
                rel = path.relative_to(root)
                if top.name == "tests" or any(part in test_dirs for part in rel.parts[:-1]):
                    tests.append(rel.as_posix())
                else:
                    production.append(rel.as_posix())
    return {"production": sorted(production), "tests": sorted(tests), "excluded_directories": sorted(excluded)}


def apply_limits(config: dict[str, Any], limits: dict[str, int]) -> None:
    for key, (section, option) in LIMIT_MAP.items():
        config.setdefault(section, {})[option] = limits[key]


def base_pylint(plugins: list[str], scope: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "main": {"load-plugins": plugins, "jobs": 1, "persistent": False,
                 "fail-under": 0, "fail-on": ["I"], "recursive": True,
                 "py-version": f"{sys.version_info.major}.{sys.version_info.minor}",
                 "ignore-paths": [exclude_regex(scope)]},
        "messages_control": {"enable": ["all"], "disable": list(PRODUCTION_DISABLED)},
        "reports": {"reports": False, "score": False, "output-format": "json2"},
        "format": {"indent-string": "    ", "indent-after-paren": 4,
                   "single-line-if-stmt": False, "single-line-class-stmt": False},
        "similarities": {"ignore-comments": True, "ignore-docstrings": True,
                          "ignore-imports": True, "ignore-signatures": True},
        "design": {"ignored-parents": IGNORED_FRAMEWORK_PARENTS.copy()},
    }


def pyright_config(scope: dict[str, list[str]], files: dict[str, list[str]]) -> dict[str, Any]:
    config: dict[str, Any] = {
        "include": ["src"] + (["tests"] if any(path.startswith("tests/") for path in files["tests"]) else []),
        "exclude": exclude_globs(scope), "ignore": exclude_globs(scope),
        "typeCheckingMode": "strict", "pythonPlatform": "Linux", "extraPaths": ["src"],
        "pythonVersion": f"{sys.version_info.major}.{sys.version_info.minor}",
        "analyzeUnannotatedFunctions": True, "enableReachabilityAnalysis": True,
        "strictListInference": True, "strictDictionaryInference": True, "strictSetInference": True,
        "deprecateTypingAliases": True, "enableExperimentalFeatures": False,
        "reportImportCycles": "none",
    }
    config.update({name: "error" for name in PYRIGHT_EXTRA})
    # Explicitly enable replacements for disabled Pylint rules, including tests.
    for name in ("reportDuplicateImport", "reportUndefinedVariable", "reportUnboundVariable", "reportPossiblyUnboundVariable"):
        config[name] = "error"
    test_roots: set[str] = set()
    for file in files["tests"]:
        parts = Path(file).parts
        for index, part in enumerate(parts[:-1]):
            if part in scope["test_directory_names"]:
                test_roots.add(Path(*parts[:index + 1]).as_posix())
                break
    # Tests still use strict types. These two demands obstruct pytest assertions/mocks.
    config["executionEnvironments"] = [
        {"root": path, "extraPaths": ["src"], "reportPrivateUsage": "none", "reportUnusedCallResult": "none"}
        for path in sorted(test_roots, key=lambda x: (-len(x), x))
    ] + [{"root": "src", "extraPaths": ["src"]}]
    return config


def pydoclint_config(scope: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "style": "google", "quiet": True, "exclude": exclude_regex(scope),
        "skip-checking-short-docstrings": False, "skip-checking-private-functions": False,
        "skip-checking-raises": False, "check-arg-order": True,
        "arg-type-hints-in-docstring": True, "arg-type-hints-in-signature": True,
        "check-return-types": True, "check-yield-types": True, "check-class-attributes": True,
        "should-document-private-class-attributes": True, "ignore-underscore-args": False,
        "ignore-private-args": False, "should-document-star-arguments": True,
        "check-style-mismatch": True, "check-arg-defaults": True,
        "allow-init-docstring": True, "require-inline-class-var-docs": False,
        "require-return-section-when-returning-nothing": False,
        "show-filenames-in-every-violation-message": True,
        "auto-regenerate-baseline": False,
    }


def clean_config() -> dict[str, Any]:
    return render_clean_config(LAYER_DIRS)


def test_pylint(base: dict[str, Any], limits: dict[str, int], catalog: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(base)
    apply_limits(config, limits)
    disabled = list(dict.fromkeys(config.get("messages_control", {}).get("disable", []) + list(TEST_DISABLED)))
    available_ids = set(catalog.get("message_ids", []))
    if LEGACY_PLUGIN in config.get("main", {}).get("load-plugins", []):
        disabled += [name for name in TEST_ARCH_DISABLED if name in available_ids]
    config.setdefault("messages_control", {})["disable"] = disabled
    return config


def policy_notes(plugins: list[str]) -> dict[str, Any]:
    return render_policy_notes(plugins, PRODUCTION_DISABLED, TEST_DISABLED, TEST_ARCH_DISABLED)


def configure(root: Path, *, initial: bool = False, plugins: list[str] | None = None,
              catalog: dict[str, Any] | None = None) -> list[str]:
    validate_settings(root)
    limits = read_limits(root)
    scope = read_scope(root)
    files = discover_files(root, scope)
    project_path = root / "pyproject.toml"
    try:
        doc = tomlkit.parse(project_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise QuardError("INVALID_PYPROJECT", str(exc)) from exc
    tool = doc.setdefault("tool", {})
    managed = generated(root)
    if managed and plugins is not None:
        plugins = official_plugins(plugins)
    if initial:
        if not plugins:
            raise QuardError("MISSING_PLUGINS", "Discover installed Pylint extensions before configuring.")
        # Initialize missing sections only. Reinstallation preserves native policy,
        # reviewed layers, stubPath and other project-specific settings.
        tool.setdefault("pylint", base_pylint(plugins, scope))
        tool.setdefault("pyright", pyright_config(scope, files))
        tool.setdefault("pydoclint", pydoclint_config(scope))
        if not managed:
            tool.setdefault("clean-arch", clean_config())
        tool["pylint"].setdefault("main", {})["load-plugins"] = plugins
        native_pyright = root / "pyrightconfig.json"
        if native_pyright.exists():
            for key, value in read_json(native_pyright).items():
                if tool["pyright"].get(key) != value:
                    tool["pyright"][key] = value
    elif "pylint" not in tool:
        raise QuardError("NOT_INSTALLED", "Missing [tool.pylint]; run the installer first.")
    if managed:
        # Reinstall/apply migrates previously generated five-layer Pylint configs.
        if not initial:
            native_catalog: dict[str, Any] = (catalog if catalog is not None else
                                              read_json(root / ".quard/pylint-catalog.json"))
            tool["pylint"].setdefault("main", {})["load-plugins"] = official_plugins(
                native_catalog["plugins"])
        removed_layer_map = tool.pop("clean-arch", None) is not None
    else:
        removed_layer_map = False
    apply_limits(tool["pylint"], limits["production"])
    # Scope is shared, while unrelated native settings are preserved on apply.
    tool["pylint"].setdefault("main", {})["ignore-paths"] = [exclude_regex(scope)]
    if "pyright" in tool:
        tool["pyright"]["exclude"] = exclude_globs(scope)
        tool["pyright"]["ignore"] = exclude_globs(scope)
        discovered = pyright_config(scope, files)
        tool["pyright"]["include"] = discovered["include"]
        if tool["pyright"].get("executionEnvironments") != discovered["executionEnvironments"]:
            tool["pyright"]["executionEnvironments"] = discovered["executionEnvironments"]
    if "pydoclint" in tool:
        tool["pydoclint"]["exclude"] = exclude_regex(scope)
    # Build and validate all target content before writing any file.
    if catalog is None:
        catalog = read_json(root / ".quard/pylint-catalog.json")
    rendered_project = tomlkit.dumps(doc)
    if removed_layer_map:
        rendered_project = rendered_project.rstrip("\n") + "\n"
    plain = tomllib.loads(rendered_project)["tool"]["pylint"]
    tests = {"tool": {"pylint": test_pylint(plain, limits["tests"], catalog)}}
    outputs: dict[str, str] = {
        "pyproject.toml": rendered_project,
        ".quard/pylint-tests.toml": "# Test-only profile. Regenerate with quard/apply.py.\n" + tomlkit.dumps(tests),
    }
    if "pyright" in tool:
        outputs["pyrightconfig.json"] = json.dumps(tomllib.loads(rendered_project)["tool"]["pyright"], ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tach_path = root / "tach.toml"
    if tach_path.exists() and not (root / ".quard/architecture.generated.json").exists():
        tach = tomlkit.parse(tach_path.read_text(encoding="utf-8"))
        tach["exclude"] = exclude_globs(scope) + ["tests/**"] + [f"**/{name}/**" for name in scope["test_directory_names"]]
        outputs["tach.toml"] = tomlkit.dumps(tach)
    if initial:
        outputs[".quard/policy-notes.json"] = json.dumps(policy_notes(plugins or []), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    previous = {}
    for name in outputs:
        path = root / name
        safe_target(path, root)
        previous[name] = path.read_bytes() if path.exists() else None
    changed = []
    try:
        for name, text in outputs.items():
            if atomic_write(root / name, text, root):
                changed.append(name)
    except BaseException:
        for name in changed:
            path = root / name
            if previous[name] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, previous[name], root)
        raise
    return changed
