#!/usr/bin/env python3
"""Initialize native Tach boundaries separately from installation; review before acceptance."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
sys.dont_write_bytecode = True
from typing import Any
import tomlkit

from eryx._common import (EryxError, atomic_write, capture, emit_event, error_exit, load_toml,
                     repo_lock, require_runtime, project_dir, tool_dir, read_json, write_json)
from eryx._policy import (ALLOWED_LAYERS, LAYER_DIRS, LAYERS, exclude_globs)
from eryx._reports import normalize
from eryx.clean_architecture_layers.configured_clean_architecture_layers import ConfiguredCleanArchitectureLayers
from eryx.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy


def tool_binary(name: str) -> str:
    path = Path(sys.executable).parent / name
    if not path.is_file():
        raise EryxError("MISSING_TOOL", f"Missing {name} in project environment; run uv sync --locked --group dev.")
    return str(path)


def package_name(root: Path) -> str:
    files = ConfiguredConsumerPolicy(root).files()["production"]
    names = {Path(path).parts[1] for path in files if len(Path(path).parts) > 2}
    direct = [path for path in files if len(Path(path).parts) == 2]
    if direct or len(names) != 1:
        raise EryxError("UNSUPPORTED_LAYOUT", "Draft generation expects one import package beneath src/, e.g. src/shop/. Existing reviewed multi-package configurations are supported.")
    name = next(iter(names))
    if not name.isidentifier():
        raise EryxError("INVALID_PACKAGE", f"Invalid Python package name: {name}")
    return name


def draft_configuration(root: Path) -> tuple[dict[str, Any], list[str]]:
    policy = ConfiguredConsumerPolicy(root)
    scope = policy.scope()
    package = package_name(root)
    files = policy.files()["production"]
    # Infer only conventional directory labels, never communities from a graph.
    modules: dict[str, str] = {package: "Domain"}
    notes = []
    for filename in files:
        parts = Path(filename).parts[1:]
        for index, part in enumerate(parts[:-1]):
            if index == 0:
                continue
            if part in LAYER_DIRS:
                modules[".".join(parts[:index + 1])] = LAYER_DIRS[part]
                for depth in range(2, index + 1):
                    modules.setdefault(".".join(parts[:depth]), "Domain")
        if len(parts) >= 3 and parts[1] not in LAYER_DIRS:
            modules.setdefault(".".join(parts[:2]), "Domain")
        if parts[-1] in {"__main__.py", "bootstrap.py", "main.py"}:
            modules[".".join((*parts[:-1], Path(parts[-1]).stem))] = "Bootstrap"
    module_list = []
    interfaces = []
    for name, layer in sorted(modules.items()):
        forbidden = [other for other, other_layer in sorted(modules.items())
                     if other != name and other_layer not in ALLOWED_LAYERS[layer]]
        module_list.append({"path": name, "layer": layer, "depends_on": [],
                            "cannot_depend_on": forbidden})
        is_file = (root / "src" / (name.replace(".", "/") + ".py")).is_file()
        expose = [] if layer == "Bootstrap" else ([r".*"] if is_file else [r"api(?:\..*)?"])
        interfaces.append({"from": ["^" + re.escape(name) + "$"], "expose": expose})
    config = {
        "source_roots": ["src"], "exclude": exclude_globs(scope) + ["tests/**"] +
        [f"**/{name}/**" for name in scope["test_directory_names"]],
        "exact": True, "forbid_circular_dependencies": True,
        "ignore_type_checking_imports": False, "layers_explicit_depends_on": True,
        "root_module": "forbid", "respect_gitignore": False,
        "layers": LAYERS, "modules": module_list, "interfaces": interfaces,
        "rules": {"unused_ignore_directives": "error", "require_ignore_directive_reasons": "error",
                  "unused_external_dependencies": "error"},
    }
    if not any(layer == "UseCase" for layer in modules.values()):
        notes.append("No application/use_cases package found. Review the default Domain assignments; initialization never moves source files.")
    notes.append("Review public API patterns: by default each package exposes api.py only. Write actual allowed exports in tach.toml.")
    notes.append("tach sync records current imports, including undesirable ones. cannot_depend_on and layer checks remain active.")
    return config, notes


def sync_clean_arch(root: Path, tach: dict[str, Any]) -> None:
    ConfiguredCleanArchitectureLayers().sync(root, tach)


def validate_architecture_config(root: Path) -> dict[str, Any]:
    path = root / "tach.toml"
    if not path.exists():
        raise EryxError("ARCHITECTURE_NOT_INITIALIZED", "Run uv run --locked --group dev python eryx/init_architecture.py; review tach.toml; then use --accept.")
    config = load_toml(path)
    if not config.get("modules") or not isinstance(config["modules"], list):
        raise EryxError("ARCHITECTURE_NOT_INITIALIZED", "tach.toml needs explicit [[modules]] entries.")
    if config.get("source_roots") != ["src"]:
        raise EryxError("INVALID_ARCHITECTURE_CONFIG", "This profile requires source_roots = ['src'].")
    if not config.get("interfaces"):
        raise EryxError("INVALID_ARCHITECTURE_CONFIG", "Define public [[interfaces]] in tach.toml before accepting.")
    for module in config["modules"]:
        if not isinstance(module, dict) or not isinstance(module.get("path"), str):
            raise EryxError("INVALID_ARCHITECTURE_CONFIG", "Every Tach module needs one explicit dotted path.")
        if module.get("layer") not in LAYERS:
            raise EryxError("INVALID_ARCHITECTURE_CONFIG", f"Assign a layer to {module.get('path')}: {LAYERS}")
        if "depends_on" not in module or not isinstance(module["depends_on"], list):
            raise EryxError("INVALID_ARCHITECTURE_CONFIG", f"Explicit depends_on required for {module['path']}.")
        if module.get("unchecked") or module.get("utility"):
            raise EryxError("INVALID_ARCHITECTURE_CONFIG", "unchecked/utility modules bypass the agreed dependency restrictions.")
    required = {"exact": True, "forbid_circular_dependencies": True, "ignore_type_checking_imports": False,
                "layers_explicit_depends_on": True, "root_module": "forbid"}
    for key, value in required.items():
        if config.get(key) != value:
            raise EryxError("INVALID_ARCHITECTURE_CONFIG", f"tach.toml must set {key} = {value!r}.")
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--accept", action="store_true", help="Accept the reviewed native tach.toml (does not suppress any violations)")
    parser.add_argument("--force", action="store_true", help="Replace an existing Tach configuration with a fresh draft")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        root = project_dir(args.project)
        require_runtime()
        with repo_lock(root):
            path = root / "tach.toml"
            ready = root / ".eryx/architecture.ready.json"
            if args.accept:
                config = validate_architecture_config(root)
                result = capture([sys.executable, str(tool_dir() / "_tach_worker.py"), "check"], root)
                normalized = normalize("tach", result, root)
                raw = root / ".eryx/logs/init-check.log"
                atomic_write(raw, result["stdout"] + "\n" + result["stderr"], root)
                if normalized["status"] == "error":
                    raise EryxError("TACH_CONFIG_OR_EXECUTION_ERROR", f"Cannot validate Tach config. See {raw.relative_to(root)}.")
                sync_clean_arch(root, config)
                ConfiguredConsumerPolicy(root).apply()
                write_json(ready, {"accepted": True, "modules": len(config["modules"]),
                                   "note": "Acceptance of architecture configuration, NOT a violations baseline."}, root)
                installation_path = root / ".eryx/installation.json"
                if installation_path.exists():
                    installation = read_json(installation_path)
                    installation["architecture_initialization_required"] = False
                    write_json(installation_path, installation, root)
                emit_event("initialized", json_output=args.json, module_count=len(config["modules"]),
                           architecture_violations=len(normalized["diagnostics"]),
                           next_command="uv run --locked --group dev python eryx/check.py", exit_code=0)
                return 0
            if path.exists() and not args.force:
                emit_event("review_required", json_output=args.json, configuration="tach.toml",
                           message="Existing configuration retained. Review it and accept, or use --force to replace.",
                           next_command="uv run --locked --group dev python eryx/init_architecture.py --accept", exit_code=0)
                return 0
            config, notes = draft_configuration(root)
            atomic_write(path, "# Review before accepting; module boundaries are convention-based, not inferred communities.\n" + tomlkit.dumps(config), root)
            if ready.exists():
                ready.unlink()
            result = capture([tool_binary("tach"), "sync"], root)
            atomic_write(root / ".eryx/logs/init-sync.log", result["stdout"] + "\n" + result["stderr"], root)
            if result["error"] or result["exit_code"] != 0:
                raise EryxError("TACH_SYNC_FAILED", "Draft tach.toml exists but sync failed. See .eryx/logs/init-sync.log. Do not mark it ready until corrected.")
            config = load_toml(path)
            sync_clean_arch(root, config)
            ConfiguredConsumerPolicy(root).apply()
            emit_event("review_required", json_output=args.json, configuration="tach.toml", notes=notes,
                       next_command="uv run --locked --group dev python eryx/init_architecture.py --accept", exit_code=0)
            return 0
    except Exception as exc:
        return error_exit(exc, args.json)


if __name__ == "__main__":
    sys.exit(main())
