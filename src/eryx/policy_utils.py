"""Validate consumer settings and render native configurations without adding rules."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Any

import tomlkit

from eryx._common import EryxError, atomic_write, load_toml, read_json, safe_target

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


def read_limits(root: Path) -> dict[str, dict[str, int]]:
    data = load_toml(root / ".eryx/readability.toml")
    if set(data) != {"production", "tests"}:
        raise EryxError("INVALID_LIMITS", "readability.toml must contain [production] and [tests] only.")
    production, tests = data["production"], data["tests"]
    if not isinstance(production, dict) or not isinstance(tests, dict):
        raise EryxError("INVALID_LIMITS", "Readability profiles must be TOML tables.")
    if set(production) != set(LIMIT_MAP) or set(tests) - set(LIMIT_MAP):
        raise EryxError("INVALID_LIMITS", f"Supported parameters: {', '.join(sorted(LIMIT_MAP))}")
    profiles = {"production": production, "tests": {**production, **tests}}
    for name, limits in profiles.items():
        for key, value in limits.items():
            lower = 0 if key == "min_public_methods" else 1
            if type(value) is not int or not lower <= value <= 1000000:
                raise EryxError("INVALID_LIMITS", f"{name}.{key} must be an integer >= {lower}.")
        if limits["max_positional_arguments"] > limits["max_args"]:
            raise EryxError("INVALID_LIMITS", f"{name}: positional arguments cannot exceed total arguments.")
        if limits["min_public_methods"] > limits["max_public_methods"]:
            raise EryxError("INVALID_LIMITS", f"{name}: minimum methods exceeds maximum.")
    return profiles


def read_scope(root: Path) -> dict[str, list[str]]:
    data = load_toml(root / ".eryx/scope.toml")
    expected = {"exclude_directory_names", "test_directory_names"}
    if set(data) != expected:
        raise EryxError("INVALID_SCOPE", f"scope.toml expects: {', '.join(sorted(expected))}")
    for key in expected:
        values = data[key]
        if not isinstance(values, list) or not values:
            raise EryxError("INVALID_SCOPE", f"{key} must be a nonempty array.")
        if any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", x)
               or x in {".", ".."} for x in values):
            raise EryxError("INVALID_SCOPE", f"{key} accepts directory basenames only.")
    return data


def read_checks(root: Path) -> bool:
    """Read the project-owned constructor opt-in; older installs default off."""
    path = root / ".eryx/checks.toml"
    if not path.exists():
        return False
    data = load_toml(path)
    if set(data) != {"constructor_purity"} or type(data["constructor_purity"]) is not bool:
        raise EryxError("INVALID_CHECKS", "checks.toml expects only constructor_purity = true or false.")
    return data["constructor_purity"]


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
            raise EryxError("SYMLINK_SOURCE", f"Source root must not be a symlink: {top}")
        for parent, dirs, files in os.walk(top, followlinks=False):
            kept = []
            for dirname in sorted(dirs):
                path = Path(parent) / dirname
                if path.is_symlink():
                    raise EryxError("SYMLINK_SOURCE", f"Source directory symlinks are unsupported: {path}")
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
                    raise EryxError("SYMLINK_SOURCE", f"Source file symlinks are unsupported: {path}")
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
    # Important: in the actual 1.2.2 source this is Layer -> patterns, not the
    # opposite direction shown by some README examples.
    mapping: dict[str, list[str]] = {}
    for name, layer in LAYER_DIRS.items():
        mapping.setdefault(layer, []).append(name)
    return {"project_type": "generic", "visibility_enforcement": True, "layer_map": mapping}


def test_pylint(base: dict[str, Any], limits: dict[str, int], catalog: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(base)
    apply_limits(config, limits)
    disabled = list(dict.fromkeys(config.get("messages_control", {}).get("disable", []) + list(TEST_DISABLED)))
    available_ids = set(catalog.get("message_ids", []))
    disabled += [name for name in TEST_ARCH_DISABLED if name in available_ids]
    config.setdefault("messages_control", {})["disable"] = disabled
    return config


def policy_notes(plugins: list[str]) -> dict[str, Any]:
    return {
        "production_disabled": PRODUCTION_DISABLED,
        "test_additional_disabled": TEST_DISABLED,
        "test_architecture_disabled_if_registered": TEST_ARCH_DISABLED,
        "pyright_disabled": {"reportImportCycles": "Pylint cyclic-import checks Python module cycles."},
        "official_extensions_loaded": [x for x in plugins if x.startswith("pylint.extensions.")],
        "official_extension_not_loaded": {"pylint.extensions.docparams": "Message-ID collisions with the clean-architecture plugin; Google docstrings validated by pydoclint, docstyle by Pylint. Not a claim of identical rule sets."},
        "kept_partial_overlaps": ["unused-variable/import: scopes differ", "protected access: inference and types differ",
                                  "clean-arch-dependency and Tach: class-level and module-level checks differ"],
        "deferred": ["Blanket ban on internal comments", "Function length in physical lines",
                     "Automatic dependency-community discovery", "Universal Protocol/ABC-only proof"],
        "not_provided": ["Formatter", "Natural-language spell checking (requires Enchant and dictionaries)",
                         "Runtime tests", "Dependency vulnerability scanner"],
    }


def configure(root: Path, *, initial: bool = False, plugins: list[str] | None = None,
              catalog: dict[str, Any] | None = None) -> list[str]:
    limits = read_limits(root)
    scope = read_scope(root)
    files = discover_files(root, scope)
    project_path = root / "pyproject.toml"
    try:
        doc = tomlkit.parse(project_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise EryxError("INVALID_PYPROJECT", str(exc)) from exc
    tool = doc.setdefault("tool", {})
    if initial:
        if not plugins:
            raise EryxError("MISSING_PLUGINS", "Discover installed Pylint extensions before configuring.")
        # Initialize missing sections only. Reinstallation preserves native policy,
        # reviewed layers, stubPath and other project-specific settings.
        tool.setdefault("pylint", base_pylint(plugins, scope))
        tool.setdefault("pyright", pyright_config(scope, files))
        tool.setdefault("pydoclint", pydoclint_config(scope))
        tool.setdefault("clean-arch", clean_config())
        tool["pylint"].setdefault("main", {})["load-plugins"] = plugins
        native_pyright = root / "pyrightconfig.json"
        if native_pyright.exists():
            for key, value in read_json(native_pyright).items():
                if tool["pyright"].get(key) != value:
                    tool["pyright"][key] = value
    elif "pylint" not in tool:
        raise EryxError("NOT_INSTALLED", "Missing [tool.pylint]; run the installer first.")
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
        catalog = read_json(root / ".eryx/pylint-catalog.json")
    rendered_project = tomlkit.dumps(doc)
    plain = tomllib.loads(rendered_project)["tool"]["pylint"]
    tests = {"tool": {"pylint": test_pylint(plain, limits["tests"], catalog)}}
    outputs: dict[str, str] = {
        "pyproject.toml": rendered_project,
        ".eryx/pylint-tests.toml": "# Test-only profile. Regenerate with eryx/apply.py.\n" + tomlkit.dumps(tests),
    }
    if "pyright" in tool:
        outputs["pyrightconfig.json"] = json.dumps(tomllib.loads(rendered_project)["tool"]["pyright"], ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tach_path = root / "tach.toml"
    if tach_path.exists() and not (root / ".eryx/architecture.generated.json").exists():
        tach = tomlkit.parse(tach_path.read_text(encoding="utf-8"))
        tach["exclude"] = exclude_globs(scope) + ["tests/**"] + [f"**/{name}/**" for name in scope["test_directory_names"]]
        outputs["tach.toml"] = tomlkit.dumps(tach)
    if initial:
        outputs[".eryx/policy-notes.json"] = json.dumps(policy_notes(plugins or []), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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


def limit_drift(root: Path) -> list[str]:
    limits = read_limits(root)
    production = load_toml(root / "pyproject.toml").get("tool", {}).get("pylint", {})
    tests = load_toml(root / ".eryx/pylint-tests.toml").get("tool", {}).get("pylint", {})
    mismatches = []
    for profile, actual in (("production", production), ("tests", tests)):
        for key, (section, option) in LIMIT_MAP.items():
            if actual.get(section, {}).get(option) != limits[profile][key]:
                mismatches.append(f"{profile}.{key}")
    return mismatches
